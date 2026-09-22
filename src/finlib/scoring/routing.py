"""Маршрутизация эмитента по корзинам — **черновик правил**.

Отвечает на один вопрос: нужен ли человек. Не на вопрос «каков класс» —
класс по нормализованным данным агрегатора и не фиксируется: состав величин
у него собственный.

**Статус черновика печатается вместе с корзиной.** Правила объявлены
в `methodology/routing.yaml` со статусом `draft`, и корзина, выданная как
решение методики, неотличима от согласованной, — а согласована она не была.

**Своих чисел у маршрута почти нет, и это условие задачи.** «Ниже нижней части
калибровочной шкалы» — балл уровня ниже границы `bands.lower_below`
из `theses.yaml`, то есть та же граница, которой делится шкала для словесных
тезисов. «За концом шкалы» — балл уровня, равный нулю: величина лежит
за последней опорной точкой, и это конец существующей шкалы, а не новый порог.
Тяжесть стоп-фактора берётся у самого стоп-фактора (`cap`
в `ifrs_issuer_type.yaml`). Новый порог ровно один и назван отдельно: срок,
после которого отсутствие годовой отчётности само становится обстоятельством.

**Разбор — обстоятельство риска, а не пробел данных.** Нерассчитанная
величина не говорит ни за эмитента, ни против: она называется основанием
внимания вместе с недостающим полем. Смешение пробела с риском отправляло
к человеку тех, о ком нечего сказать, и корзина разбора вырастала до трёх
пятых универсума.

**Корзины упорядочены.** Основание разбора старше основания внимания:
у эмитента с тяжёлым стоп-фактором и устаревшей отчётностью корзина —
разбор. Основания при этом перечисляются все: по частоте видно, чем корзина
наполняется.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.metrics.ifrs import MetricValue
from finlib.metrics.ifrs_view import IfrsMetricsView
from finlib.normalize.ifrs_issuer_type import IssuerTypePolicy, load_issuer_types
from finlib.normalize.ifrs_metrics import IfrsMetricsPolicy, load_ifrs_metrics
from finlib.scoring.ifrs import level
from finlib.scoring.theses import load_theses

logger = logging.getLogger(__name__)

# Величины решения. Перечень здесь, а не в справочнике корзин: это состав
# расчёта, и он тот же, что в замере — пять величин, по которым решается
# «нужен ли человек».
ROUTING_METRICS: tuple[str, ...] = (
    "net_debt_ebitda",
    "equity_ratio",
    "cur_liq",
)


class Ground(BaseModel):
    """Основание корзины: код, наименование и откуда берётся порог."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    why: str = Field(min_length=1)
    threshold_from: str | None = None


class Basket(BaseModel):
    """Корзина маршрута."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    order: int = Field(ge=1)
    meaning: str = Field(min_length=1)
    grounds: tuple[Ground, ...] = ()


class Severity(BaseModel):
    """Градации ограничения класса, при которых нужен человек."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_caps: tuple[str, ...] = Field(min_length=1)
    origin: str = Field(min_length=1)

    def severe(self, cap: str | None) -> bool:
        """Требует ли ограничение класса разбора.

        Довод `None` означает стоп-фактор, градации у которого нет вовсе, —
        такого в методике быть не может, и молча приравнять его к штатному
        значило бы спрятать неполноту справочника.
        """
        if cap is None:
            raise ValueError(
                "у стоп-фактора не объявлена градация ограничения класса: "
                "тяжесть берётся у методики, а не назначается маршрутом"
            )
        return cap in self.review_caps


class Freshness(BaseModel):
    """Единственный новый порог маршрута — срок сдачи годовой отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    annual_due: str = Field(pattern=r"^\d{2}-\d{2}$")
    origin: str = Field(min_length=1)
    calibration_status: str = Field(min_length=1)

    def stale(self, latest: date | None, today: date) -> bool:
        """Нет ли годовой отчётности за прошлый год после срока сдачи."""
        month, day = (int(part) for part in self.annual_due.split("-"))
        if today < date(today.year, month, day):
            return False
        return latest is None or latest.year < today.year - 1


class RoutingPolicy(BaseModel):
    """Справочник маршрутизации целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    status: str = Field(pattern="^(draft|approved)$")
    approved_by: str | None = None
    origin: str = Field(min_length=1)
    severity: Severity
    freshness: Freshness
    baskets: tuple[Basket, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _draft_is_not_signed(self) -> Self:
        """Утверждённый справочник называет, кем утверждён.

        Иначе статус `approved` появится правкой одного слова, и корзина,
        никем не согласованная, начнёт печататься как решение методики.
        """
        if self.status == "approved" and not (self.approved_by or "").strip():
            raise ValueError(
                "справочник маршрутизации объявлен утверждённым, но не сказано, "
                "кем: утверждение — решение человека, и оно называется"
            )
        return self

    def basket(self, code: str) -> Basket:
        """Корзина по коду."""
        found = next((item for item in self.baskets if item.code == code), None)
        if found is None:
            raise KeyError(f"корзины {code} в справочнике маршрутизации нет")
        return found

    def ordered(self) -> tuple[Basket, ...]:
        """Корзины в порядке старшинства."""
        return tuple(sorted(self.baskets, key=lambda item: item.order))


@lru_cache(maxsize=1)
def load_routing(path: Path | None = None) -> RoutingPolicy:
    """Читает справочник маршрутизации."""
    source = path or settings.methodology_dir / "routing.yaml"
    policy = RoutingPolicy.model_validate(
        yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    )
    logger.info(
        "маршрутизация %s (%s): корзин %d",
        policy.version,
        policy.status,
        len(policy.baskets),
    )
    return policy


@dataclass(frozen=True, slots=True)
class Finding:
    """Одно сработавшее основание: код, предмет и текст.

    **Предмет назван отдельным полем, а не только словами.** Считать, каким
    показателем наполнена корзина, приходится замеру, и вытаскивать код
    из прозы значило бы завести второй путь к тому же ответу — тот самый,
    который расходится с первым и расхождения не показывает.
    """

    ground: str
    subject: str
    text: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """Корзина эмитента и основания, по которым он в неё попал."""

    basket: str
    basket_name: str
    grounds: tuple[str, ...]
    status: str
    # Величины, по которым решение принято: без них корзина — слово без опоры.
    findings: tuple[Finding, ...] = ()

    @property
    def details(self) -> tuple[str, ...]:
        """Основания словами — в порядке, в каком сработали."""
        return tuple(item.text for item in self.findings)

    def describe(self) -> str:
        """Однострочное описание для прогона и сводки."""
        listed = ", ".join(self.grounds) or "оснований нет"
        mark = "" if self.status == "approved" else " (правила — черновик)"
        return f"{self.basket_name}: {listed}{mark}"


def route(
    computed: tuple[MetricValue, ...],
    *,
    quarantined: bool,
    stop_factors: tuple[str, ...] = (),
    financing_structure: bool = False,
    # Операционная прибыль — статья отчётности, а не показатель, и передаётся
    # доводом: расчёт показателей её не отдаёт, а знак её и есть основание.
    operating_profit: Decimal | None = None,
    latest_annual: date | None = None,
    # Класс, присвоенный нами по разобранному документу. Довод именно класс,
    # а не «оценка есть»: класс A у эмитента с тяжёлым балансом агрегатора —
    # не обстоятельство, а опровержение признака.
    assessed_class: str | None = None,
    today: date | None = None,
    policy: IfrsMetricsPolicy | None = None,
    routing: RoutingPolicy | None = None,
    types: IssuerTypePolicy | None = None,
) -> Verdict:
    """Определяет корзину эмитента по посчитанным величинам и обстоятельствам.

    Довод `computed` — итог боевого расчёта, а не собранный вручную словарь:
    второй путь к тем же величинам разошёлся бы с первым, и корзина зависела
    бы от того, кто спросил.
    """
    policy = policy or load_ifrs_metrics()
    routing = routing or load_routing()
    types = types or load_issuer_types()
    caps = {factor.code: factor.cap for factor in types.stop_factors}
    by_code = {item.code: item for item in computed}
    review: list[Finding] = []
    attention: list[Finding] = []

    if quarantined:
        review.append(
            Finding("zero_check_failed", "", "комплект в карантине по проверке нуля")
        )
    severe = tuple(
        code for code in stop_factors if routing.severity.severe(caps.get(code))
    )
    capped = tuple(code for code in stop_factors if code not in severe)
    for code in severe:
        review.append(
            Finding("stop_factor_severe", code, f"стоп-фактор разбора: {code}")
        )
    for code in capped:
        attention.append(
            Finding("stop_factor_capped", code, f"стоп-фактор внимания: {code}")
        )
    if financing_structure:
        review.append(
            Finding("financing_structure", "", "финансирующая структура группы")
        )
    if assessed_class and assessed_class in routing.severity.review_caps:
        review.append(
            Finding(
                "assessed_class_low",
                assessed_class,
                f"класс {assessed_class} по разобранному документу",
            )
        )
    for name in sorted(_absent(by_code)):
        attention.append(Finding("data_insufficient", name, f"не считается: {name}"))

    lower = load_theses().bands.lower_below
    view = IfrsMetricsView(policy)
    for code in ROUTING_METRICS:
        item = by_code.get(code)
        scale = policy.calibration_points.metrics.get(code)
        if item is None or not item.calculable or scale is None:
            continue
        score = level(item.value, scale)
        # Ноль балла и есть «за концом шкалы»: балл нуля стоит у крайней
        # опорной точки, и дальше шкала не продолжается. Оба основания сразу
        # не ставятся — величина одна, и говорить о ней дважды значило бы
        # считать одно обстоятельство за два.
        if score == 0:
            # Величина печатается единой точкой округления, а не своим
            # выражением: «−0,000» читается как ноль, которым она не является,
            # и словесную замену знает справочник показателей.
            review.append(
                Finding(
                    "level_off_scale",
                    code,
                    f"{item.name}: {view.shown(code, item.value)} за опорной "
                    f"точкой {scale.points[0][0]} своей шкалы",
                )
            )
        elif score < lower:
            # Разрядность балла здесь не украшение: «балл уровня 34 ниже 34»
            # получался округлением 33,5 и опровергал сам себя.
            attention.append(
                Finding(
                    "metric_in_lower_band",
                    code,
                    f"{item.name}: балл уровня {score:.1f} ниже {lower:.1f}",
                )
            )

    bound = by_code.get("net_debt_op_profit")
    if bound is not None and bound.calculable:
        threshold = max(
            x for x, _ in policy.calibration_points.metrics["net_debt_ebitda"].points
        )
        if bound.value > threshold:
            attention.append(
                Finding(
                    "bound_above_threshold",
                    bound.code,
                    f"оценка сверху {bound.value:.2f} выше порога {threshold}",
                )
            )

    if operating_profit is not None and operating_profit <= 0:
        attention.append(
            Finding(
                "operating_loss",
                "ifrs.operating_profit",
                f"операционная прибыль {operating_profit}",
            )
        )

    if routing.freshness.stale(latest_annual, today or date.today()):
        attention.append(
            Finding(
                "disclosure_overdue",
                "",
                f"свежая годовая отчётность: {latest_annual or 'нет вовсе'}",
            )
        )

    found = review + attention
    if review:
        return _verdict(routing, "review", found)
    return _verdict(routing, "attention" if attention else "clear", found)


def _verdict(routing: RoutingPolicy, code: str, findings: list[Finding]) -> Verdict:
    """Собирает вердикт, упорядочивая основания по объявлению в справочнике.

    Перечень, собранный в порядке проверок, читался бы как старшинство,
    которого у оснований одной корзины нет.

    **Основания корзины и обстоятельства эмитента — разные перечни.** Корзину
    называют только свои основания, а сработало у эмитента обычно больше:
    у комплекта с тяжёлым стоп-фактором есть и штатные, и нарушенный срок
    раскрытия. Они остаются в вердикте, потому что человеку, которому
    комплект передают, нужны все, — но корзину не называют.
    """
    basket = routing.basket(code)
    declared = [item.code for item in basket.grounds]
    ordered = tuple(
        sorted({item.ground for item in findings if item.ground in declared},
               key=declared.index)
    )
    return Verdict(basket.code, basket.name, ordered, routing.status, tuple(findings))


def _absent(by_code: dict[str, MetricValue]) -> set[str]:
    """Величины решения, которых расчёт не собрал.

    Текущая ликвидность у девелопера заменена диапазоном, и верхняя граница
    заменяет её здесь: «не считается» означало бы пробел данных, а это решение
    методики.
    """
    absent: set[str] = set()
    if not _ok(by_code, "net_debt_ebitda") and not _ok(by_code, "net_debt_op_profit"):
        absent.add("долговая нагрузка")
    if not _ok(by_code, "equity_ratio"):
        absent.add("автономия")
    if not _ok(by_code, "cur_liq") and not _ok(by_code, "cur_liq_ex_inventories"):
        absent.add("текущая ликвидность")
    return absent


def _ok(by_code: dict[str, MetricValue], code: str) -> bool:
    """Рассчитан ли показатель."""
    item = by_code.get(code)
    return item is not None and item.calculable


def value_of(computed: tuple[MetricValue, ...], code: str) -> Decimal | None:
    """Величина показателя по коду; None — не рассчитан."""
    item = next((entry for entry in computed if entry.code == code), None)
    return item.value if item is not None and item.calculable else None
