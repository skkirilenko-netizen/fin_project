"""Тесты разведения двух трактовок нераскрытого значения."""

from decimal import Decimal
from pathlib import Path

import pytest

from finlib.quality.values import as_addend, is_disclosed

SRC = Path(__file__).resolve().parents[1] / "src" / "finlib"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, Decimal(0)),
        (Decimal(0), Decimal(0)),
        (Decimal("-5.5"), Decimal("-5.5")),
        (Decimal("100"), Decimal("100")),
    ],
)
def test_as_addend(value: Decimal | None, expected: Decimal) -> None:
    """Для проверки арифметики нераскрытое слагаемое считается нулём."""
    result = as_addend(value)
    assert isinstance(result, Decimal)
    assert result == expected


def test_as_addend_does_not_hide_disclosure() -> None:
    """Подстановка нуля не меняет факта нераскрытия: это разные вопросы."""
    assert as_addend(None) == as_addend(Decimal(0))
    assert not is_disclosed(None)
    assert is_disclosed(Decimal(0)), "раскрытый ноль — это раскрытое значение"


def test_zero_substitution_lives_only_in_quality() -> None:
    """Подстановку нуля нельзя занести в расчёт коэффициентов незаметно.

    Инвариант 4: при расчёте показателей нераскрытое значение остаётся NULL,
    показатель получает not_calculable. Разводим трактовки точкой входа,
    а не дисциплиной, поэтому as_addend не должен появляться нигде,
    кроме пакета quality.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.parent.name == "quality":
            continue
        if "as_addend" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"подстановка нуля просочилась в {offenders}"


def test_metrics_package_is_not_exempt() -> None:
    """Если появится пакет метрик, правило распространяется и на него."""
    metrics = SRC / "metrics"
    if not metrics.exists():
        pytest.skip("пакет метрик ещё не создан — задача 6")
    for path in metrics.rglob("*.py"):
        assert "as_addend" not in path.read_text(encoding="utf-8")
