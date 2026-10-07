"""Готовые данные, интеграция публикации и поведение локального интерфейса."""

import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from finlib.config import settings
from finlib.report.watchlist import TEMPLATES, render
from finlib.report.watchlist_data import payload
from finlib.sources.notification_journal import MOSCOW

sys.path.insert(0, str(settings.base_dir / "eval"))
sys.path.insert(0, str(settings.base_dir / "scripts"))
import daily_run  # noqa: E402
import watchlist_run  # noqa: E402


def _rows() -> list[dict]:
    """Все корзины и полные сведения в заведомо синтетическом наборе."""
    return [
        {
            "name": f"Тест {index}",
            "inn": f"{index:010}",
            "basket": ["review", "attention", "clear", "out_of_scope"][index % 4],
            "basket_name": ["Разбор", "Внимание", "Без внимания", "Вне периметра"][index % 4],
            "subgroups": ["события", "данные" if index % 2 else "рынок"],
            "actions": ["смотреть", "проверять"],
            "grounds": [{"name": "Предмет", "details": ["Первое основание", "Второе основание"]}],
            "notes": ["Справочное обстоятельство"],
            "values": [["Долг", "123 млн руб."]],
            "sources": ["Источник основания"],
            "origin": "МСФО · консолидированная",
            "unit": "млн руб.",
            "assessed": "B",
            "report_date": "31.12.2089",
            "months": 1,
            "stale": False,
            "overdue": False,
            "chart": "<svg></svg>",
            "coverage": "Проверено",
            "bonds": True,
        }
        for index in range(93)
    ]


def _data(tmp_path: Path) -> dict:
    """Собирает данные тем же адаптером, что рабочая страница."""
    rows = _rows()
    data = payload(
        rows,
        {"эмитентов": 93, "все счётчики": 37},
        date(2090, 1, 3),
        output=tmp_path / "index.html",
        report=None,
        csv_path=None,
        cards=tmp_path / "cards",
        limitations=["Оговорка"],
        coverage="Охват",
    )
    event = {
        "name": "эмитент не установлен",
        "inn": None,
        "kind": "first",
        "kinds": ["first", "status"],
        "eventOn": "02.01.2090",
        "deliveredAt": "02.01.2090 14:29:06 МСК",
        "firstPrintedOn": "03.01.2090",
        "text": "выпуск synthetic, запись synthetic",
    }
    data["events"] = [event]
    data["late"] = [{**event, "kind": "grace", "kinds": ["grace"],
                     "text": "выпуск synthetic, запись other"}]
    data["urgentAvailable"], data["lateAvailable"] = True, True
    return data


def test_native_rows_summary_units_and_all_baskets_are_retained(tmp_path: Path) -> None:
    """Шаблон получает все сведения строки и сводку без промежуточного HTML."""
    data = _data(tmp_path)
    text = render(data)
    saved = json.loads(
        re.search(r'<script id="dataset" type="application/json">(.*?)</script>', text, re.S)[1]
    )
    assert len(saved["rows"]) == 93
    assert len(saved["baskets"]) == 4
    for field in ("grounds", "notes", "values", "sources", "unit", "chart", "actions"):
        assert saved["rows"][0][field] == _rows()[0][field]
    assert saved["stats"] == data["stats"]
    assert "/*DATA*/" not in text and "/*APP*/" not in text
    assert "06.10.2026" not in text and "file:///Users/" not in text


def test_interface_search_combined_filters_reset_pagination_and_drawer(tmp_path: Path) -> None:
    """Исполняет реальный JS установленным JavaScriptCore без браузера и сети."""
    engine = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
    if not engine.exists():
        pytest.skip("в системе нет JavaScriptCore; браузер проверяется отдельно")
    data = _data(tmp_path)
    script = tmp_path / "logic.js"
    harness = (settings.base_dir / "tests/watchlist_logic.js").read_text(encoding="utf-8")
    code = (TEMPLATES / "watchlist.js").read_text(encoding="utf-8")
    before, after = harness.split("/*APPLICATION*/")
    script.write_text(
        before.replace("/*PAYLOAD*/", json.dumps(data, ensure_ascii=False)) + code + after,
        encoding="utf-8",
    )
    result = subprocess.run([str(engine), str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "интерфейс проверен" in result.stdout


def test_links_point_only_to_existing_artifacts(tmp_path: Path) -> None:
    """Карточка и CSV остаются рабочими при отдельном расположении страницы."""
    cards = tmp_path / "saved/cards"
    cards.mkdir(parents=True)
    card = cards / "0000000000.md"
    card.write_text("синтетическая карточка", encoding="utf-8")
    csv = tmp_path / "saved/source.csv"
    csv.write_text("синтетическая выгрузка", encoding="utf-8")
    output = tmp_path / "preview/index.html"
    data = payload(
        _rows(),
        {"эмитентов": 93},
        date(2090, 1, 3),
        output=output,
        report=None,
        csv_path=csv,
        cards=cards,
        limitations=[],
        coverage="Охват",
    )
    assert (output.parent / data["rows"][0]["cardLink"]).resolve() == card.resolve()
    assert data["rows"][1]["cardLink"] is None
    assert (output.parent / data["csvLink"]).resolve() == csv.resolve()


def test_offline_csv_command_does_not_read_database_or_replace_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Реальная команда пробы не зовёт БД и защищает уже сохранённый HTML."""
    path = tmp_path / "source.csv"
    path.write_text(
        "инн;наименование;код корзины;корзина;основания\n0000000001;Тест;clear;Без внимания;\n",
        encoding="utf-8-sig",
    )
    out = tmp_path / "index.html"

    def fail() -> None:
        """БД запрещена в сценарии пробы."""
        raise AssertionError("попытка подключения к БД")

    monkeypatch.setattr(watchlist_run, "connection", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["watchlist_run.py", "--from-csv", str(path), "--as-of", "2090-01-03", "--out", str(out)],
    )
    assert watchlist_run.main() == 0
    saved = out.read_bytes()
    with pytest.raises(ValueError, match="не переписывается"):
        watchlist_run.main()
    assert out.read_bytes() == saved


def test_manual_run_without_out_leaves_scheduled_name_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ручной запуск без --out не занимает имя, которое ждёт плановый прогон."""
    from contextlib import nullcontext

    journals: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(watchlist_run, "connection", lambda: nullcontext(None))
    monkeypatch.setattr(watchlist_run, "rows_of", lambda conn, today: (_rows(), {}))
    monkeypatch.setattr(watchlist_run, "_coverage_line", lambda summary: "Охват")
    monkeypatch.setattr(watchlist_run, "_journal", lambda where, today: journals.append(where))
    monkeypatch.setattr(
        watchlist_run, "_now", lambda: datetime(2090, 1, 3, 9, 5, 7, tzinfo=MOSCOW)
    )
    monkeypatch.setattr(sys, "argv", ["watchlist_run.py", "--as-of", "2090-01-03"])
    monkeypatch.setattr(daily_run, "OUT", Path("data/output"))
    report = daily_run.report_path(date(2090, 1, 3), None, "")
    scheduled = report.with_name(report.name.replace("changes_", "watchlist_")).with_suffix(".html")
    assert scheduled == Path("data/output/watchlist_2090-01-03.html")

    assert watchlist_run.main() == 0
    manual = Path("data/output/watchlist_2090-01-03_manual_090507.html")
    assert manual.exists()
    assert not scheduled.exists()
    assert journals == [Path("data/output/watchlist_exclusions_2090-01-03_manual_090507.md")]

    monkeypatch.setattr(
        watchlist_run, "_now", lambda: datetime(2090, 1, 3, 9, 5, 8, tzinfo=MOSCOW)
    )
    assert watchlist_run.main() == 0
    assert Path("data/output/watchlist_2090-01-03_manual_090508.html").exists()
    assert not scheduled.exists()

    monkeypatch.setattr(sys, "argv", ["watchlist_run.py", "--as-of", "2090-01-03",
                                      "--out", str(scheduled)])
    assert watchlist_run.main() == 0
    assert scheduled.exists()
    assert journals[-1] == Path("data/output/watchlist_exclusions_2090-01-03.md")


def test_publication_uses_its_report_and_csv_before_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Границы интеграции проверены без исполнения daily_run или его стадий."""
    called: list[list[str]] = []

    def collect(path: str, run_name: str) -> None:
        """Записывает команду вместо выполнения любой стадии."""
        called.append(list(sys.argv))

    monkeypatch.setattr(daily_run, "OUT", tmp_path)
    monkeypatch.setattr(daily_run.runpy, "run_path", collect)
    argv = list(sys.argv)
    daily_run._publish(
        date(2090, 1, 3), [], {}, [], "", datetime(2090, 1, 3, 15, 7, tzinfo=MOSCOW), "повтор"
    )
    assert [item[0] for item in called] == [
        "change_report_run.py",
        "watchlist_csv.py",
        "issuer_card_run.py",
        "watchlist_run.py",
    ]
    assert called[0][called[0].index("--output") + 1].endswith("2090-01-03_1507.md")
    assert called[2][called[2].index("--watchlist") + 1].endswith("2090-01-03_1507.html")
    assert called[3][called[3].index("--report") + 1].endswith("2090-01-03_1507.md")
    assert called[3][called[3].index("--csv") + 1].endswith("2090-01-03_1507.csv")
    assert sys.argv == argv


def test_cards_failure_stops_list_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отказ карточек не скрывается и не выпускает список без новых ссылок."""
    called: list[str] = []
    argv = list(sys.argv)

    def fail_cards(path: str, run_name: str) -> None:
        """Только имитирует границу отказа без исполнения стадий."""
        called.append(sys.argv[0])
        if sys.argv[0] == "issuer_card_run.py":
            raise SystemExit(2)

    monkeypatch.setattr(daily_run.runpy, "run_path", fail_cards)
    with pytest.raises(SystemExit) as stopped:
        daily_run._publish(date(2090, 1, 3), [], {}, [], "", None, "")
    assert stopped.value.code == 2
    assert "watchlist_run.py" not in called
    assert sys.argv == argv
