"""Тесты осознанно игнорируемых наименований МСФО.

**Игнорируемое наименование — принятое решение, а неопознанное —
недоработка**, и путать их нельзя: то же различие, что у РСБУ между
`ignored_codes` и `unknown_line_code`. Прибыль на акцию методика
не использует принципиально — она относится к акции, а не к организации, —
и держать её в неопознанных значит вечно блокировать автопрохождение
из-за строки, которая нам не нужна.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_numbers import Grouping

DATES = (date(2025, 12, 31), date(2024, 12, 31))

PROFIT = """
Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка 1 200 000 1 100 000
Себестоимость продаж (800 000) (750 000)
Валовая прибыль 400 000 350 000
Прибыль до налогообложения 260 000 220 000
Расход по налогу на прибыль (52 000) (44 000)
Прибыль за период 208 000 176 000
Базовая и разводненная прибыль на акцию (в руб.) 882 652
"""


def test_every_ignored_subject_names_its_reason() -> None:
    """У каждого предмета объявлена причина: решение без причины — забытая строка."""
    catalog = load_ifrs_lines()
    assert catalog.ignored
    for subject in catalog.ignored:
        assert subject.reason.strip()


def test_earnings_per_share_is_ignored_not_unrecognised() -> None:
    """Прибыль на акцию не идёт ни в неопознанные, ни в величины."""
    found = extract(PROFIT, DATES, Grouping.RUSSIAN)
    assert [name for _form, name, _subject in found.ignored] == [
        "Базовая и разводненная прибыль на акцию (в руб.)"
    ]
    assert [row.source_name for row in found.unrecognised] == []
    # Величины у неё своя единица — рубли на акцию, — и в фактах комплекта
    # такому числу места нет.
    assert 882 not in [int(item.value) for item in found.values]


def test_ignored_rows_are_counted_apart() -> None:
    """Счётчик игнорируемых строк стоит рядом со счётчиком неопознанных.

    Ноль игнорируемых строк и «их никто не искал» — разные сведения,
    и сводка разбора обязана их различать.
    """
    found = extract(PROFIT, DATES, Grouping.RUSSIAN)
    assert "осознанно игнорируется 1" in found.describe()


def test_a_name_cannot_be_both_position_and_ignored() -> None:
    """Наименование, объявленное и позицией, и игнорируемым, — ошибка справочника.

    Иначе строка опознавалась бы и отбрасывалась одновременно, а какое
    из двух случится — зависело бы от порядка проверок в коде.
    """
    catalog = load_ifrs_lines()
    broken = catalog.model_dump()
    broken["ignored"] = [
        {
            "subject": "Выручка",
            "reason": "проверка: наименование занято позицией",
            "names": [{"name": "Выручка", "seen_at": "тест"}],
        }
    ]
    with pytest.raises(ValidationError, match="игнорируемыми"):
        IfrsCatalog.model_validate(broken)
