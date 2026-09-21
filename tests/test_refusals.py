"""Тесты отказов: место, семейство и полнота раздела (задача 28).

Раздел «Ограничения анализа» — перечень того, что нужно запросить
у организации. Отказ, потерянный по дороге, тише всего остального:
документ выглядит полным.
"""

import pytest

from finlib.quality.refusals import (
    Kind,
    Refusal,
    RefusalsLostError,
    check_complete,
    load_refusals,
    refusal,
    section,
)


def test_every_formulation_names_the_place() -> None:
    """Формулировка без предмета и места запросом не становится."""
    catalog = load_refusals()
    for item in catalog.reasons:
        assert "{where}" in item.template or "{subject}" in item.template
        assert item.request.strip()


def test_refusal_without_action_is_impossible() -> None:
    """Отказ без указания, что делать, не собирается вовсе."""
    with pytest.raises(ValueError, match="третьего не бывает"):
        Refusal("code", "предмет", "место", "текст", "   ", Kind.DATA_MISSING)


def test_families_are_distinguishable() -> None:
    """Нехватка данных и неприменимость — разные семейства и разные тексты.

    У Автодора покрытие процентов рассчитано, но исключено по типу;
    у Норникеля не рассчитано вовсе. Читатель обязан видеть разницу.
    """
    catalog = load_refusals()
    missing = refusal(
        "missing_input", "Покрытие процентов", "начисленные проценты", catalog
    )
    excluded = refusal(
        "excluded_not_applicable",
        "Покрытие процентов",
        "неприменимость объявлена методикой",
        catalog,
    )
    assert missing.kind is Kind.DATA_MISSING
    assert excluded.kind is Kind.NOT_APPLICABLE
    assert "не рассчитан" in missing.text
    assert "рассчитан, но в оценку не входит" in excluded.text
    assert "Запросить" in missing.request
    assert "Запрашивать нечего" in excluded.request


def test_same_refusals_are_grouped_and_nothing_is_lost() -> None:
    """Одинаковые отказы сводятся в строку, но все предметы названы."""
    catalog = load_refusals()
    produced = tuple(
        refusal("negative_denominator", name, "знаменатель отрицателен", catalog)
        for name in ("Рентабельность капитала", "Финансовый рычаг", "Оборачиваемость")
    )
    lines = section(produced, catalog)
    assert len(lines) == 1
    for item in produced:
        assert item.subject in lines[0]
    check_complete(produced, lines)


def test_lost_refusal_blocks_the_document() -> None:
    """Потерянный отказ — блокирующая ошибка, а не предупреждение."""
    catalog = load_refusals()
    produced = (
        refusal("missing_input", "Текущая ликвидность", "оборотные активы", catalog),
        refusal("missing_input", "Автономия", "капитал", catalog),
    )
    with pytest.raises(RefusalsLostError, match="Документ не формируется"):
        check_complete(produced, (produced[0].describe(),))


def test_place_is_named_for_the_developer_liquidity() -> None:
    """Отказ называет место величины, и семейство его — наш пробел.

    Величина раскрыта эмитентом сноской под балансом, и место отказа само
    это говорит. Прежде отказ числился нехваткой данных и просил организацию
    раскрыть то, что она раскрыла; запрашивать здесь нечего — не сделан
    перевод сноски в состав входных величин показателя, и это за нами.
    """
    from finlib.metrics.ifrs import Inputs, compute_all
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics
    from finlib.report.refusals import from_ifrs_metrics

    policy = load_ifrs_metrics()
    computed = compute_all(
        Inputs({}, {}, "developer"), policy
    )
    found = from_ifrs_metrics(computed, {}, adjustments=policy.for_type("developer"))
    liquidity = next(item for item in found if item.subject == "Текущая ликвидность")
    assert "сноской" in liquidity.text
    assert liquidity.kind is Kind.OUR_GAP
    assert "Запрашивать нечего" in liquidity.request
