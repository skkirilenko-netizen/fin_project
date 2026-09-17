"""Разметка неопознанных строк: подготовка кандидатов и проверка присвоений.

Справочник статей МСФО расширяется не по частоте наименований, а **по влиянию
на арифметику**. Система знает, какого слагаемого не хватает, чтобы сошёлся
итог раздела, и это даёт двойную проверку: присвоил код — итог сошёлся,
значит опознал верно; присвоил неверно — не сойдётся. Частота такой проверки
не даёт вовсе.

Порядок показа человеку:

1. строки, участвующие в несошедшихся итогах, по величине вклада;
2. строки сверх порога существенности, не участвующие в итогах;
3. остальные по частоте у разных эмитентов.

Ввод-вывода здесь нет: модуль готовит кандидатов и проверяет присвоения,
а терминальный разговор ведёт CLI. Так разметку можно прогнать и без
человека — в тестах.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from enum import IntEnum
from pathlib import Path

from finlib.normalize.ifrs_lines import IfrsCatalog, IfrsPosition, load_ifrs_lines
from finlib.normalize.lines import normalize_name
from finlib.quality.totals import TotalVerdict, check_total
from finlib.sources.ifrs_extract import Extraction, UnrecognisedRow, extract
from finlib.sources.ifrs_inbox import DocumentProfile, Rejection, identify, text_of

logger = logging.getLogger(__name__)

# Допуск сходимости при разметке: доля итога. Разметка ищет недостающие
# слагаемые, а не проверяет отчётность, поэтому допуск шире контрольного —
# округление последней цифры не должно выглядеть незакрытым итогом.
TOLERANCE_SHARE = Decimal("0.0001")

# Сколько подсказок показывать и насколько близким должно быть написание.
HINT_COUNT = 5
HINT_MIN_RATIO = 0.45


class Priority(IntEnum):
    """Очерёдность показа: чем меньше, тем раньше."""

    BREAKS_TOTAL = 1
    MATERIAL = 2
    OTHER = 3


class Decision(IntEnum):
    """Что человек сделал со строкой."""

    ASSIGNED = 1
    NOT_A_LINE = 2
    SPECIFIC = 3


@dataclass(frozen=True, slots=True)
class Hint:
    """Подсказка: позиция ядра, близкая по написанию."""

    code: str
    name: str
    ratio: float


@dataclass
class Candidate:
    """Неопознанная строка со всем, что нужно человеку для решения."""

    inn: str
    form: str
    source_name: str
    values: tuple[Decimal, ...]
    share_of_assets: Decimal | None
    priority: Priority
    # Итог, в состав которого строка предположительно входит, и его недостача.
    total_code: str | None = None
    total_gap: Decimal | None = None
    issuers: int = 1
    hints: tuple[Hint, ...] = ()

    @property
    def amount(self) -> Decimal:
        """Величина строки за отчётный период."""
        return self.values[0] if self.values else Decimal(0)

    def describe(self) -> str:
        """Однострочное описание для списка."""
        share = f"{self.share_of_assets:.1%}" if self.share_of_assets else "—"
        total = f", в итоге {self.total_code}" if self.total_code else ""
        return f"{self.source_name} — {self.amount} ({share} активов){total}"


@dataclass
class IssuerMarkup:
    """Разметка одного эмитента: извлечение, присвоения и состояние итогов."""

    inn: str
    path: Path
    profile: DocumentProfile
    extraction: Extraction
    assignments: dict[str, str] = field(default_factory=dict)
    dismissed: dict[str, Decision] = field(default_factory=dict)

    @property
    def report_date(self) -> date:
        """Отчётная дата комплекта."""
        return self.profile.report_dates[0]

    def values(self, catalog: IfrsCatalog) -> dict[str, Decimal]:
        """Величины по кодам с учётом присвоенного человеком."""
        found = dict(self.extraction.totals(self.report_date))
        for row in self.extraction.unrecognised:
            code = self.assignments.get(row.source_name)
            if code is None or not row.values:
                continue
            found[code] = found.get(code, Decimal(0)) + row.values[0]
        return found

    def totals_state(self, catalog: IfrsCatalog) -> dict[str, TotalVerdict]:
        """Что с итогами сейчас: сошлись, не сошлись, проверять нечем."""
        values = self.values(catalog)
        state: dict[str, TotalVerdict] = {}
        for total in catalog.totals():
            outcome = check_total(
                total,
                values.get,
                lambda code: None,
                lambda amount: abs(amount) * TOLERANCE_SHARE + Decimal(1),
            )
            state[total.code] = outcome.verdict
        return state


def load_issuer(path: Path, inn: str) -> IssuerMarkup | Rejection:
    """Готовит эмитента к разметке: приём, разбор, ничего в базу."""
    document = text_of(path)
    if not document.readable:
        from finlib.quality.codes import CheckCode

        return Rejection(CheckCode.FILE_NOT_PARSED, f"файл не прочитан: {document.error}")
    profile = identify(document.text)
    if isinstance(profile, Rejection):
        return profile
    extraction = extract(document.text, profile.report_dates, profile.grouping)
    return IssuerMarkup(inn, path, profile, extraction)


def candidates(
    issuers: list[IssuerMarkup], catalog: IfrsCatalog | None = None
) -> list[Candidate]:
    """Неопознанные строки всех эмитентов в порядке влияния на арифметику."""
    catalog = catalog or load_ifrs_lines()
    seen_by_name: dict[str, set[str]] = {}
    for issuer in issuers:
        for row in issuer.extraction.unrecognised:
            seen_by_name.setdefault(normalize_name(row.source_name), set()).add(issuer.inn)

    found: list[Candidate] = []
    for issuer in issuers:
        found.extend(_for_issuer(issuer, catalog, seen_by_name))

    found.sort(
        key=lambda item: (
            item.priority,
            -abs(item.amount) if item.priority is Priority.BREAKS_TOTAL else 0,
            -(item.share_of_assets or Decimal(0)),
            -item.issuers,
            item.source_name,
        )
    )
    return found


def _for_issuer(
    issuer: IssuerMarkup, catalog: IfrsCatalog, seen_by_name: dict[str, set[str]]
) -> list[Candidate]:
    """Кандидаты одного эмитента с привязкой к незакрытым итогам."""
    assets = issuer.extraction.value_of("ifrs.total_assets", issuer.report_date)
    threshold = catalog.materiality.share_of_total_assets
    broken = _unbalanced_totals(issuer, catalog)

    found: list[Candidate] = []
    for row in issuer.extraction.unrecognised:
        if row.source_name in issuer.assignments or row.source_name in issuer.dismissed:
            continue
        share = (
            abs(row.largest) / abs(assets) if assets not in (None, Decimal(0)) else None
        )
        total_code, gap = _belongs_to(row, issuer, broken, catalog)
        if total_code is not None:
            priority = Priority.BREAKS_TOTAL
        elif share is not None and share >= threshold:
            priority = Priority.MATERIAL
        else:
            priority = Priority.OTHER
        found.append(
            Candidate(
                inn=issuer.inn,
                form=row.form,
                source_name=row.source_name,
                values=row.values,
                share_of_assets=share,
                priority=priority,
                total_code=total_code,
                total_gap=gap,
                issuers=len(seen_by_name.get(normalize_name(row.source_name), {issuer.inn})),
                hints=hints_for(row.source_name, catalog),
            )
        )
    return found


def _unbalanced_totals(
    issuer: IssuerMarkup, catalog: IfrsCatalog
) -> dict[str, Decimal]:
    """Несошедшиеся итоги и их недостача: сколько не хватает до суммы."""
    values = issuer.values(catalog)
    broken: dict[str, Decimal] = {}
    for total in catalog.totals():
        outcome = check_total(
            total,
            values.get,
            lambda code: None,
            lambda amount: abs(amount) * TOLERANCE_SHARE + Decimal(1),
        )
        if outcome.verdict is TotalVerdict.MISMATCHED and outcome.difference is not None:
            # Недостача положительна, когда сумма состава меньше итога:
            # именно столько ищется в неопознанных строках.
            broken[total.code] = -outcome.difference
    return broken


def _belongs_to(
    row: UnrecognisedRow,
    issuer: IssuerMarkup,
    broken: dict[str, Decimal],
    catalog: IfrsCatalog,
) -> tuple[str | None, Decimal | None]:
    """К какому незакрытому итогу строка относится.

    Итог берётся **из той же формы**: строка отчёта о прибыли или убытке
    не входит в итог баланса ни при каком составе. Первая редакция брала
    первый попавшийся незакрытый итог, и «Прочая выручка» приписывалась
    к итогу внеоборотных активов.

    Среди итогов своей формы выбирается тот, чья недостача ближе к величине
    строки: если строки не хватает ровно на эту сумму, она и есть искомое
    слагаемое. Точнее сказать нельзя — состав итога и есть то, что человек
    уточняет разметкой.
    """
    amount = abs(row.values[0]) if row.values else Decimal(0)
    same_form = {
        code: gap
        for code, gap in broken.items()
        if (position := catalog.get(code)) is not None and position.form == row.form
    }
    if not same_form:
        return None, None
    best = min(same_form, key=lambda code: abs(abs(same_form[code]) - amount))
    return best, same_form[best]


def hints_for(name: str, catalog: IfrsCatalog) -> tuple[Hint, ...]:
    """Ближайшие по написанию позиции ядра.

    Подсказка, а не решение: близость написания не означает совпадения
    смысла, и последнее слово за человеком.
    """
    target = normalize_name(name)
    scored: list[Hint] = []
    for position in catalog.positions:
        ratio = max(
            SequenceMatcher(None, target, item).ratio() for item in position.match_names
        )
        if ratio >= HINT_MIN_RATIO:
            scored.append(Hint(position.code, position.name, ratio))
    scored.sort(key=lambda item: -item.ratio)
    return tuple(scored[:HINT_COUNT])


def apply_assignment(
    issuer: IssuerMarkup,
    candidate: Candidate,
    code: str,
    catalog: IfrsCatalog | None = None,
) -> tuple[bool, str | None]:
    """Присваивает код и проверяет, сошёлся ли затронутый итог.

    Возвращает, сошёлся ли итог после присвоения, и код этого итога.
    Сошедшийся итог — подтверждение правильности: неверный код сумму
    не закроет.
    """
    catalog = catalog or load_ifrs_lines()
    before = issuer.totals_state(catalog)
    issuer.assignments[candidate.source_name] = code
    after = issuer.totals_state(catalog)

    closed = [
        total
        for total, verdict in after.items()
        if verdict is TotalVerdict.MATCHED and before.get(total) is not TotalVerdict.MATCHED
    ]
    if closed:
        logger.info("после присвоения %s сошёлся итог %s", code, ", ".join(closed))
        return True, closed[0]
    return False, candidate.total_code


def known_codes(catalog: IfrsCatalog) -> dict[str, IfrsPosition]:
    """Коды ядра по коду — для проверки ввода."""
    return {item.code: item for item in catalog.positions}
