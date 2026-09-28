"""Неполный снимок рейтингов: эмитент без ответа — «наблюдения нет», не отзыв.

25.09.2026 один таймаут на 661-м эмитенте оставил день без снимка вовсе.
Теперь эмитент без ответа уходит в `refused`, и снимок идёт дальше. Цена
этого — день, в котором по части эмитентов наблюдения нет, и прочитать
такое молчание как исчезнувший рейтинг маршрут не вправе.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from finlib.config import settings
from finlib.sources import cbonds, cbonds_events
from finlib.sources.network import NetworkDownError

sys.path.insert(0, str(settings.base_dir / "scripts"))
sys.path.insert(0, str(settings.base_dir / "eval"))

import ratings_snapshot  # noqa: E402
from change_report_run import _ratings_health  # noqa: E402
from issuer_card_run import _ratings as _card_ratings  # noqa: E402

from finlib.scoring.routing import _rating_findings, load_routing  # noqa: E402

TODAY = date(2026, 9, 25)
RATED = {"scale_id": "1", "scale_point_name": "ruC", "agency_name_rus": "Эксперт РА"}


def _network(silent: set[str]):
    """Источник, молчащий по названным ИНН и отвечающий по остальным."""

    def fetch(method: str, name: str, filters: tuple, limit: int) -> dict:
        inn = filters[0]["value"]
        if inn in silent:
            raise httpx.ReadTimeout("The read operation timed out")
        return {"items": [dict(RATED, emitent_inn=inn)]}

    return fetch


def test_a_silent_issuer_goes_to_refused_and_the_snapshot_goes_on(monkeypatch) -> None:
    """Эмитент без ответа уходит в `refused`, остальные сняты."""
    monkeypatch.setattr(ratings_snapshot.cbonds, "fetch", _network({"2"}))
    got, refused, stop = ratings_snapshot.take(["1", "2", "3"], TODAY)
    assert set(got) == {"1", "3"} and stop is None
    assert set(refused) == {"2"} and "ReadTimeout" in refused["2"]


def test_many_in_a_row_are_a_refusal_of_the_source(monkeypatch) -> None:
    """Молчат подряд многие — это отказ источника, снимок прекращается.

    Снятое до обрыва не теряется, а незапрошенные стоят в отказах
    с пометкой: маршрут возьмёт им последнее наблюдение, агент в 11:30
    дозапросит ровно их.
    """
    monkeypatch.setattr(settings, "cbonds_refused_in_row_max", 3)
    monkeypatch.setattr(ratings_snapshot.cbonds, "fetch", _network({"2", "3", "4"}))
    got, refused, stop = ratings_snapshot.take(["1", "2", "3", "4", "5"], TODAY)
    assert isinstance(stop, ratings_snapshot.SourceRefusedError)
    assert issubclass(ratings_snapshot.SourceRefusedError, cbonds.CbondsError)
    assert set(got) == {"1"}
    assert set(refused) == {"2", "3", "4", "5"}
    assert refused["5"].startswith(ratings_snapshot.NOT_ASKED)


def test_no_network_stops_the_snapshot_and_keeps_what_was_taken(
    tmp_path: Path, monkeypatch
) -> None:
    """Нет сети — снимок прерван, но файл дня лежит со снятым; прогон видит сбой.

    28.09.2026 прерванный снимок не оставил файла вовсе, хотя 474 эмитента
    были сняты.
    """

    def fetch(method: str, name: str, filters: tuple, limit: int) -> dict:
        inn = filters[0]["value"]
        if inn == "2":
            raise NetworkDownError("нет сети: проба")
        return {"items": [dict(RATED, emitent_inn=inn)]}

    monkeypatch.setattr(ratings_snapshot.cbonds, "fetch", fetch)
    monkeypatch.setattr(ratings_snapshot, "SNAPSHOTS", tmp_path)
    monkeypatch.setattr(ratings_snapshot, "issuers", lambda: {"1": "А", "2": "Б", "3": "В"})
    monkeypatch.setattr(sys, "argv", ["ratings_snapshot.py"])
    with redirect_stdout(io.StringIO()), pytest.raises(NetworkDownError):
        ratings_snapshot.main()
    found = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert set(found["issuers"]) == {"1"}
    assert found["refused"]["2"].startswith("нет сети")
    assert found["refused"]["3"].startswith(ratings_snapshot.NOT_ASKED)


def test_a_repeated_run_asks_only_the_refused(tmp_path: Path, monkeypatch) -> None:
    """Повторный запуск дозапрашивает отказы, снятое не трогает."""
    path = tmp_path / "2026-09-25.json"
    kept = [{"scale_point_name": "BBB"}]
    path.write_text(
        json.dumps({"date": "2026-09-25", "issuers": {"1": kept}, "refused": {"2": "x"}}),
        encoding="utf-8",
    )
    asked: list[str] = []

    def fetch(method: str, name: str, filters: tuple, limit: int) -> dict:
        asked.append(filters[0]["value"])
        return {"items": [RATED]}

    monkeypatch.setattr(ratings_snapshot.cbonds, "fetch", fetch)
    with redirect_stdout(io.StringIO()):
        ratings_snapshot._complete(path, TODAY)
    found = json.loads(path.read_text(encoding="utf-8"))
    assert asked == ["2"]
    assert found["issuers"]["1"] == kept
    assert found["refused"] == {} and set(found["recovered"]) == {"2"}


def _snapshots(tmp_path: Path, monkeypatch) -> None:
    """Вчера у эмитента A стоял ruC; сегодня по A и C источник промолчал."""
    monkeypatch.setattr(cbonds_events, "SNAPSHOTS", tmp_path)
    (tmp_path / "2026-09-24.json").write_text(
        json.dumps({"date": "2026-09-24", "issuers": {"A": [RATED], "B": []}}),
        encoding="utf-8",
    )
    (tmp_path / "2026-09-25.json").write_text(
        json.dumps(
            {
                "date": "2026-09-25",
                "issuers": {"B": []},
                "refused": {"A": "ReadTimeout", "C": "ReadTimeout"},
            }
        ),
        encoding="utf-8",
    )


def test_silence_is_not_read_as_a_withdrawal(tmp_path: Path, monkeypatch) -> None:
    """Молчание источника — «наблюдения нет», а не исчезнувший рейтинг."""
    _snapshots(tmp_path, monkeypatch)
    on, snapshot = cbonds_events.latest_snapshot()
    assert on == TODAY
    # Наблюдавшийся прежде берёт последнее наблюдение: рейтинг у него есть.
    assert snapshot["A"] == [RATED]
    carried = cbonds_events.events_of(
        "A", snapshot=snapshot, credit=frozenset({"1"}), order={}, defaults={}
    )
    assert carried.ratings_known and carried.live
    assert carried.left_unrated() == (False, None)
    # Не наблюдавшийся никогда — наблюдения нет: ни отзыва, ни «рейтинга нет».
    unseen = cbonds_events.events_of(
        "C", snapshot=snapshot, credit=frozenset({"1"}), order={}, defaults={}
    )
    assert not unseen.ratings_known
    assert not unseen.never_rated
    assert unseen.left_unrated() == (False, None)


def _carried(tmp_path: Path, monkeypatch, inn: str):  # noqa: ANN202
    """События эмитента по снимку с перенесёнными наблюдениями."""
    _snapshots(tmp_path, monkeypatch)
    return cbonds_events.events_of(
        inn, credit=frozenset({"1"}), order={}, defaults={}
    )


def test_a_carried_value_names_its_snapshot_in_the_ground(
    tmp_path: Path, monkeypatch
) -> None:
    """Перенесённый рейтинг в основании — с датой своего снимка, не сегодняшний."""
    events = _carried(tmp_path, monkeypatch, "A")
    assert events.ratings_observed_on == date(2026, 9, 24)
    found = _rating_findings(events, load_routing())
    assert found and "по снимку 24.09.2026" in found[0].text
    # Наблюдение дня даты снимка не приписывает: оно и есть сегодняшнее.
    today = replace(events, ratings_observed_on=None)
    assert "по снимку" not in _rating_findings(today, load_routing())[0].text


def test_a_carried_value_names_its_snapshot_in_the_card(
    tmp_path: Path, monkeypatch
) -> None:
    """Карточка печатает перенесённое значение с датой снимка."""
    events = _carried(tmp_path, monkeypatch, "A")
    said: list[str] = []
    _card_ratings(SimpleNamespace(inn="A", events=events), load_routing(), said)
    text = "\n".join(said)
    assert "снимок от 24.09.2026" in text and "Наблюдение от 24.09.2026" in text


def _said(found: dict | None) -> str:
    """Что шапка отчёта изменений говорит о снимке."""
    out = io.StringIO()
    with redirect_stdout(out):
        _ratings_health(found)
    return out.getvalue()


def test_the_report_says_the_snapshot_is_partial() -> None:
    """Шапка отчёта называет неполноту снимка числами."""
    text = _said({"issuers": {"1": [], "2": []}, "refused": {"3": "ReadTimeout"}})
    assert "Снимок рейтингов неполный: 2 из 3" in text


def test_the_report_tells_silence_from_no_network_and_not_asked() -> None:
    """Молчание источника, отсутствие сети и незапрошенные — разными строками."""
    text = _said(
        {
            "issuers": {"1": []},
            "refused": {
                "2": "ReadTimeout: timed out",
                "3": "нет сети: Cbonds — проба",
                "4": "не запрошен: снимок прерван — нет сети",
                "5": "не запрошен: снимок прерван — нет сети",
            },
        }
    )
    assert "Снимок рейтингов неполный: 1 из 5" in text
    assert "источник не ответил — 1" in text
    assert "не было сети у нас — 1" in text
    assert "не запрошены: снимок прерван — 2" in text


def test_the_report_says_the_snapshot_is_missing() -> None:
    """Снимка за день нет — так и сказано; полный снимок не упоминается."""
    assert "Снимка рейтингов за этот день нет" in _said(None)
    assert _said({"issuers": {"1": []}, "refused": {}}) == ""
