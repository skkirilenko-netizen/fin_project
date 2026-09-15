"""Сборка заключения в Word.

Раздел 1 и приложение собираются из базы детерминированно, разделы 2–6 пишет
модель. Документ не появится, пока ответ модели не прошёл постпроверку:
незаверенный текст не показывается пользователю (инвариант 8), а файл на диске
показывается тем более.
"""

import logging
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
    groups_table,
    metrics_table,
    not_calculated_table,
    provenance,
)
from finlib.report.consistency import InconsistentReportError, check_document
from finlib.report.data import ReportData, load_report_data
from finlib.report.sections import Section, split_sections
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
    "показатели, контроли качества и приложение — полна и получена "
    "детерминированным расчётом. Настоящий документ заключением не является "
    "и служит расчётной справкой."
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
) -> RenderedReport:
    """Готовит заключение и записывает docx.

    Текстовая часть берётся из готового Conclusion, если он передан, иначе
    порождается здесь. Отклонённый постпроверкой ответ до документа не доходит:
    generate_conclusion поднимает ConclusionRejectedError.
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
        )
    sections = split_sections(conclusion.text) if conclusion is not None else []

    # Противоречие в документе хуже отсутствия сведений: проверяем до записи.
    problems = check_document(data, _limitations_text(inn, conn, data, standard))
    if problems:
        raise InconsistentReportError([item.message for item in problems])

    document = Document()
    _set_base_style(document)
    _write_header(document, data, bool(sections))
    _write_summary(document, data, scoring)
    if sections:
        _write_sections(document, sections, data)
    else:
        # Сигналы детерминированы и от модели не зависят: в справке без
        # текстовой части они обязаны остаться.
        if data.signals:
            document.add_heading(f"{SIGNALS_SECTION}. {SIGNALS_TITLE}", level=1)
            _write_signals(document, data)
        _write_missing_text(document)
    model = conclusion.model if conclusion is not None else NO_MODEL
    _write_appendix(document, data, model, generated_at or datetime.now())

    path = output_path(inn, data.report_date, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    logger.info("заключение записано: %s", path)
    return RenderedReport(
        path=path,
        inn=inn,
        report_date=data.report_date,
        model=model,
        sections=tuple(sections),
    )


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


def _write_header(document: Document, data: ReportData, with_text: bool) -> None:
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
    details.append(f"Отчётный период: {data.report_date:%d.%m.%Y}")
    for line in details:
        document.add_paragraph(line)

    warning = document.add_paragraph()
    run = warning.add_run(DISCLAIMER if with_text else DISCLAIMER_NO_TEXT)
    run.italic = True
    run.font.size = Pt(9)


def _write_summary(
    document: Document, data: ReportData, scoring: ScoringCatalog
) -> None:
    """Раздел 1 «Ключевой вывод»."""
    document.add_heading("1. Ключевой вывод", level=1)
    for paragraph in build_summary(data, scoring):
        written = document.add_paragraph()
        written.add_run(paragraph.text).bold = paragraph.bold


# Раздел, в который выводятся надзорные сигналы, и его название.
SIGNALS_SECTION = 4
SIGNALS_TITLE = "Риски и надзорные сигналы"

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
    """Сработавшие сигналы с предписанными формулировками."""
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
