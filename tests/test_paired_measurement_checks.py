"""Синтетические граничные случаи парного замера, без запуска замера и БД."""

import sys
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "eval"))
import paired_67  # noqa: E402
import zspread_run  # noqa: E402


def test_snapshot_name_and_read_only_must_both_match() -> None:
    """Только синтетические метаданные: к базе этот тест не подключается."""
    paired_67.check_snapshot({"db": "findb_test", "ro": "on"}, "findb_test")
    for metadata in ({"db": "other", "ro": "on"}, {"db": "findb_test", "ro": "off"}):
        with pytest.raises(ValueError, match="READ ONLY"):
            paired_67.check_snapshot(metadata, "findb_test")


def test_zero_lift_is_valid_in_every_paired_replica() -> None:
    """Сработавшие и события есть, ноль попаданий не отбрасывается."""
    result = paired_67.paired({"a": (2, 0, 1, 4)}, {"a": (2, 1, 1, 4)})
    assert result.old == Decimal(0) and result.new == Decimal(2)
    assert result.low == result.high == Decimal(2)
    assert result.valid == paired_67.REPLICAS and result.excluded == 0


@pytest.mark.parametrize(
    "rows", [{}, {"a": (0, 0, 1, 4)}, {"a": (2, 0, 0, 4)}, {"a": (0, 0, 0, 0)}]
)
def test_undefined_results_never_index_empty_replicas(rows: dict) -> None:
    """Пустой круг и нулевые знаменатели дают явную неопределённость."""
    result = paired_67.paired(rows, rows)
    assert result.old is result.new is result.low is result.high is None
    assert result.valid == 0 and result.excluded == paired_67.REPLICAS
    assert "не определено" in paired_67.result_row("синтетика", "основание", result)


def test_mixed_replicas_account_for_all_and_use_same_indices() -> None:
    """Пустые и пригодные реплики названы, парная разность совпавших мер — ноль."""
    rows = {"a": (2, 1, 1, 4), "b": (0, 0, 0, 0)}
    result = paired_67.paired(rows, rows)
    assert 0 < result.valid < paired_67.REPLICAS
    assert result.valid + result.excluded == paired_67.REPLICAS
    assert result.low == result.high == Decimal(0)
    assert paired_67.paired(rows, rows) == result


@pytest.mark.parametrize(
    "new, message",
    [
        ({"b": (2, 1, 1, 4)}, "охват ИНН"),
        ({"a": (2, 1, 2, 4)}, "события/наблюдения"),
        ({"a": (2, 1, 1, 5)}, "события/наблюдения"),
        ({"a": (2, 3, 1, 4)}, "несогласованные"),
        ({"a": (-1, 0, 1, 4)}, "некорректные"),
    ],
)
def test_unpaired_or_invalid_denominators_stop(new: dict, message: str) -> None:
    """Разный охват не скрывается нулевым дополнением, плохие счётчики не принимаются."""
    with pytest.raises(ValueError, match=message):
        paired_67.paired({"a": (2, 1, 1, 4)}, new)


def test_horizon_boundary_is_actual_selected_day() -> None:
    """Ровно полный горизонт допустим, следующий торговый день — ещё не проверен."""
    cut = date(2090, 1, 2)
    until = cut + timedelta(days=paired_67.HORIZON)
    paired_67.validate_cuts([cut], until)
    with pytest.raises(ValueError, match="неполный горизонт"):
        paired_67.validate_cuts([cut + timedelta(days=1)], until)
    with pytest.raises(ValueError, match="повторяются"):
        paired_67.validate_cuts([cut, cut], until)


@pytest.mark.parametrize("change", ["benchmark", "point", "extra", "missing"])
def test_g_reproduction_checks_both_directions(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Несовпадение ориентира, значения, лишней или пропавшей точки останавливает замер."""
    day = date(2090, 1, 2)
    base = SimpleNamespace(benchmark={day: Decimal(100)}, issuers={"a": {day: "point"}}, census={})
    again = SimpleNamespace(benchmark=dict(base.benchmark), issuers={"a": {day: "point"}})
    monkeypatch.setattr(zspread_run, "build", lambda *args: again)
    zspread_run.verify_g({day.isoformat(): []}, base)
    if change == "benchmark":
        again.benchmark[day] = Decimal(101)
    elif change == "point":
        again.issuers["a"][day] = "other"
    elif change == "extra":
        again.issuers["b"] = {day: "extra"}
    else:
        again.issuers = {}
    with pytest.raises(ValueError, match="G из pickle не воспроизводит"):
        zspread_run.verify_g({day.isoformat(): []}, base)
