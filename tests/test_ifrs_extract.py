"""Тесты извлечения форм МСФО и экрана сверки (задача 23).

Случаи взяты из разбора шести комплектов: неподписанный итог у Норникеля,
заголовок раздела вместо подписи итога у ФосАгро, сноска о средствах
на счетах эскроу у ЛСР.
"""

from datetime import date
from decimal import Decimal

from finlib.pipeline import accept_ifrs_document
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import identify
from finlib.sources.ifrs_numbers import Grouping
from finlib.sources.ifrs_review import ReviewOutcome, ReviewReason, review

DATES = (date(2024, 12, 31), date(2023, 12, 31))

# Баланс, у которого итог внеоборотных активов не подписан вовсе:
# случай Норникеля.
UNLABELLED_TOTAL = """
Консолидированный отчёт о финансовом положении
Основные средства                       700 000        650 000
Нематериальные активы                   200 000        180 000
Гудвил                                  100 000        100 000
                                      1 000 000        930 000
Запасы                                  300 000        280 000
Денежные средства и их эквиваленты      200 000        150 000
Итого оборотные активы                  500 000        430 000
Итого активы                          1 500 000      1 360 000
"""

# Полный комплект для экрана сверки: обе стороны баланса сходятся.
COMPLETE = """
Консолидированный отчёт о финансовом положении
(в миллионах российских рублей)
Пояснения      31 декабря 2024 года      31 декабря 2023 года
Основные средства                       700 000        650 000
Итого внеоборотные активы               700 000        650 000
Запасы                                  300 000        280 000
Денежные средства и их эквиваленты      500 000        430 000
Итого оборотные активы                  800 000        710 000
Итого активы                          1 500 000      1 360 000
Акционерный капитал                     400 000        400 000
Нераспределённая прибыль                200 000        160 000
Итого капитал                           600 000        560 000
Долгосрочные кредиты и займы            500 000        500 000
Итого долгосрочные обязательства        500 000        500 000
Краткосрочные кредиты и займы           400 000        300 000
Итого краткосрочные обязательства       400 000        300 000
Итого обязательства                     900 000        800 000
Итого капитал и обязательства         1 500 000      1 360 000

Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка                               1 200 000      1 100 000
Себестоимость продаж                    (800 000)      (750 000)
Валовая прибыль                         400 000        350 000
Коммерческие расходы                     (40 000)       (35 000)
Административные расходы                 (60 000)       (55 000)
Операционная прибыль                    300 000        260 000
Финансовые доходы                        10 000          8 000
Финансовые расходы                       (50 000)       (48 000)
Прибыль до налогообложения              260 000        220 000
Расход по налогу на прибыль              (52 000)       (44 000)
Прибыль за период                       208 000        176 000
"""


def extraction_of(text: str):
    """Разбор текста по русской конвенции и двум периодам."""
    return extract(text, DATES, Grouping.RUSSIAN)


# --- опознание позиций ---------------------------------------------------------


def test_positions_are_recognised_by_name() -> None:
    """Статья опознаётся по наименованию через справочник синонимов."""
    found = extraction_of(COMPLETE)
    assert found.value_of("ifrs.ppe", DATES[0]) == Decimal(700_000)
    assert found.value_of("ifrs.cash", DATES[0]) == Decimal(500_000)
    assert found.value_of("ifrs.revenue", DATES[0]) == Decimal(1_200_000)


def test_values_are_split_by_period() -> None:
    """Колонки раскладываются по отчётным датам в порядке их следования."""
    found = extraction_of(COMPLETE)
    assert found.value_of("ifrs.ppe", DATES[1]) == Decimal(650_000)
    assert found.value_of("ifrs.total_assets", DATES[1]) == Decimal(1_360_000)


def test_expense_in_brackets_keeps_its_sign() -> None:
    """Скобки — знак минус: величина расхода хранится отрицательной.

    Соглашение здесь не то же, что в РСБУ. Там вычитание задаёт оператор
    в составе итога, потому что отчётность печатает величину расхода без
    знака. В отчётности по МСФО знак стоит в самой форме, и отбрасывать его
    значило бы складывать расход с доходом.
    """
    found = extraction_of(COMPLETE)
    assert found.value_of("ifrs.cost_of_sales", DATES[0]) == Decimal(-800_000)


# --- неподписанные итоги -------------------------------------------------------


def test_unlabelled_total_is_recognised_by_structure() -> None:
    """Итог без подписи опознаётся равенством сумме предшествующих строк.

    У Норникеля итог внеоборотных активов — просто число. Наименования
    у него нет, и опереться можно только на структуру.
    """
    found = extraction_of(UNLABELLED_TOTAL)
    balance = found.forms["ifrs.statement_of_financial_position"]
    assert "ifrs.total_non_current_assets" in balance.totals_by_structure
    assert found.value_of("ifrs.total_non_current_assets", DATES[0]) == Decimal(
        1_000_000
    )


def test_structural_total_must_match_every_period() -> None:
    """Совпадение по одному периоду итогом не делает.

    По двум периодам случайное совпадение уже не проходит, и строка
    остаётся неопознанной — то есть уходит на экран сверки, а не в расчёт.
    """
    accidental = UNLABELLED_TOTAL.replace(
        "                                      1 000 000        930 000",
        "                                      1 000 000        111 111",
    )
    found = extraction_of(accidental)
    balance = found.forms["ifrs.statement_of_financial_position"]
    assert "ifrs.total_non_current_assets" not in balance.totals_by_structure
    assert any(item.values[0] == Decimal(1_000_000) for item in found.unrecognised)


# --- текст под формой ----------------------------------------------------------


def test_note_under_the_form_is_extracted() -> None:
    """Сноска под таблицей извлекается наравне с ней.

    У ЛСР под балансом сказано, что в состав денежных средств не включены
    средства на счетах эскроу — 217 501 млн руб. Парсер таблиц её не увидит,
    а без неё ликвидность читается неверно.
    """
    with_note = COMPLETE + (
        "\nВ состав денежных средств не включены средства на счетах эскроу "
        "в сумме 217 501 млн руб.\n"
    )
    found = extraction_of(with_note)
    assert any("эскроу" in note for note in found.notes)


# --- экран сверки --------------------------------------------------------------


HEADER = (
    "\n(в миллионах российских рублей)\n"
    "по состоянию на 31 декабря 2024 года и 31 декабря 2023 года\n"
    + "\nПримечания к консолидированной финансовой отчётности.\n" * 40
)


def profile_of(text: str):
    """Параметры документа для экрана сверки."""
    found = identify(text + HEADER)
    assert found.accepted, getattr(found, "reason", "")
    return found


def test_clean_extraction_passes_automatically() -> None:
    """Извлечение без единого основания проходит без участия человека.

    Режим автоматического прохождения — условие осуществимости ветки:
    при обязательном подтверждении скрининг ста эмитентов невозможен.
    """
    text = COMPLETE
    found = review(extraction_of(text), profile_of(text))
    assert found.outcome is ReviewOutcome.AUTOMATIC
    assert found.automatic
    assert found.totals_checked > 0, "автопрохождение при нуле проверок недопустимо"
    assert not found.reasons


def test_unrecognised_row_requires_confirmation() -> None:
    """Неопознанная строка — ручное подтверждение, без смягчений."""
    text = COMPLETE + "\nЗадолженность Принципала                  50 000     40 000\n"
    found = review(extraction_of(text), profile_of(text))
    assert found.outcome is ReviewOutcome.MANUAL_REQUIRED
    assert ReviewReason.UNRECOGNISED_POSITION in found.reasons


def test_material_specific_item_is_named_with_its_share() -> None:
    """Статья сверх порога существенности названа вместе с долей.

    У Автодора 85 % активов лежат в двух статьях, которых нет ни у кого
    другого. Такая статья не сворачивается в «прочее».
    """
    text = COMPLETE + "\nЗатраты в интересах Принципала         400 000    380 000\n"
    found = review(extraction_of(text), profile_of(text))
    assert ReviewReason.MATERIAL_SPECIFIC_ITEM in found.reasons
    assert found.material_items
    item = found.material_items[0]
    assert item.share_of_assets > Decimal("0.05")
    assert "Затраты в интересах Принципала" in item.describe()


def test_failed_total_requires_confirmation() -> None:
    """Несошедшийся итог — подтверждение, а не молчаливый пропуск."""
    broken = COMPLETE.replace(
        "Итого активы                          1 500 000      1 360 000",
        "Итого активы                          1 700 000      1 360 000",
    )
    found = review(extraction_of(broken), profile_of(broken))
    assert found.outcome is ReviewOutcome.MANUAL_REQUIRED
    assert ReviewReason.CHECK_FAILED in found.reasons
    assert found.totals_failed


def test_incomplete_reporting_kind_requires_confirmation() -> None:
    """Неполный вид отчётности — подтверждение по условию задания."""
    text = COMPLETE + "\nПромежуточная сокращённая консолидированная отчётность\n"
    found = review(extraction_of(text), profile_of(text))
    assert found.outcome is ReviewOutcome.MANUAL_REQUIRED
    assert ReviewReason.REPORTING_KIND in found.reasons


def test_review_reports_counters_not_only_reasons() -> None:
    """Экран сверки называет и число проверенного, и число сработавшего."""
    text = COMPLETE
    found = review(extraction_of(text), profile_of(text))
    assert found.rows_total >= found.rows_recognised > 0
    assert "итогов сверено" in found.describe()
    assert found.plausibility is not None and found.plausibility.checked > 0


def test_every_reason_carries_a_check_code() -> None:
    """Основание подтверждения уходит в журнал качества кодом контроля.

    Основание без кода в `dq_log` не попадёт, и причина отбраковки останется
    в памяти того, кто смотрел экран.
    """
    text = COMPLETE + "\nЗадолженность Принципала                  50 000     40 000\n"
    found = review(extraction_of(text), profile_of(text))
    assert found.check_codes
    assert len(found.check_codes) <= len(found.reasons)


# --- подключение к циклу -------------------------------------------------------


def test_pipeline_accepts_a_document_end_to_end() -> None:
    """Цикл проводит документ через приём, разбор и сверку.

    Этим вызовом контроли ветки МСФО и стали достижимы: до него весь разбор
    был написан, покрыт тестами и никем не вызывался.
    """
    text = COMPLETE + HEADER
    stages: list[str] = []
    found = accept_ifrs_document(text, on_stage=lambda item: stages.append(item.message))
    assert found.accepted
    assert found.review.automatic
    assert any("документ принят" in item for item in stages)
    assert any("итогов сверено" in item for item in stages)


def test_pipeline_refuses_an_annual_report() -> None:
    """Годовой отчёт эмитента отклоняется на приёме, до разбора форм."""
    annual = "Годовой отчёт за 2024 год. Стратегия и устойчивое развитие.\n" * 60
    found = accept_ifrs_document(annual)
    assert not found.accepted
    assert found.check_code == "file_not_statements"
