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
    issuer_name: str | None = None

    def describe(self) -> str:
        """Однострочная сводка для журнала прогона."""
        taken = sum(1 for item in self.notes if item.found)
        audit = self.audit.describe() if self.audit is not None else "не читалось"
        return (
            f"эмитент: {self.issuer_name or 'наименование не определено'}; "
            f"аудиторское заключение: {audit}; величины примечаний: "
            f"взято {taken} из {len(self.notes)} объявленных; "
            f"тип эмитента: {self.issuer_type or 'не определён'}"
        )


def read_document(
    text: str,
    extraction,
    profile,
    headings: dict[str, int],
    confirmed,
    document: PdfDocument | None = None,
) -> DocumentReading:
    """Читает заключение, величины примечаний и тип эмитента одним заходом.

    `headings` — смещения заголовков форм: заключение стоит до первой формы,
    примечания после последней, и оба рубежа структурные, а не по числу строк.
    `document` нужен заключению: без страниц нечитаемое заключение неотличимо
    от прочитанного, и признак всегда отвечал бы «потерь нет».

    `confirmed` — ранее подтверждённое опознание того же эмитента, довод
    **обязательный и позиционный**. Тип эмитента определяется структурными
    статьями, и у ЛСР обе статьи девелопера присвоены человеком: вердикт,
    смотревший только на опознанное справочником, отвечал «corporate»,
    поправка ликвидности не применялась, и текущая ликвидность 4,148 шла
    в балл группой 100 из 100 — при том, что 217 501 млн на счетах эскроу
    организации недоступны. Довод с умолчанием означал бы, что подтверждённое
    можно молча не передать, — это уже случалось с фактами комплекта.
    Передать «ничего» можно, но только назвав это.
    """
    from finlib.sources.ifrs_audit import read_audit_report
    from finlib.sources.ifrs_issuer_type import determine_type

    before = min(headings.values(), default=0)
    audit = read_audit_report(text, document=document, before=before)
    notes = _note_values(text, extraction, profile, headings)
    # **Опознание двух сил участвует в вердикте наравне.** Справочник
    # утверждает о строке вообще, подтверждение — о строке этого эмитента;
    # для признака типа этого довольно, и величина подтверждённой строки
    # такая же величина, как опознанная справочником.
    values = dict(extraction.totals(profile.report_dates[0]))
    if confirmed is not None:
        # Берутся **факты** подтверждённых строк: там присвоенный человеком код
        # стоит рядом со своей величиной. `values` и `extras` для этого
        # не годятся — первое сводит величины к кодам справочника, второе
        # к итогам разделов, и присвоенного кода нет ни в том, ни в другом.
        values |= {
            fact.code: fact.values[0] for fact in confirmed.facts if fact.values
        }
    issuer = determine_type(values, text)
    return DocumentReading(
        audit=audit,
        notes=notes,
        issuer_type=issuer.code,
        issuer_name=issuer_name(text, before),
    )


def issuer_name(text: str, before: int = 0, policy=None) -> str | None:
    """Наименование эмитента: титульный лист, подтверждённый колонтитулом.

    **Опора структурная.** Наименование стоит первой строкой титульного листа
    и повторяется колонтитулом каждой страницы, поэтому берётся не первое
    подходящее написание, а самое частое: в тексте отчётности называются
    и дочерние общества, и банки, и контрагенты, а колонтитул есть только
    у эмитента. Единственное вхождение наименованием эмитента не считается —
    правило то же, по которому неподписанный итог опознаётся совпадением
    по всем периодам сразу, а не по одному.

    `before` — смещение первой формы: титул и оглавление стоят до неё.
    """
    from finlib.sources.ifrs_numbers import load_parsing_policy

    policy = (policy or load_parsing_policy()).issuer_name
    limit = before or len(text)
    head, forms = text[:limit], text[limit:]
    counted: dict[str, int] = {}
    for line in head.splitlines():
        stripped = " ".join(line.split())
        if not stripped or len(stripped) > policy.max_length:
            continue
        if "«" not in stripped and '"' not in stripped:
            continue
        if not any(stripped.startswith(form) for form in policy.legal_forms):
            continue
        # **Колонтитул проходит через формы, подпись аудитора — нет.**
        # Наименование аудитора стоит в заключении и повторяется в нём же:
        # по числу вхождений в титул и заключение оно выигрывало у эмитента
        # трижды из семнадцати — «АО «Кэпт»» у Норникеля, «ООО «Б1 – Аудит»»
        # у Европлана и Самолёта. Это тот же худший исход поиска по словам:
        # не отсутствие ответа, а чужой ответ.
        times = forms.count(stripped)
        if times:
            counted[stripped] = times
    if not counted:
        logger.info("наименование эмитента в документе не найдено")
        return None
    name, times = max(counted.items(), key=lambda item: (item[1], -len(item[0])))
    if times < policy.min_occurrences:
        logger.info(
            "наименование «%s» встречено в формах %d раз: признаком "
            "не считается, колонтитул даёт больше",
            name,
            times,
        )
        return None
    return name


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
