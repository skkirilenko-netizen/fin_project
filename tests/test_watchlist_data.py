"""Сохранённые сведения интерфейса на синтетических отчётах и CSV."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from finlib.report.watchlist_data import csv_rows, json_text, payload, report_data
from finlib.sources.default_notifications import Notice
from finlib.sources.notification_journal import MOSCOW, Context, publish


def test_saved_rows_and_unknown_issuer_survive_index_failure(tmp_path: Path) -> None:
    """Интерфейс читает подтверждённую квитанцию отчёта и точные сохранённые строки."""
    first = ("- эмитент не установлен: купон, выпуск test, запись key: "
             "запись впервые обнаружена 02.01.2090; снимок доставлен: "
             "02.01.2090 14:29:06 МСК; впервые выведено 03.01.2090")

    def render(context: Context) -> str:
        """Публикует синтетический первоначальный текст уведомления."""
        context.claim(Notice("key", "test", "first_seen", date(2090, 1, 2)), first)
        return "# Что изменилось: 03.01.2090\n## Доставлено с опозданием: 1\n" + first

    path = tmp_path / "changes_2090-01-03.md"
    publish(path, render, printed_at=datetime(2090, 1, 3, tzinfo=MOSCOW))
    (tmp_path / "default_notification_journal.json").unlink()
    item = report_data(path)["late"][0]
    assert item["line"] == first
    assert item["inn"] is None and item["eventOn"] == "02.01.2090"
    assert item["deliveredAt"] == "02.01.2090 14:29:06 МСК"
    assert item["firstPrintedOn"] == "03.01.2090"


def test_report_delivery_ratings_and_corrections_are_not_silently_lost(tmp_path: Path) -> None:
    """Доставка, рейтинговое действие, уточнение и недельная сводка сохраняются порознь."""
    path = tmp_path / "changes_2090-01-03.md"
    path.write_text(
        "# Что изменилось: 03.01.2090\n"
        "*Доставки дня: снимок рейтингов — done, дефолты — cached. Прогон — «done».*\n"
        "> **Снимок рейтингов неполный: 2 из 3.**\n\n"
        "## Срочное за сутки (02.01.2090 → 03.01.2090): 1\n"
        "- Тест (0000000001): рейтинг отозван 03.01.2090\n"
        "Уточнения сведений источника — отдельно от срочных событий:\n"
        "- Тест: уточнение в снимке\n"
        "## За сутки сменили корзину: 2 из 3\n"
        "## За неделю: сводка\n| Тест | Внимание |\n",
        encoding="utf-8",
    )
    data = report_data(path)
    assert len(data["events"]) == 1 and data["events"][0]["kind"] == "rating"
    assert data["dailyChanges"] == 2
    assert data["sources"][1]["status"] == "cached"
    assert "2 из 3" in data["warnings"][0]
    assert "не ноль" in data["warnings"][-1]
    assert any("- Тест: уточнение в снимке" in section["lines"] for section in data["sections"])


def test_missing_and_zero_are_distinct_and_bad_counter_stops(tmp_path: Path) -> None:
    """Отсутствующий раздел не объявляется проверенным нулём; расходящийся счётчик — ошибка."""
    assert report_data(None)["dailyChanges"] is None
    path = tmp_path / "changes_2090-01-03.md"
    path.write_text("## Доставлено с опозданием: 0\n", encoding="utf-8")
    assert report_data(path)["lateAvailable"] is True
    path.write_text("## Доставлено с опозданием: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="не совпадает"):
        report_data(path)


def test_csv_probe_preserves_inn_amounts_units_and_every_column(tmp_path: Path) -> None:
    """Пробы читают готовую выгрузку, сохраняя строковый ИНН и пустое раскрытие."""
    path = tmp_path / "source.csv"
    path.write_text(
        "инн;наименование;код корзины;корзина;основания;единица;"
        "net_debt;cur_liq;дополнительная графа\n"
        "0000000001;Тест;out_of_scope;Вне периметра;;млн руб.;123,45 млн руб.;;сохранить\n",
        encoding="utf-8-sig",
    )
    rows, summary = csv_rows(path)
    assert rows[0]["inn"] == "0000000001"
    assert rows[0]["values"] == [["net_debt", "123,45 млн руб."]]
    assert rows[0]["csvFields"]["дополнительная графа"] == "сохранить"
    assert rows[0]["bonds"] is None and summary["с выпусками в обращении"] == "неизвестно"
    data = payload(
        rows,
        summary,
        date(2090, 1, 3),
        output=tmp_path / "index.html",
        report=None,
        csv_path=path,
        cards=tmp_path / "cards",
        limitations=[],
        coverage="Проба CSV",
    )
    assert data["rows"][0]["cardLink"] is None
    assert data["csvLink"] == "source.csv" and data["reportLink"] is None
    assert data["rows"][0]["bonds"] == "?"
    with pytest.raises(ValueError, match="повторяется"):
        payload(
            rows * 2,
            summary,
            date(2090, 1, 3),
            output=tmp_path / "index.html",
            report=None,
            csv_path=path,
            cards=tmp_path,
            limitations=[],
            coverage="",
        )


def test_inert_json_cannot_close_script_or_inject_markup() -> None:
    """Имена и сведения источника остаются данными при HTML-содержимом в строке."""
    raw = {"name": "</script><script>alert(1)</script>", "text": "<>&"}
    encoded = json_text(raw)
    assert "<" not in encoded and "&" not in encoded
    assert json.loads(encoded) == raw
