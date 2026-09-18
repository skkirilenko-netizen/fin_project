"""Замер по типам эмитента: определение типа и применимость стоп-факторов.

Отвечает на три вопроса задачи 26: у скольких эмитентов тип определяется
машинно, у скольких требует подтверждения человеком, и какие стоп-факторы
у кого меняют исход. Рядом — сверка с аудиторским заключением: стоп-фактор
с подтверждением аудитора и без него суть разные ситуации.

    uv run python eval/ifrs_type_run.py

Ничего не пишет ни в базу, ни на диск: это замер, а не загрузка.
"""

import argparse
import logging
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

from finlib.cli import _load_issuers
from finlib.normalize.ifrs_issuer_type import load_ifrs_metrics, load_issuer_types
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_audit import Determination as AuditState
from finlib.sources.ifrs_audit import read_audit_report
from finlib.sources.ifrs_inbox import form_headings, text_of
from finlib.sources.ifrs_issuer_type import (
    Determination,
    applicability,
    consistency,
    determine_type,
)
from finlib.sources.ifrs_markup import restore
from finlib.sources.ifrs_numbers import load_parsing_policy

logger = logging.getLogger(__name__)


def _fired(values: dict[str, Decimal]) -> dict[str, bool]:
    """Какие стоп-факторы сработали по величинам отчётности.

    Условия те же, что в РСБУ: правило одно, меняется только применимость.
    Величины нет — стоп-фактор не срабатывает: отсутствие не есть
    отрицательная величина.
    """
    equity = values.get("ifrs.total_equity")
    assets = values.get("ifrs.total_assets")
    current = values.get("ifrs.total_current_assets")
    liabilities = values.get("ifrs.total_current_liabilities")
    profit = values.get("ifrs.operating_profit")
    finance = values.get("ifrs.finance_costs")
    autonomy = equity / assets if equity is not None and assets else None
    nwc = current - liabilities if current is not None and liabilities is not None else None
    cover = profit / abs(finance) if profit is not None and finance else None
    return {
        "negative_equity": equity is not None and equity < 0,
        "negative_autonomy": autonomy is not None and autonomy < 0,
        "negative_nwc": nwc is not None and nwc < 0,
        "interest_cover_below_one": cover is not None and cover < 1,
    }, cover


def _known_values(issuer, catalog) -> dict[str, Decimal]:
    """Все известные величины комплекта, включая специфические статьи.

    Признак типа — как раз специфическая статья: задолженность Принципала
    и экономия по кредитам с эскроу кодов ядра не имеют, потому что бывают
    не у всех. Брать только позиции справочника значило бы не увидеть
    ни одного типа — что при первом прогоне и вышло.
    """
    found = dict(issuer.values(catalog))
    for row in issuer.extraction.unrecognised:
        code = issuer.specific.get(row.key)
        if code and row.values:
            found[code] = row.values[0]
    return found


def main(argv: list[str] | None = None) -> int:
    """Печатает замер по типам эмитента; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    types = load_issuer_types()
    metrics = load_ifrs_metrics()
    catalog = load_ifrs_lines()
    print(
        f"справочник типов {types.version}: типов {len(types.types)}, "
        f"норм неприменимости {len(types.not_applicable)}, "
        f"поправок показателей {len(metrics.adjustments)}"
    )

    issuers, skipped = _load_issuers(args.path)
    if not issuers:
        print(f"в каталоге {args.path} нет комплектов: измерять нечего")
        return 1
    try:
        restore(issuers)
    except Exception as failure:  # noqa: BLE001 — замер не должен падать из-за базы
        print(f"разметка прежних сессий не восстановлена: {failure}")

    determined = Counter()
    confirmations = 0
    changed: list[str] = []

    print("\nТИП ЭМИТЕНТА")
    verdicts = {}
    for issuer in issuers:
        document = text_of(issuer.path)
        verdict = determine_type(_known_values(issuer, catalog), document.text, types)
        verdicts[issuer.inn] = (verdict, document)
        determined[verdict.determination] += 1
        confirmations += 1 if verdict.needs_confirmation else 0
        print(f"  {issuer.inn}: {verdict.describe()}")
    print(
        f"  определено структурно {determined[Determination.STRUCTURAL]} "
        f"из {len(issuers)}, по умолчанию {determined[Determination.DEFAULT]}; "
        f"требуют подтверждения человеком {confirmations}"
    )
    for name, reason in skipped:
        print(f"  вне замера {name}: {reason}")

    print("\nСТОП-ФАКТОРЫ И ИХ ПРИМЕНИМОСТЬ")
    for issuer in issuers:
        verdict, document = verdicts[issuer.inn]
        values = _known_values(issuer, catalog)
        fired, cover = _fired(values)
        headings = form_headings(document.text, catalog, load_parsing_policy())
        report = read_audit_report(
            document.text, document, before=min(headings.values(), default=0)
        )
        readable = report.determination is AuditState.DETERMINED
        print(f"  {issuer.inn} [{verdict.code}]:")
        if not any(fired.values()):
            print("      стоп-факторы не сработали")
        for code, is_fired in fired.items():
            if not is_fired:
                continue
            outcome = applicability(code, verdict.code, {"interest_cover": cover}, types)
            state, note = consistency(code, report.sections, readable, types)
            factor = next(item for item in types.stop_factors if item.code == code)
            print(f"      {factor.name}: {outcome.describe()}; заключение — {state.value}")
            if not outcome.applicable:
                changed.append(f"{issuer.inn}/{code}")
                print(f"          оговорка: {outcome.limitation.split('.')[0]}.")
            print(f"          сверка: {note.split('.')[0]}.")

        for adjustment in metrics.for_type(verdict.code):
            missing = [
                code for code in adjustment.requires if values.get(code) is None
            ]
            state = (
                f"не рассчитывается: нет величин {', '.join(missing)}"
                if missing
                else "рассчитывается с поправкой"
            )
            print(f"      поправка показателя {adjustment.metric}: {state}")

    print(
        f"\nисход изменён у {len(changed)} стоп-факторов: "
        + (", ".join(changed) if changed else "ни у одного")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
