"""Признаки изменения по промежуточной отчётности.

**Промежуточный комплект отвечает на другой вопрос, чем годовой**: годовой
говорит, каково положение, промежуточный между двумя годовыми — что оно
изменилось. Мерить его годовыми шкалами нельзя, и в один перечень с уровнями
эти признаки не сводятся (`interim.yaml`).

**Отсечка берётся из распределения, а не назначается числом** — принцип
дорожной карты. Поэтому считает её тот же модуль, что и сами величины
(`cutoffs`), а замер только зовёт: второй способ посчитать ту же отсечку
разошёлся бы с первым.

**Ни один признак пока не идёт в маршрут** (`interim.yaml`, `status.in_route`):
до замера прироста и упреждения это было бы правилом по двум наблюдениям.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.db import PgConnection, fetch_all
from finlib.metrics.interim import Rolling, rolling_flow
from finlib.standards import Standard, load_standards

logger = logging.getLogger(__name__)

# Что смотрит признак: имя правила маршрута, а не код строки. Коды объявлены
# один раз в `routing.yaml`, блок `standards`.
LINES = {"cash": "cash_line", "short_debt": "short_debt_line", "operating": "operating_line"}


class Status(BaseModel):
    """Идут ли признаки в маршрут и почему."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    in_route: bool
    in_route_origin: str = Field(min_length=1)


class Confidence(BaseModel):
    """Понижение уверенности и оговорка, входящая в текст основания."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lower: bool
    origin: str = Field(min_length=1)
    note: str = Field(min_length=1)
    kind_words: dict[str, str] = Field(min_length=1)
    # Источники оснований маршрута (`routing.yaml`, `ground_sources`), к которым
    # оговорка о неаудированной базе приписывается.
    applies_to_sources: tuple[str, ...] = Field(min_length=1)

    def said(self, kind: str, moment: date) -> str:
        """Оговорка словами: вид комплекта и его отчётная дата."""
        return self.note.format(
            kind=self.kind_words.get(kind, kind), as_of=f"{moment:%d.%m.%Y}"
        )


class Measure(BaseModel):
    """Чем меряется изменение и что в распределение не идёт."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(pattern="^share_of_previous$")
    origin: str = Field(min_length=1)
    requires_positive_previous: bool


class Frozen(BaseModel):
    """Отсечка, замороженная днём замера: величина, день и откуда она взята."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Decimal
    measured_on: date
    origin: str = Field(min_length=1)


class Decision(BaseModel):
    """Решение владельца по признаку: в маршрут ли, где показывается, на каких числах."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by: str = Field(min_length=1)
    in_route: bool
    card: str = Field(pattern="^(reference|none)$")
    measured: str = Field(min_length=1)
    review: str = Field(min_length=1)


class Feature(BaseModel):
    """Признак изменения: величина, сторона, отсечка, формулировка."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    line: str = Field(min_length=1)
    direction: str = Field(pattern="^(up|down)$")
    rolling: bool
    percentile: int = Field(ge=50, le=100)
    percentile_origin: str = Field(min_length=1)
    calibration_status: str = Field(pattern="^(preliminary|calibrated)$")
    basket: str = Field(min_length=1)
    subgroup: str = Field(min_length=1)
    escalation: bool
    statement: str = Field(min_length=1)
    # Отсечка, замороженная днём замера; пусто — считается перцентилем
    # распределения на каждом вызове.
    frozen: Frozen | None = None
    decision: Decision | None = None

    @property
    def for_card(self) -> bool:
        """Показывается ли признак в карточке справочно по решению владельца."""
        return self.decision is not None and self.decision.card == "reference"

    @model_validator(mode="after")
    def _reference_is_frozen(self) -> "Feature":
        """Справочный в карточке признак стоит на замороженной отсечке.

        Отсечка, пересчитанная при сборке каждой карточки, отвечала бы каждый
        раз о другом, а решение владельца принято на числах одного дня.
        """
        if self.for_card and self.frozen is None:
            raise ValueError(
                f"{self.code}: в карточке справочно, а отсечка не заморожена"
            )
        return self

    @model_validator(mode="after")
    def _line_is_known(self) -> "Feature":
        """Величина названа именем правила маршрута, а не выдумана здесь."""
        if self.line not in LINES:
            listed = ", ".join(sorted(LINES))
            raise ValueError(
                f"{self.code}: величина «{self.line}» не объявлена у маршрута; "
                f"известны: {listed}"
            )
        return self

    @model_validator(mode="after")
    def _statement_says_the_caveat(self) -> "Feature":
        """Оговорка о неаудированности входит в саму формулировку.

        Оговорка, оставшаяся рядом с основанием, до читателя не доходит:
        в списке печатается строка, а не абзац.
        """
        if "{note}" not in self.statement:
            raise ValueError(
                f"{self.code}: формулировка не содержит оговорки {{note}} — "
                "основание по неаудированной отчётности обязано это говорить"
            )
        return self


class NotMeasured(BaseModel):
    """Предмет, признаком не ставший, вместе с причиной."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Band(BaseModel):
    """Отсечки доли IV квартала: ниже `low` и выше `high` — аномалия."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    low: Decimal
    high: Decimal

    @model_validator(mode="after")
    def _ordered(self) -> "Band":
        """Нижняя отсечка ниже верхней, иначе аномалией было бы всё."""
        if self.low >= self.high:
            raise ValueError("нижняя отсечка IV квартала не ниже верхней")
        return self


class ImpliedQuarter(BaseModel):
    """Сверка IV квартала: строки, отсечки и откуда они взяты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    in_route: bool
    measured_on: date
    measured_origin: str = Field(min_length=1)
    # Стандарт → код строки → отсечки.
    lines: dict[str, dict[str, Band]] = Field(min_length=1)

    @model_validator(mode="after")
    def _not_in_route(self) -> "ImpliedQuarter":
        """Сверка — запись журнала, а не основание: так решено 25.09.2026."""
        if self.in_route:
            raise ValueError("сверка IV квартала в маршрут не идёт")
        return self


class Trend(BaseModel):
    """Тренд LTM в карточке: сколько отчётных дат и какие строки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quarters: int = Field(ge=2)
    # Стандарт → код строки → наименование словами.
    lines: dict[str, dict[str, str]] = Field(min_length=1)


class InterimPolicy(BaseModel):
    """Справочник признаков изменения по промежуточной отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    status: Status
    confidence: Confidence
    measure: Measure
    features: tuple[Feature, ...] = Field(min_length=1)
    not_measured: tuple[NotMeasured, ...] = Field(min_length=1)
    trend: Trend
    implied_q4: ImpliedQuarter

    @model_validator(mode="after")
    def _codes_are_unique(self) -> "InterimPolicy":
        """Два признака с одним кодом делили бы одно основание."""
        codes = [item.code for item in self.features]
        if len(set(codes)) != len(codes):
            raise ValueError("коды признаков промежуточной отчётности повторяются")
        return self


def default_path() -> Path:
    """Путь к справочнику признаков изменения."""
    return settings.methodology_dir / "interim.yaml"


@lru_cache(maxsize=8)
def load_interim(path: Path | None = None) -> InterimPolicy:
    """Читает справочник признаков изменения по промежуточной отчётности."""
    source = Path(path) if path is not None else default_path()
    return InterimPolicy.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )


# Величины всех комплектов эмитента разом: строки, по которым считаются
# признаки изменения. Предпочтение источника называется, как у всякой выборки
# по ИНН: первоисточник старше агрегатора.
_VALUES = """
SELECT DISTINCT ON (f.report_date, f.line_code)
       f.report_date, f.line_code, f.value,
       COALESCE(s.reporting_kind, 'full') AS kind, s.unit_code
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s
  AND f.line_code = ANY(%(codes)s)
  AND s.is_actual AND s.status <> 'quarantine'
  AND (%(as_of)s::date IS NULL OR f.report_date <= %(as_of)s::date)
ORDER BY f.report_date, f.line_code, source_rank(s.source)
"""


@dataclass(frozen=True, slots=True)
class Observation:
    """Один комплект эмитента: величины признаков на его отчётную дату.

    **Вид комплекта хранится рядом с величинами.** Основание, построенное
    на промежуточном комплекте, обязано сказать, что отчётность не аудирована,
    и взять это ему больше неоткуда.
    """

    moment: date
    kind: str
    unit_code: str
    values: dict[str, Decimal | None]
    rolling: dict[str, Rolling]
    # Стандарт ряда: от него зависит срок раскрытия (402-ФЗ против 208-ФЗ),
    # и наблюдение, не знающее своего стандарта, видимым стало бы по чужому
    # сроку. Умолчания нет намеренно.
    standard: Standard

    @property
    def interim(self) -> bool:
        """Промежуточный ли комплект."""
        return self.kind == "interim"


def series(
    conn: PgConnection,
    inn: str,
    standard: Standard,
    as_of: date | None = None,
) -> tuple[Observation, ...]:
    """Комплекты эмитента по возрастанию даты с величинами признаков.

    `as_of` отсекает будущее: пересчёт назад спрашивает о том, что было
    известно на дату, и комплект, сданный позже, наблюдением на неё не был.
    Дата раскрытия здесь не моделируется — это делает маршрут, а замер
    называет, каким окном пользуется.
    """
    from finlib.scoring.routing_catalogue import catalogue_for

    rule = catalogue_for(standard).rule
    codes = {name: getattr(rule, field) for name, field in LINES.items()}
    rows = fetch_all(
        _VALUES,
        {
            "inn": inn,
            "standard": standard.value,
            "codes": sorted(set(codes.values())),
            "as_of": as_of,
        },
        conn=conn,
    )
    by_day: dict[date, dict] = {}
    for row in rows:
        seen = by_day.setdefault(
            row["report_date"],
            {"kind": row["kind"], "unit": row["unit_code"], "lines": {}},
        )
        seen["lines"][row["line_code"]] = row["value"]
    # Скользящий год считается по всему ряду сразу: тождество требует трёх
    # величин, и брать их приходится из разных комплектов.
    flows = {
        name: {day: seen["lines"].get(codes[name]) for day, seen in by_day.items()}
        for name in LINES
    }
    found: list[Observation] = []
    for day in sorted(by_day):
        seen = by_day[day]
        found.append(
            Observation(
                moment=day,
                kind=seen["kind"],
                unit_code=seen["unit"] or "",
                values={name: seen["lines"].get(codes[name]) for name in LINES},
                rolling={
                    name: rolling_flow(flows[name], day) for name in LINES
                },
                standard=standard,
            )
        )
    return tuple(found)


def issuer_series(
    conn: PgConnection, inn: str, as_of: date | None = None
) -> tuple[Observation, ...]:
    """Ряд эмитента одного стандарта, выбранного общим правилом предпочтения.

    **Пары комплектов разных стандартов не сравниваются** — ряды несопоставимы,
    и стандарт выбирается тем же правилом, что база оценки (`standards.yaml`,
    `base_standard`): у группы с консолидированной отчётностью — МСФО,
    у прочих — РСБУ. Зовут его и замер, и карточка: второй способ выбрать
    ряд разошёлся бы с первым. Пусто — комплектов нет ни в одном стандарте.
    """
    by_standard = {
        standard: series(conn, inn, standard, as_of) for standard in Standard
    }
    chosen = load_standards().base_standard.choose(
        {standard for standard, found in by_standard.items() if found}
    )
    return by_standard[chosen] if chosen is not None else ()


def value_of(item: Observation, feature: Feature) -> Decimal | None:
    """Величина признака у наблюдения: как есть либо за скользящий год."""
    if feature.rolling:
        return item.rolling[feature.line].value
    return item.values[feature.line]


def change(
    policy: InterimPolicy,
    feature: Feature,
    was: Observation,
    now: Observation,
) -> Decimal | None:
    """Доля изменения величины к прежнему наблюдению; None — мерить нечем.

    **Неположительное прежнее наблюдение доли не даёт**: рост от −20 до +100
    в долях не выражается. Такая пара в распределение не идёт и считается
    отдельно — «признак не сработал» и «мерить было нечем» разные исходы.
    """
    before, after = value_of(was, feature), value_of(now, feature)
    if before is None or after is None:
        return None
    # **Комплекты в разных единицах доли не дают** — то же правило, что
    # в карточке (`reference_readings`, `unit_differs`): у 3900019850
    # 250 210 млн руб. против 245,1 млрд руб. читались сокращением на 99,9 %,
    # а это минус два процента. Такая пара идёт в «мерить нечем».
    if was.unit_code != now.unit_code:
        return None
    if policy.measure.requires_positive_previous and before <= 0:
        return None
    moved = (after - before) / before
    return -moved if feature.direction == "down" else moved


def percentile(values: list[Decimal], share: int) -> Decimal | None:
    """Перцентиль распределения; None — распределения нет вовсе.

    Считается по возрастанию с ближайшим наблюдением: интерполяция здесь
    ничего не добавит, а порог обязан быть величиной, которая наблюдалась.
    """
    if not values:
        return None
    ordered = sorted(values)
    number = min(len(ordered) - 1, int(len(ordered) * share / 100))
    return ordered[number]


@dataclass(frozen=True, slots=True)
class InterimFinding:
    """Сработавший признак изменения: величина, отсечка и день наблюдения."""

    code: str
    name: str
    basket: str
    subgroup: str
    escalation: bool
    value: Decimal
    threshold: Decimal
    since: date
    previous: Decimal
    current: Decimal
    kind: str


def findings(
    policy: InterimPolicy,
    observations: tuple[Observation, ...],
    cutoffs: dict[str, Decimal],
    today: date,
) -> tuple[InterimFinding, ...]:
    """Признаки изменения эмитента на дату по его ряду комплектов.

    **Берётся последняя пара наблюдений, а не любая сработавшая за всю
    историю**: признак говорит о перемене, и «когда-то сокращались» — это
    свойство длины ряда, а не состояния. То же правило, что у рыночного
    слоя, где мера «сработал хотя бы раз» мерила длину ряда.
    """
    seen = [item for item in observations if item.moment <= today]
    if len(seen) < 2:
        return ()
    was, now = seen[-2], seen[-1]
    found: list[InterimFinding] = []
    for feature in policy.features:
        edge = cutoffs.get(feature.code)
        if edge is None:
            continue
        moved = change(policy, feature, was, now)
        if moved is None or moved < edge:
            continue
        before, after = value_of(was, feature), value_of(now, feature)
        assert before is not None and after is not None
        found.append(
            InterimFinding(
                code=feature.code,
                name=feature.name,
                basket=feature.basket,
                subgroup=feature.subgroup,
                escalation=feature.escalation,
                value=moved,
                threshold=edge,
                since=now.moment,
                previous=before,
                current=after,
                kind=now.kind,
            )
        )
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Reading:
    """Признак, показанный справочно: последняя пара, доля, отсечка и исход.

    **Показывается и несработавший, и неизмеренный.** Строка «признак
    не сработал» и отсутствие строки читаются одинаково, а значат разное:
    мерить могло быть нечем. Поэтому причина, по которой доли нет, названа
    кодом (`silence`), и пустым он бывает только у измеренного.
    """

    feature: Feature
    threshold: Decimal
    was: Observation | None
    now: Observation | None
    previous: Decimal | None
    current: Decimal | None
    value: Decimal | None
    # no_series — комплектов нет; single — комплект один, пары нет;
    # previous_missing / current_missing — величина не раскрыта;
    # previous_not_positive — прежняя величина неположительна, доли нет;
    # unit_differs — комплекты в разных единицах, доля не сравнима.
    silence: str | None

    @property
    def fired(self) -> bool:
        """Сработал ли признак на последней паре."""
        return self.value is not None and self.value >= self.threshold


def reference_readings(
    policy: InterimPolicy, observations: tuple[Observation, ...], today: date
) -> tuple[Reading, ...]:
    """Признаки, которые решение владельца велит показать в карточке справочно.

    Правило то же, что у `findings`: последняя пара наблюдений на дату,
    отсечка — замороженная (`frozen`), а не перцентиль дня сборки. В маршрут
    сказанное здесь не идёт.
    """
    seen = [item for item in observations if item.moment <= today]
    now = seen[-1] if seen else None
    was = seen[-2] if len(seen) >= 2 else None
    found: list[Reading] = []
    for feature in policy.features:
        if not feature.for_card:
            continue
        assert feature.frozen is not None
        before = value_of(was, feature) if was is not None else None
        after = value_of(now, feature) if now is not None else None
        moved = (
            change(policy, feature, was, now)
            if was is not None and now is not None
            else None
        )
        if now is None:
            silence: str | None = "no_series"
        elif was is None:
            silence = "single"
        elif before is None:
            silence = "previous_missing"
        elif after is None:
            silence = "current_missing"
        elif was.unit_code != now.unit_code:
            # **Комплекты в разных единицах долю не дают**: у 3900019850
            # 250 210 млн руб. против 245,1 млрд руб. читались сокращением
            # на 99,9 %, а на деле это минус два процента.
            silence = "unit_differs"
            moved = None
        elif moved is None:
            silence = "previous_not_positive"
        else:
            silence = None
        found.append(
            Reading(
                feature=feature,
                threshold=feature.frozen.value,
                was=was,
                now=now,
                previous=before,
                current=after,
                value=moved,
                silence=silence,
            )
        )
    return tuple(found)


def pairs(observations: tuple[Observation, ...]) -> list[tuple[Observation, Observation]]:
    """Соседние наблюдения ряда: пара — это одно измерение изменения."""
    return list(zip(observations, observations[1:], strict=False))


def distribution(
    policy: InterimPolicy, by_issuer: dict[str, tuple[Observation, ...]]
) -> tuple[dict[str, list[Decimal]], dict[str, dict[str, int]]]:
    """Распределение долей изменения по всем парам комплектов и знаменатели.

    **Знаменатель считается у каждого признака свой.** Общее «мерить нечем»
    складывало бы три признака в одно число и делило бы его на число пар —
    величины разного рода, и доля из них не выходит: у денежных средств
    и у операционного результата пробелы разные.
    """
    spread: dict[str, list[Decimal]] = {item.code: [] for item in policy.features}
    counts: dict[str, dict[str, int]] = {
        item.code: {"пар": 0, "измерено": 0, "мерить нечем": 0}
        for item in policy.features
    }
    for observations in by_issuer.values():
        for was, now in pairs(observations):
            for feature in policy.features:
                seen = counts[feature.code]
                seen["пар"] += 1
                moved = change(policy, feature, was, now)
                if moved is None:
                    seen["мерить нечем"] += 1
                    continue
                seen["измерено"] += 1
                spread[feature.code].append(moved)
    return spread, counts


def cutoffs(
    policy: InterimPolicy, spread: dict[str, list[Decimal]]
) -> dict[str, Decimal]:
    """Отсечки признаков перцентилем их же распределения.

    **Порог берётся из распределения, а не по событиям** — принцип дорожной
    карты. Признак, у которого распределения нет вовсе, отсечки не получает
    и не срабатывает: назначить её числом значило бы вернуть магическую
    величину, только в другом файле.
    """
    found: dict[str, Decimal] = {}
    for feature in policy.features:
        # **Замороженная отсечка не едет вместе с распределением** (решение
        # владельца 28.09.2026 по падению денежных средств): порог, пересчитанный
        # на каждом прогоне, отвечал бы каждый раз о другом — то же правило,
        # что у рыночной лестницы и сверки IV квартала.
        if feature.frozen is not None:
            found[feature.code] = feature.frozen.value
            continue
        edge = percentile(spread.get(feature.code, []), feature.percentile)
        if edge is None:
            logger.warning(
                "%s: распределения нет, отсечка не назначается", feature.code
            )
            continue
        found[feature.code] = edge
    return found
