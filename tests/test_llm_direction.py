"""Тесты согласования глагола со знаком изменения.

«Коэффициент вырос с 1,23 до 0,82» — оба числа верны, утверждение ложно.
Числовая проверка такое пропускает, проверка направления — нет.
"""

from decimal import Decimal

import pytest

from finlib.llm.direction import Direction, agrees, stated_direction
from finlib.llm.verify import Violation, verify

BLOCKS = """
=== ДАННЫЕ ОТЧЁТНОСТИ (тыс. руб.) ===
1230  Дебиторская задолженность  |  1 144 646 095
2400  Чистая прибыль (убыток)  |  -50 000

=== ПОКАЗАТЕЛИ ===
cur_liq  «Коэффициент текущей ликвидности»  31.12.2025: 0,82  |  31.12.2024: 1,23
cur_liq_chg_abs  «Текущая ликвидность, изменение за период»  31.12.2025: -0,41
cur_liq_chg_pct  «Текущая ликвидность, изменение в процентах»  31.12.2025: -33,2 %
1230_chg_pct  «Дебиторская задолженность (1230), изменение в процентах»  31.12.2025: -59,6 %
1230_share  «Дебиторская задолженность (1230), доля в валюте баланса»  31.12.2025: 4,4 %
2400_chg_abs  «Чистая прибыль (2400), изменение за период»  31.12.2025: -30 000
"""


def test_growth_word_with_negative_change_is_rejected() -> None:
    """Заявленный рост при отрицательном изменении — ложное утверждение."""
    answer = "Текущая ликвидность выросла на 33,2 % (cur_liq_chg_pct)."
    result = verify(answer, BLOCKS)
    assert not result.verified
    assert result.foreign[0].violation is Violation.WRONG_DIRECTION
    assert "снижение" in result.foreign[0].describe()


def test_decline_word_with_negative_change_passes() -> None:
    """Правильный глагол при том же числе проверку проходит."""
    answer = "Текущая ликвидность снизилась на 33,2 % (cur_liq_chg_pct)."
    assert verify(answer, BLOCKS).verified


def test_decline_word_with_positive_change_is_rejected() -> None:
    """Проверка двусторонняя: снижение при росте тоже ложь."""
    blocks = BLOCKS + "\n1230_chg_abs  «Дебиторка, изменение»  31.12.2025: 500 000\n"
    answer = "Дебиторская задолженность сократилась на 500 000 (1230_chg_abs)."
    result = verify(answer, blocks)
    assert not result.verified
    assert result.foreign[0].violation is Violation.WRONG_DIRECTION
    assert "рост" in result.foreign[0].describe()


def test_signed_value_with_matching_word_passes() -> None:
    """Число со знаком и глагол снижения друг другу не противоречат."""
    assert verify("Ликвидность снизилась на -33,2 % (cur_liq_chg_pct).", BLOCKS).verified


def test_direction_is_not_checked_for_levels() -> None:
    """Уровень направления не имеет: глагол при нём ничего не утверждает о знаке."""
    answer = "Коэффициент текущей ликвидности (cur_liq) вырос до 0,82."
    assert verify(answer, BLOCKS).verified


def test_direction_is_not_checked_for_shares() -> None:
    """Доля — не изменение, её знак направления не задаёт."""
    assert verify("Доля выросла до 4,4 % (1230_share).", BLOCKS).verified


def test_negative_base_disables_the_check() -> None:
    """У убытка «рост убытка» означает падение показателя — судить по глаголу нельзя.

    Чистая прибыль отрицательна и стала ещё отрицательнее: изменение -30 000.
    Фраза «убыток вырос на 30 000» верна по-русски, и отклонять её нельзя.
    """
    answer = "Убыток (2400) вырос на 30 000 (2400_chg_abs)."
    result = verify(answer, BLOCKS)
    assert result.verified, result.foreign_values


def test_word_from_another_sentence_is_not_used() -> None:
    """Глагол из соседнего предложения к этому числу не относится."""
    answer = (
        "Выручка выросла за период. "
        "Текущая ликвидность снизилась на 33,2 % (cur_liq_chg_pct)."
    )
    assert verify(answer, BLOCKS).verified


def test_answer_without_direction_word_passes() -> None:
    """Нет слова о направлении — нечего и сверять."""
    answer = "Изменение текущей ликвидности составило -33,2 % (cur_liq_chg_pct)."
    assert verify(answer, BLOCKS).verified


@pytest.mark.parametrize(
    "phrase",
    ["вырос", "выросла", "увеличилась", "прирост составил", "повысился", "рост составил"],
)
def test_growth_words_are_recognized(phrase: str) -> None:
    """Формы роста опознаются по корню, а не по точному написанию."""
    assert stated_direction(f"Показатель {phrase} на 5", 30) is Direction.GROWTH


@pytest.mark.parametrize(
    "phrase",
    ["снизился", "сократилась", "уменьшилось", "упала", "падение составило", "снижение на"],
)
def test_decline_words_are_recognized(phrase: str) -> None:
    """Формы снижения тоже."""
    assert stated_direction(f"Показатель {phrase} 5", 30) is Direction.DECLINE


def test_nearest_word_wins() -> None:
    """Если в предложении оба слова, решает ближайшее к числу."""
    text = "Выручка выросла, а себестоимость сократилась на 5"
    assert stated_direction(text, len(text) - 1) is Direction.DECLINE


def test_zero_change_has_no_direction() -> None:
    """Нулевое изменение согласуется с любым словом: о нём говорят «не изменился»."""
    assert agrees(Decimal(0), Direction.GROWTH)
    assert agrees(Decimal(0), Direction.DECLINE)


def test_missing_word_agrees_with_anything() -> None:
    """Отсутствие слова — не нарушение."""
    assert agrees(Decimal(-5), None)
