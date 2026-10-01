"""Величины маршрута по стандартам: маршрут один, справочника показателей два.

**Маршрут спрашивает у обоих стандартов одно и то же, а коды у них разные.**
Нужен ли человек — вопрос о долговой нагрузке, автономии и текущей
ликвидности, и он не зависит от того, чем отчиталась организация. Но в МСФО
долговая нагрузка зовётся `net_debt_ebitda`, а в РСБУ её нет вовсе:
амортизация в формах 0710001–0710005 не раскрывается, и вместо величины
остаётся граница `debt_to_op_profit` — чистый долг к прибыли от продаж.
Наименования, разрядность печати и перечень стоп-факторов у справочников
тоже свои.

**Второй маршрут писать нельзя.** Два пути к одному ответу расходятся,
и расхождения не видно, пока их не сравнить: список наблюдения показывал бы
одну корзину, а замер распределения другую, и какая попала к человеку,
зависело бы от того, кто спросил. Поэтому правила остаются одни
(`scoring/routing.py::route`), а здесь объявлено то единственное, чем
стандарты различаются: какими кодами у них зовутся величины решения, чем
считается вывод по границе и какие стоп-факторы объявлены.

**Шкалы у маршрута одни на оба стандарта, и это решение человека**
(дорожная карта, фаза 1, 22.09.2026: «те же правила маршрута на показателях
РСБУ»). Маршрут не присваивает класс — он отвечает, нужен ли человек,
и корзина, означающая разное в соседних строках одного списка, хуже
неточной шкалы. Собственная шкала РСБУ объявлена в `scoring.yaml` только
для автономии, и разводить по стандартам одну величину из трёх значило бы
получить список, в котором «Внимание» у двух эмитентов означает разное.
Величина шкал остаётся предварительной, и вердикт это печатает.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.metrics.definitions import Unit, load_metrics
from finlib.metrics.display import format_metric, scale_of_unit
from finlib.normalize.ifrs_metrics import Scale, load_ifrs_metrics
from finlib.scoring.definitions import StopEffect, load_scoring
from finlib.standards import Standard

logger = logging.getLogger(__name__)


class StandardRule(BaseModel):
    """Чем называются величины маршрута в одном стандарте.

    Пустая строка кода означает, что величины у стандарта нет вовсе, —
    и это не то же, что «не рассчитана»: у РСБУ долговой нагрузки нет
    по устройству форм, а не по пробелу в отчётности.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    # Показатели решения: те, что оцениваются по шкале. Порядок объявления —
    # порядок, в котором основания печатаются.
    metrics: tuple[str, ...] = Field(min_length=1)
    # Долговая нагрузка: код величины либо пусто, если у стандарта её нет.
    burden: str = ""
    # Вывод по границе и показатель, порог которого к ней применяется.
    bound: str = ""
    bound_of: str = Field(min_length=1)
    # Результат, знак которого решает, доказывает ли граница хоть что-нибудь:
    # при неположительном знаменателе отношение отрицательно и читается
    # шкалой как низкая нагрузка.
    bound_requires_positive: str = ""
    # Величина, знак которой гасит отношение к ней (EBITDA у МСФО).
    earnings: str = ""
    # Статья отчётности, по знаку которой ставится основание «операционный
    # убыток»: у МСФО позиция `ifrs.operating_profit`, у РСБУ строка 2200.
    operating_line: str = Field(min_length=1)
    # Денежные средства: знаменатель рефинансирования. Код строки, а не
    # показателя, — величина берётся из отчётности, а не считается.
    cash_line: str = Field(min_length=1)
    # Краткосрочные заёмные средства: величина признака изменения
    # по промежуточной отчётности (`interim.yaml`). Код объявлен здесь
    # вместе с прочими величинами маршрута — второй перечень кодов строк
    # разошёлся бы с первым при первой же правке справочника.
    short_debt_line: str = Field(min_length=1)
    # Замена показателя другим при проверке полноты: у девелопера текущая
    # ликвидность одним числом недостоверна, и методика заменяет её диапазоном;
    # верхняя граница диапазона отвечает за полноту вместо неё. Это решение
    # методики, а не пробел данных, и «не рассчитана» здесь было бы неправдой.
    replaced_by: dict[str, str] = Field(default_factory=dict)
    # Полнота покрытия для крупного долга: у МСФО требуется сама величина
    # нагрузки, у РСБУ — граница, потому что иной величины не бывает вовсе.
    cover: tuple[str, ...] = Field(min_length=1)
    # Наименования недостающих величин для основания «данных недостаточно»:
    # читателю называется предмет, а не код показателя.
    absent_names: dict[str, str] = Field(min_length=1)
    # **Строки заёмных средств и величины, которые из них считаются.** Ноль
    # по всем строкам у эмитента с выпусками в обращении означает нераскрытие
    # (`cbonds_mapping.yaml`, `debt_zero_with_bonds`): выпуск и есть заём.
    # Тогда ни одна из этих величин раскрытой не считается — иначе
    # отрицательный чистый долг читался бы как чистая денежная позиция,
    # то есть как довод в пользу эмитента, полученный из нераскрытого.
    debt_lines: tuple[str, ...] = Field(min_length=1)
    debt_metrics: tuple[str, ...] = Field(min_length=1)
    # Величины, которые строка печатает рядом с решающими, но корзины
    # не решающие: другой состав той же меры, до решения о переключении.
    alongside: tuple[str, ...] = ()
    why: str = Field(min_length=1)

    @model_validator(mode="after")
    def _absent_names_cover_what_is_checked(self) -> Self:
        """У каждой проверяемой на полноту величины объявлено наименование.

        Иначе основание «данных недостаточно» назвало бы код показателя —
        то самое, против чего заведена таблица формулировок.
        """
        named = set(self.absent_names)
        checked = set(self.cover)
        missing = checked - named
        if missing:
            raise ValueError(
                f"{self.label}: у величин полноты не объявлено наименование: "
                + ", ".join(sorted(missing))
            )
        return self


class StandardRules(BaseModel):
    """Величины маршрута по стандартам вместе с происхождением решения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scales_from: Standard
    scales_origin: str = Field(min_length=1)
    by_standard: dict[Standard, StandardRule] = Field(min_length=2)

    @model_validator(mode="after")
    def _every_standard_is_declared(self) -> Self:
        """Правила объявлены у всех стандартов модели.

        Стандарт без правил означал бы, что маршрут по нему не строится
        вовсе, — а это решение, и оно называется, а не получается умолчанием.
        """
        missing = set(Standard) - set(self.by_standard)
        if missing:
            listed = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"величины маршрута не объявлены для стандартов: {listed}")
        return self


@dataclass(frozen=True, slots=True)
class StopFactorView:
    """Стоп-фактор так, как его спрашивает маршрут.

    `metric` пуст у стоп-фактора, объявленного сразу по нескольким величинам
    (у РСБУ «Нехватка оборотного капитала и покрытия процентов» — по двум)
    либо не по величине вовсе (неопределённость непрерывности объявляется
    разделом аудиторского заключения). Пустота здесь — сведение: маршрут
    берёт величину, по которой стоп-фактор **сработал**, а не ту, по которой
    он проверяется.
    """

    code: str
    name: str
    metric: str
    cap: str


class RoutingCatalogue:
    """Справочник величин маршрута одного стандарта.

    Всё, что маршруту нужно знать о стандарте: коды величин решения, шкалы,
    наименования, печать и стоп-факторы. Собирается из справочника показателей
    самого стандарта — второго перечня наименований в проекте быть не должно.
    """

    def __init__(
        self,
        standard: Standard,
        rule: StandardRule,
        scales: dict[str, Scale],
        names: dict[str, str],
        shown: Callable[[str, Decimal, str], str],
        stops: tuple[StopFactorView, ...],
        money: frozenset[str] = frozenset(),
    ) -> None:
        self.standard = standard
        self.rule = rule
        self._scales = scales
        self._names = names
        self._shown = shown
        self.stop_factors = stops
        # Денежные величины справочника: в формулировке основания они
        # переводятся в единицу печати, отношения — нет.
        self.money = money

    @property
    def label(self) -> str:
        """Чем маршрут построен: стандарт и периметр отчётности."""
        return self.rule.label

    @property
    def metrics(self) -> tuple[str, ...]:
        """Показатели решения этого стандарта."""
        return self.rule.metrics

    def scale(self, code: str) -> Scale | None:
        """Шкала показателя; None — шкалы у него нет."""
        return self._scales.get(code)

    def threshold_of(self, code: str) -> Decimal:
        """Крайняя опорная точка шкалы: порог вывода по границе."""
        scale = self._scales.get(code)
        if scale is None:
            raise KeyError(
                f"шкалы {code} нет: порог вывода по границе брать неоткуда, "
                "а назначить его здесь значило бы завести своё число"
            )
        return max(x for x, _ in scale.points)

    def name_of(self, code: str) -> str:
        """Наименование показателя; неизвестный код — свой же код."""
        return self._names.get(code, code)

    def shown(self, code: str, value: Decimal, unit: str) -> str:
        """Величина так, как она печатается читателю.

        Печатает её справочник своего стандарта, а не эта обёртка: у МСФО
        величина бывает границей («не выше 2,32») и словом вместо числа,
        и второй набор правил печати разошёлся бы с первым.
        """
        return self._shown(code, value, unit)

    def stop_factor(self, code: str) -> StopFactorView | None:
        """Стоп-фактор по коду; None — в справочнике стандарта его нет."""
        return next((item for item in self.stop_factors if item.code == code), None)


def _ifrs_stops() -> tuple[StopFactorView, ...]:
    """Стоп-факторы МСФО: у каждого одна величина либо раздел заключения."""
    from finlib.normalize.ifrs_issuer_type import load_issuer_types

    return tuple(
        StopFactorView(
            code=factor.code,
            name=factor.name,
            metric=factor.metric or "",
            cap=factor.cap,
        )
        for factor in load_issuer_types().stop_factors
    )


def _rsbu_stops() -> tuple[StopFactorView, ...]:
    """Стоп-факторы РСБУ: последствие объявлено эффектом, а не потолком.

    `lowest_class` означает ограничение низшим классом, и потолком здесь
    служит сам низший класс справочника: градация у маршрута берётся
    у методики, а не назначается им.
    """
    scoring = load_scoring()
    return tuple(
        StopFactorView(
            code=factor.code,
            name=factor.name,
            # Величина называется, только если она одна: стоп-фактор,
            # объявленный по двум, сработать может любой из них, и назвать
            # заранее одну значило бы напечатать величину, которой основание
            # не вызвано.
            metric=factor.metrics[0] if len(factor.metrics) == 1 else "",
            cap=(
                scoring.lowest_class
                if factor.effect is StopEffect.LOWEST_CLASS
                else (factor.cap or scoring.lowest_class)
            ),
        )
        for factor in scoring.stop_factors
    )


@cache
def catalogue_for(standard: Standard) -> RoutingCatalogue:
    """Справочник величин маршрута по стандарту.

    Шкалы берутся из одного места на оба стандарта — так объявлено
    в `routing.yaml`, блок `standards`: маршрут сравнивает эмитентов обоих
    стандартов в одном списке, и корзина, означающая в соседних строках
    разное, хуже неточной шкалы.
    """
    from finlib.scoring.routing import load_routing

    rules = load_routing().standards
    rule = rules.by_standard[standard]
    scales = dict(load_ifrs_metrics().calibration_points.metrics)
    if rules.scales_from is not Standard.IFRS:  # pragma: no cover — объявлено МСФО
        raise ValueError(
            f"шкалы маршрута объявлены из {rules.scales_from}, а взять их "
            "оттуда нечем: перечень шкал есть только у справочника МСФО"
        )
    if standard is Standard.IFRS:
        from finlib.metrics.ifrs_view import IfrsMetricsView

        view = IfrsMetricsView(load_ifrs_metrics())
        names = {item.code: item.name for item in view.metrics}
        shown = view.shown
        stops = _ifrs_stops()
        money = frozenset(
            item.code for item in view.metrics if item.unit is Unit.THOUSAND_RUB
        )
    else:
        catalog = load_metrics()
        names = {item.code: item.name for item in catalog.metrics}
        units = {item.code: item.unit for item in catalog.metrics}

        def shown(code: str, value: Decimal, unit: str) -> str:
            """Величина РСБУ единой точкой печати и разрядностью методики."""
            kind = units.get(code, Unit.RATIO)
            return format_metric(
                value, kind, scale_of_unit(catalog, kind), money=unit
            )

        stops = _rsbu_stops()
        money = frozenset(
            code for code, kind in units.items() if kind is Unit.THOUSAND_RUB
        )
    logger.info(
        "величины маршрута %s: решают %s, граница %s, стоп-факторов %d",
        standard.value,
        ", ".join(rule.metrics),
        rule.bound or "нет",
        len(stops),
    )
    return RoutingCatalogue(standard, rule, scales, names, shown, stops, money)
