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
from finlib.normalize.ifrs_note_lines import load_note_lines
from finlib.quality.refusals import check_complete, section
from finlib.report.refusals import (
    from_assessment,
    from_excluded,
    from_ifrs_audit,
    from_ifrs_metrics,
    from_ifrs_notes,
    totals,
)
from finlib.scoring.ifrs import assess
from finlib.sources.ifrs_audit import Determination as AuditState
from finlib.sources.ifrs_audit import read_audit_report
from finlib.sources.ifrs_inbox import form_headings, text_of
from finlib.sources.ifrs_issuer_type import applicability, consistency, determine_type
from finlib.sources.ifrs_markup import restore
from finlib.sources.ifrs_notes import accrued_interest, index_notes, note_values
from finlib.sources.ifrs_numbers import load_parsing_policy

logger = logging.getLogger(__name__)

# Производные величины называются словами, а не кодами: раздел читает человек.
DERIVED_NAMES = {
    "interest_accrued": "начисленные проценты по заёмным средствам "
    "(примечание о финансовых доходах и расходах)",
    "debt_due_within_year": "долг к погашению в ближайшие 12 месяцев "
    "(таблица сроков в примечании о заёмных средствах)",
    "net_debt": "чистый долг: заёмные средства за вычетом денежных",
    "debt_total": "совокупный долг: долгосрочные и краткосрочные заёмные средства",
    "ebitda": "EBITDA: операционная прибыль и амортизация",
    "ffo": "FFO: поток от операционной деятельности до изменений оборотного капитала",
}

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


def _class_where(result, policy, computed, profile) -> str:
    """Чего не хватило для класса: групп, показателей и страниц — поимённо.

    «Основание узкое» без перечня — потеря сведений: читатель обязан видеть,
    какой группы не хватило и почему её показатель не рассчитан.
    """
    present = {item.code for item in result.groups}
    parts: list[str] = []
    missing = [
        group.name for code, group in policy.groups.items() if code not in present
    ]
    if missing:
        parts.append("не представлены группы: " + ", ".join(missing))
    refused = [item.name for item in computed if item.in_scoring and not item.calculable]
    if refused:
        parts.append("не рассчитаны: " + ", ".join(refused))
    if profile.pages_without_text:
        pages = ", ".join(str(item) for item in profile.pages_without_text)
        parts.append(f"страницы без текстового слоя внутри форм: {pages}")
    return "; ".join(parts) if parts else "основание полное"


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
    produced_total = 0
    named_total = 0
    with_place = 0
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

        # Отказы собираются тем же механизмом, что и в РСБУ: раздел
        # «Ограничения анализа» — перечень того, что нужно запросить.
        names = {item.code: (catalog.get(item.code).name if catalog.get(item.code) else item.code)
                 for item in computed}
        names.update({
            code: (catalog.get(code).name or code) if catalog.get(code) else code
            for code in values
        })
        names.update(DERIVED_NAMES)
        names.update({
            item.code: item.name for item in catalog.positions
        })
        note_names = {
            item.code: item.name for item in load_note_lines().lines
        }
        refusals = (
            from_ifrs_metrics(computed, names, adjustments=policy.for_type(verdict.code))
            + from_ifrs_notes(outcomes, names=note_names)
            + from_ifrs_audit(report)
            + from_assessment(result, _class_where(result, policy, computed, issuer.profile))
            + from_excluded(
                {code: by_code[code].name for code in excluded},
                "неприменимость объявлена методикой по типу эмитента",
            )
        )
        lines = section(refusals)
        check_complete(refusals, lines)
        produced_total += len(refusals)
        named_total += len(refusals)
        with_place += sum(1 for item in refusals if item.where and item.where != item.code)
        print(f"      ОГРАНИЧЕНИЯ АНАЛИЗА ({len(refusals)} отказов, {len(lines)} строк):")
        for line in lines:
            print(f"          - {line}")
        print(f"      по семействам: {totals(refusals)}")

    print(
        f"\nкласс присвоен {classed} из {len(issuers)}; "
        f"показателей исключено неприменимостью {excluded_total}"
    )
    print(
        f"отказов произведено {produced_total}, с указанием места {with_place}, "
        f"названо в разделе {named_total}"
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
