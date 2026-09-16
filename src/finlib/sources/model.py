"""Модель комплекта отчётности, общая для всех источников.

Прежде эти классы жили в `girbo.py`, и файловый источник импортировал бы
комплект из модуля конкретного источника. Комплект, организация и форма —
не деталь ГИР БО: одна и та же отчётность приходит и ответом ресурса,
и выгрузкой XLSX, разобранной вручную. Поэтому модель лежит отдельно,
а источники её наполняют.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.lines import ReportingType

# Сдвиг периода в годах назад от отчётного года комплекта.
PERIOD_OFFSETS: dict[str, int] = {"current": 0, "previous": 1, "beforePrevious": 2}

# Код налогового документа определяет набор строк отчётности. Код формы (ОКУД)
# у полной и упрощённой отчётности одинаковый, различать по нему нельзя.
KND_TO_REPORTING_TYPE: dict[str, ReportingType] = {
    "0710099": ReportingType.FULL,
    "0710096": ReportingType.SIMPLIFIED,
}


class SourceKind(StrEnum):
    """Откуда получен комплект; значения совпадают с CHECK в src_file.source."""

    GIR_BO = "gir_bo"
    # Файл отчётности, поданный вручную: источник недоступен либо отчётности
    # в нём нет, и комплект выгружен человеком.
    FILE = "file"


@dataclass(frozen=True, slots=True)
class Organization:
    """Реквизиты организации из карточки источника.

    `girbo_id` необязателен: в выгрузке XLSX идентификатора ресурса нет вовсе,
    а выдумывать его нельзя.
    """

    inn: str
    girbo_id: int | None = None
    short_name: str | None = None
    full_name: str | None = None
    ogrn: str | None = None
    kpp: str | None = None
    okpo: str | None = None
    okved: str | None = None
    okopf: str | None = None
    region: str | None = None


@dataclass(frozen=True, slots=True)
class FormData:
    """Значения одной формы по периодам: дата отчёта -> код строки -> значение.

    `names` заполняет только тот источник, который наименования строк отдаёт:
    в ответе ГИР БО их нет вовсе, а в выгрузке XLSX они есть, и для упрощённых
    форм именно наименование опознаёт строку (код в них — подсказка).
    """

    form_code: str
    values: dict[date, dict[str, Decimal | None]]
    names: dict[str, str] = field(default_factory=dict)
    # Наименования строк без кода, у которых раскрыто значение. Кодом они
    # не опознаются и в fact_report не идут; перечень нужен, чтобы объяснить
    # расхождение итога раздела, если такая строка в него входила.
    uncoded: tuple[str, ...] = ()

    @property
    def report_dates(self) -> tuple[date, ...]:
        """Периоды, за которые форма содержит данные, от свежего к старому."""
        return tuple(sorted(self.values, reverse=True))

    @property
    def depth(self) -> int:
        """Сколько периодов пришло по этой форме."""
        return len(self.values)


@dataclass(frozen=True, slots=True)
class ReportSet:
    """Один комплект отчётности: одна организация, один год, одна корректировка."""

    inn: str
    report_year: int
    report_date: date
    knd: str
    reporting_type: ReportingType
    correction_version: int
    is_actual: bool
    girbo_bfo_id: int | None = None
    forms: dict[str, FormData] = field(default_factory=dict)

    @property
    def form_codes(self) -> tuple[str, ...]:
        """Коды форм, пришедших в комплекте."""
        return tuple(sorted(self.forms))

    def report_dates(self, form_code: str) -> tuple[date, ...]:
        """Периоды, доступные по конкретной форме; у баланса их больше."""
        form = self.forms.get(form_code)
        return form.report_dates if form is not None else ()


def period_date(report_year: int, prefix: str) -> date:
    """Дата отчёта для периода комплекта: 31 декабря соответствующего года."""
    return date(report_year - PERIOD_OFFSETS[prefix], 12, 31)
