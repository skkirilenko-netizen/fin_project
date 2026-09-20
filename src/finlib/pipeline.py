"""Полный цикл обработки одной организации: от источника до заключения.

Этапы вынесены сюда, а не в CLI, по двум причинам. Первая: цикл нужен и вне
терминала — в регрессионном прогоне задачи 17. Вторая: CLI обязан сообщать,
на каком этапе он стоит и почему остановился, а для этого этапы должны быть
названными сущностями, а не строками кода.

Каждый этап либо проходит, либо останавливает цикл с названной причиной.
Молчаливого продолжения после неудачи нет: заключение по неполным данным
хуже отсутствия заключения.
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from pathlib import Path

from finlib.db import PgConnection, connection
from finlib.llm.service import (
    DEFAULT_SCHEME,
    ConclusionRejectedError,
    PromptScheme,
    generate_conclusion,
)
from finlib.metrics.engine import compute_all
from finlib.metrics.store import save_results
from finlib.normalize.lines import UnitSource
from finlib.normalize.loader import load_report_set, max_correction
from finlib.quality.codes import CheckStatus
from finlib.quality.journal import log_check
from finlib.quality.runner import run_checks
from finlib.report.consistency import InconsistentReportError
from finlib.report.document import build_report
from finlib.report.sections import MissingSectionError
from finlib.scoring.engine import assess
from finlib.scoring.store import save_assessment
from finlib.sources.errors import (
    CreditOrganizationError,
    OrganizationNotFoundError,
    ReportsNotPublishedError,
    SourceUnavailableError,
)
from finlib.sources.girbo import GirboSource
from finlib.sources.inbox import InboxRejectedError, InboxSource, ParsedFile
from finlib.sources.model import Organization, ReportSet, SourceKind
from finlib.standards import Standard

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    """Этап обработки; порядок значений — порядок выполнения."""

    # Сверка схемы идёт прежде работы с данными: база умеет молча разойтись
    # с sql/001_schema.sql, и тогда отказ приходит не там, где причина,
    # а отсутствующий внешний ключ не приходит вовсе.
    SCHEMA = "сверка схемы базы"
    FETCH = "получение отчётности"
    LOAD = "нормализация и загрузка"
    QUALITY = "контроли качества"
    METRICS = "расчёт показателей"
    SCORING = "оценка и класс"
    CONCLUSION = "текстовая часть"
    DOCUMENT = "сборка документа"


class PipelineError(RuntimeError):
    """Цикл остановлен на названном этапе с названной причиной."""

    def __init__(self, stage: Stage, reason: str) -> None:
        super().__init__(f"остановлено на этапе «{stage.value}»: {reason}")
        self.stage = stage
        self.reason = reason


@dataclass
class StageResult:
    """Итог одного этапа для вывода пользователю."""

    stage: Stage
    message: str
    ok: bool = True


@dataclass
class PipelineResult:
    """Итог цикла целиком."""

    inn: str
    report_date: date | None = None
    document: Path | None = None
    stages: list[StageResult] = field(default_factory=list)
    quarantined: int = 0
    with_llm: bool = False


def analyze(
    inn: str,
    *,
    year: int | None = None,
    standard: Standard = Standard.RSBU,
    with_llm: bool = False,
    force_refresh: bool = False,
    from_cache_only: bool = False,
    directory: Path | None = None,
    source: SourceKind = SourceKind.GIR_BO,
    inbox: InboxSource | None = None,
    on_stage: Callable[[StageResult], None] | None = None,
    scheme: PromptScheme = DEFAULT_SCHEME,
) -> PipelineResult:
    """Проводит организацию через весь цикл и возвращает путь к заключению.

    with_llm по умолчанию выключен: текстовую часть собирает расчёт. Замер
    17.09.2026 на подвыборке из семи организаций показал, чем оборачивается
    обратное умолчание — документов 4 из 7 против 7 из 7, тридцать минут
    против полутора секунд, а вклад модели укладывается в связки одного
    раздела. Обращение к модели включается явно.

    from_cache_only пропускает обращение к источнику: пересчёт идёт по уже
    загруженным фактам. force_refresh, наоборот, заставляет источник ответить
    заново, минуя кэш.

    source выбирает, откуда берётся отчётность. Комплект, поданный файлом,
    проходит те же этапы, что и полученный из источника: загрузку, контроли,
    расчёт, оценку и сборку документа.

    scheme выбирает схему текстовой части: свободную генерацию или сборку
    из предписанных тезисов. На расчётный слой она не влияет — сравнение схем
    затем и нужно, чтобы отличать поведение текстового слоя от расчётного.
    """
    result = PipelineResult(inn=inn, with_llm=with_llm)

    def report(stage: Stage, message: str, ok: bool = True) -> None:
        item = StageResult(stage, message, ok)
        result.stages.append(item)
        if on_stage is not None:
            on_stage(item)

    with connection() as conn:
        _check_schema(conn, report)
        if from_cache_only:
            report(Stage.FETCH, "пропущено: пересчёт из ранее загруженных данных")
            report(Stage.LOAD, "пропущено: пересчёт из ранее загруженных данных")
            # Контроли прогоняются и здесь. Прежде они выполнялись только
            # внутри загрузки, и пересчёт брал их результаты из журнала —
            # то есть от прошлой загрузки, какой бы давней та ни была.
            # Контроли считаются по методике, методика правится, и результат
            # обязан меняться вместе с ней: регрессионный прогон затем
            # и запускается при каждой её правке.
            _run_quality(inn, conn, standard, report, result)
        elif source is SourceKind.FILE:
            _load_from_inbox(inn, year, conn, inbox, standard, report, result)
        else:
            _fetch_and_load(inn, year, conn, force_refresh, standard, report, result)

        _compute(inn, conn, standard, report, result)

    if with_llm:
        result.document = _conclude(inn, result, standard, directory, report, scheme)
    else:
        result.document = _document_without_text(
            inn, result, standard, directory, report
        )
    return result


def _check_schema(conn: PgConnection, report: Callable[..., None]) -> None:
    """Сверяет схему базы с DDL и останавливает цикл при нехватке объектов.

    Сверка идёт один раз на процесс: схема за время прогона не меняется,
    а регрессионный набор проводит через цикл полсотни организаций подряд.
    Этап называется в выводе всегда — в том числе когда расхождений нет:
    молчаливая проверка неотличима от невыполненной.
    """
    from finlib.schema import SchemaMismatchError, ensure_schema

    try:
        ensure_schema(conn)
    except SchemaMismatchError as exc:
        raise PipelineError(
            Stage.SCHEMA,
            f"{len(exc.problems)} объектов DDL нет в базе: {'; '.join(exc.problems[:3])}. "
            "Примените схему: psql findb -f sql/001_schema.sql",
        ) from exc
    report(Stage.SCHEMA, "схема базы совпадает с sql/001_schema.sql")


def _fetch_and_load(
    inn: str,
    year: int | None,
    conn: PgConnection,
    force_refresh: bool,
    standard: Standard,
    report: Callable[..., None],
    result: PipelineResult,
) -> None:
    """Получает отчётность из источника и загружает комплекты."""
    try:
        with GirboSource() as source:
            organization, sets = source.fetch_report_sets(
                inn, force_refresh=force_refresh
            )
    except OrganizationNotFoundError as exc:
        raise PipelineError(Stage.FETCH, f"организация не найдена: {exc}") from exc
    except ReportsNotPublishedError as exc:
        raise PipelineError(Stage.FETCH, f"отчётность не опубликована: {exc}") from exc
    except CreditOrganizationError as exc:
        raise PipelineError(
            Stage.FETCH,
            "кредитная организация: отчётность сдаётся в Банк России "
            "по формам 0409 и методикой не разбирается",
        ) from exc
    except SourceUnavailableError as exc:
        raise PipelineError(Stage.FETCH, f"источник недоступен: {exc}") from exc

    chosen = [item for item in sets if year is None or item.report_year <= year]
    if not chosen:
        raise PipelineError(
            Stage.FETCH, f"за {year} год и ранее опубликованных комплектов нет"
        )
    report(
        Stage.FETCH,
        f"{organization.short_name or inn}: комплектов {len(chosen)}, "
        f"годы {min(i.report_year for i in chosen)}–{max(i.report_year for i in chosen)}",
    )

    loaded = 0
    for item in sorted(chosen, key=lambda value: value.report_year):
        loaded += 1
        load_report_set(item, organization, conn, standard=standard)
    report(Stage.LOAD, f"загружено комплектов: {loaded}")
    _run_quality(inn, conn, standard, report, result)


def load_inbox(
    inn: str,
    *,
    standard: Standard = Standard.RSBU,
    inbox: InboxSource | None = None,
    on_stage: Callable[[StageResult], None] | None = None,
) -> PipelineResult:
    """Проводит поданные файлы организации через загрузку и контроли качества.

    Расчёт показателей и оценка сюда не входят: подача пакетная, и считать
    имеет смысл по всему поданному сразу, а не по каждой организации в момент
    её загрузки. Этапы те же, что и в полном цикле, и останавливаются они
    так же — с названной причиной.
    """
    result = PipelineResult(inn=inn, with_llm=False)

    def report(stage: Stage, message: str, ok: bool = True) -> None:
        item = StageResult(stage, message, ok)
        result.stages.append(item)
        if on_stage is not None:
            on_stage(item)

    with connection() as conn:
        _check_schema(conn, report)
        _load_from_inbox(
            inn, None, conn, inbox, standard, report, result,
            stop_if_all_quarantined=False,
        )
    return result


@dataclass
class IfrsIntake:
    """Итог приёма документа МСФО: параметры, извлечение и решение сверки."""

    accepted: bool
    reason: str | None = None
    check_code: str | None = None
    # Подробности отказа, которые нужно увидеть глазами: у отказа
    # по разделителю разрядов это сами числа-свидетельства. Счётчик говорит,
    # сколько улик нашлось, и не говорит, чего они стоят.
    details: dict[str, object] | None = None
    profile: object | None = None
    extraction: object | None = None
    review: object | None = None
    # Итог записи комплекта в базу; None — запись не выполнялась, потому что
    # организация не названа.
    loaded: object | None = None


def accept_ifrs_document(
    text: str,
    on_stage: Callable[[StageResult], None] | None = None,
    *,
    inn: str | None = None,
    raw_path: str | None = None,
    confirmed_by: str | None = None,
    confirmations: dict[str, str] | None = None,
    document: object | None = None,
) -> IfrsIntake:
    """Проводит документ МСФО через приём, разбор форм и экран сверки.

    Отдельная точка входа, а не ветка внутри `analyze`: у документа МСФО
    до загрузки в базу проходит своя последовательность — определение
    параметров, извлечение форм, сверка, — и её итог человек видит прежде,
    чем комплект попадает в расчёт.

    Здесь же контроли ветки МСФО становятся достижимыми от цикла. Пока
    документ не проходил через эту функцию, все они числились
    неподключёнными: код был написан, покрыт тестами и никем не вызывался.
    """
    from finlib.sources.ifrs_extract import extract
    from finlib.sources.ifrs_inbox import Rejection, identify
    from finlib.sources.ifrs_review import review

    def report(stage: Stage, message: str, ok: bool = True) -> None:
        if on_stage is not None:
            on_stage(StageResult(stage, message, ok))

    # `document` нужен одной проверке, которую по плоскому тексту сделать
    # нельзя: не потеряна ли страница внутри форм. Без него эта проверка
    # в цикле всегда отвечала «потерь нет» — то есть не работала вовсе,
    # хотя выглядела работающей: у Автодора так пропала вся сторона пассива,
    # у Самолёта — две страницы внутри форм.
    profile = identify(text, document=document)
    if isinstance(profile, Rejection):
        report(Stage.LOAD, f"документ отклонён: {profile.reason}", ok=False)
        return IfrsIntake(False, profile.reason, profile.code.value, profile.details)
    report(Stage.LOAD, f"документ принят: {profile.describe()}")

    columns = getattr(document, "columns_of", None)
    extraction = extract(
        text,
        profile.dates_by_form,
        profile.grouping,
        columns=columns,
        layouts=profile.columns_by_form,
    )
    report(Stage.LOAD, extraction.describe())

    # Аудиторское заключение читается здесь же: его сведения относятся
    # к самой отчётности, и без них журнал комплекта молчал бы о том,
    # с оговоркой она выпущена или без.
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.sources.ifrs_audit import read_audit_report
    from finlib.sources.ifrs_inbox import form_headings
    from finlib.sources.ifrs_numbers import load_parsing_policy

    headings = form_headings(text, load_ifrs_lines(), load_parsing_policy())
    audit = read_audit_report(text, before=min(headings.values(), default=0))
    report(Stage.LOAD, f"аудиторское заключение: {audit.describe()}")

    # Ранее подтверждённое опознание у этого же эмитента — такое же знание,
    # как справочник, только слабее: оно говорит о строке этой организации,
    # а не о строке вообще. Для повторного комплекта того же эмитента этого
    # довольно, и без него автопрохождение недостижимо в принципе: экран
    # видел бы неопознанными строки, о которых человек уже сказал, чем они
    # являются, и сошедшиеся у ЛСР итоги читал бы как провал контроля.
    from finlib.sources.ifrs_confirmed import load_confirmed

    confirmed = load_confirmed(inn, extraction, profile)
    decision = review(extraction, profile, confirmed=confirmed)
    report(
        Stage.QUALITY,
        decision.describe(),
        ok=decision.automatic,
    )

    intake = IfrsIntake(True, profile=profile, extraction=extraction, review=decision)
    if inn is None:
        # Без организации комплект не записывается: привязать его не к чему.
        # Это разбор ради разбора — им пользуется прогон приёма, который
        # отвечает на вопрос о доле автоматического прохождения, а базу
        # не трогает.
        return intake

    from finlib.normalize.ifrs_loader import load_extraction

    with connection() as conn:
        _check_schema(conn, report)
        loaded = load_extraction(
            inn,
            extraction,
            profile,
            decision,
            conn,
            raw_path=raw_path,
            confirmed_by=confirmed_by,
            confirmations=confirmations,
            audit=audit,
        )
    report(Stage.LOAD, loaded.summary(), ok=not loaded.quarantined)
    intake.loaded = loaded
    return intake


def _load_from_inbox(
    inn: str,
    year: int | None,
    conn: PgConnection,
    inbox: InboxSource | None,
    standard: Standard,
    report: Callable[..., None],
    result: PipelineResult,
    *,
    stop_if_all_quarantined: bool = True,
) -> None:
    """Загружает комплекты организации из поданных вручную файлов."""
    source = inbox if inbox is not None else InboxSource()
    try:
        organization, files = source.fetch_report_sets(inn)
    except InboxRejectedError as exc:
        raise PipelineError(Stage.FETCH, str(exc)) from exc

    _journal_rejections(source.rejections(inn), conn)
    chosen = [item for item in files if year is None or item.report.report_year <= year]
    if not chosen:
        raise PipelineError(
            Stage.FETCH, f"за {year} год и ранее поданных комплектов нет"
        )
    years = [item.report.report_year for item in chosen]
    report(
        Stage.FETCH,
        f"{organization.full_name or inn}: файлов {len(chosen)}, "
        f"годы {min(years)}–{max(years)}",
    )

    loaded = 0
    for item in sorted(chosen, key=lambda value: value.report.report_year):
        _load_file(item, organization, conn, standard)
        loaded += 1
    report(Stage.LOAD, f"загружено комплектов: {loaded}")
    _run_quality(
        inn,
        conn,
        standard,
        report,
        result,
        stop_if_all_quarantined=stop_if_all_quarantined,
    )


def _load_file(
    item: ParsedFile,
    organization: Organization,
    conn: PgConnection,
    standard: Standard,
) -> None:
    """Загружает один разобранный файл как комплект отчётности."""
    report_set = item.report
    if report_set.is_actual:
        report_set = _actual_against_loaded(report_set, conn, standard)
    load_report_set(
        report_set,
        organization,
        conn,
        raw_path=str(item.path),
        checksum=item.checksum,
        standard=standard,
        source=SourceKind.FILE,
        # Выгрузка печатает единицу измерения в реквизитах, и она опознана
        # справочником: это объявление источника, а не правило о форме.
        unit_source=UnitSource.EXPLICIT,
        meta_extra=item.meta,
    )


def _actual_against_loaded(
    report: ReportSet, conn: PgConnection, standard: Standard
) -> ReportSet:
    """Снимает признак актуальности, если в базе уже лежит корректировка новее.

    Номер актуальной корректировки сообщает только сам ресурс, а он недоступен.
    По поданным файлам актуальной считается наибольшая из них, но подача старой
    версии не должна отменять загруженную новую.
    """
    loaded = max_correction(report.inn, report.report_year, SourceKind.FILE, standard, conn)
    if loaded is None or report.correction_version >= loaded:
        return report
    logger.info(
        "комплект %s за %s год: корректировка %s не актуальнее загруженной %s",
        report.inn,
        report.report_year,
        report.correction_version,
        loaded,
    )
    return replace(report, is_actual=False)


def _journal_rejections(
    rejections: list[tuple[Path, InboxRejectedError]], conn: PgConnection
) -> None:
    """Пишет в журнал качества файлы, отклонённые разбором.

    Комплекта у такого файла нет, поэтому запись идёт без src_file_id: карантин
    ставится на комплект, а здесь его не возникло вовсе. Файл, у которого
    не определился даже ИНН, в журнал попасть не может — его не к чему
    привязать, и он остаётся в отчёте загрузки.
    """
    for path, exc in rejections:
        if exc.inn is None:
            continue
        log_check(
            inn=exc.inn,
            check_code=exc.check_code,
            status=CheckStatus.FAIL,
            message=str(exc),
            details={"file_name": path.name},
            conn=conn,
        )


def _run_quality(
    inn: str,
    conn: PgConnection,
    standard: Standard,
    report: Callable[..., None],
    result: PipelineResult,
    *,
    stop_if_all_quarantined: bool = True,
) -> None:
    """Прогоняет контроли по всем комплектам организации.

    Полный цикл на сплошном карантине останавливается: считать нечего.
    Пакетная загрузка — нет: отбракованный комплект обязан остаться в базе
    вместе с причиной отбраковки, иначе journal отката не переживёт,
    и сказать, почему организация выпала, будет нечем.
    """
    from finlib.db import fetch_all

    rows = fetch_all(
        "SELECT id FROM src_file WHERE inn = %(i)s AND standard = %(s)s AND is_actual "
        "ORDER BY report_year",
        {"i": inn, "s": standard.value},
        conn=conn,
    )
    quarantined = 0
    for row in rows:
        if run_checks(row["id"], conn).quarantined:
            quarantined += 1
    result.quarantined = quarantined
    message = f"проверено комплектов {len(rows)}, в карантине {quarantined}"
    report(Stage.QUALITY, message, ok=quarantined < len(rows))
    if rows and quarantined == len(rows) and stop_if_all_quarantined:
        raise PipelineError(
            Stage.QUALITY,
            "все комплекты отчётности отбракованы контролями качества, "
            "расчёт невозможен",
        )


def _compute(
    inn: str,
    conn: PgConnection,
    standard: Standard,
    report: Callable[..., None],
    result: PipelineResult,
) -> None:
    """Считает показатели и оценку."""
    results = compute_all(inn, conn, standard=standard)
    if not results:
        raise PipelineError(Stage.METRICS, _no_metrics_reason(inn, conn, standard))
    saved = save_results(inn, results, conn, standard)
    calculated = sum(1 for item in results if item.is_ok)
    report(Stage.METRICS, f"значений записано {saved}, из них рассчитано {calculated}")

    assessment = assess(inn, conn, standard=standard)
    if assessment is None:
        raise PipelineError(Stage.SCORING, "оценка не рассчитана: нет периодов")
    save_assessment(assessment, conn)
    result.report_date = assessment.report_date
    verdict = (
        f"класс {assessment.class_code}"
        if assessment.class_code
        else f"класс не присвоен ({assessment.no_class_reason})"
    )
    report(Stage.SCORING, f"{assessment.report_date:%d.%m.%Y}: {verdict}")


def _no_metrics_reason(inn: str, conn: PgConnection, standard: Standard) -> str:
    """Почему не рассчитан ни один показатель.

    Причины две, и путать их нельзя: отчётности нет вовсе либо она есть,
    но целиком отбракована контролями. Пересчёт из ранее загруженных данных
    контролей не прогоняет, поэтому сказать об отбраковке может только этот
    этап — иначе в отчёте остаётся «не рассчитан ни один показатель»
    без причины.
    """
    from finlib.db import fetch_all

    rows = fetch_all(
        "SELECT count(*) AS total, count(*) FILTER (WHERE status = 'quarantine') AS "
        "quarantined FROM src_file WHERE inn = %(i)s AND standard = %(s)s AND is_actual",
        {"i": inn, "s": standard.value},
        conn=conn,
    )
    total = int(rows[0]["total"]) if rows else 0
    quarantined = int(rows[0]["quarantined"]) if rows else 0
    if total and quarantined == total:
        return (
            f"все комплекты отчётности ({total}) отбракованы контролями качества, "
            "расчёт невозможен"
        )
    return "по загруженным данным не рассчитан ни один показатель"


def _conclude(
    inn: str,
    result: PipelineResult,
    standard: Standard,
    directory: Path | None,
    report: Callable[..., None],
    scheme: PromptScheme = DEFAULT_SCHEME,
) -> Path:
    """Порождает текстовую часть и собирает документ."""
    try:
        conclusion = generate_conclusion(
            inn, report_date=result.report_date, standard=standard, scheme=scheme
        )
    except ConclusionRejectedError as exc:
        raise PipelineError(
            Stage.CONCLUSION,
            f"ответ модели отклонён постпроверкой. {'; '.join(exc.foreign[:5])}",
        ) from exc
    except OSError as exc:
        raise PipelineError(Stage.CONCLUSION, f"модель недоступна: {exc}") from exc
    report(
        Stage.CONCLUSION,
        f"принято с попытки {conclusion.attempt}, сверено чисел "
        f"{conclusion.checked_numbers}",
    )
    return _write(inn, result, standard, directory, report, conclusion)


def _document_without_text(
    inn: str,
    result: PipelineResult,
    standard: Standard,
    directory: Path | None,
    report: Callable[..., None],
) -> Path:
    """Собирает документ без разделов модели.

    Расчётная часть — класс, показатели, контроли — не зависит от модели
    и остаётся полной. Отсутствие текстовых разделов в документе оговаривается
    прямо, чтобы читатель не принял его за полное заключение.
    """
    report(Stage.CONCLUSION, "пропущено по требованию: документ без текстовой части")
    return _write(inn, result, standard, directory, report, None)


def _write(
    inn: str,
    result: PipelineResult,
    standard: Standard,
    directory: Path | None,
    report: Callable[..., None],
    conclusion,
) -> Path:
    """Собирает документ и сообщает путь."""
    try:
        rendered = build_report(
            inn,
            report_date=result.report_date,
            standard=standard,
            conclusion=conclusion,
            directory=directory,
            with_text=conclusion is not None,
        )
    except MissingSectionError as exc:
        raise PipelineError(Stage.DOCUMENT, str(exc)) from exc
    except InconsistentReportError as exc:
        raise PipelineError(Stage.DOCUMENT, str(exc)) from exc
    report(Stage.DOCUMENT, f"записано: {rendered.path}")
    return rendered.path


def stages_of(result: PipelineResult) -> Iterator[StageResult]:
    """Этапы цикла по порядку — для вывода и для тестов."""
    yield from result.stages
