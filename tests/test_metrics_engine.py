"""Тесты расчёта показателей, включая контрольный пример, посчитанный вручную."""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.definitions import load_metrics
from finlib.metrics.engine import MetricStatus, compute_metric
from finlib.normalize.lines import ReportingType
from finlib.quality.periods import PeriodConfidence
from finlib.quality.thresholds import load_thresholds
from finlib.standards import Standard

PERIOD = date(2025, 12, 31)

# Отчётность ПАО «Газпром» за 2025 год, тысячи рублей, из сохранённой пробы.
CURRENT: dict[str, Decimal | None] = {
    "1100": Decimal("23339986338"),
    "1200": Decimal("2396341798"),
    "1210": Decimal("721319028"),
    "1230": Decimal("1258080152"),
    "1240": Decimal("6543066"),
    "1250": Decimal("432590853"),
    "1300": Decimal("16432222886"),
    "1400": Decimal("6386347532"),
    "1410": Decimal("3600130619"),
    "1500": Decimal("2917757718"),
    "1510": Decimal("825567180"),
    "1600": Decimal("25736328136"),
    "1700": Decimal("25736328136"),
    "2100": Decimal("1721253735"),
    "2110": Decimal("5846351786"),
    "2120": Decimal("4125098051"),
    "2200": Decimal("127437867"),
    "2330": Decimal("522243472"),
    "2400": Decimal("11284564"),
}

PREVIOUS: dict[str, Decimal | None] = {
    "1210": Decimal("704882303"),
    "1230": Decimal("1704049459"),
    "1300": Decimal("17517595295"),
    "1600": Decimal("26161674847"),
}


# Отличает «аргумент не передан» от явно переданного None: иначе тест
# на отсутствие предыдущего периода молча проверял бы не то.
DEFAULT = object()


def compute(
    code: str,
    current=DEFAULT,
    previous=DEFAULT,
    reporting_type=ReportingType.FULL,
    standards=None,
):
    """Считает один показатель на подготовленных данных.

    Стандарт величин передаётся всегда: параметр обязателен намеренно —
    контроль смешения, который можно молча не передать, неотличим
    от невыполненного. По умолчанию все величины одного стандарта.
    """
    metric = load_metrics().require(code)
    values = CURRENT if current is DEFAULT else current
    earlier = PREVIOUS if previous is DEFAULT else previous
    if standards is None:
        codes = set(values or {}) | set(earlier or {})
        standards = dict.fromkeys(codes, Standard.RSBU.value)
    return compute_metric(
        metric,
        reporting_type,
        PERIOD,
        values,
        earlier,
        PeriodConfidence.VERIFIED,
        load_thresholds(),
        standards,
    )


# --- контрольный пример -----------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("cur_liq", "0.82"),
        ("quick_liq", "0.58"),
        ("abs_liq", "0.15"),
        ("nwc", "-521415920.00"),
        ("equity", "16432222886.00"),
        ("equity_ratio", "0.64"),
        ("debt_to_equity", "0.57"),
        ("own_wc_ratio", "-2.88"),
        ("fin_leverage", "1.57"),
        ("debt_total", "4425697799.00"),
        ("net_debt", "3993106946.00"),
        ("debt_to_op_profit", "31.33"),
        ("interest_cover", "0.24"),
        ("gross_margin", "0.29"),
        ("op_margin", "0.02"),
        ("net_margin", "0.00"),
        ("roa", "0.00"),
        ("roe", "0.00"),
        ("asset_turnover", "0.23"),
        ("receivables_days", "92.47"),
        ("inventory_days", "63.10"),
    ],
)
def test_control_example(code: str, expected: str) -> None:
    """Все значения совпадают с ручным расчётом до второго знака."""
    result = compute(code)
    assert result is not None
    assert result.status is MetricStatus.OK, result.reason
    assert isinstance(result.value, Decimal)
    assert result.value.quantize(Decimal("0.01")) == Decimal(expected)


def test_exact_value_is_not_rounded_in_storage() -> None:
    """В результате хранится полная точность, округление — дело вывода."""
    result = compute("cur_liq")
    assert result is not None
    assert str(result.value).startswith("0.82129567")


# --- отсутствие данных ------------------------------------------------------


def test_missing_line_blocks_metric() -> None:
    """Нераскрытая строка — показатель не считается, причина называет код."""
    current = dict(CURRENT)
    current["1500"] = None
    result = compute("cur_liq", current=current)
    assert result is not None
    assert result.status is MetricStatus.NOT_CALCULABLE
    assert result.reason_code == "missing_lines"
    assert "1500" in (result.reason or "")


def test_all_missing_lines_are_listed() -> None:
    """В причине перечисляются все отсутствующие коды, а не только первый."""
    current = dict(CURRENT)
    current["1230"] = None
    current["1240"] = None
    result = compute("quick_liq", current=current)
    assert result is not None
    assert "1230" in (result.reason or "") and "1240" in (result.reason or "")


def test_no_previous_period_blocks_average() -> None:
    """Без предыдущего периода средняя величина не считается и не подменяется."""
    result = compute("roa", previous=None)
    assert result is not None
    assert result.status is MetricStatus.NOT_CALCULABLE
    assert result.reason_code == "no_previous_period"
    assert "подстановка" in (result.reason or "")


def test_previous_period_without_line_blocks_average() -> None:
    """Если строки нет на начало периода, средняя не считается."""
    result = compute("roa", previous={"1300": Decimal(1)})
    assert result is not None
    assert result.status is MetricStatus.NOT_CALCULABLE
    assert result.reason_code == "missing_lines"
    assert "1600" in (result.reason or "")


def test_metric_without_averages_works_without_previous() -> None:
    """Показателю без средних величин предыдущий период не нужен."""
    result = compute("cur_liq", previous=None)
    assert result is not None and result.status is MetricStatus.OK


# --- деление на ноль --------------------------------------------------------


def test_zero_denominator_is_not_infinity() -> None:
    """Нулевой знаменатель — not_calculable, а не бесконечность."""
    current = dict(CURRENT)
    current["2330"] = Decimal(0)
    result = compute("interest_cover", current=current)
    assert result is not None
    assert result.status is MetricStatus.NOT_CALCULABLE
    assert result.reason_code == "zero_denominator"


def test_interest_cover_distinguishes_two_cases() -> None:
    """Нераскрытые проценты и нулевые проценты — разные причины и разные тексты."""
    undisclosed = dict(CURRENT)
    undisclosed["2330"] = None
    first = compute("interest_cover", current=undisclosed)

    zero = dict(CURRENT)
    zero["2330"] = Decimal(0)
    second = compute("interest_cover", current=zero)

    assert first is not None and second is not None
    assert first.reason_code == "missing_lines"
    assert second.reason_code == "zero_denominator"
    assert first.reason != second.reason
    # У нулевого случая — содержательная оговорка из методики.
    assert "заёмные средства" in (second.reason or "")
    assert "заёмные средства" not in (first.reason or "")


def test_negative_denominator_is_not_calculable() -> None:
    """Отрицательный капитал в знаменателе делает коэффициент неинтерпретируемым.

    Случай ПК «Стройсервис»: капитал −442 тыс. руб. Отношение обязательств
    к капиталу выходит отрицательным и для показателя «меньше — лучше»
    читалось бы как отличный результат.
    """
    current = dict(CURRENT)
    current["1300"] = Decimal("-442")
    result = compute("debt_to_equity", current=current)
    assert result is not None
    assert result.status is MetricStatus.NOT_CALCULABLE
    assert result.reason_code == "negative_denominator"
    assert "не интерпретируется" in (result.reason or "")
    assert "1300" in (result.reason or "")


def test_negative_denominator_applies_to_averages() -> None:
    """Проверяется и средняя величина в знаменателе."""
    current = dict(CURRENT)
    current["1300"] = Decimal("-1000")
    previous = dict(PREVIOUS)
    previous["1300"] = Decimal("-500")
    result = compute("roe", current=current, previous=previous)
    assert result is not None
    assert result.reason_code == "negative_denominator"


def test_positive_denominator_is_not_blocked() -> None:
    """Признак не мешает обычному расчёту."""
    result = compute("debt_to_equity")
    assert result is not None and result.status is MetricStatus.OK


def test_denominator_flag_is_declared_in_methodology() -> None:
    """Признак задаётся в методике, а не списком в коде расчёта."""
    catalog = load_metrics()
    assert catalog.require("debt_to_equity").denominator_must_be_positive
    assert catalog.require("roe").denominator_must_be_positive
    # У абсолютных величин без деления признака нет.
    assert not catalog.require("net_debt").denominator_must_be_positive
    assert not catalog.require("equity").denominator_must_be_positive


def test_disclosed_zero_is_not_missing_data() -> None:
    """Раскрытый ноль — это данные: показатель не определён, но данные полны."""
    current = dict(CURRENT)
    current["1210"] = Decimal(0)
    previous = dict(PREVIOUS)
    previous["1210"] = Decimal(0)
    result = compute("inventory_days", current=current, previous=previous)
    assert result is not None
    assert result.status is MetricStatus.OK
    assert result.value == 0


# --- применимость к форме ---------------------------------------------------


def test_metric_absent_in_simplified_returns_nothing() -> None:
    """Неприменимый к упрощённой форме показатель результата не даёт."""
    assert compute("gross_margin", reporting_type=ReportingType.SIMPLIFIED) is None
    assert compute("receivables_days", reporting_type=ReportingType.SIMPLIFIED) is None


def test_simplified_uses_its_own_formula() -> None:
    """Для упрощённой формы применяется переопределённая формула."""
    current = {
        "1210": Decimal(100),
        "1240": Decimal(50),
        "1250": Decimal(20),
        "1510": Decimal(40),
        "1520": Decimal(60),
        "1550": Decimal(10),
    }
    result = compute("cur_liq", current=current, reporting_type=ReportingType.SIMPLIFIED)
    assert result is not None
    assert result.status is MetricStatus.OK
    assert result.value == Decimal(170) / Decimal(110)


# --- методика ---------------------------------------------------------------


def test_no_industry_norms_in_methodology() -> None:
    """В определениях показателей нет полей с нормативами."""
    catalog = load_metrics()
    for metric in catalog.metrics:
        assert not hasattr(metric, "norm")
        assert "norm" not in metric.model_dump()


def test_stop_factors_are_few_and_explicit() -> None:
    """Абсолютные пороги есть только там, где они содержательны вне отрасли."""
    codes = {metric.code for metric in load_metrics().stop_factors()}
    assert codes == {"equity", "equity_ratio", "nwc", "interest_cover"}
    for metric in load_metrics().stop_factors():
        assert metric.stop_factor is not None
        assert metric.stop_factor.note


def test_stop_factor_triggers() -> None:
    """Стоп-фактор срабатывает на отрицательном собственном капитале."""
    metric = load_metrics().require("equity")
    assert metric.stop_factor is not None
    assert metric.stop_factor.triggered(Decimal(-1))
    assert not metric.stop_factor.triggered(Decimal(0))


def test_interest_cover_stop_factor_is_one() -> None:
    """Покрытие процентов меньше единицы — стоп-фактор."""
    metric = load_metrics().require("interest_cover")
    assert metric.stop_factor is not None
    assert metric.stop_factor.triggered(Decimal("0.24"))
    assert not metric.stop_factor.triggered(Decimal("1.5"))


def test_days_constant_comes_from_methodology() -> None:
    """Число дней задано в методике, а не константой в коде."""
    assert load_thresholds().constants["DAYS"] == Decimal(365)
    assert "DAYS" in load_metrics().require("receivables_days").formula


def test_equity_note_explains_difference_from_net_assets() -> None:
    """Оговорка о разнице с чистыми активами по методике Минфина сохранена."""
    note = load_metrics().require("equity").note or ""
    assert "84н" in note
    assert "чистые активы" in note.lower()


def test_debt_total_note_names_the_limitation() -> None:
    """Оговорка о составе долга доедет до заключения."""
    note = load_metrics().require("debt_total").note or ""
    assert "1410" in note and "1510" in note
    assert "аренд" in note
