"""Точка входа: команды analyze, show, quality, reprocess.

Вывод рассчитан на человека за терминалом: на каждом этапе видно, что
происходит, а остановка называет этап и причину. Молчаливых неудач нет —
цикл, прерванный на контролях качества, обязан сказать, на чём именно.
"""

import logging
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from finlib.db import fetch_all
from finlib.llm.service import PromptScheme
from finlib.metrics.definitions import Unit
from finlib.metrics.display import format_metric
from finlib.pipeline import PipelineError, StageResult, analyze, load_inbox
from finlib.sources.inbox import InboxScan, InboxSource
from finlib.sources.model import SourceKind
from finlib.standards import Standard

app = typer.Typer(
    add_completion=False,
    help="Анализ финансового состояния юрлица по бухгалтерской отчётности РСБУ.",
)

INN_HELP = "ИНН организации: 10 цифр для юрлица, 12 для предпринимателя"

_METRICS = """
SELECT m.report_date, m.metric_code, m.value, m.status, m.confidence, m.reason
FROM metric_value m
WHERE m.inn = %(inn)s AND m.standard = %(s)s
ORDER BY m.metric_code, m.report_date DESC
"""

_CHECKS = """
SELECT d.check_code, d.severity, d.status, d.report_date, d.line_code, d.message
FROM dq_log d
WHERE d.inn = %(inn)s
ORDER BY d.severity, d.check_code, d.report_date DESC NULLS LAST
"""

_ASSESSMENT = """
SELECT report_date, class_code, class_name, total_score, no_class_reason,
       breadth_reason, stop_factor_code, confidence
FROM assessment WHERE inn = %(inn)s AND standard = %(s)s
ORDER BY report_date DESC LIMIT 1
"""


def _setup_logging(verbose: bool) -> None:
    """Подробный журнал только по требованию: обычный вывод и так говорящий."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _echo_stage(item: StageResult) -> None:
    """Печатает итог этапа по мере прохождения."""
    mark = typer.style("✓", fg=typer.colors.GREEN) if item.ok else typer.style(
        "!", fg=typer.colors.YELLOW
    )
    typer.echo(f"  {mark} {item.stage.value}: {item.message}")


def _fail(message: str) -> None:
    """Останавливает команду с внятной причиной."""
    typer.echo(typer.style(f"\nОстановлено. {message}", fg=typer.colors.RED), err=True)
    raise typer.Exit(code=1)


def _check_inn(inn: str) -> str:
    """Проверяет форму ИНН до любых обращений к сети и базе."""
    if not (inn.isdigit() and len(inn) in (10, 12)):
        _fail(f"ИНН «{inn}» не похож на настоящий: ожидается 10 или 12 цифр")
    return inn


# Короткое имя схемы в терминале — не то же, что имя шаблона в prompts/:
# в журнал идёт имя шаблона, а пользователю называется схема.
_SCHEMES: dict[str, PromptScheme] = {
    "free": PromptScheme.FREE,
    "theses": PromptScheme.THESES,
}


def _scheme(name: str) -> PromptScheme:
    """Схема текстовой части по имени из командной строки."""
    found = _SCHEMES.get(name)
    if found is None:
        _fail(f"схема «{name}» неизвестна: допустимы {', '.join(sorted(_SCHEMES))}")
    return found


def _render(value: Decimal | None, unit: Unit, scale: int) -> str:
    """Значение показателя в единице и разрядности методики."""
    if value is None:
        return "—"
    return format_metric(value, unit, scale)


@app.command("analyze")
def analyze_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    year: Annotated[int | None, typer.Option("--year", help="Последний отчётный год")] = None,
    llm: Annotated[
        bool,
        typer.Option(
            "--llm",
            help="Поручить связки между утверждениями языковой модели; "
            "по умолчанию текстовая часть собирается расчётом",
        ),
    ] = False,
    force_refresh: Annotated[
        bool, typer.Option("--force-refresh", help="Запросить источник, минуя кэш")
    ] = False,
    from_inbox: Annotated[
        bool,
        typer.Option(
            "--from-inbox",
            help="Взять отчётность из поданных вручную файлов, а не из источника",
        ),
    ] = False,
    inbox_dir: Annotated[
        Path | None, typer.Option("--inbox", help="Каталог ручной подачи")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Каталог для документа")
    ] = None,
    prompt_scheme: Annotated[
        str,
        typer.Option(
            "--prompt-scheme",
            help="Схема текстовой части: free — свободная генерация, "
            "theses — сборка из предписанных тезисов",
        ),
    ] = "free",
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Полный цикл: получение, загрузка, контроли, расчёт, оценка, заключение."""
    _setup_logging(verbose)
    _check_inn(inn)
    scheme = _scheme(prompt_scheme)
    source = SourceKind.FILE if from_inbox or inbox_dir else SourceKind.GIR_BO
    where = "по поданным файлам" if source is SourceKind.FILE else "по данным ГИР БО"
    typer.echo(f"Анализ организации {inn} {where}\n")
    try:
        result = analyze(
            inn,
            year=year,
            with_llm=llm,
            force_refresh=force_refresh,
            directory=output,
            source=source,
            inbox=InboxSource(inbox_dir) if source is SourceKind.FILE else None,
            on_stage=_echo_stage,
            scheme=scheme,
        )
    except PipelineError as exc:
        _fail(f"Этап «{exc.stage.value}». {exc.reason}")
    else:
        typer.echo(
            typer.style(f"\nГотово: {result.document}", fg=typer.colors.GREEN, bold=True)
        )
        if result.quarantined:
            typer.echo(
                typer.style(
                    f"Комплектов в карантине: {result.quarantined}. "
                    "Отбракованные периоды в расчёт не вошли.",
                    fg=typer.colors.YELLOW,
                )
            )


@app.command("ingest")
def ingest_command(
    path: Annotated[
        Path | None, typer.Option("--path", help="Каталог ручной подачи")
    ] = None,
    inn: Annotated[
        str | None, typer.Option("--inn", help="Загрузить только одну организацию")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Загружает поданные вручную файлы отчётности и прогоняет контроли качества.

    Организация, период и единица измерения берутся из содержимого файла;
    имя файла не значит ничего. Файл, у которого их определить нельзя,
    не загружается, а причина называется здесь и — когда ИНН известен —
    в журнале качества.
    """
    _setup_logging(verbose)
    source = InboxSource(path)
    scan = source.scan()
    targets = [_check_inn(inn)] if inn else scan.inns
    typer.echo(
        f"Каталог подачи: {source.directory}\n"
        f"Файлов: {len(scan.unattributed) + sum(len(v) for v in scan.by_inn.values())}, "
        f"организаций: {len(scan.inns)}\n"
    )
    if inn and inn not in scan.by_inn:
        _fail(f"в каталоге подачи нет файлов по ИНН {inn}")

    loaded = quarantined = failed = 0
    for target in targets:
        typer.echo(typer.style(f"ИНН {target}", bold=True))
        try:
            result = load_inbox(target, inbox=source, on_stage=_echo_stage)
        except PipelineError as exc:
            failed += 1
            typer.echo(
                typer.style(
                    f"  ! остановлено на этапе «{exc.stage.value}»: {exc.reason}",
                    fg=typer.colors.RED,
                )
            )
            continue
        loaded += 1
        quarantined += result.quarantined

    _echo_rejected(source, scan)
    typer.echo(
        f"\nОрганизаций загружено {loaded}, остановлено {failed}; "
        f"комплектов в карантине {quarantined}."
    )


def _echo_rejected(source: InboxSource, scan: InboxScan) -> None:
    """Печатает файлы, которые разобрать не удалось.

    Файл без определимого ИНН к организации не привязать, и в журнале качества
    ему места нет: журнал ведётся по организациям. Поэтому он называется здесь
    и только здесь.
    """
    rejected = source.rejections()
    if rejected:
        typer.echo(typer.style("\nОтклонённые файлы:", fg=typer.colors.YELLOW, bold=True))
        for path, exc in rejected:
            typer.echo(
                typer.style(
                    f"  {path.name}: {exc.check_code.value} — {exc}", fg=typer.colors.YELLOW
                )
            )
    if scan.unattributed:
        typer.echo(
            typer.style(
                "\nФайлы, у которых не определился ИНН (в журнал качества "
                "не попадают — привязать их не к чему):",
                fg=typer.colors.RED,
                bold=True,
            )
        )
        for path in scan.unattributed:
            typer.echo(typer.style(f"  {path.name}", fg=typer.colors.RED))


@app.command("show")
def show_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Показывает рассчитанные показатели и оценку."""
    _setup_logging(verbose)
    _check_inn(inn)
    from finlib.metrics.definitions import load_metrics

    catalog = load_metrics()
    params = {"inn": inn, "s": Standard.RSBU.value}
    rows = fetch_all(_METRICS, params)
    if not rows:
        _fail(f"по ИНН {inn} показателей не рассчитано. Сначала: analyze --inn {inn}")

    by_metric: dict[str, list[dict]] = {}
    periods: set[date] = set()
    for row in rows:
        by_metric.setdefault(row["metric_code"], []).append(row)
        periods.add(row["report_date"])
    ordered = sorted(periods, reverse=True)[:3]

    header = f"{'Показатель':<42}" + "".join(f"{p:%d.%m.%Y}".rjust(20) for p in ordered)
    typer.echo(typer.style(header, bold=True))
    typer.echo("─" * len(header))
    shown = 0
    for code in sorted(by_metric):
        metric = catalog.get(code)
        if metric is None:
            continue  # производные величины: их место в приложении, не в сводке
        shown += 1
        values = {item["report_date"]: item for item in by_metric[code]}
        cells = ""
        for period in ordered:
            item = values.get(period)
            text = (
                _render(item["value"], metric.unit, catalog.scale_for(code))
                if item and item["status"] == "ok"
                else "—"
            )
            cells += text.rjust(20)
        typer.echo(f"{metric.name[:41]:<42}{cells}")
    typer.echo(f"\nПоказателей: {shown}")
    _echo_assessment(params)


def _echo_assessment(params: dict[str, str]) -> None:
    """Печатает вердикт под таблицей показателей."""
    rows = fetch_all(_ASSESSMENT, params)
    if not rows:
        typer.echo("Оценка не рассчитана.")
        return
    row = rows[0]
    if row["class_code"]:
        line = f"Класс: {row['class_code']} — {row['class_name']}"
        # Балл приводится только когда балльная оценка сформирована: класс
        # от стоп-фактора при узком основании балла не имеет.
        if row["total_score"] is not None and not row["breadth_reason"]:
            line += f", балл {row['total_score']:.2f} из 100"
        typer.echo(typer.style(f"\n{line}", bold=True))
    else:
        typer.echo(
            typer.style(f"\nКласс не присвоен: {row['no_class_reason']}", bold=True)
        )
    if row["breadth_reason"]:
        typer.echo(f"Балльная оценка не формируется: {row['breadth_reason']}")
    if row["stop_factor_code"]:
        typer.echo(f"Сработал стоп-фактор: {row['stop_factor_code']}")
    typer.echo(f"Уверенность в оценке: {row['confidence']}")


@app.command("quality")
def quality_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    failed_only: Annotated[
        bool, typer.Option("--failed-only", help="Только сработавшие контроли")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Показывает журнал контролей качества."""
    _setup_logging(verbose)
    _check_inn(inn)
    rows = fetch_all(_CHECKS, {"inn": inn})
    if not rows:
        _fail(f"по ИНН {inn} контроли не выполнялись. Сначала: analyze --inn {inn}")

    problems = [row for row in rows if row["status"] in ("fail", "warning")]
    shown = problems if failed_only else rows
    colors = {"fail": typer.colors.RED, "warning": typer.colors.YELLOW}
    for row in shown:
        period = f"{row['report_date']:%d.%m.%Y}" if row["report_date"] else "комплект"
        line = f"  {row['status']:<8} {row['check_code']:<24} {period:<12} {row['message'][:70]}"
        typer.echo(typer.style(line, fg=colors.get(row["status"])))

    typer.echo(
        f"\nЗаписей {len(rows)}, из них сработало {len(problems)} "
        f"({sum(1 for r in problems if r['severity'] == 'blocking')} блокирующих)."
    )


@app.command("reprocess")
def reprocess_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    llm: Annotated[
        bool,
        typer.Option(
            "--llm",
            help="Поручить связки между утверждениями языковой модели; "
            "по умолчанию текстовая часть собирается расчётом",
        ),
    ] = False,
    output: Annotated[
        Path | None, typer.Option("--output", help="Каталог для документа")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Пересчитывает из ранее загруженных данных, не обращаясь к источнику."""
    _setup_logging(verbose)
    _check_inn(inn)
    typer.echo(f"Пересчёт организации {inn} без обращения к источнику\n")
    try:
        result = analyze(
            inn,
            with_llm=llm,
            from_cache_only=True,
            directory=output,
            on_stage=_echo_stage,
        )
    except PipelineError as exc:
        _fail(f"Этап «{exc.stage.value}». {exc.reason}")
    else:
        typer.echo(
            typer.style(f"\nГотово: {result.document}", fg=typer.colors.GREEN, bold=True)
        )


@app.command("ifrs-markup")
def ifrs_markup_command(
    path: Annotated[
        Path, typer.Option("--path", help="Каталог с документами МСФО по ИНН")
    ] = Path("data/raw/ifrs"),
    who: Annotated[
        str, typer.Option("--who", help="Кто размечает: попадёт в подтверждение")
    ] = "",
    limit: Annotated[
        int, typer.Option("--limit", help="Сколько строк показать за присест")
    ] = 0,
    grouping: Annotated[
        list[str],
        typer.Option(
            "--grouping",
            help="Конвенция чисел вручную: ИНН=russian|english|plain, можно "
            "повторять. Для документа, у которого разметка не читается "
            "ни голосованием, ни арифметикой",
        ),
    ] = [],  # noqa: B006 — typer требует list по умолчанию
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Разметка неопознанных строк МСФО: присвоение кодов позициям модели.

    Строки показываются не по частоте, а по влиянию на арифметику: сначала
    те, без которых не сходится итог раздела. Это даёт двойную проверку —
    присвоил код, итог сошёлся, значит опознал верно.
    """
    _setup_logging(verbose)
    if not who.strip():
        _fail("укажите --who: подтверждение без автора в журнале бесполезно")

    manual: dict[str, str] = {}
    for item in grouping:
        inn, _, convention = item.partition("=")
        if not inn.strip() or not convention.strip():
            _fail(f"--grouping принимает пару ИНН=конвенция, получено «{item}»")
        manual[inn.strip()] = convention.strip()

    issuers, skipped = _load_issuers(path, manual)
    if not issuers:
        _fail(f"в каталоге {path} нет документов, прошедших приём")
    for name, reason in skipped:
        typer.echo(typer.style(f"  пропущен {name}: {reason}", fg=typer.colors.YELLOW))

    _markup_loop(issuers, who.strip(), limit)


def _load_issuers(
    path: Path, grouping: dict[str, str] | None = None
) -> tuple[list, list[tuple[str, str]]]:
    """Готовит эмитентов к разметке; всё непринятое называется поимённо.

    **Папка — это организация, а не комплект.** Отчётная дата и вид отчётности
    берутся из содержимого документа, а не из имени файла или папки: правило
    то же, что в РСБУ, где имя выгрузки не значит ничего. Поэтому в папке
    эмитента лежит сколько угодно документов, и все они разбираются.

    **Выбор между документами не делается молча.** Прежде брался первый PDF,
    а всё остальное исчезало без единого сообщения: в каждой папке рядом
    с PDF лежит `report.txt`, и какой из них разобран, по выводу прогона
    было не установить. Теперь разбирается каждый, а одинаковые комплекты
    сводятся по объявленному правилу — предпочитается документ со страницами
    и координатами, потому что у текстовой выгрузки нет ни того ни другого,
    и потеря страницы по ней не обнаруживается. Отвергнутая доставка
    называется вместе с причиной.

    `grouping` — заданные вручную конвенции по ИНН: выход для документа,
    разметка чисел которого не читается ни голосованием, ни арифметикой.
    """
    from finlib.sources.ifrs_inbox import Rejection
    from finlib.sources.ifrs_markup import load_issuer
    from finlib.sources.ifrs_numbers import Grouping

    grouping = grouping or {}
    issuers, skipped = [], []

    stray = [
        item
        for item in sorted(path.iterdir())
        if item.is_file() and item.suffix.lower() in (".pdf", ".txt", ".md")
    ]
    for item in stray:
        skipped.append(
            (item.name, "файл лежит вне папки организации и не разбирается")
        )

    for folder in sorted(p for p in path.iterdir() if p.is_dir()):
        if not re.fullmatch(r"\d{10}|\d{12}", folder.name):
            # Имя папки — это ИНН и ничто иное: оно попадает в ключ комплекта
            # и в отчёт. Суффикс вида «_interim» стал бы частью ИНН, а вид
            # отчётности определяется по содержимому документа, а не по имени.
            skipped.append(
                (
                    folder.name,
                    "имя папки не ИНН: организация определяется папкой, "
                    "а период и вид отчётности — содержимым документа",
                )
            )
            continue
        documents = [
            item
            for item in sorted(folder.iterdir())
            if item.suffix.lower() in (".pdf", ".txt", ".md")
        ]
        if not documents:
            skipped.append((folder.name, "в папке организации нет документов"))
            continue
        chosen_grouping = grouping.get(folder.name)
        accepted: list = []
        for document in documents:
            found = load_issuer(
                document,
                folder.name,
                Grouping(chosen_grouping) if chosen_grouping else None,
            )
            if isinstance(found, Rejection):
                skipped.append((document.name, found.reason))
                continue
            accepted.append(found)
        issuers.extend(_one_per_report(accepted, skipped))
    return issuers, skipped


def _one_per_report(accepted: list, skipped: list[tuple[str, str]]) -> list:
    """Сводит доставки одного комплекта в одну, называя отвергнутые.

    Комплект различается отчётной датой и видом отчётности — тем, что
    прочитано из документа. Две доставки одного комплекта (PDF и текстовая
    выгрузка) — это один комплект, и брать оба значило бы посчитать эмитента
    дважды; брать любой молча — потерять сведения о том, какой разобран.
    """
    by_report: dict[tuple, list] = {}
    for item in accepted:
        key = (item.profile.report_dates[0], item.profile.reporting_kind)
        by_report.setdefault(key, []).append(item)

    found = []
    for group in by_report.values():
        # Документ со страницами старше текстовой выгрузки: у выгрузки нет
        # ни страниц, ни координат, и потеря страницы по ней не видна.
        group.sort(key=lambda item: (item.path.suffix.lower() != ".pdf", item.path.name))
        found.append(group[0])
        for other in group[1:]:
            skipped.append(
                (
                    other.name if hasattr(other, "name") else other.path.name,
                    f"та же отчётность, что «{group[0].path.name}»: разобран "
                    "документ со страницами и координатами",
                )
            )
    return found


def _markup_loop(issuers: list, who: str, limit: int) -> None:
    """Разговор с человеком: список, ввод кода, подсказки, пересчёт итогов."""
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.sources.ifrs_markup import (
        NOT_A_LINE_CODE,
        Decision,
        apply_assignment,
        candidates,
        check_part_of,
        code_is_taken,
        forget,
        known_codes,
        last_confirmation,
        restore,
    )

    catalog = load_ifrs_lines()
    codes = known_codes(catalog)
    by_inn = {item.inn: item for item in issuers}

    # Разметка идёт в несколько присестов: сделанное прежде не показывается
    # повторно, а присвоенные коды участвуют в суммах — без них итоги
    # считались бы незакрытыми, и очередь выстроилась бы по недостаче,
    # которой уже нет.
    already = restore(issuers)
    left = len(candidates(issuers, catalog))
    typer.echo(
        typer.style(
            f"\nРазмечено прежде: {already}. Осталось строк: {left}. "
            f"Эмитентов: {len(issuers)}.",
            bold=True,
        )
    )

    # ИНН, ключ строки и наименование: ключ нужен, чтобы отменить именно эту
    # строку, наименование — чтобы сказать человеку, что отменено.
    history: list[tuple[str, tuple[str, int], str]] = []
    skipped: set[tuple[str, tuple[str, int]]] = set()
    saved = 0

    while True:
        queue = [
            item
            for item in candidates(issuers, catalog)
            if (item.inn, item.key) not in skipped
        ]
        if not queue:
            typer.echo(typer.style("\nОчередь пуста.", bold=True))
            break
        if limit and saved >= limit:
            typer.echo(f"\nРазмечено {limit} строк, как просили. Осталось {len(queue)}.")
            break

        item = queue[0]
        issuer = by_inn[item.inn]
        _show_candidate(item, len(queue))

        raw = typer.prompt(
            "  код или номер подсказки — строка и есть эта позиция, в итог идёт\n"
            "  «д [код]» — строка часть позиции. Если у позиции есть своя строка,\n"
            "     величина уже внутри неё и в итог не идёт, а сумма частей\n"
            "     обязана с ней совпасть; если своей строки нет — части\n"
            "     составляют позицию и в итог идут\n"
            "  «а [коды]» — строка укрупняет несколько позиций; в итог идёт целиком,\n"
            "     на первую из перечисленных\n"
            "  «с [код]» — статья, которой в справочнике нет; в итог идёт\n"
            "  «н» — не статья, «п» — пропустить, «о» — отменить, «в» — выход",
            default="в",
        )
        answer, argument = _parse_command(raw)

        if answer == "в":
            break
        if answer == "п":
            skipped.add((item.inn, item.key))
            typer.echo(f"  пропущено: «{item.source_name or '(без наименования)'}»")
            continue
        if answer == "о":
            # Отмена не ограничена присестом: ошибку замечают и через день,
            # а править журнал руками неудобно и опасно.
            if history:
                inn, key, name = history.pop()
            else:
                found = last_confirmation(issuers)
                if found is None:
                    typer.echo("  отменять нечего")
                    continue
                inn, name, key = found
            forget(by_inn[inn], key, name)
            saved = max(0, saved - 1)
            typer.echo(
                typer.style(f"  отменено: «{name}»", fg=typer.colors.YELLOW)
            )
            continue
        if answer == "н":
            issuer.dismissed[item.key] = Decision.NOT_A_LINE
            try:
                _save_confirmation(
                    issuer, item, NOT_A_LINE_CODE, who, relation="not_a_line"
                )
            except Exception as exc:  # noqa: BLE001 — откат и внятная причина
                issuer.dismissed.pop(item.key, None)
                typer.echo(typer.style(f"  не сохранено: {exc}", fg=typer.colors.RED))
                continue
            history.append((item.inn, item.key, item.source_name))
            saved += 1
            typer.echo(
                f"  сохранено: «{item.source_name or '(без наименования)'}» → не статья"
            )
            continue
        if answer == "д":
            # Детализация: строка вместе с соседними даёт позицию. Гипотеза
            # принимается только тогда, когда сумма сошлась с величиной
            # позиции; не сошлось — разметка сохраняется непроверенной,
            # и об этом сказано прямо.
            code = argument or _ask_code(
                "  код позиции, которую строка детализирует", codes
            )
            if not code or code not in codes:
                if code:
                    typer.echo(
                        typer.style(
                            f"  кода {code} нет в справочнике: строка осталась "
                            "неразмеченной",
                            fg=typer.colors.RED,
                        )
                    )
                continue
            if _refused(issuer, item, code, catalog):
                continue
            # Решение применяется целиком либо не применяется вовсе: сначала
            # запись в журнал, затем влияние на итоги. Прежде величина
            # попадала в итог, а строка оставалась в очереди.
            issuer.parts[item.key] = code
            matched, total = check_part_of(issuer, code, catalog)
            try:
                _save_confirmation(
                    issuer, item, code, who, relation="part_of", confirmed=matched
                )
            except Exception as exc:  # noqa: BLE001 — откат и внятная причина
                issuer.parts.pop(item.key, None)
                typer.echo(typer.style(f"  не сохранено: {exc}", fg=typer.colors.RED))
                continue
            history.append((item.inn, item.key, item.source_name))
            saved += 1
            typer.echo(f"  сохранено: «{item.source_name}» → детализация {code}")
            if matched is True:
                typer.echo(
                    typer.style(
                        f"  сумма детализации {total} сошлась с {code}",
                        fg=typer.colors.GREEN,
                    )
                )
            elif matched is False:
                declared = issuer.extraction.value_of(code, issuer.report_date)
                typer.echo(
                    typer.style(
                        f"  сумма детализации {total} не равна раскрытой величине "
                        f"{code} ({declared}): гипотеза не подтверждена. "
                        "Детализация раскрытой позиции в итог не идёт — она уже "
                        "внутри неё, — поэтому недостача итога не изменится",
                        fg=typer.colors.RED,
                    )
                )
            else:
                typer.echo(
                    "  проверить нечем: сама позиция у эмитента не раскрыта, "
                    "и детализация составит её величину целиком"
                )
            continue
        if answer == "а":
            # Агрегат: строка укрупняет несколько позиций. Перечень объявляется
            # при разметке — без него неизвестно, что именно она покрывает.
            listed = argument or typer.prompt(
                "  коды позиций через запятую, которые строка укрупняет", default=""
            )
            parts = tuple(
                part.strip() for part in listed.replace(";", ",").split(",") if part.strip()
            )
            unknown = [code for code in parts if code not in codes]
            if len(parts) < 2 or unknown:
                typer.echo(
                    typer.style(
                        "  нужны два и более кода из справочника"
                        + (f"; неизвестны: {', '.join(unknown)}" if unknown else "")
                        + ": строка осталась неразмеченной",
                        fg=typer.colors.RED,
                    )
                )
                continue
            if _refused(issuer, item, parts[0], catalog):
                continue
            issuer.aggregates[item.key] = parts
            try:
                _save_confirmation(
                    issuer, item, parts[0], who, relation="aggregate_of", related=parts
                )
            except Exception as exc:  # noqa: BLE001 — откат и внятная причина
                issuer.aggregates.pop(item.key, None)
                typer.echo(typer.style(f"  не сохранено: {exc}", fg=typer.colors.RED))
                continue
            history.append((item.inn, item.key, item.source_name))
            saved += 1
            typer.echo(
                typer.style(
                    f"  сохранено: «{item.source_name}» → агрегат "
                    f"{', '.join(parts)}",
                    fg=typer.colors.GREEN,
                )
            )
            continue
        if answer == "с":
            code = argument or typer.prompt(
                "  код специфической статьи, например ifrs.principal_receivable",
                default="",
            ).strip()
            if not code:
                typer.echo("  код не введён: строка осталась неразмеченной")
                continue
            taken = code_is_taken(code, catalog, item.inn)
            if taken is not None:
                typer.echo(typer.style(f"  код занят: {taken}", fg=typer.colors.RED))
                continue
            elsewhere = _code_seen_elsewhere(code, item.inn)
            if elsewhere:
                typer.echo(
                    typer.style(
                        f"  этот код уже есть у {elsewhere}: статья перестала быть "
                        "специфической — её место в ядре справочника",
                        fg=typer.colors.YELLOW,
                    )
                )
            issuer.dismissed[item.key] = Decision.SPECIFIC
            issuer.specific[item.key] = code
            try:
                _save_confirmation(issuer, item, code, who, relation="specific")
            except Exception as exc:  # noqa: BLE001 — откат и внятная причина
                issuer.dismissed.pop(item.key, None)
                issuer.specific.pop(item.key, None)
                typer.echo(typer.style(f"  не сохранено: {exc}", fg=typer.colors.RED))
                continue
            history.append((item.inn, item.key, item.source_name))
            saved += 1
            # Код заведён и годится дальше: у эмитента бывает вторая строка
            # того же содержания, и её помечают детализацией этого же кода.
            # Прежде такой ввод отвергался словами «кода нет в справочнике».
            codes[code] = None
            typer.echo(
                typer.style(
                    f"  сохранено: «{item.source_name}» → специфическая {code}",
                    fg=typer.colors.GREEN,
                )
            )
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(item.hints):
            answer = item.hints[int(answer) - 1].code
        if answer not in codes:
            typer.echo(
                typer.style(
                    f"  кода «{answer}» нет в справочнике: строка осталась "
                    "неразмеченной",
                    fg=typer.colors.RED,
                )
            )
            continue

        if codes.get(answer) is None:
            # Код подтверждённый, а не ядровый: такую же статью уже размечали
            # у другого эмитента. Строка остаётся специфической — позиции
            # в справочнике у кода нет, — и в итог раздела входит через
            # `extras`, как всякая специфическая.
            issuer.dismissed[item.key] = Decision.SPECIFIC
            issuer.specific[item.key] = answer
            try:
                _save_confirmation(issuer, item, answer, who, relation="specific")
            except Exception as exc:  # noqa: BLE001 — откат и внятная причина
                issuer.dismissed.pop(item.key, None)
                issuer.specific.pop(item.key, None)
                typer.echo(typer.style(f"  не сохранено: {exc}", fg=typer.colors.RED))
                continue
            history.append((item.inn, item.key, item.source_name))
            saved += 1
            elsewhere = _code_seen_elsewhere(answer, item.inn)
            typer.echo(
                typer.style(
                    f"  сохранено: «{item.source_name}» → специфическая {answer}"
                    + (f"; тот же код у {elsewhere}" if elsewhere else ""),
                    fg=typer.colors.GREEN,
                )
            )
            continue

        if _refused(issuer, item, answer, catalog):
            continue
        closed, total = apply_assignment(issuer, item, answer, catalog)
        try:
            _save_confirmation(issuer, item, answer, who)
        except Exception as exc:  # noqa: BLE001 — откат и внятная причина
            issuer.assignments.pop(item.key, None)
            typer.echo(typer.style(f"  не сохранено: {exc}", fg=typer.colors.RED))
            continue
        history.append((item.inn, item.key, item.source_name))
        saved += 1
        typer.echo(f"  сохранено: «{item.source_name}» → {answer}")
        if closed:
            typer.echo(
                typer.style(
                    f"  итог {total} сошёлся — присвоение подтверждено арифметикой",
                    fg=typer.colors.GREEN,
                )
            )
        else:
            typer.echo("  итог пока не сошёлся: не хватает других строк")

    _show_skipped(skipped, issuers, catalog)
    typer.echo(f"\nПрисвоений за присест: {saved}.")


def _refused(issuer, item, code: str, catalog) -> bool:
    """Называет причину, по которой код строке не присваивается; True — отказ.

    Проверяется то же, что у автоматического опознания: форма и раздел
    позиции. Человеку это правило прежде не предъявлялось вовсе.
    """
    from finlib.sources.ifrs_markup import markup_problem

    problem = markup_problem(issuer, item, code, catalog)
    if problem is None:
        return False
    typer.echo(
        typer.style(
            f"  {problem}: строка осталась неразмеченной", fg=typer.colors.RED
        )
    )
    return True


def _show_candidate(item, left: int) -> None:
    """Печатает строку со всем, что нужно для решения."""
    typer.echo("")
    typer.echo(typer.style("─" * 72, dim=True))
    typer.echo(
        f"Осталось {left}. ИНН {item.inn}, форма {item.form}, "
        f"очередь: {item.priority.name}"
    )
    # Соседи печатаются вокруг строки: «Прочие» или «Итого» без контекста
    # не опознать, а раздел виден по тому, что стоит рядом.
    if item.previous_name:
        typer.echo(typer.style(f"    ↑ {item.previous_name}", dim=True))
    shown = item.source_name or "(наименования нет, только величины)"
    typer.echo(typer.style(f"  «{shown}»", bold=True))
    if item.next_name:
        typer.echo(typer.style(f"    ↓ {item.next_name}", dim=True))

    values = ", ".join(str(value) for value in item.values)
    # Мера у каждой формы своя, и называется она по имени: статья баланса
    # соизмеряется с валютой баланса, строка ОПУ — с выручкой, а у потока
    # денежных средств такой меры нет вовсе.
    base = {
        "ifrs.statement_of_financial_position": "доля активов",
        "ifrs.statement_of_profit_or_loss": "доля выручки",
    }.get(item.form, "доля")
    share = f"{item.share_of_assets:.2%}" if item.share_of_assets else "—"
    typer.echo(f"  величины: {values}; {base}: {share}")
    if item.total_code:
        typer.echo(
            f"  входит в незакрытый итог {item.total_code}, недостача {item.total_gap}"
        )
    if item.issuers > 1:
        typer.echo(f"  встречается у {item.issuers} эмитентов")
    if item.hints:
        typer.echo("  подсказки справочника:")
        for number, hint in enumerate(item.hints, start=1):
            typer.echo(f"    {number}) {hint.code} — {hint.name}")


def _show_skipped(skipped: set, issuers: list, catalog) -> None:
    """Пропущенные строки — отдельной очередью в конце присеста."""
    if not skipped:
        return
    from finlib.sources.ifrs_markup import candidates

    typer.echo("")
    typer.echo(typer.style(f"Пропущено строк: {len(skipped)}", bold=True))
    for item in candidates(issuers, catalog):
        if (item.inn, item.key) in skipped:
            typer.echo(f"  {item.inn}  {item.describe()}")


# Однобуквенные команды разметки и их латинские двойники. Раскладку
# переключают не всегда, и «a» вместо «а» — не ошибка человека, а свойство
# клавиатуры: команда обязана приниматься в обоих написаниях.
_COMMAND_ALIASES: dict[str, str] = {
    "a": "а",  # агрегат
    "n": "н",
    "h": "н",  # не статья
    "p": "п",  # пропустить
    "o": "о",  # отменить
    "c": "с",  # специфическая
    "d": "д",
    "g": "д",  # детализация
    "v": "в",
    "b": "в",
    "q": "в",  # выход
}

# Команды, принимающие продолжение в той же строке: «д ifrs.x»,
# «а ifrs.x, ifrs.y». Спрашивать вторым вопросом можно, но заставлять —
# лишний шаг на каждой из двух сотен строк.
_COMMANDS_WITH_ARGUMENT = frozenset({"д", "а", "с"})


def _parse_command(raw: str) -> tuple[str, str]:
    """Разбирает ввод на команду и её продолжение.

    Ввод нормализуется: снимаются пробелы и невидимые знаки, регистр
    не учитывается, латинские двойники приводятся к кириллице. Прежде
    «a» отвергалось как неизвестный код, а «а ifrs.x, ifrs.y» одной строкой
    целиком принималось за код.
    """
    cleaned = raw.replace(" ", " ").strip().casefold()
    if not cleaned:
        return "в", ""
    head, _, tail = cleaned.partition(" ")
    command = _COMMAND_ALIASES.get(head, head)
    if len(head) == 1 and command in _COMMANDS_WITH_ARGUMENT:
        return command, tail.strip()
    if len(cleaned) == 1:
        return _COMMAND_ALIASES.get(cleaned, cleaned), ""
    # Не команда: это код позиции либо номер подсказки, и регистр кодов
    # значения не имеет — они строчные по правилу справочника.
    return cleaned, ""


def _code_seen_elsewhere(code: str, inn: str) -> str:
    """У каких ещё эмитентов встречается этот код; пусто — ни у кого."""
    from finlib.db import fetch_all

    try:
        rows = fetch_all(
            "SELECT DISTINCT inn FROM ifrs_line_confirmation WHERE code = %(code)s",
            {"code": code},
        )
    except Exception:  # noqa: BLE001 — разметка без базы тоже работает
        return ""
    others = sorted(row["inn"] for row in rows if row["inn"] != inn)
    return ", ".join(others)


def _ask_code(question: str, codes: dict) -> str | None:
    """Спрашивает код позиции и проверяет, что он есть в справочнике."""
    answer = typer.prompt(question, default="").strip()
    if not answer:
        return None
    if answer not in codes:
        typer.echo(typer.style(f"  кода {answer} нет в справочнике", fg=typer.colors.RED))
        return None
    return answer


def _save_confirmation(
    issuer,
    candidate,
    code: str,
    who: str,
    *,
    relation: str = "exact",
    related: tuple[str, ...] | None = None,
    confirmed: bool | None = None,
) -> None:
    """Пишет разметку в ifrs_line_confirmation.

    Вид разметки хранится рядом с кодом: детализация, агрегат и точное
    соответствие проверяются по-разному, и по одному коду их не различить.
    Подтверждение арифметикой пишется третьим состоянием — «не проверялось»
    не то же, что «не сошлось».
    """
    from finlib.db import connection, execute

    with connection() as conn:
        execute(
            "INSERT INTO organization (inn) VALUES (%(inn)s) ON CONFLICT DO NOTHING",
            {"inn": issuer.inn},
            conn=conn,
        )
        execute(
            "INSERT INTO ifrs_line_confirmation (code, inn, report_date, source_name, "
            "form_code, value, share_of_assets, confirmed_by, relation, related_codes, "
            "arithmetic_confirmed, row_index) VALUES (%(code)s, %(inn)s, %(date)s, "
            "%(name)s, %(form)s, %(value)s, %(share)s, %(who)s, %(relation)s, "
            "%(related)s, %(confirmed)s, %(index)s) "
            "ON CONFLICT (code, inn, report_date, source_name) DO UPDATE SET "
            "value = EXCLUDED.value, share_of_assets = EXCLUDED.share_of_assets, "
            "confirmed_by = EXCLUDED.confirmed_by, relation = EXCLUDED.relation, "
            "related_codes = EXCLUDED.related_codes, row_index = EXCLUDED.row_index, "
            "arithmetic_confirmed = EXCLUDED.arithmetic_confirmed, confirmed_at = now()",
            {
                "code": code,
                "inn": issuer.inn,
                "date": issuer.report_date,
                "name": candidate.source_name,
                "form": candidate.form,
                "value": candidate.amount,
                "share": candidate.share_of_assets or 0,
                "who": who,
                "relation": relation,
                "related": list(related) if related else None,
                "confirmed": confirmed,
                "index": candidate.index,
            },
            conn=conn,
        )


def main() -> int:
    """Запуск приложения."""
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
