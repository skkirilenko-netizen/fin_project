"""Тесты контроля утверждений текста (задача 14).

Постпроверка чисел отвечает на вопрос «откуда взялось число». Здесь
проверяется другое: не противоречит ли текст сам себе и расчёту.
"""

import pytest

from finlib.llm.cleanup import has_identifiers, strip_identifiers
from finlib.llm.textcheck import (
    SEVERITY,
    Severity,
    TextContext,
    TextRule,
    blocking,
    check_text,
)

CONTEXT = TextContext(
    known_lines=frozenset({"1150", "1210", "1240", "1250", "1300", "1520", "1600"}),
    refused_metrics={"fin_leverage": "Финансовый рычаг"},
    days_metrics=frozenset({"Оборачиваемость дебиторской задолженности, дней"}),
    forbidden_templates={
        "Это итог раздела III баланса, а не чистые активы": (
            "показатель «Собственный капитал» в расчёте не участвовал"
        )
    },
)


def issues(text: str, section: int = 3, context: TextContext = CONTEXT):
    """Нарушения в одном разделе; текст подаётся очищенным, как из verify."""
    return check_text({section: strip_identifiers(text)}, context)


def rules(text: str, section: int = 3, context: TextContext = CONTEXT) -> set[TextRule]:
    """Коды найденных правил."""
    return {item.rule for item in issues(text, section, context)}


# --- снятие технических идентификаторов -------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "Коэффициент автономии (equity_ratio) вырос с 0,62 до 0,64.",
            "Коэффициент автономии вырос с 0,62 до 0,64.",
        ),
        (
            "Капитал увеличился на 27 019 тыс. руб. (equity_chg_abs).",
            "Капитал увеличился на 27 019 тыс. руб.",
        ),
        # Величина изменения остаётся: снимается код, а не скобка целиком.
        # Требуемый формат — «снизилось с 0,10 до 0,04 (−0,06, или −59,0 %)».
        (
            "Ликвидность (cur_liq) снизилась с 1,23 до 0,82 (cur_liq_chg_pct: -33,2 %).",
            "Ликвидность снизилась с 1,23 до 0,82 (-33,2 %).",
        ),
        (
            "Снизилась с 0,10 до 0,04 (cur_liq_chg_abs -0,06, или cur_liq_chg_pct -59,0 %).",
            "Снизилась с 0,10 до 0,04 (-0,06, или -59,0 %).",
        ),
    ],
)
def test_identifiers_are_stripped(raw: str, clean: str) -> None:
    """Код — механизм проверки, а не часть заключения."""
    assert strip_identifiers(raw) == clean


def test_line_codes_survive() -> None:
    """Код строки отчётности остаётся: это обычная бухгалтерская ссылка."""
    raw = "Валюта баланса (1600) — 25 736 328 136 тыс. руб."
    assert strip_identifiers(raw) == raw


def test_stripping_leaves_no_leftovers() -> None:
    """После снятия технических идентификаторов не остаётся."""
    raw = "Рычаг (fin_leverage) и рентабельность (roa_chg_pct) изменились."
    assert has_identifiers(strip_identifiers(raw)) == []


def test_latin_terms_of_our_own_texts_survive() -> None:
    """Запрещены коды, а не латынь вообще.

    Оговорка показателя «Чистый долг к прибыли от продаж» содержит слово
    EBITDA, и модель обязана привести её дословно. Запрет всей латыни
    отклонял бы верное цитирование нашего же текста.
    """
    raw = "Это НЕ «Чистый долг / EBITDA»: амортизация не раскрывается."
    assert strip_identifiers(raw) == raw
    assert has_identifiers(raw) == []


def test_unclean_text_is_blocking() -> None:
    """Правило сторожит, что очистка выполнена, а не собственную очистку.

    Очистку делает вызывающий; если её забыли, коды доходят до правила
    и оно блокирует документ. Проверяется именно этот случай: разделы
    поданы сырыми.
    """
    raw = {3: "Коэффициент автономии (equity_ratio) вырос."}
    found = check_text(raw, CONTEXT)
    assert TextRule.TECHNICAL_IDENTIFIER in {item.rule for item in found}
    assert blocking(found)


def test_cleaned_text_passes_the_same_rule() -> None:
    """Очищенный текст тем же правилом пропускается."""
    assert TextRule.TECHNICAL_IDENTIFIER not in rules(
        "Коэффициент автономии (equity_ratio) вырос."
    )


def test_clean_text_passes() -> None:
    """Текст без кодов нарушением не считается."""
    assert TextRule.TECHNICAL_IDENTIFIER not in rules(
        "Коэффициент автономии вырос с 0,62 до 0,64."
    )


# --- класс не утверждается и не отрицается разом ----------------------------


def test_class_stated_both_ways_is_blocking() -> None:
    """Два взаимоисключающих утверждения о классе отменяют ответ."""
    text = "Организации присвоен класс E. При этом класс не присвоен."
    assert TextRule.CLASS_STATED_BOTH_WAYS in rules(text, section=1)


def test_class_stated_once_passes() -> None:
    """Одно утверждение о классе нарушением не является."""
    assert TextRule.CLASS_STATED_BOTH_WAYS not in rules(
        "Организации присвоен класс E.", section=1
    )
    assert TextRule.CLASS_STATED_BOTH_WAYS not in rules(
        "Класс не присвоен: основание слишком узкое.", section=1
    )


# --- изменение равно разности уровней ---------------------------------------


def test_delta_mismatch_is_blocking() -> None:
    """Заявленное изменение обязано равняться разности приведённых уровней."""
    text = "Рентабельность активов снизилась с 0,41 до 0,31, изменение 0,11."
    assert TextRule.DELTA_MISMATCH in rules(text)


def test_consistent_delta_passes() -> None:
    """Согласованное изменение проходит."""
    text = "Рентабельность активов снизилась с 0,41 до 0,31, изменение 0,10."
    assert TextRule.DELTA_MISMATCH not in rules(text)


def test_delta_sign_does_not_matter() -> None:
    """Знак заявленного изменения проверяется отдельным правилом, не этим."""
    text = "Показатель снизился с 0,41 до 0,31, изменение -0,10."
    assert TextRule.DELTA_MISMATCH not in rules(text)


# --- шаблонный блок и применимость ------------------------------------------


def test_inapplicable_template_is_blocking() -> None:
    """Шаблонный блок не выводится при невыполнении условия применения."""
    text = "Это итог раздела III баланса, а не чистые активы по методике Минфина."
    assert TextRule.TEMPLATE_NOT_APPLICABLE in rules(text)


def test_applicable_text_passes() -> None:
    """Обычный текст под запрет не подпадает."""
    assert TextRule.TEMPLATE_NOT_APPLICABLE not in rules(
        "Собственный капитал вырос за период."
    )


# --- отменённый знаменатель --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Финансовый рычаг заметно вырос за период.",
        "Финансовый рычаг указывает на высокую зависимость от заёмных средств.",
        "Финансовый рычаг свидетельствует об устойчивости.",
    ],
)
def test_free_interpretation_is_blocking(text: str) -> None:
    """Показатель с отменённым знаменателем истолкованию не подлежит."""
    assert TextRule.FREE_INTERPRETATION in rules(text)


def test_statement_of_non_calculation_passes() -> None:
    """Сказать, что показатель не рассчитан, можно."""
    assert TextRule.FREE_INTERPRETATION not in rules(
        "Финансовый рычаг не рассчитан: собственный капитал отрицателен."
    )


# --- вопросы в пределах набора форм -----------------------------------------


def test_question_about_absent_line_is_blocking() -> None:
    """Вопрос о строке вне набора форм содержательного ответа не имеет."""
    text = "1. Почему не раскрыты строки 1410 и 1510?"
    assert TextRule.QUESTION_OUT_OF_FORM_SET in rules(text, section=6)


def test_question_about_present_line_passes() -> None:
    """Вопрос о раскрытой строке правомерен."""
    text = "1. Чем вызван рост кредиторской задолженности по строке 1520?"
    assert TextRule.QUESTION_OUT_OF_FORM_SET not in rules(text, section=6)


def test_rule_applies_only_to_questions() -> None:
    """Правило привязано к разделу «Вопросы», а не ко всему тексту."""
    text = "В упрощённой форме строки 1410 и 1510 не предусмотрены."
    assert TextRule.QUESTION_OUT_OF_FORM_SET not in rules(text, section=5)


# --- предупреждения ----------------------------------------------------------


def test_days_direction_conflict_is_a_warning() -> None:
    """Сокращение периода оборота означает ускорение, а не замедление."""
    text = (
        "Оборачиваемость дебиторской задолженности, дней снизилась до 124,2, "
        "несмотря на замедление расчётов."
    )
    found = issues(text)
    assert TextRule.DAYS_DIRECTION in {item.rule for item in found}
    assert not blocking(found), "предупреждение документу не мешает"


def test_days_direction_agreement_passes() -> None:
    """Согласованная формулировка предупреждения не вызывает."""
    text = (
        "Оборачиваемость дебиторской задолженности, дней снизилась до 124,2, "
        "то есть расчёты ускорились."
    )
    assert TextRule.DAYS_DIRECTION not in rules(text)


def test_flag_conflict_must_be_stated() -> None:
    """Конфликт флага и стоп-фактора фиксируется в тексте."""
    context = TextContext(flag_conflict="требуется ручная верификация")
    assert TextRule.FLAG_CONFLICT_NOT_STATED in rules("Обычный текст.", context=context)
    assert TextRule.FLAG_CONFLICT_NOT_STATED not in rules(
        "Здесь требуется ручная верификация.", context=context
    )


# --- уровни нарушений --------------------------------------------------------


def test_severity_is_declared_for_every_rule() -> None:
    """У каждого правила объявлен уровень."""
    for rule in TextRule:
        assert rule in SEVERITY, rule


def test_blocking_rules_match_the_specification() -> None:
    """Блокирующими объявлены ровно те правила, что перечислены в задаче."""
    expected = {
        TextRule.TECHNICAL_IDENTIFIER,
        TextRule.CLASS_STATED_BOTH_WAYS,
        TextRule.DELTA_MISMATCH,
        TextRule.TEMPLATE_NOT_APPLICABLE,
        TextRule.FREE_INTERPRETATION,
        TextRule.QUESTION_OUT_OF_FORM_SET,
    }
    assert {
        rule for rule, level in SEVERITY.items() if level is Severity.BLOCKING
    } == expected


def test_warnings_do_not_block() -> None:
    """Предупреждения документ не отменяют."""
    warnings = {rule for rule, level in SEVERITY.items() if level is Severity.WARNING}
    assert warnings == {TextRule.DAYS_DIRECTION, TextRule.FLAG_CONFLICT_NOT_STATED}


def test_verification_blocks_only_on_blocking_rules() -> None:
    """Предупреждение в постпроверке ответ не отменяет, блокирующее — отменяет."""
    from finlib.llm.verify import verify

    blocks = """
=== ПОКАЗАТЕЛИ ===
cur_liq  «Коэффициент текущей ликвидности»  31.12.2025: 0,82

=== ОЦЕНКА ===
Класс: C — Состояние с признаками напряжения
"""
    warning_only = (
        "### 3. Аналитическая интерпретация\n"
        "Оборачиваемость дебиторской задолженности, дней снизилась, "
        "несмотря на замедление."
    )
    result = verify(warning_only, blocks, text_context=CONTEXT)
    assert result.statements
    assert result.verified, result.problems

    blocked = (
        "### 3. Аналитическая интерпретация\nФинансовый рычаг заметно вырос."
    )
    assert not verify(blocked, blocks, text_context=CONTEXT).verified


def test_without_context_text_checks_are_skipped() -> None:
    """Без контекста расчёта правила не применяются: сверять не с чем."""
    from finlib.llm.verify import verify

    blocks = "=== ПОКАЗАТЕЛИ ===\ncur_liq  «Ликвидность»  0,82\n"
    result = verify("Финансовый рычаг вырос.", blocks, require_anchor=False)
    assert result.statements == []


def test_issue_reports_its_own_severity() -> None:
    """Нарушение само знает, блокирует оно документ или нет."""
    found = issues("Финансовый рычаг заметно вырос.")
    assert found[0].blocking
    assert found[0].severity is Severity.BLOCKING
    assert found[0].describe() == found[0].message


def test_russian_number_formatting_is_parsed() -> None:
    """Разряды пробелами и запятая как десятичный знак разбираются.

    Иначе правило дельт молча пропускало бы все крупные величины: разбор
    провалился бы, и сравнивать было бы нечего.
    """
    consistent = "Капитал вырос с 55 232 до 82 251 тыс. руб., изменение 27 019."
    assert TextRule.DELTA_MISMATCH not in rules(consistent)

    broken = "Капитал вырос с 55 232 до 82 251 тыс. руб., изменение 27 000."
    assert TextRule.DELTA_MISMATCH in rules(broken), (
        "разбор крупных чисел не работает, правило выродилось"
    )
