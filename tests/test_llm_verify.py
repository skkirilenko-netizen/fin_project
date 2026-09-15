"""Тесты постпроверки: выдуманное число и чужой код обязаны отклоняться."""

from decimal import Decimal

import pytest

from finlib.llm.verify import (
    Violation,
    extract_numbers,
    strip_reasoning,
    verify,
)

BLOCKS = """
=== ОРГАНИЗАЦИЯ ===
ИНН: 7736050003
Отчётный период: 31.12.2025

=== ДАННЫЕ ОТЧЁТНОСТИ (тыс. руб.) ===
1600  БАЛАНС (актив)  |  25 736 328 136
1300  Итого по разделу III  |  16 432 222 886
2400  Чистая прибыль (убыток)  |  11 284 564

=== ПОКАЗАТЕЛИ ===
cur_liq  «Коэффициент текущей ликвидности»  31.12.2025: 0,82  |  31.12.2024: 1,23
equity_ratio  «Коэффициент автономии»  31.12.2025: 0,64
nwc  «Чистый оборотный капитал»  31.12.2025: -521 415 920

=== ОЦЕНКА ===
Класс: C — Состояние с признаками напряжения
Общий балл: 60,90 из 100
"""


def test_invented_number_is_rejected() -> None:
    """Число, которого нет во входных блоках, отклоняет ответ целиком."""
    answer = (
        "Текущая ликвидность (cur_liq) — 0,82, "
        "рентабельность активов (roa) достигла 14,7 %."
    )
    result = verify(answer, BLOCKS)
    assert not result.verified
    assert result.foreign_values == ["14,7"]
    assert result.foreign[0].violation is Violation.NOT_IN_BLOCKS


def test_quoted_numbers_pass() -> None:
    """Числа, взятые из блоков и снабжённые своим кодом, проходят."""
    answer = (
        "Валюта баланса (1600) — 25 736 328 136 тыс. руб., "
        "собственный капитал (1300) — 16 432 222 886 тыс. руб. "
        "Коэффициент автономии (equity_ratio) составил 0,64."
    )
    result = verify(answer, BLOCKS)
    assert result.verified, result.foreign_values


def test_right_number_at_wrong_code_is_rejected() -> None:
    """Верное число при чужом коде — ложное утверждение, а не опечатка."""
    answer = "Коэффициент автономии (equity_ratio) составил 0,82."
    result = verify(answer, BLOCKS)
    assert not result.verified
    assert result.foreign_values == ["0,82"]
    assert result.foreign[0].violation is Violation.WRONG_ANCHOR
    assert result.foreign[0].anchor == "equity_ratio"
    assert "не является значением" in result.foreign[0].describe()


def test_same_number_at_its_own_code_passes() -> None:
    """То же число при своём коде проходит: отличает пару, а не число."""
    assert verify("Текущая ликвидность (cur_liq) — 0,82.", BLOCKS).verified


def test_number_swapped_between_line_codes_is_rejected() -> None:
    """Подмена работает и на кодах строк, не только на показателях."""
    result = verify("Собственный капитал (1300) — 25 736 328 136 тыс. руб.", BLOCKS)
    assert not result.verified
    assert result.foreign[0].violation is Violation.WRONG_ANCHOR
    assert result.foreign[0].anchor == "1300"


def test_number_without_code_is_rejected() -> None:
    """Число без кода рядом — нарушение формата: сверять его не с чем."""
    result = verify("Ликвидность снизилась до 0,82.", BLOCKS)
    assert not result.verified
    assert result.foreign[0].violation is Violation.NO_ANCHOR
    assert "без кода показателя" in result.foreign[0].describe()


def test_anchor_requirement_can_be_relaxed() -> None:
    """Без require_anchor проверяется только принадлежность числа блокам."""
    assert verify("Ликвидность снизилась до 0,82.", BLOCKS, require_anchor=False).verified


def test_code_itself_is_not_treated_as_value() -> None:
    """Код строки в скобках — ссылка на показатель, а не величина."""
    result = verify("Валюта баланса (1600) — 25 736 328 136 тыс. руб.", BLOCKS)
    assert result.verified, result.foreign_values
    assert result.checked == 1


def test_preceding_code_wins_over_following_one() -> None:
    """При перечислении число относится к коду слева, а не к следующему."""
    answer = (
        "Коэффициент текущей ликвидности (cur_liq) снизился с 1,23 до 0,82, "
        "а коэффициент автономии (equity_ratio) вырос до 0,64."
    )
    result = verify(answer, BLOCKS)
    assert result.verified, result.foreign_values


def test_computed_number_is_rejected() -> None:
    """Модель не вычисляет: производное число проверку не проходит."""
    answer = "Капитал (1300) — 16,4 трлн руб., то есть 63,8 % активов."
    result = verify(answer, BLOCKS)
    assert not result.verified
    assert set(result.foreign_values) == {"16,4", "63,8"}


def test_years_are_allowed() -> None:
    """Номера годов не требуют привязки к данным."""
    answer = "За 2025 год по сравнению с 2024 годом ликвидность (cur_liq) — 0,82."
    assert verify(answer, BLOCKS).verified


@pytest.mark.parametrize("number", ["0", "1", "100"])
def test_trivial_numbers_are_allowed(number: str) -> None:
    """Ноль, единица и сто разрешены без привязки."""
    assert verify(f"Значение равно {number}.", BLOCKS).verified


def test_document_number_is_ignored() -> None:
    """Номер нормативного документа — реквизит, а не величина отчётности."""
    answer = "Методика утверждена приказом от 28.08.2014 № 84н."
    result = verify(answer, BLOCKS)
    assert result.verified, result.foreign_values


def test_list_item_numbers_are_ignored() -> None:
    """Номера пунктов списка числами отчётности не считаются."""
    answer = "1. Ликвидность (cur_liq) — 0,82\n2. Класс C\n7. Прочее"
    assert verify(answer, BLOCKS).verified


def test_markdown_heading_numbers_are_ignored() -> None:
    """Номера разделов в заголовках Markdown тоже не числа отчётности."""
    answer = (
        "### 4. Риски\nЛиквидность (cur_liq) — 0,82.\n\n"
        "### 6. Вопросы к организации\nВопрос."
    )
    result = verify(answer, BLOCKS)
    assert result.verified, result.foreign_values


def test_rounding_to_given_precision_is_allowed() -> None:
    """Цитирование с меньшей точностью — не вычисление."""
    blocks = 'cur_liq  «Коэффициент текущей ликвидности»  0,8212956762'
    assert verify("Ликвидность (cur_liq) — 0,82.", blocks).verified
    assert not verify("Ликвидность (cur_liq) — 0,85.", blocks).verified


def test_unit_conversion_is_not_rounding() -> None:
    """Перевод единиц измерения проверку не проходит."""
    blocks = "1600  БАЛАНС  |  25 736 328 136"
    assert not verify("Активы (1600) — 25,7 трлн руб.", blocks).verified


def test_reasoning_block_is_stripped() -> None:
    """Черновик рассуждающей модели в проверке не участвует."""
    answer = (
        "<think>посчитаю: 25736328136 / 2 = 12868164068</think>"
        "Ликвидность (cur_liq) — 0,82."
    )
    result = verify(answer, BLOCKS)
    assert result.verified, result.foreign_values
    assert "<think>" not in strip_reasoning(answer)


def test_reasoning_is_removed_from_text() -> None:
    """Ход мысли не попадает в заключение."""
    assert strip_reasoning("<think>черновик</think>Ответ") == "Ответ"
    assert strip_reasoning("Без рассуждения") == "Без рассуждения"


def test_negative_numbers_are_matched() -> None:
    """Отрицательные величины сверяются со знаком."""
    assert verify("Оборотный капитал (nwc) — -521 415 920 тыс. руб.", BLOCKS).verified
    assert not verify("Оборотный капитал (nwc) — -521 415 921 тыс. руб.", BLOCKS).verified


def test_absolute_value_of_negative_is_rejected() -> None:
    """Величина по модулю со знаком в словах не проходит: тире — не минус."""
    answer = "Оборотный капитал (nwc) перешёл в отрицательную зону — 521 415 920 тыс. руб."
    result = verify(answer, BLOCKS)
    assert not result.verified
    assert result.foreign[0].violation is Violation.NOT_IN_BLOCKS


def test_foreign_number_carries_context() -> None:
    """В журнал уходит не только число, но и то, где оно появилось."""
    result = verify("Рентабельность капитала (roe) достигла 42,5 процента.", BLOCKS)
    assert not result.verified
    assert "42,5" in result.foreign[0].context
    assert "Рентабельность" in result.foreign[0].context


def test_checked_count_reported() -> None:
    """Сообщается, сколько чисел сверено."""
    result = verify(
        "Ликвидность (cur_liq) — 0,82 при автономии (equity_ratio) 0,64.", BLOCKS
    )
    assert result.verified, result.foreign_values
    assert result.checked == 2
    assert "сверено чисел: 2" in result.summary()


def test_extract_handles_russian_formatting() -> None:
    """Разряды пробелами и запятая как десятичный знак разбираются."""
    found = dict(extract_numbers("25 736 328 136 и 0,82 и 1 234"))
    assert found["25 736 328 136"] == Decimal("25736328136")
    assert found["0,82"] == Decimal("0.82")
    assert found["1 234"] == Decimal("1234")


def test_answer_without_numbers_passes() -> None:
    """Текст без чисел проверку проходит."""
    result = verify("Организация находится в состоянии напряжения.", BLOCKS)
    assert result.verified
    assert result.checked == 0
