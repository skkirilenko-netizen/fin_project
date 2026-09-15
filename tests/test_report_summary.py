"""Тесты раздела «Ключевой вывод» и правил раскрытия балла.

Класса нет у двух организаций из трёх — это штатный исход, а не сбой.
Главное правило: без класса балл не приводится нигде, ни в разделе 1,
ни в приложении.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.report.appendix import groups_table, provenance
from finlib.report.data import ReportData, load_report_data
from finlib.report.summary import build_summary
from finlib.scoring.definitions import load_scoring
from finlib.standards import Standard

FULL_INN = "7736050003"
SIMPLE_INN = "2100010824"
CORRECTED_INN = "2522002003"

SCORING = load_scoring()


@pytest.fixture
def with_class(db_conn) -> ReportData:
    """Организация с присвоенным классом и сработавшим стоп-фактором."""
    return load_report_data(FULL_INN, db_conn)


@pytest.fixture
def without_class(db_conn) -> ReportData:
    """Организация, которой класс не присвоен."""
    return load_report_data(SIMPLE_INN, db_conn)


def text_of(data: ReportData) -> str:
    """Раздел 1 одной строкой."""
    return "\n".join(item.text for item in build_summary(data, SCORING))


# --- класс присвоен ---------------------------------------------------------


def test_class_is_stated_when_assigned(with_class) -> None:
    """Присвоенный класс называется вместе с наименованием."""
    assert with_class.class_code
    assert f"Класс финансового состояния: {with_class.class_code}" in text_of(with_class)


def test_stop_factor_is_stated(with_class) -> None:
    """Сработавший стоп-фактор выносится в ключевой вывод."""
    assert with_class.stop_factor_code
    assert "Сработал стоп-фактор" in text_of(with_class)


def test_score_is_withheld_when_stop_factor_fired(with_class) -> None:
    """При стоп-факторе балл в раздел 1 не выносится, но в приложении есть."""
    assert with_class.stop_factor_code
    assert with_class.score_in_appendix
    assert not with_class.score_in_summary
    assert "Балл:" not in text_of(with_class)
    assert any(
        "до применения стоп-фактора" in line
        for line in provenance(with_class, "модель", _now())
    )


# --- класс не присвоен ------------------------------------------------------


def test_missing_class_is_reported_with_reason(without_class) -> None:
    """Вместо класса приводится причина отказа."""
    assert without_class.class_code is None
    text = text_of(without_class)
    assert "Класс финансового состояния не присвоен" in text
    assert without_class.assessment["no_class_reason"] in text


def test_missing_metrics_are_listed(without_class) -> None:
    """Перечисляются показатели, которых не хватило."""
    assert without_class.missing_metrics
    text = text_of(without_class)
    for metric in without_class.missing_metrics:
        assert metric.name in text


def test_methodology_exclusions_are_not_called_missing(without_class) -> None:
    """Исключение методикой не выдаётся за пробел в отчётности."""
    text = text_of(without_class)
    assert "Не участвуют в балльной оценке по методике" in text
    assert "решение методики, а не пробел в отчётности" in text


def test_score_is_absent_everywhere_without_class(without_class) -> None:
    """Без класса балла нет ни в разделе 1, ни в приложении, ни в разложении.

    Балл 67 при отрицательном собственном капитале арифметически верен,
    но рядом с отказом присвоить класс читается как противоречие.
    """
    assert not without_class.score_in_summary
    assert not without_class.score_in_appendix
    assert groups_table(without_class) is None

    score = f"{without_class.assessment['total_score']:.2f}".replace(".", ",")
    text = text_of(without_class)
    assert score not in text
    assert "Балл:" not in text
    assert all(score not in line for line in provenance(without_class, "м", _now()))


def test_group_scores_are_withheld_without_class(without_class) -> None:
    """Балл группы тоже не приводится: он описывает часть картины."""
    text = text_of(without_class)
    scored = [item for item in without_class.groups if item["score"] is not None]
    assert scored, "у организации есть группы с посчитанным баллом"
    for group in scored:
        rendered = f"{group['score']:.2f}".replace(".", ",")
        assert rendered not in text, group["group_name"]
    assert "Расчёт оказался возможен только по группам" in text


def test_group_names_are_still_named(without_class) -> None:
    """Названия групп остаются: читателю нужно знать, что вообще считалось."""
    text = text_of(without_class)
    scored = [item for item in without_class.groups if item["score"] is not None]
    for group in scored:
        assert group["group_name"] in text


# --- общее ------------------------------------------------------------------


def test_confidence_is_always_stated(with_class, without_class) -> None:
    """Уверенность приводится в обоих случаях."""
    for data in (with_class, without_class):
        assert "Уверенность в оценке:" in text_of(data)


def test_confidence_reasons_are_listed(without_class) -> None:
    """Что ограничивает уверенность — перечисляется."""
    reasons = without_class.assessment["confidence_reasons"] or []
    assert reasons
    text = text_of(without_class)
    for reason in reasons:
        assert reason in text


def test_flag_text_is_carried_whole(with_class) -> None:
    """Текст флага передаётся готовым и не сокращается."""
    assert with_class.flags
    text = text_of(with_class)
    for flag in with_class.flags:
        assert flag["message"] in text


def test_provenance_carries_versions(with_class) -> None:
    """В приложении есть версии методики, модель и дата."""
    lines = provenance(with_class, "qwen3", _now())
    joined = "\n".join(lines)
    assert with_class.assessment["metrics_version"] in joined
    assert with_class.assessment["scoring_version"] in joined
    assert with_class.assessment["flags_version"] in joined
    assert "qwen3" in joined
    assert "Дата формирования" in joined


def test_report_data_is_bound_to_one_standard(db_conn) -> None:
    """Выборка идёт в пределах одного стандарта."""
    data = load_report_data(CORRECTED_INN, db_conn, standard=Standard.RSBU)
    assert data.standard is Standard.RSBU
    assert data.periods


def test_missing_and_methodology_exclusions_do_not_overlap(without_class) -> None:
    """Показатель не может быть одновременно недостающим и исключённым методикой."""
    missing = {item.code for item in without_class.missing_metrics}
    excluded = {item.code for item in without_class.excluded_by_methodology}
    assert missing
    assert excluded
    assert not (missing & excluded)


def _now():
    """Момент формирования для тестов."""
    from datetime import datetime

    return datetime(2026, 1, 1, 12, 0)


def test_score_rule_is_a_pure_function_of_class_and_stop() -> None:
    """Правило раскрытия балла не зависит от значения самого балла."""
    base = dict(
        inn="1", report_date=date(2025, 12, 31), standard=Standard.RSBU, organization={}
    )
    high = {"total_score": Decimal("99.00"), "class_code": None, "stop_factor_code": None}
    assert not ReportData(**base, assessment=high).score_in_appendix

    ok = {"total_score": Decimal("10.00"), "class_code": "E", "stop_factor_code": None}
    data = ReportData(**base, assessment=ok)
    assert data.score_in_appendix and data.score_in_summary

    stopped = {"total_score": Decimal("85.00"), "class_code": "E", "stop_factor_code": "x"}
    data = ReportData(**base, assessment=stopped)
    assert data.score_in_appendix and not data.score_in_summary
