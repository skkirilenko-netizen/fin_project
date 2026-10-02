"""Платежи по выпускам: график с диска. **Читает диск, не сеть.**

Отвечает на один вопрос: сколько эмитенту предстоит заплатить по облигациям
в ближайшие месяцы. Годовая отчётность этого не говорит — в ней остаток долга,
а не его срочность, — и «долг 40 млрд» у эмитента с погашением через восемь
лет и у эмитента с погашением в марте означает разное.

**Величина платежа приведена к одной облигации, а не к выпуску.** Источник
даёт купон и погашение на номинал (`cupon_sum` 17,26 при номинале 1 000),
и сумма выпуска получается умножением на число бумаг в обращении —
`outstanding_volume / nominal_price`. Без этого «платёж 17 рублей» стоял бы
рядом с балансом в миллионах.

**Число бумаг делится на первоначальный номинал, а не на остаточный.**
У амортизируемого выпуска они разные — у «Аэрофьюэлз, 002Р-02» номинал 1 000
при остаточном 250, — и объём в обращении источник отдаёт по первоначальному:
1 400 000 000 при 250 остатка означают 1 400 000 бумаг, а не 5 600 000.
Проверено округлостью: у «ППК РЭО» 1 591 075 000 / 1 000 даёт ровно
1 591 075 бумаг, а деление на остаточные 588,01 — 2 706 200 с копейками,
то есть не число бумаг. Величина же платежа уменьшение номинала уже
учитывает: купон у ЕвроТранса идёт 11,18, затем 8,38, затем 5,59.
Амортизируемых выпусков в обращении 79 из 1 207.

**График платежей факта платежа не содержит.** Поле `actual_payment_date`
выглядит датой уплаты, а является сроком, сдвинутым на рабочий день: оно
заполнено и у платежей будущих лет. Поэтому здесь по нему не судят ни о чём,
а дефолт определяется статусом выпуска (`cbonds_events`).

**Оферта — не платёж графика.** Предъявление бумаги к выкупу — право
владельца, а не обязанность, и складывать оферту с купоном значило бы
считать возможное состоявшимся. Обе величины возвращаются порознь.

**Ближайшая оферта берётся у метода оферт, а не у записи выпуска.** Поле
`offert_date_put` выглядит достаточным, и проверка это опровергла: у
«Русбонд-Удобрения, 001Р-СПВБ-01» запись объявляет 29.03.2027, а `get_offert`
даёт 28.09.2026 — внутри годового окна. Контрольная выборка выпусков,
не объявивших даты вовсе, нашла оферты ещё у четырёх. Экономия на запросах
стоила бы потери ближайшей оферты, то есть ровно той величины, ради которой
считается всё остальное.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")


@dataclass(frozen=True, slots=True)
class Payment:
    """Платёж графика: срок и величина на одну облигацию."""

    due: date
    coupon: Decimal
    redemption: Decimal
    # **Пустой купон — не объявленный, а не нулевой.** Явный ноль — у
    # бескупонной бумаги; пустое — ставка ещё не определена, и оценивает
    # его `sources.floating`, а не этот разбор.
    coupon_known: bool = True
    start: date | None = None
    # Ставка купона в процентах, когда источник её назвал.
    rate: Decimal | None = None
    # Номер купонного периода источника (`coupon_num`): по нему ставка,
    # прописанная в тексте условий для диапазона купонов, относится к платежу.
    number: int | None = None

    @property
    def total(self) -> Decimal:
        """Купон и погашение вместе: в один день бывают оба."""
        return self.coupon + self.redemption


@dataclass(frozen=True, slots=True)
class Schedule:
    """График выпуска: платежи и номинал, к которому они приведены."""

    emission_id: str
    payments: tuple[Payment, ...]
    nominal: Decimal | None

    def due_between(
        self, start: date, edge: date, outstanding: Decimal | None
    ) -> Decimal | None:
        """Платежи отрезка в валюте выпуска; None — считать нечем.

        **Отрезок задан двумя датами, а не сроком от одной.** Край окна
        закреплён на отчётной дате плюс год, а считать надо от сегодня:
        платёж, который уже сделан, впереди не стоит, а край при этом
        не должен ехать — иначе он вносит и выносит платёж от смены месяца.

        **`None` и ноль различаются.** Ноль означает, что в окне платежей нет;
        `None` — что номинал либо объём в обращении неизвестны, и умножать
        не на что. Ноль вместо этого читался бы как отсутствие обязательств.
        """
        if not self.nominal or self.nominal <= 0 or outstanding is None:
            return None
        bonds = outstanding / self.nominal
        return sum(
            (item.total * bonds for item in self.payments if start <= item.due < edge),
            start=Decimal(0),
        )


def _number(value: object) -> Decimal:
    """Величина источника; пустое и мусор считаются нулём платежа."""
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — мусор источника величиной не становится
        return Decimal(0)


def schedule_of(emission_id: str) -> Schedule | None:
    """График выпуска с диска; None — ответа источника нет.

    **«Графика нет» и «платежей нет» — разные вещи.** Первое означает, что
    доставка до выпуска не дошла, и молчать об этом нельзя: сумма к погашению
    окажется занижена ровно на его платежи.
    """
    path = CACHE / f"flow_{emission_id}.json"
    if not path.exists():
        return None
    items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    payments: list[Payment] = []
    nominal: Decimal | None = None
    for item in items:
        when = str(item.get("date") or "")[:10]
        try:
            due = date.fromisoformat(when)
        except ValueError:
            continue
        if nominal is None:
            value = _number(item.get("emission_nominal_price"))
            nominal = value if value > 0 else None
        try:
            start = date.fromisoformat(str(item.get("start_date") or "")[:10])
        except ValueError:
            start = None
        rate = _number(item.get("cupon_rate")) * 100 if item.get("cupon_rate") else None
        raw_number = str(item.get("coupon_num") or "")
        number = int(raw_number) if raw_number.isdigit() else None
        payments.append(
            Payment(
                due=due,
                coupon=_number(item.get("cupon_sum")),
                redemption=_number(item.get("redemtion")),
                coupon_known=item.get("cupon_sum") not in (None, ""),
                start=start,
                rate=rate,
                number=number,
            )
        )
    return Schedule(
        emission_id=str(emission_id),
        payments=tuple(sorted(payments, key=lambda item: item.due)),
        nominal=nominal,
    )


# Оценщик неустановленного купона: сумма на одну бумагу (None — оценки нет),
# основание и признак «данные условий выпуска, а не оценка».
Estimator = Callable[[str, Payment, Schedule, date], tuple[Decimal | None, str, bool]]


def offers_of(
    emission_id: str, kinds: tuple[str, ...] | None = None
) -> tuple[date, ...] | None:
    """Даты оферт выпуска с диска; None — ответа источника нет.

    Записей на одну оферту бывает две — день предъявления и день приобретения,
    — и различать их здесь незачем: в окно попадают обе или ни одна. `None`
    означает, что доставка до выпуска не дошла, и это не «оферт нет».

    `kinds` — виды оферт (`type_rus` источника), которые берутся; пусто —
    все. Маршрут передаёт перечень методики: call — право эмитента, и во
    вторую меру он не идёт.
    """
    path = CACHE / f"offert_{emission_id}.json"
    if not path.exists():
        return None
    found: list[date] = []
    for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
        if kinds is not None and str(item.get("type_rus") or "") not in kinds:
            continue
        when = str(item.get("date") or "")[:10]
        try:
            found.append(date.fromisoformat(when))
        except ValueError:
            continue
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class Refinancing:
    """Что эмитенту предстоит заплатить по облигациям в окне месяцев.

    **Знаменатель стоит рядом с величиной.** «К погашению 0» у эмитента,
    графиков которого нет на диске, и у эмитента без платежей — разные
    сведения, и различает их `without_schedule`.
    """

    # Горизонт окна днями — тот же, что объявлен методикой: строка списка
    # называет его словом, а замер числом, и второго его выражения нет.
    days: int
    scheduled: Decimal
    offered: Decimal
    issues: int
    without_schedule: int
    without_volume: int
    # Выпуски, по которым ответа об офертах нет: «оферт ноль» у них означает
    # недошедшую доставку, а не отсутствие права предъявления.
    without_offers: int = 0
    # Оценка неустановленных купонов окна (входит в `scheduled`), число
    # выпусков, у которых купоны окна не оценены вовсе, и основания оценки.
    estimated: Decimal = Decimal(0)
    unknown: int = 0
    bases: tuple[str, ...] = ()
    # Купоны окна, ставка которых прописана в тексте условий выпуска, а сумма
    # в графике не проставлена: это данные, а не оценка (решение владельца
    # 02.10.2026), и в `estimated` они не входят — только в `scheduled`.
    by_terms: Decimal = Decimal(0)

    @property
    def known(self) -> bool:
        """Есть ли у чего считать: хоть один выпуск с графиком и объёмом."""
        return self.issues > self.without_schedule + self.without_volume


def refinancing(
    issues: tuple[object, ...],
    days: int,
    today: date,
    offer_kinds: tuple[str, ...] | None = None,
    estimator: Estimator | None = None,
) -> Refinancing:
    """Платежи и оферты ближайших месяцев по выпускам эмитента, в рублях.

    Оферты считаются порознь: предъявление — право владельца, и сложенное
    с купоном оно выдало бы возможное за состоявшееся.

    **Окно скользящее: от сегодня на объявленное число дней вперёд**
    (решение человека 23.09.2026). Край едет посуточно, и это не порок,
    а устройство: мера отвечает на вопрос «хватит ли денег на то, что
    впереди», и горизонт впереди всегда одинаков.

    **Дребезг вносила ступень, а не движение края.** Прежде край вставал
    на первое число месяца, и платёж входил в окно не тогда, когда до него
    оставался год, а первого числа вместе с целым месяцем платежей — и так же
    выходил. На пересчитанной истории все двенадцать возвратов внутри окна
    отмены оказались этим, у одного эмитента трижды подряд. **В скользящем
    окне платёж входит однажды и не выходит**, пока не будет заплачен.

    Денежные средства при этом остаются на отчётную дату, а платежи всегда
    будущие: моменты расходятся намеренно — это предмет меры, а не её изъян.
    """
    scheduled = offered = estimated = by_terms = Decimal(0)
    counted = no_schedule = no_volume = no_offers = unknown = 0
    bases: list[str] = []
    start = today
    edge = today + timedelta(days=days)
    for issue in issues:
        status = str(getattr(issue, "status", ""))
        if status not in ("в обращении", "размещается"):
            continue
        counted += 1
        emission = str(getattr(issue, "emission_id", ""))
        outstanding = getattr(issue, "outstanding", None)
        plan = schedule_of(emission)
        if plan is None:
            no_schedule += 1
            continue
        if outstanding is None:
            no_volume += 1
            continue
        due = plan.due_between(start, edge, outstanding)
        if due is None:
            no_volume += 1
            continue
        scheduled += due
        if estimator is not None and plan.nominal:
            # **Неустановленный купон окна оценивается, а не читается нулём**
            # (`sources.floating`); не оценённый — выпуск идёт в счёт
            # неполных, и сумма становится границей снизу.
            missed = False
            for item in plan.payments:
                if not (start <= item.due < edge) or item.coupon_known:
                    continue
                amount, basis, data = estimator(emission, item, plan, today)
                if amount is None:
                    missed = True
                    continue
                part = amount * outstanding / plan.nominal
                scheduled += part
                if data:
                    by_terms += part
                    continue
                estimated += part
                if basis and basis not in bases:
                    bases.append(basis)
            unknown += int(missed)
        # **Ближайшая оферта — у метода оферт.** Поле записи выпуска называет
        # не всегда ближайшую: у «Русбонд-Удобрения» оно объявляет 29.03.2027
        # при оферте 28.09.2026. Ответа нет — это называется, а не считается
        # отсутствием оферты.
        offers = offers_of(emission, offer_kinds)
        if offers is None:
            no_offers += 1
            continue
        if any(start <= item < edge for item in offers):
            offered += outstanding
    return Refinancing(
        days=days,
        scheduled=scheduled,
        offered=offered,
        issues=counted,
        without_schedule=no_schedule,
        without_volume=no_volume,
        without_offers=no_offers,
        estimated=estimated,
        unknown=unknown,
        bases=tuple(bases),
        by_terms=by_terms,
    )
