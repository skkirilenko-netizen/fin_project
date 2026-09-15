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
from finlib.pipeline import PipelineError, StageResult, analyze
from finlib.report.appendix import UNIT_SUFFIX
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


def _render(value: Decimal | None, unit: str) -> str:
    """Значение показателя в его единице измерения."""
    if value is None:
        return "—"
    if unit == "thousand_rub":
        return f"{value.quantize(Decimal(1)):,}".replace(",", " ") + UNIT_SUFFIX[unit]
    if unit in ("days", "percent"):
        return f"{value.quantize(Decimal('0.1'))}".replace(".", ",") + UNIT_SUFFIX[unit]
    return f"{value.quantize(Decimal('0.01'))}".replace(".", ",")


@app.command("analyze")
def analyze_command(
    inn: Annotated[str, typer.Option("--inn", help=INN_HELP)],
    year: Annotated[int | None, typer.Option("--year", help="Последний отчётный год")] = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Документ без текстовых разделов")
    ] = False,
    force_refresh: Annotated[
        bool, typer.Option("--force-refresh", help="Запросить источник, минуя кэш")
    ] = False,
    output: Annotated[
        Path | None, typer.Option("--output", help="Каталог для документа")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Подробный журнал")] = False,
) -> None:
    """Полный цикл: получение, загрузка, контроли, расчёт, оценка, заключение."""
    _setup_logging(verbose)
    _check_inn(inn)
    typer.echo(f"Анализ организации {inn}\n")
    try:
        result = analyze(
            inn,
            year=year,
            with_llm=not no_llm,
            force_refresh=force_refresh,
            directory=output,
            on_stage=_echo_stage,
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
                _render(item["value"], metric.unit.value)
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
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Документ без текстовых разделов")
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
            with_llm=not no_llm,
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


def main() -> int:
    """Запуск приложения."""
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
