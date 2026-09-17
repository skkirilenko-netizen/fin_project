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
from finlib.report.sections import EXPECTED, TEXT_PART, Section, split_sections
from finlib.report.summary import build_summary
from finlib.scoring.definitions import ScoringCatalog, load_scoring
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Название документа одно на оба режима. Прежде документ без модели назывался
# расчётной справкой: тогда сборка расчётом была запасным путём, а разделы 2,
# 4 и 6 писала модель. Теперь все утверждения — предписанные формулировки
# расчёта в обоих режимах, и различает их только то, кем поставлены связки
# между абзацами. Называть документ справкой из-за связок значило бы утверждать
# о нём неправду.
TITLE = "Заключение о финансовом состоянии"

# Дисклеймер в шапке. Формулировка намеренно не смягчена: документ уходит
# человеку, который будет принимать по нему решение.
# Дисклеймер контекстный: он утверждает факт о конкретном документе,
# а не описывает систему вообще. Шаблонная фраза «текстовая часть подготовлена
# с применением языковой модели» в документе, собранном без модели, — ложное
# утверждение того же рода, что и шаблонная причина исключения из балла.
_COMMON_HEAD = (
    "Документ сформирован автоматически. Расчётная часть — класс, балл, "
    "показатели и контроли качества — получена детерминированным расчётом "
    "по данным бухгалтерской отчётности. "
)
_COMMON_TAIL = (
    " Документ подлежит проверке ответственным сотрудником и самостоятельным "
    "основанием для принятия решения не является."
)

# Режим с моделью. Названо ровно то, что модель делает: утверждения о
# показателях предписаны расчётом и приводятся дословно, языковой модели
# принадлежат переходы между ними.
DISCLAIMER = (
    _COMMON_HEAD
    + "Утверждения текстовой части (разделы 2–6) предписаны методикой "
    "и получены расчётом; текстовые связки между ними порождены языковой "
    "моделью, и текст прошёл автоматическую проверку на соответствие "
    "расчётным данным. Воспроизводимость текстовых связок не гарантируется."
    + _COMMON_TAIL
)

# Режим по умолчанию. Воспроизводимость названа прямо: повторный прогон
# на тех же данных и той же версии методики даёт тот же текст, и это
# свойство документа, а не подробность устройства.
DISCLAIMER_CALCULATED = (
    _COMMON_HEAD
    + "Текстовая часть (разделы 2–6) сформирована детерминированно: "
    "утверждения выбраны из предписанных методикой формулировок по машинным "
    "признакам, связки между ними шаблонные. Языковая модель не привлекалась. "
    "Текст воспроизводим: повторная сборка по тем же данным и той же версии "
    "методики даёт тот же результат."
    + _COMMON_TAIL
)


# Модель не привлекалась: документ собран только из расчётной части.
NO_MODEL = "не привлекалась"

# Оговорка о происхождении текстовой части в режиме по умолчанию. Прежде она
# говорила, что документ заключением не является и служит расчётной справкой:
# тогда сборка расчётом была запасным путём при недоступной модели. Теперь
# это основной режим, и та формулировка дискредитировала бы обычный документ.
# Названо оставшееся различие — связки между утверждениями шаблонные, —
# и названо как свойство, а не как изъян.
CALCULATED_TEXT_NOTICE = (
    "Текстовая часть собрана расчётом. Состав разделов задан методикой, "
    "утверждения о показателях выбраны из предписанных формулировок "
    "по машинным признакам, переходы между ними шаблонные: связного "
    "изложения обстоятельств своими словами в документе нет. Языковая модель "
    "не привлекалась, и текст воспроизводим — повторная сборка по тем же "
    "данным и той же версии методики даёт тот же результат."
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
        # Оговорка о происхождении текста идёт первой, а разделы — после неё:
        # иначе документ сначала печатал раздел 4, а затем сообщал, чем
        # текстовая часть собрана.
        _write_calculated_text_notice(document)
        # Текстовая часть собирается целиком расчётом: разделы 2, 4 и 6
        # и без того его, раздел 3 складывается из предписанных тезисов
        # шаблонными связками, раздел 5 состоит из предписанных оговорок,
        # которые модель всё равно приводила дословно.
        for number, title in TEXT_PART:
            document.add_heading(f"{number}. {title}", level=1)
            if number in _CALCULATED:
                _CALCULATED[number](document, data)
            else:
                for text in _without_model(number, inn, conn, data, standard):
                    document.add_paragraph(text)
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
    """Текст разделов, написанных моделью, из записанного документа.

    Читается с диска, а не из памяти: сверять надо то, что получит читатель,
    вместе с последствиями оформления и сериализации.

    Разделы расчёта в сверку не входят: их числа в тексте модели отсутствуют
    по построению. Прежде они передавались в сверку перечнем исключений —
    теперь разделы просто пропускаются, и перечень не может разойтись с тем,
    что на самом деле напечатано.

    Моделью написаны разделы 3 и 5; собираются они из заголовка до следующего
    заголовка раздела, каким бы он ни был.
    """
    document = Document(str(path))
    collected: list[str] = []
    written = {number for number, _ in EXPECTED}
    inside = False
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        heading = re.match(r"^(\d)\.\s", text)
        if heading is not None:
            inside = int(heading.group(1)) in written
        elif text.startswith("Приложение"):
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


def _write_calculated_text_notice(document: Document) -> None:
    """Оговорка перед разделами, собранными расчётом.

    Разделы на месте и величины в них те же, но связного изложения
    обстоятельств нет, и читатель обязан это видеть, а не обнаруживать
    по складу текста.
    """
    document.add_heading("О происхождении текстовой части", level=1)
    paragraph = document.add_paragraph()
    run = paragraph.add_run(CALCULATED_TEXT_NOTICE)
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
    run = warning.add_run(DISCLAIMER if with_text else DISCLAIMER_CALCULATED)
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

FACT_BASE_SECTION = 2
QUESTIONS_SECTION = 6


def _write_sections(
    document: Document, sections: list[Section], data: ReportData
) -> None:
    """Текстовая часть: разделы 2, 4 и 6 из расчёта, разделы 3 и 5 от модели.

    Разделы расчёта встают на свои места по номеру, а не приписываются
    в конец: порядок задаём мы, и разрыв в нумерации читатель принял бы
    за пропавший раздел.
    """
    written = {item.number: item for item in sections}
    for number, title in TEXT_PART:
        document.add_heading(f"{number}. {title}", level=1)
        if number in _CALCULATED:
            _CALCULATED[number](document, data)
            continue
        section = written.get(number)
        for text in section.paragraphs if section is not None else ():
            document.add_paragraph(text)


def _without_model(
    number: int,
    inn: str,
    conn: PgConnection | None,
    data: ReportData,
    standard: Standard,
) -> list[str]:
    """Раздел 3 или 5, собранный без обращения к модели.

    Раздел 3 складывается из предписанных тезисов шаблонными связками
    (`methodology/theses.yaml`, блок narrative); раздел 5 — из предписанных
    оговорок, которые модель и так приводила дословно. Коды снимаются здесь
    же: они механизм проверки, а не часть заключения.
    """
    from finlib.llm.cleanup import strip_identifiers
    from finlib.scoring.theses import build_theses

    if number == 3:
        found = build_theses(
            inn, conn, report_date=data.report_date, standard=standard
        )
        return [strip_identifiers(item) for item in found.narrative()]
    if number == 5:
        block = _limitations_text(inn, conn, data, standard)
        return [
            line.lstrip("- ").strip()
            for line in block.split("\n")
            if line.startswith("- ")
        ]
    return []  # pragma: no cover — прочих разделов у модели не осталось


def _write_fact_base(document: Document, data: ReportData) -> None:
    """Раздел 2 «Фактическая база» целиком из расчёта."""
    from finlib.metrics.definitions import load_metrics
    from finlib.normalize.lines import ReportingType, load_lines
    from finlib.report.composition import fact_base

    reporting_type = ReportingType(data.organization["reporting_type"])
    for text in fact_base(
        data,
        load_policy(),
        load_lines(),
        load_metrics(),
        load_scoring(),
        reporting_type,
    ):
        document.add_paragraph(text)


def _write_questions(document: Document, data: ReportData) -> None:
    """Раздел 6 «Вопросы к организации» целиком из расчёта."""
    from finlib.metrics.definitions import load_metrics
    from finlib.report.composition import questions

    years = sorted(
        {
            int(row["report_year"])
            for row in data.sources
            if row["status"] == "quarantine"
        }
    )
    found = questions(data, load_policy(), load_metrics(), load_scoring(), years)
    for number, text in enumerate(found, start=1):
        document.add_paragraph(f"{number}. {text}" if len(found) > 1 else text)


def _write_risks(document: Document, data: ReportData) -> None:
    """Раздел 4 «Риски и надзорные сигналы» целиком из расчёта.

    Модели здесь делать нечего: формулировки сигналов предписаны методикой,
    величины и отсечки набраны при оценке, стоп-фактор и его последствие
    объявлены в scoring.yaml. Оставленный ей раздел вырождался — по ООО
    «Магнит» он свёлся к фразе «Надзорный сигнал имеет величину 20,8».
    """
    policy = load_policy()
    if data.signals:
        _write_signals(document, data)
    _write_stop_factor_risk(document, data, policy)
    if not data.signals and not data.stop_factor_code:
        document.add_paragraph(policy.risks.none_found_text)


def _write_stop_factor_risk(
    document: Document, data: ReportData, policy: ReportPolicy
) -> None:
    """Стоп-фактор в картине рисков, формулировкой из методики."""
    code = data.stop_factor_code
    if not code:
        return
    scoring = load_scoring()
    factor = next((item for item in scoring.stop_factors if item.code == code), None)
    if factor is None:  # pragma: no cover — код приходит из той же методики
        return
    heading = document.add_paragraph()
    heading.add_run(policy.risks.stop_factor_text).bold = True
    paragraph = document.add_paragraph()
    paragraph.add_run(f"{factor.name}. ").bold = True
    paragraph.add_run(" ".join(factor.statement.split()))


def _write_signals(document: Document, data: ReportData) -> None:
    """Сработавшие сигналы с предписанными формулировками.

    К каждому сигналу приводится величина и порог, по которому он сработал:
    тезис в этом разделе без числа и отсечки проверить нечем, а отсечки
    экспертные и объявлены предварительными.
    """
    if not data.signals:
        return
    heading = document.add_paragraph()
    heading.add_run(load_policy().risks.intro_text).bold = True
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


# Разделы текстовой части, которые собирает расчёт. Таблица объявлена после
# самих сборщиков: порядок разделов задаёт TEXT_PART, а здесь только сказано,
# кто какой из них пишет.
_CALCULATED = {
    FACT_BASE_SECTION: _write_fact_base,
    SIGNALS_SECTION: _write_risks,
    QUESTIONS_SECTION: _write_questions,
}


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
