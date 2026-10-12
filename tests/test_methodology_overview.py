"""Обзор методики для аналитика называет каждое основание маршрута и пороги справочника."""

from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "methodology_overview.md"


def _yaml(name: str) -> dict:
    """Справочник методики целиком."""
    return yaml.safe_load((ROOT / "methodology" / name).read_text(encoding="utf-8"))


def _ground_codes() -> set[str]:
    """Все коды оснований routing.yaml: корзины, справочные, типы, источники."""
    routing = _yaml("routing.yaml")
    codes = {
        ground["code"]
        for basket in routing["baskets"]
        for ground in basket.get("grounds") or []
    }
    codes |= {item["code"] for item in routing.get("reference") or []}
    codes |= {kind["ground"] for kind in routing.get("issuer_types") or []}
    codes |= set(routing.get("ground_sources") or {})
    return codes


def _ru(value: object) -> str:
    """Число так, как оно напечатано в документе: запятая вместо точки."""
    return format(Decimal(str(value)).normalize(), "f").replace(".", ",")


def test_every_ground_code_is_named() -> None:
    """Каждый код основания из routing.yaml назван в обзоре кодом."""
    text = DOC.read_text(encoding="utf-8")
    missing = sorted(code for code in _ground_codes() if f"`{code}`" not in text)
    assert not missing, f"основания не названы в обзоре: {missing}"


def test_ground_codes_are_found() -> None:
    """Перечень не пуст: тест выше не проходит молча на пустом справочнике."""
    assert len(_ground_codes()) >= 40


def test_market_thresholds_match_yaml() -> None:
    """Рыночные пороги в обзоре совпадают со справочником."""
    text = DOC.read_text(encoding="utf-8")
    market = _yaml("market.yaml")
    steps = {step["code"]: step for step in market["ladder"]["steps"]}
    expected = {
        "ratio_below": f"ниже {_ru(market['distress_zone']['ratio_below'])}",
        "price_below_percent": f"{market['distress_zone']['price_below_percent']} %",
        "lifetime": f"{market['lifetime']['trading_days']} торговых дней",
        "floor_bp": f"{market['benchmark']['floor_bp']} б. п.",
        "level_p95": f"{_ru(steps['level_p95']['multiple'])}×",
        "level_p99": f"{_ru(steps['level_p99']['multiple'])}×",
    }
    missing = {key: shown for key, shown in expected.items() if shown not in text}
    assert not missing, f"порог в обзоре не совпадает со справочником: {missing}"


def test_routing_thresholds_match_yaml() -> None:
    """Пороги маршрута в обзоре совпадают со справочником."""
    text = DOC.read_text(encoding="utf-8")
    routing = _yaml("routing.yaml")
    expected = [
        f"default_stale_years: {routing['events']['default_stale_years']}",
        f"rating_withdrawn_stale_years: {routing['events']['rating_withdrawn_stale_years']}",
        f"top_share: {routing['systemic']['top_share']}",
        f"band: {routing['tolerance']['band']}",
        f"cover_ratio: {routing['refinancing']['cover_ratio']}",
        f"annual_due: \"{routing['freshness']['annual_due']}\"",
        f"status_unknown_after_cycles: {routing['freshness']['status_unknown_after_cycles']}",
        f"offers_in_payments: {str(routing['refinancing']['offers_in_payments']).lower()}",
    ]
    missing = [shown for shown in expected if shown not in text]
    assert not missing, f"порог в обзоре не совпадает со справочником: {missing}"
