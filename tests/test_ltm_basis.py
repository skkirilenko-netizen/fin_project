"""База маршрута — LTM на последнюю отчётную дату (фаза 5-бис).

Решение владельца 25.09.2026: потоки — последний годовой плюс текущее с начала
года минус прошлогоднее с начала года, отрезки одной длины; баланс — на дату;
стандарт один; промежуточный — база, только если новее годового; неаудированная
база оговаривается в формулировке основания; IV квартал сверяется.
"""

from datetime import date
from decimal import Decimal

from finlib.metrics.ifrs import MetricValue
from finlib.metrics.interim import ltm_values, rolling_flow
from finlib.quality.implied_q4 import outside, quarters
from finlib.scoring.interim import Band, load_interim
from finlib.scoring.routing import route
from finlib.scoring.routing_catalogue import catalogue_for
from finlib.scoring.routing_store import interim_base
from finlib.standards import Standard

YEAR = date(2025, 12, 31)
HALF = date(2026, 6, 30)
HALF_BEFORE = date(2025, 6, 30)


def test_ltm_is_the_identity_of_three_segments() -> None:
    """LTM = годовой + текущее с начала года − прошлогоднее с начала года."""
    got = rolling_flow(
        {YEAR: Decimal(1000), HALF: Decimal(600), HALF_BEFORE: Decimal(450)}, HALF
    )
    assert got.value == Decimal(1150)


def test_the_annual_must_be_last_year_not_the_nearest_known() -> None:
    """Годовой — ровно прошлого года: позапрошлый тождество не даёт.

    Прежде бралась ближайшая известная годовая дата, и при пропущенном годе
    выходила величина настоящего вида, которой не соответствует ни один период.
    """
    got = rolling_flow(
        {
            date(2024, 12, 31): Decimal(900),
            HALF: Decimal(600),
            HALF_BEFORE: Decimal(450),
        },
        HALF,
    )
    assert not got.known and "годового периода" in got.reason


def test_a_segment_of_another_length_refuses() -> None:
    """Прошлогоднего полугодия нет — есть девять месяцев: отказ, не приближение."""
    got = rolling_flow(
        {YEAR: Decimal(1000), HALF: Decimal(600), date(2025, 9, 30): Decimal(700)},
        HALF,
    )
    assert not got.known


def test_flows_roll_and_the_balance_stays_on_its_date() -> None:
    """Поток приводится к двенадцати месяцам, балансовая величина — как есть."""
    values, rolled = ltm_values(
        {
            YEAR: {"2110": Decimal(1000), "1600": Decimal(5000)},
            HALF: {"2110": Decimal(600), "1600": Decimal(5400)},
            HALF_BEFORE: {"2110": Decimal(450), "1600": Decimal(4800)},
        },
        HALF,
        {"2110"},
    )
    assert values["2110"] == Decimal(1150) and rolled["2110"].known
    assert values["1600"] == Decimal(5400)


def test_the_interim_becomes_the_base_only_when_newer() -> None:
    """Промежуточный — база, если новее годового; годовой того же года — назад."""
    latest = {("1", "ifrs"): (HALF, False)}
    assert interim_base("1", Standard.IFRS, YEAR, latest) == (HALF, False)
    # Годовой 2026 года, раскрытый позже, новее полугодия и возвращает базу.
    assert interim_base("1", Standard.IFRS, date(2026, 12, 31), latest) is None


def test_the_base_does_not_mix_standards() -> None:
    """Промежуточный РСБУ базой эмитента с годовой МСФО не становится."""
    latest = {("1", "rsbu"): (HALF, False)}
    assert interim_base("1", Standard.IFRS, YEAR, latest) is None


def test_an_unaudited_base_is_said_in_the_ground_itself() -> None:
    """Оговорка о неаудированной базе стоит в формулировке основания о величинах."""
    note = load_interim().confidence.said("interim", HALF)
    verdict = route(
        (
            MetricValue(
                code="debt_to_op_profit",
                name="debt_to_op_profit",
                group="debt",
                in_scoring=True,
                value=Decimal("9.5"),
                denominator=Decimal("100"),
            ),
            MetricValue(
                code="equity_ratio", name="equity_ratio", group="debt",
                in_scoring=True, value=Decimal("0.6"),
            ),
            MetricValue(
                code="cur_liq", name="cur_liq", group="debt",
                in_scoring=True, value=Decimal("2.5"),
            ),
        ),
        unit="тыс. руб.",
        quarantined=False,
        operating_profit=Decimal(100),
        latest_annual=YEAR,
        today=date(2026, 9, 25),
        catalogue=catalogue_for(Standard.RSBU),
        basis_note=note,
    )
    bound = next(item for item in verdict.findings if item.ground == "bound_above_threshold")
    assert note in bound.text and "неаудированной" in note


def test_a_negative_fourth_quarter_of_revenue_is_an_anomaly() -> None:
    """Годовая выручка меньше девяти месяцев — IV квартал отрицателен: аномалия."""
    rows = [
        {"report_date": YEAR, "line_code": "2110", "value": 900, "src_file_id": 1},
        {"report_date": date(2025, 9, 30), "line_code": "2110", "value": 950,
         "src_file_id": 2},
    ]
    found = quarters(rows, {"2110"})
    band = load_interim().implied_q4.lines["rsbu"]["2110"]
    assert len(found) == 1 and outside(found[0], band)


def test_an_ordinary_fourth_quarter_is_not() -> None:
    """Обычная доля IV квартала аномалией не является; неположительный год — не мерится."""
    band = Band(low=Decimal("0"), high=Decimal("0.97"))
    ordinary = quarters(
        [
            {"report_date": YEAR, "line_code": "2110", "value": 1000, "src_file_id": 1},
            {"report_date": date(2025, 9, 30), "line_code": "2110", "value": 720,
             "src_file_id": 2},
        ],
        {"2110"},
    )
    assert not outside(ordinary[0], band)
    loss = quarters(
        [
            {"report_date": YEAR, "line_code": "2200", "value": -10, "src_file_id": 1},
            {"report_date": date(2025, 9, 30), "line_code": "2200", "value": 50,
             "src_file_id": 2},
        ],
        {"2200"},
    )
    assert loss[0].share is None and not outside(loss[0], band)
