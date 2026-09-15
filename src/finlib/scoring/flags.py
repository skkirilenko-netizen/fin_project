"""Вычисление флагов и подстановка чисел в текст оговорки."""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finlib.metrics.formula import (
    FormulaError,
    ZeroDenominatorError,
    evaluate,
    line_codes,
)
from finlib.scoring.definitions import FlagDef, FlagsCatalog, load_flags

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FlagHit:
    """Сработавший флаг с готовым текстом."""

    code: str
    name: str
    level: str
    affects_class: bool
    lowers_confidence: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _percent(value: Decimal) -> str:
    """Доля в процентах с одним знаком."""
    return f"{value * 100:.1f} %".replace(".", ",")


def _participation(income: Decimal | None, profit: Decimal | None) -> str:
    """Соотношение доходов от участия с прибылью от продаж.

    При неположительной прибыли от продаж отношение бессмысленно, поэтому
    фраза строится иначе. Подстановка детерминированная, модель не участвует.
    """
    if income is None:
        return "не раскрыты"
    if profit is None or profit <= 0:
        return f"составили {income:,.0f} тыс. руб. при отрицательной прибыли от продаж".replace(
            ",", " "
        )
    ratio = income / profit
    return f"превышают прибыль от продаж в {ratio:.1f} раза".replace(".", ",")


def evaluate_flag(
    flag: FlagDef, values: dict[str, Decimal | None], constants: dict[str, Decimal]
) -> FlagHit | None:
    """Проверяет условия флага на значениях периода."""
    computed: list[Decimal] = []
    for condition in flag.conditions:
        missing = [code for code in line_codes(condition.tree) if values.get(code) is None]
        if missing:
            logger.info(
                "флаг %s не проверен: не раскрыты строки %s", flag.code, ", ".join(missing)
            )
            return None
        try:
            computed.append(evaluate(condition.tree, values, None, constants))
        except (ZeroDenominatorError, FormulaError) as exc:
            logger.info("флаг %s не проверен: %s", flag.code, exc)
            return None

    results = [
        condition.holds(value)
        for condition, value in zip(flag.conditions, computed, strict=True)
    ]
    fired = all(results) if flag.combine == "all" else any(results)
    if not fired:
        return None

    return FlagHit(
        code=flag.code,
        name=flag.name,
        level=flag.level.value,
        affects_class=flag.affects_class,
        lowers_confidence=flag.lowers_confidence,
        message=_render(flag, values, computed),
        details={
            "conditions": [
                {"expression": condition.expression, "computed": str(value)}
                for condition, value in zip(flag.conditions, computed, strict=True)
            ],
            "calibration": flag.calibration.strip(),
        },
    )


def _render(flag: FlagDef, values: dict[str, Decimal | None], computed: list[Decimal]) -> str:
    """Подставляет числа расчёта в шаблон оговорки."""
    text = " ".join(flag.text.split())
    if flag.code == "holding_structure":
        text = text.replace("{share_of_assets}", _percent(computed[0]))
        text = text.replace(
            "{participation}", _participation(values.get("2310"), values.get("2200"))
        )
    return text


def evaluate_flags(
    values: dict[str, Decimal | None],
    constants: dict[str, Decimal],
    catalog: FlagsCatalog | None = None,
) -> list[FlagHit]:
    """Проверяет все флаги на значениях одного периода."""
    catalog = catalog if catalog is not None else load_flags()
    hits = [evaluate_flag(flag, values, constants) for flag in catalog.flags]
    return [hit for hit in hits if hit is not None]
