"""Платежи по выпускам: величина приводится к выпуску, оферта считается порознь.

**Три случая, и каждый стоил бы ошибки в разы.** Платёж источник даёт
на одну облигацию — «купон 17,26» рядом с балансом в миллионах не значит
ничего, пока не умножен на число бумаг. Оферта не платёж графика:
предъявление бумаги — право владельца, и сложенная с купоном она выдала бы
возможное за состоявшееся. И «графика нет» не то же самое, что «платежей
нет»: первое означает, что доставка до выпуска не дошла.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.sources import cbonds_flows
from finlib.sources.cbonds_flows import refinancing, schedule_of


@dataclass(frozen=True)
class FakeIssue:
    """Выпуск: столько, сколько нужно расчёту платежей."""

    emission_id: str
    status: str = "в обращении"
    outstanding: Decimal | None = Decimal(300000000)
    offer: date | None = None


def put(folder: Path, emission: str, rows: list[dict]) -> None:
    """Кладёт ответ источника о графике на диск."""
    (folder / f"flow_{emission}.json").write_text(
        json.dumps({"items": rows}, ensure_ascii=False), encoding="utf-8"
    )


def coupon(when: str, amount: str, redemption: str = "0") -> dict:
    """Запись графика: срок, купон и погашение на одну облигацию."""
    return {
        "date": when,
        "cupon_sum": amount,
        "redemtion": redemption,
        "emission_nominal_price": "1000",
        # Поле сдвинутого срока заполнено и у будущих платежей: фактом
        # уплаты оно не является, и расчёт на него не смотрит.
        "actual_payment_date": when,
    }


@pytest.fixture()
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Каталог ответов источника на время теста."""
    monkeypatch.setattr(cbonds_flows, "CACHE", tmp_path)
    return tmp_path


def test_payment_is_scaled_to_the_number_of_bonds(cache: Path) -> None:
    """Величина платежа приводится к выпуску, а не остаётся на облигацию.

    Купон 17,26 при номинале 1 000 и объёме 300 000 000 — это 300 000 бумаг
    и 5 178 000 рублей. Без приведения платёж стоял бы рядом с балансом
    в миллионах, отличаясь от него на шесть порядков.
    """
    put(
        cache,
        "1",
        [coupon("2026-10-05", "17.26"), coupon("2026-11-05", "17.26")],
    )
    plan = schedule_of("1")
    assert plan is not None
    due = plan.due_within(12, date(2026, 9, 22), Decimal(300000000))
    assert due == Decimal("17.26") * 2 * 300000


def test_payments_outside_the_window_are_not_counted(cache: Path) -> None:
    """Платёж за краем окна в сумму не входит, а прошедший — тем более."""
    put(
        cache,
        "1",
        [
            coupon("2026-08-05", "10"),
            coupon("2026-10-05", "10"),
            coupon("2027-11-05", "10"),
        ],
    )
    plan = schedule_of("1")
    assert plan is not None
    assert plan.due_within(12, date(2026, 9, 22), Decimal(1000)) == Decimal(10)


def test_redemption_and_coupon_of_one_day_are_added(cache: Path) -> None:
    """В день погашения платятся оба: и купон, и номинал."""
    put(cache, "1", [coupon("2026-12-05", "13.56", "1000")])
    plan = schedule_of("1")
    assert plan is not None
    assert plan.due_within(12, date(2026, 9, 22), Decimal(1000)) == Decimal("1013.56")


def test_missing_schedule_is_not_zero(cache: Path) -> None:
    """«Графика нет» и «платежей нет» — разные сведения, и счётчик их делит."""
    assert schedule_of("нет такого") is None
    plan = refinancing((FakeIssue(emission_id="нет такого"),), 12, date(2026, 9, 22))
    assert plan.scheduled == 0
    assert plan.without_schedule == 1
    assert not plan.known


def test_unknown_volume_gives_no_sum(cache: Path) -> None:
    """Объём в обращении неизвестен — умножать не на что, и это не ноль."""
    put(cache, "1", [coupon("2026-10-05", "10")])
    plan = schedule_of("1")
    assert plan is not None
    assert plan.due_within(12, date(2026, 9, 22), None) is None
    counted = refinancing(
        (FakeIssue(emission_id="1", outstanding=None),), 12, date(2026, 9, 22)
    )
    assert counted.without_volume == 1
    assert not counted.known


def offers(folder: Path, emission: str, dates: list[str]) -> None:
    """Кладёт ответ источника об офертах на диск."""
    (folder / f"offert_{emission}.json").write_text(
        json.dumps({"items": [{"date": item} for item in dates]}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_offer_is_counted_apart_from_the_schedule(cache: Path) -> None:
    """Оферта не складывается с купонами: предъявление — право владельца."""
    put(cache, "1", [coupon("2026-10-05", "10")])
    offers(cache, "1", ["2026-12-01"])
    plan = refinancing(
        (FakeIssue(emission_id="1", outstanding=Decimal(1000)),),
        12,
        date(2026, 9, 22),
    )
    assert plan.scheduled == Decimal(10)
    assert plan.offered == Decimal(1000)


def test_offer_is_taken_from_the_method_not_from_the_issue(cache: Path) -> None:
    """Ближайшая оферта берётся у метода оферт, а не у записи выпуска.

    У «Русбонд-Удобрения, 001Р-СПВБ-01» запись объявляет 29.03.2027, а метод
    даёт 28.09.2026 — внутри годового окна. Поле записи ближайшую оферту
    называет не всегда, и доверять ему значило бы терять ту величину, ради
    которой считается всё остальное.
    """
    put(cache, "1", [coupon("2026-10-05", "10")])
    offers(cache, "1", ["2026-09-28", "2027-03-29"])
    plan = refinancing(
        (
            FakeIssue(
                emission_id="1",
                outstanding=Decimal(1000),
                # Запись выпуска называет далёкую дату — она не в счёт.
                offer=date(2027, 3, 29),
            ),
        ),
        12,
        date(2026, 9, 22),
    )
    assert plan.offered == Decimal(1000)


def test_missing_offers_answer_is_not_absence_of_offers(cache: Path) -> None:
    """Ответа об офертах нет — это называется, а не считается нулём."""
    put(cache, "1", [coupon("2026-10-05", "10")])
    plan = refinancing(
        (FakeIssue(emission_id="1", outstanding=Decimal(1000)),),
        12,
        date(2026, 9, 22),
    )
    assert plan.offered == 0
    assert plan.without_offers == 1


def test_redeemed_issues_are_out_of_the_window(cache: Path) -> None:
    """Погашенный выпуск платежей не несёт и в знаменатель не идёт."""
    put(cache, "1", [coupon("2026-10-05", "10")])
    plan = refinancing(
        (FakeIssue(emission_id="1", status="погашена"),), 12, date(2026, 9, 22)
    )
    assert plan.issues == 0
    assert plan.scheduled == 0
