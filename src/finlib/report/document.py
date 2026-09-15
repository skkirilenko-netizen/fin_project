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

TITLE = "Заключение о финансовом состоянии"

# Дисклеймер в шапке. Формулировка намеренно не смягчена: документ уходит
# человеку, который будет принимать по нему решение.
DISCLAIMER = (
    "Документ сформирован автоматически. Расчётная часть — класс, балл, "
    "показатели и контроли качества — получена детерминированным расчётом "
    "по данным бухгалтерской отчётности. Текстовая часть (разделы 2–6) "
    "подготовлена с применением языковой модели и прошла автоматическую "
    "проверку на соответствие расчётным данным. Документ подлежит проверке "
    "ответственным сотрудником и самостоятельным основанием для принятия "
    "решения не является."
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
) -> RenderedReport:
    """Готовит заключение и записывает docx.

    Текстовая часть берётся из готового Conclusion, если он передан, иначе
    порождается здесь. Отклонённый постпроверкой ответ до документа не доходит:
    generate_conclusion поднимает ConclusionRejectedError.
    """
    scoring = scoring if scoring is not None else load_scoring()
    data = load_report_data(inn, conn, report_date=report_date, standard=standard)

    if conclusion is None:
        conclusion = generate_conclusion(
            inn, conn, report_date=data.report_date, standard=standard
        )
    sections = split_sections(conclusion.text)

    # Противоречие в документе хуже отсутствия сведений: проверяем до записи.
    problems = check_document(data, _limitations_text(inn, conn, data, standard))
    if problems:
        raise InconsistentReportError([item.message for item in problems])

    document = Document()
    _set_base_style(document)
    _write_header(document, data)
    _write_summary(document, data, scoring)
    _write_sections(document, sections)
    _write_appendix(document, data, conclusion.model, generated_at or datetime.now())

    path = output_path(inn, data.report_date, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    logger.info("заключение записано: %s", path)
    return RenderedReport(
        path=path,
        inn=inn,
        report_date=data.report_date,
        model=conclusion.model,
        sections=tuple(sections),
    )


def _limitations_text(
    inn: str, conn: PgConnection | None, data: ReportData, standard: Standard
) -> str:
    """Раздел «Ограничения анализа» тем же составом, что уходит в документ."""
    from finlib.llm.context import build_context

    context = build_context(inn, conn, report_date=data.report_date, standard=standard)
    return context.limitations


def _set_base_style(document: Document) -> None:
    """Базовый шрифт документа."""
    style = document.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)


def _write_header(document: Document, data: ReportData) -> None:
    """Шапка: наименование, реквизиты, период, дисклеймер."""
    organization = data.organization
    document.add_heading(TITLE, level=0)

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
    run = warning.add_run(DISCLAIMER)
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


def _write_sections(document: Document, sections: list[Section]) -> None:
    """Разделы 2–6, написанные моделью."""
    for section in sections:
        document.add_heading(f"{section.number}. {section.title}", level=1)
        for text in section.paragraphs:
            document.add_paragraph(text)


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
