"""Сборка заключения в Word.

Раздел 1 и приложение собираются из базы детерминированно, разделы 2–6 пишет
модель. Документ не появится, пока ответ модели не прошёл постпроверку:
незаверенный текст не показывается пользователю (инвариант 8), а файл на диске
показывается тем более.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from finlib.config import settings
from finlib.db import PgConnection
from finlib.llm.service import Conclusion, generate_conclusion
from finlib.report.appendix import (
    Table,
    checks_table,
    exclusions_table,
    groups_table,
    metrics_table,
    not_calculated_table,
    provenance,
)
from finlib.report.consistency import InconsistentReportError, check_document
from finlib.report.data import ReportData, load_report_data
from finlib.report.integrity import NumbersAlteredError, check_numbers
from finlib.report.policy import ReportPolicy, Trigger, load_policy
from finlib.report.sections import EXPECTED, Section, split_sections
from finlib.report.summary import build_summary
from finlib.scoring.definitions import ScoringCatalog, load_scoring
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Название документа зависит от того, есть ли текстовая часть: оговорка
# ниже прямо называет документ без неё расчётной справкой, и заголовок
# «Заключение» ей противоречил бы.
TITLE = "Заключение о финансовом состоянии"
TITLE_NO_TEXT = "Расчётная справка о финансовом состоянии"

# Дисклеймер в шапке. Формулировка намеренно не смягчена: документ уходит
# человеку, который будет принимать по нему решение.
# Дисклеймер контекстный: он утверждает факт о конкретном документе,
# а не описывает систему вообще. Шаблонная фраза «текстовая часть подготовлена
# с применением языковой модели и прошла проверку» в документе, собранном
# без модели, — ложное утверждение.
_COMMON_HEAD = (
    "Документ сформирован автоматически. Расчётная часть — класс, балл, "
    "показатели и контроли качества — получена детерминированным расчётом "
    "по данным бухгалтерской отчётности. "
)
_COMMON_TAIL = (
    " Документ подлежит проверке ответственным сотрудником и самостоятельным "
    "основанием для принятия решения не является."
)

DISCLAIMER = (
    _COMMON_HEAD
    + "Текстовая часть (разделы 2–6) подготовлена с применением языковой модели "
    "и прошла автоматическую проверку на соответствие расчётным данным."
    + _COMMON_TAIL
)

DISCLAIMER_NO_TEXT = (
    _COMMON_HEAD
    + "Текстовая часть (разделы 2–6) не формировалась: языковая модель "
    "не привлекалась, и документ содержит только расчётную часть."
    + _COMMON_TAIL
)


# Модель не привлекалась: документ собран только из расчётной части.
NO_MODEL = "не привлекалась"

NO_TEXT_NOTICE = (
    "Текстовая часть заключения (разделы 2–6) не формировалась: документ "
    "подготовлен без привлечения языковой модели. Расчётная часть — класс, "
    "показатели, надзорные сигналы, предложения по дальнейшим действиям, "
    "контроли качества и приложение — полна и получена детерминированным "
    "расчётом. Настоящий документ заключением не является и служит "
    "расчётной справкой. Ниже приведены разделы, не зависящие от модели."
)


@dataclass(frozen=True, slots=True)
class RenderedReport:
    """Готовый документ и то, из чего он собран."""

    path: Path
    inn: str
    report_date: date
    model: str
    sections: tuple[Section, ...]


def output_path(inn: str, report_date: date, directory: Path | None = None) -> Path:
    """Путь выгрузки: data/output/{ИНН}_{дата}.docx."""
    target = directory or settings.output_dir
    return target / f"{inn}_{report_date:%Y-%m-%d}.docx"


def build_report(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
    standard: Standard = Standard.RSBU,
    conclusion: Conclusion | None = None,
    directory: Path | None = None,
    scoring: ScoringCatalog | None = None,
    generated_at: datetime | None = None,
    with_text: bool = True,
    is_test: bool = False,
) -> RenderedReport:
    """Готовит заключение и записывает docx.

    Текстовая часть берётся из готового Conclusion, если он передан, иначе
    порождается здесь. Отклонённый постпроверкой ответ до документа не доходит:
    generate_conclusion поднимает ConclusionRejectedError.

    is_test передаётся в журнал обращений к модели. Признак нужен здесь,
    а не только в `generate_conclusion`: тест, которому нужен полный путь
    сборки, иначе не имеет способа пометить свою запись, и она навсегда
    оседает в журнале как боевая.
    """
    scoring = scoring if scoring is not None else load_scoring()
    data = load_report_data(inn, conn, report_date=report_date, standard=standard)

    if conclusion is None and with_text:
        from finlib.metrics.definitions import load_metrics
        from finlib.normalize.lines import ReportingType, load_lines

        lines_catalog = load_lines()
        reporting_type = ReportingType(data.organization["reporting_type"])
        conclusion = generate_conclusion(
            inn,
            conn,
            report_date=data.report_date,
            standard=standard,
            text_context=data.text_context(
                lines_catalog, reporting_type, load_metrics()
            ),
            is_test=is_test,
        )
    sections = split_sections(conclusion.text) if conclusion is not None else []

    # Противоречие в документе хуже отсутствия сведений: проверяем до записи.
    problems = check_document(data, _limitations_text(inn, conn, data, standard))
    if problems:
        raise InconsistentReportError([item.message for item in problems])

    written_at = generated_at or datetime.now()
    document = Document()
    _set_base_style(document)
    _write_header(document, data, bool(sections), written_at)
    _write_summary(document, data, scoring, written_at)
    if sections:
        _write_sections(document, sections, data)
    else:
        # Оговорка об отсутствии текстовой части идёт первой, а детерминированный
        # раздел сигналов — после неё: иначе документ сначала печатал раздел 4,
        # а затем сообщал, что разделов 2–6 нет.
        _write_missing_text(document)
        # Сигналы детерминированы и от модели не зависят: в справке без
        # текстовой части они обязаны остаться.
        if data.signals:
            document.add_heading(f"{SIGNALS_SECTION}. {SIGNALS_TITLE}", level=1)
            _write_signals(document, data)
    # Предложения по дальнейшим действиям — следствие машинных признаков,
    # а не суждение модели, поэтому раздел собирается здесь и стоит
    # в документе всегда, с текстовой частью и без неё.
    _write_actions(document, data, written_at)
    model = conclusion.model if conclusion is not None else NO_MODEL
    _write_appendix(document, data, model, written_at)

    path = output_path(inn, data.report_date, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)

    # Сквозная сверка: всё, что выполняется после постпроверки — снятие
    # разметки, разбор на разделы, оформление, запись docx, — находилось
    # вне контроля. Очистка однажды съела минус величины, и число сменило
    # знак уже после того, как проверка его подтвердила.
    if conclusion is not None and conclusion.verified_text:
        try:
            check_numbers(
                conclusion.verified_text,
                _model_text_of(path),
                extra=[
                    # Номера заголовков разделов.
                    *(f"{number}." for number, _ in EXPECTED),
                    # Предписанные формулировки сигналов и основания, по которым
                    # они сработали: всё это детерминировано и в тексте модели
                    # отсутствует по построению.
                    *(item["message"] for item in data.signals),
                    *(_signal_basis(item) for item in data.signals),
                ],
            )
        except NumbersAlteredError:
            path.unlink(missing_ok=True)
            raise
    logger.info("заключение записано: %s", path)
    return RenderedReport(
        path=path,
        inn=inn,
        report_date=data.report_date,
        model=model,
        sections=tuple(sections),
    )


def _model_text_of(path: Path) -> str:
    """Текст разделов 2–6 из записанного документа.

    Читается с диска, а не из памяти: сверять надо то, что получит читатель,
    вместе с последствиями оформления и сериализации.
    """
    document = Document(str(path))
    collected: list[str] = []
    inside = False
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if re.match(r"^2\.\s", text) or text.startswith("2–6"):
            inside = True
        # Раздел предложений и приложение написаны не моделью: сверять
        # в них нечего, а их собственные числа выглядели бы приписками.
        elif text.startswith("Приложение") or text.startswith(
            f"{ACTIONS_SECTION}. {ACTIONS_TITLE}"
        ):
            break
        if inside:
            collected.append(paragraph.text)
    return "\n".join(collected)


def _limitations_text(
    inn: str, conn: PgConnection | None, data: ReportData, standard: Standard
) -> str:
    """Раздел «Ограничения анализа» тем же составом, что уходит в документ."""
    from finlib.llm.context import build_context

    context = build_context(inn, conn, report_date=data.report_date, standard=standard)
    return context.limitations


def _write_missing_text(document: Document) -> None:
    """Оговорка вместо разделов модели.

    Расчётная часть не зависит от модели и остаётся полной, но документ
    без разделов 2–6 — не заключение, и читатель обязан это видеть, а не
    обнаруживать по отсутствию текста.
    """
    document.add_heading("2–6. Текстовая часть", level=1)
    paragraph = document.add_paragraph()
    run = paragraph.add_run(NO_TEXT_NOTICE)
    run.bold = True


def _set_base_style(document: Document) -> None:
    """Базовый шрифт документа."""
    style = document.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)


def _write_header(
    document: Document, data: ReportData, with_text: bool, generated_at: datetime
) -> None:
    """Шапка: наименование, реквизиты, период, дисклеймер."""
    organization = data.organization
    document.add_heading(TITLE if with_text else TITLE_NO_TEXT, level=0)

    name = organization["name"] or organization["short_name"] or data.inn
    heading = document.add_paragraph()
    heading.add_run(name).bold = True
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    details = [f"ИНН: {data.inn}"]
    if organization["ogrn"]:
        details.append(f"ОГРН: {organization['ogrn']}")
    if organization["okved"]:
        details.append(f"Основной вид деятельности (ОКВЭД): {organization['okved']}")
    if organization["region"]:
        details.append(f"Регион: {organization['region']}")
    details.append(f"Отчётная дата: {data.report_date:%d.%m.%Y}")
    # Дата формирования и разрыв стоят рядом с отчётной датой: документ,
    # собранный через двадцать месяцев после отчётной даты, описывает
    # состояние на неё, а не нынешнее, и читатель обязан видеть это сразу.
    details.append(f"Дата формирования документа: {generated_at:%d.%m.%Y}")
    details.append(
        f"Разрыв между отчётной датой и формированием: "
        f"{data.months_since_report(generated_at)} мес."
    )
    for line in details:
        document.add_paragraph(line)

    warning = document.add_paragraph()
    run = warning.add_run(DISCLAIMER if with_text else DISCLAIMER_NO_TEXT)
    run.italic = True
    run.font.size = Pt(9)


def _write_summary(
    document: Document,
    data: ReportData,
    scoring: ScoringCatalog,
    generated_at: datetime,
) -> None:
    """Раздел 1 «Ключевой вывод»."""
    document.add_heading("1. Ключевой вывод", level=1)
    for paragraph in build_summary(data, scoring, generated_at):
        written = document.add_paragraph()
        written.add_run(paragraph.text).bold = paragraph.bold


# Раздел, в который выводятся надзорные сигналы, и его название.
SIGNALS_SECTION = 4
SIGNALS_TITLE = "Риски и надзорные сигналы"

# Раздел предложений идёт после разделов модели: он подводит итог документу.
ACTIONS_SECTION = 7
ACTIONS_TITLE = "Предложения по дальнейшим действиям"

SIGNAL_LEVELS: dict[str, str] = {
    "supervisory": "надзорный сигнал",
    "attention": "требует внимания",
}


def _write_sections(
    document: Document, sections: list[Section], data: ReportData
) -> None:
    """Разделы 2–6: сигналы детерминированы, остальное пишет модель.

    Сигналы выводятся первыми в своём разделе и дословно: их выявление
    не может оставаться на усмотрение модели, а формулировка задана
    методикой и пересказу не подлежит.
    """
    for section in sections:
        title = (
            SIGNALS_TITLE if section.number == SIGNALS_SECTION else section.title
        )
        document.add_heading(f"{section.number}. {title}", level=1)
        if section.number == SIGNALS_SECTION:
            _write_signals(document, data)
        for text in section.paragraphs:
            document.add_paragraph(text)


def _write_signals(document: Document, data: ReportData) -> None:
    """Сработавшие сигналы с предписанными формулировками.

    К каждому сигналу приводится величина и порог, по которому он сработал:
    тезис в этом разделе без числа и отсечки проверить нечем, а отсечки
    экспертные и объявлены предварительными.
    """
    if not data.signals:
        return
    heading = document.add_paragraph()
    heading.add_run(
        "Выявлены обстоятельства, требующие внимания. Формулировки заданы "
        "методикой и получены расчётом, а не оценочным суждением:"
    ).bold = True
    for signal in data.signals:
        level = SIGNAL_LEVELS.get(signal["level"], signal["level"])
        paragraph = document.add_paragraph()
        paragraph.add_run(f"{signal['signal_name']} ({level}). ").bold = True
        paragraph.add_run(signal["message"])
        basis = _signal_basis(signal)
        if basis:
            note = document.add_paragraph()
            run = note.add_run(basis)
            run.italic = True
            run.font.size = Pt(9)


def _signal_basis(signal: dict) -> str:
    """Величина и отсечка, по которым сигнал сработал.

    Печатаются готовыми: обе набраны расчётом в тот же момент, что и сама
    формулировка, и той же разрядностью. Прежде основание округляло само
    и печатало знак, которого в формулировке нет, — рядом стояли
    «изменение — 181,2 п. п.» и «Расчётная величина: -181,18».
    """
    details = signal["details"] or {}
    value = details.get("value_shown")
    if value is None:
        # Оценка посчитана прежней версией: набирать величину здесь заново
        # значило бы печатать её иначе, чем она стоит в формулировке, —
        # ровно тем расхождением, ради которого написано всё это место.
        logger.warning(
            "сигнал %s посчитан без готовой величины, основание не печатается; "
            "пересчитайте оценку",
            signal["signal_code"],
        )
        return ""
    parts = [f"Расчётная величина: {value}"]
    threshold = details.get("threshold_shown")
    if threshold is not None:
        parts.append(f"отсечка: {threshold}")
    return (
        "; ".join(parts)
        + ". Отсечка задана методикой, объявлена экспертной и предварительной."
    )


def _write_actions(
    document: Document, data: ReportData, generated_at: datetime
) -> None:
    """Раздел «Предложения по дальнейшим действиям».

    Прежде документ содержал вопросы, но не содержал вывода о том, что
    с организацией делать. Предложение выводится по машинным признакам —
    сработал сигнал, сработал стоп-фактор, класс не присвоен, комплект
    отбракован, данные устарели, — а формулировка берётся из методики.
    """
    policy = load_policy()
    actions = policy.actions_for(_triggers(data, generated_at, policy))
    if not actions:
        return
    document.add_heading(f"{ACTIONS_SECTION}. {ACTIONS_TITLE}", level=1)
    intro = document.add_paragraph()
    intro.add_run(
        "Предложения следуют из признаков, установленных расчётом, "
        "и приведены в предписанных методикой формулировках:"
    ).bold = True
    for action in actions:
        paragraph = document.add_paragraph()
        paragraph.add_run(f"{action.name}. ").bold = True
        paragraph.add_run(action.message)


def _triggers(
    data: ReportData, generated_at: datetime, policy: ReportPolicy
) -> set[Trigger]:
    """Машинные признаки организации, по которым выводятся предложения."""
    found: set[Trigger] = set()
    levels = {item["level"] for item in data.signals}
    if "supervisory" in levels:
        found.add(Trigger.SUPERVISORY_SIGNAL)
    if "attention" in levels:
        found.add(Trigger.ATTENTION_SIGNAL)
    if data.stop_factor_code:
        found.add(Trigger.STOP_FACTOR)
    if data.assessment is not None and not data.class_code:
        found.add(Trigger.NO_CLASS)
    if data.flag_conflict() is not None:
        found.add(Trigger.FLAG_CONFLICT)
    if policy.freshness.stale(data.months_since_report(generated_at)):
        found.add(Trigger.STALE_DATA)
    if data.quarantined_sources:
        found.add(Trigger.QUARANTINED_SET)
    if data.blocking_failures:
        found.add(Trigger.BLOCKING_CHECK_FAILED)
    return found


def _write_appendix(
    document: Document, data: ReportData, model: str, generated_at: datetime
) -> None:
    """Приложение: таблицы и происхождение документа."""
    document.add_page_break()
    document.add_heading("Приложение", level=1)

    # Номер таблицы ставится здесь, а не в её заголовке: таблица может
    # не строиться (разложение балла без класса), и захардкоженные номера
    # оставляли в документе дыру — таблицы 3 не было, а таблица 4 номер
    # сохраняла.
    tables = [
        metrics_table(data),
        exclusions_table(data),
        not_calculated_table(data),
        groups_table(data),
        checks_table(data),
    ]
    for number, table in enumerate(
        (item for item in tables if item is not None), start=1
    ):
        _write_table(document, table, number)

    document.add_heading("Происхождение документа", level=2)
    for line in provenance(data, model, generated_at):
        document.add_paragraph(line)


def _write_table(document: Document, table: Table, number: int) -> None:
    """Одна таблица приложения под сквозным номером."""
    document.add_heading(f"Таблица {number}. {table.title}", level=2)
    written = document.add_table(rows=1, cols=len(table.header))
    written.style = "Table Grid"
    for cell, title in zip(written.rows[0].cells, table.header, strict=True):
        cell.text = ""
        cell.paragraphs[0].add_run(title).bold = True
    for row in table.rows:
        cells = written.add_row().cells
        for cell, value in zip(cells, row, strict=True):
            cell.text = value
    if table.note:
        note = document.add_paragraph()
        run = note.add_run(table.note)
        run.italic = True
        run.font.size = Pt(9)
