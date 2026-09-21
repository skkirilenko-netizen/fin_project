"""Что читается в документе МСФО помимо таблиц форм: заключение, примечания, тип.

**Одно чтение на все пути загрузки.** Сведения эти собирал только цикл
(`pipeline.accept_ifrs_document`), а базу наполнял прогон приёма
(`eval/ifrs_intake_run.py`) — и он их не собирал вовсе. Шесть кодов
аудиторского заключения, подключённых в задаче 25, не появились ни у одного
комплекта: в журнале по ним ноль записей, и это выглядело как «оговорок нет».
Проверено на базе: у годового комплекта ФосАгро мнение с оговоркой, а записи
о ней нет.

Отсюда устройство: сведения читаются здесь, одной функцией, а запись комплекта
требует их **названным доводом** — довод, который можно молча не передать,
неотличим от невыполненного чтения. То же основание, по которому обязателен
`standards` у `compute_metric` и число месяцев берётся из отчётной даты,
а не задаётся вызывающим.
"""

import logging
from dataclasses import dataclass

from finlib.sources.ifrs_audit import AuditReport
from finlib.sources.ifrs_notes import NoteValue
from finlib.sources.pdf_text import PdfDocument

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentReading:
    """Прочитанное в документе помимо таблиц форм.

    Поля без умолчаний намеренно: каждое — отдельное сведение о комплекте,
    и пропуск любого из них означает не «сведения нет», а «не читали».
    Разница та же, что между `not_disclosed` и `missing`.
    """

    audit: AuditReport | None
    notes: tuple[NoteValue, ...]
    issuer_type: str | None

    def describe(self) -> str:
        """Однострочная сводка для журнала прогона."""
        taken = sum(1 for item in self.notes if item.found)
        audit = self.audit.describe() if self.audit is not None else "не читалось"
        return (
            f"аудиторское заключение: {audit}; величины примечаний: "
            f"взято {taken} из {len(self.notes)} объявленных; "
            f"тип эмитента: {self.issuer_type or 'не определён'}"
        )


def read_document(
    text: str,
    extraction,
    profile,
    headings: dict[str, int],
    document: PdfDocument | None = None,
) -> DocumentReading:
    """Читает заключение, величины примечаний и тип эмитента одним заходом.

    `headings` — смещения заголовков форм: заключение стоит до первой формы,
    примечания после последней, и оба рубежа структурные, а не по числу строк.
    `document` нужен заключению: без страниц нечитаемое заключение неотличимо
    от прочитанного, и признак всегда отвечал бы «потерь нет».
    """
    from finlib.sources.ifrs_audit import read_audit_report
    from finlib.sources.ifrs_issuer_type import determine_type

    before = min(headings.values(), default=0)
    audit = read_audit_report(text, document=document, before=before)
    notes = _note_values(text, extraction, profile, headings)
    issuer = determine_type(extraction.totals(profile.report_dates[0]), text)
    return DocumentReading(audit=audit, notes=notes, issuer_type=issuer.code)


def _note_values(
    text: str, extraction, profile, headings: dict[str, int]
) -> tuple[NoteValue, ...]:
    """Величины примечаний по ссылкам из строк форм — вместе с отказами.

    Ссылки берутся у величин **отчётного** периода: примечание расшифровывает
    строку формы, и номер ссылки стоит в ней. Отказ возвращается наравне
    с величиной: показатель, которому её не хватило, обязан назвать причину.
    """
    from finlib.sources.ifrs_notes import index_notes, note_values

    index = index_notes(text, after=min(headings.values(), default=0))
    rows = {
        item.code: item.note_reference
        for form in extraction.forms.values()
        for item in form.values
        if item.report_date == profile.report_dates[0]
    }
    _found, outcomes = note_values(
        index, rows, text, profile.grouping, len(profile.report_dates)
    )
    return tuple(outcomes)
