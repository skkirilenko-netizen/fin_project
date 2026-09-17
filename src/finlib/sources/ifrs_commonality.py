"""Метрика общности статей: покроет ли справочник новых эмитентов.

Ради этого числа и делается разметка. Расчёт зафиксирован **до** подведения
итогов — иначе всегда найдётся способ посчитать так, чтобы вышло убедительно.

**Три показателя, и они отвечают на разные вопросы.** По количеству — какая
доля строк опознаётся общим кодом. По сумме — какая доля величин ими покрыта:
двадцать мелких строк и одна крупная дают одинаковый вклад в первый
показатель и разный во второй. По частоте — сколько кодов встречается
у большинства эмитентов: код, увиденный однажды, справочником не является.

**Из счёта исключается то, что статьёй не является**: колонтитулы и
контрольные суммы, отсеянные без человека, и строки, которые человек назвал
не статьёй. Пометка «встречается у трёх эмитентов» у номера страницы
завышала бы общность тем сильнее, чем хуже разобран документ.

**Итоговые строки считаются отдельно от статей.** Итог опознаётся почти
всегда — он и подписан однообразно, и стоит в справочнике, — и смешение
итогов со статьями поднимало бы общность независимо от того, что мы знаем
о статьях.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.sources.ifrs_markup import IssuerMarkup

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Coverage:
    """Покрытие строк общими кодами: по количеству и по сумме."""

    items: int = 0
    items_covered: int = 0
    totals: int = 0
    totals_covered: int = 0
    amount: Decimal = Decimal(0)
    amount_covered: Decimal = Decimal(0)
    excluded: int = 0

    @property
    def by_count(self) -> Decimal | None:
        """Доля статей с общим кодом; None — статей нет вовсе."""
        return None if not self.items else Decimal(self.items_covered) / Decimal(self.items)

    @property
    def by_amount(self) -> Decimal | None:
        """Доля величины статей, покрытая общими кодами."""
        return None if not self.amount else self.amount_covered / self.amount

    @property
    def totals_share(self) -> Decimal | None:
        """Доля итоговых строк, опознанных справочником."""
        return (
            None if not self.totals else Decimal(self.totals_covered) / Decimal(self.totals)
        )

    def describe(self) -> str:
        """Однострочная сводка со счётчиками проверенного."""
        count = "—" if self.by_count is None else f"{self.by_count:.1%}"
        amount = "—" if self.by_amount is None else f"{self.by_amount:.1%}"
        totals = "—" if self.totals_share is None else f"{self.totals_share:.1%}"
        return (
            f"статей {self.items}, из них общим кодом {self.items_covered} ({count}); "
            f"по сумме {amount}; итогов {self.totals}, опознано {totals}; "
            f"не статьи {self.excluded}"
        )


def _add(first: Coverage, second: Coverage) -> Coverage:
    """Складывает покрытие двух эмитентов или двух форм."""
    return Coverage(
        items=first.items + second.items,
        items_covered=first.items_covered + second.items_covered,
        totals=first.totals + second.totals,
        totals_covered=first.totals_covered + second.totals_covered,
        amount=first.amount + second.amount,
        amount_covered=first.amount_covered + second.amount_covered,
        excluded=first.excluded + second.excluded,
    )


@dataclass(frozen=True, slots=True)
class Frequency:
    """Сколько кодов встречается у скольких эмитентов."""

    issuers: int
    by_code: dict[str, int] = field(default_factory=dict)

    def at_least(self, count: int) -> int:
        """Сколько кодов встретилось не менее чем у стольких эмитентов."""
        return sum(1 for seen in self.by_code.values() if seen >= count)

    @property
    def many(self) -> int:
        """У скольких эмитентов код должен встретиться, чтобы считаться общим.

        Две трети набора, но не меньше двух: у набора из двух эмитентов
        «у большинства» означает «у обоих», и порог в четыре наблюдения
        давал бы ноль общих кодов при любом справочнике.
        """
        return max(2, min(self.issuers, (self.issuers * 2 + 2) // 3))

    def describe(self) -> str:
        """Разбивка по частоте: у большинства, у нескольких, у одного."""
        wide = self.at_least(self.many)
        few = self.at_least(2) - wide
        alone = self.at_least(1) - self.at_least(2)
        return (
            f"кодов всего {len(self.by_code)}: у {self.many} и более эмитентов "
            f"{wide}, у двух и более, но реже {few}, у одного {alone}"
        )


@dataclass(frozen=True, slots=True)
class IssuerCommonality:
    """Общность по одному эмитенту."""

    inn: str
    coverage: Coverage
    codes: frozenset[str]
    specific_share: Decimal | None
    atypical: bool

    def describe(self) -> str:
        """Строка для отчёта."""
        share = "—" if self.specific_share is None else f"{self.specific_share:.1%}"
        mark = ", нетипичная модель" if self.atypical else ""
        return f"{self.inn}: {self.coverage.describe()}; специфических статей {share}{mark}"


# Доля валюты баланса в статьях, которых нет ни у кого другого, при которой
# модель эмитента считается нетипичной. Основание — Автодор: «Задолженность
# Принципала» и «Затраты, осуществлённые в интересах Принципала» составляют
# около восьмидесяти процентов его активов, и это агентская модель, а не
# промышленная. Порог экспертный и грубый намеренно: он разделяет эмитента
# с несколькими своими статьями и эмитента, у которого своя вся отчётность.
ATYPICAL_SHARE = Decimal("0.30")


def commonality(
    issuers: list[IssuerMarkup], catalog: IfrsCatalog | None = None
) -> tuple[list[IssuerCommonality], Coverage, Frequency]:
    """Общность статей по эмитентам, в целом и по частоте кодов."""
    catalog = catalog or load_ifrs_lines()
    found = [_for_issuer(issuer, catalog) for issuer in issuers]
    overall = Coverage()
    for item in found:
        overall = _add(overall, item.coverage)
    seen: Counter[str] = Counter()
    for item in found:
        seen.update(item.codes)
    frequency = Frequency(issuers=len(found), by_code=dict(seen))
    logger.info("общность по %d эмитентам: %s", len(found), overall.describe())
    return found, overall, frequency


def without_atypical(
    found: list[IssuerCommonality],
) -> tuple[Coverage, Frequency]:
    """То же, но без эмитентов с нетипичной моделью.

    Считается в двух вариантах не для красоты: у эмитента, вся отчётность
    которого своя, общность занижена по существу дела, а не потому, что
    справочник узок. Какой из двух вариантов верен, решает не расчёт.
    """
    typical = [item for item in found if not item.atypical]
    overall = Coverage()
    for item in typical:
        overall = _add(overall, item.coverage)
    seen: Counter[str] = Counter()
    for item in typical:
        seen.update(item.codes)
    return overall, Frequency(issuers=len(typical), by_code=dict(seen))


def _for_issuer(issuer: IssuerMarkup, catalog: IfrsCatalog) -> IssuerCommonality:
    """Покрытие одного эмитента и признак нетипичной модели."""
    coverage = Coverage()
    codes: set[str] = set()
    for form in issuer.extraction.forms.values():
        part, seen = _for_form(issuer, form, catalog)
        coverage = _add(coverage, part)
        codes |= seen
    return IssuerCommonality(
        inn=issuer.inn,
        coverage=coverage,
        codes=frozenset(codes),
        specific_share=(share := _specific_share(issuer, catalog)),
        atypical=share is not None and share >= ATYPICAL_SHARE,
    )


def _for_form(
    issuer: IssuerMarkup, form, catalog: IfrsCatalog
) -> tuple[Coverage, set[str]]:
    """Покрытие одной формы: статьи, итоги, величины."""
    items = covered = totals = totals_covered = 0
    amount = covered_amount = Decimal(0)
    codes: set[str] = set()

    for value in form.values:
        position = catalog.get(value.code)
        if position is None or value.report_date != issuer.report_date:
            continue
        codes.add(value.code)
        if position.is_total:
            totals += 1
            totals_covered += 1
            continue
        items += 1
        covered += 1
        amount += abs(value.value)
        covered_amount += abs(value.value)

    for row in form.unrecognised:
        if row.key in issuer.dismissed:
            # Человек назвал строку не статьёй — в счёт она не идёт.
            continue
        assigned = _code_of(issuer, row.key)
        if assigned is not None:
            codes.add(assigned)
        position = catalog.get(assigned) if assigned else None
        if position is not None and position.is_total:
            totals += 1
            totals_covered += 1
            continue
        items += 1
        amount += abs(row.largest)
        if assigned is not None:
            covered += 1
            covered_amount += abs(row.largest)

    excluded = len(form.auto_dismissed) + sum(
        1 for row in form.unrecognised if row.key in issuer.dismissed
    )
    return (
        Coverage(
            items=items,
            items_covered=covered,
            totals=totals,
            totals_covered=totals_covered,
            amount=amount,
            amount_covered=covered_amount,
            excluded=excluded,
        ),
        codes,
    )


def _code_of(issuer: IssuerMarkup, key: tuple[str, int]) -> str | None:
    """Общий код строки, если человек его присвоил любым видом разметки."""
    if key in issuer.assignments:
        return issuer.assignments[key]
    if key in issuer.parts:
        return issuer.parts[key]
    aggregate = issuer.aggregates.get(key)
    return aggregate[0] if aggregate else None


def _specific_share(issuer: IssuerMarkup, catalog: IfrsCatalog) -> Decimal | None:
    """Доля валюты баланса в строках, оставшихся без общего кода.

    Это и есть машинный признак нетипичной модели: у Автодора «Задолженность
    Принципала» и «Затраты, осуществлённые в интересах Принципала» — около
    восьмидесяти процентов активов, и общим кодом их не разметить, потому что
    таких статей нет больше ни у кого. Признак считается по базе, а не
    объявляется в файле состава: объявленный описывал бы намерение.
    """
    assets = issuer.extraction.value_of("ifrs.total_assets", issuer.report_date)
    if assets is None or assets == 0:
        return None
    balance = issuer.extraction.forms.get("ifrs.statement_of_financial_position")
    if balance is None:
        return None
    # Считается сторона актива, а не весь баланс: строки обеих сторон вместе
    # дают двойную валюту баланса, и доля перевалила бы за сто процентов
    # у любого эмитента. Граница стороны — место строки «Итого активы».
    edge = balance.recognised_at.get("ifrs.total_assets")
    if edge is None:
        return None
    specific = sum(
        (
            abs(row.largest)
            for row in balance.unrecognised
            if row.index < edge
            and _code_of(issuer, row.key) is None
            and row.key not in issuer.dismissed
        ),
        Decimal(0),
    )
    return specific / abs(assets)
