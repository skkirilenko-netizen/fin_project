"""Замер по расчёту и оценке МСФО (задача 27).

Отвечает на вопросы, ради которых состав подбирался: сколько показателей
считается у каждого эмитента, у скольких присваивается класс, какие
стоп-факторы меняют исход и как часто расходятся две меры долговой нагрузки.

**Расхождение мер одной группы считается отдельно**: Чистый долг / EBITDA
и FFO / Долг измеряют одно и то же разными способами, и систематическое
расхождение означает либо содержательный сигнал, либо неверную калибровку.
Различить их может только человек, поэтому расхождение печатается.

    uv run python eval/ifrs_scoring_run.py

Ничего не пишет ни в базу, ни на диск: это замер, а не загрузка.
"""

import argparse
import logging
import sys
from decimal import Decimal
from pathlib import Path

from finlib.cli import _load_issuers
from finlib.metrics.ifrs import Inputs, compute_all
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.scoring.ifrs import assess
from finlib.sources.ifrs_audit import Determination as AuditState
from finlib.sources.ifrs_audit import read_audit_report
from finlib.sources.ifrs_inbox import form_headings, text_of
from finlib.sources.ifrs_issuer_type import applicability, consistency, determine_type
from finlib.sources.ifrs_markup import restore
from finlib.sources.ifrs_notes import accrued_interest, index_notes, note_values
from finlib.sources.ifrs_numbers import load_parsing_policy

logger = logging.getLogger(__name__)

# Стоп-факторы считаются по тем же величинам, что и показатели: правило одно,
# меняется только применимость (задача 26).
STOP_FACTORS = {
    "negative_equity": "ifrs.total_equity",
    "negative_autonomy": "equity_ratio",
    "negative_nwc": "nwc",
    "interest_cover_below_one": "interest_cover_accrued",
}


def _known(issuer, catalog) -> dict[str, Decimal]:
    """Величины комплекта вместе со специфическими статьями."""
    found = dict(issuer.values(catalog))
    for row in issuer.extraction.unrecognised:
        code = issuer.specific.get(row.key)
        if code and row.values:
            found[code] = row.values[0]
    return found


def main(argv: list[str] | None = None) -> int:
    """Печатает замер по расчёту и оценке; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    policy = load_ifrs_metrics()
    catalog = load_ifrs_lines()
    print(
        f"справочник показателей {policy.version}: показателей {len(policy.metrics)}, "
        f"в балле {len(policy.scored())}, групп {len(policy.groups)}"
    )

    issuers, skipped = _load_issuers(args.path)
    if not issuers:
        print(f"в каталоге {args.path} нет комплектов: измерять нечего")
        return 1
    try:
        restore(issuers)
    except Exception as failure:  # noqa: BLE001 — замер не должен падать из-за базы
        print(f"разметка прежних сессий не восстановлена: {failure}")

    classed = 0
    divergences = 0
    gaps: list = []
    excluded_total = 0
    for issuer in issuers:
        document = text_of(issuer.path)
        values = _known(issuer, catalog)
        headings = form_headings(document.text, catalog, load_parsing_policy())
        index = index_notes(
            document.text, document, after=min(headings.values(), default=0)
        )
        rows = {
            item.code: item.note_reference
            for form in issuer.extraction.forms.values()
            for item in form.values
            if item.report_date == issuer.report_date
        }
        notes, outcomes = note_values(
            index,
            rows,
            document.text,
            issuer.profile.grouping,
            len(issuer.profile.report_dates),
        )
        verdict = determine_type(values, document.text, None)
        # Знаменатель покрытия процентов собирается из примечаний здесь:
        # в расчётный слой он приходит готовым, и подставить величину
        # из формы там нечем — её в этом словаре нет.
        notes = dict(notes)
        accrued = accrued_interest(notes, outcomes)
        if accrued is not None:
            notes["interest_accrued"] = accrued
        computed = compute_all(
            Inputs(values, notes, verdict.code, months=12), policy
        )

        # Неприменимость стоп-фактора по типу и по обстановке действует
        # в расчёте: показатель считается и печатается, а в балл не идёт.
        by_code = {item.code: item for item in computed}
        cover = by_code["interest_cover_accrued"].value
        excluded: list[str] = []
        for factor, metric in STOP_FACTORS.items():
            outcome = applicability(factor, verdict.code, {"interest_cover": cover})
            if not outcome.applicable and metric in by_code:
                excluded.append(metric)
        excluded_total += len(excluded)

        result = assess(computed, policy, tuple(excluded))
        if result.class_code is not None:
            classed += 1
        if result.divergence:
            divergences += 1

        report = read_audit_report(
            document.text, document, before=min(headings.values(), default=0)
        )
        readable = report.determination is AuditState.DETERMINED

        print(f"\n{issuer.inn} [{verdict.code}]")
        for item in computed:
            print(f"      {item.describe()}")
        for item in outcomes:
            if not item.found:
                print(f"      примечания: {item.describe()}")
        for group in result.groups:
            print(f"      {group.describe()}")
        print(f"      {result.describe()}")
        gaps.append(result.divergence_gap)
        print(
            "      расхождение мер долговой нагрузки: "
            + (
                f"{result.divergence_gap:.1f} балла"
                if result.divergence_gap is not None
                else "не сравнивалось — считается не обе"
            )
        )
        for note in result.divergence:
            print(f"      сверх порога: {note}")
        for metric in excluded:
            print(f"      исключён из балла неприменимостью: {metric}")
        state, note = consistency("negative_nwc", report.sections, readable)
        print(f"      заключение: {state.value} — {note.split('.')[0]}.")

    print(
        f"\nкласс присвоен {classed} из {len(issuers)}; "
        f"показателей исключено неприменимостью {excluded_total}"
    )
    compared = [item for item in gaps if item is not None]
    print(
        f"расхождение мер долговой нагрузки сравнивалось у {len(compared)} "
        f"из {len(issuers)}; превысило порог у {divergences}; "
        + (
            f"наибольший разрыв {max(compared):.1f} балла"
            if compared
            else "разрывов не измерено"
        )
    )
    for name, reason in skipped:
        print(f"  вне замера {name}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
