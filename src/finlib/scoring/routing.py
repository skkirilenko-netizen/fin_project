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
тезисов. Порог вывода по границе — нижняя опорная точка шкалы долговой
нагрузки. Новый порог ровно один и назван отдельно: срок, после которого
отсутствие годовой отчётности само становится обстоятельством.

**Корзины упорядочены.** Основание разбора старше основания внимания:
у эмитента со сработавшим стоп-фактором и устаревшей отчётностью корзина —
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
class Verdict:
    """Корзина эмитента и основания, по которым он в неё попал."""

    basket: str
    basket_name: str
    grounds: tuple[str, ...]
    status: str
    # Величины, по которым решение принято: без них корзина — слово без опоры.
    details: tuple[str, ...] = ()

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
    today: date | None = None,
    policy: IfrsMetricsPolicy | None = None,
    routing: RoutingPolicy | None = None,
) -> Verdict:
    """Определяет корзину эмитента по посчитанным величинам и обстоятельствам.

    Довод `computed` — итог боевого расчёта, а не собранный вручную словарь:
    второй путь к тем же величинам разошёлся бы с первым, и корзина зависела
    бы от того, кто спросил.
    """
    policy = policy or load_ifrs_metrics()
    routing = routing or load_routing()
    by_code = {item.code: item for item in computed}
    grounds: list[str] = []
    details: list[str] = []

    if quarantined:
        grounds.append("zero_check_failed")
    if stop_factors:
        grounds.append("stop_factor")
        details.append("стоп-факторы: " + ", ".join(stop_factors))
    if financing_structure:
        grounds.append("financing_structure")
    absent = _absent(by_code)
    if absent:
        grounds.append("metrics_missing")
        details.append("не считаются: " + ", ".join(sorted(absent)))
    if grounds:
        basket = routing.basket("review")
        return Verdict(basket.code, basket.name, tuple(grounds), routing.status, tuple(details))

    lower = load_theses().bands.lower_below
    for code in ROUTING_METRICS:
        item = by_code.get(code)
        scale = policy.calibration_points.metrics.get(code)
        if item is None or not item.calculable or scale is None:
            continue
        score = level(item.value, scale)
        if score < lower:
            grounds.append("metric_in_lower_band")
            details.append(f"{item.name}: балл уровня {score:.0f} ниже {lower:.0f}")

    bound = by_code.get("net_debt_op_profit")
    if bound is not None and bound.calculable:
        threshold = max(
            x for x, _ in policy.calibration_points.metrics["net_debt_ebitda"].points
        )
        if bound.value > threshold:
            grounds.append("bound_above_threshold")
            details.append(f"оценка сверху {bound.value:.2f} выше порога {threshold}")

    if operating_profit is not None and operating_profit <= 0:
        grounds.append("operating_loss")
        details.append(f"операционная прибыль {operating_profit}")

    if routing.freshness.stale(latest_annual, today or date.today()):
        grounds.append("stale_annual")
        details.append(
            f"свежая годовая отчётность: {latest_annual or 'нет вовсе'}"
        )

    code = "attention" if grounds else "clear"
    basket = routing.basket(code)
    # Порядок оснований — порядок объявления в справочнике: перечень, собранный
    # в порядке проверок, читался бы как старшинство, которого у них нет.
    declared = [item.code for item in basket.grounds]
    ordered = tuple(sorted(set(grounds), key=declared.index))
    return Verdict(basket.code, basket.name, ordered, routing.status, tuple(details))


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
