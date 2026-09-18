"""Замер по критериям решения по ветке МСФО — `eval/ifrs_markup_criteria.md`.

Критерии зафиксированы до разметки и до получения результата; здесь они
только считаются. Три измерения — те же, что объявлены в критериях:

1. покрытие валюты баланса опознанными позициями по каждому эмитенту;
2. доля эмитентов, у которых опознаны все позиции ключевых показателей;
3. разброс покрытия между эмитентами.

Рядом — два ответа, без которых по критериям нельзя решать:
сколько позиций закрывает Cbonds, а сколько требует PDF (скрининг по API
реален ровно настолько, насколько велика первая доля), и какие итоги
не сошлись после разметки.

**Счётчик проверенного стоит рядом со счётчиком сработавшего.** Эмитент,
у которого не определилась валюта баланса, не даёт нулевого покрытия —
он говорит, что измерения по нему не было; печатать ноль здесь значило бы
выдать отсутствие данных за результат.

    uv run python eval/ifrs_criteria_run.py
    uv run python eval/ifrs_criteria_run.py --grouping 7736216869=english

Ничего не пишет ни в базу, ни на диск: это замер, а не загрузка.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from finlib.cli import _load_issuers
from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.normalize.lines import Operator
from finlib.quality.totals import TotalVerdict
from finlib.sources.cbonds import cached
from finlib.sources.ifrs_markup import (
    IssuerMarkup,
    candidates,
    normal_sign_of,
    restore,
    review_saved,
)

logger = logging.getLogger(__name__)

# Валюта баланса: итог актива, а при его отсутствии — итог пассива. Величина
# одна и та же, и сторона здесь только источник: комплект, у которого
# не опознан итог одной стороны, измеряется по другой, а не выпадает из замера.
BALANCE_TOTALS = ("ifrs.total_assets", "ifrs.total_equity_and_liabilities")

# Стороны баланса и разделы каждой. Покрытие считается по слагаемым разделов,
# а не по самим итогам: итог опознаётся почти всегда и поднимал бы покрытие
# независимо от того, что мы знаем о статьях.
SIDES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("актив", ("ifrs.total_non_current_assets", "ifrs.total_current_assets")),
    (
        "пассив",
        (
            "ifrs.total_equity",
            "ifrs.total_non_current_liabilities",
            "ifrs.total_current_liabilities",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class MetricNeed:
    """Показатель ветки и позиции, без которых он не считается.

    Состав повторяет определения РСБУ один в один: долг — только заёмные
    средства, аренда в него не входит (`metrics.yaml`, оговорка к `debt_total`),
    покрытие процентов — операционная прибыль к финансовым расходам. Замер
    не вправе определять показатель шире методики: набор, подобранный под
    имеющиеся данные, измерял бы сам себя.
    """

    code: str
    name: str
    needs: tuple[str, ...]
    # Позиция, которой можно заменить недостающую, и что тогда меняется.
    # Замена не засчитывается в измерение: она названа, чтобы человек видел
    # цену отказа, а не чтобы поднять долю.
    fallback: tuple[str, str] | None = None


METRICS: tuple[MetricNeed, ...] = (
    MetricNeed(
        "net_debt_ebitda",
        "Чистый долг / EBITDA",
        (
            "ifrs.long_term_borrowings",
            "ifrs.short_term_borrowings",
            "ifrs.cash",
            "ifrs.operating_profit",
            "ifrs.depreciation",
        ),
    ),
    MetricNeed(
        "interest_cover",
        "Покрытие процентов",
        ("ifrs.operating_profit", "ifrs.finance_costs"),
        fallback=(
            "ifrs.interest_paid",
            "проценты уплаченные из ОДДС вместо финансовых расходов",
        ),
    ),
    MetricNeed(
        "cur_liq",
        "Текущая ликвидность",
        ("ifrs.total_current_assets", "ifrs.total_current_liabilities"),
    ),
)

# Соответствие позиций нашей модели именованным кодам Cbonds
# (`data/raw/cbonds/nomenclature.json`, отчёт `report_msfo_real`). `None` —
# кода нет вовсе: величина есть только в PDF.
CBONDS_CODES: dict[str, str | None] = {
    "ifrs.long_term_borrowings": "ln17",
    "ifrs.short_term_borrowings": "ln14",
    "ifrs.cash": "ln3",
    "ifrs.operating_profit": "ln37",
    "ifrs.depreciation": "ln79",
    "ifrs.finance_costs": None,
    "ifrs.interest_paid": None,
    "ifrs.total_current_assets": "ln6",
    "ifrs.total_current_liabilities": "ln38",
}

# Готовые величины Cbonds: показатель посчитан источником. Проверяются
# отдельно от слагаемых — по правилу ветки ноль у Cbonds не означает нуля,
# и готовая величина, равная нулю, показателем не является.
CBONDS_READY: dict[str, str] = {
    "ln34": "Общий долг",
    "ln35": "Чистый долг",
    "ln36": "EBITDA",
    "ln79": "Износ, истощение и амортизация",
    "ln74": "Чистый долг / EBITDA TTM",
}


@dataclass(frozen=True, slots=True)
class SideCoverage:
    """Опознанное по одной стороне баланса: ядром, специфическими, остаток."""

    side: str
    core: Decimal
    specific: Decimal


@dataclass(frozen=True, slots=True)
class Coverage:
    """Покрытие валюты баланса по одному эмитенту."""

    inn: str
    report_date: date
    currency: Decimal | None
    sides: tuple[SideCoverage, ...]

    def share(self, side: SideCoverage, with_specific: bool = True) -> Decimal | None:
        """Доля валюты баланса, объяснённая стороной; None — валюты нет."""
        if self.currency is None or self.currency == 0:
            return None
        covered = side.core + (side.specific if with_specific else Decimal(0))
        return covered / abs(self.currency)

    @property
    def overall(self) -> Decimal | None:
        """Покрытие обеих сторон разом: опознанное к двум валютам баланса."""
        if self.currency is None or self.currency == 0:
            return None
        covered = sum(
            (item.core + item.specific for item in self.sides), start=Decimal(0)
        )
        return covered / (abs(self.currency) * 2)

    @property
    def core_only(self) -> Decimal | None:
        """То же без специфических статей: покрытие одним ядром справочника."""
        if self.currency is None or self.currency == 0:
            return None
        covered = sum((item.core for item in self.sides), start=Decimal(0))
        return covered / (abs(self.currency) * 2)


def coverage_of(issuer: IssuerMarkup, catalog: IfrsCatalog) -> Coverage:
    """Считает покрытие валюты баланса опознанными позициями."""
    values = issuer.values(catalog)
    extras = issuer.extras(catalog)
    sign_of = normal_sign_of(catalog)
    currency = next(
        (values[code] for code in BALANCE_TOTALS if values.get(code) is not None), None
    )
    sides: list[SideCoverage] = []
    for side, sections in SIDES:
        core = Decimal(0)
        specific = Decimal(0)
        for section in sections:
            position = catalog.get(section)
            if position is None:
                continue
            for component in position.components:
                value = values.get(component.code)
                if value is None:
                    continue
                if sign_of(component.code) < 0 and value > 0:
                    value = -value
                core += value if component.op is Operator.PLUS else -value
            specific += extras.get(section, Decimal(0))
        sides.append(SideCoverage(side, core, specific))
    return Coverage(issuer.inn, issuer.report_date, currency, tuple(sides))


@dataclass(frozen=True, slots=True)
class MetricReadiness:
    """Готовность одного показателя у одного эмитента."""

    inn: str
    metric: MetricNeed
    missing: tuple[str, ...]
    fallback_available: bool = False

    @property
    def ready(self) -> bool:
        """Опознаны ли все позиции: мера двоичная, частичного набора нет."""
        return not self.missing


def readiness_of(
    issuer: IssuerMarkup, catalog: IfrsCatalog, metric: MetricNeed
) -> MetricReadiness:
    """Проверяет, опознаны ли все позиции показателя за отчётный период."""
    values = issuer.values(catalog)
    missing = tuple(code for code in metric.needs if values.get(code) is None)
    fallback = bool(
        metric.fallback is not None and values.get(metric.fallback[0]) is not None
    )
    return MetricReadiness(issuer.inn, metric, missing, fallback)


def _cbonds_item(inn: str, report_date: date) -> dict | None:
    """Запись Cbonds за ту же отчётную дату; None — записи за период нет."""
    wanted = report_date.isoformat()
    return next((row for row in cached(inn) if row.get("date") == wanted), None)


def _cbonds_state(item: dict | None, code: str | None) -> str:
    """Что у Cbonds по этому коду: величина, ноль, пусто, кода нет."""
    if code is None:
        return "кода нет"
    if item is None:
        return "нет записи за период"
    raw = item.get(code)
    if raw in (None, ""):
        return "не заполнено"
    if Decimal(str(raw)) == 0:
        return "ноль (нулём не считается)"
    return "величина"


def _issuer_name(inn: str) -> str:
    """Наименование эмитента из записи Cbonds; пусто — записи нет."""
    found = next((row.get("emitent_name_rus", "") for row in cached(inn)), "")
    return str(found)


def _name_of(code: str, catalog: IfrsCatalog) -> str:
    """Наименование позиции; код — если позиции в справочнике нет."""
    position = catalog.get(code)
    return position.name if position is not None else code


def _percent(value: Decimal | None) -> str:
    """Доля процентом с запятой; прочерк — измерения не было."""
    return "—" if value is None else f"{value:.1%}".replace(".", ",")


def _points(value: Decimal) -> str:
    """Разница долей — процентные пункты, а не проценты: единицы разные."""
    return f"{value * 100:.1f}".replace(".", ",") + " п. п."


def main(argv: list[str] | None = None) -> int:
    """Печатает замер по трём критериям; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument(
        "--grouping",
        action="append",
        default=[],
        metavar="ИНН=конвенция",
        help="Разделитель разрядов вручную: ИНН=russian|english|plain",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    manual: dict[str, str] = {}
    for item in args.grouping:
        inn, _, convention = item.partition("=")
        manual[inn.strip()] = convention.strip()

    issuers, skipped = _load_issuers(args.path, manual)
    if not issuers:
        print(f"в каталоге {args.path} нет документов, прошедших приём")
        return 1

    catalog = load_ifrs_lines()
    try:
        restored = restore(issuers)
    except Exception as failure:  # noqa: BLE001 — замер не должен падать из-за базы
        print(f"разметка прежних сессий не восстановлена: {failure}")
    else:
        print(f"восстановлено присвоений прежних сессий: {restored}")

    # Судьба прежней разметки — часть достоверности замера, а не служебная
    # подробность: присвоение, не нашедшее своей строки, не участвует ни в
    # покрытии, ни в сходимости итогов, и замер по нему занижен.
    try:
        saved = review_saved(issuers)
    except Exception as failure:  # noqa: BLE001 — замер не должен падать из-за базы
        print(f"судьба прежней разметки не проверена: {failure}")
    else:
        fates: dict[str, int] = {}
        for item in saved:
            fates[item.fate] = fates.get(item.fate, 0) + 1
        print("судьба прежней разметки: " + "; ".join(
            f"{fate} {count}" for fate, count in sorted(fates.items())
        ))
        for item in saved:
            if item.fate != "восстановлено":
                print(f"  {item.describe()}")

    print(f"\nЭмитентов в замере: {len(issuers)}")
    for issuer in issuers:
        lost = issuer.profile.pages_without_text
        note = (
            ", страницы без текстового слоя внутри форм: "
            + ", ".join(str(number) for number in lost)
            if lost
            else ""
        )
        print(
            f"  {issuer.inn} {_issuer_name(issuer.inn)}: {issuer.report_date}, "
            f"{issuer.profile.currency}, строк {issuer.extraction.rows_total}{note}"
        )
    for name, reason in skipped:
        print(f"  вне замера {name}: {reason}")

    print("\n1. ПОКРЫТИЕ ВАЛЮТЫ БАЛАНСА")
    print("   ИНН          актив   пассив   обе стороны   одним ядром")
    measured: list[Coverage] = []
    for issuer in issuers:
        found = coverage_of(issuer, catalog)
        measured.append(found)
        assets, passives = found.sides
        print(
            f"   {found.inn}   {_percent(found.share(assets)):>6}   "
            f"{_percent(found.share(passives)):>6}   "
            f"{_percent(found.overall):>11}   {_percent(found.core_only):>11}"
        )
    without = [item.inn for item in measured if item.overall is None]
    if without:
        print(
            "   валюта баланса не опознана, покрытие не измерено: "
            + ", ".join(without)
        )
    print(f"   измерено эмитентов: {len(measured) - len(without)} из {len(issuers)}")

    print("\n2. ПОЛНОТА НАБОРА ПОЗИЦИЙ КЛЮЧЕВЫХ ПОКАЗАТЕЛЕЙ")
    ready_all: list[str] = []
    for issuer in issuers:
        checks = [readiness_of(issuer, catalog, metric) for metric in METRICS]
        if all(item.ready for item in checks):
            ready_all.append(issuer.inn)
        print(f"   {issuer.inn}:")
        for check in checks:
            if check.ready:
                print(f"      {check.metric.name}: все позиции опознаны")
                continue
            missing = ", ".join(
                f"{_name_of(code, catalog)} ({code})" for code in check.missing
            )
            print(f"      {check.metric.name}: не опознано — {missing}")
            if check.metric.fallback is not None:
                _, explanation = check.metric.fallback
                state = "есть" if check.fallback_available else "нет"
                print(f"         замена {state}: {explanation}")
    share = Decimal(len(ready_all)) / Decimal(len(issuers))
    print(
        f"   все три показателя считаются у {len(ready_all)} из {len(issuers)} "
        f"({_percent(share)})"
    )
    for metric in METRICS:
        count = sum(
            1 for issuer in issuers if readiness_of(issuer, catalog, metric).ready
        )
        print(f"      {metric.name}: {count} из {len(issuers)}")

    print("\n3. РАЗБРОС ПОКРЫТИЯ")
    shares = [(item.inn, item.overall) for item in measured if item.overall is not None]
    if not shares:
        print("   покрытие не измерено ни у одного эмитента: разброса нет")
    else:
        low = min(shares, key=lambda item: item[1])
        high = max(shares, key=lambda item: item[1])
        print(f"   наименьшее: {low[0]} — {_percent(low[1])}")
        print(f"   наибольшее: {high[0]} — {_percent(high[1])}")
        print(f"   разница: {_points(high[1] - low[1])} по {len(shares)} эмитентам")
        # Разброс считается ещё раз без комплектов, внутри форм которых
        # потеряна страница: там измеряется дефект документа, а не полнота
        # справочника, и два диагноза методика различает. Признак машинный —
        # страница без текстового слоя, — а не «этот эмитент нетипичен».
        whole = [
            (issuer.inn, item.overall)
            for issuer, item in zip(issuers, measured, strict=True)
            if item.overall is not None and not issuer.profile.pages_without_text
        ]
        if whole and len(whole) != len(shares):
            lowest = min(whole, key=lambda item: item[1])
            highest = max(whole, key=lambda item: item[1])
            print(
                "   без комплектов с потерянными страницами "
                f"({len(whole)} из {len(shares)}): от {_percent(lowest[1])} "
                f"({lowest[0]}) до {_percent(highest[1])} ({highest[0]}), "
                f"разница {_points(highest[1] - lowest[1])}"
            )

    print("\nЧТО ЗАКРЫВАЕТ CBONDS, А ЧТО ТРЕБУЕТ PDF")
    wanted = tuple(
        dict.fromkeys(
            code
            for metric in METRICS
            for code in (*metric.needs, *(x[0] for x in (metric.fallback,) if x))
        )
    )
    for issuer in issuers:
        item = _cbonds_item(issuer.inn, issuer.report_date)
        states = {code: _cbonds_state(item, CBONDS_CODES.get(code)) for code in wanted}
        closed = sum(1 for state in states.values() if state == "величина")
        print(f"   {issuer.inn} ({issuer.report_date}): закрыто {closed} из {len(wanted)}")
        for code, state in states.items():
            if state != "величина":
                print(f"      {_name_of(code, catalog)} ({code}): {state}")
    print("   готовые величины Cbonds:")
    for issuer in issuers:
        item = _cbonds_item(issuer.inn, issuer.report_date)
        parts = [
            f"{name} — {_cbonds_state(item, code)}"
            for code, name in CBONDS_READY.items()
        ]
        print(f"      {issuer.inn}: " + "; ".join(parts))

    print("\nНЕСОШЕДШИЕСЯ ИТОГИ ПОСЛЕ РАЗМЕТКИ")
    broken_total = 0
    checked_total = 0
    for issuer in issuers:
        outcomes = issuer.totals(catalog)
        checked = [
            item
            for item in outcomes.values()
            if item.verdict in (TotalVerdict.MATCHED, TotalVerdict.MISMATCHED)
        ]
        broken = [
            item for item in checked if item.verdict is TotalVerdict.MISMATCHED
        ]
        checked_total += len(checked)
        broken_total += len(broken)
        print(
            f"   {issuer.inn}: не сошлось {len(broken)} из {len(checked)} "
            f"проверенных итогов"
        )
        for item in broken:
            difference = item.difference if item.difference is not None else Decimal(0)
            print(
                f"      {_name_of(item.code, catalog)} ({item.code}): "
                f"итог {item.total}, состав {item.computed}, "
                f"расхождение {difference}"
            )
            if item.undisclosed:
                missing = ", ".join(
                    f"{_name_of(code, catalog)} ({code})" for code in item.undisclosed
                )
                print(f"         не раскрыты слагаемые: {missing}")
    print(f"   всего: не сошлось {broken_total} из {checked_total} проверенных итогов")

    print("\nОДИН КОД НА НЕСКОЛЬКО СТРОК")
    # Затирание величины — самый дорогой из известных ветке дефектов: у ЛСР
    # краткосрочный долг однажды затёр долгосрочный, и показатели считались
    # по величине, заниженной в разы, при сходящемся балансе. Счёт идёт
    # по всем кодам сразу, а не по подозрительным: код, на который легли две
    # строки, обязан быть назван независимо от того, заметна ли разница.
    merged_total = 0
    contested_total = 0
    rejected_total = 0
    for issuer in issuers:
        merged = issuer.extraction.merged
        contested = issuer.extraction.contested
        rejected = issuer.rejects(catalog)
        merged_total += len(merged)
        contested_total += len(contested)
        rejected_total += len(rejected)
        print(
            f"   {issuer.inn}: сложено строк {len(merged)}, спорных позиций "
            f"{len(contested)}, присвоений отклонено {len(rejected)}"
        )
        for code, name, kind in merged:
            print(f"      сложено: {_name_of(code, catalog)} ({code}) ← «{name}» [{kind}]")
        for code, reason in contested:
            print(f"      спор: {_name_of(code, catalog)} ({code}) — {reason}")
        for key, reason in rejected.items():
            print(f"      отклонено присвоение {key}: {reason}")
    print(
        f"   всего: сложено {merged_total}, спорных {contested_total}, "
        f"отклонено присвоений {rejected_total}"
    )

    print("\nОСТАТОК ОЧЕРЕДИ РАЗМЕТКИ")
    rows = candidates(issuers, catalog)
    if not rows:
        print("   неразмеченных строк не осталось")
    for row in rows:
        print(f"   {row.inn}: {row.describe()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
