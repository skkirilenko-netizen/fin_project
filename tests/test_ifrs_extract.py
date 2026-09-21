"""Тесты извлечения форм МСФО и экрана сверки (задача 23).

Случаи взяты из разбора шести комплектов: неподписанный итог у Норникеля,
заголовок раздела вместо подписи итога у ФосАгро, сноска о средствах
на счетах эскроу у ЛСР.
"""

from datetime import date
from decimal import Decimal

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.pipeline import accept_ifrs_document
from finlib.sources.ifrs_extract import extract, materiality_base
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
    """Сноска извлекается в той вёрстке, в какой стоит у ЛСР.

    Прежний тест приписывал сноску **после** таблицы одной строкой — то есть
    проверял механизм, а не документ, и потому молчал, когда у настоящего
    комплекта ЛСР сноска не извлекалась вовсе. В документе она устроена иначе:
    знак сноски, четыре строки переноса, величина в последней, и всё это
    **выше** таблицы — между заголовком формы и шапкой колонок, потому что
    баланс продолжается со страницы, потерянной без текстового слоя.
    """
    with_note = COMPLETE.replace(
        "Пояснения      31 декабря 2024 года      31 декабря 2023 года",
        "* В состав статьи «Денежные средства и их эквиваленты» не включены\n"
        "денежные средства на счетах эскроу, полученные уполномоченным банком\n"
        "от владельцев счетов (участников долевого строительства) в сумме\n"
        "217 501 млн руб. на 31 декабря 2024 г. (2023 г.: 137 899 млн руб.).\n"
        "\n"
        "Пояснения      31 декабря 2024 года      31 декабря 2023 года",
    )
    found = extraction_of(with_note)
    note = next((item for item in found.notes if "эскроу" in item), "")
    assert note, "сноска не извлечена"
    # **Сноска без величины бесполезна там, где нужна.** Взятая одной строкой,
    # она называет предмет и умалчивает сумму: ликвидность девелопера
    # без 217 501 читается вчетверо выше.
    assert "217 501" in note


def test_note_marker_does_not_swallow_every_line() -> None:
    """Знак сноски сравнивается по сырому тексту, а не по приведённому.

    `normalize_name("*")` даёт пустую строку, а пустая строка входит в любой
    текст: то же, чем была проверка валюты по знаку «₽». Пока сноска искалась
    в хвосте блока — обычно пустом, — условие почти не срабатывало; по всему
    блоку оно дало 58 «сносок» из заголовков форм и колонтитулов.
    """
    found = extraction_of(COMPLETE)
    assert found.notes == (), f"сносок быть не должно, найдено: {found.notes}"


# --- экран сверки --------------------------------------------------------------


HEADER = (
    "\n(в миллионах российских рублей)\n"
    "по состоянию на 31 декабря 2024 года и 31 декабря 2023 года\n"
    + "\nПримечания к консолидированной финансовой отчётности.\n" * 40
)


# Оглавление: документ объявляет состав отчётности сам, и форма из перечня,
# в тексте не найденная, считается потерянной. Без перечня состав берётся
# обязательным по МСФО (IAS) 1 — тогда синтетический комплект из двух форм
# требовал бы и третьей.
CONTENTS = """
Содержание
Консолидированный отчёт о финансовом положении 3
Консолидированный отчёт о прибыли или убытке 4
Примечания к консолидированной финансовой отчётности 5
"""


def profile_of(text: str, contents: str = CONTENTS):
    """Параметры документа для экрана сверки."""
    found = identify(contents + text + HEADER)
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
    assert item.materiality_share > Decimal("0.05")
    # База названа, и это база **формы строки**: приписка стоит в конце
    # документа, то есть в отчёте о прибыли, и мерится выручкой.
    assert item.base == materiality_base(item.row.form, load_ifrs_lines())
    assert item.base in item.describe()
    assert "Затраты в интересах Принципала" in item.describe()
    # Измеренные строки считаются рядом со сработавшими: ноль статей сверх
    # порога при неизвестном числе измеренных ничего не означает.
    assert found.rows_measured >= 1


def test_cash_flow_row_is_never_material() -> None:
    """У строки потока порога существенности нет вовсе, а не нулевой.

    Поток за период правомерно кратен валюте баланса: у О'КЕЙ поступления
    от покупателей составляют 336,9 % её, а выплаты поставщикам 301,5 %.
    Прежде порог применялся ко строкам любой формы, и основание
    `material_specific_item` держало комплект по мере, которой у формы
    не существует: из 106 статей сверх порога 65 были строками потока.

    Основание обязано **исчезнуть**, а не получить нулевой порог: нулевой
    сработал бы на любой строке потока, то есть дал бы то же самое наоборот.
    """
    text = COMPLETE + (
        "\nКонсолидированный отчёт о движении денежных средств\n"
        "(в миллионах российских рублей)\n"
        "Прибыль до налогообложения                260 000        220 000\n"
        "Амортизация основных средств               40 000         38 000\n"
        "Изменение запасов                         (20 000)       (18 000)\n"
        "Проценты уплаченные                       (50 000)       (48 000)\n"
        "Налог на прибыль уплаченный               (52 000)       (44 000)\n"
        "Поступление денежных средств от покупателей  9 000 000  8 000 000\n"
        "Денежные средства, выплаченные поставщикам  (8 000 000)  (7 100 000)\n"
    )
    extraction = extraction_of(text)
    assert "ifrs.statement_of_cash_flows" in extraction.forms
    found = review(extraction, profile_of(text))
    flows = [
        item
        for item in found.material_items
        if item.row.form == "ifrs.statement_of_cash_flows"
    ]
    assert not flows, [item.describe() for item in flows]
    # Строка в очередь по-прежнему попадает — она не опознана, и об этом
    # основание своё: исчезла мера, а не строка.
    assert ReviewReason.UNRECOGNISED_POSITION in found.reasons
    # И она не вошла в число измеренных: мерить её нечем. Знаменатель при этом
    # печатается — ноль статей сверх порога при неизвестном числе измеренных
    # строк не означает ничего.
    unrecognised_flows = [
        item
        for item in extraction.unrecognised
        if item.form == "ifrs.statement_of_cash_flows"
    ]
    assert unrecognised_flows
    assert found.rows_measured == len(extraction.unrecognised) - len(unrecognised_flows)


def test_promised_form_that_is_missing_is_a_lost_page() -> None:
    """Форма, обещанная документом и не найденная, — потеря, а не отсутствие.

    У СИБУРа страница отчёта о прибылях — 7-я из 60 — лишена текстового слоя,
    и первой найденной формой стал отчёт о совокупном доходе. Признак
    потерянных страниц о ней молчал: окно считается между первой и последней
    **найденной** формой, и потеря первой формы оказывается до окна. Форма,
    потерянная целиком, выглядела как форма, которой в документе нет.
    """
    promised = CONTENTS + "Консолидированный отчёт о движении денежных средств 6\n"
    # Документ обещает три формы, а в тексте их две.
    profile = profile_of(COMPLETE, contents=promised)
    assert profile.expected_from == "contents"
    assert profile.missing_forms == ("ifrs.statement_of_cash_flows",)

    found = review(extraction_of(COMPLETE), profile)
    assert ReviewReason.LOST_PAGE in found.reasons
    assert any("не найдены" in item for item in found.problems)


def test_document_without_any_promise_expects_the_ias1_composition() -> None:
    """Нет ни заключения, ни оглавления — состав берётся обязательным по IAS 1.

    Умолчание здесь безопасно ровно потому, что ошибка обнаруживается сразу:
    формы, которой нет, недостаёт и в расчёте.
    """
    profile = profile_of(COMPLETE, contents="")
    assert profile.expected_from == "ias1"
    assert profile.missing_forms == ("ifrs.statement_of_cash_flows",)


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
    text = CONTENTS + COMPLETE + HEADER
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


# --- шапка таблицы и склейки, выверенные на живых выгрузках --------------------

HEADER_NOISE = """
Консолидированный отчёт о финансовом положении
Млн руб. Прим. 2025 2024
Основные средства 12 700 000 650 000
Итого внеоборотные активы 700 000 650 000
Запасы 13 300 000 280 000
Итого оборотные активы 300 000 280 000
Итого активы 1 000 000 930 000
"""


def test_table_header_is_not_an_item() -> None:
    """Шапка таблицы статьёй не становится.

    «Млн руб. Прим. 2025 2024» — номер колонки и годы, и строка выглядела
    статьёй: в очереди занимала место, а в недостаче итога давала слагаемое
    из ниоткуда.
    """
    found = extraction_of(HEADER_NOISE)
    balance = found.forms["ifrs.statement_of_financial_position"]
    assert not any("Прим" in row.source_name for row in balance.unrecognised)
    assert any(why == "auto_table_header" for _, _, why in balance.auto_dismissed)


def test_note_number_glued_to_the_first_value_is_separated() -> None:
    """Номер примечания, слипшийся с первой величиной, отделяется.

    У Сегежи «Добавочный капитал 19 116 179 35 122» — это примечание 19
    и величины 116 179 и 35 122. Отличие от «1 500 000 1 360 000» в том,
    что там величины смежных лет сравнимы, а здесь различаются в пятьсот
    сорок пять раз.
    """
    text = COMPLETE.replace(
        "Акционерный капитал                     400 000        400 000",
        "Акционерный капитал 19 116 179 35 122",
    )
    found = extraction_of(text)
    assert found.value_of("ifrs.share_capital", DATES[0]) == Decimal(116_179)
    assert found.value_of("ifrs.share_capital", DATES[1]) == Decimal(35_122)


def test_neighbouring_years_are_not_mistaken_for_a_note_number() -> None:
    """Величины смежных лет, различающиеся вдвое, не режутся.

    «Прочие операционные доходы, нетто 1 393 49» — это 1 393 и 49, а правило
    отсечения номера примечания превращало доход в 393.
    """
    text = COMPLETE.replace(
        "Финансовые доходы                        10 000          8 000",
        "Финансовые доходы 1 393 49",
    )
    found = extraction_of(text)
    assert found.value_of("ifrs.finance_income", DATES[0]) == Decimal(1393)
    assert found.value_of("ifrs.finance_income", DATES[1]) == Decimal(49)


# --- две спорные строки в одном разделе ----------------------------------------

# Случай Сегежи: в разделе внеоборотных активов спорны сразу две строки,
# и поодиночке ни одна итога не восстанавливает. «Гудвил 21 444» — это 21
# и 444 за два года, «Авансы 11 129 1 650» — примечание 11 и величины
# 129 и 1 650. Раздел сходится только при обеих исправленных сразу:
# 700 000 + 21 + 129 = 700 150.
TWO_CONTESTED = COMPLETE.replace(
    "Итого внеоборотные активы               700 000        650 000",
    "Гудвил 21 444\n"
    "Авансы, выданные под внеоборотные активы 11 129 1 650\n"
    "Итого внеоборотные активы 700 150 652 094",
).replace(
    "Итого активы                          1 500 000      1 360 000",
    "Итого активы 1 500 150 1 362 094",
)


def test_two_contested_rows_of_one_section_are_resolved_together() -> None:
    """Спорные строки раздела разбираются вместе, а не по одной.

    Поодиночке ни одна итога не восстанавливает, и правка каждой
    откатывалась: у Сегежи так и остались гудвил 21 444 вместо 21 и авансы
    11 129 вместо 129 — числа настоящие на вид, при сходящемся балансе.
    """
    found = extraction_of(TWO_CONTESTED)
    assert found.value_of("ifrs.goodwill", DATES[0]) == Decimal(21)
    assert found.value_of("ifrs.goodwill", DATES[1]) == Decimal(444)
    assert found.value_of("ifrs.advances_for_non_current_assets", DATES[0]) == Decimal(129)
    assert found.value_of("ifrs.advances_for_non_current_assets", DATES[1]) == Decimal(1650)


def test_note_reading_does_not_apply_to_a_negative_value() -> None:
    """Номер примечания не бывает отрицательным и не бывает в скобках.

    У ЛСР «Курсовые разницы при пересчете из других валют (9 202) 3 708»
    читались как примечание 9 и величина 202: знак терялся вместе с первой
    группой цифр, и совокупный доход переставал сходиться.
    """
    text = COMPLETE.replace(
        "Финансовые расходы                       (50 000)       (48 000)",
        "Финансовые расходы (9 202) (3 708)",
    )
    found = extraction_of(text)
    assert found.value_of("ifrs.finance_costs", DATES[0]) == Decimal(-9202)


def test_note_reading_is_not_applied_against_a_bound_alone() -> None:
    """Чтение с отсечением группы цифр требует арифметики раздела, а не границы.

    «Прибыль за год 10 778 28 598» по границе «слагаемое не больше итога»
    читалась бы как примечание 10 и прибыль 778: у ЛСР совокупный доход
    за год меньше прибыли, потому что прочий совокупный доход отрицателен.
    Такое чтение допускается только против сходимости раздела.
    """
    text = COMPLETE.replace(
        "Прибыль за период                       208 000        176 000",
        "Прибыль за период 10 778 28 598",
    )
    found = extraction_of(text)
    assert found.value_of("ifrs.profit_for_period", DATES[0]) == Decimal(10_778)
