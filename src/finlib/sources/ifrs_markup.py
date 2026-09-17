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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from enum import IntEnum, StrEnum
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


class Relation(StrEnum):
    """Чем строка приходится позиции справочника.

    Вид разметки — не оттенок, а разное отношение к справочнику, и от него
    зависит, как разметка проверяется арифметикой. Смешивать виды нельзя:
    детализация обязана суммироваться в позицию, агрегат — раскладываться
    на перечень, специфическая статья не сводится ни к чему.
    """

    EXACT = "exact"
    PART_OF = "part_of"
    AGGREGATE_OF = "aggregate_of"
    SPECIFIC = "specific"


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
    # Место строки в форме: по нему решение применяется именно к ней.
    index: int = 0
    # Итог, в состав которого строка предположительно входит, и его недостача.
    total_code: str | None = None
    total_gap: Decimal | None = None
    issuers: int = 1
    hints: tuple[Hint, ...] = ()
    # Соседние строки формы: без них «Прочие» и «Итого» не опознать,
    # а у строк без наименования это единственная опора.
    previous_name: str = ""
    next_name: str = ""

    @property
    def amount(self) -> Decimal:
        """Величина строки за отчётный период."""
        return self.values[0] if self.values else Decimal(0)

    @property
    def key(self) -> tuple[str, int]:
        """Устойчивый ключ строки — форма и место в ней."""
        return (self.form, self.index)

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
    # Все решения человека хранятся по ключу строки — форме и месту в ней.
    # По наименованию хранить нельзя: у части строк его нет вовсе, а «Прочие
    # расходы» встречаются в форме дважды. Из-за ключа по имени решение
    # применялось не к той строке либо не применялось вовсе, и строка
    # возвращалась в очередь с уже учтённой величиной.
    assignments: dict[tuple[str, int], str] = field(default_factory=dict)
    dismissed: dict[tuple[str, int], Decision] = field(default_factory=dict)
    # Строки, помеченные детализацией: ключ строки → код позиции, которую
    # они вместе составляют. Держатся отдельно от точных присвоений: в сумму
    # позиции они входят, а самой позицией не являются.
    parts: dict[tuple[str, int], str] = field(default_factory=dict)
    # Строки-агрегаты: ключ строки → позиции, которые строка укрупняет.
    aggregates: dict[tuple[str, int], tuple[str, ...]] = field(default_factory=dict)

    def decided(self, row: UnrecognisedRow) -> bool:
        """Решена ли строка — любым способом.

        Проверяются все виды разом: прежде очередь смотрела только точные
        присвоения и отказы, а детализация и агрегат в неё не попадали —
        строка оставалась в очереди, хотя её величина уже шла в итог.
        """
        return (
            row.key in self.assignments
            or row.key in self.dismissed
            or row.key in self.parts
            or row.key in self.aggregates
        )

    @property
    def report_date(self) -> date:
        """Отчётная дата комплекта."""
        return self.profile.report_dates[0]

    def values(self, catalog: IfrsCatalog) -> dict[str, Decimal]:
        """Величины по кодам с учётом присвоенного человеком."""
        found = dict(self.extraction.totals(self.report_date))
        for row in self.extraction.unrecognised:
            if not row.values:
                continue
            # Точное присвоение задаёт величину позиции; детализация к ней
            # прибавляется. Агрегат в сумму не идёт: он покрывает несколько
            # позиций сразу, и подставлять его в одну значило бы удвоить.
            code = self.assignments.get(row.key)
            if code is not None:
                found[code] = found.get(code, Decimal(0)) + row.values[0]
                continue
            part = self.parts.get(row.key)
            if part is not None and part not in found:
                found[part] = found.get(part, Decimal(0)) + row.values[0]
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
                normal_sign_of(catalog),
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
        if issuer.decided(row):
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
                index=row.index,
                share_of_assets=share,
                priority=priority,
                total_code=total_code,
                total_gap=gap,
                issuers=len(seen_by_name.get(normalize_name(row.source_name), {issuer.inn})),
                hints=hints_for(
                    row.source_name or row.previous_name,
                    catalog,
                    form=row.form,
                    section=_section_of(total_code, catalog),
                ),
                previous_name=row.previous_name,
                next_name=row.next_name,
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
            normal_sign_of(catalog),
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


def _section_of(code: str | None, catalog: IfrsCatalog) -> str | None:
    """Раздел позиции по её коду; None — код неизвестен."""
    if code is None:
        return None
    position = catalog.get(code)
    return position.section if position is not None else None


def hints_for(
    name: str,
    catalog: IfrsCatalog,
    form: str | None = None,
    section: str | None = None,
) -> tuple[Hint, ...]:
    """Ближайшие по написанию позиции ядра — из той же формы.

    Подсказка, а не решение: близость написания не означает совпадения
    смысла, и последнее слово за человеком.

    **Форма отсекает, раздел упорядочивает.** Близость написания сама по себе
    приводила к подсказкам не из той формы вовсе: для «Обязательства
    по договорам, кредиторская задолженность» предлагалась дебиторская
    задолженность, для «Результаты операционной деятельности» — потоки
    денежных средств, для «Налог на прибыль уплаченный» в ОДДС — расход
    по налогу из ОПУ. Это не близкий вариант, а заведомо неверный: строка
    баланса кодом ОПУ не размечается никогда. Раздел мягче — статья
    правомерно стоит не в том разделе, где её ждёшь, — поэтому он поднимает
    подсказку в списке, но чужие не убирает.
    """
    target = normalize_name(name)
    scored: list[tuple[bool, float, Hint]] = []
    for position in catalog.positions:
        if form is not None and position.form != form:
            continue
        ratio = max(
            SequenceMatcher(None, target, item).ratio() for item in position.match_names
        )
        if ratio >= HINT_MIN_RATIO:
            same_section = section is not None and position.section == section
            scored.append((same_section, ratio, Hint(position.code, position.name, ratio)))
    scored.sort(key=lambda item: (not item[0], -item[1]))
    return tuple(item[2] for item in scored[:HINT_COUNT])


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
    issuer.assignments[candidate.key] = code
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


def normal_sign_of(catalog: IfrsCatalog) -> Callable[[str], int]:
    """Нормальный знак позиции по её коду — подсказка для вывода знака."""

    def sign(code: str) -> int:
        position = catalog.get(code)
        return position.normal_sign if position is not None else 1

    return sign


def check_part_of(
    issuer: IssuerMarkup, code: str, catalog: IfrsCatalog
) -> tuple[bool | None, Decimal | None]:
    """Сходится ли сумма строк, помеченных детализацией, с величиной позиции.

    Проверка обязательна и определяет, принята ли гипотеза: если эмитент
    раскрывает статью подробнее модели, сумма его строк обязана равняться
    величине позиции. Не сошлось — гипотеза не подтверждена.

    `None` означает, что проверять нечем: сама позиция у эмитента
    не раскрыта, и сравнивать сумму не с чем. Это не то же, что «не сошлось».
    """
    declared = issuer.extraction.value_of(code, issuer.report_date)
    if declared is None:
        return None, None
    parts = [
        row.values[0]
        for row in issuer.extraction.unrecognised
        if issuer.parts.get(row.key) == code and row.values
    ]
    if not parts:
        return None, None
    total = sum(parts, start=Decimal(0))
    tolerance = abs(declared) * TOLERANCE_SHARE + Decimal(1)
    return abs(total - declared) <= tolerance, total


def known_codes(catalog: IfrsCatalog) -> dict[str, IfrsPosition]:
    """Коды ядра по коду — для проверки ввода."""
    return {item.code: item for item in catalog.positions}


_SAVED = """
SELECT code, inn, source_name, form_code, row_index, relation, related_codes
FROM ifrs_line_confirmation WHERE inn = ANY(%(inns)s)
"""

_TAKEN = """
SELECT code, inn, source_name FROM ifrs_line_confirmation WHERE code = %(code)s
"""

_FORGET = """
DELETE FROM ifrs_line_confirmation
WHERE inn = %(inn)s AND report_date = %(date)s
  AND (row_index = %(index)s OR (row_index IS NULL AND source_name = %(name)s))
"""


def restore(issuers: list[IssuerMarkup], conn=None) -> int:
    """Возвращает присвоения, сделанные в прежние присесты.

    Разметка идёт в несколько заходов, и показывать размеченное повторно
    нельзя. Восстановление не только убирает строку из очереди: присвоенный
    код участвует в суммах, и без него итоги считались бы незакрытыми —
    очередь выстроилась бы по недостаче, которой уже нет.
    """
    from finlib.db import fetch_all

    by_inn = {item.inn: item for item in issuers}
    if not by_inn:
        return 0
    rows = fetch_all(_SAVED, {"inns": list(by_inn)}, conn=conn)
    restored = 0
    for row in rows:
        issuer = by_inn.get(row["inn"])
        if issuer is None:
            continue
        key = _restore_key(issuer, row)
        if key is None:
            logger.warning(
                "разметка «%s» (%s) не восстановлена: строки нет в разборе",
                row["source_name"],
                row["inn"],
            )
            continue
        relation = row["relation"] or Relation.EXACT.value
        if relation == Relation.PART_OF.value:
            issuer.parts[key] = row["code"]
        elif relation == Relation.AGGREGATE_OF.value:
            issuer.aggregates[key] = tuple(row["related_codes"] or (row["code"],))
        elif relation == Relation.SPECIFIC.value:
            issuer.dismissed[key] = Decision.SPECIFIC
        else:
            issuer.assignments[key] = row["code"]
        restored += 1
    if restored:
        logger.info("восстановлено присвоений прежних присестов: %d", restored)
    return restored


def _restore_key(issuer: IssuerMarkup, row: dict) -> tuple[str, int] | None:
    """Ключ строки для восстановленной разметки.

    Индекс строки пишется с самого начала, но записи прежних сессий его
    не имеют: для них строка ищется по форме и наименованию, а при пустом
    имени — не ищется вовсе. Молчать об этом нельзя, иначе разметка тихо
    пропадёт и покажется заново.
    """
    if row.get("row_index") is not None:
        return (row["form_code"], int(row["row_index"]))
    for item in issuer.extraction.unrecognised:
        if item.form == row["form_code"] and item.source_name == row["source_name"]:
            return item.key
    return None


def code_is_taken(code: str, catalog: IfrsCatalog, conn=None) -> str | None:
    """Занят ли код; возвращает объяснение, чем именно занят.

    Код ядра для специфической статьи брать нельзя: ядро описывает то,
    что есть у всех, а специфическая статья — то, чего нет ни у кого
    другого. Код, уже присвоенный другой статье, тоже занят — иначе две
    разные вещи окажутся под одним кодом, и поднять их в ядро будет нельзя.
    """
    from finlib.db import fetch_all

    position = catalog.get(code)
    if position is not None:
        return f"это код ядра: {position.name}"
    rows = fetch_all(_TAKEN, {"code": code}, conn=conn)
    names = {row["source_name"] for row in rows}
    if len(names) > 1 or (names and code not in {item for item in names}):
        listed = ", ".join(sorted(names)[:3])
        return f"код уже присвоен статье: {listed}"
    return None


_LAST = """
SELECT inn, source_name, code, form_code, row_index FROM ifrs_line_confirmation
WHERE inn = ANY(%(inns)s) ORDER BY confirmed_at DESC, id DESC LIMIT 1
"""


def last_confirmation(
    issuers: list[IssuerMarkup], conn=None
) -> tuple[str, str, tuple[str, int]] | None:
    """Последнее присвоение по журналу: эмитент, наименование, код.

    Отмена не ограничена текущим присестом: ошибку замечают и через день,
    а править журнал руками неудобно и опасно.
    """
    from finlib.db import fetch_all

    rows = fetch_all(_LAST, {"inns": [item.inn for item in issuers]}, conn=conn)
    if not rows:
        return None
    row = rows[0]
    by_inn = {item.inn: item for item in issuers}
    issuer = by_inn.get(row["inn"])
    key = _restore_key(issuer, row) if issuer is not None else None
    return row["inn"], row["source_name"], key or (row["form_code"], -1)


def forget(
    issuer: IssuerMarkup, key: tuple[str, int], source_name: str = "", conn=None
) -> None:
    """Отменяет разметку: убирает из памяти и из журнала подтверждений.

    Отменяются все виды разом — точное присвоение, детализация, агрегат,
    отказ: человек отменяет решение о строке, а не одну из его форм.
    Влияние на итоги снимается вместе с записью, потому что итоги считаются
    по этим же словарям.

    Ошибка на двух сотнях строк неизбежна, а править потом в базе руками
    неудобно и опасно: исправление тем и отличается от второго наблюдения,
    что старую запись надо убрать, а не добавить рядом.
    """
    from finlib.db import execute

    issuer.assignments.pop(key, None)
    issuer.dismissed.pop(key, None)
    issuer.parts.pop(key, None)
    issuer.aggregates.pop(key, None)
    execute(
        _FORGET,
        {
            "inn": issuer.inn,
            "index": key[1],
            "name": source_name,
            "date": issuer.report_date,
        },
        conn=conn,
    )
