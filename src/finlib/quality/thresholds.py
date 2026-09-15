"""Загрузка допусков и уровней контролей из methodology/thresholds.yaml."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from finlib.config import settings
from finlib.normalize.lines import ReportingType
from finlib.quality.codes import Severity


class Rounding(BaseModel):
    """Допуск на округление при проверке сходимости."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    absolute: Decimal = Field(ge=0)
    relative: Decimal = Field(ge=0)

    def tolerance(self, total: Decimal) -> Decimal:
        """Допустимое расхождение для конкретного итога."""
        return max(self.absolute, abs(total) * self.relative)


class JumpDetection(BaseModel):
    """Порог выявления скачка показателя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: Decimal = Field(gt=1)
    min_base: Decimal = Field(ge=0)


class BalanceTotalBounds(BaseModel):
    """Границы правдоподобия валюты баланса при заявленной единице."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: Decimal = Field(gt=0)
    max: Decimal = Field(gt=0)
    origin: str = Field(min_length=1)

    def implausible(self, total: Decimal) -> str | None:
        """Чем величина неправдоподобна при заявленной единице; None — в границах.

        Ноль исключён: умножение на тысячу его не меняет, поэтому подменой
        единицы он быть не может. Нулевая валюта баланса — признак
        недействующей организации, и говорят о ней другие контроли.
        """
        value = abs(total)
        if value == 0:
            return None
        if value < self.min:
            return "ниже нижней границы правдоподобия"
        if value > self.max:
            return "выше верхней границы правдоподобия"
        return None


class PeriodMagnitudeShift(BaseModel):
    """Кратность между смежными периодами, указывающая на подмену единицы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: Decimal = Field(gt=1)
    tolerance: Decimal = Field(gt=0, lt=1)
    min_base: Decimal = Field(ge=0)
    origin: str = Field(min_length=1)

    def shifted(self, current: Decimal, previous: Decimal) -> Decimal | None:
        """Кратность, близкая к подмене единицы; None — обычное изменение.

        Проверяется близость отношения к тысяче, а не просто большая
        величина: рост в триста раз бывает хозяйственным событием, ровно
        тысячекратный — нет.
        """
        if abs(previous) < self.min_base or previous == 0 or current == 0:
            return None
        ratio = abs(current) / abs(previous)
        for candidate in (self.factor, Decimal(1) / self.factor):
            if abs(ratio - candidate) <= candidate * self.tolerance:
                return ratio
        return None


class Magnitude(BaseModel):
    """Контроли правдоподобия абсолютных величин."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    balance_total: BalanceTotalBounds
    period_shift: PeriodMagnitudeShift


class RetainedEarningsLink(BaseModel):
    """Порог необъяснённого расхождения прироста прибыли с чистой прибылью."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unexplained_share_of_assets: Decimal = Field(ge=0)
    min_absolute: Decimal = Field(ge=0)

    def tolerance(self, assets: Decimal | None) -> Decimal:
        """Порог: одновременно и доля валюты баланса, и абсолютный минимум."""
        by_share = abs(assets) * self.unexplained_share_of_assets if assets is not None else 0
        return max(self.min_absolute, by_share)


class CheckPolicy(BaseModel):
    """Политика одного контроля."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Severity


class Thresholds(BaseModel):
    """Допуски и уровни контролей качества."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    unit_code: str = Field(min_length=1)
    # Именованные константы формул: числовых литералов в формулах нет.
    constants: dict[str, Decimal] = Field(default_factory=dict)
    rounding: Rounding
    jump_detection: JumpDetection
    magnitude: Magnitude
    retained_earnings_link: RetainedEarningsLink
    checks: dict[str, CheckPolicy]
    mandatory_lines: dict[ReportingType, tuple[str, ...]]

    def severity_of(self, check_code: str) -> Severity:
        """Уровень контроля; отсутствие политики — ошибка методики, не умолчание."""
        policy = self.checks.get(check_code)
        if policy is None:
            raise KeyError(f"для контроля {check_code} не задан уровень в thresholds.yaml")
        return policy.severity

    def mandatory_for(self, reporting_type: ReportingType) -> tuple[str, ...]:
        """Обязательные к раскрытию строки для набора отчётности."""
        return self.mandatory_lines[reporting_type]


def default_path() -> Path:
    """Путь к файлу допусков по умолчанию."""
    return settings.methodology_dir / "thresholds.yaml"


@lru_cache(maxsize=8)
def load_thresholds(path: Path | None = None) -> Thresholds:
    """Читает и проверяет допуски; результат кэшируется по пути."""
    source = Path(path) if path is not None else default_path()
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    return Thresholds.model_validate(raw)
