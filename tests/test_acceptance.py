"""Условия приёмки экспертной проверки читаются из methodology/acceptance.yaml."""

from datetime import date
from decimal import Decimal

from finlib.acceptance import load_acceptance


def test_the_acceptance_is_read_from_the_methodology() -> None:
    """Доля «лишних» — Decimal, счёт с 15.10.2026, две проверки подряд, критичных ноль."""
    found = load_acceptance()
    assert found.extra_share_max == Decimal("0.10")
    assert isinstance(found.extra_share_max, Decimal)
    assert found.critical_max == 0
    assert found.consecutive == 2
    assert found.counted_from == date(2026, 10, 15)
