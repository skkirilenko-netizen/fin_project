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

**Маршрут один на оба стандарта отчётности, а справочника показателей два.**
Вопрос «нужен ли человек» от стандарта не зависит, коды величин зависят:
в МСФО долговая нагрузка — величина, в РСБУ её нет вовсе, потому что
амортизация в формах не раскрывается, и вывод делается границей. Всё, чем
стандарты различаются, объявлено в `routing.yaml`, блок `standards`,
и приходит сюда доводом `catalogue` (`scoring/routing_catalogue.py`).
Умолчание — МСФО: маршрут начинался с неё, и менять умолчание значило бы
менять поведение вызовов, стандарта не назвавших.

**Отчётности может не быть вовсе, и маршрут при этом строится.** События
и рейтинги от стандарта не зависят: дефолт по выпуску виден у эмитента,
о котором чисел у нас нет ни одного. Обстоятельство называется одно —
«отчётность недоступна», — а не перечнем недостающих величин: перечислять
следствия вместо причины хуже, чем молчать, а молчать нельзя вовсе.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.metrics.display import money, percent
from finlib.metrics.ifrs import MetricValue
from finlib.scoring.ifrs import level
from finlib.scoring.routing_catalogue import (
    RoutingCatalogue,
    StandardRules,
    catalogue_for,
)
from finlib.scoring.theses import load_theses
from finlib.sources.cbonds_events import DEFAULT_STATUSES, DefaultEvent
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# **Величины решения объявлены справочником, а не кодом.** Прежде перечень
# стоял здесь константой — и был перечнем одного стандарта: в РСБУ долговой
# нагрузки нет вовсе, вместо неё граница. Теперь состав называет
# `routing.yaml`, блок `standards`, и берётся он через `catalogue_for`.


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
    # Величина, которой стоп-фактор обязан был сработать, чтобы обстоятельство
    # совпало. Стоп-фактор РСБУ объявлен по паре величин, и совпадает
    # с ликвидностью только одна из них: покрытие процентов ниже единицы —
    # обстоятельство другое, и гасить им ликвидность нельзя. Пусто — стоп-фактор
    # объявлен по одной величине, и уточнять нечего.
    by_value: str = ""
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


def _years_back(today: date, years: int) -> date:
    """Та же дата N лет назад; 29 февраля сдвигается на 28-е.

    Календарь правилу методики не подчиняется, а падать на нём правило
    не вправе. Одна реализация на оба порога давности — дефолта и отзыва
    рейтинга: второе выражение того же расходится с первым раз в четыре года
    и молча.
    """
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, month=2, day=28)


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
    # Давность дефолта: три года. Четыре исхода развёрнуты в `routing.yaml`,
    # и порог предварителен — наблюдений семнадцать.
    default_stale_years: int = Field(gt=0)
    default_stale_origin: str = Field(min_length=1)
    # Давность отзыва рейтинга — год, и она короче дефолтной намеренно:
    # дефолт есть событие с последствиями, отзыв — исчезновение мнения,
    # и через год это данность, а не перемена.
    rating_withdrawn_stale_years: int = Field(gt=0)
    rating_withdrawn_origin: str = Field(min_length=1)
    # Поручительство и оферта — разные обязательства: первое о долге, второе
    # о ликвидности, и корзину поручителя по оферте брать нельзя.
    guarantee_statuses: tuple[str, ...] = Field(min_length=1)
    offer_statuses: tuple[str, ...] = Field(min_length=1)
    guarantee_origin: str = Field(min_length=1)
    credit_scales: dict[str, str] = Field(min_length=1)
    credit_scales_origin: str = Field(min_length=1)
    review_categories: tuple[str, ...] = Field(min_length=1)
    attention_categories: tuple[str, ...] = Field(min_length=1)
    attention_outlooks: tuple[str, ...] = Field(min_length=1)
    categories_origin: str = Field(min_length=1)

    def stale_before(self, today: date) -> date:
        """Дата, раньше которой дефолт считается давним.

        29 февраля сдвигается на 28-е: календарь правилу методики
        не подчиняется, а падать на нём правило не вправе.
        """
        return _years_back(today, self.default_stale_years)

    def rating_stale_before(self, today: date) -> date:
        """Дата, раньше которой отзыв рейтинга считается давним."""
        return _years_back(today, self.rating_withdrawn_stale_years)


class Refinancing(BaseModel):
    """Отсечка срочности долга: платежи года против денежных средств.

    **Единица объяснима словами, а не калибровкой** — денежных средств
    не хватает на платежи ближайших двенадцати месяцев. Замер показал,
    что единица и двойка дают на наборе одно и то же, и выбрана та, у которой
    есть смысл помимо подгонки.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    months: int = Field(gt=0)
    cover_ratio: Decimal = Field(gt=0)
    origin: str = Field(min_length=1)
    calibration_status: str = Field(pattern="^(preliminary|calibrated)$")


class HoldingFallback(BaseModel):
    """Запасной признак холдинга — по отчётности, а не по виду деятельности.

    **Признак, работающий только при доступном источнике, неотличим
    от невыполненного в тот день, когда источник недоступен.** Вид
    деятельности приносит ГИР БО, а он молчал всю ночь на 23.09.2026,
    и правило не срабатывало у 773 организаций из 807.

    Существо то же: организация, у которой активы — вложения в другие
    организации, а собственной выручки почти нет, ведёт не свою деятельность,
    а владеет чужой.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    financial_investments: tuple[str, ...] = Field(min_length=1)
    assets: str = Field(min_length=1)
    revenue: str = Field(min_length=1)
    investments_share: Decimal = Field(gt=0, le=1)
    revenue_share: Decimal = Field(gt=0, le=1)
    origin: str = Field(min_length=1)
    calibration_status: str = Field(pattern="^(preliminary|calibrated)$")

    def share(self, lines: Mapping[str, Decimal | None]) -> Decimal | None:
        """Доля финансовых вложений в активах; None — признак не сработал.

        **Нераскрытая величина признака не даёт.** Ни вложения, ни активы,
        ни выручка нулём не подменяются: признак утверждает об эмитенте,
        и утверждать его по величине, которой нет, нельзя. Выручка обязана
        быть раскрытой: «выручки почти нет» и «выручка не раскрыта» —
        разные сведения, и второе о холдинге не говорит.
        """
        assets = lines.get(self.assets)
        revenue = lines.get(self.revenue)
        if assets is None or assets <= 0 or revenue is None:
            return None
        parts = [lines.get(code) for code in self.financial_investments]
        if any(item is None for item in parts):
            return None
        invested = sum(parts, start=Decimal(0))
        if invested <= assets * self.investments_share:
            return None
        if revenue >= assets * self.revenue_share:
            return None
        return invested / assets


class Holdings(BaseModel):
    """Виды деятельности, при которых отчётность РСБУ описывает не группу.

    Коды сравниваются началом: подвид 64.20.1 означает то же, что 64.20,
    а сравнивать по наименованию нельзя — оно пишется свободно, и слово
    «холдинг» встречается у видов деятельности, к холдингам не относящихся.

    **Признаков два, и второй запасной.** Вид деятельности точнее — он
    объявлен реестром, — но приходит из источника, который бывает недоступен;
    отчётность при этом у нас уже есть. Порядок объявлен: при известном виде
    решает он.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    okved: dict[str, str] = Field(min_length=1)
    fallback: HoldingFallback
    origin: str = Field(min_length=1)
    calibration_status: str = Field(pattern="^(preliminary|calibrated)$")

    def holds(self, okved: str) -> bool:
        """Относится ли вид деятельности к холдинговым."""
        return bool(self.activity(okved))

    def activity(self, okved: str) -> str:
        """Наименование холдингового вида деятельности; пусто — вид не тот.

        Наименование берётся отсюда, а не из выписки: в выписке оно пишется
        свободно, и основание маршрута читал бы человек, которому нужен вид
        деятельности, а не код.
        """
        code = (okved or "").strip()
        if not code:
            return ""
        for item, name in self.okved.items():
            if code == item or code.startswith(f"{item}."):
                return " ".join(name.split())
        return ""


class Systemic(BaseModel):
    """Верхний десяток по объёму долга: «Без внимания» даётся строже.

    Отсечка — доля, а не сумма: рублёвый порог устарел бы с первым
    размещением, а верхний десяток остаётся верхним.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    top_share: Decimal = Field(gt=0, le=1)
    origin: str = Field(min_length=1)
    calibration_status: str = Field(pattern="^(preliminary|calibrated)$")


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
    """Состав списка: кто из него выходит, по какому основанию и куда.

    **Ни один эмитент не покидает список молча.** Выход объявляется причиной,
    датой и преемником; неподтверждённое — не выход, а очередь «установить
    статус эмитента».

    **Поле поглощения выходом не является.** Оно названо у источника
    «Компания, оставшаяся после слияния/поглощения» и заполнено у живых тоже —
    у Ростелекома, МегаФона, Норникеля. Прочитанное как «поглощён», оно вывело
    из списка 24 живых эмитента; решает статус, а поле лишь называет преемника.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statuses: dict[str, str] = Field(min_length=1)
    statuses_origin: str = Field(min_length=1)
    exclude_statuses: tuple[str, ...] = Field(min_length=1)
    require_successor: bool
    unconfirmed_to: str = Field(min_length=1)
    origin: str = Field(min_length=1)

    def status_of(self, code: str) -> str:
        """Наименование статуса по коду; неизвестный код называется кодом.

        **Неизвестный статус молча действующим не становится**: справочника
        у API нет, значения выведены из описания поля, и новое значение обязано
        быть замечено, а не приравнено к уже известному.
        """
        return self.statuses.get(str(code), f"статус {code} не опознан")

    def known_status(self, code: str) -> bool:
        """Опознан ли статус карточки."""
        return str(code) in self.statuses


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
    # Чем зовутся величины маршрута в каждом стандарте: маршрут один,
    # справочника показателей два.
    standards: StandardRules
    same_circumstance: tuple[SameCircumstance, ...] = ()
    stop_factor_muted: tuple[MutedStopFactor, ...] = ()
    events: Events
    statements: Statements
    # Откуда пришло обстоятельство: выпуск, рейтинг, отчётность, группа,
    # поручитель. Перечень один на замер и на выгрузку — второй экземпляр
    # разошёлся бы при первом же новом основании.
    ground_sources: dict[str, str] = Field(min_length=1)
    universe: Universe
    severity: Severity
    systemic: Systemic
    holdings: Holdings
    refinancing: Refinancing
    freshness: Freshness
    # **Чего список не проверяет и чем ограничен.** Объявлено методикой,
    # а не написано в коде страницы: текст, который читатель принимает
    # за оговорку методики, правится диффом, как всякая формулировка.
    limitations: tuple[str, ...] = Field(min_length=1)
    baskets: tuple[Basket, ...] = Field(min_length=3)
    # **Справочные основания корзины не называют.** Урегулированный дефолт
    # десятилетней давности о сегодняшнем эмитенте не говорит, но и молчать
    # о нём нельзя: человек найдёт признак в карточке сам и не поймёт, почему
    # маршрут его не заметил. Объявлены отдельно от корзин именно потому, что
    # корзину не назначают.
    reference: tuple[Ground, ...] = ()

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
        } | {ground.code for ground in self.reference}
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
        # Справочное основание корзины не называет — и не может называть
        # её заодно: одно основание с двумя исходами читалось бы как правило,
        # а было бы порядком проверок.
        both = {ground.code for ground in self.reference} & {
            ground.code for basket in self.baskets for ground in basket.grounds
        }
        if both:
            raise ValueError(
                "основания объявлены и справочными, и основаниями корзины: "
                + ", ".join(sorted(both))
            )
        # Источник объявлен у каждого основания: корзина без источника ничего
        # не доказывает, а перечень вторым экземпляром разошёлся бы с первым.
        nameless = declared - set(self.ground_sources)
        if nameless:
            raise ValueError(
                "у оснований не объявлен источник: " + ", ".join(sorted(nameless))
            )
        unknown = set(self.ground_sources) - declared
        if unknown:
            raise ValueError(
                "источник объявлен у оснований, которых нет: "
                + ", ".join(sorted(unknown))
            )
        return self

    def source_of(self, ground: str) -> str:
        """Откуда пришло основание; неизвестное основание — свой же код."""
        return self.ground_sources.get(ground, ground)

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
class Refinance:
    """Платежи по облигациям ближайших месяцев против денежных средств.

    **Обе величины приведены к единице комплекта, и единица названа рядом.**
    Объём выпуска источник отдаёт в рублях, отчётность бывает в миллионах,
    и число без единицы читатель прочтёт в той, которую предположит сам.

    `due is None` означает, что графиков платежей нет на диске; `cash is None` —
    что денежные средства не раскрыты. Ни то ни другое не ноль.
    """

    due: Decimal | None
    cash: Decimal | None
    unit: str
    months: int
    # **Вторая мера: те же платежи при предъявлении оферт.** Предъявление —
    # право владельца, и в отсечку корзины оно не входит; но величина
    # считалась и не доходила ни до одного выхода, то есть была неотличима
    # от невыполненного правила. `None` — ответа об офертах по выпускам нет
    # на диске, и это не «оферт нет».
    offered: Decimal | None = None
    # **Знаменатели обеих мер.** «К погашению ноль» у эмитента, графиков
    # которого нет на диске, и у эмитента без платежей — разные сведения;
    # то же у оферт. Считаются они по выпускам, а не по эмитентам: график
    # доставляется на выпуск.
    issues: int = 0
    without_schedule: int = 0
    without_offers: int = 0


@dataclass(frozen=True, slots=True)
class ManualFloor:
    """Решение человека о корзине: не ниже названной, с именем и сроком.

    **Обстоятельство, которого машина не видит, вносит человек.** Статуса
    наблюдения у источника нет вовсе: у Русагро АКРА объявило наблюдение
    22.04.2026, а в карточке стоит стабильный прогноз, и одно из другого
    не следует.

    «Не ниже», а не «назначить»: решение добавляется к машинным основаниям,
    а не отменяет их, — иначе журнал стал бы способом вывести эмитента
    из разбора. Срок обязателен: наблюдение агентства снимается, а запись
    о нём без срока пережила бы своё основание.
    """

    basket: str
    author: str
    reason: str
    decided_on: date
    valid_until: date


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
    # Справочные обстоятельства: корзину не называют, но и не исчезают.
    # Урегулированный дефолт десятилетней давности сюда и попадает.
    notes: tuple[Finding, ...] = ()

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
    # Наименование денежной единицы комплекта. Довод обязательный: основания
    # маршрута печатают денежные величины, а единая точка печати без единицы
    # их не печатает вовсе. Прежде она подставляла «тыс. руб.» умолчанием,
    # и 39 строк списка подписали миллионы тысячами.
    unit: str,
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
    # Платежи по облигациям ближайших месяцев против денежных средств:
    # срочность долга, которой в балансе нет вовсе.
    refinance: "Refinance | None" = None,
    # Поручитель эмитента, стоящий в разборе: его обстоятельство говорит
    # о том, кто отвечает по долгу, и член такой пары не мягче внимания.
    guarantor_under_review: str = "",
    # Объём долга в обращении, если эмитент в верхнем десятке по нему:
    # такому «Без внимания» даётся строже — только при полном покрытии.
    systemic_volume: Decimal | None = None,
    # Выпуски, переведённые биржей в сектор повышенного риска: решение биржи
    # с датой, а не наше суждение о величинах.
    risk_sector: tuple[object, ...] = (),
    # Статус карточки говорит о ликвидации, а преемник не подтверждён: эмитент
    # из списка не выходит, но и величинами о нём судить нельзя.
    status_unconfirmed: str = "",
    # Действующее решение человека о корзине: обстоятельство, которого машина
    # не видит. Истёкшие решения сюда не доходят — их отбирает выборка журнала.
    manual_floor: "ManualFloor | None" = None,
    # Поручитель финансирующей структуры, названный источником. Корзина его
    # берётся вторым проходом; здесь он нужен, чтобы формулировка не говорила
    # о группе там, где речь о том, кто отвечает по долгу.
    guarantor: str = "",
    # Чем присвоен класс: вид отчётности и отчётная дата. Страницы у оценки
    # нет — класс присвоен комплекту, а не месту в документе.
    assessed_where: str = "",
    # События эмитента: выпуски и рейтинги. Годовая отчётность их не видит
    # по устройству, и без них «Без внимания» стоит у эмитента с дефолтом.
    events: object | None = None,
    # Основной вид деятельности из ЕГРЮЛ: у холдинга отчётность РСБУ описывает
    # управляющую компанию, а не группу, и это обстоятельство маршрута.
    okved: str = "",
    # Строки, по которым холдинг опознаётся запасным признаком, когда вида
    # деятельности у нас нет: финансовые вложения, активы, выручка.
    holding_lines: Mapping[str, Decimal | None] | None = None,
    # Почему отчётности нет вовсе: пусто — она есть. Обстоятельство одно,
    # а не три недостающие величины, и называется оно причиной.
    reporting_unavailable: str = "",
    today: date | None = None,
    routing: RoutingPolicy | None = None,
    # Справочник величин своего стандарта: коды, шкалы, наименования, печать
    # и стоп-факторы. Умолчание — МСФО: маршрут начинался с неё, и менять
    # умолчание значило бы менять поведение вызовов, стандарта не назвавших.
    catalogue: RoutingCatalogue | None = None,
    # Величина, которой сработал стоп-фактор: код стоп-фактора → код
    # показателя. Нужна там, где стоп-фактор объявлен сразу по нескольким
    # величинам, — «по какому проверяется» и «каким сработал» это разные
    # сведения, и второе знает расчёт, а не справочник.
    stop_factor_values: dict[str, str] | None = None,
) -> Verdict:
    """Определяет корзину эмитента по посчитанным величинам и обстоятельствам.

    Довод `computed` — итог боевого расчёта, а не собранный вручную словарь:
    второй путь к тем же величинам разошёлся бы с первым, и корзина зависела
    бы от того, кто спросил.
    """
    routing = routing or load_routing()
    catalogue = catalogue or catalogue_for(Standard.IFRS)
    rule = catalogue.rule
    fired = stop_factor_values or {}
    caps = {factor.code: factor.cap for factor in catalogue.stop_factors}
    by_code = {item.code: item for item in computed}
    review: list[Finding] = []
    attention: list[Finding] = []
    # Справочные обстоятельства: корзину не называют, но и молчать о них
    # нельзя — молчание маршрута человек прочтёт как недосмотр.
    notes: list[Finding] = []

    status: list[Finding] = []
    # **Неподтверждённый выход — не выход, а вопрос о статусе.** Прежде такой
    # эмитент исчезал из списка по полю поглощения, и вместе с ним исчезли
    # 24 живых.
    if status_unconfirmed:
        status.append(
            Finding(
                "status_not_confirmed",
                "",
                routing.say("status_not_confirmed", reason=status_unconfirmed),
            )
        )
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
    by_factor = {factor.code: factor for factor in catalogue.stop_factors}

    def about(code: str) -> str:
        # Формулировка стоп-фактора: наименование и его величина в скобках.
        # «Стоп-фактор: отрицательный собственный капитал» без величины
        # заставляет читателя искать её в других графах.
        #
        # **Печатается та величина, которой стоп-фактор сработал.** Объявлен
        # он бывает по нескольким: у РСБУ «Нехватка оборотного капитала
        # и покрытия процентов» — по двум, и напечатать заранее выбранную
        # значило бы назвать величину, основанием не ставшую.
        factor = by_factor.get(code)
        metric = fired.get(code) or (factor.metric if factor is not None else "")
        extra = ""
        if metric:
            item = by_code.get(metric)
            if item is not None and item.calculable:
                extra = (
                    f" ({catalogue.name_of(metric).lower()} "
                    f"{catalogue.shown(metric, item.value, unit)})"
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
    # **Группа карточки — справочное обстоятельство, а не основание корзины.**
    # Поле источника отражает бенефициара, и проверка семи названных пар
    # не нашла ни одной финансовой связи: ассоциированное общество (Озон
    # у «Системы»), совместное предприятие (Славнефть у «Газпрома»), дочерняя
    # вместо материнской (ОАК в группе «ИРКУT»), общий бенефициар (ПГК
    # у НЛМК, СТМ у ТМК) и совпадение написаний (Монополия и ГТМ
    # у «Globaltrans»). Признака головной компании у источника нет вовсе,
    # поэтому распространять вниз нечего, а вверх — нечем: консолидированных
    # активов группы у нас нет.
    if group_under_review is not None:
        whose, member = group_under_review
        notes.append(
            Finding(
                "group_under_review",
                whose,
                routing.say("group_under_review", group=whose, leader=member),
            )
        )
    # **Поручитель — тот же контур, только объявленный договором.** Корзина
    # его не переносится: разбор сказан о нём, а не о заёмщике, — но член
    # такой пары не мягче внимания.
    if guarantor_under_review:
        attention.append(
            Finding(
                "guarantor_under_review",
                guarantor_under_review,
                routing.say(
                    "guarantor_under_review", guarantor=guarantor_under_review
                ),
            )
        )
    # **Перевод в сектор риска — событие биржи, и корзина у него разбора.**
    # Биржа не предполагает и не считает: она перевела бумагу в другой режим
    # и день назвала. Обстоятельство при этом о выпуске, поэтому называется
    # каждый переведённый выпуск, а не эмитент одной строкой.
    for entry in risk_sector:
        named = str(getattr(entry, "name", "") or getattr(entry, "isin", ""))
        board = str(getattr(entry, "board", ""))
        when = getattr(entry, "since", None)
        review.append(
            Finding(
                "risk_sector",
                str(getattr(entry, "isin", "")),
                routing.say(
                    "risk_sector",
                    "" if when is not None else "undated",
                    issue=named,
                    board=board,
                    date=f"{when:%d.%m.%Y}" if when is not None else "",
                    came_from=str(getattr(entry, "came_from", "")) or "не назван",
                ),
            )
        )
    if events is not None:
        by_default, watched, referenced = _default_findings(
            events, routing, today or date.today()
        )
        review.extend(by_default)
        review.extend(_rating_findings(events, routing))
        attention.extend(watched)
        attention.extend(_rating_outlook_adverse(events, routing))
        attention.extend(_rating_withdrawn(events, routing, today or date.today()))
        notes.extend(referenced)
    if financing_structure:
        # **Поручитель вне списка — не то же самое, что поручителя нет.**
        # Первое называет того, кто отвечает по долгу, и говорит, что корзины
        # у него взять негде; второе оставляет только группу. Корзина
        # поручителя, стоящего в списке, берётся вторым проходом
        # (`led_by_guarantor`): она известна лишь после того, как посчитаны все.
        review.append(
            Finding(
                "financing_structure",
                guarantor or group,
                routing.say(
                    "financing_structure",
                    "guarantor_unlisted" if guarantor else "",
                    group=group or "не названа в справочнике",
                    guarantor=guarantor,
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
    debt_threshold = catalogue.threshold_of(rule.bound_of)
    # **Отчётности нет вовсе — одно обстоятельство, а не перечень пробелов.**
    # Маршрут при этом строится: события и рейтинги от стандарта не зависят.
    # Называть три недостающие величины значило бы перечислять следствия,
    # а молчать — выдавать отсутствие данных за отсутствие обстоятельств.
    absent: set[str] = set()
    if reporting_unavailable:
        attention.append(
            Finding(
                "reporting_unavailable",
                "",
                routing.say("reporting_unavailable", reporting_unavailable),
            )
        )
    else:
        absent = _absent(by_code, debt_threshold, rule, operating_profit)
    # **Рефинансирование — обстоятельство отчётности, а не события.** График
    # платежей говорит о срочности долга, которой в балансе нет: «долг
    # 40 млрд» у эмитента с погашением через восемь лет и с погашением
    # в марте означает разное.
    if refinance is not None and refinance.due:
        if refinance.cash is None:
            # Величина платежей есть, знаменателя нет: это пробел данных,
            # а не обстоятельство риска, и поле называется.
            absent.add("денежные средства")
        elif refinance.cash * routing.refinancing.cover_ratio < refinance.due:
            attention.append(
                Finding(
                    "refinancing_gap",
                    "refinancing",
                    routing.say(
                        "refinancing_gap",
                        months=refinance.months,
                        due=money(refinance.due),
                        cash=money(refinance.cash),
                        unit=refinance.unit,
                    ),
                )
            )
    # **Вторая мера рефинансирования — основание внимания** (решение человека
    # 23.09.2026). Предъявление оферты к выкупу есть право владельца, а не
    # обязанность эмитента, и прежде мера была справочной: корзина по чужому
    # праву означала бы «человек нужен» у всякого эмитента с офертой в окне.
    # Довод снят существом права — предъявляют оферту именно в стрессе,
    # и ошибиться в мягкую сторону здесь дешевле. Отсечка та же, своего
    # числа здесь нет.
    if (
        refinance is not None
        and refinance.offered
        and refinance.cash is not None
        and refinance.cash * routing.refinancing.cover_ratio < refinance.offered
    ):
        attention.append(
            Finding(
                "refinancing_offers",
                "refinancing",
                routing.say(
                    "refinancing_offers",
                    months=refinance.months,
                    offered=money(refinance.offered),
                    cash=money(refinance.cash),
                    unit=refinance.unit,
                ),
            )
        )
    # **Крупному долгу «Без внимания» даётся строже.** Оценка сверху
    # прохождение критерия доказывает, а величину не заменяет, и у эмитента
    # верхнего десятка цена этой замены выше всех прочих. Обстоятельство
    # здесь о нашем знании, а не о нём.
    if systemic_volume is not None and not reporting_unavailable:
        # Состав полноты объявлен стандартом: у МСФО требуется сама величина
        # нагрузки, у РСБУ — граница, потому что иной величины там не бывает
        # вовсе, и требовать её значило бы объявить неполным всякого эмитента
        # без консолидированной отчётности.
        thin = [
            catalogue.name_of(code).lower()
            for code in rule.cover
            if not _ok(by_code, code)
        ]
        if thin:
            attention.append(
                Finding(
                    "systemic_partial_cover",
                    "systemic",
                    routing.say(
                        "systemic_partial_cover",
                        volume=money(systemic_volume),
                        unit="руб.",
                        missing="не рассчитано — " + ", ".join(sorted(thin)),
                    ),
                )
            )
    for name in sorted(absent):
        attention.append(
            Finding(
                "data_insufficient", name, routing.say("data_insufficient", field=name)
            )
        )

    lower = load_theses().bands.lower_below
    spoken_for = _spoken_for(stop_factors, routing, catalogue, fired)
    silenced: list[str] = []

    # **Знак EBITDA — своё основание, и он гасит величины отношения к ней.**
    # Отношение чистого долга к неположительной EBITDA отрицательно и читается
    # шкалой как низкая нагрузка: у эмитента с убытком выходило бы «без
    # внимания». Обстоятельство при этом одно, поэтому величина отношения
    # своего основания не даёт — его даёт знак.
    ebitda = by_code.get(rule.earnings) if rule.earnings else None
    if ebitda is not None and ebitda.calculable and ebitda.value <= 0:
        attention.append(
            Finding(
                "negative_ebitda",
                rule.earnings,
                routing.say(
                    "negative_ebitda",
                    value=catalogue.shown(rule.earnings, ebitda.value, unit),
                ),
            )
        )
        if rule.burden:
            spoken_for = spoken_for | {rule.burden}
    for code in rule.metrics:
        item = by_code.get(code)
        scale = catalogue.scale(code)
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
                        value=catalogue.shown(code, item.value, unit),
                        threshold=catalogue.shown(code, scale.points[0][0], unit),
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
                        value=catalogue.shown(code, item.value, unit),
                    ),
                )
            )

    bound = by_code.get(rule.bound) if rule.bound else None
    if _bound_proves(bound, operating_profit) and bound.value > debt_threshold:
        attention.append(
            Finding(
                "bound_above_threshold",
                bound.code,
                # **Граница сверху — не «не менее».** Настоящее значение
                # бывает и ниже порога: это отсутствие вывода, а не плохая
                # величина, и формулировка обязана говорить именно так.
                routing.say(
                    "bound_above_threshold",
                    value=catalogue.shown(rule.bound_of, bound.value, unit),
                ),
            )
        )

    if operating_profit is not None and operating_profit <= 0:
        # Величина денежная, и печатается она единой точкой округления:
        # «-7378338.000» — не число для человека, а внутреннее представление.
        # **Единица называется здесь же.** Прежде основание печатало число
        # без неё, «объявить единицу» оставалось тому, кто печатает строку
        # целиком, и никто её не объявлял: строка списка читалась в тысячах
        # у эмитента, отчитавшегося в миллионах.
        attention.append(
            Finding(
                "operating_loss",
                rule.operating_line,
                routing.say(
                    "operating_loss", value=money(operating_profit), unit=unit
                ),
            )
        )

    # **Отчётность управляющей компании группой не является.** Действует
    # только там, где маршрут построен по РСБУ: у эмитента с консолидированной
    # отчётностью группа видна, и холдинговый вид деятельности ничего
    # не скрывает.
    # **Признаков два, и порядок между ними объявлен**: вид деятельности
    # точнее — он объявлен реестром, — но приходит из источника, который
    # бывает недоступен. Запасной признак читает ту же отчётность, по которой
    # построен маршрут, и работает, когда вида деятельности у нас нет.
    if catalogue.standard is Standard.RSBU and not reporting_unavailable:
        invested = routing.holdings.fallback.share(holding_lines or {})
        if routing.holdings.holds(okved):
            attention.append(
                Finding(
                    "holding_rsbu_only",
                    okved,
                    routing.say(
                        "holding_rsbu_only",
                        okved=okved,
                        activity=routing.holdings.activity(okved),
                    ),
                )
            )
        elif invested is not None:
            attention.append(
                Finding(
                    "holding_rsbu_only",
                    "financial_investments",
                    routing.say(
                        "holding_rsbu_only",
                        "by_values",
                        share=percent(invested * 100),
                    ),
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

    # **Решение человека — основание наравне с машинными, и последнее
    # по очереди.** Оно добавляется к тому, что нашёл маршрут, а не заменяет
    # найденное: «не ниже» означает пол, а не назначение корзины.
    if manual_floor is not None:
        told = Finding(
            "manual_floor",
            manual_floor.author,
            routing.say(
                "manual_floor",
                author=manual_floor.author,
                reason=manual_floor.reason,
                date=f"{manual_floor.decided_on:%d.%m.%Y}",
                until=f"{manual_floor.valid_until:%d.%m.%Y}",
            ),
        )
        (review if manual_floor.basket == "review" else attention).append(told)

    found = status + review + attention
    if status:
        # Очередь статуса старше корзин тяжести: по числам такой давности
        # решение принимать нельзя, каким бы тяжёлым обстоятельство ни было.
        code = "status_unknown"
    elif review:
        code = "review"
    else:
        code = "attention" if attention else "clear"
    return _verdict(
        routing, code, found, tuple(silenced), muted, notes=tuple(notes)
    )


def led_by_guarantor(
    verdict: Verdict,
    guarantor: str,
    guaranteed: Verdict,
    group: str,
    routing: RoutingPolicy,
) -> Verdict:
    """Вердикт финансирующей структуры, взятый у её поручителя.

    **Финансирующая структура собой не оценивается, и это не смягчение.**
    У SPV «прочие» — внутригрупповые займы, а отрицательный капитал бывает
    устройством: величины её описывают договор, а не деятельность. Отвечает
    по долгу поручитель, и корзина берётся у него — вместе с его основаниями,
    потому что человеку, открывшему строку, нужны они, а не наши.

    **Корзина поручителя бывает любой, в том числе «Без внимания».** Тогда
    и SPV в ней, а объяснение остаётся справочным: без него строка выглядит
    как решение, принятое по её собственным величинам.

    Второй проход здесь неизбежен по той же причине, что у группового контура:
    корзину решает обстоятельство другого эмитента, известное только после
    того, как посчитаны все.
    """
    text = routing.say(
        "financing_structure",
        "by_guarantor",
        group=group or "не названа в справочнике",
        guarantor=guarantor,
    )
    told = Finding("financing_structure", guarantor, text)
    # Свои основания финансирующей структуры остаются в перечне: они
    # не называют корзину, но человек, разбирающий строку, видит и их.
    own = tuple(
        item for item in verdict.findings if item.ground != "financing_structure"
    )
    return Verdict(
        basket=guaranteed.basket,
        basket_name=guaranteed.basket_name,
        grounds=guaranteed.grounds,
        status=guaranteed.status,
        findings=guaranteed.findings + own,
        subgroups=guaranteed.subgroups,
        subgroup_names=guaranteed.subgroup_names,
        actions=guaranteed.actions,
        muted=verdict.muted,
        spoken_for=verdict.spoken_for,
        thresholds=guaranteed.thresholds,
        notes=(told,) + verdict.notes,
    )


def _default_findings(
    events: object, routing: RoutingPolicy, today: date
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Дефолты выпусков: разбор, внимание и справочное — порознь.

    **Давность разводит четыре исхода, и разводит по двум признакам.** Улажен
    ли дефолт — говорит карточка выпуска; давно ли — дата, которой источник
    не приводит вовсе и которая не восстанавливается графиком платежей
    (`cbonds_events.default_event`). Отсюда:

    | улажен | давность | исход |
    |---|---|---|
    | нет | до трёх лет либо неизвестна | разбор |
    | нет | старше | внимание: проверить статус урегулирования |
    | да | до трёх лет | внимание: кредитная история |
    | да | старше | справочно |

    **Неизвестная давность корзину не понижает.** У ЕвроТранса и Антерры
    выпуски в обращении, и дата погашения лежит в будущем: она говорит
    о сроке, а не о событии. Понизить корзину по ней значило бы принять
    решение по отсутствию данных.
    """
    review: list[Finding] = []
    attention: list[Finding] = []
    notes: list[Finding] = []
    if not routing.events.default_review:
        return review, attention, notes
    edge = routing.events.stale_before(today)
    years = routing.events.default_stale_years

    unsettled = bool(getattr(events, "unsettled_default", False))
    settled_only = bool(getattr(events, "settled_only", False))
    if not unsettled and not settled_only:
        return review, attention, notes
    event = events.event() if hasattr(events, "event") else DefaultEvent(None, "", "")
    # Событие без даты давности не имеет: считать её по другому событию
    # значило бы объявить старым то, о чём даты нет.
    undated = bool(getattr(events, "undated_records", ()))
    stale = event.known and event.when < edge and not undated

    if unsettled and stale:
        attention.append(
            Finding(
                "default_unsettled_stale",
                _issue_name(events, event.issue) or event.issue,
                routing.say(
                    "default_unsettled_stale",
                    year=f"{event.when:%Y} года",
                    where=event.origin or "вид события источник не называет",
                ),
            )
        )
    elif unsettled:
        # **Свежий либо недатированный неурегулированный дефолт — разбор,
        # и он называется по выпускам.** Перечня дефолтов может не быть
        # на диске вовсе, и тогда давность неизвестна: понизить корзину
        # по неизвестной давности значило бы решить по отсутствию данных.
        for issue in getattr(events, "defaulted", ()):
            where = _where_of(events, issue)
            # **Статус выпуска и признак неурегулированности — разные
            # сведения.** «Дефолт по погашению» говорит, что случилось;
            # признак у выпуска в обращении — что случившееся не улажено,
            # и называть это «в обращении» значило бы сказать обратное.
            what = (
                issue.status.capitalize()
                if issue.status in DEFAULT_STATUSES
                else f"Неурегулированный дефолт (выпуск в статусе «{issue.status}»)"
            )
            review.append(
                Finding(
                    "emission_default",
                    issue.name,
                    routing.say(
                        "emission_default", what=what, issue=issue.name, where=where
                    ),
                )
            )
        if not review:
            # Признак стоит у событий, а не у выпуска: обстоятельство
            # называется им, иначе оно исчезнет вместе с корзиной.
            review.append(
                Finding(
                    "emission_default",
                    event.issue,
                    routing.say(
                        "emission_default",
                        what="Неурегулированный дефолт",
                        issue=event.issue or "выпуск источник не называет",
                        where=event.origin or "даты события источник не приводит",
                    ),
                )
            )
    elif settled_only:
        # **Выпуск называется наименованием, а не своим номером у источника.**
        # «Выпуск 525165» человеку не говорит ничего, а событие приходит
        # с идентификатором, а не с наименованием.
        issue = _issue_name(events, event.issue) or _issues_named(
            getattr(events, "settled", ())
        )
        if stale:
            notes.append(
                Finding(
                    "default_settled_stale",
                    issue,
                    routing.say(
                        "default_settled_stale",
                        year=f"{event.when:%Y}",
                        years=years,
                        issue=issue,
                    ),
                )
            )
        else:
            attention.append(
                Finding(
                    "default_settled_recent",
                    issue,
                    routing.say(
                        "default_settled_recent",
                        "" if event.known else "no_year",
                        year=f"{event.when:%Y}" if event.known else "",
                        issue=issue,
                    ),
                )
            )
    return review, attention, notes


def _issue_name(events: object, emission_id: str) -> str:
    """Наименование выпуска по идентификатору источника; пусто — не найдено."""
    for item in getattr(events, "issues", ()):
        if getattr(item, "emission_id", "") == emission_id:
            return str(getattr(item, "name", ""))
    return ""


def _where_of(events: object, issue: object) -> str:
    """Чем подтверждён дефолт выпуска: событие с суммой либо срок погашения.

    **Неисполненная сумма — сведение, которого нет больше нигде.** «Дефолт
    по погашению» говорит, что случилось; «купон, срок 03.08.2026, не исполнено
    86 602 000» говорит, сколько именно не заплатили.
    """
    mine = [
        item
        for item in getattr(events, "open_records", ())
        if item.emission_id == getattr(issue, "emission_id", "")
    ]
    dated = [item for item in mine if item.moment is not None]
    if dated:
        # На одну дату приходится и купон, и погашение — у Кириллицы 4 068 000
        # и 300 000 000: называется большее, потому что оно и есть предмет.
        latest = max(
            dated, key=lambda item: (item.moment, item.amount or Decimal(0))
        )
        text = f"{latest.kind.lower()} {latest.moment:%d.%m.%Y}, {latest.status.lower()}"
        if latest.amount is not None:
            # **Единица называется.** Величина события приходит в рублях,
            # а не в единице комплекта, и число без единицы читатель прочтёт
            # в той, которую предположит сам.
            text += f", не исполнено {money(latest.amount)} руб."
        return text
    # **«Перечня нет» и «события не датированы» — разные сведения.** Первое
    # о нашей доставке, второе об источнике, и путать их значило бы выдать
    # недошедшие данные за свойство источника.
    if not getattr(events, "records_known", False):
        return "перечня событий дефолта на диске нет"
    maturity = getattr(issue, "maturity", None)
    if maturity is not None:
        return f"погашение {maturity:%d.%m.%Y}, события источник не датирует"
    return "даты события источник не приводит"


def _issues_named(issues: tuple[object, ...]) -> str:
    """Выпуски словами: один называется, у нескольких назван первый и число."""
    names = [str(getattr(item, "name", "")) for item in issues]
    names = [item for item in names if item and item != "—"]
    if not names:
        return "наименование источник не приводит"
    return names[0] if len(names) == 1 else f"{names[0]} и ещё {len(names) - 1}"


def _rating_findings(events: object, routing: RoutingPolicy) -> list[Finding]:
    """Основание разбора из рейтинга: категория дефолта либо преддефолтная."""
    found: list[Finding] = []
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


def _rating_outlook_adverse(events: object, routing: RoutingPolicy) -> list[Finding]:
    """Основание внимания: спекулятивная категория с неблагоприятным прогнозом.

    Категория сама по себе — уровень, а не событие; событием её делает
    объявленный агентством прогноз. **Неблагоприятны оба объявленных**, и имя
    основания это называет: прежде оно звалось `rating_watch` и «негативным
    прогнозом», а срабатывало и на «развивающийся». Слово «watch» при этом
    обещало статус наблюдения, которого у источника нет вовсе.
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
                    "rating_outlook_adverse",
                    rating.category,
                    routing.say(
                        "rating_outlook_adverse",
                        point=rating.point,
                        agency=rating.agency,
                        outlook=rating.outlook.lower(),
                    ),
                )
            )
    return found


def _rating_withdrawn(
    events: object, routing: RoutingPolicy, today: date
) -> list[Finding]:
    """Основание внимания: кредитные рейтинги отозваны всеми агентствами.

    **Отзыв — исчезновение мнения, а не суждение о риске.** Агентство,
    снимая рейтинг, об эмитенте не говорит ничего: причину источник
    не раскрывает, а причин две и они противоположны — инициатива агентства,
    у которого не стало сведений, и окончание договора с эмитентом. Поэтому
    корзина «Внимание», а не «Разбор», и формулировка называет нераскрытость
    причины прямо: без неё читатель достроит её сам.

    **Обстоятельство — перемена, и потому отсутствие рейтинга сюда не входит.**
    У эмитента, которого не оценивали никогда, мнение не исчезало.

    **Неизвестная дата корзину не понижает** — то же правило, что у дефолта:
    решение по отсутствию данных было бы решением ни о чём.
    """
    unrated = getattr(events, "left_unrated", None)
    left, moment = unrated() if unrated is not None else (False, None)
    if not left:
        return []
    if moment is not None and moment < routing.events.rating_stale_before(today):
        return []
    agencies = sorted({item.agency for item in getattr(events, "revoked", ())})
    named = ", ".join(agencies) if agencies else "агентство источник не называет"
    return [
        Finding(
            "rating_withdrawn",
            "rating",
            routing.say(
                "rating_withdrawn",
                "" if moment is not None else "undated",
                agencies=named,
                date=f"{moment:%d.%m.%Y}" if moment is not None else "",
            ),
        )
    ]


def _spoken_for(
    stop_factors: tuple[str, ...],
    routing: RoutingPolicy,
    catalogue: RoutingCatalogue,
    fired: dict[str, str],
) -> set[str]:
    """Показатели, о которых уже сказал сработавший стоп-фактор.

    Два источника, и оба нужны. Совпадение кода показателя действует само:
    отрицательная автономия проверяется по тому же `equity_ratio`, что
    и маршрут. Совпадение предмета при разных кодах объявляется методикой
    (`same_circumstance`): отрицательный чистый оборотный капитал считается
    по `nwc`, а ликвидность ниже единицы — по `cur_liq`, и то, что это одно
    обстоятельство, выводу из имён не поддаётся.

    **Уточнение величиной.** Стоп-фактор бывает объявлен сразу по нескольким
    величинам — у РСБУ «Нехватка оборотного капитала и покрытия процентов»
    по двум, — и совпадает с ликвидностью только одна из них. Поэтому пара
    объявляет, какой величиной стоп-фактор обязан был сработать; сработавшую
    называет расчёт, а не справочник.
    """
    triggered = set(stop_factors)
    metrics = {
        factor.metric
        for factor in catalogue.stop_factors
        if factor.code in triggered and factor.metric
    }
    metrics |= {fired[code] for code in triggered if fired.get(code)}
    metrics |= {
        item.metric
        for item in routing.same_circumstance
        if item.stop_factor in triggered
        and (not item.by_value or fired.get(item.stop_factor) == item.by_value)
    }
    return metrics


def _bound_proves(bound: MetricValue | None, operating_profit: Decimal | None) -> bool:
    """Доказывает ли вывод по границе хоть что-нибудь.

    **Граница осмысленна только при положительном знаменателе.** Отношение
    чистого долга к убытку отрицательно, и шкала читает его как низкую
    нагрузку — тот же обман, что у отношения к неположительной EBITDA.
    У показателя МСФО положительность знаменателя объявлена самим
    справочником (`denominator_must_be_positive`), у РСБУ — нет: там
    та же формула считается и при убытке, и правило обязано стоять здесь.

    Знаменатель берётся у самой границы — он хранится рядом с отношением, —
    и лишь когда его нет, у величины отчётности, поданной доводом.
    """
    if bound is None or not bound.calculable:
        return False
    if bound.denominator is not None:
        return bound.denominator > 0
    return operating_profit is None or operating_profit > 0


def _verdict(
    routing: RoutingPolicy,
    code: str,
    findings: list[Finding],
    spoken_for: tuple[str, ...] = (),
    muted: tuple[str, ...] = (),
    notes: tuple[Finding, ...] = (),
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
        notes=notes,
    )


def _absent(
    by_code: dict[str, MetricValue],
    threshold: Decimal,
    rule: object,
    operating_profit: Decimal | None,
) -> set[str]:
    """Величины решения, которых расчёт не собрал.

    Текущая ликвидность у девелопера заменена диапазоном, и верхняя граница
    заменяет её здесь: «не считается» означало бы пробел данных, а это решение
    методики.

    **Что чем зовётся, объявляет стандарт.** У МСФО долговая нагрузка —
    величина, и граница лишь доказывает её прохождение; у РСБУ величины нет
    вовсе, и граница — единственный способ о ней судить. Наименования
    недостающего берутся из справочника: основание читает человек,
    и `net_debt_ebitda` ему не говорит ничего.
    """
    absent: set[str] = set()
    names: dict[str, str] = dict(getattr(rule, "absent_names", {}))
    # **Вывод по границе — доказательство, а не пробел, и он обязан быть
    # виден.** При положительной операционной прибыли амортизация неотрицательна,
    # поэтому отношение к EBITDA не выше отношения к операционной прибыли: граница
    # ниже порога означает, что и показатель ниже. Дефект был не в правиле,
    # а в печати — строка писала «не считается» и границы не показывала.
    # Граница берётся только от операционной прибыли отчётности: замена EBITDA
    # полем источника в неё не входит по устройству показателя.
    bound_code = getattr(rule, "bound", "")
    bound = by_code.get(bound_code) if bound_code else None
    proven = _bound_proves(bound, operating_profit) and bound.value <= threshold
    replaced = dict(getattr(rule, "replaced_by", {}))
    for code in getattr(rule, "cover", ()):
        if _ok(by_code, code):
            continue
        if proven and code in (bound_code, getattr(rule, "burden", "")):
            continue
        if code in replaced and _ok(by_code, replaced[code]):
            continue
        absent.add(names.get(code, code))
    return absent


def _ok(by_code: dict[str, MetricValue], code: str) -> bool:
    """Рассчитан ли показатель."""
    item = by_code.get(code)
    return item is not None and item.calculable


def value_of(computed: tuple[MetricValue, ...], code: str) -> Decimal | None:
    """Величина показателя по коду; None — не рассчитан."""
    item = next((entry for entry in computed if entry.code == code), None)
    return item.value if item is not None and item.calculable else None
