"""Маршрутизация эмитента по корзинам.

Отвечает на один вопрос: нужен ли человек. Не на вопрос «каков класс» —
класс по нормализованным данным агрегатора и не фиксируется: состав величин
у него собственный.

**Структура правил утверждена, пороги предварительны, и это два разных
сведения.** Состав корзин, перечень оснований, градации тяжести и подгруппы
внимания согласованы человеком (`methodology/routing.yaml`, `status:
approved`); величины остаются непроверенными (`thresholds: preliminary`),
и вердикт печатает это вместе с корзиной. Одно поле на оба утверждения
означало бы, что согласие со структурой читается как согласие с числами.

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
from finlib.sources.cbonds_events import DEFAULT_STATUSES

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
    group: str | None = None


class Subgroup(BaseModel):
    """Подгруппа корзины: своя природа обстоятельства и своё действие."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    order: int = Field(ge=1)
    action: str = Field(min_length=1)
    why: str = Field(min_length=1)


class Basket(BaseModel):
    """Корзина маршрута."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    order: int = Field(ge=1)
    meaning: str = Field(min_length=1)
    groups: tuple[Subgroup, ...] = ()
    grounds: tuple[Ground, ...] = ()

    @model_validator(mode="after")
    def _grounds_name_a_declared_group(self) -> Self:
        """Подгруппа объявлена у всех оснований корзины либо ни у одного.

        Основание без подгруппы в корзине с подгруппами не показалось бы
        нигде — то есть исчезло бы вместе со своим действием, а подгруппа,
        названная там, где их нет, читалась бы как объявленная.
        """
        declared = {item.code for item in self.groups}
        named = {item.group for item in self.grounds if item.group}
        unknown = named - declared
        if unknown:
            raise ValueError(
                f"корзина {self.code}: основания называют подгруппы, которых "
                f"в ней нет: {', '.join(sorted(unknown))}"
            )
        if declared:
            silent = [item.code for item in self.grounds if not item.group]
            if silent:
                raise ValueError(
                    f"корзина {self.code} разделена на подгруппы, а основания "
                    f"{', '.join(silent)} ни одной не называют: показать их "
                    "было бы негде"
                )
        return self

    def subgroup(self, code: str) -> Subgroup | None:
        """Подгруппа по коду; None — подгрупп у корзины нет."""
        return next((item for item in self.groups if item.code == code), None)

    def group_of(self, ground: str) -> str:
        """Подгруппа основания; пустая строка — корзина не разделена."""
        found = next((item for item in self.grounds if item.code == ground), None)
        return (found.group or "") if found is not None else ""


class SameCircumstance(BaseModel):
    """Стоп-фактор и показатель маршрута, говорящие об одном обстоятельстве."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_factor: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    why: str = Field(min_length=1)
    origin: str = Field(min_length=1)


class MutedStopFactor(BaseModel):
    """Отрасли, в которых стоп-фактор основания маршрута не даёт.

    **Гасится основание маршрута, а не стоп-фактор.** Класс методика
    по-прежнему ограничивает: отрицательный оборотный капитал у сетевой
    компании остаётся обстоятельством оценки, а к человеку по нему никого
    не отправляют — это устройство отрасли.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_factor: str = Field(min_length=1)
    branches: tuple[str, ...] = Field(min_length=1)
    why: str = Field(min_length=1)
    origin: str = Field(min_length=1)


class Events(BaseModel):
    """Событийный слой: как дефолт и рейтинг входят в маршрут.

    **Годовая отчётность события между отчётными датами не видит.** У ЕвроТранса
    числа за 2025 год спокойны, а по двенадцати выпускам стоит неурегулированный
    дефолт и рейтинги всех четырёх агентств отозваны. Категории берутся
    из справочника точек шкал источника; перечни здесь — решение методики
    о том, что с категорией делать.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_review: bool
    default_origin: str = Field(min_length=1)
    credit_scales: dict[str, str] = Field(min_length=1)
    credit_scales_origin: str = Field(min_length=1)
    review_categories: tuple[str, ...] = Field(min_length=1)
    attention_categories: tuple[str, ...] = Field(min_length=1)
    attention_outlooks: tuple[str, ...] = Field(min_length=1)
    categories_origin: str = Field(min_length=1)


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
    status_unknown_after_cycles: int = Field(gt=0)
    origin: str = Field(min_length=1)
    calibration_status: str = Field(min_length=1)

    def stale(self, latest: date | None, today: date) -> bool:
        """Нет ли годовой отчётности за прошлый год после срока сдачи."""
        month, day = (int(part) for part in self.annual_due.split("-"))
        if today < date(today.year, month, day):
            return False
        return latest is None or latest.year < today.year - 1

    def cycles_behind(self, latest: date | None, today: date) -> int | None:
        """Сколько циклов раскрытия прошло без годовой отчётности.

        Цикл — год: отчётность за истекший год публикуется к сроку `annual_due`.
        До этого срока последняя ожидаемая отчётность — за позапрошлый год,
        после — за прошлый. `None` означает, что годовой отчётности нет вовсе:
        сравнивать не с чем, и это не «ноль циклов».
        """
        if latest is None:
            return None
        month, day = (int(part) for part in self.annual_due.split("-"))
        expected = today.year - 1 if today >= date(today.year, month, day) else today.year - 2
        return max(expected - latest.year, 0)


class Statements(BaseModel):
    """Формулировки оснований: смысл, а не механика расчёта.

    **«Величина маршрута за концом своей калибровочной шкалы» — правда о том,
    как устроен расчёт, и ничего не говорит человеку, которому эмитента
    передают.** Формулировки объявлены методикой и правятся диффом, как всякая
    формулировка документа; у показателя бывает своя, потому что «автономия
    0,03» и «чистый долг / EBITDA 5,2x» читаются по-разному.

    Неизвестный слот в формулировке роняет справочник при загрузке: подстановка
    обрушилась бы у первого же эмитента, у которого основание сработает.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_ground: dict[str, dict[str, str]] = Field(min_length=1)

    def say(self, ground: str, key: str = "", **slots: object) -> str:
        """Формулировка основания с подставленными величинами.

        `key` выбирает формулировку показателя: «автономия 0,03» и «чистый долг
        / EBITDA 5,2x» читаются по-разному, и одна формулировка на оба случая
        говорила бы о механике, а не о смысле.
        """
        templates = self.by_ground.get(ground)
        if templates is None:
            raise KeyError(
                f"формулировка основания {ground} не объявлена: "
                "механический текст читателю ничего не говорит"
            )
        template = templates.get(key) or templates.get("default")
        if template is None:
            raise KeyError(
                f"у основания {ground} нет формулировки ни для {key}, "
                "ни по умолчанию"
            )
        return " ".join(template.split()).format(**slots)


class Universe(BaseModel):
    """Состав списка: кого в нём не бывает и почему.

    **Поглощённый эмитент строкой списка быть не может.** Карточка агрегатора
    объявляет поглощение полем `emitents_id_absorption`, и его отчётность —
    история: у «Глоракс (не сущ.)» это поле равно идентификатору живого
    «Глоракса». Правило структурное, а не по наименованию: статусы эмитента
    приходят кодами, справочника к ним у API нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exclude_absorbed: bool
    origin: str = Field(min_length=1)


class RoutingPolicy(BaseModel):
    """Справочник маршрутизации целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    status: str = Field(pattern="^(draft|approved)$")
    approved_by: str | None = None
    # **Согласие со структурой и зрелость порогов — разные сведения.**
    # Утверждённый состав корзин не делает калиброванными их величины,
    # и одно поле на оба утверждения читалось бы как согласие с числами.
    thresholds: str = Field(default="preliminary", pattern="^(preliminary|calibrated)$")
    thresholds_origin: str = ""
    origin: str = Field(min_length=1)
    same_circumstance: tuple[SameCircumstance, ...] = ()
    stop_factor_muted: tuple[MutedStopFactor, ...] = ()
    events: Events
    statements: Statements
    universe: Universe
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
        if self.thresholds == "preliminary" and not self.thresholds_origin.strip():
            raise ValueError(
                "пороги объявлены предварительными, но не сказано почему: "
                "зрелость величины — сведение, а не отговорка"
            )
        return self

    @model_validator(mode="after")
    def _every_ground_has_a_statement(self) -> Self:
        """У каждого основания объявлена формулировка.

        Основание без формулировки печаталось бы механическим текстом —
        тем самым, из-за которого таблица формулировок и появилась.
        """
        declared = {
            ground.code for basket in self.baskets for ground in basket.grounds
        }
        missing = declared - set(self.statements.by_ground)
        if missing:
            raise ValueError(
                "у оснований нет формулировок: " + ", ".join(sorted(missing))
            )
        stray = set(self.statements.by_ground) - declared
        if stray:
            raise ValueError(
                "формулировки объявлены для оснований, которых нет: "
                + ", ".join(sorted(stray))
            )
        return self

    def say(self, ground: str, key: str = "", **slots: object) -> str:
        """Формулировка основания: одно место на список, замер и выгрузку."""
        return self.statements.say(ground, key, **slots)

    def muted_for(self, branch: str) -> frozenset[str]:
        """Стоп-факторы, не дающие основания маршрута в этой отрасли."""
        if not branch:
            return frozenset()
        return frozenset(
            item.stop_factor
            for item in self.stop_factor_muted
            if branch in item.branches
        )

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
    # Подгруппы корзины в порядке старшинства: первая — та, по которой
    # эмитент показывается, остальные называются рядом. Пусто — корзина
    # на подгруппы не разделена.
    subgroups: tuple[str, ...] = ()
    subgroup_names: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    # Стоп-факторы, погашенные отраслью: основания маршрута они не дали,
    # и счётчик обязан их назвать — правило, гасящее молча, неотличимо
    # от невыполненного.
    muted: tuple[str, ...] = ()
    # Показатели, у которых основание **не** поставлено, потому что о том же
    # обстоятельстве уже сказал стоп-фактор. Считается это наравне
    # со сработавшим: правило, гасящее молча, неотличимо от невыполненного.
    spoken_for: tuple[str, ...] = ()
    # Зрелость порогов справочника: утверждённая структура не делает величины
    # калиброванными, и вердикт обязан нести оба сведения.
    thresholds: str = "preliminary"

    @property
    def details(self) -> tuple[str, ...]:
        """Основания словами — в порядке, в каком сработали."""
        return tuple(item.text for item in self.findings)

    @property
    def subgroup(self) -> str:
        """Старшая подгруппа: по ней эмитент и показывается."""
        return self.subgroups[0] if self.subgroups else ""

    def describe(self) -> str:
        """Однострочное описание для прогона и сводки."""
        listed = ", ".join(self.grounds) or "оснований нет"
        where = f" [{self.subgroup_names[0]}]" if self.subgroup_names else ""
        # Статус печатается при любом исходе: «правила утверждены, пороги
        # предварительны» — сведение, а не оговорка, и умолчание о нём выдало
        # бы предварительную величину за калиброванную.
        mark = (
            " (правила — черновик)"
            if self.status != "approved"
            else ("" if self.thresholds == "calibrated" else " (пороги предварительны)")
        )
        return f"{self.basket_name}{where}: {listed}{mark}"


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
    # Отрасль карточки источника: в пяти отраслях отрицательный оборотный
    # капитал — модель бизнеса, и основания маршрута он не даёт.
    branch: str = "",
    # Эмитент той же группы, стоящий в разборе: обстоятельство группы говорит
    # и о её члене, но корзину ему не назначает — оно не мягче внимания.
    group_under_review: tuple[str, str] | None = None,
    # Группа карточки и её представитель в списке: формулировка SPV называет
    # обоих — «оценка по группе» без имени группы ничего не значит.
    group: str = "",
    group_leader: str = "",
    # Чем присвоен класс: вид отчётности и отчётная дата. Страницы у оценки
    # нет — класс присвоен комплекту, а не месту в документе.
    assessed_where: str = "",
    # События эмитента: выпуски и рейтинги. Годовая отчётность их не видит
    # по устройству, и без них «Без внимания» стоит у эмитента с дефолтом.
    events: object | None = None,
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

    status: list[Finding] = []
    # **Давность видна всегда и более сильным основанием не гасится.** Если
    # последняя годовая отчётность старше двух циклов раскрытия, маршрут
    # по её числам не строится вовсе: они описывают организацию, которой могло
    # не стать, и вопрос к ней другой — о статусе, а не о нагрузке.
    cycles = routing.freshness.cycles_behind(latest_annual, today or date.today())
    if cycles is not None and cycles >= routing.freshness.status_unknown_after_cycles:
        status.append(
            Finding(
                "reporting_two_cycles_old",
                "",
                f"последняя годовая отчётность за {latest_annual:%Y} год, "
                f"циклов раскрытия прошло {cycles}",
            )
        )

    if quarantined:
        review.append(
            Finding("zero_check_failed", "", "комплект в карантине по проверке нуля")
        )
    severe = tuple(
        code for code in stop_factors if routing.severity.severe(caps.get(code))
    )
    # **Гашение считается наравне со сработавшим.** Правило, гасящее молча,
    # неотличимо от невыполненного, и «в этой отрасли признак не в счёт»
    # обязано быть видно числом.
    muted_here = routing.muted_for(branch)
    muted = tuple(
        code for code in stop_factors if code not in severe and code in muted_here
    )
    capped = tuple(
        code
        for code in stop_factors
        if code not in severe and code not in muted_here
    )
    # **Стоп-фактор называется наименованием, а не кодом.** Основание читает
    # человек — на экране наблюдения и в сводке, — и `negative_nwc` ему
    # не говорит ничего; код остаётся предметом основания, по нему считают.
    view = IfrsMetricsView(policy)
    by_factor = {factor.code: factor for factor in types.stop_factors}

    def about(code: str) -> str:
        # Формулировка стоп-фактора: наименование и его величина в скобках.
        # «Стоп-фактор: отрицательный собственный капитал» без величины
        # заставляет читателя искать её в других графах.
        factor = by_factor.get(code)
        extra = ""
        if factor is not None and factor.metric:
            item = by_code.get(factor.metric)
            if item is not None and item.calculable:
                extra = (
                    f" ({view.require(factor.metric).name.lower()} "
                    f"{view.shown(factor.metric, item.value)})"
                )
        return routing.say(
            "stop_factor_severe" if code in severe else "stop_factor_capped",
            name=(factor.name if factor is not None else code),
            extra=extra,
        )

    for code in severe:
        review.append(Finding("stop_factor_severe", code, about(code)))
    for code in capped:
        attention.append(Finding("stop_factor_capped", code, about(code)))
    if group_under_review is not None:
        whose, member = group_under_review
        attention.append(
            Finding(
                "group_under_review",
                whose,
                routing.say("group_under_review", group=whose, leader=member),
            )
        )
    if events is not None:
        review.extend(_event_findings(events, routing))
        attention.extend(_rating_watch(events, routing))
    if financing_structure:
        review.append(
            Finding(
                "financing_structure",
                group,
                routing.say(
                    "financing_structure",
                    group=group or "не названа в справочнике",
                    leader=group_leader or "головной компании в списке нет",
                ),
            )
        )
    if assessed_class and assessed_class in routing.severity.review_caps:
        review.append(
            Finding(
                "assessed_class_low",
                assessed_class,
                routing.say(
                    "assessed_class_low",
                    where=assessed_where or "разобранному документу",
                    **{"class": assessed_class},
                ),
            )
        )
    debt_threshold = max(
        x for x, _ in policy.calibration_points.metrics["net_debt_ebitda"].points
    )
    for name in sorted(_absent(by_code, debt_threshold)):
        attention.append(
            Finding(
                "data_insufficient", name, routing.say("data_insufficient", field=name)
            )
        )

    lower = load_theses().bands.lower_below
    spoken_for = _spoken_for(stop_factors, routing, types)
    silenced: list[str] = []

    # **Знак EBITDA — своё основание, и он гасит величины отношения к ней.**
    # Отношение чистого долга к неположительной EBITDA отрицательно и читается
    # шкалой как низкая нагрузка: у эмитента с убытком выходило бы «без
    # внимания». Обстоятельство при этом одно, поэтому величина отношения
    # своего основания не даёт — его даёт знак.
    ebitda = by_code.get("ebitda")
    if ebitda is not None and ebitda.calculable and ebitda.value <= 0:
        attention.append(
            Finding(
                "negative_ebitda",
                "ebitda",
                routing.say("negative_ebitda", value=view.shown("ebitda", ebitda.value)),
            )
        )
        spoken_for = spoken_for | {"net_debt_ebitda"}
    for code in ROUTING_METRICS:
        item = by_code.get(code)
        scale = policy.calibration_points.metrics.get(code)
        if item is None or not item.calculable or scale is None:
            continue
        score = level(item.value, scale)
        # **Одно обстоятельство — одно решение.** По этому показателю уже
        # сработал стоп-фактор, и тяжесть обстоятельства названа методикой;
        # величина повторяет его и своего основания не даёт. Гашение считается
        # только там, где основание было бы поставлено: иначе счётчик мерил бы
        # число здоровых показателей.
        if code in spoken_for:
            if score < lower:
                silenced.append(code)
            continue
        # Ноль балла и есть «за концом шкалы»: балл нуля стоит у крайней
        # опорной точки, и дальше шкала не продолжается. Оба основания сразу
        # не ставятся — величина одна, и говорить о ней дважды значило бы
        # считать одно обстоятельство за два.
        if score == 0:
            # Формулировка говорит о смысле, а не о механике: «за опорной
            # точкой калибровочной шкалы» — правда об устройстве расчёта
            # и ничего не значит для того, кому эмитента передают. Величина
            # печатается единой точкой округления: «−0,000» читается как ноль,
            # которым она не является.
            review.append(
                Finding(
                    "level_off_scale",
                    code,
                    routing.say(
                        "level_off_scale",
                        code,
                        metric=item.name,
                        value=view.shown(code, item.value),
                        threshold=view.shown(code, scale.points[0][0]),
                    ),
                )
            )
        elif score < lower:
            attention.append(
                Finding(
                    "metric_in_lower_band",
                    code,
                    routing.say(
                        "metric_in_lower_band",
                        code,
                        metric=item.name,
                        value=view.shown(code, item.value),
                    ),
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
                    # **Граница сверху — не «не менее».** Настоящее значение
                    # бывает и ниже порога: это отсутствие вывода, а не плохая
                    # величина, и формулировка обязана говорить именно так.
                    routing.say(
                        "bound_above_threshold",
                        value=view.shown("net_debt_ebitda", bound.value),
                    ),
                )
            )

    if operating_profit is not None and operating_profit <= 0:
        # Величина денежная, и печатается она единой точкой округления:
        # «-7378338.000» — не число для человека, а внутреннее представление.
        # Единица здесь не называется: она свойство комплекта, и объявляет её
        # тот, кто печатает строку целиком.
        from finlib.metrics.display import money

        attention.append(
            Finding(
                "operating_loss",
                "ifrs.operating_profit",
                routing.say("operating_loss", value=money(operating_profit)),
            )
        )

    if routing.freshness.stale(latest_annual, today or date.today()):
        attention.append(
            Finding(
                "disclosure_overdue",
                "",
                routing.say(
                    "disclosure_overdue",
                    date=f"{latest_annual:%Y}" if latest_annual else "— её нет вовсе",
                ),
            )
        )

    found = status + review + attention
    if status:
        # Очередь статуса старше корзин тяжести: по числам такой давности
        # решение принимать нельзя, каким бы тяжёлым обстоятельство ни было.
        return _verdict(routing, "status_unknown", found, tuple(silenced), muted)
    if review:
        return _verdict(routing, "review", found, tuple(silenced), muted)
    return _verdict(
        routing, "attention" if attention else "clear", found, tuple(silenced), muted
    )


def _event_findings(events: object, routing: RoutingPolicy) -> list[Finding]:
    """Основания разбора из событий: дефолт по выпуску и рейтинг категории.

    **Дата события источником не приводится.** У дефолта по погашению опорой
    служит дата погашения — день, когда платёж был должен состояться;
    у дефолта по выпуску в обращении остаётся дата обновления записи. Каждая
    называется тем, чем является: выдумывать дату события мы не будем.
    """
    found: list[Finding] = []
    if routing.events.default_review:
        for issue in getattr(events, "defaulted", ()):
            where = (
                f"погашение {issue.maturity:%d.%m.%Y}"
                if issue.maturity is not None
                else (
                    f"запись обновлена {issue.updated:%d.%m.%Y}"
                    if issue.updated is not None
                    else "даты события источник не приводит"
                )
            )
            # **Статус выпуска и признак неурегулированности — разные
            # сведения.** «Дефолт по погашению» говорит, что случилось;
            # признак у выпуска в обращении — что случившееся не улажено,
            # и называть это «в обращении» значило бы сказать обратное.
            what = (
                issue.status.capitalize()
                if issue.status in DEFAULT_STATUSES
                else f"Неурегулированный дефолт (выпуск {issue.status})"
            )
            found.append(
                Finding(
                    "emission_default",
                    issue.name,
                    routing.say(
                        "emission_default", what=what, issue=issue.name, where=where
                    ),
                )
            )
    # **Одно агентство — одно основание.** У агентства бывает две шкалы
    # (национальная и собственной кредитоспособности), и обе дают одну и ту же
    # категорию: два основания об одном читались бы как два события.
    seen: set[tuple[str, str]] = set()
    for rating in getattr(events, "live", ()):
        if (rating.agency, rating.category) in seen:
            continue
        seen.add((rating.agency, rating.category))
        if rating.category in routing.events.review_categories:
            found.append(
                Finding(
                    "rating_default",
                    rating.category,
                    routing.say(
                        "rating_default",
                        point=rating.point,
                        agency=rating.agency,
                        date=(
                            f"{rating.assigned:%d.%m.%Y}"
                            if rating.assigned is not None
                            else "дата не указана"
                        ),
                        category=rating.category,
                    ),
                )
            )
    return found


def _rating_watch(events: object, routing: RoutingPolicy) -> list[Finding]:
    """Основание внимания: спекулятивная категория с негативным прогнозом.

    Категория сама по себе — уровень, а не событие; событием её делает
    объявленный агентством прогноз.
    """
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for rating in getattr(events, "live", ()):
        if (rating.agency, rating.category) in seen:
            continue
        seen.add((rating.agency, rating.category))
        if (
            rating.category in routing.events.attention_categories
            and rating.outlook in routing.events.attention_outlooks
        ):
            found.append(
                Finding(
                    "rating_watch",
                    rating.category,
                    routing.say(
                        "rating_watch",
                        point=rating.point,
                        agency=rating.agency,
                        outlook=rating.outlook.lower(),
                    ),
                )
            )
    return found


def _spoken_for(
    stop_factors: tuple[str, ...], routing: RoutingPolicy, types: IssuerTypePolicy
) -> set[str]:
    """Показатели, о которых уже сказал сработавший стоп-фактор.

    Два источника, и оба нужны. Совпадение кода показателя действует само:
    отрицательная автономия проверяется по тому же `equity_ratio`, что
    и маршрут. Совпадение предмета при разных кодах объявляется методикой
    (`same_circumstance`): отрицательный чистый оборотный капитал считается
    по `nwc`, а ликвидность ниже единицы — по `cur_liq`, и то, что это одно
    обстоятельство, выводу из имён не поддаётся.
    """
    triggered = set(stop_factors)
    metrics = {
        factor.metric
        for factor in types.stop_factors
        if factor.code in triggered and factor.metric
    }
    metrics |= {
        item.metric
        for item in routing.same_circumstance
        if item.stop_factor in triggered
    }
    return metrics


def _verdict(
    routing: RoutingPolicy,
    code: str,
    findings: list[Finding],
    spoken_for: tuple[str, ...] = (),
    muted: tuple[str, ...] = (),
) -> Verdict:
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
    # Подгруппы — в порядке старшинства, объявленном справочником: эмитент
    # показывается по старшей, остальные называются рядом.
    groups = sorted(
        {basket.group_of(item) for item in ordered} - {""},
        key=lambda item: next(
            entry.order for entry in basket.groups if entry.code == item
        ),
    )
    # Доводы по именам: порядок полей вердикта менялся трижды, и позиционная
    # передача однажды положила зрелость порогов в перечень погашенных.
    return Verdict(
        basket=basket.code,
        basket_name=basket.name,
        grounds=ordered,
        status=routing.status,
        findings=tuple(findings),
        subgroups=tuple(groups),
        subgroup_names=tuple(
            found.name for code in groups if (found := basket.subgroup(code)) is not None
        ),
        actions=tuple(
            found.action
            for code in groups
            if (found := basket.subgroup(code)) is not None
        ),
        muted=muted,
        spoken_for=spoken_for,
        thresholds=routing.thresholds,
    )


def _absent(by_code: dict[str, MetricValue], threshold: Decimal) -> set[str]:
    """Величины решения, которых расчёт не собрал.

    Текущая ликвидность у девелопера заменена диапазоном, и верхняя граница
    заменяет её здесь: «не считается» означало бы пробел данных, а это решение
    методики.
    """
    absent: set[str] = set()
    # **Вывод по границе — доказательство, а не пробел, и он обязан быть
    # виден.** При положительной операционной прибыли амортизация неотрицательна,
    # поэтому отношение к EBITDA не выше отношения к операционной прибыли: граница
    # ниже порога означает, что и показатель ниже. Дефект был не в правиле,
    # а в печати — строка писала «не считается» и границы не показывала.
    # Граница берётся только от операционной прибыли отчётности: замена EBITDA
    # полем источника в неё не входит по устройству показателя.
    bound = by_code.get("net_debt_op_profit")
    proven = bound is not None and bound.calculable and bound.value <= threshold
    if not _ok(by_code, "net_debt_ebitda") and not proven:
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
