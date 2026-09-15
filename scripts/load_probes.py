"""Перезаливка сохранённых проб ГИР БО в findb.

Схему приходится пересоздавать при каждой правке DDL, а без данных не отладить
ни контроли, ни интерпретацию. Скрипт восстанавливает рабочий набор одной
командой: `make probes`.

Данные берутся только из data/raw/probe — сеть не используется.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from finlib.db import connection, execute
from finlib.metrics.engine import compute_all
from finlib.metrics.store import save_results
from finlib.normalize.loader import load_report_set
from finlib.quality.runner import run_checks
from finlib.scoring.engine import assess
from finlib.scoring.store import save_assessment
from finlib.sources.girbo import Organization, parse_report_sets
from finlib.utils import json_loads_decimal

logger = logging.getLogger("load_probes")

PROBE_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "probe"


@dataclass(frozen=True, slots=True)
class Probe:
    """Проба: файл комплектов и реквизиты организации."""

    file_name: str
    inn: str
    girbo_id: int
    short_name: str
    note: str


PROBES: tuple[Probe, ...] = (
    Probe(
        "girbo_bfo_full_7736050003.json",
        "7736050003",
        6622458,
        'ПАО "ГАЗПРОМ"',
        "полная отчётность, четыре комплекта, признаки холдинговой структуры",
    ),
    Probe(
        "girbo_bfo_simplified_2100010824.json",
        "2100010824",
        12283623,
        'ПК "СТРОЙСЕРВИС"',
        "упрощённая отчётность, неоднозначный код 1190",
    ),
    Probe(
        "girbo_bfo_corrected_2522002003.json",
        "2522002003",
        2422342,
        'ООО "МАГНИТ"',
        "корректировки отчётности, комплект 2025 года не проходит контроли",
    ),
)


def load_probe(probe: Probe, conn, *, with_metrics: bool = True) -> dict[str, int]:
    """Загружает одну пробу: комплекты, контроли, показатели."""
    path = PROBE_DIR / probe.file_name
    if not path.exists():
        raise FileNotFoundError(f"проба не найдена: {path}")

    sets = parse_report_sets(json_loads_decimal(path.read_bytes()), probe.inn)
    organization = Organization(
        inn=probe.inn,
        girbo_id=probe.girbo_id,
        short_name=probe.short_name,
        full_name=probe.short_name,
    )

    quarantined = 0
    for report in sorted(sets, key=lambda item: item.report_year):
        src_file_id = load_report_set(
            report, organization, conn, raw_path=str(path)
        ).src_file_id
        assert src_file_id is not None
        if run_checks(src_file_id, conn).quarantined:
            quarantined += 1

    metrics = 0
    graded = "нет"
    if with_metrics:
        results = compute_all(probe.inn, conn)
        metrics = save_results(probe.inn, results, conn)
        # Оценка пересчитывается здесь же. Без этого набор данных расходится
        # сам с собой: показатели свежие, а класс и разложение остаются
        # от прежней версии методики.
        assessment = assess(probe.inn, conn)
        if assessment is not None:
            save_assessment(assessment, conn)
            graded = assessment.class_code or "без класса"

    return {
        "комплектов": len(sets),
        "в карантине": quarantined,
        "показателей": metrics,
        "класс": graded,
    }


def wipe(conn) -> None:
    """Убирает прежние данные проб, не трогая другие организации."""
    inns = [probe.inn for probe in PROBES]
    execute("DELETE FROM metric_value WHERE inn = ANY(%(i)s)", {"i": inns}, conn=conn)
    execute("DELETE FROM dq_log WHERE inn = ANY(%(i)s)", {"i": inns}, conn=conn)
    execute("DELETE FROM organization WHERE inn = ANY(%(i)s)", {"i": inns}, conn=conn)


def main(argv: list[str] | None = None) -> int:
    """Точка входа скрипта."""
    parser = argparse.ArgumentParser(description="Перезаливка проб ГИР БО в findb")
    parser.add_argument("--keep", action="store_true", help="не удалять прежние данные проб")
    parser.add_argument("--no-metrics", action="store_true", help="только загрузка и контроли")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    with connection() as conn:
        if not args.keep:
            wipe(conn)
        for probe in PROBES:
            stats = load_probe(probe, conn, with_metrics=not args.no_metrics)
            summary = ", ".join(f"{key} {value}" for key, value in stats.items())
            print(f"{probe.inn}  {probe.short_name:22} {summary}")
            print(f"{'':12}  {probe.note}")
    print("\nпробы загружены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
