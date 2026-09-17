"""Точка входа: команды analyze, show, quality, reprocess.

Вывод рассчитан на человека за терминалом: на каждом этапе видно, что
происходит, а остановка называет этап и причину. Молчаливых неудач нет —
цикл, прерванный на контролях качества, обязан сказать, на чём именно.
"""

import logging
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

    issuers, skipped = _load_issuers(path)
    if not issuers:
        _fail(f"в каталоге {path} нет документов, прошедших приём")
    for name, reason in skipped:
        typer.echo(typer.style(f"  пропущен {name}: {reason}", fg=typer.colors.YELLOW))

    _markup_loop(issuers, who.strip(), limit)


def _load_issuers(path: Path) -> tuple[list, list[tuple[str, str]]]:
    """Готовит эмитентов к разметке; непринятые документы называются отдельно."""
    from finlib.sources.ifrs_inbox import Rejection
    from finlib.sources.ifrs_markup import load_issuer

    issuers, skipped = [], []
    for folder in sorted(p for p in path.iterdir() if p.is_dir()):
        documents = [
            item
            for item in sorted(folder.iterdir())
            if item.suffix.lower() in (".pdf", ".txt", ".md")
        ]
        chosen = next(
            (item for item in documents if item.suffix.lower() == ".pdf"),
            documents[0] if documents else None,
        )
        if chosen is None:
            continue
        found = load_issuer(chosen, folder.name)
        if isinstance(found, Rejection):
            skipped.append((chosen.name, found.reason))
            continue
        issuers.append(found)
    return issuers, skipped


def _markup_loop(issuers: list, who: str, limit: int) -> None:
    """Разговор с человеком: список, ввод кода, подсказки, пересчёт итогов."""
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.sources.ifrs_markup import (
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

    history: list[tuple[str, str]] = []
    skipped: set[tuple[str, str]] = set()
    saved = 0

    while True:
        queue = [
            item
            for item in candidates(issuers, catalog)
            if (item.inn, item.source_name) not in skipped
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

        answer = typer.prompt(
            "  код, номер подсказки, «д» — детализация, «а» — агрегат, "
            "«с» — специфическая, «н» — не статья, «п» — пропустить, "
            "«о» — отменить, «в» — выход",
            default="в",
        ).strip()

        if answer in ("в", "q", ""):
            break
        if answer == "п":
            skipped.add((item.inn, item.source_name))
            continue
        if answer == "о":
            # Отмена не ограничена присестом: ошибку замечают и через день,
            # а править журнал руками неудобно и опасно.
            if history:
                inn, name = history.pop()
                code = issuer.assignments.get(name, "")
            else:
                found = last_confirmation(issuers)
                if found is None:
                    typer.echo("  отменять нечего")
                    continue
                inn, name, code = found
            forget(by_inn[inn], name)
            saved = max(0, saved - 1)
            typer.echo(
                typer.style(
                    f"  отменено: «{name}» ({code})", fg=typer.colors.YELLOW
                )
            )
            continue
        if answer == "н":
            issuer.dismissed[item.source_name] = Decision.NOT_A_LINE
            continue
        if answer == "д":
            # Детализация: строка вместе с соседними даёт позицию. Гипотеза
            # принимается только тогда, когда сумма сошлась с величиной
            # позиции; не сошлось — разметка сохраняется непроверенной,
            # и об этом сказано прямо.
            code = _ask_code("  код позиции, которую строка детализирует", codes)
            if code is None:
                continue
            issuer.parts[item.source_name] = code
            matched, total = check_part_of(issuer, code, catalog)
            _save_confirmation(
                issuer, item, code, who, relation="part_of", confirmed=matched
            )
            history.append((item.inn, item.source_name))
            saved += 1
            if matched is True:
                typer.echo(
                    typer.style(
                        f"  сумма детализации {total} сошлась с {code}",
                        fg=typer.colors.GREEN,
                    )
                )
            elif matched is False:
                typer.echo(
                    typer.style(
                        f"  сумма детализации {total} не равна величине {code}: "
                        "гипотеза не подтверждена",
                        fg=typer.colors.RED,
                    )
                )
            else:
                typer.echo("  проверить нечем: сама позиция у эмитента не раскрыта")
            continue
        if answer == "а":
            # Агрегат: строка укрупняет несколько позиций. Перечень объявляется
            # при разметке — без него неизвестно, что именно она покрывает.
            listed = typer.prompt(
                "  коды позиций через запятую, которые строка укрупняет", default=""
            ).strip()
            parts = tuple(item.strip() for item in listed.split(",") if item.strip())
            unknown = [code for code in parts if code not in codes]
            if len(parts) < 2 or unknown:
                typer.echo(
                    typer.style(
                        "  нужны два и более кода из справочника"
                        + (f"; неизвестны: {', '.join(unknown)}" if unknown else ""),
                        fg=typer.colors.RED,
                    )
                )
                continue
            issuer.aggregates[item.source_name] = parts
            _save_confirmation(
                issuer,
                item,
                parts[0],
                who,
                relation="aggregate_of",
                related=parts,
            )
            history.append((item.inn, item.source_name))
            saved += 1
            typer.echo(
                typer.style(
                    f"  сохранено как агрегат {len(parts)} позиций",
                    fg=typer.colors.GREEN,
                )
            )
            continue
        if answer == "с":
            code = typer.prompt(
                "  код специфической статьи, например ifrs.principal_receivable",
                default="",
            ).strip()
            if not code:
                continue
            taken = code_is_taken(code, catalog)
            if taken is not None:
                typer.echo(typer.style(f"  код занят: {taken}", fg=typer.colors.RED))
                continue
            issuer.dismissed[item.source_name] = Decision.SPECIFIC
            _save_confirmation(issuer, item, code, who)
            history.append((item.inn, item.source_name))
            saved += 1
            typer.echo(typer.style(f"  сохранено как {code}", fg=typer.colors.GREEN))
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(item.hints):
            answer = item.hints[int(answer) - 1].code
        if answer not in codes:
            typer.echo(
                typer.style(f"  кода {answer} нет в справочнике", fg=typer.colors.RED)
            )
            continue

        closed, total = apply_assignment(issuer, item, answer, catalog)
        _save_confirmation(issuer, item, answer, who)
        history.append((item.inn, item.source_name))
        saved += 1
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
    share = f"{item.share_of_assets:.2%}" if item.share_of_assets else "—"
    typer.echo(f"  величины: {values}; доля активов: {share}")
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
        if (item.inn, item.source_name) in skipped:
            typer.echo(f"  {item.inn}  {item.describe()}")


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
            "arithmetic_confirmed) VALUES (%(code)s, %(inn)s, %(date)s, %(name)s, "
            "%(form)s, %(value)s, %(share)s, %(who)s, %(relation)s, %(related)s, "
            "%(confirmed)s) "
            "ON CONFLICT (code, inn, report_date, source_name) DO UPDATE SET "
            "value = EXCLUDED.value, share_of_assets = EXCLUDED.share_of_assets, "
            "confirmed_by = EXCLUDED.confirmed_by, relation = EXCLUDED.relation, "
            "related_codes = EXCLUDED.related_codes, "
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
            },
            conn=conn,
        )


def main() -> int:
    """Запуск приложения."""
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
