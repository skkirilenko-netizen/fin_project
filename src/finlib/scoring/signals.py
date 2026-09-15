"""Надзорные сигналы: детерминированные признаки, требующие внимания.

Экспертная оценка показала, что по ПК «Стройсервис» система не отразила
единственный существенный сигнал: при чистой прибыли 40 839 тыс. руб.
собственный капитал сократился с 691 до −442 тыс. руб. Выявление такого
не может оставаться на усмотрение модели.

Поэтому сигнал — арифметика, а не интерпретация: условие проверяется
по формуле, формулировка берётся из `methodology/signals.yaml` дословно
и подставляется величинами. Модель их не изобретает и не переписывает.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finlib.config import settings
from finlib.metrics.definitions import Condition
from finlib.utils import safe_div

logger = logging.getLogger(__name__)

class SignalLevel(StrEnum):
    """Вес сигнала для читателя."""

    ATTENTION = "attention"
    SUPERVISORY = "supervisory"


class CalibrationStatus(StrEnum):
    """Зрелость порога: на скольких наблюдениях он установлен."""

    # Порог экспертный, проверен на нескольких организациях.
    PRELIMINARY = "preliminary"
    # Порог установлен на регрессионном наборе (задача 17).
    CALIBRATED = "calibrated"


@dataclass(frozen=True, slots=True)
class SignalHit:
    """Сработавший сигнал с готовой формулировкой."""

    code: str
    name: str
    level: SignalLevel
    value: Decimal
    message: str
    details: dict[str, str]


def _triggered(condition: Condition, value: Decimal, threshold: Decimal) -> bool:
    """Сработало ли условие сигнала."""
    if condition is Condition.LT:
        return value < threshold
    if condition is Condition.LTE:
        return value <= threshold
    if condition is Condition.GT:
        return value > threshold
    return value >= threshold


class SignalRule(BaseModel):
    """Общее у всех сигналов: условие, происхождение порога и формулировка.

    Условие обязательно у каждого, включая сигналы, величина которых считается
    не формулой. Иначе отсечка лежала бы в методике, а знак сравнения — в коде,
    и по справочнику нельзя было бы сказать, когда печатается формулировка.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    level: SignalLevel
    condition: Condition
    origin: str = Field(min_length=1)
    # Происхождение и зрелость порога — разные сведения: origin отвечает,
    # откуда взялась величина, статус — можно ли на неё опираться.
    calibration_status: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("calibration_status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        """Статус начинается машинным признаком, дальше — пояснение словами."""
        token = value.split(",", 1)[0].strip()
        if token not in set(CalibrationStatus):
            allowed = ", ".join(item.value for item in CalibrationStatus)
            raise ValueError(
                f"статус калибровки «{token}» неизвестен; допустимы: {allowed}"
            )
        return value

    @property
    def calibration(self) -> CalibrationStatus:
        """Машинный признак зрелости порога."""
        return CalibrationStatus(self.calibration_status.split(",", 1)[0].strip())

    @property
    def preliminary(self) -> bool:
        """Порог предварительный: опираться на него как на норму нельзя."""
        return self.calibration is CalibrationStatus.PRELIMINARY


class SignalDef(SignalRule):
    """Сигнал, задаваемый выражением по строкам отчётности."""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    expression: str = Field(min_length=1)
    threshold: Decimal
    # Порог задан долей этой строки, а не абсолютом: у крупной организации
    # расхождение в миллион — округление, у малой — вся деятельность.
    threshold_of: str | None = Field(default=None, pattern=r"^\d{4}$")
    # Разрядность подстановки величины в формулировку: денежные величины
    # целыми тысячами, кратности с одним знаком, доли с двумя.
    display_scale: int = Field(default=1, ge=0)
    # Подставлять величину по модулю: знак уже выражен словами формулировки
    # («не объясняется», «расхождение»), и минус читался бы как опечатка.
    as_absolute: bool = False


class StructureShift(SignalRule):
    """Структурный сдвиг баланса: изменение доли укрупнённой статьи."""

    threshold_points: Decimal = Field(gt=0)


class RevisionIntensity(SignalRule):
    """Интенсивность пересмотра сравнительных данных."""

    threshold_per_set: Decimal = Field(gt=0)


class SignalsCatalog(BaseModel):
    """Справочник надзорных сигналов."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    signals: tuple[SignalDef, ...] = Field(min_length=1)
    structure_shift: StructureShift
    revision_intensity: RevisionIntensity

    @model_validator(mode="after")
    def _check_codes(self) -> Self:
        """Коды сигналов уникальны."""
        seen: set[str] = set()
        for item in self.signals:
            if item.code in seen:
                raise ValueError(f"код сигнала {item.code} встречается дважды")
            seen.add(item.code)
        return self


def default_path() -> Path:
    """Путь к справочнику сигналов."""
    return settings.methodology_dir / "signals.yaml"


@lru_cache(maxsize=1)
def load_signals(path: Path | None = None) -> SignalsCatalog:
    """Читает справочник сигналов."""
    target = path or default_path()
    return SignalsCatalog(**yaml.safe_load(target.read_text(encoding="utf-8")))


def _evaluate(
    expression: str,
    current: dict[str, Decimal | None],
    previous: dict[str, Decimal | None],
) -> Decimal | None:
    """Считает выражение сигнала; None — не хватает данных.

    Язык тот же, что у показателей: код строки, prev(код) для предыдущего
    периода, четыре действия и скобки. Разбор идёт тем же интерпретатором,
    без eval.
    """
    from finlib.metrics.formula import FormulaError, evaluate, parse_formula

    try:
        return evaluate(parse_formula(expression), current, previous, {})
    except (FormulaError, KeyError, ZeroDivisionError):
        return None


def evaluate_signals(
    current: dict[str, Decimal | None],
    previous: dict[str, Decimal | None],
    catalog: SignalsCatalog | None = None,
) -> list[SignalHit]:
    """Проверяет выражения сигналов по значениям двух периодов."""
    catalog = catalog if catalog is not None else load_signals()
    found: list[SignalHit] = []
    for signal in catalog.signals:
        value = _evaluate(signal.expression, current, previous)
        if value is None:
            continue
        threshold = signal.threshold
        if signal.threshold_of is not None:
            base = current.get(signal.threshold_of)
            if base is None or base == 0:
                continue
            threshold = signal.threshold * abs(base)
        if not _triggered(signal.condition, value, threshold):
            continue
        found.append(
            SignalHit(
                code=signal.code,
                name=signal.name,
                level=signal.level,
                value=value,
                message=_format(signal, value, current),
                details={
                    "expression": signal.expression,
                    "value": str(value),
                    "threshold": str(threshold),
                },
            )
        )
    return found


def structure_shifts(
    shares_now: dict[str, Decimal],
    shares_before: dict[str, Decimal],
    names: dict[str, str],
    catalog: SignalsCatalog | None = None,
) -> list[SignalHit]:
    """Статьи, доля которых в балансе изменилась сверх порога."""
    catalog = catalog if catalog is not None else load_signals()
    rule = catalog.structure_shift
    found: list[SignalHit] = []
    for code, after in shares_now.items():
        before = shares_before.get(code)
        if before is None:
            continue
        shift = after - before
        # Сравнивается модуль: сигнал даёт и уход доли, и её приход.
        if not _triggered(rule.condition, abs(shift), rule.threshold_points):
            continue
        found.append(
            SignalHit(
                code=f"structure_shift_{code}",
                name=rule.name,
                level=rule.level,
                value=shift,
                message=rule.text.format(
                    line=names.get(code, code),
                    value=_money(abs(shift), 1),
                    before=f"{_money(before, 1)} %",
                    after=f"{_money(after, 1)} %",
                ),
                details={
                    "line_code": code,
                    "shift_points": str(shift),
                    # Отсечка идёт вместе с величиной: в документе тезис
                    # приводится с тем порогом, по которому он сработал.
                    "threshold": str(rule.threshold_points),
                },
            )
        )
    return sorted(found, key=lambda item: abs(item.value), reverse=True)


def revision_intensity(
    mismatches: int, sets: int, catalog: SignalsCatalog | None = None
) -> SignalHit | None:
    """Пересмотр сравнительных данных интенсивнее порога."""
    catalog = catalog if catalog is not None else load_signals()
    rule = catalog.revision_intensity
    if sets <= 0:
        return None
    per_set = safe_div(Decimal(mismatches), Decimal(sets))
    if per_set is None or not _triggered(
        rule.condition, per_set, rule.threshold_per_set
    ):
        return None
    return SignalHit(
        code="revision_intensity",
        name=rule.name,
        level=rule.level,
        value=per_set,
        message=rule.text.format(value=mismatches, sets=sets),
        details={
            "mismatches": str(mismatches),
            "sets": str(sets),
            "threshold": str(rule.threshold_per_set),
        },
    )


def _format(
    signal: "SignalDef", value: Decimal, values: dict[str, Decimal | None]
) -> str:
    """Подставляет величины в предписанную формулировку."""
    profit = values.get("2400")
    shown = abs(value) if signal.as_absolute else value
    return signal.text.format(
        value=_money(shown, signal.display_scale),
        profit=_money(profit, 0) if profit is not None else "—",
    )


def _money(value: Decimal, scale: int) -> str:
    """Величина в русском написании с разделителями разрядов."""
    from finlib.metrics.display import round_to

    return f"{round_to(value, scale):,}".replace(",", " ").replace(".", ",")
