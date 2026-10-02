"""Оферты в основании рефинансирования: шаг 1 и шаг 2, каждый против базы. Только диск и чтение БД.

    uv run python eval/offers_run.py > отчёт.md

Сейчас (`sources.cbonds_flows.refinancing`) оферта любого вида в окне
года добавляет весь объём выпуска в обращении во вторую меру
(`refinancing_offers`), а платежи года — в первую (`refinancing_gap`);
отсечка у обеих одна — покрытие денежными средствами.

- **Шаг 1 — без call.** Call — право эмитента погасить, а не право
  владельца предъявить; во второй мере остаются put и «доп. оферта»
  (выкуп по предложению эмитента — тоже право владельца).
- **Шаг 2 — объединение.** Оферты в окне складываются с платежами года
  в одну меру (позиция владельца 01.10.2026: оферта в окне — возможное
  погашение всего объёма в обращении на дату оферты), вторая мера пуста.
  Виды оферт — как сейчас: шаги меряются порознь.

**Замер не считает сам**: подменяется только чтение оферт (шаг 1) либо
итог `refinancing` (шаг 2); корзины — `routing_rows`, календарь — поточечная
мера корзины. Купоны после даты оферты в шаге 2 не вычитаются — это
завышение сверху, названное здесь, а не исправленное.
"""

import contextlib
import json
import logging
import sys
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_variants  # noqa: E402
from market_lead_run import events  # noqa: E402

from finlib.scoring import routing_store  # noqa: E402
from finlib.sources import cbonds_flows  # noqa: E402
from finlib.sources.market import series  # noqa: E402

logger = logging.getLogger(__name__)

HOLDER_RIGHT = ("put", "доп. оферта")


def _offers_without_call(
    emission_id: str, kinds: tuple[str, ...] | None = None  # noqa: ARG001
) -> tuple[date, ...] | None:
    """Даты оферт выпуска, кроме call; None — ответа источника нет.

    С 02.10.2026 шаг 1 в main (`refinancing.offer_kinds`), и замер повторяет
    базу; подмена оставлена, чтобы прогон ночи воспроизводился.
    """
    path = cbonds_flows.CACHE / f"offert_{emission_id}.json"
    if not path.exists():
        return None
    found: list[date] = []
    for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
        if str(item.get("type_rus") or "") not in HOLDER_RIGHT:
            continue
        try:
            found.append(date.fromisoformat(str(item.get("date") or "")[:10]))
        except ValueError:
            continue
    return tuple(sorted(found))


@contextlib.contextmanager
def step_one():  # noqa: ANN201
    """Шаг 1: call в меру оферт не идёт."""
    original = cbonds_flows.offers_of
    cbonds_flows.offers_of = _offers_without_call
    try:
        yield
    finally:
        cbonds_flows.offers_of = original


@contextlib.contextmanager
def step_two():  # noqa: ANN201
    """Шаг 2: оферты года складываются с платежами года в одну меру."""
    original = routing_store.refinancing

    def merged(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        found = original(*args, **kwargs)
        return replace(found, scheduled=found.scheduled + found.offered, offered=found.offered * 0)

    routing_store.refinancing = merged
    try:
        yield
    finally:
        routing_store.refinancing = original


def main() -> int:
    """Печатает смены корзины и календарь для базы и двух шагов."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    market = series()
    when = events()
    moments = route_variants.monthly_moments(market)
    last_day = max(market.benchmark)
    kinds: Counter[str] = Counter()
    for path in cbonds_flows.CACHE.glob("offert_*.json"):
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            kinds[str(item.get("type_rus") or "—")] += 1
    print("# Оферты в основании рефинансирования: шаг 1 и шаг 2 против базы\n")
    print(f"Записей оферт на диске по видам: {dict(kinds.most_common())}.\n")
    memo: dict = {}
    variants = (
        ("база", contextlib.nullcontext),
        ("шаг 1: без call", step_one),
        ("шаг 2: оферты вместе с платежами года", step_two),
    )
    said: dict[str, dict] = {}
    for name, around in variants:
        with around():
            said[name] = route_variants.baskets(moments, memo if name == "база" else {})
    print("## Календарь событий\n")
    for name, _ in variants:
        print(route_variants.calendar(name, said[name], when, last_day) + "\n")
    for name, _ in variants[1:]:
        found = route_variants.changes(said["база"], said[name])
        print(f"## Смены корзины: {name}\n")
        print(route_variants.summary(found, moments) + "\n")
        today = max(moments)
        for moment, inn, was, now in found:
            if moment == today:
                grounds = ", ".join(said[name][moment][inn][1])
                print(f"- {moment:%d.%m.%Y} {inn}: {was} → {now} ({grounds})")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
