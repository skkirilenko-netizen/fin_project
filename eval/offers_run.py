"""Оферты в основании рефинансирования: шаг 1 и шаг 2, каждый против базы. Только диск и чтение БД.

    uv run python eval/offers_run.py > отчёт.md

Сейчас (`sources.cbonds_flows.refinancing`) оферта любого вида в окне
года добавляет весь объём выпуска в обращении во вторую меру
(`refinancing_offers`), а платежи года — в первую (`refinancing_gap`);
отсечка у обеих одна — покрытие денежными средствами.

- **Шаг 1 — без call.** Call — право эмитента погасить, а не право
  владельца предъявить; во второй мере остаются put и «доп. оферта»
  (выкуп по предложению эмитента — тоже право владельца).
- **Шаг 2 — объединение.** Оферты в окне входят в платежи года одной мерой
  (решения владельца 01.10.2026 и 07.10.2026; `refinancing.offers_in_payments`):
  платежи графика до дня выкупа включительно, объём на день выкупа после
  погашений — по цене оферты, когда она названа, — платежи после выкупа
  сняты, вторая мера не выставляется. Виды оферт — как сейчас: шаги
  меряются порознь.

**Замер не считает сам**: подменяется только чтение оферт (шаг 1) либо
флаг справочника (шаг 2); корзины — `routing_rows`, календарь — поточечная
мера корзины.
"""

import contextlib
import json
import logging
import sys
from collections import Counter
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
    """Шаг 2: боевой расчёт с `refinancing.offers_in_payments: true`.

    Подменяется только справочник, который читает сборка входов маршрута:
    расчёт платежей, формулировки и отказ от второй меры — те же, что
    включит переключение флага в `routing.yaml`.
    """
    original = routing_store.load_routing

    def switched():  # noqa: ANN202
        policy = original()
        return policy.model_copy(
            update={
                "refinancing": policy.refinancing.model_copy(
                    update={"offers_in_payments": True}
                )
            }
        )

    routing_store.load_routing = switched
    try:
        yield
    finally:
        routing_store.load_routing = original


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
