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
from finlib.quality.totals import (
    Composition,
    TotalCheck,
    TotalVerdict,
    check_total,
    with_extra,
)
from finlib.sources.cbonds import other_shares
from finlib.sources.ifrs_claims import Claim, Fold, fold
from finlib.sources.ifrs_extract import (
    Extraction,
    UnrecognisedRow,
    extract,
    nearest_total_below,
    share_of_assets,
)
from finlib.sources.ifrs_inbox import DocumentProfile, Rejection, identify, text_of
from finlib.sources.ifrs_numbers import Grouping

logger = logging.getLogger(__name__)

# Допуск сходимости при разметке: доля итога. Разметка ищет недостающие
# слагаемые, а не проверяет отчётность, поэтому допуск шире контрольного —
# округление последней цифры не должно выглядеть незакрытым итогом.
TOLERANCE_SHARE = Decimal("0.0001")

# Сколько подсказок показывать и насколько близким должно быть написание.
HINT_COUNT = 5
HINT_MIN_RATIO = 0.45
# Порог для кодов, уже подтверждённых у другого эмитента: ниже общего.
# Их немного, и пропустить такой код дороже, чем показать лишний.
HINT_MIN_RATIO_CONFIRMED = 0.35


class Priority(IntEnum):
    """Очерёдность показа: чем меньше, тем раньше.

    `IN_CBONDS_OTHER` — строка, которую внешний источник не различает:
    она попала в его «прочие», и величина её существенна. Такая строка
    стоит первой, потому что размечать имеет смысл ровно то, чего нет
    больше нигде: основные формы Cbonds отдаёт нормализованными, а всё,
    что свёрнуто в «прочие», есть только в PDF. У Автодора так свёрнуто
    86 % валюты баланса, у ЛСР — 0,3 %, и разметка нужна им в разной мере.
    """

    IN_CBONDS_OTHER = 1
    BREAKS_TOTAL = 2
    MATERIAL = 3
    OTHER = 4


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
    # Строка вообще не статья: колонтитул, номер страницы, промежуточный
    # итог, уже учтённый составом. Решение хранится наравне с прочими —
    # иначе оно живёт один присест, строка возвращается в очередь, и её
    # размечают снова, на этот раз, может быть, неверно.
    NOT_A_LINE = "not_a_line"


# Код, под которым хранится решение «не статья». Позиции у него нет и быть
# не может, поэтому из перечня доступных кодов он исключается: это отметка
# об отсутствии статьи, а не статья.
NOT_A_LINE_CODE = "ifrs.not_a_line"


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
    # Насколько строка велика в своей форме: статья баланса — доля валюты
    # баланса, строка отчёта о прибылях — доля выручки, поток — прочерк.
    # Это мера для экрана и для порядка очереди, **и она не доля активов**:
    # прежде поле называлось `share_of_assets`, и у строки ОПУ в графу
    # «доля активов» журнала подтверждений попадала доля выручки.
    relative_size: Decimal | None
    priority: Priority
    # Место строки в форме: по нему решение применяется именно к ней.
    index: int = 0
    # Отчётная дата комплекта, из которого строка пришла. Одного ИНН мало:
    # у эмитента комплектов сколько угодно, и решение по строке годового
    # комплекта, применённое к промежуточному, — тихое присвоение чужого кода.
    report_date: date | None = None
    # Итог, в состав которого строка предположительно входит, и его недостача.
    total_code: str | None = None
    total_gap: Decimal | None = None
    issuers: int = 1
    hints: tuple[Hint, ...] = ()
    # Соседние строки формы: без них «Прочие» и «Итого» не опознать,
    # а у строк без наименования это единственная опора.
    previous_name: str = ""
    next_name: str = ""
    # Доля строки в валюте баланса — мера существенности из методики, та же
    # самая, по которой решает экран сверки. Считается одной функцией
    # (`ifrs_extract.share_of_assets`) и в журнал подтверждений идёт она.
    share_of_assets: Decimal = Decimal(0)

    @property
    def amount(self) -> Decimal:
        """Величина строки за отчётный период."""
        return self.values[0] if self.values else Decimal(0)

    @property
    def key(self) -> tuple[str, int]:
        """Устойчивый ключ строки — форма и место в ней."""
        return (self.form, self.index)

    @property
    def issuer_key(self) -> tuple[str, date | None]:
        """Ключ комплекта, которому строка принадлежит: ИНН и отчётная дата."""
        return (self.inn, self.report_date)

    def describe(self) -> str:
        """Однострочное описание для списка."""
        share = f"{self.relative_size:.1%}" if self.relative_size else "—"
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
    # Специфические статьи: ключ строки → код, заведённый человеком. Код
    # хранится, а не только признак «специфическая»: без него величина
    # не попадает в итог раздела, и разметка не двигает арифметику.
    specific: dict[tuple[str, int], str] = field(default_factory=dict)

    def rejects(self, catalog: IfrsCatalog) -> dict[tuple[str, int], str]:
        """Присвоения, отклонённые правилом формы и раздела.

        Правило «статья чужого раздела не опознаётся вовсе» действовало
        у автомата и не действовало у человека: размечая строку руками,
        коду оборотных активов можно было отдать строку из внеоборотных.
        У ЛСР так и вышло — дебиторская задолженность 1 410 из внеоборотных
        легла к оборотной, и оба итога разошлись ровно на неё. Правило одно
        на обе стороны, и живёт оно в `ifrs_claims`.

        Отклонённая строка не пропадает: она возвращается в очередь
        разметки — отказ означает «мы не знаем, чем эта строка является»,
        а не «этой строки нет».
        """
        found: dict[tuple[str, int], str] = {}
        for row in self.extraction.unrecognised:
            code = (
                self.assignments.get(row.key)
                or self.parts.get(row.key)
                or (self.aggregates.get(row.key) or (None,))[0]
            )
            if code is None:
                continue
            position = catalog.get(code)
            if position is None:
                # Код подтверждённой специфической статьи: позиции
                # в справочнике у него нет, и форму с разделом взять неоткуда.
                continue
            outcome = fold(position, [self.claim(row, catalog, position)])
            if outcome.kind is Fold.NONE:
                found[row.key] = outcome.reason
        return found

    def claim(
        self, row: UnrecognisedRow, catalog: IfrsCatalog, position: IfrsPosition
    ) -> Claim:
        """Притязание строки разметки: где она стоит и что несёт.

        У итоговой строки раздел не спрашивается, и это не послабление:
        раздел определяется ближайшим итогом **ниже** строки, а ниже итога
        стоит итог объемлющий — «Итого краткосрочные обязательства» так
        оказывалось бы строкой раздела «Итого обязательства». Итог, отданный
        не тому разделу, ловится арифметикой: неверный состав не сойдётся.
        """
        return Claim(
            row.source_name,
            row.values,
            row.form,
            None if position.is_total else _section_at(row, self, catalog),
            row.key,
        )

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
        """Величины по кодам с учётом присвоенного человеком.

        **Что стоит в форме, то входит в итог своего раздела** — и различаются
        виды разметки не этим, а тем, где именно величина уже посчитана.

        | Вид | Идёт ли в итог |
        |---|---|
        | точное присвоение | да, задаёт величину позиции |
        | детализация позиции **без своей строки** | да, части составляют её |
        | детализация позиции **со своей строкой** | нет, она уже внутри неё |
        | агрегат | да, кладётся на первую из покрываемых позиций |
        | специфическая статья | да, но через `extras`: кода в справочнике нет |

        Детализация раскрытой позиции — единственный случай, когда величина
        в итог не идёт, и это не изъян режима, а его смысл: строка уже учтена
        той строкой, частью которой объявлена. У ФосАгро «Прочие внеоборотные
        активы» раскрыты величиной 90, и три строки на 23 823, помеченные их
        детализацией, детализацией не являются — сумма не сходится, и
        `check_part_of` это говорит.

        Детализация складывается **целиком**: прежде бралась первая часть,
        а остальные молча терялись, и недостача не двигалась, сколько строк
        ни размечай.

        Агрегат кладётся на первую из покрываемых позиций: разложить его
        по нескольким нечем — разложения в отчётности нет, — а величина
        в итоге нужна ровно одна.
        """
        disclosed = self.extraction.totals(self.report_date)
        found = dict(disclosed)
        details: dict[str, Decimal] = {}
        rejected = self.rejects(catalog)
        for row in self.extraction.unrecognised:
            if not row.values or row.key in rejected:
                continue
            code = self.assignments.get(row.key) or (
                self.aggregates.get(row.key) or (None,)
            )[0]
            if code is not None:
                found[code] = found.get(code, Decimal(0)) + row.values[0]
                continue
            part = self.parts.get(row.key)
            if part is not None:
                details[part] = details.get(part, Decimal(0)) + row.values[0]
        for code, amount in details.items():
            # Детализация идёт в итог только тогда, когда своей строки
            # у позиции нет вовсе — ни опознанной справочником, ни присвоенной
            # человеком. Прежде смотрели только на справочник, и у ФосАгро
            # «Прибыль за отчетный год» 114 243 складывалась со своей же
            # разбивкой по акционерам: итог выходил ровно вдвое больше.
            if code not in found:
                found[code] = amount
        return found

    def extras(self, catalog: IfrsCatalog) -> dict[str, Decimal]:
        """Величины, которые входят в итог раздела помимо позиций справочника.

        Специфическая статья стоит в форме и в итог раздела входит, но кода
        справочника у неё нет, и состав итога о ней не знает. Без этого
        недостача не двигалась после разметки: у ФосАгро итог краткосрочных
        обязательств не замечал «Дивиденды к уплате» (11 135), а итог
        оборотных активов — «Налог на прибыль к возмещению» (11 881).

        Раздел берётся по месту строки — ближайшему итогу ниже неё.
        """
        found: dict[str, Decimal] = {}
        for row in self.extraction.unrecognised:
            code = self.specific.get(row.key)
            if code is None or not row.values:
                continue
            total = _total_below(row, self, catalog)
            if total is None:
                continue
            found[total] = found.get(total, Decimal(0)) + row.values[0]
        return found

    def totals_state(self, catalog: IfrsCatalog) -> dict[str, TotalVerdict]:
        """Что с итогами сейчас: сошлись, не сошлись, проверять нечем."""
        return {
            code: outcome.verdict for code, outcome in self.totals(catalog).items()
        }

    def totals(self, catalog: IfrsCatalog) -> dict[str, TotalCheck]:
        """Сверка каждого итога с учётом разметки и специфических статей."""
        values = self.values(catalog)
        extras = self.extras(catalog)
        found: dict[str, TotalCheck] = {}
        for total in catalog.totals():
            extra = extras.get(total.code, Decimal(0))
            found[total.code] = best_composition(
                total, values, catalog, extra, TOLERANCE_SHARE
            )
        return found


def load_issuer(
    path: Path, inn: str, grouping: Grouping | None = None
) -> IssuerMarkup | Rejection:
    """Готовит эмитента к разметке: приём, разбор, ничего в базу.

    `grouping` задаёт разделитель разрядов вручную — для документа, у которого
    он не читается ни голосованием, ни арифметикой.

    Валюта разметке безразлична: состав статей от неё не зависит, и отчётность
    в долларах размечается ровно так же. Отказ по валюте остаётся там, где
    считаются рублёвые показатели.
    """
    document = text_of(path)
    if not document.readable:
        from finlib.quality.codes import CheckCode

        return Rejection(CheckCode.FILE_NOT_PARSED, f"файл не прочитан: {document.error}")
    profile = identify(
        document.text, grouping=grouping, any_currency=True, document=document
    )
    if isinstance(profile, Rejection):
        return profile
    extraction = extract(
        document.text,
        profile.dates_by_form,
        profile.grouping,
        columns=document.columns_of,
        layouts=profile.columns_by_form,
    )
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

    confirmed = confirmed_names()
    found: list[Candidate] = []
    for issuer in issuers:
        found.extend(_for_issuer(issuer, catalog, seen_by_name, confirmed))

    found.sort(
        key=lambda item: (
            item.priority,
            -abs(item.amount) if item.priority is Priority.BREAKS_TOTAL else 0,
            -(item.relative_size or Decimal(0)),
            -item.issuers,
            item.source_name,
        )
    )
    return found


def _for_issuer(
    issuer: IssuerMarkup,
    catalog: IfrsCatalog,
    seen_by_name: dict[str, set[str]],
    confirmed: tuple[tuple[str, str], ...] = (),
) -> list[Candidate]:
    """Кандидаты одного эмитента с привязкой к незакрытым итогам."""
    assets = issuer.extraction.value_of("ifrs.total_assets", issuer.report_date)
    revenue = issuer.extraction.value_of("ifrs.revenue", issuer.report_date)
    threshold = catalog.materiality.share_of_total_assets
    broken = _unbalanced_totals(issuer, catalog)
    hidden = other_shares(issuer.inn, issuer.report_date)

    # Присвоение, отклонённое правилом формы и раздела, разметкой не является:
    # строка возвращается в очередь, иначе отказ был бы тихой потерей.
    rejected = issuer.rejects(catalog)

    found: list[Candidate] = []
    for row in issuer.extraction.unrecognised:
        if issuer.decided(row) and row.key not in rejected:
            continue
        share = _relative_size(row, issuer, assets, revenue)
        total_code, gap = _belongs_to(row, issuer, broken, catalog)
        # Раздел берётся от места строки, а не от привязки к несошедшемуся
        # итогу: итог мог сойтись, а строка всё равно стоит в своём разделе.
        section = _section_at(row, issuer, catalog) or _section_of(total_code, catalog)
        # Строка, попавшая в «прочие» внешнего источника: её нет нигде, кроме
        # PDF. Разметка нужна прежде всего ей.
        #
        # Порог здесь двойной, и оба нужны. Само «прочее» раздела обязано быть
        # существенным по валюте баланса — иначе там нечего размечать: у ЛСР
        # это от трёх десятых процента до двух, и раздел закрыт внешним
        # источником целиком. А строка обязана быть существенной **внутри
        # прочего**, а не по валюте баланса: у Сегежи прочие внеоборотные
        # активы — тридцать процентов баланса, и строка в полтора процента
        # валюты составляет двадцатую часть того, чего не видно вовсе.
        bucket = hidden.get(section) if section else None
        in_other = (
            bucket is not None
            and bucket.share_of_assets >= threshold
            and bucket.amount != 0
            and abs(row.largest) / abs(bucket.amount) >= threshold
        )
        if in_other:
            priority = Priority.IN_CBONDS_OTHER
        elif total_code is not None:
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
                report_date=issuer.report_date,
                relative_size=share,
                share_of_assets=share_of_assets(row, assets),
                priority=priority,
                total_code=total_code,
                total_gap=gap,
                issuers=len(seen_by_name.get(normalize_name(row.source_name), {issuer.inn})),
                hints=hints_for(
                    row.source_name or row.previous_name,
                    catalog,
                    form=row.form,
                    section=_section_of(total_code, catalog),
                    confirmed=confirmed,
                ),
                previous_name=row.previous_name,
                next_name=row.next_name,
            )
        )
    return found


def best_composition(
    total: IfrsPosition,
    values: dict[str, Decimal],
    catalog: IfrsCatalog,
    extra: Decimal = Decimal(0),
    tolerance_share: Decimal = TOLERANCE_SHARE,
) -> TotalCheck:
    """Сверка итога по лучшему из его составов.

    Эмитенты раскрывают отчёт о прибылях по-разному, и промежуточной строки
    может не быть вовсе: у Сегежи валовой прибыли нет, операционный убыток
    набирается прямо из выручки и расходов. Один состав на всех означал бы,
    что у такого эмитента арифметика ОПУ не проверяется ничем.

    Лучшим считается сошедшийся, а среди несошедшихся — тот, чьё расхождение
    меньше: он и показывает, какого слагаемого недостаёт. Подбора здесь нет —
    составы объявлены методикой поимённо, а не перебираются.
    """
    outcomes = [
        with_extra(
            check_total(
                Composition(total.code, group),
                values.get,
                lambda code: None,
                lambda amount: abs(amount) * tolerance_share + Decimal(1),
                normal_sign_of(catalog),
            ),
            extra,
        )
        for group in total.compositions
    ]
    matched = next(
        (item for item in outcomes if item.verdict is TotalVerdict.MATCHED), None
    )
    if matched is not None:
        return matched
    return min(
        outcomes,
        key=lambda item: abs(item.difference) if item.difference is not None else _FAR,
    )


# Заведомо большее расхождение, чем любое настоящее: им помечается исход,
# у которого расхождения нет вовсе — сравнивать его с числом нельзя.
_FAR = Decimal("1e30")


def _unbalanced_totals(
    issuer: IssuerMarkup, catalog: IfrsCatalog
) -> dict[str, Decimal]:
    """Несошедшиеся итоги и их недостача: сколько не хватает до суммы."""
    broken: dict[str, Decimal] = {}
    for code, outcome in issuer.totals(catalog).items():
        if outcome.verdict is TotalVerdict.MISMATCHED and outcome.difference is not None:
            # Недостача положительна, когда сумма состава меньше итога:
            # именно столько ищется в неопознанных строках.
            broken[code] = -outcome.difference
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

    **Решает положение строки, а не близость недостачи.** В отчётности
    по МСФО слагаемые стоят над своим итогом, и строка входит в ближайший
    итог **ниже** себя: итог закрывает раздел, и всё, что стоит после него,
    к нему уже не относится. Прежде выбирался итог с ближайшей по величине
    недостачей, и строка попадала куда угодно: «Всего активов» приписывалось
    к итогу оборотных активов, «Резервы» — к итогу капитала, а у Автодора
    «Затраты, осуществлённые в интересах Принципала» уходили в итог
    внеоборотных активов, хотя стоят после него, — недостача там становилась
    отрицательной и росла с каждой новой строкой.

    **Итог из одних итогов строк не принимает.** Состав «Итого активы» —
    два итога разделов, и отдельная статья в него входит только через свой
    раздел. То же у «Итого капитал и обязательства». Это не перечень
    исключений, а свойство состава, поэтому и проверяется по составу.

    Близость недостачи остаётся запасным правилом — на случай, когда ни один
    итог формы не опознан по наименованию и положения его в таблице мы
    не знаем.
    """
    amount = abs(row.values[0]) if row.values else Decimal(0)
    same_form = {
        code: gap
        for code, gap in broken.items()
        if (position := catalog.get(code)) is not None
        and position.form == row.form
        and not _totals_only(code, catalog)
    }
    if not same_form:
        return None, None

    places = issuer.extraction.forms[row.form].recognised_at
    below = {code: places[code] for code in same_form if places.get(code, -1) > row.index}
    if below:
        nearest = min(below, key=lambda code: below[code])
        return nearest, same_form[nearest]

    if places:
        # Места итогов в форме известны, но ниже строки их нет: раздел,
        # к которому она относится, итогом не закрыт. Приписать её к чужому
        # разделу хуже, чем не приписать ни к какому. У Сегежи разбивка
        # убытка по акционерам стоит последними строками отчёта о прибылях,
        # и запасное правило приписывало её к валовой прибыли — единственному
        # незакрытому итогу, место которого в форме неизвестно, потому что
        # такой строки у Сегежи нет вовсе.
        return None, None

    best = min(same_form, key=lambda code: abs(abs(same_form[code]) - amount))
    return best, same_form[best]


def _totals_only(code: str, catalog: IfrsCatalog) -> bool:
    """Состоит ли итог из одних итогов — тогда отдельных статей он не берёт."""
    position = catalog.get(code)
    if position is None or not position.components:
        return False
    return all(
        (item := catalog.get(component.code)) is not None and item.is_total
        for component in position.components
    )


def _relative_size(
    row: UnrecognisedRow,
    issuer: IssuerMarkup,
    assets: Decimal | None,
    revenue: Decimal | None,
) -> Decimal | None:
    """Насколько строка велика — относительно того, с чем её сравнивают.

    **Мера у каждой формы своя.** Статья баланса соизмеряется с валютой
    баланса, строка отчёта о прибылях — с выручкой: себестоимость ФосАгро
    как «48,89 % активов» не значит ничего и при разметке сбивает.
    У отчёта о движении денежных средств такой меры нет вовсе — поток
    за период не доля ни от запаса, ни от оборота, — и вместо числа стоит
    прочерк.
    """
    if row.form == "ifrs.statement_of_profit_or_loss":
        base = revenue
    elif row.form == "ifrs.statement_of_financial_position":
        base = assets
    else:
        return None
    if base is None or base == 0:
        return None
    return abs(row.largest) / abs(base)


def _total_places(
    issuer: IssuerMarkup, form: str, catalog: IfrsCatalog
) -> dict[str, int]:
    """Места итогов в форме: опознанных справочником и размеченных человеком.

    Разметка человека входит наравне с опознанием: у Норникеля итоги обоих
    разделов обязательств не подписаны вовсе и опознаны руками. Без них
    ближайшего итога ниже у строк пассива не находилось, и раздел определялся
    по итогу из чужого места — то есть неверно.
    """
    places = {
        code: place
        for code, place in issuer.extraction.forms[form].recognised_at.items()
        if (position := catalog.get(code)) is not None and position.is_total
    }
    for (row_form, index), code in issuer.assignments.items():
        position = catalog.get(code)
        if row_form == form and position is not None and position.is_total:
            places.setdefault(code, index)
    return places


def _total_below(
    row: UnrecognisedRow, issuer: IssuerMarkup, catalog: IfrsCatalog
) -> str | None:
    """Код ближайшего итога ниже строки — итога её раздела.

    Правило берётся из одного места (`ifrs_extract.nearest_total_below`):
    им же разбор разводит одинаковые наименования, и два выражения одного
    правила однажды разошлись бы.
    """
    return nearest_total_below(row.index, _total_places(issuer, row.form, catalog))


def _section_at(
    row: UnrecognisedRow, issuer: IssuerMarkup, catalog: IfrsCatalog
) -> str | None:
    """Раздел, в котором стоит строка: по ближайшему итогу ниже неё.

    То же правило, по которому строится иерархия итогов: в МСФО слагаемые
    стоят над своим итогом. Само правило — в `ifrs_extract`, одно на разбор,
    разметку и проверку ранее подтверждённого.
    """
    return _section_of(_total_below(row, issuer, catalog), catalog)


def _section_of(code: str | None, catalog: IfrsCatalog) -> str | None:
    """Раздел позиции по её коду; None — код неизвестен."""
    if code is None:
        return None
    position = catalog.get(code)
    return position.section if position is not None else None


def confirmed_names(conn=None) -> tuple[tuple[str, str], ...]:
    """Подтверждённые коды с наименованием, как оно стояло у эмитента.

    Нужны подсказкам: у второго эмитента та же статья называется почти так
    же, и код для неё уже заведён. Без подсказки человек его не найдёт —
    в справочнике кода нет, — и заведёт второй, а доля общих статей
    от этого занизится.
    """
    from finlib.db import fetch_all

    try:
        rows = fetch_all(
            "SELECT DISTINCT ON (code) code, source_name FROM ifrs_line_confirmation"
            " WHERE relation = 'specific' ORDER BY code, confirmed_at",
            {},
            conn=conn,
        )
    except Exception as failure:  # noqa: BLE001 — разметка без базы тоже работает
        logger.warning("подтверждённые наименования не прочитаны: %s", failure)
        return ()
    return tuple((row["code"], row["source_name"]) for row in rows)


def hints_for(
    name: str,
    catalog: IfrsCatalog,
    form: str | None = None,
    section: str | None = None,
    confirmed: tuple[tuple[str, str], ...] = (),
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
    # Подтверждённые коды идут наравне с ядром: у второго эмитента та же
    # статья называется почти так же, и код для неё уже заведён. Формой
    # они не отсекаются — формы у них нет, — а раздел им неизвестен.
    #
    # Порог им ниже: список короток, а цена пропуска высока. Не увидев кода,
    # человек заведёт второй для той же статьи, и доля общих статей —
    # главное число ветки — занизится тем сильнее, чем лучше идёт разметка.
    # «НДС к возмещению и текущие переплаты по налогам» против «НДС и прочие
    # налоги к возмещению» дают 0,41 — ниже общего порога и явно то же самое.
    for code, source_name in confirmed:
        ratio = SequenceMatcher(None, target, normalize_name(source_name)).ratio()
        if ratio >= HINT_MIN_RATIO_CONFIRMED:
            scored.append((False, ratio, Hint(code, f"{source_name} (подтверждён)", ratio)))
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
    problem = markup_problem(issuer, candidate, code, catalog)
    if problem is not None:
        raise ValueError(problem)
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


def markup_problem(
    issuer: IssuerMarkup, candidate: Candidate, code: str, catalog: IfrsCatalog
) -> str | None:
    """Почему строке нельзя присвоить этот код; None — можно.

    Проверяется то же, что у автоматического опознания: код принадлежит форме
    и разделу, а строка стоит там, где стоит. Человеку это правило прежде
    не предъявлялось, и разметка обходила его молча.
    """
    position = catalog.get(code)
    if position is None:
        return None
    row = next(
        (
            item
            for item in issuer.extraction.unrecognised
            if item.key == candidate.key
        ),
        None,
    )
    if row is None:
        return None
    outcome = fold(position, [issuer.claim(row, catalog, position)])
    return outcome.reason if outcome.kind is Fold.NONE else None


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


def known_codes(catalog: IfrsCatalog) -> dict[str, IfrsPosition | None]:
    """Коды, которые разметка принимает: ядро справочника и подтверждённые.

    Специфический код заводится человеком на одной строке, но живёт дальше:
    у эмитента бывает вторая строка того же содержания, и её надо пометить
    детализацией того же кода. Прежде такой ввод отвергался словами «кода нет
    в справочнике» — код был, но не там, где его искали.

    Значение `None` означает, что код подтверждённый, а не ядровый: позиции
    справочника у него нет, и раздел с формой у него взять неоткуда.
    """
    from finlib.db import fetch_all

    found: dict[str, IfrsPosition | None] = {
        item.code: item for item in catalog.positions
    }
    try:
        for row in fetch_all(
            "SELECT DISTINCT code FROM ifrs_line_confirmation "
            "WHERE relation <> 'not_a_line'",
            {},
        ):
            found.setdefault(row["code"], None)
    except Exception as failure:  # noqa: BLE001 — разметка без базы тоже работает
        logger.warning("подтверждённые коды не прочитаны: %s", failure)
    return found


# Отчётная дата выбирается наравне с ИНН: у эмитента бывает несколько
# комплектов, и разметка принадлежит тому, на котором сделана. Индекс строки
# у другого комплекта означает другую строку, поэтому перенос присвоения
# между комплектами — не помощь, а тихое присвоение чужого кода.
_SAVED = """
SELECT code, inn, report_date, source_name, form_code, row_index,
       relation, related_codes
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


@dataclass(frozen=True, slots=True)
class SavedMarkup:
    """Присвоение прежней сессии и что с ним стало после правок разбора."""

    inn: str
    form: str
    source_name: str
    code: str
    relation: str
    # Чем строка стала после правок: восстановлена как есть, опознана
    # справочником самостоятельно (и тогда важно, тем ли кодом) или
    # не найдена в разборе вовсе.
    fate: str
    catalog_code: str | None = None
    reason: str = ""

    def describe(self) -> str:
        """Строка для отчёта."""
        tail = f" → справочник даёт {self.catalog_code}" if self.catalog_code else ""
        if self.reason:
            tail += f" — {self.reason}"
        return f"{self.inn} «{self.source_name}» = {self.code} [{self.fate}]{tail}"

    @property
    def lost(self) -> bool:
        """Пропала ли работа человека: присвоение есть, а действия нет."""
        return self.fate in (FATE_MISSING, FATE_REFUSED)


# Что стало с присвоением прежней сессии. Исходов пять, и путать их нельзя:
# «отклонено» — наша недоработка, из-за которой человек размечает строку
# заново присест за присестом, а «опознано справочником» — обратное, работа,
# которую справочник перенял.
FATE_RESTORED = "восстановлено"
FATE_RECOGNISED = "опознано справочником"
FATE_OTHER_CODE = "справочник даёт другой код"
FATE_MISSING = "строка в очереди, а разметка не применилась"
FATE_REFUSED = "отклонено правилом формы и раздела"
# Строки нет ни в очереди, ни под своим наименованием: её опознали
# справочником под другим написанием либо разбор её больше не даёт.
# Потерей это не считается — размечать нечего.
FATE_GONE = "строки в очереди нет"


def review_saved(issuers: list[IssuerMarkup], conn=None) -> list[SavedMarkup]:
    """Что стало с разметкой прежних сессий после правок разбора.

    Проверка обязательна, а не любезна: правки разбора меняют и состав строк,
    и их величины, и справочник. Присвоение, сделанное по прежнему разбору,
    могло остаться верным, могло перестать находиться, а могло разойтись
    с тем, что теперь даёт справочник сам. Молча оставить любой из трёх
    случаев значило бы потерять работу человека либо принять её за проверку.
    """
    from finlib.db import fetch_all

    by_report = _by_report(issuers)
    if not by_report:
        return []
    catalog = load_ifrs_lines()
    found: list[SavedMarkup] = []
    for row in fetch_all(_SAVED, {"inns": _inns(issuers)}, conn=conn):
        issuer = by_report.get((row["inn"], row["report_date"]))
        if issuer is None:
            continue
        position = catalog.match_by_name(row["source_name"], form=row["form_code"])
        if position is not None and position.form == row["form_code"]:
            found.append(
                SavedMarkup(
                    row["inn"],
                    row["form_code"],
                    row["source_name"],
                    row["code"],
                    row["relation"] or Relation.EXACT.value,
                    FATE_RECOGNISED
                    if position.code == row["code"]
                    else FATE_OTHER_CODE,
                    position.code,
                )
            )
            continue
        key = _restore_key(issuer, row)
        # Отклонённое притязание — отдельный исход, и он хуже остальных:
        # строка возвращается в очередь, а человек об этом не узнаёт. У ФосАгро
        # «права пользования» получили балансовый код в отчёте о движении
        # денежных средств, притязание отклонялось правилом формы, и строка
        # размечалась заново три присеста подряд.
        refused = issuer.rejects(catalog).get(key) if key is not None else None
        if key is not None:
            fate = FATE_REFUSED if refused is not None else FATE_RESTORED
        else:
            # Потеря — это когда строка в очереди стоит, а разметка к ней
            # не применилась. Если строки в очереди нет вовсе, размечать
            # нечего: её опознал справочник под другим написанием либо
            # разбор её больше не даёт.
            wanted = normalize_name(row["source_name"])
            in_queue = any(
                item.form == row["form_code"]
                and normalize_name(item.source_name) == wanted
                for item in issuer.extraction.unrecognised
            )
            fate = FATE_MISSING if in_queue else FATE_GONE
        found.append(
            SavedMarkup(
                row["inn"],
                row["form_code"],
                row["source_name"],
                row["code"],
                row["relation"] or Relation.EXACT.value,
                fate,
                reason=refused or "",
            )
        )
    return found


def restore(issuers: list[IssuerMarkup], conn=None) -> int:
    """Возвращает присвоения, сделанные в прежние присесты.

    Разметка идёт в несколько заходов, и показывать размеченное повторно
    нельзя. Восстановление не только убирает строку из очереди: присвоенный
    код участвует в суммах, и без него итоги считались бы незакрытыми —
    очередь выстроилась бы по недостаче, которой уже нет.
    """
    from finlib.db import fetch_all

    by_report = _by_report(issuers)
    if not by_report:
        return 0
    rows = fetch_all(_SAVED, {"inns": _inns(issuers)}, conn=conn)
    restored = 0
    for row in rows:
        issuer = by_report.get((row["inn"], row["report_date"]))
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
        if relation == Relation.NOT_A_LINE.value:
            issuer.dismissed[key] = Decision.NOT_A_LINE
        elif relation == Relation.PART_OF.value:
            issuer.parts[key] = row["code"]
        elif relation == Relation.AGGREGATE_OF.value:
            issuer.aggregates[key] = tuple(row["related_codes"] or (row["code"],))
        elif relation == Relation.SPECIFIC.value:
            issuer.dismissed[key] = Decision.SPECIFIC
            issuer.specific[key] = row["code"]
        else:
            issuer.assignments[key] = row["code"]
        restored += 1
    if restored:
        logger.info("восстановлено присвоений прежних присестов: %d", restored)
    return restored


def _restore_key(issuer: IssuerMarkup, row: dict) -> tuple[str, int] | None:
    """Ключ строки для восстановленной разметки: место и наименование вместе.

    **Одного индекса мало, и одного наименования мало.** Индекс — устойчивый
    ключ внутри одного разбора, но правка разбора его сдвигает: за один день
    разбор менялся трижды — графы, склейка слова, граница примечаний, — и
    присвоение легло бы на чужую строку молча. Наименование от правок разбора
    не зависит, но у части строк его нет вовсе, а «Прочие расходы» встречаются
    в форме дважды.

    Поэтому: строка по индексу берётся, если наименование совпало; иначе
    наименование ищется по форме и берётся, если оно там единственное; иначе
    ключа нет — разметка не применяется, и `review_saved` называет это потерей.
    Записи прежних сессий индекса не имеют и ищутся только по наименованию.
    """
    same_form = [
        item
        for item in issuer.extraction.unrecognised
        if item.form == row["form_code"]
    ]
    wanted = normalize_name(row["source_name"])
    if row.get("row_index") is not None:
        key = (row["form_code"], int(row["row_index"]))
        at_index = next((item for item in same_form if item.key == key), None)
        if at_index is not None and normalize_name(at_index.source_name) == wanted:
            return key
        # Индекс сместился либо строка опознана справочником. Наименование
        # переносит разметку, если оно в форме единственное: иначе решение
        # применилось бы к произвольной из тёзок.
        named = [item for item in same_form if normalize_name(item.source_name) == wanted]
        if len(named) == 1:
            if at_index is not None:
                logger.info(
                    "разметка «%s» (%s): индекс сместился %s → %s",
                    row["source_name"],
                    row["inn"],
                    key[1],
                    named[0].index,
                )
            return named[0].key
        return None
    named = [item for item in same_form if normalize_name(item.source_name) == wanted]
    return named[0].key if len(named) == 1 and wanted else None


def code_is_taken(
    code: str,
    catalog: IfrsCatalog,
    inn: str | None = None,
    conn=None,
    source_name: str | None = None,
) -> str | None:
    """Занят ли код; возвращает объяснение, чем именно занят.

    Код ядра для специфической статьи брать нельзя: ядро описывает то,
    что есть у всех, а специфическая статья — то, чего нет ни у кого другого.

    **У другого эмитента тот же код брать можно и нужно.** «НДС и прочие
    налоги к возмещению» есть и у ФосАгро, и у Сегежи, и если второму
    эмитенту код запрещён, одинаковые статьи остаются несвязанными,
    а доля общих статей — главное число ветки — занижается тем сильнее,
    чем лучше идёт разметка. Занятым код считается только внутри одного
    эмитента: там второе присвоение означало бы две разные вещи под одним
    кодом.

    **Та же статья в другом комплекте того же эмитента кода не занимает.**
    Разметка принадлежит комплекту, и «Обязательства, относящиеся к опционным
    соглашениям» размечаются и в годовом, и в промежуточном комплекте Сегежи
    одним кодом: это одна статья, а не две. Поэтому строки с тем же
    наименованием из счёта исключаются — иначе человеку пришлось бы заводить
    второй код для одной вещи, то есть делать ровно то, от чего это правило
    и охраняет.
    """
    from finlib.db import fetch_all

    position = catalog.get(code)
    if position is not None:
        return f"это код ядра: {position.name}"
    rows = fetch_all(_TAKEN, {"code": code}, conn=conn)
    mine = {
        row["source_name"]
        for row in rows
        if (inn is None or row["inn"] == inn)
        and (source_name is None or row["source_name"] != source_name)
    }
    if mine:
        listed = ", ".join(sorted(mine)[:3])
        return f"у этого эмитента код уже присвоен статье: {listed}"
    return None


def shared_specific(conn=None) -> dict[str, tuple[str, ...]]:
    """Специфические коды, присвоенные более чем одному эмитенту.

    Такой код перестал быть специфическим: статья встретилась у нескольких
    эмитентов, и место ей в ядре справочника. Поднятие остаётся решением
    человека и правкой YAML руками, а разметка обязана об этом сказать —
    сама она справочник не правит.
    """
    from finlib.db import fetch_all

    found: dict[str, set[str]] = {}
    for row in fetch_all(
        "SELECT code, inn FROM ifrs_line_confirmation WHERE relation = 'specific'",
        {},
        conn=conn,
    ):
        found.setdefault(row["code"], set()).add(row["inn"])
    return {
        code: tuple(sorted(inns)) for code, inns in found.items() if len(inns) > 1
    }


_LAST = """
SELECT inn, report_date, source_name, code, form_code, row_index
FROM ifrs_line_confirmation
WHERE inn = ANY(%(inns)s) ORDER BY confirmed_at DESC, id DESC LIMIT 1
"""


def _by_report(issuers: list[IssuerMarkup]) -> dict[tuple[str, date], IssuerMarkup]:
    """Комплекты по паре «ИНН, отчётная дата».

    Ключом был один ИНН, и пока у эмитента был один комплект, разницы
    не было. С появлением промежуточной отчётности рядом с годовой такой
    словарь оставлял один комплект из двух, а разметку второго молча терял.
    """
    return {(item.inn, item.report_date): item for item in issuers}


def _inns(issuers: list[IssuerMarkup]) -> list[str]:
    """Перечень ИНН без повторов — для отбора в запросе."""
    return sorted({item.inn for item in issuers})


def last_confirmation(
    issuers: list[IssuerMarkup], conn=None
) -> tuple[IssuerMarkup, str, tuple[str, int]] | None:
    """Последнее присвоение по журналу: комплект, наименование, ключ строки.

    Отмена не ограничена текущим присестом: ошибку замечают и через день,
    а править журнал руками неудобно и опасно.

    Возвращается сам комплект, а не ИНН: у эмитента их сколько угодно,
    и отменять нужно в том, где присвоение сделано. Комплекта нет среди
    разбираемых — отменять нечего, и это `None`, а не чужой комплект.
    """
    from finlib.db import fetch_all

    rows = fetch_all(_LAST, {"inns": [item.inn for item in issuers]}, conn=conn)
    if not rows:
        return None
    row = rows[0]
    issuer = _by_report(issuers).get((row["inn"], row.get("report_date")))
    if issuer is None:
        return None
    key = _restore_key(issuer, row)
    return issuer, row["source_name"], key or (row["form_code"], -1)


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
