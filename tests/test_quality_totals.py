"""Тесты вынесенной арифметики сходимости итогов.

Контроль проверяет равенство суммы, а не природу кодов: ему безразлично,
четырёхзначный ли это код строки РСБУ или позиция `ifrs.*` унифицированной
модели. Здесь проверяется именно это — один и тот же код работает с обоими
справочниками.
"""

from decimal import Decimal

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.normalize.lines import load_lines
from finlib.quality.totals import TotalVerdict, check_total

TOLERANCE = Decimal(1)


def checker(values: dict[str, Decimal | None], blocked: dict[str, str] | None = None):
    """Доступ к величинам и причинам незагрузки для одного периода."""
    blocked = blocked or {}
    return (
        lambda code: values.get(code),
        lambda code: blocked.get(code),
        lambda total: TOLERANCE,
    )


def test_matching_total_passes_on_rsbu() -> None:
    """Сошедшийся итог РСБУ: 1300 = 1310 − 1320 + 1340 + 1350 + 1360 + 1370."""
    line = load_lines().require("1300")
    values = {item.code: Decimal(0) for item in line.components}
    values["1310"] = Decimal(1000)
    values["1370"] = Decimal(500)
    found = check_total(line, *checker({**values, "1300": Decimal(1500)}))
    assert found.verdict is TotalVerdict.MATCHED
    assert found.computed == Decimal(1500)


def test_matching_total_passes_on_ifrs() -> None:
    """Тот же код на справочнике МСФО: итог активов из двух разделов."""
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.total_assets")
    values = {
        "ifrs.total_non_current_assets": Decimal(700),
        "ifrs.total_current_assets": Decimal(300),
        "ifrs.total_assets": Decimal(1000),
    }
    found = check_total(line, *checker(values))
    assert found.verdict is TotalVerdict.MATCHED
    assert found.computed == Decimal(1000)


def test_expense_component_carries_its_sign_on_ifrs() -> None:
    """Расходная статья приходит со знаком, и оператор её складывает."""
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.gross_profit")
    values = {
        "ifrs.revenue": Decimal(1000),
        "ifrs.cost_of_sales": Decimal(-600),
        "ifrs.gross_profit": Decimal(400),
    }
    found = check_total(line, *checker(values))
    assert found.verdict is TotalVerdict.MATCHED


def test_sign_of_expense_is_inferred_when_printed_without_brackets() -> None:
    """Расход без скобок опознаётся по нормальному знаку статьи.

    Знак выводится только там, где он противоречит нормальному: статья
    с `normal_sign = −1`, пришедшая положительной. Перебирать знаки у всех
    слагаемых нельзя — так сумма подберётся к любому итогу.
    """
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.gross_profit")
    values = {
        "ifrs.revenue": Decimal(1000),
        "ifrs.cost_of_sales": Decimal(600),
        "ifrs.gross_profit": Decimal(400),
    }
    normal_sign = lambda code: (  # noqa: E731
        position.normal_sign if (position := catalog.get(code)) is not None else 1
    )
    assert check_total(line, *checker(values)).verdict is TotalVerdict.MISMATCHED
    assert check_total(line, *checker(values), normal_sign).verdict is TotalVerdict.MATCHED


def test_mismatch_reports_difference_and_tolerance() -> None:
    """Несошедшийся итог называет расхождение и допуск."""
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.total_assets")
    values = {
        "ifrs.total_non_current_assets": Decimal(700),
        "ifrs.total_current_assets": Decimal(300),
        "ifrs.total_assets": Decimal(1200),
    }
    found = check_total(line, *checker(values))
    assert found.verdict is TotalVerdict.MISMATCHED
    assert found.difference == Decimal(-200)
    assert found.tolerance == TOLERANCE
    assert found.details["components"]


def test_undisclosed_component_counts_as_zero() -> None:
    """Нераскрытое слагаемое считается нулём — иначе контроль не работает.

    Это единственная трактовка, при которой сходимость проверяема на форме,
    где раскрыты три статьи из семи. Для расчёта коэффициентов трактовка
    обратная, и путать их нельзя.
    """
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.total_current_assets")
    values = {
        "ifrs.inventories": Decimal(100),
        "ifrs.cash": Decimal(50),
        "ifrs.total_current_assets": Decimal(150),
    }
    found = check_total(line, *checker(values))
    assert found.verdict is TotalVerdict.MATCHED
    assert set(found.undisclosed) == {
        "ifrs.trade_receivables",
        "ifrs.advances_paid",
        "ifrs.other_current_assets",
    }


def test_nothing_disclosed_is_not_a_failure() -> None:
    """Ни одно слагаемое не раскрыто — контроль не выполнялся, а не провален."""
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.total_current_assets")
    found = check_total(line, *checker({"ifrs.total_current_assets": Decimal(150)}))
    assert found.verdict is TotalVerdict.NOTHING_TO_CHECK
    assert "ни одно слагаемое" in found.reason


def test_undisclosed_total_is_not_a_failure() -> None:
    """Итог не раскрыт — сверять не с чем."""
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.total_assets")
    found = check_total(line, *checker({"ifrs.total_current_assets": Decimal(300)}))
    assert found.verdict is TotalVerdict.NOTHING_TO_CHECK
    assert "не раскрыт" in found.reason


def test_blocked_component_makes_the_check_impossible() -> None:
    """Незагруженное слагаемое — наш пробел, а не дефект отчётности."""
    catalog = load_ifrs_lines()
    line = catalog.require("ifrs.total_current_assets")
    values = {"ifrs.total_current_assets": Decimal(150), "ifrs.cash": Decimal(50)}
    found = check_total(
        line, *checker(values, {"ifrs.inventories": "код подходит двум позициям"})
    )
    assert found.verdict is TotalVerdict.NOT_VERIFIABLE
    assert "ifrs.inventories" in found.reason
    assert "код подходит двум позициям" in found.reason


def test_metrics_do_not_import_the_zero_substitution() -> None:
    """Подстановка нуля живёт только в контролях сходимости.

    Проверка была и раньше, но модуль переехал: теперь `as_addend`
    импортирует `quality/totals.py`, и правило обязано сторожить обоих.
    """
    from pathlib import Path

    metrics = Path("src/finlib/metrics")
    for path in metrics.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "as_addend" not in text, path
        assert "quality.totals" not in text, path
