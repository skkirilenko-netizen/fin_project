"""Нет сети у нас — не отказ источника: пережидается, называется своим именем.

28.09.2026 у машины пропала сеть, и снимок рейтингов записал пять
`ConnectError` подряд (имя хоста не разрешилось) отказом Cbonds: прогон дня
остановился с «источник отказал», хотя до источника не дошёл ни один запрос.
"""

import errno
import io
import socket
import sys
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest

from finlib.config import settings
from finlib.sources import cbonds, network

sys.path.insert(0, str(settings.base_dir / "eval"))
sys.path.insert(0, str(settings.base_dir / "scripts"))

from change_report_run import _health  # noqa: E402


def _dns() -> httpx.ConnectError:
    """Сбой разрешения имени, как его поднимает httpx: причина — gaierror."""
    failure = httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
    failure.__cause__ = socket.gaierror(8, "nodename nor servname provided, or not known")
    return failure


def _os(code: int) -> httpx.ConnectError:
    """Сбой соединения с ошибкой сокета `code` в причине."""
    failure = httpx.ConnectError(f"[Errno {code}]")
    failure.__cause__ = OSError(code, "проба")
    return failure


def test_our_side_is_told_from_the_far_side() -> None:
    """Имя не разрешилось и сеть недоступна — у нас; сброс и таймаут — нет."""
    assert network.network_down(_dns())
    assert network.network_down(_os(errno.ENETUNREACH))
    assert network.network_down(_os(errno.EHOSTUNREACH))
    # Сброс соединения — узел на той стороне нас услышал.
    assert not network.network_down(_os(errno.ECONNRESET))
    assert not network.network_down(httpx.ReadTimeout("timed out"))


@pytest.fixture
def _paused(monkeypatch) -> list[float]:
    """Паузы записываются, а не выжидаются."""
    pauses: list[float] = []
    monkeypatch.setattr(network.time, "sleep", pauses.append)
    monkeypatch.setattr(settings, "network_retries", 3)
    monkeypatch.setattr(settings, "network_retry_pause_s", 60.0)
    return pauses


def _flaky(outcomes: list):  # noqa: ANN202
    """Обращение, отдающее исходы по очереди: исключение поднимается."""
    calls: list[int] = []

    def call() -> str:
        outcome = outcomes[len(calls)]
        calls.append(1)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return call, calls


def test_the_network_is_waited_for_in_minutes(_paused: list[float]) -> None:
    """Сеть вернулась на третьей попытке — ответ получен, паузы в минутах."""
    call, calls = _flaky([_dns(), _dns(), "ответ"])
    assert network.send(call, "проба") == "ответ"
    assert len(calls) == 3
    assert _paused == [60.0, 120.0]


def test_no_network_after_all_retries_is_named_so(_paused: list[float]) -> None:
    """Повторы исчерпаны — «нет сети», а не сбой источника."""
    call, calls = _flaky([_dns()] * 4)
    with pytest.raises(network.NetworkDownError, match="нет сети"):
        network.send(call, "проба")
    assert len(calls) == 4
    assert not issubclass(network.NetworkDownError, cbonds.CbondsError)
    assert not issubclass(network.NetworkDownError, httpx.HTTPError)


def test_a_far_side_failure_is_not_retried_here(_paused: list[float]) -> None:
    """Сбой на той стороне поднимается как был: его судит клиент источника."""
    call, calls = _flaky([httpx.ReadTimeout("timed out"), "ответ"])
    with pytest.raises(httpx.ReadTimeout):
        network.send(call, "проба")
    assert len(calls) == 1 and _paused == []


def test_cbonds_does_not_count_an_unsent_request(
    tmp_path: Path, monkeypatch, _paused: list[float]
) -> None:
    """Запрос, не ушедший с машины, в суточную норму источника не идёт."""
    monkeypatch.setattr(cbonds, "CACHE", tmp_path)
    monkeypatch.setattr(cbonds, "pace", cbonds.Pace())
    monkeypatch.setattr(cbonds.settings, "cbonds_login", "проба")
    monkeypatch.setattr(cbonds.settings, "cbonds_password", "проба")
    outcomes: list = [_dns()]

    def post(url: str, **kwargs) -> httpx.Response:  # noqa: ANN003
        if outcomes:
            raise outcomes.pop()
        return httpx.Response(
            200, text='{"items": [], "total": 0}', request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(cbonds.httpx, "post", post)
    cbonds.fetch("get_rating_emitent_maxdate", "проба_сети")
    assert cbonds.pace.requested == 1
    assert _paused == [60.0]


def test_the_daily_run_names_no_network_as_such(tmp_path: Path, monkeypatch) -> None:
    """Стадия без сети — «offline» с причиной, а не «failed»."""
    import daily_run

    script = tmp_path / "stage.py"
    script.write_text(
        "from finlib.sources.network import NetworkDownError\n"
        "raise NetworkDownError('нет сети: проба')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(daily_run, "_marker", lambda stage: tmp_path / "нет.json")
    stage = daily_run.Stage(
        code="probe", name="проба", script=str(script), every=1,
        source="cbonds", why="тест",
    )
    said = daily_run._run_stage(stage, dry=False)
    assert said["status"] == "offline" and "нет сети" in said["why"]


def test_the_report_says_no_network_not_a_refusal() -> None:
    """Отчёт изменений называет «нет сети» и не говорит «источник отказал»."""
    out = io.StringIO()
    with redirect_stdout(out):
        _health(
            "run",
            [
                {
                    "status": "done",
                    "note": "эмитентов 901",
                    "sources": [
                        {"code": "ratings", "name": "снимок рейтингов",
                         "status": "offline", "why": "нет сети: Cbonds — проба"},
                    ],
                }
            ],
        )
    text = out.getvalue()
    assert "Доставка неполна" in text and "нет сети" in text
    assert "отказал" not in text
