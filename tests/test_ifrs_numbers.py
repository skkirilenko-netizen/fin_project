"""Тесты определения разделителя разрядов документа МСФО.

Отдельное число конвенции не выдаёт: «663,888» — это 663 888 при запятой
в роли разделителя разрядов и 663,888 при запятой в роли десятичного знака.
Прочтения различаются в тысячу раз, и ни один контроль сходимости ошибки
не поймает: баланс сойдётся, разделы сойдутся, коэффициенты будут верны.
"""

from decimal import Decimal

import pytest

from finlib.sources.ifrs_numbers import (
    Grouping,
    GroupingUndetermined,
    check_plausibility,
    detect_grouping,
    load_parsing_policy,
    parse_amount,
)

# Выдержка вёрстки ФосАгро: разряды отделены запятой, дробная часть — точкой.
PHOSAGRO = """
Итого активы 663,888 452,110
Выручка 507,718 469,004
Основные средства 1,234,567
Прибыль на акцию 12.5
Рентабельность 0.31
"""

# Русская вёрстка: разряды пробелом, дробная часть запятой.
RUSSIAN_DOC = """
Итого активы 663 888 452 110
Выручка 507 718
Прибыль на акцию 12,5
Рентабельность 0,31
"""

# Неоднозначный документ: единственная разметка — «X,YYY», и она допускает
# оба прочтения.
AMBIGUOUS_DOC = """
Итого активы 663,888
Выручка 507,718
Капитал 155,770
"""


# --- определение конвенции ----------------------------------------------------


def test_phosagro_reads_comma_as_thousands_separator() -> None:
    """У ФосАгро «663,888» — это 663 888, а не 663,888.

    Свидетельства в пользу английской конвенции: число с двумя запятыми
    («1,234,567») и дробные части после точки, где цифр не три.
    """
    found = detect_grouping(PHOSAGRO)
    assert found.convention is Grouping.ENGLISH
    assert found.english_evidence >= load_parsing_policy().digit_grouping.min_evidence
    assert found.russian_evidence == 0
    assert parse_amount("663,888", found.convention) == Decimal(663888)
    assert parse_amount("12.5", found.convention) == Decimal("12.5")


def test_russian_document_is_not_read_as_english() -> None:
    """Документ с пробелом как разделителем запятой не трактуется.

    Обратный случай к ФосАгро: здесь «12,5» — двенадцать с половиной,
    а не двенадцать тысяч пятьсот.
    """
    found = detect_grouping(RUSSIAN_DOC)
    assert found.convention is Grouping.RUSSIAN
    assert found.english_evidence == 0
    assert parse_amount("663 888", found.convention) == Decimal(663888)
    assert parse_amount("12,5", found.convention) == Decimal("12.5")


def test_ambiguous_document_is_refused() -> None:
    """Документ, где осмысленны оба прочтения, не разбирается вовсе.

    Выбор по умолчанию здесь означал бы ошибку в тысячу раз у половины
    документов — молча и без единого сработавшего контроля.
    """
    found = detect_grouping(AMBIGUOUS_DOC)
    assert not found.determined
    assert found.reason is GroupingUndetermined.AMBIGUOUS
    assert found.ambiguous >= 3


def test_conflicting_conventions_are_refused() -> None:
    """Числа обеих конвенций в одном документе — отказ, а не голосование."""
    mixed = PHOSAGRO + RUSSIAN_DOC
    found = detect_grouping(mixed)
    assert not found.determined
    assert found.reason is GroupingUndetermined.CONFLICTING
    assert found.russian_evidence and found.english_evidence


def test_single_evidence_is_not_a_convention() -> None:
    """Одно свидетельство — опечатка составителя, а не конвенция документа."""
    thin = "Итого активы 1 234 567\nПрочее 42\n"
    found = detect_grouping(thin)
    assert not found.determined
    assert found.reason is GroupingUndetermined.INSUFFICIENT


def test_document_without_separators_needs_no_convention() -> None:
    """Где разделителей нет вовсе, выбирать не из чего."""
    plain = "Итого активы 663888\nВыручка 507718\nКапитал 155770\n"
    found = detect_grouping(plain)
    assert found.convention is Grouping.PLAIN
    assert parse_amount("663888", found.convention) == Decimal(663888)


def test_detection_counts_what_it_looked_at() -> None:
    """Определение сообщает не только решение, но и число просмотренных чисел.

    Ноль свидетельств против ноля просмотренных чисел — разные вещи,
    и по журналу их надо различать.
    """
    found = detect_grouping(PHOSAGRO)
    assert found.numbers_seen > 0
    assert "чисел просмотрено" in found.describe()
    assert found.samples


# --- разбор чисел -------------------------------------------------------------


def test_convention_is_required_to_parse() -> None:
    """Число без конвенции не читается: значения по умолчанию нет.

    Умолчание здесь означало бы ровно то, ради отказа от чего написан
    весь модуль, — угадывание.
    """
    with pytest.raises(TypeError):
        parse_amount("663,888")  # type: ignore[call-arg]


def test_same_token_reads_differently_by_convention() -> None:
    """Одна и та же запись даёт величины, различающиеся в тысячу раз."""
    assert parse_amount("663,888", Grouping.ENGLISH) == Decimal(663888)
    assert parse_amount("663,888", Grouping.RUSSIAN) == Decimal("663.888")


def test_brackets_are_stripped_not_interpreted() -> None:
    """Скобки — способ печати расхода; знак берётся из справочника статей."""
    assert parse_amount("(29 390)", Grouping.RUSSIAN) == Decimal(29390)
    assert parse_amount("(29,390)", Grouping.ENGLISH) == Decimal(29390)


def test_non_numeric_token_is_not_a_number() -> None:
    """Прочерк и текст числами не становятся."""
    for token in ("—", "-", "", "н/д", "1 2 3,4,5"):
        assert parse_amount(token, Grouping.RUSSIAN) is None


def test_minus_sign_survives_parsing() -> None:
    """Математический минус читается наравне с дефисом."""
    assert parse_amount("−15 008", Grouping.RUSSIAN) == Decimal(-15008)
    assert parse_amount("-15,008", Grouping.ENGLISH) == Decimal(-15008)


# --- правдоподобие выбранной конвенции ----------------------------------------


def test_plausibility_confirms_consistent_totals() -> None:
    """Согласованные величины подтверждают конвенцию и называют их число."""
    found = check_plausibility(
        {
            "ifrs.total_non_current_assets": Decimal(400_000),
            "ifrs.total_current_assets": Decimal(263_888),
            "ifrs.total_assets": Decimal(663_888),
            "ifrs.total_equity_and_liabilities": Decimal(663_888),
        },
        revenue=Decimal(507_718),
    )
    assert found.plausible
    assert found.checked == 3
    assert "сверено величин: 3" in found.describe()


def test_plausibility_catches_a_thousandfold_split() -> None:
    """Часть чисел, прочитанная по другой конвенции, расходится на три порядка.

    Ровно то, что случилось бы с ФосАгро при русском прочтении: «663,888»
    стало бы 663,888, а «1 234 567» осталось бы собой.
    """
    found = check_plausibility(
        {
            "ifrs.total_non_current_assets": Decimal(400_000),
            "ifrs.total_current_assets": Decimal(263_888),
            "ifrs.total_assets": Decimal("663.888"),
        }
    )
    assert not found.plausible
    assert found.checked == 1
    assert "кратно тысяче" in found.problems[0]


def test_plausibility_catches_implausible_assets_to_revenue() -> None:
    """Валюта баланса против порядка выручки: расхождение видно сразу."""
    found = check_plausibility(
        {"ifrs.total_assets": Decimal(663_888)}, revenue=Decimal("507.718")
    )
    assert not found.plausible
    assert "границ правдоподобия" in found.problems[0]


def test_plausibility_says_when_it_checked_nothing() -> None:
    """Проверять было нечего — так и сказано, а не «подтверждено»."""
    found = check_plausibility({})
    assert found.checked == 0
    assert "не проверялось" in found.describe()


def test_appendix_prints_the_convention(db_conn, tmp_path) -> None:
    """Конвенция печатается в приложении наравне с единицей измерения.

    Читатель должен иметь возможность проверить, как прочитаны числа: ошибка
    в разделителе разрядов даёт верные коэффициенты при неверных величинах,
    и по самому документу её иначе не увидеть.
    """
    from docx import Document

    from finlib.report.appendix import GROUPING_NAMES, GROUPING_NOT_APPLICABLE
    from finlib.report.document import build_report

    report = build_report("2100010824", db_conn, directory=tmp_path, with_text=False)
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert "Запись чисел в исходном документе" in text
    # Комплект получен из ГИР БО: числа приходят машиночитаемо, и графа
    # объясняет, почему конвенции нет, вместо того чтобы пустовать.
    assert GROUPING_NOT_APPLICABLE in text
    assert not any(name in text for name in GROUPING_NAMES.values())


def test_stored_convention_reaches_the_appendix(db_conn, tmp_path) -> None:
    """Определённая конвенция доходит из src_file до документа."""
    from docx import Document

    from finlib.db import execute
    from finlib.report.appendix import GROUPING_NAMES
    from finlib.report.document import build_report

    execute(
        "UPDATE src_file SET digit_grouping = 'english' "
        "WHERE inn = '2100010824' AND is_actual",
        conn=db_conn,
    )
    report = build_report("2100010824", db_conn, directory=tmp_path, with_text=False)
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert GROUPING_NAMES["english"] in text


def test_ordinary_mismatch_is_not_a_grouping_problem() -> None:
    """Несошедшийся итог сам по себе о конвенции не говорит.

    Проверяется именно кратность тысяче: расхождение в полтора раза бывает
    дефектом отчётности и ловится контролем сходимости, а не этим.
    """
    found = check_plausibility(
        {
            "ifrs.total_non_current_assets": Decimal(400_000),
            "ifrs.total_current_assets": Decimal(263_888),
            "ifrs.total_assets": Decimal(900_000),
        }
    )
    assert found.plausible
