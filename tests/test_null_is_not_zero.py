"""Нераскрытое значение не превращается в ноль в сигналах и флагах.

Инвариант 4: «не раскрыто» — это `NULL`, а не ноль, и подстановка нуля
разрешена ровно одной функции — `quality/values.py::as_addend`, которой
пользуются контроли сходимости. Нарушение этого правила уже случилось
и прошло незамеченным: признак «обороты около нуля» в регрессионном наборе
считал долю выручки в валюте баланса и при нераскрытой выручке получал ноль,
то есть объявлял организацию без выручки организацией с нулевым оборотом.

Здесь проверяется весь слой признаков разом: для каждого сигнала и каждого
условия флага берутся величины, на которых он срабатывает, и каждая строка
по очереди объявляется нераскрытой. Сигнал обязан замолчать: сказать
по нераскрытой строке нечего.
"""

from decimal import Decimal

import pytest

from finlib.metrics.formula import average_codes, line_codes, parse_formula
from finlib.quality.thresholds import load_thresholds
from finlib.scoring.definitions import load_flags
from finlib.scoring.flags import evaluate_flags
from finlib.scoring.signals import evaluate_signals, load_signals, structure_shifts

SIGNALS = load_signals()
FLAGS = load_flags()

# Отчётность ПК «Стройсервис»: на ней срабатывают изъятие капитала,
# транзитная структура и предельная рентабельность разом.
FIRING_NOW: dict[str, Decimal | None] = {
    "1300": Decimal(-442),
    "2400": Decimal(40839),
    "2110": Decimal(44771),
    "1600": Decimal(418),
}
FIRING_BEFORE: dict[str, Decimal | None] = {
    "1300": Decimal(691),
    "2400": Decimal(4628),
    "2110": Decimal(4920),
    "1600": Decimal(916),
}

# Величины, на которых срабатывает флаг холдинговой структуры.
HOLDING: dict[str, Decimal | None] = {
    "1170": Decimal(5452831319),
    "1600": Decimal(25736328136),
    "2310": Decimal(691333849),
    "2200": Decimal(127437867),
}


def _codes_of(expression: str) -> set[str]:
    """Коды строк, которые участвуют в выражении."""
    return set(line_codes(parse_formula(expression)))


def _signal_codes() -> list[tuple[str, str]]:
    """Пары «сигнал — код строки» для всех выражений справочника."""
    pairs: list[tuple[str, str]] = []
    for signal in SIGNALS.signals:
        for code in sorted(_codes_of(signal.expression)):
            pairs.append((signal.code, code))
        if signal.threshold_of is not None:
            pairs.append((signal.code, signal.threshold_of))
    return sorted(set(pairs))


def test_signals_fire_on_the_reference_values() -> None:
    """Опорные величины действительно дают срабатывания — иначе проверка пуста."""
    fired = {item.code for item in evaluate_signals(FIRING_NOW, FIRING_BEFORE, SIGNALS)}
    assert {"equity_withdrawal", "transit_structure", "extreme_margin"} <= fired


@pytest.mark.parametrize(("signal_code", "line_code"), _signal_codes())
def test_undisclosed_line_silences_the_signal(signal_code: str, line_code: str) -> None:
    """Сигнал, которому не хватает строки, молчит, а не считает её нулём."""
    current = {**FIRING_NOW, line_code: None}
    fired = {item.code for item in evaluate_signals(current, FIRING_BEFORE, SIGNALS)}
    assert signal_code not in fired, (
        f"сигнал {signal_code} сработал при нераскрытой строке {line_code}"
    )


def _previous_codes() -> list[tuple[str, str]]:
    """Пары «сигнал — код», к которым сигнал обращается через prev()."""
    pairs: list[tuple[str, str]] = []
    for signal in SIGNALS.signals:
        tree = parse_formula(signal.expression)
        for code in sorted(average_codes(tree)):
            pairs.append((signal.code, code))
    return sorted(set(pairs))


@pytest.mark.parametrize(("signal_code", "line_code"), _previous_codes())
def test_undisclosed_previous_line_silences_the_signal(
    signal_code: str, line_code: str
) -> None:
    """То же для предыдущего периода: prev(код) без значения — не ноль."""
    before = {**FIRING_BEFORE, line_code: None}
    fired = {item.code for item in evaluate_signals(FIRING_NOW, before, SIGNALS)}
    assert signal_code not in fired, (
        f"сигнал {signal_code} сработал при нераскрытой строке {line_code} "
        "предыдущего периода"
    )


def test_flag_fires_on_the_reference_values() -> None:
    """Опорные величины дают флаг холдинговой структуры."""
    constants = load_thresholds().constants
    fired = {item.code for item in evaluate_flags(HOLDING, constants, FLAGS)}
    assert "holding_structure" in fired


@pytest.mark.parametrize("line_code", sorted(HOLDING))
def test_undisclosed_line_silences_the_flag(line_code: str) -> None:
    """Флаг с нераскрытой строкой не проверяется вовсе."""
    constants = load_thresholds().constants
    values = {**HOLDING, line_code: None}
    fired = {item.code for item in evaluate_flags(values, constants, FLAGS)}
    assert "holding_structure" not in fired, (
        f"флаг сработал при нераскрытой строке {line_code}"
    )


def test_structure_shift_ignores_undisclosed_lines() -> None:
    """Доля нераскрытой статьи не считается нулевой долей.

    Уход доли с нуля и отсутствие сведений о статье — разные вещи; вторая
    не может дать сдвиг структуры.
    """
    from finlib.scoring.engine import _shares

    shares = _shares({"1600": Decimal(1000), "1100": Decimal(400), "1200": None})
    assert "1200" not in shares
    assert shares["1100"] == Decimal(40)
    # Нераскрытая валюта баланса не делает доли нулевыми — их нет вовсе.
    assert _shares({"1600": None, "1100": Decimal(400)}) == {}


def test_structure_shift_needs_both_periods() -> None:
    """Статья, доли которой нет в одном из периодов, сдвига не даёт."""
    now = {"1300": Decimal(-105)}
    found = structure_shifts(now, {}, {"1300": "Капитал и резервы"}, SIGNALS)
    assert found == []
