"""Точка входа: команды analyze, show, quality, reprocess.

Вывод рассчитан на человека за терминалом: на каждом этапе видно, что
происходит, а остановка называет этап и причину. Молчаливых неудач нет —
цикл, прерванный на контролях качества, обязан сказать, на чём именно.
"""

import logging
import re
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from finlib.db import fetch_all
from finlib.llm.service import PromptScheme
from finlib.metrics.definitions import Unit
from finlib.metrics.display import format_metric
from finlib.normalize.facts import unit_name_of
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

# Стандарт комплекта берётся вместе с записью и печатается: организация
# может сдавать отчётность по обоим, и запись журнала сама о своём стандарте
# не говорит. Молча смешанный перечень выглядит как перечень одного ряда.
_CHECKS = """
SELECT d.check_code, d.severity, d.status, d.report_date, d.line_code, d.message,
       coalesce(s.standard, '—') AS standard
FROM dq_log d LEFT JOIN src_file s ON s.id = d.src_file_id
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


def _echo_caveat(inn: str, report_date: date) -> None:
    """Печатает вид оговорки аудитора: установленный и предложенный машиной.

    Вид решает, понижается ли уверенность и возникает ли эскалация, поэтому
    подтверждающий обязан видеть, что записалось: предложение машины,
    принятое молча, было бы решением методики, сделанным приметой в прозе.
    """
    from finlib.db import connection, fetch_one
    from finlib.normalize.ifrs_audit import load_audit_policy
    from finlib.sources.ifrs_audit import audit_from_meta

    with connection() as conn:
        row = fetch_one(
            # Предпочтение первоисточника: за год комплектов два — документ
            # и доставка агрегатора, — а сведения заключения есть только
            # у документа.
            "SELECT meta FROM src_file WHERE inn = %(inn)s AND standard = 'ifrs' "
            "AND report_year = %(year)s AND is_actual "
            "ORDER BY source_rank(source), id DESC LIMIT 1",
            {"inn": inn, "year": report_date.year},
            conn=conn,
        )
    audit = audit_from_meta((row or {}).get("meta"))
    if audit is None or not audit.modified:
        return
    policy = load_audit_policy()
    kind = policy.caveat_kind(audit.effective_caveat_kind(policy))
    proposed = policy.caveat_kind(audit.proposed_caveat_kind(policy))
    typer.echo(
        f"  вид оговорки: {kind.name if kind is not None else 'не определён'}"
        + (f" (подтвердил {audit.caveat_confirmed_by})" if audit.caveat_confirmed_by else "")
    )
    if not audit.caveat_kind:
        typer.echo(
            "    предложение машины по приметам основания: "
            + (proposed.name if proposed is not None else "приметы вида не называют")
        )
        typer.echo(
            "    следствия вида не применялись: уверенность не понижена, "
            "эскалации нет. Установить: --caveat about_values | about_disclosure"
        )


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


def _render(value: Decimal | None, unit: Unit, scale: int, unit_name: str) -> str:
    """Значение показателя в единице и разрядности методики.

    `unit_name` — денежная единица комплекта. Довод обязательный: терминал
    печатает те же величины, что документ, и умолчание «тыс. руб.» подписало
    бы тысячами миллионы.
    """
    if value is None:
        return "—"
    return format_metric(value, unit, scale, money=unit_name)


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
    # Единица комплекта отчётного периода: величины прежних периодов
    # приведены к ней же — комплект один.
    unit_name = unit_name_of(inn, ordered[0], None, Standard.RSBU.value)

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
                _render(
                    item["value"], metric.unit, catalog.scale_for(code), unit_name
                )
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
        line = (
            f"  {row['status']:<8} {row['standard']:<5} {row['check_code']:<24} "
            f"{period:<12} {row['message'][:64]}"
        )
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


@app.command("ifrs-confirm")
def ifrs_confirm_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    report_date: Annotated[
        str,
        typer.Option(
            "--report-date",
            help="Отчётная дата комплекта, ГГГГ-ММ-ДД: у эмитента их несколько, "
            "и подтверждается один",
        ),
    ],
    who: Annotated[
        str, typer.Option("--who", help="Кто подтверждает: попадёт в журнал")
    ],
    accept: Annotated[
        list[str],
        typer.Option(
            "--accept",
            help="Принять основание экрана сверки: КОД=причина. Можно повторять. "
            "Карантин снимается только по названным основаниям",
        ),
    ] = [],  # noqa: B006 — typer требует list по умолчанию
    caveat: Annotated[
        str,
        typer.Option(
            "--caveat",
            help="Вид оговорки аудитора: о величинах отчётности или о полноте "
            "раскрытий. Следствия у них разные, и устанавливает вид человек",
        ),
    ] = "",
    path: Annotated[
        Path, typer.Option("--path", help="Каталог с документами МСФО по ИНН")
    ] = Path("data/raw/ifrs"),
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Подтверждает комплект МСФО человеком и записывает его заново.

    **Вид оговорки аудитора устанавливает человек** — `--caveat about_values`
    либо `--caveat about_disclosure`. Оговорка о величинах ставит под вопрос
    числа, из которых считаются показатели, и понижает уверенность; оговорка
    о полноте раскрытий величин не затрагивает. До решения человека действует
    вид «не установлен»: уровень сигнала «требует внимания», уверенность
    не понижается, эскалации не возникает, а приметы основания печатаются
    предложением машины.

    **Разметка и подтверждение — разные действия.** Присест разметки пишет
    присвоенные коды в журнал подтверждений, но комплект остаётся в карантине:
    решение «я посмотрел это извлечение и отвечаю за него» принимается
    о комплекте целиком, а не о строке.

    **Основание принимается поимённо и с причиной.** Прежде подтверждение
    снимало карантин целиком, и вместе с неопознанными строками молча
    принимались несошедшийся итог и неполный вид отчётности: провал
    блокирующего контроля проходил побочно, без решения и без причины.
    Теперь каждое основание называется своим кодом — `--accept
    check_failed="итог не сходится на нераскрытые слагаемые"`, — причина идёт
    в журнал и в «Ограничения анализа» заключения, а основание, не названное
    человеком, оставляет комплект в карантине.

    Без `--accept` команда ничего не принимает: она перезагружает комплект
    и печатает, какие основания остались и какими кодами они называются.
    """
    _setup_logging(verbose)
    _check_inn(inn)
    try:
        wanted = date.fromisoformat(report_date.strip())
    except ValueError:
        _fail(f"--report-date принимает дату ГГГГ-ММ-ДД, получено «{report_date}»")
    if not who.strip():
        _fail("укажите --who: подтверждение без автора в журнале бесполезно")

    from finlib.pipeline import accept_ifrs_document
    from finlib.sources.ifrs_inbox import text_of
    from finlib.sources.ifrs_review import ReviewReason

    known = {item.value for item in ReviewReason}
    taken: dict[str, str] = {}
    for item in accept:
        code, _, reason = item.partition("=")
        if code.strip() not in known:
            _fail(
                f"основание «{code.strip()}» неизвестно; допустимы: "
                + ", ".join(sorted(known))
            )
        if not reason.strip():
            _fail(
                f"основание {code.strip()} принято без причины: причина идёт "
                "в журнал и в документ, и без неё решение проверить нечем"
            )
        taken[code.strip()] = reason.strip()

    from finlib.normalize.ifrs_audit import load_audit_policy

    kinds = {
        item.code
        for item in load_audit_policy().caveat_kinds
        if not item.applies_until_confirmed
    }
    if caveat.strip() and caveat.strip() not in kinds:
        _fail(
            f"вид оговорки «{caveat.strip()}» неизвестен; допустимы: "
            + ", ".join(sorted(kinds))
        )

    # Читается только папка этого эмитента: подтверждается один комплект,
    # и разбирать ради этого весь каталог незачем.
    issuers, skipped = _load_issuers(path, only=frozenset({inn}))
    for name, reason in skipped:
        typer.echo(typer.style(f"  пропущен {name}: {reason}", fg=typer.colors.YELLOW))
    mine = [
        item for item in issuers if item.inn == inn and item.report_date == wanted
    ]
    if not mine:
        dates = sorted({str(item.report_date) for item in issuers if item.inn == inn})
        _fail(
            f"комплекта {inn} за {wanted} в каталоге нет"
            + (f"; есть: {', '.join(dates)}" if dates else "")
        )

    issuer = mine[0]
    document = text_of(issuer.path)
    typer.echo(typer.style(f"\nПодтверждение комплекта {inn} за {wanted}", bold=True))
    typer.echo(f"  документ: {issuer.path}")
    intake = accept_ifrs_document(
        document.text,
        _echo_stage,
        inn=inn,
        raw_path=str(issuer.path),
        confirmed_by=who.strip(),
        accepted=taken,
        document=document,
        caveat_kind=caveat.strip() or None,
    )
    if not intake.accepted:
        _fail(f"документ отклонён приёмом [{intake.check_code}]: {intake.reason}")
    loaded = intake.loaded
    if loaded is None:  # pragma: no cover — ИНН назван, запись обязана состояться
        _fail("комплект не записан")
    _echo_caveat(inn, wanted)
    grounds = [item.value for item in intake.review.reasons]
    for code in grounds:
        mark = "принято" if code in taken else "НЕ ПРИНЯТО"
        colour = typer.colors.GREEN if code in taken else typer.colors.YELLOW
        typer.echo(
            typer.style(f"  {code}: {mark}", fg=colour)
            + (f" — {taken[code]}" if code in taken else "")
        )
    if loaded.quarantined:
        left = sorted(set(grounds) - set(taken))
        typer.echo(
            typer.style(
                "\nКарантин не снят. Не принятые основания: "
                + (", ".join(left) if left else "строки без кода в разметке"),
                fg=typer.colors.YELLOW,
                bold=True,
            )
        )
        typer.echo(
            "  принять каждое поимённо: --accept КОД=«причина». Причина идёт "
            "в журнал и в «Ограничения анализа» заключения."
        )
        raise typer.Exit(code=1)
    typer.echo(
        typer.style(
            "\nКарантин снят по названным основаниям, комплект идёт в расчёт",
            fg=typer.colors.GREEN,
        )
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
    priority: Annotated[
        list[str],
        typer.Option(
            "--priority",
            help="Показывать только эти приоритеты: IN_CBONDS_OTHER, "
            "BREAKS_TOTAL, MATERIAL, OTHER. Можно повторять",
        ),
    ] = [],  # noqa: B006 — typer требует list по умолчанию
    # Имя переменной не `inn`: ниже так называется ИНН из пары `--grouping`,
    # и второе значение под тем же именем уже стоило разбора — отбор получал
    # строку вместо перечня и распадался на отдельные цифры.
    only_inn: Annotated[
        list[str],
        typer.Option(
            "--inn",
            help="Размечать только эти организации. Можно повторять. Каталог "
            "остаётся общим: разметка одного эмитента опирается на коды, "
            "подтверждённые у других",
        ),
    ] = [],  # noqa: B006 — typer требует list по умолчанию
    report_date: Annotated[
        str,
        typer.Option(
            "--report-date",
            help="Размечать только комплект с этой отчётной датой, ГГГГ-ММ-ДД. "
            "У эмитента комплектов несколько, и разметка принадлежит комплекту",
        ),
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Разметка неопознанных строк МСФО: присвоение кодов позициям модели.

    Строки показываются не по частоте, а по влиянию на арифметику: сначала
    те, без которых не сходится итог раздела. Это даёт двойную проверку —
    присвоил код, итог сошёлся, значит опознал верно.

    `--priority` сужает очередь до названных приоритетов. Нужен, когда время
    ограничено: «прочие Cbonds» и статьи сверх порога существенности стоят
    в очереди третьими и четвёртыми по счёту строк, и без отбора до них
    за присест не дойти.
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

    if only_inn:
        # Отбор по организации: пройти четыреста строк, чтобы добраться
        # до одной, незачем. Отбор, молча оставивший ноль комплектов,
        # неотличим от исчерпанной разметки, поэтому он называет, что есть.
        chosen = {item.strip() for item in only_inn if item.strip()}
        listed = sorted({item.inn for item in issuers})
        issuers = [item for item in issuers if item.inn in chosen]
        if not issuers:
            _fail(
                "ни один комплект не подошёл под --inn "
                f"{', '.join(sorted(chosen))}; в каталоге есть: {', '.join(listed)}"
            )

    if report_date.strip():
        # Отбор по комплекту, а не по эмитенту: у ФосАгро их два, и строка,
        # которая держит годовой комплект, стоит в очереди последней
        # из семнадцати.
        try:
            wanted_date = date.fromisoformat(report_date.strip())
        except ValueError:
            _fail(f"--report-date принимает дату ГГГГ-ММ-ДД, получено «{report_date}»")
        dates = sorted({str(item.report_date) for item in issuers})
        issuers = [item for item in issuers if item.report_date == wanted_date]
        if not issuers:
            _fail(
                f"комплекта с отчётной датой {wanted_date} нет; "
                f"есть: {', '.join(dates)}"
            )

    from finlib.sources.ifrs_markup import Priority

    wanted: set[Priority] = set()
    for item in priority:
        try:
            wanted.add(Priority[item.strip().upper()])
        except KeyError:
            listed = ", ".join(sorted(member.name for member in Priority))
            _fail(f"приоритет «{item}» неизвестен; допустимы: {listed}")

    _markup_loop(issuers, who.strip(), limit, wanted)


def _load_issuers(
    path: Path,
    grouping: dict[str, str] | None = None,
    only: frozenset[str] | None = None,
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
        # **Отбор до разбора, а не после.** Подтверждение одного комплекта
        # перечитывало весь каталог — семнадцать документов вместо одного,
        # и чужие отказы печатались в вывод команды об этом эмитенте.
        if only is not None and folder.name not in only:
            continue
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


def _markup_loop(
    issuers: list, who: str, limit: int, priority: set | None = None
) -> None:
    """Разговор с человеком: список, ввод кода, подсказки, пересчёт итогов.

    `priority` сужает очередь; пустое множество означает всю очередь, а не
    пустую — отбор, молча оставляющий ноль строк, неотличим от исчерпанной
    разметки.
    """
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.sources.ifrs_confirmed import refresh_match_keys
    from finlib.sources.ifrs_markup import (
        NOT_A_LINE_CODE,
        Decision,
        apply_assignment,
        candidates,
        check_part_of,
        code_is_taken,
        declared_value,
        forget,
        known_codes,
        last_confirmation,
        restore,
    )

    catalog = load_ifrs_lines()
    codes = known_codes(catalog)
    # Комплект, а не эмитент: ключом был один ИНН, и у эмитента с двумя
    # комплектами решение по строке одного применялось к другому — вместе
    # с его отчётной датой в журнале. У Сегежи так три присвоения
    # промежуточного комплекта легли на годовой, а одно затёрло запись
    # годового по ключу (код, ИНН, дата, наименование).
    by_report = {(item.inn, item.report_date): item for item in issuers}

    # Разметка идёт в несколько присестов: сделанное прежде не показывается
    # повторно, а присвоенные коды участвуют в суммах — без них итоги
    # считались бы незакрытыми, и очередь выстроилась бы по недостаче,
    # которой уже нет.
    # Ключ сопоставления — величина производная, и он обязан соответствовать
    # нынешнему разбору, а не тому, который действовал в день подтверждения:
    # наименование в журнале остаётся записью о том, что было, вместе с мусором,
    # который разбор тогда прочитал. Пересчёт идёт перед восстановлением —
    # иначе оно искало бы прежним ключом.
    refreshed = sum(
        refresh_match_keys(inn) for inn in sorted({item.inn for item in issuers})
    )
    if refreshed:
        typer.echo(f"  ключей сопоставления пересчитано: {refreshed}")

    already = restore(issuers)
    _show_lost_markup(issuers)
    whole = candidates(issuers, catalog)
    left = [item for item in whole if not priority or item.priority in priority]
    chosen = (
        ""
        if not priority
        else " по приоритетам " + ", ".join(sorted(item.name for item in priority))
    )
    typer.echo(
        typer.style(
            f"\nРазмечено прежде: {already}. Осталось строк: {len(left)} "
            f"из {len(whole)}{chosen}. Эмитентов: {len(issuers)}.",
            bold=True,
        )
    )

    # Комплект, ключ строки и наименование: ключ нужен, чтобы отменить именно
    # эту строку, наименование — чтобы сказать человеку, что отменено.
    # Комплект — потому что «форма и место в ней» у другого комплекта того же
    # эмитента означают другую строку.
    history: list[tuple[tuple[str, date | None], tuple[str, int], str]] = []
    skipped: set[tuple[tuple[str, date | None], tuple[str, int]]] = set()
    saved = 0

    while True:
        queue = [
            item
            for item in candidates(issuers, catalog)
            if (item.issuer_key, item.key) not in skipped
            and (not priority or item.priority in priority)
        ]
        if not queue:
            typer.echo(typer.style("\nОчередь пуста.", bold=True))
            break
        if limit and saved >= limit:
            typer.echo(f"\nРазмечено {limit} строк, как просили. Осталось {len(queue)}.")
            break

        item = queue[0]
        issuer = by_report[item.issuer_key]
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
            skipped.add((item.issuer_key, item.key))
            typer.echo(f"  пропущено: «{item.source_name or '(без наименования)'}»")
            continue
        if answer == "о":
            # Отмена не ограничена присестом: ошибку замечают и через день,
            # а править журнал руками неудобно и опасно.
            if history:
                issuer_key, key, name = history.pop()
                undone = by_report[issuer_key]
            else:
                found = last_confirmation(issuers)
                if found is None:
                    typer.echo("  отменять нечего")
                    continue
                undone, name, key = found
            forget(undone, key, name)
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
            history.append((item.issuer_key, item.key, item.source_name))
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
            history.append((item.issuer_key, item.key, item.source_name))
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
                declared = declared_value(issuer, code)
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
            history.append((item.issuer_key, item.key, item.source_name))
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
            taken = code_is_taken(
                code, catalog, item.inn, source_name=item.source_name
            )
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
            history.append((item.issuer_key, item.key, item.source_name))
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
            history.append((item.issuer_key, item.key, item.source_name))
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
        history.append((item.issuer_key, item.key, item.source_name))
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
    # Отчётная дата печатается наравне с ИНН: у эмитента комплектов сколько
    # угодно, и «Прибыль за период» годового и промежуточного — разные строки
    # с разными величинами. Без даты человек не знает, что размечает.
    typer.echo(
        f"Осталось {left}. ИНН {item.inn}, комплект {item.report_date}, "
        f"форма {item.form}, очередь: {item.priority.name}"
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
    # Мера у каждой формы своя, и база её называется: статья баланса
    # соизмеряется с валютой баланса, строка ОПУ — с выручкой, а у потока
    # денежных средств базы нет вовсе, и мера не применяется. База берётся
    # из методики, а не перечисляется здесь: перечень в коде разошёлся бы
    # с тем, по которому решает экран сверки.
    if item.materiality_share is None or not item.materiality_base:
        typer.echo(f"  величины: {values}; мера существенности не применяется")
    else:
        typer.echo(
            f"  величины: {values}; доля от {item.materiality_base}: "
            f"{item.materiality_share:.2%}"
        )
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


def _show_lost_markup(issuers: list) -> None:
    """Называет разметку, которая не применилась, — прежде она молчала.

    Строка, чьё притязание отклонено правилом формы или раздела, возвращается
    в очередь, и человек размечает её заново, не зная, что уже размечал.
    У ФосАгро «права пользования» так получили три кода за три присеста:
    первый, балансовый в отчёте о движении денежных средств, отклонялся
    и держал строку в очереди, а два верных ложились рядом с ним.
    """
    from finlib.sources.ifrs_markup import review_saved

    try:
        saved = review_saved(issuers)
    except Exception as failure:  # noqa: BLE001 — разметка работает и без базы
        typer.echo(
            typer.style(f"  журнал подтверждений недоступен: {failure}", fg=typer.colors.YELLOW)
        )
        return
    lost = [item for item in saved if item.lost]
    if not lost:
        return
    typer.echo(
        typer.style(
            f"\nНе применилось присвоений: {len(lost)} из {len(saved)}. "
            "Эти строки вернулись в очередь, и размечать их заново незачем, "
            "пока не устранена причина:",
            fg=typer.colors.YELLOW,
            bold=True,
        )
    )
    for item in lost:
        typer.echo(typer.style(f"  {item.describe()}", fg=typer.colors.YELLOW))


def _show_skipped(skipped: set, issuers: list, catalog) -> None:
    """Пропущенные строки — отдельной очередью в конце присеста."""
    if not skipped:
        return
    from finlib.sources.ifrs_markup import candidates

    typer.echo("")
    typer.echo(typer.style(f"Пропущено строк: {len(skipped)}", bold=True))
    for item in candidates(issuers, catalog):
        if (item.issuer_key, item.key) in skipped:
            typer.echo(f"  {item.inn} {item.report_date}  {item.describe()}")


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
    from finlib.sources.ifrs_confirmed import match_key

    with connection() as conn:
        execute(
            "INSERT INTO organization (inn) VALUES (%(inn)s) ON CONFLICT DO NOTHING",
            {"inn": issuer.inn},
            conn=conn,
        )
        execute(
            "INSERT INTO ifrs_line_confirmation (code, inn, report_date, source_name, "
            "match_key, form_code, value, materiality_share, confirmed_by, relation, "
            "related_codes, arithmetic_confirmed, row_index) VALUES (%(code)s, "
            "%(inn)s, %(date)s, %(name)s, %(key)s, %(form)s, %(value)s, %(share)s, "
            "%(who)s, %(relation)s, %(related)s, %(confirmed)s, %(index)s) "
            # Ключ конфликта — строка комплекта, а не пара «строка, код»:
            # исправление обязано **заменить** прежнее решение. Прежде оно
            # ложилось рядом, восстановление применяло оба, и отклонённое
            # правилом формы возвращало строку в очередь — человек размечал
            # её присест за присестом.
            "ON CONFLICT (inn, report_date, form_code, row_index) DO UPDATE SET "
            "code = EXCLUDED.code, source_name = EXCLUDED.source_name, "
            "match_key = EXCLUDED.match_key, "
            "value = EXCLUDED.value, materiality_share = EXCLUDED.materiality_share, "
            "confirmed_by = EXCLUDED.confirmed_by, relation = EXCLUDED.relation, "
            "related_codes = EXCLUDED.related_codes, "
            "arithmetic_confirmed = EXCLUDED.arithmetic_confirmed, confirmed_at = now()",
            {
                "code": code,
                "inn": issuer.inn,
                "date": issuer.report_date,
                "name": candidate.source_name,
                # Дословная запись и ключ поиска — разные графы: наименование
                # не правится никогда, ключ пересчитывается текущим разбором.
                "key": match_key(candidate.source_name),
                "form": candidate.form,
                "value": candidate.amount,
                # Мера, которой нет, пишется как NULL, а не как ноль: ноль
                # означал бы «несущественна».
                "share": candidate.materiality_share,
                "who": who,
                "relation": relation,
                "related": list(related) if related else None,
                "confirmed": confirmed,
                "index": candidate.index,
            },
            conn=conn,
        )


@app.command("ifrs-reapply")
def ifrs_reapply_command(
    path: Annotated[
        Path, typer.Option("--path", help="Каталог с документами МСФО по ИНН")
    ] = Path("data/raw/ifrs"),
    only_inn: Annotated[
        list[str],
        typer.Option(
            "--inn",
            help="Только эти организации. Можно повторять; без отбора — все",
        ),
    ] = [],  # noqa: B006 — typer требует list по умолчанию
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Применяет заново решения человека после правок разбора — без вопросов.

    **Изменился ключ, а не решение.** Наименование строки хранится дословно,
    вместе с мусором того разбора, который его прочитал, а ключ поиска
    вычисляется разбором нынешним. После правки разбора ключ в журнале
    устаревает, подтверждение перестаёт находиться, и строка возвращается
    в очередь — хотя человек о ней уже сказал, чем она является. Пересчитать
    ключ и записать комплект заново — не новое суждение, и спрашивать
    человека тут не о чем.

    **Что заново не применяется, называется поимённо.** Притязание строки
    чужой формы или чужого раздела отклоняется правилом, и отклонение
    остаётся: решение человека здесь не сильнее справочника. Такие строки
    и строки, которых разбор больше не даёт, перечисляются — это работа,
    которую придётся делать глазами.
    """
    _setup_logging(verbose)
    from finlib.pipeline import accept_ifrs_document
    from finlib.sources.ifrs_confirmed import refresh_match_keys
    from finlib.sources.ifrs_inbox import text_of
    from finlib.sources.ifrs_markup import review_saved

    chosen = {item.strip() for item in only_inn if item.strip()}
    issuers, skipped = _load_issuers(path, only=frozenset(chosen) or None)
    if not issuers:
        _fail(f"в каталоге {path} нет документов, прошедших приём")
    for name, reason in skipped:
        typer.echo(typer.style(f"  пропущен {name}: {reason}", fg=typer.colors.YELLOW))

    refreshed = sum(refresh_match_keys(inn) for inn in sorted({i.inn for i in issuers}))
    typer.echo(f"Ключей сопоставления пересчитано: {refreshed}")

    saved = review_saved(issuers)
    lost = [item for item in saved if item.lost]
    # Судьбы называются все, а не только потери: «восстановлено» и «опознано
    # справочником» — разные исходы, и второй означает, что работу человека
    # перенял справочник, а не что она пропала.
    fates: Counter[str] = Counter(item.fate for item in saved)
    typer.echo(
        f"Подтверждений у этих комплектов: {len(saved)}, "
        f"не применяется: {len(lost)}"
    )
    for fate, count in fates.most_common():
        typer.echo(f"  {fate}: {count}")
    # **Сила подтверждений называется числом.** Признак сходимости писался
    # в журнал с первого дня разметки и не читался ничем: подтверждение
    # при сошедшемся итоге и при провалившемся выглядели одинаково. Три
    # исхода, и третий — «проверять было нечем» — не то же, что «не сошлось».
    strength = Counter(item.arithmetic for item in saved)
    typer.echo(
        "  арифметика при подтверждении: сошлась "
        f"{strength.get(True, 0)}, не сошлась {strength.get(False, 0)}, "
        f"проверять было нечем {strength.get(None, 0)}"
    )

    written = 0
    for issuer in issuers:
        document = text_of(issuer.path)
        intake = accept_ifrs_document(
            document.text,
            inn=issuer.inn,
            raw_path=str(issuer.path),
            document=document,
        )
        if not intake.accepted or intake.loaded is None:
            typer.echo(
                typer.style(
                    f"  {issuer.inn} {issuer.report_date}: не записан — "
                    f"{intake.reason or 'запись не выполнена'}",
                    fg=typer.colors.YELLOW,
                )
            )
            continue
        loaded = intake.loaded
        written += loaded.collisions.by_confirmation
        mark = "КАРАНТИН" if loaded.quarantined else "расчёт разрешён"
        typer.echo(
            f"  {issuer.inn} {issuer.report_date}: фактов по подтверждению "
            f"{loaded.collisions.by_confirmation}, всего записано "
            f"{loaded.facts_written} из {loaded.facts_total}, {mark}"
        )

    typer.echo(
        typer.style(
            f"\nФактов по подтверждению человека записано: {written}", bold=True
        )
    )
    if lost:
        typer.echo(
            typer.style(
                "\nОстаётся глазами — притязание отклонено правилом формы "
                "и раздела либо разметка не применилась:",
                fg=typer.colors.YELLOW,
                bold=True,
            )
        )
        for item in lost:
            typer.echo(typer.style(f"  {item.describe()}", fg=typer.colors.YELLOW))

    # **«Справочник даёт другой код» и «строки в очереди нет» — не потери,
    # но и не применённое решение.** В первом случае расходятся наш код
    # и код человека, и разойтись они могли только в одну сторону: кто-то
    # из двух неправ. Во втором строки в разборе больше нет — либо её
    # опознали под другим написанием, либо разбор её потерял, и различить
    # это может только человек. Молчание здесь читалось бы как «применено».
    from finlib.sources.ifrs_markup import FATE_GONE, FATE_OTHER_CODE

    eyes = [item for item in saved if item.fate in (FATE_OTHER_CODE, FATE_GONE)]
    if eyes:
        typer.echo(
            typer.style(
                f"\nТребует глаз, но потерей не считается: {len(eyes)}",
                bold=True,
            )
        )
        for item in eyes:
            typer.echo(f"  {item.describe()}")

    # **Специфическая статья, у которой строки больше нет, — единственный
    # случай, когда «строки в очереди нет» означает потерю величины.** Код
    # из справочника объясняется просто: строку опознал справочник, и она
    # в очередь не попала. Код вне справочника опознать нечем — значит, строки
    # в разборе нет вовсе, и величина не учтена нигде.
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.sources.ifrs_markup import NOT_A_LINE_CODE

    # Именно справочник, а не `known_codes`: тот включает и подтверждённые
    # человеком коды, то есть ответил бы «код известен» на любой из них.
    codes = {item.code for item in load_ifrs_lines().positions}
    orphans = [
        item
        for item in eyes
        if item.fate == FATE_GONE
        and item.code not in codes
        and item.code != NOT_A_LINE_CODE
    ]
    if orphans:
        typer.echo(
            typer.style(
                f"\nИз них статьи вне справочника, строки которых разбор больше "
                f"не даёт: {len(orphans)}. Величина такой строки не учтена "
                "нигде, и смотреть надо документ, а не разметку:",
                fg=typer.colors.YELLOW,
                bold=True,
            )
        )
        for item in orphans:
            typer.echo(typer.style(f"  {item.describe()}", fg=typer.colors.YELLOW))


@app.command("ifrs-assess")
def ifrs_assess_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    report_date: Annotated[
        str,
        typer.Option(
            "--report-date",
            help="Отчётная дата комплекта, ГГГГ-ММ-ДД; по умолчанию самая свежая",
        ),
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Считает показатели и оценку по МСФО **по фактам базы** и пишет их.

    Путь расчёта здесь тот же, что у заключения: вход собирается из
    `fact_report`, а не из разобранного документа. Замер задачи 27 считает
    по документу, и два пути обязаны давать одно число — у ФосАгро это класс B
    и балл 66,0. Пока этой команды не было, расчёт по фактам МСФО не вызывался
    ниоткуда: он был написан и недостижим.

    Комплекты в карантине не читаются, и это не молчание: причина стоит
    в журнале качества, а команда называет, что считать нечего.
    """
    _setup_logging(verbose)
    _check_inn(inn)

    from finlib.db import connection
    from finlib.metrics.ifrs_store import (
        IfrsPeriodMissingError,
        compute_from_facts,
        periods_of,
    )
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics
    from finlib.scoring.ifrs_store import assess_ifrs

    policy = load_ifrs_metrics()
    with connection() as conn:
        periods = periods_of(inn, conn)
        if not periods:
            _fail(
                f"по МСФО у {inn} нет комплектов вне карантина: считать нечего. "
                "Причина отбраковки — в журнале качества (fin-analysis quality)"
            )
        if report_date.strip():
            try:
                target = date.fromisoformat(report_date.strip())
            except ValueError:
                _fail(f"--report-date принимает дату ГГГГ-ММ-ДД, получено «{report_date}»")
            if target not in periods:
                listed = ", ".join(str(item) for item in periods)
                _fail(f"комплекта на {target} нет; есть: {listed}")
        else:
            target = periods[0]

        try:
            computed = compute_from_facts(inn, target, conn, policy)
        except IfrsPeriodMissingError as failure:
            _fail(str(failure))

        typer.echo(f"Расчёт по МСФО, {inn}, период {target:%d.%m.%Y}\n")
        for item in computed:
            typer.echo(f"  {item.describe()}")

        # Показатели считаются по всем периодам вне карантина, а балл — по
        # уровню отчётного: правило объявлено методикой, изменения идут
        # читателю, а не шкале.
        result, saved, stops = assess_ifrs(inn, conn, policy)

    # **Стоп-факторы печатаются поимённо, включая не сработавшие.** Класс
    # у Сегежи выходит низшим и по баллу, и по стоп-фактору, и без перечня
    # проверенного одно от другого не отличить: прогон, ради которого
    # снимался карантин, не показывал главного.
    typer.echo("")
    typer.echo(f"  стоп-факторы: проверено {stops.checked}")
    for check in stops.checks:
        colour = typer.colors.YELLOW if check.verdict == "triggered" else None
        typer.echo(typer.style(f"    {check.describe()}", fg=colour))
    if stops.code:
        typer.echo(
            f"    записан в оценку: {stops.code}; класс до применения "
            f"{result.class_before_stop}, после {result.class_code}"
        )
        typer.echo(f"    сверка с аудиторским заключением: {stops.audit_note}")
    for metric, limitation in stops.excluded_reasons:
        typer.echo(f"    из балла исключён {metric}: {limitation}")

    typer.echo("")
    for group in result.groups:
        typer.echo(f"  {group.describe()}")
    typer.echo(
        typer.style(f"\n{result.describe()}", fg=typer.colors.GREEN, bold=True)
    )
    typer.echo(f"значений записано: {saved}")
    if result.divergence:
        # Расхождение двух мер долговой нагрузки печатается всегда: ноль
        # превышений при неизвестном разрыве неотличим от несделанного
        # сравнения.
        typer.echo("расхождение мер долговой нагрузки: " + "; ".join(result.divergence))


@app.command("pdf-check")
def pdf_check_command(
    paths: Annotated[
        list[Path],
        typer.Argument(help="Файлы PDF либо каталоги с ними"),
    ],
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Проверяет документ МСФО до выгрузки в проект: извлекаются ли формы.

    **«Текстовый слой есть» и «формы извлекаются» — разные вещи.** Слой
    бывает у всего документа и при этом отсутствует у отдельных страниц
    внутри форм: у Автодора баланс занимает страницы 8 и 9, слой есть
    только у восьмой, и вся сторона пассива не существовала — актив при
    этом сходился сам с собой. У Самолёта таких страниц две. Скан
    отбраковывается на входе, а потеря одной страницы не отбраковывается
    ничем: половина формы извлеклась, контроли по ней прошли, заметить
    нечем.

    Поэтому команда печатает и то и другое: страницы без слоя отдельно
    от тех, что попали внутрь форм, и рядом — что из документа извлеклось:
    формы, отчётные даты каждой из них, опознанные строки, сведённые итоги.
    В базу не пишется ничего.
    """
    _setup_logging(verbose)
    from finlib.sources.pdf_text import read_document

    documents: list[Path] = []
    for item in paths:
        if item.is_dir():
            documents.extend(sorted(item.rglob("*.pdf")))
        else:
            documents.append(item)
    if not documents:
        _fail("ни одного файла не найдено")

    bad = 0
    for path in documents:
        if not _echo_pdf_check(path, read_document(path)):
            bad += 1
    typer.echo(
        f"\nПроверено файлов {len(documents)}, к загрузке не готовы {bad}."
    )
    if bad:
        raise typer.Exit(code=1)


def _echo_pdf_check(path: Path, document) -> bool:
    """Печатает разбор одного файла; возвращает готовность к загрузке."""
    from finlib.pipeline import accept_ifrs_document

    typer.echo(typer.style(f"\n{path}", bold=True))
    if not document.readable:
        typer.echo(
            typer.style(f"  файл не прочитан: {document.error}", fg=typer.colors.RED)
        )
        return False
    typer.echo(f"  {document.describe()}")

    intake = accept_ifrs_document(document.text, inn=None, document=document)
    if not intake.accepted:
        typer.echo(
            typer.style(
                f"  отказ приёма [{intake.check_code}]: {intake.reason}",
                fg=typer.colors.RED,
            )
        )
        # Отказ по разделителю разрядов проверяется только глазами: счётчик
        # улик говорит «сколько», а разбираться приходится с «какие». Числа
        # печатаются вместе со строками, в которых стоят, — это и есть места
        # документа, куда надо посмотреть.
        for line in (intake.details or {}).get("evidence", ()):
            typer.echo(f"      {line}")
        for line in (intake.details or {}).get("where", ()):
            typer.echo(typer.style(f"      {line}", fg=typer.colors.YELLOW))
        return False

    profile = intake.profile
    typer.echo(f"  форм найдено {len(profile.forms)}, страниц под ними {profile.form_pages}")
    for code, dates in sorted(profile.dates_by_form.items()):
        inherited = code in profile.inherited_dates
        mark = " — даты документа, своих форма не объявила" if inherited else ""
        typer.echo(
            f"    {code.removeprefix('ifrs.'):34}"
            + ", ".join(f"{item:%d.%m.%Y}" for item in dates)
            + mark
        )

    # Страницы без слоя называются все, но готовность отменяют только те,
    # что попали внутрь форм: аудиторское заключение сканом разбору не мешает.
    empty = document.pages_without_text
    inside = profile.pages_without_text
    typer.echo(
        f"  страниц без текстового слоя {len(empty)}"
        + (f": {', '.join(str(item) for item in empty)}" if empty else "")
    )
    if inside:
        typer.echo(
            typer.style(
                "  ВНУТРИ ФОРМ страницы-изображения: "
                + ", ".join(str(item) for item in inside)
                + " — содержимое не извлечено вовсе",
                fg=typer.colors.RED,
                bold=True,
            )
        )

    decision, extraction = intake.review, intake.extraction
    typer.echo(
        f"  извлечено: строк опознано {decision.rows_recognised} из "
        f"{decision.rows_total}, величин {len(extraction.values)}, итогов "
        f"сверено {decision.totals_checked}, из них не сошлось "
        f"{len(decision.totals_failed)}"
    )
    if decision.automatic:
        typer.echo(typer.style("  принимается автоматически", fg=typer.colors.GREEN))
    else:
        typer.echo(
            typer.style(
                "  потребует подтверждения человеком: "
                + ", ".join(item.value for item in decision.reasons),
                fg=typer.colors.YELLOW,
            )
        )
    return not inside


_DECISION_ADD = """
INSERT INTO routing_decision
       (inn, standard, basket, author, reason, decided_on, valid_until)
VALUES (%(inn)s, %(standard)s, %(basket)s, %(author)s, %(reason)s,
        %(decided_on)s, %(valid_until)s)
ON CONFLICT (inn, standard, decided_on) DO UPDATE SET
    basket = EXCLUDED.basket,
    author = EXCLUDED.author,
    reason = EXCLUDED.reason,
    valid_until = EXCLUDED.valid_until
"""

_DECISION_LIST = """
SELECT inn, standard, basket, author, reason, decided_on, valid_until
FROM routing_decision
ORDER BY inn, decided_on DESC
"""


@app.command("routing-decide")
def routing_decide_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    basket: Annotated[
        str, typer.Option("--basket", help="Корзина не ниже: attention либо review")
    ],
    author: Annotated[str, typer.Option("--who", help="Кто принял решение")],
    reason: Annotated[str, typer.Option("--reason", help="Основание словами")],
    until: Annotated[
        str, typer.Option("--until", help="Срок действия, ГГГГ-ММ-ДД")
    ],
    decided: Annotated[
        str | None, typer.Option("--decided", help="Дата решения, ГГГГ-ММ-ДД")
    ] = None,
    standard: Annotated[
        str,
        typer.Option(
            "--standard",
            help="Стандарт отчётности, по которой построен маршрут: rsbu либо ifrs",
        ),
    ] = Standard.IFRS.value,
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Вносит решение человека о корзине маршрута в журнал.

    **Обстоятельство, которого машина не видит, вносит человек.** Статуса
    наблюдения у источника нет вовсе: у Русагро АКРА объявило наблюдение
    22.04.2026, а в карточке стоит стабильный прогноз, и вывести одно
    из другого нельзя.

    Решение называет автора, основание и срок. Срок обязателен: наблюдение
    агентства снимается, а запись о нём без срока пережила бы своё основание.
    «Не ниже», а не «назначить»: решение добавляется к машинным основаниям,
    а не отменяет их.

    **Стандарт называется, как у всякой выборки по ИНН.** Маршрут строится
    по консолидированной отчётности либо по отчётности юридического лица,
    и решение ищется по стандарту своей строки: записанное под чужим,
    оно не нашлось бы вовсе.
    """
    _setup_logging(verbose)
    _check_inn(inn)
    from datetime import date as _date

    from finlib.db import execute

    if standard not in (Standard.RSBU.value, Standard.IFRS.value):
        _fail("стандарт решения — rsbu либо ifrs: выборка по ИНН называет стандарт")
    if basket not in ("attention", "review"):
        _fail("корзина решения — attention либо review: «не ниже», а не назначение")
    if not reason.strip():
        _fail("основание пустое: пустая причина — отказ, а не молчание")
    try:
        valid_until = _date.fromisoformat(until)
        decided_on = _date.fromisoformat(decided) if decided else _date.today()
    except ValueError:
        _fail("даты задаются как ГГГГ-ММ-ДД")
    if valid_until < decided_on:
        _fail("срок действия раньше даты решения")
    execute(
        _DECISION_ADD,
        {
            "inn": inn,
            "standard": standard,
            "basket": basket,
            "author": author,
            "reason": reason.strip(),
            "decided_on": decided_on,
            "valid_until": valid_until,
        },
    )
    typer.echo(
        f"записано: {inn} ({standard}) не ниже «{basket}» до "
        f"{valid_until:%d.%m.%Y} ({author}, {decided_on:%d.%m.%Y}) — "
        f"{reason.strip()}"
    )
    rows = fetch_all(_DECISION_LIST, {})
    live = [row for row in rows if row["valid_until"] >= _date.today()]
    typer.echo(f"\nВ журнале записей {len(rows)}, из них действует {len(live)}:")
    for row in rows:
        mark = " " if row in live else "истекло"
        typer.echo(
            f"  {row['inn']:<12} {row['standard']:<5} {row['basket']:<10} до "
            f"{row['valid_until']:%d.%m.%Y} {mark:<8} {row['author']:<16} "
            f"{row['reason'][:56]}"
        )


def main() -> int:
    """Запуск приложения."""
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
