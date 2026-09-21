"""Расчёт показателей по МСФО (задача 27).

**Подстановки нет нигде.** Правило пришло из РСБУ и в этой ветке уже дважды
подтвердилось ценой: покрытие процентов считается только по начисленным
из примечаний — величина из отчёта о прибыли или убытке у Автодора дала бы
414 вместо 54 382; ликвидность девелопера без средств на счетах эскроу
завышена вчетверо и потому не приводится вовсе.

**Отрицательный знаменатель отменяет показатель.** «Чистый долг / EBITDA
−1,86» у Сегежи арифметически верен и читается как низкая нагрузка, будучи
противоположным. Причина `negative_denominator`; сама отрицательная EBITDA
при этом не пропадает — она остаётся величиной и идёт в сигналы.

**FFO на промежуточной отчётности не считается.** Выбор объявлен: приведение
операционного потока к году умножением на 12/N даёт величину, которую нечем
проверить, а сезонность у девелопера делает её заведомо ложной. EBITDA
и проценты приводятся и помечаются.
"""

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.ifrs_metrics import IfrsMetricsPolicy, MetricDef, load_ifrs_metrics

logger = logging.getLogger(__name__)


class Reason(StrEnum):
    """Почему показатель не рассчитан."""

    MISSING_INPUT = "missing_input"
    ZERO_DENOMINATOR = "zero_denominator"
    NEGATIVE_DENOMINATOR = "negative_denominator"
    NOT_EXTRACTED_YET = "not_extracted_yet"
    ADJUSTMENT_IMPOSSIBLE = "adjustment_impossible"
    INTERIM_NOT_ANNUALISED = "interim_not_annualised"
    REPLACED_BY_RANGE = "replaced_by_range"


REASON_TEXT: dict[Reason, str] = {
    Reason.MISSING_INPUT: "нет входных величин",
    Reason.ZERO_DENOMINATOR: "знаменатель равен нулю",
    Reason.NEGATIVE_DENOMINATOR: "знаменатель отрицателен, отношение читалось бы наоборот",
    Reason.NOT_EXTRACTED_YET: "величина знаменателя пока не извлекается",
    # **Не «нечем посчитать»: величина раскрыта.** Сноска под балансом
    # извлекается и приводится в документе дословно; не сделан перевод её
    # в состав входных величин показателя, и это наш пробел, а не нехватка
    # данных у эмитента.
    Reason.ADJUSTMENT_IMPOSSIBLE: "поправка по типу эмитента не применена",
    Reason.REPLACED_BY_RANGE: "показатель заменён диапазоном двух границ",
    Reason.INTERIM_NOT_ANNUALISED: "на промежуточной отчётности не рассчитывается",
}


@dataclass(frozen=True, slots=True)
class MetricValue:
    """Значение показателя либо отказ с названной причиной."""

    code: str
    name: str
    group: str
    in_scoring: bool
    value: Decimal | None = None
    reason: Reason | None = None
    missing: tuple[str, ...] = ()
    annualised: bool = False
    # Части отношения хранятся рядом с ним: по ним делается **вывод по знаку**.
    # Неположительный числитель при положительном знаменателе доказывает, что
    # отношение ниже единицы, без деления — и доказывает это даже тогда, когда
    # само отношение не посчитано. Того же рода, что вывод нуля раздела
    # из тождества баланса.
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    # Показатель, который эта величина ограничивает сверху, и формулировка
    # печати. Оценка сверху — не значение показателя, и печатать её как
    # значение нельзя: «5,2» и «не выше 5,2» — разные утверждения.
    bound_for: str | None = None
    bound_shown: str | None = None

    @property
    def shown(self) -> str:
        """Величина словами: граница — со своей формулировкой, прочее — числом."""
        if self.value is None:  # pragma: no cover — печатается только рассчитанное
            return ""
        number = str(self.value.quantize(Decimal("0.001")))
        if self.bound_shown:
            return self.bound_shown.format(value=number)
        return number

    @property
    def below_one_by_sign(self) -> bool:
        """Доказано ли знаком, что отношение ниже единицы."""
        return (
            self.numerator is not None
            and self.denominator is not None
            and self.numerator <= 0
            and self.denominator > 0
        )

    @property
    def calculable(self) -> bool:
        """Рассчитан ли показатель."""
        return self.value is not None

    def describe(self) -> str:
        """Однострочное описание для отчёта."""
        if self.calculable:
            mark = " (приведён к году)" if self.annualised else ""
            return f"{self.name}: {self.shown}{mark}"
        return f"{self.name}: не рассчитан, {reason_text(self)}"


@dataclass(frozen=True, slots=True)
class Inputs:
    """Вход расчёта: величины отчётности, величины примечаний, обстановка.

    Величины примечаний приходят отдельным словарём намеренно: показатель,
    объявленный `notes_only`, обязан быть не в состоянии взять величину
    из формы — не по договорённости, а потому, что её здесь нет.
    """

    values: dict[str, Decimal]
    notes: dict[str, Decimal]
    issuer_type: str = "corporate"
    months: int = 12

    @property
    def interim(self) -> bool:
        """Промежуточная ли отчётность."""
        return self.months != 12


# Наименования производных величин словами. **Внутренний код в документ
# не попадает**: «нет входных величин — interest_accrued» — технический
# идентификатор в тексте, а правило его запрещает. Перечень один на весь
# проект: замер и документ называют величину одинаково, иначе читатель
# сверяет одно с другим и не находит соответствия.
DERIVED_NAMES: dict[str, str] = {
    "interest_accrued": "начисленные проценты по заёмным средствам "
    "(примечание о финансовых доходах и расходах)",
    "debt_due_within_year": "долг к погашению в ближайшие 12 месяцев "
    "(таблица сроков в примечании о заёмных средствах)",
    "net_debt": "чистый долг: заёмные средства за вычетом денежных",
    "debt_total": "совокупный долг: долгосрочные и краткосрочные заёмные средства",
    "ebitda": "EBITDA: операционная прибыль и амортизация",
    "nwc": "чистый оборотный капитал: итог оборотных активов за вычетом итога "
    "краткосрочных обязательств",
    "ffo": "FFO: поток от операционной деятельности до изменений оборотного капитала",
    "current_assets_ex_inventories": "оборотные активы за вычетом запасов",
    "current_assets_ex_escrow_claims": "оборотные активы за вычетом запасов "
    "и требований, погашаемых раскрытием счетов эскроу (примечание "
    "о дебиторской задолженности)",
}


def named(code: str) -> str:
    """Величина словами: производная — своим наименованием, позиция — кодом.

    Позиция отчётности в перечне недостающих остаётся кодом намеренно:
    её наименование подставляет тот, кто печатает отказ, — у него есть
    справочник статей, а здесь только расчёт.
    """
    return DERIVED_NAMES.get(code, code)


def reason_text(item: "MetricValue", policy: IfrsMetricsPolicy | None = None) -> str:
    """Причина отказа словами — **одна на все места, где она печатается**.

    Прежде текст набирался дважды: здесь для терминала и в записи показателей
    для базы. Два пути к одному ответу расходятся, и расходились: у ЛСР
    в терминал шёл код `ifrs.escrow_balance`, а в документ — место из методики.

    **У невозможной поправки место называет методика.** Недостающей величины
    в справочнике позиций нет вовсе — она стоит сноской, — и назвать её
    словами нечем: код читателю ничего не говорит, а методика объявляет место
    сама («средства на счетах эскроу раскрыты сноской под балансом»).
    """
    text = REASON_TEXT.get(item.reason, "причина не названа")
    missing = ", ".join(named(code) for code in item.missing)
    if item.reason is Reason.ADJUSTMENT_IMPOSSIBLE:
        policy = policy or load_ifrs_metrics()
        declared = next(
            (found.where for found in policy.adjustments if found.metric == item.code),
            "",
        )
        missing = declared or missing
    return f"{text} — {missing}" if missing else text


def months_of(
    report_date, reporting_kind: str, policy: IfrsMetricsPolicy | None = None
) -> int:
    """Число месяцев периода комплекта: из отчётной даты у промежуточного.

    Правило живёт в методике (`annualisation.months_from`), а не в коде:
    число месяцев, задаваемое снаружи, однажды задают двенадцатью — и
    аннуализация перестаёт срабатывать, не сообщая об этом.
    """
    policy = policy or load_ifrs_metrics()
    if reporting_kind != "interim":
        return 12
    if policy.annualisation.months_from != "report_date_month":
        raise ValueError(
            f"правило числа месяцев {policy.annualisation.months_from} "
            "не реализовано: молча вернуть двенадцать значило бы не привести "
            "величины к году и не сказать об этом"
        )
    return report_date.month


def compute_all(
    inputs: Inputs, policy: IfrsMetricsPolicy | None = None
) -> tuple[MetricValue, ...]:
    """Считает все показатели справочника, включая отказы с причинами.

    Показатель, объявленный только для своего типа эмитента, у прочих
    не считается **и не отказывает**: его там не существует, и отказ
    «нет входных величин» описывал бы пробел, которого нет.
    """
    policy = policy or load_ifrs_metrics()
    derived = _derived(inputs, policy)
    applicable = [
        item for item in policy.metrics if item.only_for_type in (None, inputs.issuer_type)
    ]
    found = [
        _compute(item, inputs, derived, policy)
        for item in applicable
        if not item.bound_for
    ]
    # **Оценка сверху считается только там, где точной величины нет.** Рядом
    # с посчитанной точной граница ничего не добавляет, а читатель, увидев два
    # числа об одном показателе, правильно им не верит.
    exact = {item.code: item.calculable for item in found}
    for item in applicable:
        if not item.bound_for or exact.get(item.bound_for):
            continue
        found.append(_compute(item, inputs, derived, policy))
    return tuple(found)


def _derived(inputs: Inputs, policy: IfrsMetricsPolicy) -> dict[str, Decimal | None]:
    """Производные величины: долг, чистый долг, EBITDA, FFO.

    Величина, собранная не полностью, остаётся `None`: один недостающий
    компонент отменяет её целиком, и частичных сумм здесь нет.
    """
    get = inputs.values.get
    long_debt, short_debt = get("ifrs.long_term_borrowings"), get("ifrs.short_term_borrowings")
    debt = None
    if long_debt is not None or short_debt is not None:
        debt = (long_debt or Decimal(0)) + (short_debt or Decimal(0))
    cash = get("ifrs.cash")
    profit, depreciation = get("ifrs.operating_profit"), get("ifrs.depreciation")
    before = get("ifrs.cash_before_working_capital_changes")
    interest_paid, taxes_paid = get("ifrs.interest_paid"), get("ifrs.income_taxes_paid")

    ebitda = profit + abs(depreciation) if profit is not None and depreciation is not None else None
    ffo = None
    if None not in (before, interest_paid, taxes_paid) and not inputs.interim:
        ffo = before - abs(interest_paid) - abs(taxes_paid)
    scale = Decimal(12) / Decimal(inputs.months)
    if inputs.interim and ebitda is not None and "ebitda" in policy.annualisation.scaled:
        ebitda *= scale
    # **Чистый оборотный капитал считается по итогам разделов баланса.**
    # Отговорка «состав оборотных активов различается от эмитента к эмитенту»
    # не держится: итоги раздела опознаются справочником — их и вычитаем,
    # а состав внутри итога на разность не влияет. Один недостающий итог
    # отменяет величину целиком, как и везде здесь.
    current_assets = get("ifrs.total_current_assets")
    current_liabilities = get("ifrs.total_current_liabilities")
    nwc = (
        current_assets - current_liabilities
        if current_assets is not None and current_liabilities is not None
        else None
    )
    # **Границы диапазона текущей ликвидности девелопера.** Считаются здесь,
    # а не в показателе, по общему правилу: числовых литералов и арифметики
    # в справочнике показателей нет, состав величины объявляется методикой,
    # а собирается один раз. Один недостающий компонент отменяет границу
    # целиком — частичных сумм здесь нет, как и у прочих производных.
    inventories = get("ifrs.inventories")
    ex_inventories = (
        current_assets - inventories
        if current_assets is not None and inventories is not None
        else None
    )
    escrow_claims = inputs.notes.get("ifrs.escrow_backed_claims")
    ex_escrow = (
        ex_inventories - escrow_claims
        if ex_inventories is not None and escrow_claims is not None
        else None
    )
    return {
        "debt_total": debt,
        "nwc": nwc,
        "current_assets_ex_inventories": ex_inventories,
        "current_assets_ex_escrow_claims": ex_escrow,
        "net_debt": debt - cash if debt is not None and cash is not None else None,
        "ebitda": ebitda,
        "ffo": ffo,
        "interest_accrued": _annualised(
            inputs.notes.get("interest_accrued"), inputs, policy, "interest_accrued"
        ),
        "debt_due_within_year": inputs.notes.get("debt_due_within_year"),
    }


def _annualised(
    value: Decimal | None, inputs: Inputs, policy: IfrsMetricsPolicy, code: str
) -> Decimal | None:
    """Приводит потоковую величину к году, если период неполный."""
    if value is None or not inputs.interim or code not in policy.annualisation.scaled:
        return value
    return value * Decimal(12) / Decimal(inputs.months)


def _compute(
    metric: MetricDef,
    inputs: Inputs,
    derived: dict[str, Decimal | None],
    policy: IfrsMetricsPolicy,
) -> MetricValue:
    """Считает один показатель по правилам справочника."""
    empty = MetricValue(
        metric.code,
        metric.name,
        metric.group,
        metric.in_scoring,
        bound_for=metric.bound_for,
        bound_shown=metric.bound_shown,
    )

    adjustment = next(
        (item for item in policy.for_type(inputs.issuer_type) if item.metric == metric.code),
        None,
    )
    if adjustment is not None:
        if adjustment.replaced_by:
            # **Показатель заменён диапазоном, а не не посчитан.** Исход тот же
            # по форме — величины нет, — но причина другая: это решение
            # методики, а не пробел данных, и запрашивать у организации нечего.
            return _refused(empty, Reason.REPLACED_BY_RANGE)
        missing = tuple(
            code for code in adjustment.requires if inputs.values.get(code) is None
        )
        if missing:
            # Поправку нечем посчитать — исходный показатель не приводится:
            # завышенный вчетверо хуже отсутствующего.
            return _refused(empty, Reason.ADJUSTMENT_IMPOSSIBLE, missing)

    if metric.availability == "not_extracted_yet":
        return _refused(empty, Reason.NOT_EXTRACTED_YET)

    numerator = _value_of(metric.numerator, inputs, derived, metric)
    if numerator is None:
        if metric.numerator in policy.annualisation.never_scaled and inputs.interim:
            return _refused(empty, Reason.INTERIM_NOT_ANNUALISED)
        return _refused(empty, Reason.MISSING_INPUT, (metric.numerator,))

    if metric.denominator is None:
        return MetricValue(
            metric.code,
            metric.name,
            metric.group,
            metric.in_scoring,
            numerator,
            annualised=_is_annualised(metric.numerator, inputs, policy),
            bound_for=metric.bound_for,
            bound_shown=metric.bound_shown,
        )

    denominator = _value_of(metric.denominator, inputs, derived, metric)
    if denominator is None:
        return _refused(empty, Reason.MISSING_INPUT, (metric.denominator,))
    # Части отношения остаются при отказе: по ним делается вывод по знаку,
    # и отказ, у которого их нет, лишает вывода доказательства.
    empty = replace(empty, numerator=numerator, denominator=denominator)
    if denominator == 0:
        return _refused(empty, Reason.ZERO_DENOMINATOR)
    if metric.denominator_must_be_positive and denominator < 0:
        return _refused(empty, Reason.NEGATIVE_DENOMINATOR)

    annualised = _is_annualised(metric.numerator, inputs, policy) or _is_annualised(
        metric.denominator, inputs, policy
    )
    return MetricValue(
        metric.code,
        metric.name,
        metric.group,
        metric.in_scoring,
        numerator / denominator,
        annualised=annualised,
        numerator=numerator,
        denominator=denominator,
        bound_for=metric.bound_for,
        bound_shown=metric.bound_shown,
    )


def _value_of(
    code: str,
    inputs: Inputs,
    derived: dict[str, Decimal | None],
    metric: MetricDef,
) -> Decimal | None:
    """Величина по коду: из примечаний, из производных либо из отчётности.

    Показатель, объявленный `notes_only`, берёт знаменатель **только**
    из примечаний. Величина из формы здесь недоступна — не по соглашению,
    а потому, что её в этом словаре нет.
    """
    if metric.denominator_source == "notes_only" and code == metric.denominator:
        return inputs.notes.get(code) if code in inputs.notes else derived.get(code)
    if code in derived:
        return derived[code]
    return inputs.values.get(code)


def _is_annualised(code: str, inputs: Inputs, policy: IfrsMetricsPolicy) -> bool:
    """Приводилась ли эта величина к году."""
    return inputs.interim and code in policy.annualisation.scaled


def _refused(
    empty: MetricValue, reason: Reason, missing: tuple[str, ...] = ()
) -> MetricValue:
    """Отказ с названной причиной."""
    logger.info("показатель %s не рассчитан: %s", empty.code, reason.value)
    return MetricValue(
        empty.code,
        empty.name,
        empty.group,
        empty.in_scoring,
        reason=reason,
        missing=missing,
        # Части отношения переносятся в отказ: вывод по знаку опирается
        # на них, а не на посчитанную величину.
        numerator=empty.numerator,
        denominator=empty.denominator,
    )
