"""Отчёт изменений: причина «у нас» по отпечатку прогона и решающее основание.

30.09.2026 недельный отчёт назвал 93 смены корзины «у эмитента», хотя все
они пришли от наших правок: объявленная версия методики «1.0.0» правкой
не поднималась, а смена отпечатка входов сама объявляла причину у эмитента.
И графа «Почему» у Республики Саха (Якутия) называла рейтинг АКРА, а корзину
сменил перевод вне периметра по типу эмитента.
"""

import sys
from datetime import date

from finlib.config import settings
from finlib.scoring.routing import load_routing

sys.path.insert(0, str(settings.base_dir / "eval"))

import change_report_run as report  # noqa: E402

DAY = date(2026, 9, 30)


def _row(basket: str, grounds: tuple[str, ...], fingerprint: str, code: str) -> dict:
    """Точка истории с отпечатком входов и отпечатком прогона."""
    return {
        "basket": basket,
        "grounds": list(grounds),
        "fingerprint": fingerprint,
        "report_date": date(2025, 12, 31),
        "code_version": code,
        "methodology": {"routing": "1.0.0", "content": "m1", "route_code": code},
    }


def test_a_code_change_is_ours_even_when_inputs_moved() -> None:
    """Разные версии — «у нас», хоть отпечаток входов и сменился; те же — «у эмитента»."""
    routing = load_routing()
    was = {
        "1": _row("attention", ("refinancing_gap",), "a", "c1"),
        "2": _row("attention", ("refinancing_gap",), "a", "c1"),
    }
    now = {
        "1": _row("review", ("market_price_distress",), "b", "c2"),
        "2": _row("review", ("market_price_distress",), "b", "c1"),
    }
    found = report._classify(routing, was, now, DAY, DAY)
    assert found["ours"] == ["1"] and found["issuer"] == ["2"]


def test_the_decisive_ground_is_the_one_of_the_new_basket() -> None:
    """Перевод вне периметра называется типом эмитента, а не прочими основаниями."""
    routing = load_routing()
    before = _row("attention", ("rating_outlook_adverse",), "a", "c1")
    after = _row("out_of_scope", ("out_of_scope_issuer", "rating_outlook_adverse"), "b", "c1")
    assert report._decisive(routing, before, after) == {"out_of_scope_issuer"}


def test_improvement_names_the_departed_review_ground(monkeypatch, capsys) -> None:
    """Переход Парк Сказки вниз объясняется ушедшим p99, а не новым p95."""
    routing = load_routing()
    before = _row("review", ("market_spread_extreme",), "a", "c1")
    after = _row("attention", ("market_spread_wide",), "b", "c2")
    assert report._decisive(routing, before, after) == {"market_spread_extreme"}
    # Изолированный тест печати: сведения источника здесь не проверяются.
    monkeypatch.setattr(report, "_why", lambda *args: "данные изменились, слой не назван")
    monkeypatch.setattr(report, "_named", lambda inn: f"Парк Сказка ({inn})")
    report._moves_table(
        routing, {"ours": ["7743185160"]}, {"7743185160": before},
        {"7743185160": after}, DAY, DAY,
        {basket.code: basket.order for basket in routing.baskets},
    )
    text = capsys.readouterr().out
    assert "ушло основание:" in text and "p99" in text
    assert "p95" not in text
    assert "c1 → c2" in text and "точная правка не установлена" in text


def test_worsening_prefers_the_arriving_senior_ground() -> None:
    """Ухудшение объясняется новым основанием старшей корзины."""
    before = _row("attention", ("market_spread_wide",), "a", "c1")
    after = _row("review", ("market_spread_extreme",), "b", "c1")
    assert report._decisive(load_routing(), before, after) == {"market_spread_extreme"}
