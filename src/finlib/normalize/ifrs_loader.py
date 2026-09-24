"""Запись комплекта МСФО в базу: src_file, факты, журнал качества.

Загрузка идёт одной транзакцией, как и у РСБУ: снятие признака актуальности
с прежних версий, запись комплекта и запись фактов происходят вместе. Иначе
при сбое посередине в базе остаётся либо ноль актуальных версий, либо две.

Ключи те же, что у РСБУ, и стандарт входит в них: `fact_report` различает
величину по МСФО и по РСБУ за одну и ту же дату. Без этого расчёт по одному
стандарту молча затирал бы значения другого.

Роль периода назначается по порядку колонок: первая отчётная дата — `current`,
остальные — сравнительные. Это то же правило приоритета, что в РСБУ:
сравнительное значение не затирает отчётное, а расхождение между ними —
содержательный сигнал о переклассификации, а не техническая деталь.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.db import PgConnection, execute, fetch_all, fetch_one
from finlib.normalize.facts import PRIORITY_WHERE
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.normalize.loader import PERIOD_RANK
from finlib.quality.codes import (
    LOADER_SEVERITY,
    MAPPING_CODES,
    CheckCode,
    CheckStatus,
    Severity,
)
from finlib.quality.journal import CheckRecord, log_records
from finlib.quality.values import sign_only_difference
from finlib.sources.ifrs_confirmed import Confirmed, ConfirmedFact, match_key
from finlib.sources.ifrs_document import DocumentReading
from finlib.sources.ifrs_extract import Extraction, materiality_share
from finlib.sources.ifrs_inbox import DocumentProfile
from finlib.sources.ifrs_notes import NoteValue
from finlib.sources.ifrs_review import REASON_CODES, ReviewResult
from finlib.standards import Standard
from finlib.version import code_version

logger = logging.getLogger(__name__)

# Роли периодов по порядку колонок документа. Четвёртой и далее колонке роли
# нет: модель хранит три, как и для РСБУ.
PERIOD_ROLES: tuple[str, ...] = ("current", "previous", "before_previous")


def form_of(line_code: str) -> str | None:
    """Форма, которой принадлежит позиция справочника; None — позиция чужая.

    Нужна записи величин примечаний: примечание расшифровывает строку формы,
    и факт принадлежит **её** форме.
    """
    position = load_ifrs_lines().get(line_code)
    return position.form if position is not None else None


class Recognition(StrEnum):
    """Чем строка опознана: справочником или подтверждением человека.

    Две силы опознания, и доверие к ним разное: справочник утверждает о строке
    с таким наименованием вообще, подтверждение — о строке **этого** эмитента.
    В расчёт величины идут наравне, в документе печатаются порознь. Значение
    хранится в `fact_report.recognition`, и свободных строк в коде для него
    быть не должно.
    """

    CATALOG = "catalog"
    CONFIRMATION = "confirmation"
    # Величина взята из примечания по ссылке из строки формы. Третья сила,
    # и слабее двух прочих она не потому, что менее достоверна, а потому,
    # что добыта нами: строка формы её не содержит вовсе.
    NOTE = "note"

# Записи, описывающие **состояние** комплекта, а не событие: сводка
# сопоставления строк, сводка столкновений периодов, основания экрана сверки
# и сведения аудиторского заключения. Повторная загрузка снимает прежние
# и кладёт нынешние — иначе счётчики множатся от прогона к прогону, а
# исправленный справочник не снимает карантин, поставленный по устаревшей
# записи. Правило то же, что у РСБУ (`MAPPING_CODES`), и семейство здесь шире
# ровно на то, что у РСБУ не пишется.
#
# История в это семейство не входит и не переписывается никогда: расхождение
# периодов, расхождение соглашения о знаке и перезапись значения — события.
STATE_CODES: frozenset[CheckCode] = MAPPING_CODES | frozenset(
    {
        CheckCode.PERIOD_PRIORITY,
        CheckCode.AUDIT_OPINION_MODIFIED,
        CheckCode.AUDIT_GOING_CONCERN,
        CheckCode.AUDIT_STATEMENTS_RESTATED,
        CheckCode.AUDIT_REPORT_NOT_READABLE,
        CheckCode.AUDIT_REPORT_ABSENT,
        CheckCode.AUDIT_REVIEW_ENGAGEMENT,
        # Величины примечаний и отказы их извлечения описывают комплект:
        # исправленный справочник примечаний обязан менять эти записи,
        # а не добавлять к прежним.
        CheckCode.NOTE_VALUES,
        CheckCode.NOTE_VALUE_NOT_EXTRACTED,
    }
)

# Коды, которыми основания экрана сверки писались **прежде**. Переименование
# кода контроля не должно оставлять за собой старую запись: она описывает
# то же состояние комплекта, и документ печатал оба имени разом — «полнота вида
# отчётности» и «определение типа отчётности по содержимому файла» об одном
# и том же основании. Удаление ограничено комплектом МСФО, поэтому те же коды
# у контролей РСБУ не задеваются.
RETIRED_REASON_CODES: frozenset[CheckCode] = frozenset(
    {
        CheckCode.SECTION_SUM,
        CheckCode.LINE_NOT_RECOGNIZED,
        CheckCode.FILE_REPORTING_TYPE_UNKNOWN,
        CheckCode.FILE_TEXT_LAYER_MISSING,
    }
)

# Организация по МСФО может быть новой: в базе РСБУ её нет, если отчётность
# по ней не загружалась. Наименование не выдумывается — оно остаётся пустым
# до тех пор, пока не будет извлечено из документа: назвать организацию
# по имени файла значило бы взять название из того, что назначил человек,
# выгружавший отчётность.
_ENSURE_ORGANIZATION = """
INSERT INTO organization (inn, name) VALUES (%(inn)s, %(name)s)
ON CONFLICT (inn) DO UPDATE SET
    name = COALESCE(organization.name, EXCLUDED.name),
    updated_at = now()
"""

_DROP_ACTUAL = """
UPDATE src_file SET is_actual = false
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(report_year)s
  AND id <> %(keep)s AND is_actual
"""

_UPSERT_SRC_FILE = """
INSERT INTO src_file (
    inn, standard, report_year, period_end, source, raw_path, checksum, form_codes,
    correction_version, is_actual, reporting_type, reporting_kind, unit_code,
    unit_source, digit_grouping, status, meta, code_version
) VALUES (
    %(inn)s, %(standard)s, %(report_year)s, %(period_end)s, 'file', %(raw_path)s,
    %(checksum)s,
    %(form_codes)s, %(correction_version)s, true, 'full', %(reporting_kind)s,
    %(unit_code)s, 'explicit', %(digit_grouping)s, %(status)s, %(meta)s,
    %(code_version)s
)
ON CONFLICT (inn, standard, period_end, source, correction_version) DO UPDATE SET
    raw_path = EXCLUDED.raw_path,
    checksum = EXCLUDED.checksum,
    form_codes = EXCLUDED.form_codes,
    is_actual = true,
    reporting_kind = EXCLUDED.reporting_kind,
    unit_code = EXCLUDED.unit_code,
    digit_grouping = EXCLUDED.digit_grouping,
    status = EXCLUDED.status,
    meta = EXCLUDED.meta,
    code_version = EXCLUDED.code_version,
    loaded_at = now()
RETURNING id
"""

_UPSERT_FACT = """
INSERT INTO fact_report (
    src_file_id, inn, standard, report_date, form_code, line_code,
    source_line_code, value, value_status, period_role, recognition,
    note_number, note_source_name
) VALUES (
    %(src_file_id)s, %(inn)s, %(standard)s, %(report_date)s, %(form_code)s,
    %(line_code)s, %(source_line_code)s, %(value)s, 'ok', %(period_role)s,
    %(recognition)s, %(note_number)s, %(note_source_name)s
)
ON CONFLICT (inn, standard, report_date, form_code, line_code) DO UPDATE SET
    src_file_id = EXCLUDED.src_file_id,
    source_line_code = EXCLUDED.source_line_code,
    value = EXCLUDED.value,
    period_role = EXCLUDED.period_role,
    recognition = EXCLUDED.recognition,
    note_number = EXCLUDED.note_number,
    note_source_name = EXCLUDED.note_source_name,
    updated_at = now()
-- **Совпавшее значение не переписывается, но владелец факта уточняется.**
-- Условие сравнивало только величину, и факт оставался за комплектом, который
-- загрузился первым: у ПАО «Сегежа Групп» промежуточный комплект записал
-- 37 балансовых величин на 31.12.2025 сравнительной колонкой, а пришедший
-- вслед годовой те же величины отчётной колонкой не переписал — они совпали.
-- Факты годового комплекта остались с ролью `previous` и с чужим
-- `src_file_id`, и при карантине промежуточного комплекта баланс годового
-- исчезал из расчёта целиком.
--
-- Перечень сравниваемых граф тот же, что у РСБУ (`normalize/loader.py`):
-- правило одно, и расходиться ему нельзя. Само условие приоритета живёт
-- в `normalize/facts.py`: его вставляют оба загрузчика, и второго определения
-- правила быть не должно.
""" + PRIORITY_WHERE

_EXISTING = """
SELECT report_date, form_code, line_code, value, period_role, src_file_id
FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
"""

_CLEAR_STATE_RECORDS = """
DELETE FROM dq_log WHERE src_file_id = %(id)s AND check_code = ANY(%(codes)s)
"""

# Место строки пишется и здесь: ключ уникальности строится по строке
# комплекта, и без индекса повторное подтверждение ложилось бы рядом
# с прежним вместо того, чтобы его заменить.
_INSERT_CONFIRMATION = """
INSERT INTO ifrs_line_confirmation (
    code, inn, src_file_id, report_date, source_name, match_key, form_code, value,
    materiality_share, confirmed_by, note, row_index
) VALUES (
    %(code)s, %(inn)s, %(src_file_id)s, %(report_date)s, %(source_name)s,
    %(match_key)s, %(form_code)s, %(value)s, %(share)s, %(confirmed_by)s,
    %(note)s, %(index)s
)
ON CONFLICT (inn, report_date, form_code, row_index) DO UPDATE SET
    code = EXCLUDED.code,
    source_name = EXCLUDED.source_name,
    match_key = EXCLUDED.match_key,
    src_file_id = EXCLUDED.src_file_id,
    value = EXCLUDED.value,
    materiality_share = EXCLUDED.materiality_share,
    confirmed_by = EXCLUDED.confirmed_by,
    note = EXCLUDED.note,
    confirmed_at = now()
"""


@dataclass
class Collisions:
    """Столкновения входящих величин с уже загруженными за тот же период.

    **Счётчик сверенного стоит рядом со счётчиком сработавшего.** Правило
    приоритета — «сравнительное значение не затирает отчётное» — до сих пор
    не имело ни одного наблюдения: расхождений в журнале не появлялось,
    и молчание читалось как исправная работа, хотя столкновений попросту
    не происходило. Ноль отклонений при неизвестном числе сверок ничего
    не означает.
    """

    # Сколько входящих величин встретили уже загруженную за тот же период.
    checked: int = 0
    # Сколько записей вообще предъявлено базе: величины форм, величины
    # по подтверждению человека и величины примечаний вместе. Знаменатель
    # обязан покрывать всё, что считается в числителе: «фактов записано 0
    # из 166, без изменений 177» — сложение, которое не сходится, и читатель
    # правильно ему не верит.
    attempted: int = 0
    # Величины, база которых не тронула: значение то же, что уже лежало.
    # Без этой графы «фактов записано 0 из 50» читается как несостоявшаяся
    # загрузка, тогда как это повторный прогон того же комплекта.
    unchanged: int = 0
    # Совпали до копейки: столкновение было, спора не было.
    agreed: int = 0
    # Сколько фактов записано не справочником, а по подтверждению человека.
    # Графа отдельная, потому что доверие к двум силам опознания разное,
    # а «фактов записано N» одним числом этого не показывает.
    by_confirmation: int = 0
    # Сколько фактов пришло из примечаний — по ссылке из строки формы.
    by_note: int = 0
    # Приоритет оставил загруженное значение: входящее хуже по роли периода.
    kept_by_priority: int = 0
    # Входящее значение победило: роль не хуже загруженной.
    overwritten: int = 0
    revisions: tuple["Clash", ...] = ()
    # Расхождения, различающиеся только знаком при равной величине.
    sign_only: tuple["Clash", ...] = ()
    # Величины того же комплекта, перезаписанные повторной загрузкой. Это
    # не столкновение периодов, а исправление разбора, и в счётчики
    # столкновений оно не входит: путать их — значит приписывать эмитенту
    # правку нашего парсера.
    rewritten: tuple["Clash", ...] = ()

    def describe(self) -> str:
        """Однострочная сводка со знаменателем."""
        return (
            f"сверено с загруженным {self.checked} величин: совпало "
            f"{self.agreed}, приоритет оставил отчётное {self.kept_by_priority}, "
            f"пересмотров {len(self.revisions)}, расхождений знака "
            f"{len(self.sign_only)}, перезаписано своих {len(self.rewritten)}"
        )


@dataclass
class LoadResult:
    """Итог записи комплекта МСФО."""

    src_file_id: int
    inn: str
    facts_written: int = 0
    facts_total: int = 0
    revisions: tuple[str, ...] = ()
    quarantined: bool = False
    confirmations: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)
    collisions: Collisions = field(default_factory=Collisions)

    def summary(self) -> str:
        """Однострочная сводка со счётчиками проверенного."""
        return (
            f"ИНН {self.inn}, комплект {self.src_file_id}: фактов записано "
            f"{self.facts_written} из {self.facts_total} предъявленных, из них "
            f"по подтверждению человека {self.collisions.by_confirmation}, "
            f"из примечаний {self.collisions.by_note}, "
            f"без изменений {self.collisions.unchanged}, "
            f"{self.collisions.describe()}, расхождений "
            f"со сравнительными данными {len(self.revisions)}, "
            f"подтверждённых статей {self.confirmations}, "
            + ("КАРАНТИН" if self.quarantined else "расчёт разрешён")
        )


def load_extraction(
    inn: str,
    extraction: Extraction,
    profile: DocumentProfile,
    review: ReviewResult,
    conn: PgConnection,
    reading: DocumentReading,
    *,
    raw_path: str | None = None,
    checksum: str | None = None,
    correction_version: int = 0,
    confirmed_by: str | None = None,
    confirmations: dict[str, str] | None = None,
    confirmed: Confirmed | None = None,
    accepted: dict[str, str] | None = None,
    organization_name: str | None = None,
    caveat_kind: str | None = None,
) -> LoadResult:
    """Пишет принятый комплект МСФО одной транзакцией.

    confirmations — коды, присвоенные человеком неопознанным статьям:
    наименование в отчётности → код позиции. Пустой словарь означает, что
    подтверждения не было, и статьи остались неопознанными.

    confirmed — ранее подтверждённое опознание того же эмитента. Его величины
    идут в факты наравне с опознанными справочником, с пометкой источника
    опознания: прежде они не писались вовсе, и размеченные статьи в расчёт
    не попадали.

    reading — прочитанное в документе помимо таблиц: аудиторское заключение,
    величины примечаний, тип эмитента. Довод **обязательный и позиционный**:
    прежде это были три названных довода с умолчаниями, цикл их передавал,
    а прогон приёма — нет, и базу наполнял именно он. Шесть кодов заключения
    не появились ни у одного комплекта, и журнал выглядел так, будто
    оговорок нет.

    accepted — основания экрана сверки, принятые человеком: код основания →
    причина словами. **Карантин снимается только по названным основаниям.**
    Прежде разметка строк снимала его целиком: у комплекта Сегежи вместе
    с неопознанными строками молча принимались несошедшийся итог и неполный
    вид отчётности — то есть провал блокирующего контроля принимался побочно,
    без решения и без причины.

    Комплект, не прошедший экран сверки без подтверждения, уходит в карантин:
    извлечение, о котором машина не знает, что перед ней, в расчёт не идёт.
    """
    notes = reading.notes
    execute(
        # Наименование организации приходит из самого документа: прежде оно
        # передавалось названным доводом, которого не передавал никто, и у всех
        # комплектов МСФО наименования в базе не было — заключение выходило
        # с ИНН в шапке вместо наименования.
        _ENSURE_ORGANIZATION,
        {"inn": inn, "name": organization_name or reading.issuer_name},
        conn=conn,
    )
    report_date = profile.report_dates[0]
    unconfirmed = _unconfirmed(
        extraction, confirmations or {}, frozenset(review.rows_confirmed)
    )
    # **Карантин снимается по названным основаниям, а не подтверждением
    # вообще.** Прежде довольно было автора подтверждения и размеченных строк:
    # у комплекта Сегежи вместе с ними молча принимались несошедшийся итог
    # и неполный вид отчётности. Теперь каждое основание экрана должно быть
    # названо человеком с причиной, а неразмеченная строка не принимается
    # ничем: извлечение без неё неполно.
    fingerprint = input_fingerprint(extraction)
    accepted, confirmed_by = _accepted_with_previous(
        inn,
        profile.report_dates[0].year,
        correction_version,
        conn,
        accepted,
        confirmed_by,
        fingerprint,
    )
    grounds = {item.value for item in review.reasons}
    covered = bool(confirmed_by) and not unconfirmed and grounds <= set(accepted)
    quarantined = not review.automatic and not covered

    # Вид оговорки аудитора — решение человека о комплекте, и повторный разбор
    # его не стирает: не названный в этой загрузке, он берётся из прежней записи.
    audit = _audit_with_caveat(
        inn,
        profile.report_dates[0].year,
        correction_version,
        conn,
        reading,
        caveat_kind,
        confirmed_by,
    )

    src_file_id = _write_src_file(
        inn,
        profile,
        review,
        conn,
        raw_path=raw_path,
        checksum=checksum,
        correction_version=correction_version,
        quarantined=quarantined,
        reading=reading,
        accepted=accepted,
        confirmed_by=confirmed_by,
        audit=audit,
        fingerprint=fingerprint,
        footnotes=tuple(
            (code, note)
            for code, form in extraction.forms.items()
            for note in form.notes_under_form
        ),
    )

    # Присвоения этого присеста — такое же подтверждение человека, как и
    # прежние: величина строки идёт в факты с той же пометкой.
    fresh = _fresh_facts(extraction, confirmations or {})
    written, collisions = _write_facts(
        inn,
        src_file_id,
        extraction,
        profile,
        conn,
        confirmed=(*(confirmed.facts if confirmed else ()), *fresh),
        notes=notes,
    )
    revisions = [item.describe() for item in collisions.revisions]

    records = _journal_records(
        inn,
        src_file_id,
        profile,
        extraction,
        review,
        collisions,
        quarantined,
        notes,
        accepted,
        confirmed_by,
    )
    records.extend(_audit_records(inn, src_file_id, audit))
    execute(
        _CLEAR_STATE_RECORDS,
        {
            "id": src_file_id,
            "codes": sorted(
                {code.value for code in STATE_CODES}
                | {code.value for code in REASON_CODES.values()}
                | {code.value for code in RETIRED_REASON_CODES}
            ),
        },
        conn=conn,
    )
    log_records(records, conn=conn)

    saved = 0
    if confirmations and confirmed_by:
        saved = _save_confirmations(
            inn, src_file_id, report_date, extraction, confirmations, confirmed_by, conn
        )

    result = LoadResult(
        src_file_id=src_file_id,
        inn=inn,
        facts_written=written,
        # Знаменатель — число предъявленных базе записей, а не число величин
        # форм: к ним добавляются величины по подтверждению человека и величины
        # примечаний, и «записано 0 из 166, без изменений 177» было сложением,
        # которое не сходится.
        facts_total=collisions.attempted,
        revisions=tuple(revisions),
        quarantined=quarantined,
        confirmations=saved,
        notes=extraction.notes,
        collisions=collisions,
    )
    logger.info("загрузка МСФО: %s", result.summary())
    return result


_PREVIOUS_AUDIT = """
SELECT meta -> 'audit' AS audit FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(year)s
  AND source = 'file' AND correction_version = %(version)s
"""

_PREVIOUS_ACCEPTED = """
SELECT meta -> 'accepted' AS accepted FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(year)s
  AND source = 'file' AND correction_version = %(version)s
"""


def input_fingerprint(extraction: Extraction) -> str:
    """Отпечаток входных величин, которые человек видел, принимая комплект.

    **Решение человека относится к тому извлечению, которое он смотрел.**
    Перенос основания по совпадению кодов оснований этого не обеспечивает:
    правка разбора меняет сами величины, оставляя перечень оснований прежним, —
    и подтверждение молча распространилось бы на другие числа. У ЛСР так
    менялся долгосрочный долг (35 876 → 328 256) при неизменном перечне
    оснований.

    В отпечаток входит то, что человек проверял глазами: величины всех форм
    вместе с формой, кодом и датой и наименования неопознанных строк. Порядок
    строк на отпечаток не влияет — он свойство разбора, а не извлечения.

    **Оснований экрана в отпечатке нет намеренно.** Они меняются от разметки
    строк, а не от правки разбора, и сверяются отдельно: перенос не снимает
    основания, которого человек не принимал (`grounds <= set(accepted)`).
    Считая их здесь, мы сбрасывали бы подтверждение ровно тогда, когда человек
    доразметил строки, то есть наказывали бы за выполненную работу.
    """
    values = sorted(
        f"{code}|{item.code}|{item.report_date:%Y-%m-%d}|{item.value}"
        for code, form in extraction.forms.items()
        for item in form.values
    )
    unrecognised = sorted(row.source_name for row in extraction.unrecognised)
    payload = "\n".join([*values, "--", *unrecognised])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _accepted_with_previous(
    inn: str,
    report_year: int,
    correction_version: int,
    conn: PgConnection,
    accepted: dict[str, str] | None,
    confirmed_by: str | None,
    fingerprint: str = "",
) -> tuple[dict[str, str], str | None]:
    """Принятые человеком основания переживают повторный разбор.

    **Решение человека о комплекте — то же, что вид оговорки аудитора.**
    Оно устанавливается не документом, и повторная загрузка его читать
    неоткуда: не названное в этой загрузке, оно берётся из прежней записи
    комплекта. Иначе всякий прогон приёма возвращал бы в карантин комплекты,
    принятые вручную, — у ЛСР вместе с основанием «страница 6 без слоя, все
    14 итогов сошлись, статьи сверены с Cbonds» пропадал бы и автор решения.

    Безопасность правила держится тем же, чем у разметки: перенос не снимает
    **новых** оснований. Если разбор изменился и экран сверки назвал основание,
    которого человек не принимал, `grounds <= set(accepted)` не выполняется
    и комплект уходит в карантин — то есть решение переносится о том же
    извлечении, а не о любом.

    **Молчание о основаниях и основание с пустой причиной — разные вещи.**
    Прогон приёма об основаниях не говорит вовсе (`None`), и прежнее решение
    в силе; названное пустой причиной — решение не принимать, и переносить
    поверх него прежнее значило бы отменить отказ человека.

    **Решение привязано к версии входных величин и сбрасывается, когда они
    изменились** (`input_fingerprint`). Совпадения перечня оснований для
    переноса мало: правка разбора меняет сами числа, оставляя перечень
    прежним, — у ЛСР так менялся долгосрочный долг с 35 876 на 328 256.
    Отпечаток не сошёлся — подтверждение не переносится, комплект уходит
    в карантин, и человек смотрит извлечение заново.
    """
    if accepted is not None:
        return {
            code: text for code, text in accepted.items() if text.strip()
        }, confirmed_by
    row = fetch_one(
        _PREVIOUS_ACCEPTED,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "year": report_year,
            "version": correction_version,
        },
        conn=conn,
    )
    previous = (row or {}).get("accepted") or {}
    grounds = {
        code: text
        for code, text in (previous.get("grounds") or {}).items()
        if text and text.strip()
    }
    if not grounds:
        return {}, confirmed_by
    stored = previous.get("fingerprint") or ""
    if not stored:
        # Подтверждение принято до появления поля: отпечатка у него нет,
        # и сверять не с чем. Оно переносится один раз — эта же загрузка
        # отпечаток и запишет, поэтому дальше правило работает в полную силу.
        logger.info("принятие комплекта перенесено без сверки: отпечаток не записан")
        return grounds, confirmed_by or previous.get("by")
    if stored != fingerprint:
        logger.info(
            "принятие комплекта не перенесено: входные величины изменились "
            "(отпечаток %s против %s)",
            stored[:12] or "не записан",
            fingerprint[:12],
        )
        return {}, confirmed_by
    return grounds, confirmed_by or previous.get("by")


def _audit_with_caveat(
    inn: str,
    report_year: int,
    correction_version: int,
    conn: PgConnection,
    reading: DocumentReading,
    caveat_kind: str | None,
    confirmed_by: str | None,
) -> object | None:
    """Заключение с видом оговорки, подтверждённым человеком.

    **Решение человека переживает повторный разбор.** Сведения заключения
    читаются из документа заново при каждой загрузке, а вид оговорки читается
    не оттуда: его устанавливает человек. Не названный в этой загрузке, он
    берётся из прежней записи комплекта — иначе подтверждение исчезало бы
    при всякой правке разбора, как исчезала бы разметка строк.
    """
    from dataclasses import replace

    if reading.audit is None:
        return None
    if caveat_kind:
        return replace(
            reading.audit,
            caveat_kind=caveat_kind,
            caveat_confirmed_by=confirmed_by or "",
        )
    row = fetch_one(
        _PREVIOUS_AUDIT,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "year": report_year,
            "version": correction_version,
        },
        conn=conn,
    )
    previous = (row or {}).get("audit") or {}
    if not previous.get("caveat_kind"):
        return reading.audit
    return replace(
        reading.audit,
        caveat_kind=previous["caveat_kind"],
        caveat_confirmed_by=previous.get("caveat_confirmed_by", ""),
    )


def _write_src_file(
    inn: str,
    profile: DocumentProfile,
    review: ReviewResult,
    conn: PgConnection,
    *,
    raw_path: str | None,
    checksum: str | None,
    correction_version: int,
    quarantined: bool,
    reading: DocumentReading,
    accepted: dict[str, str],
    confirmed_by: str | None,
    audit: object | None = None,
    fingerprint: str = "",
    footnotes: tuple[tuple[str, str], ...] = (),
) -> int:
    """Записывает комплект и снимает актуальность с прежних версий года."""
    report_year = profile.report_dates[0].year
    meta = {
        "reporting_kind": profile.reporting_kind.value,
        "review_outcome": review.outcome.value,
        "review_reasons": [item.value for item in review.reasons],
        "notes_under_forms": list(profile.forms),
        # **Сноска под формой — раскрытие эмитента, и хранится она дословно.**
        # Прежде в комплект писались только коды форм, у которых сноска нашлась,
        # а сам текст оставался в разборе: документ собирается из базы, файла
        # при сборке нет, и величина сноски в заключение попасть не могла.
        # У ЛСР так пропадали 217 501 млн руб. на счетах эскроу — величина,
        # ради которой сноски и извлекаются.
        "footnotes": [{"form": form, "text": text} for form, text in footnotes],
        "grouping_evidence": profile.grouping_detection.describe(),
        # Опознание двух сил, порознь: справочник утверждает о строке вообще,
        # ранее подтверждённое — о строке этого эмитента. В документ идут
        # оба числа, потому что доверие к ним разное.
        "recognition": {
            "by_catalog": review.rows_recognised,
            "by_confirmation": len(review.rows_confirmed),
            "rows_total": review.rows_total,
            "confirmed_from": list(review.confirmed_from),
        },
        # Тип эмитента опознаётся статьями и текстом документа, а расчёт
        # по фактам базы документа не видит: без записи поправка показателя
        # по типу молча не применялась бы.
        "issuer_type": reading.issuer_type,
        # **Сведения заключения хранятся с комплектом.** Документ собирается
        # из базы, самого файла при этом нет, и оговорку аудитора взять больше
        # неоткуда: у ФосАгро мнение с оговоркой не дошло до документа вовсе.
        # Хранятся факты и дословный текст разделов, формулировки — из методики
        # в момент сборки.
        "audit": audit.as_meta() if audit is not None else None,
        # **Принятое человеком основание хранится вместе с причиной.** Решение
        # «я принимаю несошедшийся итог, потому что…» — сведение о комплекте,
        # и документ обязан его напечатать: провал блокирующего контроля,
        # принятый молча, неотличим от контроля, который не провалился.
        "accepted": {
            "by": confirmed_by,
            "grounds": {code: " ".join(text.split()) for code, text in accepted.items()},
            # Отпечаток входных величин, которые человек видел. Решение
            # переносится на повторную загрузку только при его совпадении:
            # перечень оснований от правки разбора не меняется, а числа
            # меняются, и подтверждение распространилось бы на другие.
            "fingerprint": fingerprint,
        }
        if accepted
        else None,
    }
    row = fetch_one(
        _UPSERT_SRC_FILE,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "report_year": report_year,
            # Период в ключе комплекта: у эмитента за год бывает годовой
            # комплект и промежуточный, и год их сталкивал. Берётся первая
            # отчётная дата профиля — она и есть отчётный период комплекта.
            "period_end": profile.report_dates[0],
            "raw_path": raw_path,
            "checksum": checksum,
            "form_codes": list(profile.forms),
            "correction_version": correction_version,
            "reporting_kind": profile.reporting_kind.value,
            "unit_code": profile.unit_code,
            "digit_grouping": profile.grouping.value,
            "status": "quarantine" if quarantined else "loaded",
            "meta": json.dumps(meta, ensure_ascii=False),
            # Версия кода загрузки: по ней сводка контролей отличает записи
            # журнала, описывающие это извлечение, от записей прежних разборов.
            "code_version": code_version(),
        },
        conn=conn,
    )
    assert row is not None
    src_file_id = int(row["id"])
    execute(
        _DROP_ACTUAL,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "report_year": report_year,
            "keep": src_file_id,
        },
        conn=conn,
    )
    return src_file_id


def _fresh_facts(
    extraction: Extraction, confirmations: dict[str, str]
) -> tuple[ConfirmedFact, ...]:
    """Величины строк, размеченных в этом присесте, для записи фактами.

    Присвоение этого присеста ничем не слабее прежнего подтверждения: человек
    сказал, чем строка является, и величина идёт в расчёт. Коды приходят
    наименованиями, потому что так их называет экран разметки; строка без
    величин фактом не становится.
    """
    return tuple(
        ConfirmedFact(row.form, code, row.values, row.source_name, row.index)
        for row in extraction.unrecognised
        if (code := confirmations.get(row.source_name)) is not None and row.values
    )


def _write_facts(
    inn: str,
    src_file_id: int,
    extraction: Extraction,
    profile: DocumentProfile,
    conn: PgConnection,
    confirmed: tuple[ConfirmedFact, ...] = (),
    notes: tuple[NoteValue, ...] = (),
) -> tuple[int, Collisions]:
    """Пишет факты комплекта; возвращает число записанных и разбор столкновений.

    **Роль периода назначается по колонкам своей формы**, а не по колонкам
    документа. В промежуточном комплекте у баланса сравнительная колонка —
    конец прошлого года, у отчёта о прибыли — то же полугодие прошлого года;
    одна раскладка на все формы кладёт величину под чужую дату, и столкновение
    с отчётным значением другого комплекта тогда не происходит вовсе.

    Записанным считается то, что записала база: `execute` возвращает число
    затронутых строк, и правило приоритета в `ON CONFLICT` может отклонить
    входящее значение молча. Счётчик, считающий наши намерения, при этом
    показывал бы полную запись.
    """
    existing = _existing(inn, profile, conn)
    collisions = Collisions()
    revisions: list[Clash] = []
    sign_only: list[Clash] = []
    overwritten: list[Clash] = []
    written = 0
    unchanged = 0
    attempted = 0

    for form_code, form in extraction.forms.items():
        roles = _roles(profile.dates_of(form_code))
        for item in form.values:
            role = roles.get(item.report_date)
            if role is None:
                # Колонок в форме больше, чем ролей периода: четвёртая
                # и далее в модель не пишутся. Молчать об этом нельзя,
                # и запись об этом делает журнал качества.
                continue
            previous = existing.get((item.report_date, form_code, item.code))
            if previous is not None and previous["src_file_id"] == src_file_id:
                # Тот же комплект, загруженный заново: это перезапись, а не
                # столкновение периодов. Молча затирать загруженное число
                # нельзя — правило то же, что у РСБУ.
                if previous["value"] != item.value:
                    overwritten.append(Clash(item, previous, role, form_code))
            elif previous is not None:
                verdict = _classify(item, previous)
                collisions.checked += 1
                if verdict is _Verdict.AGREED:
                    collisions.agreed += 1
                else:
                    clash = Clash(item, previous, role, form_code)
                    if verdict is _Verdict.SIGN_ONLY:
                        sign_only.append(clash)
                    else:
                        revisions.append(clash)
                    if PERIOD_RANK[role] > PERIOD_RANK[previous["period_role"]]:
                        collisions.kept_by_priority += 1
                    else:
                        collisions.overwritten += 1
            touched = execute(
                _UPSERT_FACT,
                {
                    "src_file_id": src_file_id,
                    "inn": inn,
                    "standard": Standard.IFRS.value,
                    "report_date": item.report_date,
                    "form_code": form_code,
                    "line_code": item.code,
                    "source_line_code": item.code,
                    "value": item.value,
                    "period_role": role,
                    "recognition": Recognition.CATALOG.value,
                    "note_number": None,
                    "note_source_name": None,
                },
                conn=conn,
            )
            written += touched
            unchanged += not touched
            attempted += 1

    # **Подтверждённое человеком идёт в факты наравне с опознанным.** Прежде
    # факты писались только из строк, опознанных справочником, и статьи,
    # о которых человек уже сказал, чем они являются, в расчёт не попадали
    # вовсе: у Норникеля выпадали все 64 подтверждённые статьи, у Автодора —
    # 39 из 40, включая две, в которых лежат 85 % активов. Различает их
    # не участие в расчёте, а графа `recognition`: доверие к двум силам
    # опознания разное, и в документе они печатаются порознь.
    by_confirmation = 0
    for fact in confirmed:
        roles = _roles(profile.dates_of(fact.form))
        dates = profile.dates_of(fact.form)
        for index, value in enumerate(fact.values):
            if index >= len(dates):
                # Величин в строке больше, чем отчётных дат формы: лишние
                # графы — предмет отдельного контроля, а не молчания.
                break
            report_date = dates[index]
            role = roles.get(report_date)
            if role is None:
                continue
            touched = execute(
                _UPSERT_FACT,
                {
                    "src_file_id": src_file_id,
                    "inn": inn,
                    "standard": Standard.IFRS.value,
                    "report_date": report_date,
                    "form_code": fact.form,
                    "line_code": fact.code,
                    "source_line_code": fact.code,
                    "value": value,
                    "period_role": role,
                    "recognition": Recognition.CONFIRMATION.value,
                    "note_number": None,
                    "note_source_name": None,
                },
                conn=conn,
            )
            written += touched
            by_confirmation += touched
            unchanged += not touched
            attempted += 1

    # **Величина примечания — факт той формы, строка которой на примечание
    # ссылается.** Примечание расшифровывает конкретную строку конкретной
    # формы, поэтому форма берётся у неё, а источником называется примечание:
    # номер и наименования строк лежат рядом с величиной. Код при этом свой —
    # `ifrs.interest_expense_accrued` и `ifrs.finance_costs` два разных факта
    # одной формы: у Автодора 414 в строке формы и 54 382 по примечанию стоят
    # рядом, и подменять одно другим нельзя.
    by_note = 0
    for outcome in notes:
        if not outcome.found or not outcome.from_line:
            continue
        form = form_of(outcome.from_line)
        if form is None:
            logger.warning(
                "величина примечания %s не записана: форма строки %s неизвестна",
                outcome.code,
                outcome.from_line,
            )
            continue
        dates = profile.dates_of(form)
        roles = _roles(dates)
        # **Сравнительная графа примечания — та же величина за прошлый период.**
        # Прежде писалась только отчётная, и показатель, считающийся
        # по примечанию, за сравнительный период не считался вовсе: у ФосАгро
        # покрытие процентов за 2024 год отказывало «нет входных величин»,
        # тогда как проценты стоят в примечании 10 рядом с отчётными.
        # Граф бывает меньше, чем дат у формы, и лишняя дата величины
        # не получает — домысливать её нечем.
        by_period = outcome.values or (outcome.value,)
        for index, value in enumerate(by_period):
            if index >= len(dates):
                break
            report_date = dates[index]
            role = roles.get(report_date)
            if role is None:
                continue
            touched = execute(
                _UPSERT_FACT,
                {
                    "src_file_id": src_file_id,
                    "inn": inn,
                    "standard": Standard.IFRS.value,
                    "report_date": report_date,
                    "form_code": form,
                    "line_code": outcome.code,
                    "source_line_code": outcome.code,
                    "value": value,
                    "period_role": role,
                    "recognition": Recognition.NOTE.value,
                    "note_number": outcome.note,
                    "note_source_name": "; ".join(outcome.rows) or None,
                },
                conn=conn,
            )
            written += touched
            by_note += touched
            unchanged += not touched
            attempted += 1

    collisions.by_confirmation = by_confirmation
    collisions.by_note = by_note
    collisions.unchanged = unchanged
    collisions.attempted = attempted
    collisions.revisions = tuple(revisions)
    collisions.sign_only = tuple(sign_only)
    collisions.rewritten = tuple(overwritten)
    logger.info("столкновения периодов: %s", collisions.describe())
    return written, collisions


class _Verdict(StrEnum):
    """Чем оказалось столкновение входящей величины с загруженной."""

    AGREED = "agreed"
    # Величина та же, знак обратный: соглашение о печати знака, а не пересмотр.
    SIGN_ONLY = "sign_only"
    RESTATED = "restated"


def _classify(item, previous: dict) -> _Verdict:
    """Различает расхождение соглашения о знаке и пересмотр эмитентом.

    **Сигнал обязан мерить эмитента, а не нас.** Пересмотр меняет величину;
    расхождение, в котором величина совпадает до копейки и расходится только
    знак, — это способ печати расходной статьи, и приписывать его эмитенту
    как пересмотр отчётности нельзя. Определение одно на оба стандарта
    и лежит в `quality/values.py`.
    """
    stored = previous["value"]
    if stored is None or stored == item.value:
        return _Verdict.AGREED
    if sign_only_difference(stored, item.value):
        return _Verdict.SIGN_ONLY
    return _Verdict.RESTATED


@dataclass(frozen=True, slots=True)
class Clash:
    """Столкновение одной величины: что лежало, что пришло и в какой роли.

    Хранится разобранным, а не строкой. **Сравнить «было / стало» можно
    только по `dq_log`** — в `fact_report` истории версий нет, — и запись
    без `previous_value`, `line_code` и даты для этого не годится: искать
    величины разбором сообщения значит хранить их дважды.
    """

    item: object
    previous: dict
    role: str
    form_code: str

    def describe(self) -> str:
        """Человеческое описание столкновения для сообщения журнала."""
        return (
            f"{self.item.code} за {self.item.report_date:%d.%m.%Y}: было "
            f"{self.previous['value']} ({self.previous['period_role']}), "
            f"пришло {self.item.value} ({self.role})"
        )


def _existing(
    inn: str, profile: DocumentProfile, conn: PgConnection
) -> dict[tuple[date, str, str], dict]:
    """Ранее загруженные величины за все периоды комплекта.

    Даты берутся по всем формам разом: у промежуточного комплекта их три,
    и выборка по датам одной формы прошла бы мимо столкновения.
    """
    return {
        (row["report_date"], row["form_code"], row["line_code"]): row
        for row in fetch_all(
            _EXISTING,
            {
                "inn": inn,
                "standard": Standard.IFRS.value,
                "dates": list(profile.all_dates),
            },
            conn=conn,
        )
    }


def _roles(report_dates: tuple[date, ...]) -> dict[date, str]:
    """Роль периода по порядку колонок документа."""
    return {
        item: PERIOD_ROLES[index]
        for index, item in enumerate(report_dates)
        if index < len(PERIOD_ROLES)
    }


def _unconfirmed(
    extraction: Extraction,
    confirmations: dict[str, str],
    confirmed_rows: frozenset[tuple[str, int]] = frozenset(),
) -> list[str]:
    """Неопознанные статьи, которым человек кода не присвоил.

    Присвоенным считается и код, подтверждённый прежде у **этого же**
    эмитента: строки, о которых человек уже сказал, чем они являются,
    неподтверждёнными не числятся — иначе комплект уходил бы в карантин
    за то, что уже разобрано.
    """
    return [
        row.source_name
        for row in extraction.unrecognised
        if row.source_name not in confirmations and row.key not in confirmed_rows
    ]


def _review_passes(
    inn: str, src_file_id: int, profile: DocumentProfile, review: ReviewResult
) -> list[CheckRecord]:
    """Пройденные условия экрана сверки — записями, а не молчанием.

    **Сводка контролей комплекта не содержала ни одной пройденной проверки.**
    Экран писал только основания отбраковки, и у годового комплекта ФосАгро,
    прошедшего автоматически, в таблице стояли лишь сведения о сопоставлении
    строк: по документу нельзя было сказать, что именно проверено. Ноль
    нарушений при неизвестном числе проверок — не успех, а отсутствие
    сведений; правило то же, по которому контроли сходимости пишут `pass`.

    Каждое условие называет **число проверенных объектов**. Условие, у которого
    объектов не нашлось, идёт исходом «не выполнялся» с причиной: ноль
    сверенных итогов и все сошедшиеся итоги — разные вещи.
    """
    from finlib.sources.ifrs_review import ReviewReason

    failed = set(review.reasons)
    found: list[CheckRecord] = []
    report_date = profile.report_dates[0] if profile.report_dates else None

    def record(code: CheckCode, passed: bool, message: str) -> None:
        found.append(
            CheckRecord(
                inn=inn,
                check_code=code,
                status=CheckStatus.PASS if passed else CheckStatus.INFO,
                severity=Severity.BLOCKING if passed else Severity.INFO,
                message=message,
                src_file_id=src_file_id,
                report_date=report_date,
            )
        )

    if ReviewReason.CHECK_FAILED not in failed:
        if review.totals_checked:
            record(
                CheckCode.IFRS_TOTAL_MISMATCH,
                True,
                f"Сходимость итогов форм: сошлись все {review.totals_checked} "
                "сверенных итога",
            )
        else:
            record(
                CheckCode.IFRS_TOTAL_MISMATCH,
                False,
                "Сходимость итогов форм: не выполнялась — ни один итог "
                "не опознан, сверять нечего",
            )
    if ReviewReason.UNRECOGNISED_POSITION not in failed:
        record(
            CheckCode.IFRS_UNRECOGNISED_POSITION,
            bool(review.rows_total),
            f"Опознание позиций: опознаны все {review.rows_total} строк "
            f"с величинами (справочником {review.rows_recognised}, "
            f"по ранее подтверждённому {len(review.rows_confirmed)})"
            if review.rows_total
            else "Опознание позиций: не выполнялось — строк с величинами нет",
        )
    if ReviewReason.MATERIAL_SPECIFIC_ITEM not in failed:
        record(
            CheckCode.IFRS_MATERIAL_ITEM,
            bool(review.rows_measured),
            f"Статьи сверх порога существенности: их нет, измерено "
            f"{review.rows_measured} строк"
            if review.rows_measured
            else "Статьи сверх порога существенности: не проверялось — "
            "неопознанных строк с измеримой существенностью нет",
        )
    if ReviewReason.REPORTING_KIND not in failed:
        record(
            CheckCode.IFRS_REPORTING_KIND,
            True,
            f"Вид отчётности: {profile.reporting_kind.value} — методика "
            "применяется к нему целиком",
        )
    return found


def _audit_records(inn: str, src_file_id: int, audit) -> list[CheckRecord]:
    """Записи о том, что сказано в аудиторском заключении.

    **Сведения заключения идут в журнал комплекта**, а не только на экран:
    мнение с оговоркой, существенная неопределённость и пересмотр
    относятся к самой отчётности, на которой построен расчёт. Молчание
    журнала о заключении читалось бы как «оговорок нет», а это ровно
    та подмена, против которой задача 25 и делалась.

    Три состояния определённости дают три разные записи: заключения нет,
    заключение не прочитано, вид мнения назван. Нечитаемое заключение —
    не отсутствие оговорок.
    """
    from finlib.sources.ifrs_audit import Determination, Engagement

    if audit is None:
        return []
    found: list[CheckRecord] = []

    def record(code: CheckCode, message: str, details: dict | None = None) -> None:
        found.append(
            CheckRecord(
                inn=inn,
                check_code=code,
                status=CheckStatus.WARNING,
                severity=LOADER_SEVERITY[code],
                src_file_id=src_file_id,
                message=message,
                details=details or {},
            )
        )

    if audit.determination is Determination.ABSENT:
        record(CheckCode.AUDIT_REPORT_ABSENT, "Аудиторское заключение не приложено")
        return found
    if audit.determination is Determination.NOT_READABLE:
        record(
            CheckCode.AUDIT_REPORT_NOT_READABLE,
            "Аудиторское заключение не прочитано: страницы без текстового слоя",
            {"pages": list(audit.unreadable_pages)},
        )
        return found

    if audit.engagement is Engagement.REVIEW:
        record(
            CheckCode.AUDIT_REVIEW_ENGAGEMENT,
            "Отчётность прошла обзорную проверку, а не аудит",
        )
    if audit.modified:
        # **Вид оговорки называется вместе с мнением.** Уверенность в оценке
        # и эскалация зависят от него, и запись без вида не говорит, применялись
        # ли следствия: у ФосАгро оговорка о раскрытии, у Автодора о величинах.
        from finlib.normalize.ifrs_audit import load_audit_policy

        policy = load_audit_policy()
        kind = policy.caveat_kind(audit.effective_caveat_kind(policy))
        record(
            CheckCode.AUDIT_OPINION_MODIFIED,
            f"Мнение аудитора модифицировано: {audit.opinion_name}"
            + (f"; вид оговорки — {kind.name.lower()}" if kind is not None else ""),
            {
                "opinion": audit.opinion,
                "caveat_kind": kind.code if kind is not None else None,
                "caveat_confirmed_by": audit.caveat_confirmed_by or None,
                "caveat_kind_proposed": audit.proposed_caveat_kind(policy),
            },
        )
    if "going_concern_uncertainty" in audit.sections:
        record(
            CheckCode.AUDIT_GOING_CONCERN,
            "Аудитор объявил существенную неопределённость в отношении "
            "непрерывности деятельности",
        )
    if "statements_restated" in audit.signals:
        record(
            CheckCode.AUDIT_STATEMENTS_RESTATED,
            "Аудитор обратил внимание на пересмотр ранее выпущенной отчётности",
        )
    return found


def _layouts(extraction: Extraction) -> str:
    """Разметка граф по формам одной строкой — для сообщения журнала."""
    found = [
        f"{code.removeprefix('ifrs.')} — {form.layout.describe()}"
        for code, form in sorted(extraction.forms.items())
        if form.layout is not None
    ]
    return "; ".join(found) or "не определялась"


def _journal_records(
    inn: str,
    src_file_id: int,
    profile: DocumentProfile,
    extraction: Extraction,
    review: ReviewResult,
    collisions: Collisions,
    quarantined: bool,
    notes: tuple[NoteValue, ...] = (),
    accepted: dict[str, str] | None = None,
    confirmed_by: str | None = None,
) -> list[CheckRecord]:
    """Записи журнала качества по итогам приёма и сверки.

    Счётчик проверенного идёт рядом со счётчиком сработавшего: одной записью
    сообщается, сколько итогов сверено и сколько строк опознано, — иначе
    отсутствие провалов неотличимо от невыполненной проверки.
    """
    records = [
        CheckRecord(
            inn=inn,
            check_code=CheckCode.LINE_MAPPING,
            status=CheckStatus.INFO,
            severity=Severity.INFO,
            message=(
                # Две силы опознания печатаются порознь: справочник утверждает
                # о строке вообще, подтверждение — о строке этого эмитента.
                f"Опознано позиций "
                f"{review.rows_recognised + len(review.rows_confirmed)} "
                f"из {review.rows_total}: справочником {review.rows_recognised}, "
                f"по ранее подтверждённому {len(review.rows_confirmed)}"
                + (
                    f" (подтверждения комплектов {', '.join(review.confirmed_from)})"
                    if review.confirmed_from
                    else ""
                )
                + f", осознанно игнорируется {review.rows_ignored}"
                + f"; итогов сверено {review.totals_checked}, не сошлось "
                f"{len(review.totals_failed)}; строк сложено с другими "
                f"{len(extraction.merged)}, спорных позиций "
                f"{len(extraction.contested)}; величины отброшены у "
                f"{review.rows_with_dropped} строк из {review.rows_with_values} "
                f"с величинами; графы форм: {_layouts(extraction)}; "
                f"правдоподобие конвенции: "
                f"{review.plausibility.describe() if review.plausibility else '—'}"
            ),
            src_file_id=src_file_id,
            details={
                "rows_recognised": review.rows_recognised,
                "rows_confirmed": len(review.rows_confirmed),
                # Осознанно игнорируемые строки — решение методики, и в журнале
                # они стоят рядом с неопознанными, а не вместо них.
                "rows_ignored": len(extraction.ignored),
                "ignored_subjects": sorted(
                    {subject for _form, _name, subject in extraction.ignored}
                ),
                "confirmed_from": list(review.confirmed_from),
                "rows_total": review.rows_total,
                "totals_checked": review.totals_checked,
                "totals_failed": len(review.totals_failed),
                "totals_by_structure": [
                    code
                    for form in extraction.forms.values()
                    for code in form.totals_by_structure
                ],
                # Сложение и спор — события разбора, и молчать о них нельзя:
                # затирание величины было неотличимо от честного опознания
                # ровно потому, что нигде не считалось.
                "merged_rows": [
                    {"code": code, "name": name, "kind": kind}
                    for code, name, kind in extraction.merged
                ],
                "contested_codes": [code for code, _ in extraction.contested],
                # Разметка граф и потерянные величины: по числу взятых граф
                # не увидеть, те ли это графы. У промежуточного ФосАгро
                # брались последние две из четырёх — квартальные.
                "columns": {
                    code: form.layout.describe()
                    for code, form in extraction.forms.items()
                    if form.layout is not None
                },
                "rows_with_values": review.rows_with_values,
                "dropped_values": [
                    {
                        "form": form,
                        "name": name,
                        "values": [str(value) for value in values],
                    }
                    for form, name, values in extraction.dropped_values
                ],
            },
        )
    ]

    records.extend(_review_passes(inn, src_file_id, profile, review))

    for code, reason in zip(review.check_codes, review.reasons, strict=False):
        # **Принятое основание называет, кем и почему принято.** Запись
        # остаётся записью о провале — основание никуда не делось, — но
        # к ней приписано решение человека: провал, принятый молча,
        # неотличим от контроля, который не провалился.
        taken = (accepted or {}).get(reason.value)
        records.append(
            CheckRecord(
                inn=inn,
                check_code=code,
                status=CheckStatus.FAIL if quarantined else CheckStatus.WARNING,
                severity=Severity.BLOCKING if quarantined else Severity.WARNING,
                message=(
                    f"Экран сверки: {reason.value}. "
                    + "; ".join(review.problems[:3])
                    + (
                        f" Принято человеком ({confirmed_by}): {' '.join(taken.split())}"
                        if taken
                        else ""
                    )
                ),
                src_file_id=src_file_id,
                # **Отчётная дата комплекта стоит в записи.** Без неё документ
                # по годовому комплекту печатал основания промежуточного, и по
                # «Ключевому выводу» нельзя было понять, какой период отбракован:
                # графа «Затронутые отчётные даты» оставалась пустой.
                report_date=profile.report_dates[0] if profile.report_dates else None,
            )
        )

    # Счётчик сверенного идёт в журнал вместе с расхождениями: ноль
    # расхождений при неизвестном числе сверок не означает ни того, что
    # отчётность не пересматривалась, ни того, что приоритет сработал.
    records.append(
        CheckRecord(
            inn=inn,
            check_code=CheckCode.PERIOD_PRIORITY,
            status=CheckStatus.INFO,
            severity=Severity.INFO,
            message=(
                "Столкновение периодов: "
                + collisions.describe()
                + (
                    ""
                    if collisions.checked
                    else " — ранее загруженных величин за эти периоды нет"
                )
            ),
            src_file_id=src_file_id,
            details={
                "checked": collisions.checked,
                "agreed": collisions.agreed,
                "kept_by_priority": collisions.kept_by_priority,
                "overwritten": collisions.overwritten,
                "restated": len(collisions.revisions),
                "sign_only": len(collisions.sign_only),
            },
        )
    )

    # **Отказ извлечения из примечания — исход, а не отсутствие факта.**
    # У Норникеля капитализированные проценты раскрыты прозой, и показатель,
    # которому величины не хватило, обязан назвать причину, иначе она
    # останется в памяти того, кто смотрел документ. Рядом стоит сводка:
    # сколько величин взято и сколько отказов — ноль отказов при неизвестном
    # числе объявленных величин не означает ничего.
    if notes:
        taken = [item for item in notes if item.found]
        refused = [item for item in notes if not item.found]
        records.append(
            CheckRecord(
                inn=inn,
                check_code=CheckCode.NOTE_VALUES,
                status=CheckStatus.INFO,
                severity=Severity.INFO,
                message=(
                    f"Величины примечаний: взято {len(taken)} "
                    f"из {len(notes)} объявленных, отказов {len(refused)}"
                    + (
                        "; "
                        + "; ".join(
                            f"{item.code} — примечание {item.note}" for item in taken
                        )
                        if taken
                        else ""
                    )
                ),
                src_file_id=src_file_id,
                details={
                    "taken": [
                        {
                            "code": item.code,
                            "note": item.note,
                            "from_line": item.from_line,
                            "rows": list(item.rows),
                            "value": str(item.value),
                        }
                        for item in taken
                    ],
                    "refused": [
                        {"code": item.code, "reason": item.describe()}
                        for item in refused
                    ],
                },
            )
        )
        records.extend(
            CheckRecord(
                inn=inn,
                check_code=CheckCode.NOTE_VALUE_NOT_EXTRACTED,
                status=CheckStatus.WARNING,
                severity=LOADER_SEVERITY[CheckCode.NOTE_VALUE_NOT_EXTRACTED],
                message=f"Величина примечания не извлечена: {item.describe()}",
                src_file_id=src_file_id,
                line_code=item.code,
                details={
                    "refusal": item.refusal.value if item.refusal else None,
                    "note": item.note,
                },
            )
            for item in refused
        )

    def clash_record(
        clash: Clash, code: CheckCode, status: CheckStatus, message: str
    ) -> CheckRecord:
        """Запись о столкновении: величины стоят в графах, а не только в тексте."""
        return CheckRecord(
            inn=inn,
            check_code=code,
            status=status,
            severity=LOADER_SEVERITY[code],
            src_file_id=src_file_id,
            report_date=clash.item.report_date,
            form_code=clash.form_code,
            line_code=clash.item.code,
            previous_value=clash.previous["value"],
            new_value=clash.item.value,
            message=f"{message}: {clash.describe()}",
            details={
                "stored_period_role": clash.previous["period_role"],
                "incoming_period_role": clash.role,
            },
        )

    records.extend(
        clash_record(
            item,
            CheckCode.PERIOD_VALUE_MISMATCH,
            CheckStatus.WARNING,
            "Сравнительные данные расходятся с загруженными",
        )
        for item in collisions.revisions
    )

    # Перезапись своей же величины — событие разбора, а не отчётности.
    records.extend(
        clash_record(
            item,
            CheckCode.FACT_OVERWRITE,
            CheckStatus.INFO,
            "Ранее загруженное значение перезаписано",
        )
        for item in collisions.rewritten
    )

    # Расхождение знака при равной величине — соглашение о печати, а не
    # пересмотр эмитентом. В `period_value_mismatch` ему не место: по этому
    # коду считается интенсивность пересмотра, и она мерила бы нас.
    records.extend(
        clash_record(
            item,
            CheckCode.SIGN_CONVENTION_MISMATCH,
            CheckStatus.WARNING,
            "Величина совпадает, знак обратный — расхождение соглашения "
            "о печати знака, а не пересмотр",
        )
        for item in collisions.sign_only
    )
    return records


def _save_confirmations(
    inn: str,
    src_file_id: int,
    report_date: date,
    extraction: Extraction,
    confirmations: dict[str, str],
    confirmed_by: str,
    conn: PgConnection,
) -> int:
    """Сохраняет коды, присвоенные человеком неопознанным статьям.

    Наименование хранится дословно: по коду не увидеть, одну ли вещь
    подтверждали у разных эмитентов под разными названиями, — код присваивали
    мы. Мера существенности хранится рядом: по ней видно, ради чего статья
    вынесена отдельной позицией. База меры — своя у каждой формы, а у строки
    потока её нет вовсе, и тогда в графе `NULL`: «мерить нечем» и «мера мала» —
    разные сведения.
    """

    def value_of(code: str) -> Decimal | None:
        """Величина позиции за отчётный период комплекта."""
        return extraction.value_of(code, report_date)

    catalog = load_ifrs_lines()
    saved = 0
    for row in extraction.unrecognised:
        code = confirmations.get(row.source_name)
        if code is None:
            continue
        share = materiality_share(row, catalog, value_of)
        execute(
            _INSERT_CONFIRMATION,
            {
                "code": code,
                "inn": inn,
                "src_file_id": src_file_id,
                "report_date": report_date,
                "source_name": row.source_name,
                # Ключ сопоставления вычисляется текущим разбором и лежит
                # рядом с дословной записью, а не вместо неё.
                "match_key": match_key(row.source_name),
                "form_code": row.form,
                "value": row.values[0] if row.values else None,
                "share": share,
                "confirmed_by": confirmed_by,
                "note": None,
                "index": row.index,
            },
            conn=conn,
        )
        saved += 1
    return saved


__all__ = ["LoadResult", "PERIOD_RANK", "load_extraction"]
