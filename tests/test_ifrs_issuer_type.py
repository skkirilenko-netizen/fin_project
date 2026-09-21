"""Тесты типа эмитента и применимости стоп-факторов (задача 26).

Случаи с живых комплектов: у Автодора расчёты с Принципалом и покрытие
процентов, которое измеряет устройство расчётов, а не способность
обслуживать долг; у ФосАгро отрицательный оборотный капитал при покрытии
5,7; у ЛСР ликвидность 4,15 при недоступных средствах на счетах эскроу;
у Сегежи стоп-факторы подтверждены разделом заключения о непрерывности.
"""

from decimal import Decimal

import pytest

from finlib.normalize.ifrs_issuer_type import load_ifrs_metrics, load_issuer_types
from finlib.sources.ifrs_issuer_type import (
    Consistency,
    Determination,
    applicability,
    consistency,
    determine_type,
)

PRINCIPAL_TEXT = (
    "Государственная компания создана в соответствии с Федеральным законом. "
    "Задолженность Принципала. Концессионные соглашения. Целевое финансирование."
)


def test_type_is_determined_by_structure_not_by_words() -> None:
    """Слова о концессиях без статей типа не дают.

    У ЛСР дважды сказано «кредитная организация» — речь о банках, дающих
    проектное финансирование, — и по тексту он стал бы финансовым.
    """
    policy = load_issuer_types()
    found = determine_type({}, PRINCIPAL_TEXT, policy)
    assert found.code == "corporate"
    assert found.determination is Determination.DEFAULT


def test_structural_feature_with_confirmation_gives_the_type() -> None:
    """Статья расчётов с Принципалом плюс подтверждения в тексте — тип."""
    policy = load_issuer_types()
    found = determine_type(
        {"ifrs.agency_principal_receivable": Decimal(856349)}, PRINCIPAL_TEXT, policy
    )
    assert found.code == "quasi_sovereign"
    assert found.determination is Determination.STRUCTURAL
    assert found.needs_confirmation


def test_structure_without_confirmation_is_not_enough() -> None:
    """Статья есть, подтверждений в тексте нет — тип не присваивается."""
    policy = load_issuer_types()
    found = determine_type(
        {"ifrs.agency_principal_receivable": Decimal(1)}, "Выручка и расходы", policy
    )
    assert found.code == "corporate"


def test_stop_factor_is_not_applied_by_type() -> None:
    """Покрытие процентов к квазисуверенной структуре не применяется."""
    policy = load_issuer_types()
    found = applicability(
        "interest_cover_below_one", "quasi_sovereign", {}, policy
    )
    assert not found.applicable
    assert found.kind == "by_type"
    assert "не применён" in found.limitation


def test_stop_factor_is_not_applied_by_context() -> None:
    """Отрицательный оборотный капитал при высоком покрытии не применяется."""
    policy = load_issuer_types()
    # Ключ словаря — код показателя ветки. Прежде норма ссылалась
    # на `interest_cover`, которого в справочнике МСФО нет: замер подставлял
    # величину отдельным словарём, а в расчёте по фактам норма не сработала бы
    # никогда.
    high = applicability(
        "negative_nwc", "corporate", {"interest_cover_accrued": Decimal("5.71")}, policy
    )
    low = applicability(
        "negative_nwc", "corporate", {"interest_cover_accrued": Decimal("-1.99")}, policy
    )
    assert not high.applicable and high.kind == "by_context"
    assert low.applicable


def test_context_condition_needs_the_metric() -> None:
    """Величины нет — условие не выполняется, стоп-фактор остаётся в силе.

    Неприменимость по обстановке обязана опираться на посчитанный показатель,
    а не на его отсутствие.
    """
    policy = load_issuer_types()
    found = applicability(
        "negative_nwc", "corporate", {"interest_cover_accrued": None}, policy
    )
    assert found.applicable


def test_consistency_with_the_audit_report_has_three_outcomes() -> None:
    """Подтверждён, не подтверждён и сверить нельзя — три разных исхода."""
    policy = load_issuer_types()
    confirmed, _ = consistency(
        "negative_nwc", ("going_concern_uncertainty",), True, policy
    )
    unconfirmed, note = consistency("negative_nwc", ("emphasis_of_matter",), True, policy)
    unreadable, unreadable_note = consistency("negative_nwc", (), False, policy)
    assert confirmed is Consistency.CONFIRMED
    assert unconfirmed is Consistency.UNCONFIRMED
    assert "внешнего" in note or "подтверждения" in note
    assert unreadable is Consistency.NOT_READABLE
    assert "не прочитано" in unreadable_note


def test_confirmed_value_gives_the_type_as_the_catalogue_would() -> None:
    """Величина, присвоенная человеком, участвует в вердикте о типе наравне.

    Оба структурных признака девелопера справочник не опознаёт: у ЛСР
    «Экономия по кредитам с эскроу…» и «Движение денежных средств,
    направленных на операционную деятельность» размечены человеком. Вердикт,
    смотревший только на опознанное справочником, отвечал «corporate» —
    и поправка ликвидности не применялась: 4,148 шло в балл группой
    100 из 100 при 217 501 млн на недоступных счетах эскроу.

    Это третий случай «подтверждённое человеком не доходит до…»: прежде
    так не доходили факты комплекта.
    """
    from dataclasses import dataclass
    from datetime import date

    from finlib.sources.ifrs_document import read_document

    policy = load_issuer_types()
    text = (
        "Средства на счетах эскроу, полученные от участников долевого "
        "строительства, и проектное финансирование."
    )
    # Без подтверждённого: структурных статей нет, тип по умолчанию.
    assert determine_type({}, text, policy).code == "corporate"
    # С подтверждённой величиной — тот же вердикт, что дал бы справочник.
    found = determine_type(
        {"ifrs.escrow_savings_in_revenue": Decimal(-20950)}, text, policy
    )
    assert found.code == "developer"
    assert found.determination is Determination.STRUCTURAL

    # И довод доходит до чтения документа: он обязательный и позиционный,
    # потому что молча не переданное подтверждение неотличимо от его отсутствия.
    @dataclass(frozen=True)
    class _Fact:
        code: str
        values: tuple

    @dataclass(frozen=True)
    class _Confirmed:
        facts: tuple

    class _Extraction:
        notes: tuple = ()
        forms: dict = {}

        def totals(self, report_date, catalog=None) -> dict:
            return {}

        def value_of(self, code: str, report_date) -> None:
            return None

    class _Profile:
        from finlib.sources.ifrs_numbers import Grouping

        report_dates = (date(2025, 12, 31),)
        grouping = Grouping.RUSSIAN

    reading = read_document(
        text,
        _Extraction(),
        _Profile(),
        {},
        _Confirmed(facts=(_Fact("ifrs.escrow_savings_in_revenue", (Decimal(-20950),)),)),
    )
    assert reading.issuer_type == "developer"


def test_metric_adjustment_belongs_to_metrics_and_refuses_without_the_value() -> None:
    """Поправка показателя живёт в составе показателей и отказывает без величины.

    У ЛСР текущая ликвидность 4,15, но средства на счетах эскроу раскрыты
    сноской, а не строкой: исключить их нечем, и показатель не приводится
    вовсе — завышенный вчетверо хуже отсутствующего.
    """
    metrics = load_ifrs_metrics()
    found = metrics.for_type("developer")
    assert [item.metric for item in found] == ["cur_liq"]
    adjustment = found[0]
    assert adjustment.on_missing == "not_calculable"
    assert "ifrs.escrow_balance" in adjustment.requires
    assert "не рассчитана" in adjustment.limitation


def test_norm_without_its_condition_does_not_load(tmp_path) -> None:
    """Неприменимость по обстановке без условия справочник не принимает."""
    from finlib.normalize.ifrs_issuer_type import IssuerTypePolicy

    broken = {
        "version": "0.0.1",
        "types": [
            {"code": "corporate", "name": "Обычный", "default": True,
             "confirmation": "not_required"},
            {"code": "developer", "name": "Девелопер",
             "structural_any_of": ["ifrs.escrow_savings_in_revenue"],
             "confirmation": "required"},
        ],
        "stop_factors": [{"code": "negative_nwc", "name": "ЧОК"}],
        "not_applicable": [
            {
                "stop_factor": "negative_nwc",
                "kind": "by_context",
                "rationale": "…",
                "limitation": "…",
                "origin": "…",
                "calibration_status": "preliminary",
            }
        ],
        "audit_consistency": {
            "confirmed_by": {"negative_nwc": ["going_concern_uncertainty"]},
            "confirmed_note": "…",
            "unconfirmed_note": "…",
            "not_readable_note": "…",
        },
    }
    with pytest.raises(ValueError, match="без условия"):
        IssuerTypePolicy.model_validate(broken)
