"""Сборка предписанных тезисов: готовые утверждения вместо свободного текста.

Модель не истолковывает показатели. Расчёт выбирает из `methodology/theses.yaml`
готовые утверждения по машинным признакам — знак, положение относительно
бесспорного ориентира, часть калибровочной шкалы, направление изменения,
статус расчёта — и подставляет в них величины. Модель получает перечень
и связывает его в текст, не добавляя утверждений (инвариант 1).

Величины подставляются единой точкой округления (`metrics/display.py`): в тезис
идёт ровно та величина, что в приложение, в блок ПОКАЗАТЕЛИ и в вывод CLI.
Иначе тезис и таблица разошлись бы между собой, а постпроверка сочла бы
процитированное число посторонним.

Надзорные сигналы входят в тот же перечень. Связь между тезисом показателя
и сигналом считается здесь, а не строится моделью: у сигнала есть строки
отчётности, по которым он посчитан, у показателя — строки его формулы,
и пересечение означает, что речь об одном обстоятельстве.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.db import PgConnection, fetch_all
from finlib.metrics.definitions import MetricDef, MetricsCatalog, Unit, load_metrics
from finlib.metrics.display import format_metric, round_to
from finlib.metrics.formula import average_codes, line_codes
from finlib.normalize.lines import ReportingType
from finlib.scoring.definitions import ScoringCatalog, load_scoring
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Слоты подстановки, объявленные в справочнике. Текст, требующий слота,
# которого нет в этом перечне, справочник не примет: молча подставить
# пустую строку значит напечатать утверждение без величины.
_SIGNAL_SLOTS = frozenset({"code", "name", "level", "value", "threshold"})

# Код строки отчётности в тексте причины отказа: «Не раскрыты строки: 1210, 1550».
_LINE_CODE = re.compile(r"\b\d{4}\b")

# Обычный пробельный набор — без неразрывного пробела, которым разделены
# разряды числа. `\s` его захватывает, поэтому класс перечислен явно.
_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")

_PERIODS = """
SELECT DISTINCT report_date FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s ORDER BY report_date DESC LIMIT 3
"""

_VALUES = """
SELECT report_date, metric_code, value, status, reason, reason_code
FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
"""

_REPORTING_TYPE = """
SELECT reporting_type FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(year)s
  AND is_actual
LIMIT 1
"""

_SIGNALS = """
SELECT s.signal_code, s.signal_name, s.level, s.details
FROM assessment_signal s
JOIN assessment a ON a.id = s.assessment_id
WHERE a.inn = %(inn)s AND a.standard = %(standard)s AND a.report_date = %(date)s
ORDER BY CASE s.level WHEN 'supervisory' THEN 0 ELSE 1 END, s.signal_code
"""


class ThesisKind(StrEnum):
    """Семейство, к которому относится тезис.

    Семейство — не украшение: из каждого выдаётся не больше одного тезиса,
    иначе показатель описывался бы дважды разными словами.
    """

    STATUS = "status"
    DYNAMICS = "dynamics"
    SIGN_CHANGE = "sign_change"
    POSITION = "position"
    BAND = "band"
    SIGN = "sign"


class ThesisRule(BaseModel):
    """Одно предписанное утверждение и признаки, при которых оно выдаётся."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    when: dict[str, str] = Field(min_length=1)
    # Единицы измерения, к которым применима формулировка. Нужны динамике:
    # у показателя в днях сокращение периода оборота — ускорение, и глагол
    # обязан этому соответствовать.
    units: tuple[Unit, ...] | None = None
    text: str = Field(min_length=1)
    # Та же мысль сразу о нескольких показателях. Три предложения подряд одной
    # конструкцией читаются как сбой, а не как текст.
    merged: str | None = None
    verb: str | None = None
    unless: dict[str, str] | None = None
    only_if: dict[str, str] | None = None

    @property
    def slots(self) -> set[str]:
        """Слоты, которых требует одиночная формулировка."""
        return set(re.findall(r"\{(\w+)\}", self.text))

    @property
    def all_slots(self) -> set[str]:
        """Слоты обеих формулировок: по ним справочник проверяется целиком.

        В подстановку идут разные наборы: `names` есть только у объединённой
        формулировки, и требовать его от одиночной значило бы отбросить
        каждый тезис, которому не с чем сливаться.
        """
        return self.slots | set(re.findall(r"\{(\w+)\}", self.merged or ""))


class CommonTheses(BaseModel):
    """Тезисы, применимые к любому показателю."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: tuple[ThesisRule, ...] = Field(min_length=1)
    dynamics: tuple[ThesisRule, ...] = Field(min_length=1)
    sign_change: tuple[ThesisRule, ...] = Field(min_length=1)


class MetricTheses(BaseModel):
    """Тезисы о содержании одного показателя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: tuple[ThesisRule, ...] = ()
    band: tuple[ThesisRule, ...] = ()
    sign: tuple[ThesisRule, ...] = ()


class Bands(BaseModel):
    """Границы частей калибровочной шкалы, выраженные баллом уровня."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lower_below: Decimal
    upper_from: Decimal
    origin: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        """Нижняя граница не может стоять выше верхней."""
        if self.lower_below >= self.upper_from:
            raise ValueError("границы частей шкалы заданы в обратном порядке")
        return self

    def of(self, score: Decimal) -> str:
        """Часть шкалы по баллу уровня."""
        if score < self.lower_below:
            return "lower"
        if score >= self.upper_from:
            return "upper"
        return "middle"


class DynamicsPolicy(BaseModel):
    """Порог существенности изменения для словесного описания.

    Свой, а не `material_change` показателя: тот откалиброван для балльной
    оценки и там гасит шум, а для описания слишком груб — падение
    рентабельности собственного капитала вдвое он считает несущественным.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    material_change: Decimal = Field(gt=0)
    origin: str = Field(min_length=1)
    calibration_status: str = Field(min_length=1)

    def is_stable(self, change: Decimal, before: Decimal) -> bool:
        """Считается ли изменение несущественным.

        База — прежний уровень по модулю. Нулевой прежний уровень базы
        не даёт: доли от нуля не существует, и изменение описывается
        движением, а не устойчивостью.
        """
        if not before:
            return False
        return abs(change) / abs(before) < self.material_change


class Narrative(BaseModel):
    """Связки для сборки раздела 3 без модели."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lead: str = Field(min_length=1)
    joiners: tuple[str, ...] = Field(min_length=1)

    def paragraph(self, group: str, texts: list[str]) -> str:
        """Абзац группы: ведущее наименование и тезисы со связками.

        Связка ставится через один тезис: перед каждым получается частокол,
        а без них — перечень, а не текст.
        """
        parts = [self.lead.format(group=group)]
        joiner = 0
        for position, text in enumerate(texts):
            if position and position % 2 == 0:
                word = self.joiners[joiner % len(self.joiners)]
                joiner += 1
                parts.append(f"{word} {text[0].lower()}{text[1:]}")
                continue
            parts.append(text)
        return " ".join(parts)


class SignalRendering(BaseModel):
    """Как тезис сигнала печатается в перечне."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headline: str = Field(min_length=1)
    lines_prefix: str = Field(min_length=1)
    metrics_prefix: str = Field(min_length=1)
    no_metrics: str = Field(min_length=1)
    levels: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_slots(self) -> Self:
        """Заголовок сигнала подставляет только объявленные величины."""
        unknown = set(re.findall(r"\{(\w+)\}", self.headline)) - _SIGNAL_SLOTS
        if unknown:
            raise ValueError(f"в заголовке сигнала неизвестные слоты: {sorted(unknown)}")
        return self


class ThesesCatalog(BaseModel):
    """Справочник предписанных интерпретаций."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    slots: dict[str, str] = Field(min_length=1)
    selectors: dict[str, dict] = Field(min_length=1)
    cross_conditions: dict[str, str] = Field(min_length=1)
    bands: Bands
    dynamics: DynamicsPolicy
    narrative: Narrative
    signals: SignalRendering
    verbs: dict[str, dict[str, str]] = Field(min_length=1)
    genders: dict[str, str] = Field(min_length=1)
    short_names: dict[str, str] = Field(default_factory=dict)
    common: CommonTheses
    metrics: dict[str, MetricTheses] = Field(min_length=1)
    # Тезисы ветки МСФО: справочник отдельный, потому что показатели у стандартов
    # разные, а совпадающие коды означают разное. `cur_liq` есть у обоих, и
    # наименование у него разное — тезис РСБУ в документе МСФО называет
    # показатель не тем именем.
    ifrs_metrics: dict[str, MetricTheses] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_slots(self) -> Self:
        """Каждый слот, которого требует текст, объявлен в блоке slots."""
        declared = set(self.slots)
        for rule in self._all_rules():
            unknown = rule.all_slots - declared
            if unknown:
                raise ValueError(
                    f"тезис {rule.code}: неизвестные слоты {sorted(unknown)}"
                )
        return self

    @model_validator(mode="after")
    def _check_codes(self) -> Self:
        """Коды тезисов уникальны: по ним тезис опознаётся в журнале и в тестах."""
        seen: set[str] = set()
        for rule in self._all_rules():
            if rule.code in seen:
                raise ValueError(f"код тезиса {rule.code} встречается дважды")
            seen.add(rule.code)
        return self

    @model_validator(mode="after")
    def _check_verbs(self) -> Self:
        """Правило, требующее глагола, называет форму из блока verbs."""
        for rule in self._all_rules():
            if "verb" not in rule.slots:
                continue
            if rule.verb is None or rule.verb not in self.verbs:
                raise ValueError(f"тезис {rule.code}: форма глагола не объявлена")
        return self

    def _all_rules(self) -> list[ThesisRule]:
        """Все правила справочника одним перечнем."""
        rules = list(self.common.status + self.common.dynamics + self.common.sign_change)
        for item in (*self.metrics.values(), *self.ifrs_metrics.values()):
            rules.extend(item.position + item.band + item.sign)
        return rules

    def gender_of(self, code: str) -> str | None:
        """Род наименования показателя; None — общие тезисы динамики не выдаются."""
        return self.genders.get(code)

    def name_of(self, metric: MetricDef) -> str:
        """Наименование для тезиса: короткое, если оно задано."""
        return self.short_names.get(metric.code, metric.name)


def default_path() -> Path:
    """Путь к справочнику тезисов."""
    return settings.methodology_dir / "theses.yaml"


@lru_cache(maxsize=1)
def load_theses(path: Path | None = None) -> ThesesCatalog:
    """Читает справочник предписанных интерпретаций."""
    target = path or default_path()
    return ThesesCatalog(**yaml.safe_load(target.read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class Thesis:
    """Готовое утверждение о показателе."""

    code: str
    subject: str
    kind: ThesisKind
    text: str
    # Группа показателей методики. Порядок раздела задаём мы: оставленный
    # модели, он дал семнадцать тезисов подряд одним абзацем.
    group: str = ""


@dataclass(frozen=True, slots=True)
class SignalThesis:
    """Надзорный сигнал в перечне тезисов: величина, отсечка и связь."""

    code: str
    name: str
    level: str
    headline: str
    lines: tuple[str, ...]
    related: tuple[str, ...]


def _group_names(standard: Standard) -> dict[str, str]:
    """Наименования групп показателей своего стандарта, в порядке методики."""
    if standard is Standard.IFRS:
        from finlib.normalize.ifrs_metrics import load_ifrs_metrics

        policy = load_ifrs_metrics()
        names = {code: item.name for code, item in policy.groups.items()}
        names |= {code: item.name for code, item in policy.appendix_groups.items()}
        return names
    catalog = load_metrics()
    return {code: item.name for code, item in catalog.groups.items()}


@dataclass(frozen=True, slots=True)
class TheseSet:
    """Перечень тезисов организации за отчётный период."""

    inn: str
    report_date: date
    theses: tuple[Thesis, ...]
    signals: tuple[SignalThesis, ...]
    # Стандарт, по которому собраны тезисы: наименования групп берутся
    # из справочника своего стандарта. Перечень групп РСБУ, применённый
    # к тезисам МСФО, оставил бы абзацы без наименований — а наименование
    # группы обязано открывать абзац раздела 3.
    standard: Standard = Standard.RSBU

    def block(self) -> str:
        """Блок ТЕЗИСЫ для контекста модели."""
        return render_block(self)

    def narrative(self) -> list[str]:
        """Раздел «Аналитическая интерпретация», собранный без модели.

        Тезисы предписаны, порядок и раскладка по группам заданы расчётом,
        связки берутся из справочника. Модели в таком разделе остаётся
        только выбор слов между предложениями — и стоимость обращения
        сравнивается именно с этим текстом.
        """
        policy = load_theses().narrative
        return [
            policy.paragraph(name, [item.text for item in items])
            for name, items in self.by_group()
        ]

    def by_group(self) -> list[tuple[str, tuple[Thesis, ...]]]:
        """Тезисы по группам показателей в порядке методики.

        Порядок групп — тот, в котором они объявлены в metrics.yaml: он же
        порядок абзацев раздела 3. Показатель без группы в перечень не попадёт,
        потому что группа у показателя обязательна.
        """
        names = _group_names(self.standard)
        grouped: dict[str, list[Thesis]] = {code: [] for code in names}
        for item in self.theses:
            grouped.setdefault(item.group, []).append(item)
        return [
            (names.get(code, code), tuple(items))
            for code, items in grouped.items()
            if items
        ]


@dataclass(frozen=True, slots=True)
class _MetricState:
    """Машинные признаки одного показателя за отчётный период."""

    code: str
    selectors: dict[str, str]
    values: dict[str, str]


def _period_values(
    inn: str, dates: list[date], conn: PgConnection | None, standard: Standard
) -> dict[date, dict[str, dict]]:
    """Значения показателей по периодам: период → код → строка."""
    rows = fetch_all(
        _VALUES, {"inn": inn, "standard": standard.value, "dates": dates}, conn=conn
    )
    found: dict[date, dict[str, dict]] = {item: {} for item in dates}
    for row in rows:
        found.setdefault(row["report_date"], {})[row["metric_code"]] = row
    return found


def _reporting_type(
    inn: str, target: date, conn: PgConnection | None, standard: Standard
) -> ReportingType:
    """Набор форм комплекта, которым закрыт отчётный период."""
    rows = fetch_all(
        _REPORTING_TYPE,
        {"inn": inn, "standard": standard.value, "year": target.year},
        conn=conn,
    )
    if not rows:
        return ReportingType.FULL
    return ReportingType(rows[0]["reporting_type"])


def _selectors(
    metric: MetricDef,
    catalog: MetricsCatalog,
    scoring: ScoringCatalog,
    bands: Bands,
    dynamics: DynamicsPolicy,
    current: dict | None,
    previous: dict | None,
    derived: dict[str, dict],
) -> _MetricState:
    """Машинные признаки показателя и готовые к подстановке величины.

    Признаки считаются по **округлённой** величине: тезис печатает её же,
    и признак, взятый по полной точности, разошёлся бы с напечатанным
    числом на границе — «положителен: 0,00».
    """
    scale = catalog.scale_for(metric.code)
    selectors: dict[str, str] = {}
    values: dict[str, str] = {"code": metric.code}

    if current is None:
        return _MetricState(metric.code, selectors, values)
    # Отчётный год: без него утверждение об отказе расчёта ложно, если
    # за сравнительный период показатель посчитан. Год, а не дата: точка
    # внутри даты рвёт предложение, по которому идёт проверка утверждений.
    values["period"] = str(current["report_date"].year)
    if current["status"] != "ok" or current["value"] is None:
        selectors["status"] = "not_calculable"
        selectors["reason_code"] = current["reason_code"] or ""
        reason = current["reason"] or ""
        values["reason"] = reason
        values["lines"] = ", ".join(_LINE_CODE.findall(reason))
        return _MetricState(metric.code, selectors, values)

    selectors["status"] = "ok"
    value = round_to(current["value"], scale)
    values["value"] = format_metric(current["value"], metric.unit, scale)

    if metric.benchmark is not None:
        selectors["position"] = (
            "above" if value > metric.benchmark
            else "below" if value < metric.benchmark
            else "at"
        )
    selectors["sign"] = "positive" if value > 0 else "negative" if value < 0 else "zero"

    scale_def = scoring.calibration_points.scale_for(metric.code)
    if scale_def is not None:
        selectors["band"] = bands.of(scale_def.score_for(current["value"]))

    if previous is None or previous["status"] != "ok" or previous["value"] is None:
        return _MetricState(metric.code, selectors, values)

    before = round_to(previous["value"], scale)
    values["previous"] = format_metric(previous["value"], metric.unit, scale)
    if (value > 0 and before < 0) or (value < 0 and before > 0):
        selectors["sign_change"] = "to_positive" if value > 0 else "to_negative"
        return _MetricState(metric.code, selectors, values)

    change = derived.get(f"{metric.code}_chg_abs")
    if change is None or change["status"] != "ok" or change["value"] is None:
        return _MetricState(metric.code, selectors, values)
    delta = round_to(change["value"], scale)
    values["change_abs"] = format_metric(change["value"], metric.unit, scale)
    values["change_code"] = f"{metric.code}_chg_abs"
    percent = derived.get(f"{metric.code}_chg_pct")
    if percent is not None and percent["status"] == "ok" and percent["value"] is not None:
        values["change_pct"] = format_metric(percent["value"], Unit.PERCENT)
        values["change_pct_code"] = f"{metric.code}_chg_pct"

    # Существенность считается от прежнего уровня порогом описания, а не
    # порогом балльной оценки: `material_change` показателя откалиброван
    # для балла и для слов слишком груб.
    if dynamics.is_stable(delta, before):
        selectors["dynamics"] = "stable"
    elif delta > 0:
        selectors["dynamics"] = "grew"
    elif delta < 0:
        selectors["dynamics"] = "fell"
    else:
        selectors["dynamics"] = "stable"
    return _MetricState(metric.code, selectors, values)


def _matches(rule: ThesisRule, state: _MetricState) -> bool:
    """Совпали ли признаки показателя с условием правила."""
    for key, expected in rule.when.items():
        if expected == "any":
            if key not in state.selectors:
                return False
            continue
        if state.selectors.get(key) != expected:
            return False
    return True


def _cross_holds(
    rule: ThesisRule, states: dict[str, _MetricState]
) -> bool:
    """Держится ли условие по соседнему показателю.

    Нужно ровно там, где показатель арифметически считается, а истолкованию
    не подлежит: рентабельность собственного капитала при отрицательном
    капитале даёт большое положительное число, и «капитал принёс прибыль»
    рядом с отрицательным капиталом — ложное утверждение.
    """
    for code, expected in (rule.unless or {}).items():
        other = states.get(code)
        if other is not None and expected in other.selectors.values():
            return False
    for code, expected in (rule.only_if or {}).items():
        other = states.get(code)
        if other is None or expected not in other.selectors.values():
            return False
    return True


def _render(
    rule: ThesisRule, state: _MetricState, name: str, catalog: ThesesCatalog
) -> str | None:
    """Подставляет величины в предписанную формулировку; None — слота нет.

    Отсутствие слота означает, что величины нет, а не что её можно опустить:
    напечатать утверждение с дырой хуже, чем не напечатать его вовсе.
    """
    values = dict(state.values)
    values["name"] = name
    if "verb" in rule.slots:
        gender = catalog.gender_of(state.code)
        if gender is None or rule.verb is None:
            logger.warning(
                "показатель %s: род наименования не объявлен, тезис %s пропущен",
                state.code,
                rule.code,
            )
            return None
        form = catalog.verbs[rule.verb].get(gender)
        if form is None:
            return None
        values["verb"] = form
    missing = rule.slots - set(values)
    if missing:
        logger.warning(
            "тезис %s по показателю %s: нет величин %s",
            rule.code,
            state.code,
            sorted(missing),
        )
        return None
    # Свёртка складчатой формулировки справочника в одну строку. Свёртывается
    # только обычный пробельный набор: `" ".join(text.split())` съел бы
    # неразрывный пробел, которым metrics/display.py разделяет разряды числа,
    # и величина в тезисе разошлась бы с величиной в приложении.
    text = _WHITESPACE.sub(" ", rule.text.format(**values)).strip()
    # Единица измерения кончается точкой («тыс. руб.»), и точка формулировки
    # встаёт второй. Сокращать саму единицу нельзя — она часть величины,
    # поэтому лишняя точка снимается здесь.
    return text.replace("..", ".")


def _merge(
    built: list[tuple[ThesisRule, _MetricState, str, ThesisKind, str, str]],
    catalog: ThesesCatalog,
) -> list[Thesis]:
    """Сливает однотипные тезисы одной группы в одно утверждение.

    Три предложения подряд одной конструкцией — «Показатель X за 2024 год
    не рассчитан: не раскрыты строки 1550» — читаются как сбой, а не как текст.
    Объединять их должен расчёт: модель читает требования «привести дословно»
    и «связать в текст» как противоречивые и выбирает первое.

    Сливаются только тезисы одного правила с совпадающей причиной и в пределах
    одной группы: иначе причина одного показателя приписалась бы другому.
    """
    buckets: dict[tuple, list[int]] = {}
    for position, item in enumerate(built):
        rule, state, _, _, group, _ = item
        if rule.merged is None:
            buckets[("сам", position)] = [position]
            continue
        keys = sorted(set(re.findall(r"\{(\w+)\}", rule.merged)) - {"names"})
        signature = (rule.code, group, *(state.values.get(key) for key in keys))
        buckets.setdefault(signature, []).append(position)

    found: list[Thesis] = []
    for positions in buckets.values():
        rule, state, name, kind, group, text = built[positions[0]]
        if len(positions) == 1 or rule.merged is None:
            found.append(Thesis(rule.code, state.code, kind, text, group))
            continue
        listed = _listed(
            [
                f"{built[item][2][0].lower()}{built[item][2][1:]} "
                f"({built[item][1].code})"
                for item in positions
            ]
        )
        values = dict(state.values)
        values["names"] = listed
        values["name"] = name
        merged = _WHITESPACE.sub(" ", rule.merged.format(**values)).strip()
        found.append(
            Thesis(rule.code, state.code, kind, merged.replace("..", "."), group)
        )
        logger.debug(
            "слиты тезисы %s по показателям %s",
            rule.code,
            [built[item][1].code for item in positions],
        )
    return found


def _listed(items: list[str]) -> str:
    """Перечисление через запятую с союзом перед последним."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} и {items[-1]}"


def _pick(
    rules: tuple[ThesisRule, ...],
    state: _MetricState,
    states: dict[str, _MetricState],
    unit: Unit,
) -> ThesisRule | None:
    """Первое подходящее правило семейства; из семейства выдаётся одно."""
    for rule in rules:
        if rule.units is not None and unit not in rule.units:
            continue
        if not _matches(rule, state):
            continue
        if not _cross_holds(rule, states):
            continue
        return rule
    return None


def build_ifrs_theses(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
) -> TheseSet:
    """Тезисы ветки МСФО: часть калибровочной шкалы и знак, без ориентиров.

    **Отдельная сборка, а не ветка внутри РСБУ-шной.** Показатели, шкалы
    и группы у стандартов свои, бесспорных ориентиров у ветки нет вовсе,
    и признак тезиса здесь один — часть калибровочной шкалы, в которую попал
    балл уровня. Долговая нагрузка и обслуживание долга несут 70 % веса балла,
    и раздел без них описывал бы не то, по чему присвоен класс.

    Динамика в тезисы пока не идёт: правило её участия объявлено в методике
    (балл МСФО — уровень, динамика справочно), а сами изменения печатает
    приложение. Тезис о динамике потребует своих формулировок, и выдумывать
    их здесь, в коде, нельзя.
    """
    from finlib.metrics.ifrs_store import compute_from_facts, periods_of
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics
    from finlib.scoring.ifrs import level as ifrs_level

    policy = load_ifrs_metrics()
    theses_catalog = load_theses()
    dates = periods_of(inn, conn)
    if not dates:
        raise ValueError(f"для ИНН {inn} нет комплектов МСФО вне карантина")
    target = report_date or dates[0]

    built: list[Thesis] = []
    for item in compute_from_facts(inn, target, conn, policy):
        rules = theses_catalog.ifrs_metrics.get(item.code)
        if rules is None or not item.calculable:
            continue
        scale = policy.calibration_points.metrics.get(item.code)
        if scale is None:
            continue
        selectors = {
            # Балл уровня считает та же функция, что и оценка: два выражения
            # одной величины неминуемо разошлись бы, и тезис говорил бы
            # о другой части шкалы, чем балл.
            "band": theses_catalog.bands.of(ifrs_level(item.value, scale)),
            "sign": (
                "positive"
                if item.value > 0
                else "negative"
                if item.value < 0
                else "zero"
            ),
        }
        state = _MetricState(
            item.code,
            selectors,
            {
                "code": item.code,
                # Величина печатается той же функцией, что в документе:
                # у покрытия процентов при убытке она словесная, и тезис,
                # набравший число сам, разошёлся бы с приложением.
                "value": _ifrs_shown(item.code, item.value, policy),
                "name": item.name,
            },
        )
        for kind, family in (
            (ThesisKind.BAND, rules.band),
            (ThesisKind.SIGN, rules.sign),
        ):
            rule = _pick(family, state, {}, _ifrs_unit(item.code, policy))
            if rule is None:
                continue
            text = _render(rule, state, item.name, theses_catalog)
            if text is None:
                continue
            built.append(
                Thesis(
                    code=rule.code,
                    subject=item.code,
                    kind=kind,
                    text=text,
                    group=item.group,
                )
            )
    return TheseSet(
        inn=inn,
        report_date=target,
        theses=tuple(built),
        signals=(),
        standard=Standard.IFRS,
    )


def _ifrs_unit(code: str, policy) -> Unit:
    """Единица показателя МСФО в терминах единой точки округления."""
    metric = next((item for item in policy.metrics if item.code == code), None)
    if metric is None:
        return Unit.RATIO
    return Unit.THOUSAND_RUB if metric.unit == "currency" else Unit.RATIO


def _ifrs_shown(code: str, value, policy) -> str:
    """Величина показателя МСФО так, как её печатает документ.

    Одна функция на тезис и на приложение: словесная замена отрицательной
    величины объявлена методикой, и набрать число здесь значило бы завести
    второй способ его напечатать.
    """
    from finlib.metrics.ifrs_view import IfrsMetricsView

    return IfrsMetricsView(policy).shown(code, value)


def build_theses(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
    standard: Standard = Standard.RSBU,
) -> TheseSet:
    """Собирает перечень предписанных тезисов организации за отчётный период."""
    catalog = load_metrics()
    scoring = load_scoring()
    theses_catalog = load_theses()

    dates = [
        row["report_date"]
        for row in fetch_all(
            _PERIODS, {"inn": inn, "standard": standard.value}, conn=conn
        )
    ]
    if not dates:
        raise ValueError(f"для ИНН {inn} нет рассчитанных показателей")
    target = report_date or dates[0]
    previous_date = next((item for item in dates if item < target), None)
    wanted = [target] + ([previous_date] if previous_date else [])
    by_period = _period_values(inn, wanted, conn, standard)
    current = by_period.get(target, {})
    previous = by_period.get(previous_date, {}) if previous_date else {}
    reporting_type = _reporting_type(inn, target, conn, standard)

    states: dict[str, _MetricState] = {}
    for metric in catalog.metrics:
        states[metric.code] = _selectors(
            metric,
            catalog,
            scoring,
            theses_catalog.bands,
            theses_catalog.dynamics,
            current.get(metric.code),
            previous.get(metric.code),
            current,
        )

    built: list[tuple[ThesisRule, _MetricState, str, ThesisKind, str, str]] = []
    for metric in catalog.metrics:
        state = states[metric.code]
        if not state.selectors:
            continue  # показатель не считался за этот период вовсе
        name = theses_catalog.name_of(metric)
        families: list[tuple[ThesisKind, tuple[ThesisRule, ...]]] = [
            (ThesisKind.STATUS, theses_catalog.common.status),
            (ThesisKind.SIGN_CHANGE, theses_catalog.common.sign_change),
            (ThesisKind.DYNAMICS, theses_catalog.common.dynamics),
        ]
        specific = theses_catalog.metrics.get(metric.code)
        if specific is not None:
            families = [
                (ThesisKind.POSITION, specific.position),
                (ThesisKind.BAND, specific.band),
                (ThesisKind.SIGN, specific.sign),
                *families,
            ]
        for kind, rules in families:
            rule = _pick(rules, state, states, metric.unit)
            if rule is None:
                continue
            text = _render(rule, state, name, theses_catalog)
            if text is None:
                continue
            built.append((rule, state, name, kind, metric.group, text))

    found = _merge(built, theses_catalog)

    signals = _signal_theses(
        inn, target, conn, standard, catalog, reporting_type, found, states, theses_catalog
    )
    return TheseSet(inn=inn, report_date=target, theses=tuple(found), signals=signals)


def _metric_lines(
    metric: MetricDef, reporting_type: ReportingType
) -> frozenset[str]:
    """Коды строк отчётности, на которых построен показатель."""
    tree = metric.tree_for(reporting_type)
    if tree is None:
        return frozenset()
    return frozenset(line_codes(tree) | average_codes(tree))


def _signal_lines(details: dict, code: str) -> tuple[str, ...]:
    """Строки отчётности, по которым посчитан сигнал.

    У сигнала, заданного выражением, они берутся из самого выражения;
    у структурного сдвига — из кода статьи и валюты баланса. У интенсивности
    пересмотра строк нет вовсе: она считается по журналу качества, а не
    по отчётности, и связи с показателями у неё поэтому не будет.
    """
    expression = details.get("expression")
    if expression:
        return tuple(sorted(set(_LINE_CODE.findall(expression))))
    line = details.get("line_code")
    if line:
        return tuple(sorted({line, "1600"}))
    _ = code
    return ()


def _signal_theses(
    inn: str,
    target: date,
    conn: PgConnection | None,
    standard: Standard,
    catalog: MetricsCatalog,
    reporting_type: ReportingType,
    theses: list[Thesis],
    states: dict[str, _MetricState],
    theses_catalog: ThesesCatalog,
) -> tuple[SignalThesis, ...]:
    """Тезисы сигналов вместе со связью с тезисами показателей.

    Связь считается здесь: сигнал и показатель говорят об одном обстоятельстве,
    если опираются на общие строки отчётности. Оставить эту связь модели значило
    бы вернуть свободную интерпретацию туда, откуда её убирают.

    В связь идут только рассчитанные показатели. Строка 1300 входит в формулы
    половины справочника, и без этого отбора сигнал об изъятии капитала
    указывал бы в том числе на показатели, которых у организации нет:
    истолковать сигнал через нерассчитанный показатель нельзя.
    """
    rows = fetch_all(
        _SIGNALS,
        {"inn": inn, "standard": standard.value, "date": target},
        conn=conn,
    )
    if not rows:
        return ()
    rendering = theses_catalog.signals
    subjects = {item.subject for item in theses}
    lines_by_metric = {
        metric.code: _metric_lines(metric, reporting_type)
        for metric in catalog.metrics
        if metric.code in subjects
        and metric.code in states
        and states[metric.code].selectors.get("status") == "ok"
    }

    found: list[SignalThesis] = []
    for row in rows:
        details = row["details"] or {}
        value = details.get("value_shown")
        threshold = details.get("threshold_shown")
        if value is None or threshold is None:
            # Оценка, посчитанная прежней версией, основания не получает вовсе:
            # набрать величину заново значит вернуть расхождение разрядности.
            logger.warning(
                "сигнал %s по ИНН %s без набранной величины: требуется пересчёт оценки",
                row["signal_code"],
                inn,
            )
            continue
        lines = _signal_lines(details, row["signal_code"])
        related = tuple(
            code
            for code, codes in sorted(lines_by_metric.items())
            if codes & set(lines)
        )
        found.append(
            SignalThesis(
                code=row["signal_code"],
                name=row["signal_name"] or row["signal_code"],
                level=row["level"],
                headline=rendering.headline.format(
                    code=row["signal_code"],
                    name=row["signal_name"] or row["signal_code"],
                    level=rendering.levels.get(row["level"], row["level"]),
                    value=value,
                    threshold=threshold,
                ),
                lines=lines,
                related=related,
            )
        )
    return tuple(found)


def render_block(found: TheseSet) -> str:
    """Блок ТЕЗИСЫ: перечень утверждений и связанные с ними сигналы."""
    catalog = load_theses()
    rendering = catalog.signals
    lines = [
        "=== ТЕЗИСЫ ===",
        "Готовые утверждения о показателях, разложенные по группам методики.",
        "Содержание и порядок заданы расчётом: приводи тезисы дословно, не меняя",
        "чисел, кодов и знаков, и в том порядке, в каком они стоят здесь.",
        "Каждая группа — один абзац раздела 3. Наименования групп в текст",
        "не переносятся: они показывают границы абзацев, а не служат",
        "заголовками. Твоя работа — переходы между тезисами и между абзацами.",
    ]
    for name, items in found.by_group():
        lines.append("")
        lines.append(f"{name}:")
        lines.extend(f"- {item.text}" for item in items)
    if not found.signals:
        return "\n".join(lines)

    lines.append("")
    lines.append(
        "Надзорные сигналы. Предписанную формулировку каждого печатает расчёт"
    )
    lines.append(
        "в разделе 4 — переписывать её не нужно. Здесь величина, отсечка"
    )
    lines.append(
        "и связь с тезисами выше: сигнал и названные показатели говорят"
    )
    lines.append("об одном обстоятельстве, и связь установлена расчётом.")
    lines.append("")
    for signal in found.signals:
        lines.append(signal.headline)
        related = (
            f"{rendering.metrics_prefix} {', '.join(signal.related)}"
            if signal.related
            else rendering.no_metrics
        )
        lines.append(
            f"    {rendering.lines_prefix} {', '.join(signal.lines) or '—'}; {related}"
        )
    return "\n".join(lines)
