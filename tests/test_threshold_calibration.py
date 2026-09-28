"""Калибровка фазы 6: мера, выбор на обучении и парная проверка.

Меры здесь — устройство замера, а не маршрут: основание при другом пороге
даёт боевой маршрут (`tests/test_routing_overrides.py`). Проверяется, что
сработавшее после события не ловит его, что при равенстве остаётся нынешний
порог и что одинаковые пороги дают нулевую разность.
"""

import sys
from datetime import date
from decimal import Decimal

from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "eval"))

import threshold_calibration_run as run  # noqa: E402

PART = run.Part("проверка", date(2026, 7, 1), date(2026, 9, 25))


def _subject() -> run.Subject:
    """Ступень рефинансирования: основание и предмет."""
    return run.Subject(
        "проба", "cover", "refinancing_gap", "refinance.due",
        frozenset({"refinancing_gap"}), "refinancing", True, Decimal(1),
    )


def _fired(days: dict[date, bool]) -> dict[date, frozenset]:
    """История одного эмитента: сработало ли основание в каждый день."""
    return {
        day: frozenset({("refinancing_gap", "refinancing")}) if on else frozenset()
        for day, on in days.items()
    }


def _flags(history: dict, members: set[str], calendar: dict) -> dict:
    """Отметки эмитентов по истории «прежнего» варианта — тем же путём, что в замере."""
    done = run.Pass()
    for inn, by_day in history.items():
        done.base[inn].update(by_day)
    observed = {inn: set(by_day) for inn, by_day in done.base.items()}
    return run.flags(
        done.days_of("прежний", _subject()), observed, members, calendar, PART
    )


def test_firing_after_the_event_does_not_catch_it() -> None:
    """Основание, появившееся в день события и позже, событие не ловит."""
    history = {
        "A": _fired({date(2026, 6, 1): False, date(2026, 7, 10): True}),
        "B": _fired({date(2026, 6, 1): False, date(2026, 7, 20): True}),
    }
    calendar = {"A": date(2026, 7, 15), "B": date(2026, 7, 15)}
    marks = _flags(history, {"A", "B"}, calendar)
    assert marks["A"].fired and marks["A"].lead == 5
    assert not marks["B"].fired


def test_standing_from_the_first_day_is_not_an_appearance() -> None:
    """Основание, стоявшее с первого дня истории, ничего не предсказало."""
    history = {"A": _fired({date(2025, 9, 24): True, date(2026, 7, 10): True})}
    marks = _flags(history, {"A"}, {"A": date(2026, 8, 1)})
    assert not marks["A"].fired


def test_an_event_before_the_part_leaves_the_circle() -> None:
    """Эмитент, чьё событие было до части, в её круг не входит."""
    history = {"A": _fired({date(2026, 6, 1): False})}
    marks = _flags(history, {"A"}, {"A": date(2026, 3, 1)})
    assert marks == {}


def test_equal_thresholds_give_zero_difference() -> None:
    """Одинаковые пороги на одних выборках — разность ровно ноль."""
    same = {
        str(n): run.Flags(positive=n < 3, fired=n % 2 == 0, lead=None)
        for n in range(40)
    }
    low, high, _ = run.paired(same, same)
    assert low == high == 0


def test_a_tie_on_training_keeps_the_current_threshold() -> None:
    """Равный прирост на обучении — остаётся нынешний порог."""
    subject = _subject()
    spread = run.Distribution(
        edges={Decimal("90"): Decimal(5)}, current_share=Decimal(40)
    )
    same = run.Score(population=100, positives=5, fired=20, caught=2, leads=(10, 20))
    train = {"прежний": same, run._name(subject, Decimal("90")): same}
    assert run.choose(subject, spread, train) == "прежний"


def test_too_few_fired_are_not_chosen() -> None:
    """Вариант с малым числом сработавших не выбирается, как бы высок ни был прирост."""
    subject = _subject()
    spread = run.Distribution(edges={Decimal("99"): Decimal(50)}, current_share=Decimal(40))
    train = {
        "прежний": run.Score(100, 5, 20, 2, (10,)),
        run._name(subject, Decimal("99")): run.Score(100, 5, run.MIN_FIRED - 1, 3, (10,)),
    }
    assert run.choose(subject, spread, train) == "прежний"
