"""Тесты сборки контекста: модель получает только посчитанное."""

from datetime import date
from decimal import Decimal

import pytest

from finlib.llm.context import build_context, format_metric, money, ratio
from finlib.llm.verify import verify
from finlib.metrics.definitions import Unit, load_metrics

INN = "7736050003"
SIMPLIFIED_INN = "2100010824"


@pytest.fixture(scope="module")
def context():
    """Контекст по отчётности Газпрома из рабочей базы."""
    return build_context(INN)


# --- форматирование ---------------------------------------------------------


def test_money_uses_group_separators() -> None:
    """Денежные величины подаются с разделителями разрядов и без дробей."""
    assert money(Decimal("25736328136")) == "25 736 328 136"
    assert money(Decimal("1234.6")) == "1 235"


def test_ratio_is_rounded_to_two_places() -> None:
    """Коэффициенты округляются заранее: у модели нет повода считать самой."""
    assert ratio(Decimal("0.8212956762")) == "0,82"
    assert ratio(Decimal("-2.88")) == "-2,88"


def test_format_metric_by_unit() -> None:
    """Каждая единица измерения оформляется по-своему."""
    assert format_metric(Decimal("0.82"), Unit.RATIO) == "0,82"
    assert format_metric(Decimal("63.0971"), Unit.DAYS) == "63,1 дн."
    assert "тыс. руб." in format_metric(Decimal("1000"), Unit.THOUSAND_RUB)


# --- состав блоков ----------------------------------------------------------


def test_all_six_blocks_present(context) -> None:
    """В контексте шесть блоков, как требует шаблон."""
    blocks = context.blocks()
    for name in ("ОРГАНИЗАЦИЯ", "ДАННЫЕ", "ПОКАЗАТЕЛИ", "ФЛАГИ", "ОЦЕНКА", "ОГРАНИЧЕНИЯ"):
        assert f"=== {name}" in blocks


def test_organisation_block_has_requisites(context) -> None:
    """Реквизиты и происхождение отчётности названы."""
    assert INN in context.organization
    assert "ГАЗПРОМ" in context.organization
    assert "РСБУ" in context.organization
    assert "тысячи рублей" in context.organization


def test_data_block_ties_numbers_to_line_codes(context) -> None:
    """Каждое число фактической базы идёт со своим кодом строки."""
    assert "1600" in context.data
    assert "1300" in context.data
    assert "БАЛАНС (актив)" in context.data


def test_metrics_block_names_codes(context) -> None:
    """Показатели идут со своими кодами и наименованиями."""
    assert "cur_liq" in context.metrics
    assert "Коэффициент текущей ликвидности" in context.metrics
    assert "equity_ratio" in context.metrics


def test_not_calculable_metrics_come_with_reasons() -> None:
    """Нерассчитанные показатели названы вместе с причиной.

    У упрощённой отчётности их много: часть строк там не раскрывается.
    """
    metrics = build_context(SIMPLIFIED_INN).metrics
    assert "Не рассчитаны:" in metrics
    assert "Не раскрыты строки" in metrics or "не интерпретируется" in metrics


def test_assessment_block_has_class_and_groups(context) -> None:
    """Класс, уверенность и баллы групп переданы модели."""
    assert "Класс" in context.assessment
    assert "Уверенность в оценке" in context.assessment
    assert "Баллы по группам" in context.assessment


def test_limitations_block_has_calibration_note(context) -> None:
    """Постоянный пункт об отсутствии отраслевой привязки на месте."""
    assert "отраслевой привязки" in context.limitations


def test_flags_block_carries_ready_text(context) -> None:
    """Текст оговорки по флагу передан готовым, собирать его модели не нужно."""
    assert "холдинговой структуры" in context.flags


# --- инварианты -------------------------------------------------------------


def test_model_gets_only_computed_values(context) -> None:
    """Исходный файл отчётности модели не передаётся."""
    blocks = context.blocks()
    assert "data/raw" not in blocks
    assert "current1600" not in blocks, "сырые атрибуты источника не просачиваются"
    assert "girbo" not in blocks.lower()


def test_methodology_notes_never_reach_the_prompt(context) -> None:
    """Условная оговорка методики в контекст не идёт ни одним полем.

    «В упрощённой отчётности не рассчитывается» — свойство методики, а не факт
    об организации. Поданная рядом с посчитанным значением, она читается как
    утверждение: модель написала так про Газпром, который сдаёт полную
    отчётность, и постпроверка чисел этого не поймала.
    """
    blocks = context.blocks()
    checked = 0
    for metric in load_metrics().metrics:
        if not metric.methodology_note:
            continue
        checked += 1
        fragment = " ".join(metric.methodology_note.split())[:60]
        assert fragment not in blocks, f"{metric.code}: описание методики попало в промпт"
    assert checked >= 5, "в методике не осталось условных оговорок — проверка выродилась"


def test_metrics_block_states_only_facts(context) -> None:
    """В блоке ПОКАЗАТЕЛИ нет оговорок — только значения и причины отказа."""
    assert "оговорка" not in context.metrics
    assert "не рассчитывается" not in context.metrics


def test_unconditional_notes_still_reach_limitations(context) -> None:
    """Безусловная оговорка о содержании показателя из заключения не исчезла."""
    assert "чистые активы по методике Минфина" in context.limitations


def test_context_is_self_consistent_for_verification(context) -> None:
    """Числа самого контекста проходят постпроверку против него же.

    Иначе модель не смогла бы процитировать даже то, что ей дали.
    """
    result = verify(context.data, context.blocks())
    assert result.verified, result.foreign_values


def test_quoting_context_numbers_passes(context) -> None:
    """Цитирование величины из блоков проверку проходит."""
    answer = "Валюта баланса (строка 1600) — 25 736 328 136 тыс. руб."
    assert verify(answer, context.blocks()).verified


def test_invented_number_fails_against_context(context) -> None:
    """Выдуманное число против реального контекста отклоняется."""
    answer = "Рентабельность инвестиций составила 18,3 %."
    assert not verify(answer, context.blocks()).verified


def test_simplified_organisation_reports_no_class() -> None:
    """Если класс не присвоен, в блоке ОЦЕНКА названа причина."""
    context = build_context(SIMPLIFIED_INN)
    assert "Класс не присвоен" in context.assessment
    assert "одной группой" in context.assessment


def test_unknown_organisation_raises() -> None:
    """По организации без расчётов контекст не собирается молча."""
    with pytest.raises(ValueError, match="нет рассчитанных показателей"):
        build_context("0000000000")


def test_report_date_matches_latest_period(context) -> None:
    """Контекст собирается за самый свежий рассчитанный период."""
    assert context.report_date == date(2025, 12, 31)
