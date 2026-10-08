"""Повтор стадий, упавших по таймауту или обрыву сети, внутри прогона.

05.10.2026 медленный Cbonds держался дольше пауз клиента: снимок рейтингов —
пять эмитентов подряд без ответа, дефолты — `ReadTimeout`, и день был потерян
целиком. Решение владельца 07.10.2026: один повтор через паузу, только
таймауты и обрывы сети; не дошла и после повтора — прогон `failed`.
"""

import sys
from datetime import date

import httpx
import pytest

from finlib.config import settings
from finlib.sources import cbonds
from finlib.sources.network import NetworkDownError, transient

sys.path.insert(0, str(settings.base_dir / "scripts"))

import daily_run  # noqa: E402
import ratings_snapshot  # noqa: E402

TODAY = date(2026, 10, 5)


def _refused(cause: BaseException) -> ratings_snapshot.SourceRefusedError:
    """Отказ снимка рейтингов с причиной последнего эмитента, как в `take`."""
    stop = ratings_snapshot.SourceRefusedError("источник не ответил по 5 эмитентам подряд")
    stop.__cause__ = cause
    return stop


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("The read operation timed out"),
        httpx.ConnectTimeout("timed out"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.ReadError("connection reset"),
        NetworkDownError("нет сети"),
        _refused(httpx.ReadTimeout("timed out")),
        httpx.ConnectError("Connection refused"),
        cbonds.CbondsError("Cbonds get_emissions: 503 — сервис недоступен", status=503),
        _refused(cbonds.CbondsError("Cbonds get_rating_emitent_maxdate: 502", status=502)),
    ],
)
def test_temporary_failures_are_transient(failure: BaseException) -> None:
    """Таймаут, обрыв и отказ в соединении, ответ 5xx — в том числе причиной отказа снимка."""
    assert transient(failure)


@pytest.mark.parametrize(
    "failure",
    [
        cbonds.CbondsError("Cbonds get_emissions: 403 — доступ запрещён", status=403),
        cbonds.CbondsError("Cbonds get_emissions: 429 — предел", status=429),
        _refused(cbonds.CbondsError("Cbonds get_rating_emitent_maxdate: 400", status=400)),
        cbonds.CbondsError("Cbonds get_emissions: в ответе нет `items`"),
        httpx.UnsupportedProtocol("Request URL is missing a scheme"),
        ValueError("разбор ответа"),
    ],
)
def test_client_errors_and_our_defects_are_not_transient(failure: BaseException) -> None:
    """Ответ 4xx, ответ без записей и наш дефект повтором не лечатся."""
    assert not transient(failure)


def _script(failures: dict[str, list[BaseException | None]]):
    """Подмена `runpy`: по скрипту стадии — очередь исходов попыток."""

    def run(path: str, run_name: str) -> None:
        code = next(
            stage.code for stage in daily_run.STAGES if path.endswith(stage.script)
        )
        queue = failures.get(code) or [None]
        failure = queue.pop(0) if len(queue) > 1 else queue[0]
        if failure is not None:
            raise failure

    return run


@pytest.fixture
def _quiet(monkeypatch) -> list[float]:
    """Стадии не ходят в сеть и не смотрят на диск; паузы записываются."""
    pauses: list[float] = []
    monkeypatch.setattr(daily_run.time, "sleep", pauses.append)
    monkeypatch.setattr(daily_run, "_fresh", lambda path, every, today: False)
    # Файл доставки «обновлён» всегда: судим исход, а не «cached».
    stamps = iter(range(10_000))
    monkeypatch.setattr(daily_run, "_stamp", lambda path: float(next(stamps)))
    monkeypatch.setattr(settings, "stage_retry_pause_s", 1200.0)
    return pauses


def _by_code(delivered: list[dict]) -> dict[str, dict]:
    return {item["code"]: item for item in delivered}


def test_a_timed_out_stage_is_retried_once_after_the_pause(
    _quiet: list[float], monkeypatch
) -> None:
    """Таймаут дефолтов — одна пауза, повтор удался: стадия `done`, первая попытка записана."""
    monkeypatch.setattr(
        daily_run.runpy,
        "run_path",
        _script({"defaults": [httpx.ReadTimeout("timed out"), None]}),
    )
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    got = _by_code(delivered)["defaults"]
    assert _quiet == [1200.0]
    assert got["status"] == "done" and got["retries"] == 1
    assert got["first"]["status"] == "failed"
    assert "ReadTimeout" in got["first"]["error"]
    assert all("retries" not in item for item in delivered if item["code"] != "defaults")
    assert daily_run._shortfall(delivered) == ""


def test_one_pause_for_all_retried_stages(_quiet: list[float], monkeypatch) -> None:
    """Рейтинги без сети и дефолты по таймауту — пауза одна, повторены обе."""
    monkeypatch.setattr(
        daily_run.runpy,
        "run_path",
        _script(
            {
                "ratings": [NetworkDownError("нет сети"), None],
                "defaults": [httpx.ReadTimeout("timed out"), None],
            }
        ),
    )
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    assert _quiet == [1200.0]
    got = _by_code(delivered)
    assert got["ratings"]["first"]["status"] == "offline"
    assert got["ratings"]["status"] == got["defaults"]["status"] == "done"
    assert [item["code"] for item in delivered] == [s.code for s in daily_run.STAGES]


def test_failed_again_fails_the_run(_quiet: list[float], monkeypatch) -> None:
    """Не дошла и после повтора — прогон `failed`, второго повтора нет."""
    monkeypatch.setattr(
        daily_run.runpy,
        "run_path",
        _script({"defaults": [httpx.ReadTimeout("timed out")]}),
    )
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    got = _by_code(delivered)["defaults"]
    assert _quiet == [1200.0]
    assert got["status"] == "failed" and got["retries"] == 1
    assert daily_run._shortfall(delivered) == "доставка неполна: перечень дефолтов — отказ"


def test_a_server_error_is_retried(_quiet: list[float], monkeypatch) -> None:
    """Ответ 5xx после попыток клиента — временный сбой: повтор через паузу."""
    monkeypatch.setattr(
        daily_run.runpy,
        "run_path",
        _script({"defaults": [cbonds.CbondsError("Cbonds get_defaults: 503", status=503), None]}),
    )
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    got = _by_code(delivered)["defaults"]
    assert _quiet == [1200.0] and got["status"] == "done" and got["retries"] == 1


@pytest.mark.parametrize(
    "failure",
    [
        cbonds.CbondsError("Cbonds get_defaults: 403 — доступ запрещён", status=403),
        SystemExit(1),
    ],
)
def test_answers_and_silent_exits_are_not_retried(
    _quiet: list[float], monkeypatch, failure: BaseException
) -> None:
    """Ответ 4xx и ненулевой код без причины не повторяются: пауз нет."""
    monkeypatch.setattr(
        daily_run.runpy, "run_path", _script({"defaults": [failure]})
    )
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    got = _by_code(delivered)["defaults"]
    assert _quiet == []
    assert got["status"] == "failed" and "retries" not in got


def test_retry_respects_the_quota(_quiet: list[float], monkeypatch) -> None:
    """Норма Cbonds исчерпана к повтору — стадия `no_quota`, источник не спрашивается."""
    ran: list[str] = []
    spent = {"now": 0}
    # Норма кончается за время паузы: до неё стадии идут, после — нет.
    monkeypatch.setattr(
        daily_run.time, "sleep", lambda pause: spent.update(now=daily_run.DAILY_QUOTA)
    )
    monkeypatch.setattr(daily_run, "_spent", lambda: spent["now"])

    def run(path: str, run_name: str) -> None:
        ran.append(path)
        if path.endswith("defaults_fetch.py"):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(daily_run.runpy, "run_path", run)
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    got = _by_code(delivered)["defaults"]
    assert got["status"] == "no_quota" and got["retries"] == 1
    assert sum(path.endswith("defaults_fetch.py") for path in ran) == 1


def test_dry_run_does_not_retry(_quiet: list[float]) -> None:
    """Прогон без обращений к источникам не ждёт и не повторяет."""
    delivered: list[dict] = []
    daily_run._deliver(TODAY, True, delivered)
    assert _quiet == []
    assert {item["status"] for item in delivered} == {"skipped"}
