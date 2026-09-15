"""Тесты нечисловых проверок: отсылки к нормативу и ложный нерасчёт.

Постпроверка чисел защищает только числовые утверждения. «Коэффициент
абсолютной ликвидности не рассчитывается» и «что ниже норматива 1,0» она
пропускала: в первом случае чисел нет, во втором 1,0 разрешено как тривиальное.
"""

from decimal import Decimal

import pytest

from finlib.llm.verify import verify
from finlib.llm.wording import find_forbidden
from finlib.metrics.definitions import load_metrics

BLOCKS = """
=== ДАННЫЕ ОТЧЁТНОСТИ (тыс. руб.) ===
1600  БАЛАНС (актив)  |  25 736 328 136
1120  Результаты исследований и разработок  |  не раскрыто
2340  Прочие доходы  |  0

=== ПОКАЗАТЕЛИ ===
cur_liq  «Коэффициент текущей ликвидности»  31.12.2025: 0,82
abs_liq  «Коэффициент абсолютной ликвидности»  31.12.2025: 0,16
equity_ratio  «Коэффициент автономии»  31.12.2025: 0,64
interest_cover  «Покрытие процентов прибылью от продаж»  31.12.2025: 0,24

Не рассчитаны:
  gross_margin «Валовая рентабельность»: не раскрыта строка 2100
"""

THRESHOLDS = load_metrics().stop_factor_values()


# --- отсылки к нормативу ----------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "ниже норматива",
        "при норме не менее",
        "нормативное значение составляет",
        "не дотягивает до рекомендуемого значения",
        "не соответствует норме",
        "должен быть не менее",
        "минимально допустимым считают",
        "безопасным уровнем считается",
        "общепринятый порог",
        "пороговое значение",
    ],
)
def test_normative_wordings_are_caught(phrase: str) -> None:
    """Каждая запрещённая формулировка опознаётся."""
    found = find_forbidden(f"Коэффициент автономии 0,64, {phrase} по отрасли.")
    assert found, phrase


def test_normative_wording_rejects_the_answer() -> None:
    """Отсылка к нормативу отклоняет ответ, даже если все числа верны."""
    answer = "Коэффициент автономии (equity_ratio) — 0,64, что ниже норматива."
    result = verify(answer, BLOCKS, thresholds=THRESHOLDS)
    assert not result.verified
    assert result.foreign == []
    assert result.wordings
    assert "норматив" in result.wordings[0].describe()


@pytest.mark.parametrize(
    "phrase",
    [
        "коэффициент автономии (equity_ratio) составил 0,64",
        "текущая ликвидность (cur_liq) — 0,82",
        "балл по группе «Структура капитала» — 45 из 100",
    ],
)
def test_plain_statements_are_not_flagged(phrase: str) -> None:
    """Обычное изложение величины запрещённым не считается."""
    assert find_forbidden(phrase) == []


def test_wording_carries_context() -> None:
    """В журнал уходит окружение формулировки, а не только она сама."""
    found = find_forbidden("Автономия 0,64, это ниже норматива отрасли.")
    assert "Автономия" in found[0].context


# --- изобретённые пороги из тривиальных чисел -------------------------------


def test_invented_threshold_from_trivial_number_is_rejected() -> None:
    """«Что ниже 1,0» — выдуманный порог, хотя 1 разрешена без привязки."""
    answer = "Коэффициент текущей ликвидности (cur_liq) — 0,82, что ниже 1,0."
    result = verify(answer, BLOCKS, thresholds=THRESHOLDS)
    assert not result.verified
    assert result.foreign[0].text == "1,0"


def test_stop_factor_threshold_may_be_named_at_its_metric() -> None:
    """Порог стоп-фактора объявлен методикой прямо, называть его разрешено."""
    answer = "Покрытие процентов (interest_cover) — 0,24, ниже 1: сработал стоп-фактор."
    assert verify(answer, BLOCKS, thresholds=THRESHOLDS).verified


def test_same_threshold_at_another_metric_is_rejected() -> None:
    """То же число при чужом показателе остаётся выдумкой."""
    answer = "Коэффициент текущей ликвидности (cur_liq) — 0,82, ниже 1."
    assert not verify(answer, BLOCKS, thresholds=THRESHOLDS).verified


def test_trivial_number_outside_comparison_still_passes() -> None:
    """Ноль как факт, а не как порог, проверку проходит."""
    assert verify("Прочие доходы (2340) раскрыты и равны 0.", BLOCKS).verified


def test_years_are_not_treated_as_thresholds() -> None:
    """«По сравнению с 2024 годом» сравнивает периоды, а не с порогом."""
    answer = "За 2025 год по сравнению с 2024 годом валюта баланса (1600) выросла."
    assert verify(answer, BLOCKS).verified


# --- ложные утверждения о нерасчёте -----------------------------------------


def test_claim_of_non_calculation_is_rejected() -> None:
    """Показатель назван нерассчитанным, хотя значение приведено."""
    answer = "Коэффициент абсолютной ликвидности (abs_liq) не рассчитывается."
    result = verify(answer, BLOCKS, thresholds=THRESHOLDS)
    assert not result.verified
    assert result.claims
    assert result.claims[0].code == "abs_liq"


@pytest.mark.parametrize(
    "phrase",
    [
        "не рассчитывается",
        "не рассчитан",
        "не может быть рассчитан",
        "не определён",
        "не раскрыт",
    ],
)
def test_denial_forms_are_recognized(phrase: str) -> None:
    """Формы отрицания опознаются по корню, а не по точному написанию."""
    answer = f"Коэффициент абсолютной ликвидности (abs_liq) {phrase}."
    assert not verify(answer, BLOCKS, thresholds=THRESHOLDS).verified


def test_true_claim_of_non_calculation_passes() -> None:
    """Про действительно нерассчитанный показатель сказать так можно."""
    answer = "Валовая рентабельность (gross_margin) не рассчитана: не раскрыта строка 2100."
    result = verify(answer, BLOCKS, thresholds=THRESHOLDS)
    assert result.verified, result.problems


def test_claim_without_a_code_is_not_checked() -> None:
    """Без кода рядом утверждение не с чем сверять."""
    assert verify("Часть показателей не рассчитана.", BLOCKS).verified


def test_lines_are_outside_the_check() -> None:
    """Строки под проверку не подпадают: раскрытие у них своё в каждом периоде.

    «Строка 1120 за 2025 год не раскрыта» верно и тогда, когда за 2024 год
    значение есть, а по блокам эти случаи не различить.
    """
    assert verify("Строка 1120 (1120) не раскрыта.", BLOCKS).verified


def test_period_qualified_claim_is_not_checked() -> None:
    """Утверждение о конкретном периоде проверке не подлежит по той же причине."""
    answer = "Коэффициент абсолютной ликвидности (abs_liq) не рассчитан за 2023 год."
    assert verify(answer, BLOCKS, thresholds=THRESHOLDS).verified


# --- сводка -----------------------------------------------------------------


def test_all_problem_kinds_reach_the_summary() -> None:
    """В сводке видны все три вида замечаний, а не только числа."""
    answer = (
        "Коэффициент абсолютной ликвидности (abs_liq) не рассчитывается. "
        "Автономия (equity_ratio) — 0,64, ниже норматива. "
        "Рентабельность (roa) достигла 42,5 %."
    )
    result = verify(answer, BLOCKS, thresholds=THRESHOLDS)
    assert not result.verified
    summary = result.summary()
    assert "посторонних чисел" in summary
    assert "отсылок к нормативу" in summary
    assert "ложных утверждений о нерасчёте" in summary
    assert len(result.problems) >= 3


def test_thresholds_are_declared_per_metric() -> None:
    """Пороги стоп-факторов разнесены по показателям, а не свалены в кучу."""
    assert THRESHOLDS
    assert all(isinstance(value, frozenset) for value in THRESHOLDS.values())
    assert THRESHOLDS["interest_cover"] == frozenset({Decimal(1)})
