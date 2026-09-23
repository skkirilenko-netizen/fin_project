"""Маршрут один, справочника показателей два: что именно у них разное.

**Второй маршрут писать нельзя, и тест закрепляет именно это.** Правила
корзин, градации тяжести и формулировки остаются общими; своим у стандарта
остаётся то, чем зовутся величины решения и чем делается вывод о долговой
нагрузке. В МСФО она величина (`net_debt_ebitda`), в РСБУ её нет вовсе —
амортизация в формах 0710001–0710005 не раскрывается, — и остаётся граница:
чистый долг к прибыли от продаж.

Проверяется здесь и **то, чего маршрут делать не должен**: доказывать
прохождение критерия границей, посчитанной от убытка. Отношение чистого долга
к отрицательной прибыли отрицательно, и шкала прочла бы его как низкую
нагрузку — тот же обман, что у отношения к неположительной EBITDA, только
у показателя РСБУ положительность знаменателя справочником не объявлена.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.ifrs import MetricValue
from finlib.scoring.routing import load_routing, route
from finlib.scoring.routing_catalogue import catalogue_for
from finlib.standards import Standard

UNIT = "тыс. руб."
TODAY = date(2026, 5, 1)
ANNUAL = date(2025, 12, 31)


def metric(code: str, value: str | None, **extra) -> MetricValue:
    """Показатель маршрута; None — не рассчитан."""
    return MetricValue(
        code=code,
        name=code,
        group="debt",
        in_scoring=True,
        value=None if value is None else Decimal(value),
        **extra,
    )


def rsbu(**kwargs):
    """Вердикт по справочнику РСБУ с обычными доводами."""
    computed = kwargs.pop("computed", ())
    return route(
        computed,
        unit=kwargs.pop("unit", UNIT),
        quarantined=kwargs.pop("quarantined", False),
        today=kwargs.pop("today", TODAY),
        latest_annual=kwargs.pop("latest_annual", ANNUAL),
        catalogue=catalogue_for(Standard.RSBU),
        **kwargs,
    )


# --- справочник величин -----------------------------------------------------


def test_every_standard_declares_its_metrics() -> None:
    """Величины маршрута объявлены у каждого стандарта модели.

    Стандарт без правил означал бы, что маршрут по нему не строится вовсе,
    и это решение — оно называется, а не получается умолчанием.
    """
    declared = load_routing().standards.by_standard
    assert set(declared) == set(Standard)
    for standard in Standard:
        catalogue = catalogue_for(standard)
        assert catalogue.metrics, standard
        assert catalogue.label
        assert catalogue.stop_factors


def test_rsbu_has_no_debt_burden_value_at_all() -> None:
    """У РСБУ долговой нагрузки нет, и это не пробел отчётности.

    Амортизация в формах не раскрывается, поэтому вместо величины объявлена
    граница. Пустота здесь означает «величины не бывает», а не «не рассчитана».
    """
    rule = catalogue_for(Standard.RSBU).rule
    assert rule.burden == ""
    assert rule.bound == "debt_to_op_profit"
    assert rule.bound_of == "net_debt_ebitda"
    assert "debt_to_op_profit" in rule.cover
    ifrs = catalogue_for(Standard.IFRS).rule
    assert ifrs.burden == "net_debt_ebitda"
    assert ifrs.bound == "net_debt_op_profit"


def test_the_scale_of_the_bound_is_the_scale_of_what_it_bounds() -> None:
    """Порог вывода по границе берётся у шкалы ограничиваемого показателя.

    Своего числа маршрут здесь не вводит: порог — крайняя опорная точка
    шкалы долговой нагрузки, и она одна на оба стандарта.
    """
    for standard in Standard:
        catalogue = catalogue_for(standard)
        assert catalogue.threshold_of(catalogue.rule.bound_of) == Decimal("5.0")


# --- вывод по границе -------------------------------------------------------


def test_the_bound_proves_nothing_when_the_result_is_a_loss() -> None:
    """Граница, посчитанная от убытка, основания не даёт вовсе.

    Знак знаменателя решает: при убытке и чистой денежной позиции отношение
    получается **положительным** и выше порога, то есть выглядит настоящей
    долговой нагрузкой. Обстоятельство при этом одно и называется знаком
    результата, а не отношением: «не выше 6,0x» сказало бы о нагрузке
    эмитента, у которого прибыли нет вовсе.
    """
    computed = (
        metric("debt_to_op_profit", "6.0", denominator=Decimal("-100")),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    verdict = rsbu(computed=computed, operating_profit=Decimal("-100"))
    assert "bound_above_threshold" not in verdict.grounds
    # Обстоятельство называется знаком результата, а не отношением.
    assert "operating_loss" in verdict.grounds
    assert verdict.basket == "attention"


def test_the_bound_above_the_threshold_is_attention() -> None:
    """Граница выше порога вывода о прохождении не даёт."""
    computed = (
        metric("debt_to_op_profit", "9.5", denominator=Decimal("100")),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    verdict = rsbu(computed=computed, operating_profit=Decimal("100"))
    assert verdict.basket == "attention"
    assert "bound_above_threshold" in verdict.grounds


def test_the_bound_below_the_threshold_closes_the_gap() -> None:
    """Граница ниже порога доказывает прохождение: пробела данных нет."""
    computed = (
        metric("debt_to_op_profit", "1.2", denominator=Decimal("100")),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    verdict = rsbu(computed=computed, operating_profit=Decimal("100"))
    assert verdict.basket == "clear"
    assert verdict.grounds == ()


def test_a_missing_bound_is_named_as_debt_burden() -> None:
    """Границы нет — недостающее называется предметом, а не кодом."""
    verdict = rsbu(
        computed=(metric("equity_ratio", "0.6"), metric("cur_liq", "2.5"))
    )
    texts = " ".join(verdict.details)
    assert "долговая нагрузка" in texts
    assert "debt_to_op_profit" not in texts


# --- стоп-фактор, объявленный по двум величинам -----------------------------


def test_the_stop_factor_names_the_value_it_fired_by() -> None:
    """Печатается величина, которой стоп-фактор сработал, а не первая из двух.

    Стоп-фактор РСБУ «Нехватка оборотного капитала и покрытия процентов»
    объявлен по двум показателям, и назвать заранее выбранный значило бы
    напечатать величину, основанием не ставшую.
    """
    computed = (
        metric("nwc", "500"),
        metric("interest_cover", "0.55"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    verdict = rsbu(
        computed=computed,
        stop_factors=("weak_coverage",),
        stop_factor_values={"weak_coverage": "interest_cover"},
    )
    said = " ".join(verdict.details)
    assert "0,55" in said
    assert "500" not in said


def test_only_the_working_capital_silences_liquidity() -> None:
    """Гасит текущую ликвидность оборотный капитал, а не покрытие процентов.

    Отрицательный чистый оборотный капитал означает ликвидность ниже единицы —
    это одно обстоятельство. Покрытие процентов ниже единицы обстоятельство
    другое, и величина ликвидности остаётся своим основанием.
    """
    weak = (
        metric("nwc", "-500"),
        metric("interest_cover", "0.55"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "0.5"),
        metric("debt_to_op_profit", "1.0", denominator=Decimal("100")),
    )
    by_nwc = rsbu(
        computed=weak,
        stop_factors=("weak_coverage",),
        stop_factor_values={"weak_coverage": "nwc"},
        operating_profit=Decimal("100"),
    )
    assert "cur_liq" in by_nwc.spoken_for
    by_cover = rsbu(
        computed=weak,
        stop_factors=("weak_coverage",),
        stop_factor_values={"weak_coverage": "interest_cover"},
        operating_profit=Decimal("100"),
    )
    assert "cur_liq" not in by_cover.spoken_for
    assert "level_off_scale" in by_cover.grounds


# --- отчётности нет вовсе ---------------------------------------------------


def test_no_reporting_is_one_ground_not_three() -> None:
    """Отчётности нет — одно основание, а не перечень недостающих величин.

    Перечислять три недостающие величины значило бы называть следствия
    вместо причины; молчать — выдавать отсутствие данных за отсутствие
    обстоятельств.
    """
    verdict = rsbu(computed=(), reporting_unavailable="default")
    assert verdict.basket == "attention"
    assert verdict.grounds == ("reporting_unavailable",)
    assert "data_insufficient" not in verdict.grounds


def test_quarantine_and_absence_are_told_apart() -> None:
    """Отчётность отбракована нами и её нет у источника — разные сведения."""
    absent = rsbu(computed=(), reporting_unavailable="default")
    held = rsbu(computed=(), reporting_unavailable="quarantined")
    assert absent.details != held.details
    assert "карантин" in " ".join(held.details)


# --- холдинг на одной отчётности РСБУ ---------------------------------------


def test_a_holding_on_rsbu_alone_is_not_softer_than_attention() -> None:
    """Отчётность управляющей компании группой не является.

    Величины её верны, а обслуживание долга зависит от дочерних обществ,
    которых в этой отчётности нет.
    """
    computed = (
        metric("debt_to_op_profit", "1.0", denominator=Decimal("100")),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    verdict = rsbu(
        computed=computed, operating_profit=Decimal("100"), okved="64.20"
    )
    assert verdict.basket == "attention"
    assert "holding_rsbu_only" in verdict.grounds
    assert "Деятельность холдинговых компаний" in " ".join(verdict.details)


def test_a_holding_with_consolidated_reporting_is_not_flagged() -> None:
    """У эмитента с консолидированной отчётностью группа видна.

    Основание о периметре относится к отчётности юридического лица, и вид
    деятельности сам по себе обстоятельством не является.
    """
    computed = (
        metric("net_debt_ebitda", "1.0"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    verdict = route(
        computed,
        unit=UNIT,
        quarantined=False,
        today=TODAY,
        latest_annual=ANNUAL,
        okved="64.20",
        catalogue=catalogue_for(Standard.IFRS),
    )
    assert verdict.basket == "clear"


@pytest.mark.parametrize("okved", ["64.20", "64.20.1", "70.10", "70.10.9"])
def test_subtypes_of_the_holding_activity_count(okved: str) -> None:
    """Подвид означает то же, что вид: сравнение идёт началом кода."""
    assert load_routing().holdings.holds(okved)


@pytest.mark.parametrize("okved", ["64.2", "6.420", "70.1", "", "46.71.4"])
def test_a_foreign_activity_is_not_a_holding(okved: str) -> None:
    """Сравнение по началу кода не должно захватывать соседние виды."""
    assert not load_routing().holdings.holds(okved)


def lines(invested: str, assets: str, revenue: str) -> dict[str, Decimal]:
    """Строки запасного признака холдинга."""
    return {
        "1170": Decimal(invested),
        "1240": Decimal(0),
        "1600": Decimal(assets),
        "2110": Decimal(revenue),
    }


def test_the_fallback_holding_sign_works_without_okved() -> None:
    """Признак, работающий только при доступном источнике, неотличим от мёртвого.

    Вид деятельности приносит ГИР БО, а он молчал шесть часов подряд: правило
    не срабатывало у 773 организаций из 807. Запасной признак читает ту же
    отчётность, по которой построен маршрут.
    """
    fallback = load_routing().holdings.fallback
    assert fallback.share(lines("800", "1000", "10")) == Decimal("0.8")


def test_revenue_at_the_materiality_cutoff_is_not_a_holding() -> None:
    """Своя выручка есть — организация ведёт деятельность, а не владеет чужой."""
    fallback = load_routing().holdings.fallback
    assert fallback.share(lines("800", "1000", "200")) is None


def test_investments_at_half_of_assets_are_not_enough() -> None:
    """Отсечка строгая: ровно половина — ещё не «активы суть вложения»."""
    fallback = load_routing().holdings.fallback
    assert fallback.share(lines("500", "1000", "10")) is None


def test_an_undisclosed_line_gives_no_holding_sign() -> None:
    """Нераскрытая величина признака не даёт и нулём не подменяется.

    «Выручки почти нет» и «выручка не раскрыта» — разные сведения, и второе
    о холдинге не говорит ничего.
    """
    fallback = load_routing().holdings.fallback
    without_revenue = lines("800", "1000", "10") | {"2110": None}
    assert fallback.share(without_revenue) is None
    assert fallback.share({"1600": Decimal("1000"), "2110": Decimal("10")}) is None
    assert fallback.share({}) is None


def test_the_okved_sign_wins_when_the_activity_is_known() -> None:
    """При известном виде деятельности решает он: он объявлен реестром.

    Запасной признак — наше чтение отчётности, и ставить его выше факта
    о регистрации значило бы предпочесть догадку заявлению.
    """
    computed = (
        metric("debt_to_op_profit", "1.0", denominator=Decimal("100")),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    verdict = rsbu(
        computed=computed,
        operating_profit=Decimal("100"),
        okved="64.20",
        holding_lines=lines("800", "1000", "10"),
    )
    said = " ".join(
        entry.text for entry in verdict.findings if entry.ground == "holding_rsbu_only"
    )
    assert "64.20" in said
    assert "финансовые вложения" not in said


# --- четвёртый признак «ноль не означает нуля» ------------------------------


def test_zero_debt_with_outstanding_bonds_is_not_disclosure() -> None:
    """Выпуск в обращении и есть заём: нуля по заёмным средствам не бывает."""
    from finlib.normalize.facts import debt_undisclosed

    zeros = {
        "1410": (Decimal(0), "cbonds"),
        "1510": (Decimal(0), "cbonds"),
    }
    assert debt_undisclosed(zeros, has_bonds=True)
    # Без выпусков ноль остаётся нулём: заёмных средств у организации
    # действительно может не быть.
    assert not debt_undisclosed(zeros, has_bonds=False)


def test_a_zero_from_the_primary_source_stays_a_zero() -> None:
    """Признак относится к нулю агрегатора: у первоисточника ноль означает ноль.

    Правило чтения нулей объявлено у вида отчёта агрегатора, а выгрузка
    ГИР БО различает прочерк и ноль сама.
    """
    from finlib.normalize.facts import debt_undisclosed

    assert not debt_undisclosed(
        {"1410": (Decimal(0), "gir_bo"), "1510": (Decimal(0), "gir_bo")},
        has_bonds=True,
    )


def test_one_disclosed_debt_line_is_enough() -> None:
    """Ненулевая строка долга снимает признак: долг раскрыт."""
    from finlib.normalize.facts import debt_undisclosed

    assert not debt_undisclosed(
        {"1410": (Decimal(0), "cbonds"), "1510": (Decimal("500"), "cbonds")},
        has_bonds=True,
    )


def test_an_absent_debt_line_is_not_the_sign() -> None:
    """Строки нет вовсе — показатель и так не считается, признак не нужен.

    Признак заведён для обратного случая: величина есть, равна нулю
    и выглядит раскрытой.
    """
    from finlib.normalize.facts import debt_undisclosed

    assert not debt_undisclosed({"1410": (None, "cbonds")}, has_bonds=True)
    assert not debt_undisclosed({}, has_bonds=True)


def test_a_human_decision_is_found_by_the_standard_of_its_row() -> None:
    """Решение человека ищется по стандарту своей строки, а не всегда по МСФО.

    Универсум задан долгом, и у 439 эмитентов маршрут строится по отчётности
    юридического лица. Выборка, называвшая `ifrs` наглухо, решения о любом
    из них не нашла бы вовсе: правило не срабатывало бы никогда, а по журналу
    выглядело бы записанным.
    """
    from finlib.scoring.routing import ManualFloor
    from finlib.scoring.routing_store import floor_for

    floor = ManualFloor(
        basket="review",
        author="имя",
        reason="группа: связанный эмитент в разборе",
        decided_on=date(2026, 9, 23),
        valid_until=date(2027, 3, 31),
    )
    decided = {"1": {Standard.RSBU.value: floor}}
    assert floor_for(decided, "1", Standard.RSBU) is floor
    # Стандарт назван у всякой выборки по ИНН: решение по отчётности
    # юридического лица о консолидированной не говорит.
    assert floor_for(decided, "1", Standard.IFRS) is None
    # У эмитента без отчётности стандарта нет вовсе, и спутать решение
    # не с чем: комплекта, о котором оно сказало бы другое, не существует.
    assert floor_for(decided, "1", None) is floor
    assert floor_for(decided, "2", Standard.RSBU) is None


def _type(code: str):
    """Тип эмитента по коду из справочника маршрутизации."""
    return next(
        kind for kind in load_routing().issuer_types if kind.code == code
    )


def test_a_structural_issuer_is_not_judged_by_its_balance() -> None:
    """У СФО отрицательный капитал — устройство сделки, а не риск.

    Решение человека 23.09.2026: из 186 позиций «Разбора» 59 занимали СФО
    и ипотечные агенты, 44 из них по отрицательному капиталу. Однотипные
    эмитенты стояли при этом в трёх разных корзинах по знаку строки 1300 —
    корзина мерила устройство сделки.
    """
    verdict = route(
        (metric("equity_ratio", "-0.4"),),
        unit=UNIT,
        quarantined=False,
        stop_factors=("negative_equity",),
        issuer_type=_type("structural"),
        type_marker="фирменное наименование: «сфо»",
        latest_annual=ANNUAL,
        today=TODAY,
        catalogue=catalogue_for(Standard.RSBU),
    )
    assert verdict.basket == "structural_pool"
    assert verdict.grounds == ("structural_issuer",)
    # Отброшенное считается: правило, гасящее молча, неотличимо
    # от невыполненного.
    assert "stop_factor_severe" in verdict.inapplicable
    # Признак назван: «структурный» без него читался бы как наше суждение.
    assert "сфо" in verdict.details[0]


def test_an_event_still_routes_a_structural_issuer() -> None:
    """Тип не отменяет маршрут: дефолт у СФО — дефолт.

    Неприменимы к нему корпоративные коэффициенты, а не события.
    """
    from finlib.sources.cbonds_events import Issue, IssuerEvents

    issue = Issue(
        emission_id="1",
        name="СФО, 01",
        isin="RU1",
        status="дефолт по погашению",
        default=True,
        unsettled=True,
        maturity=date(2026, 3, 1),
        offer=None,
        outstanding=None,
        updated=ANNUAL,
    )
    verdict = route(
        (metric("equity_ratio", "-0.4"),),
        unit=UNIT,
        quarantined=False,
        stop_factors=("negative_equity",),
        issuer_type=_type("structural"),
        events=IssuerEvents(inn="1", issues=(issue,), issues_known=True),
        latest_annual=ANNUAL,
        today=TODAY,
        catalogue=catalogue_for(Standard.RSBU),
    )
    assert verdict.basket == "review"
    assert verdict.grounds == ("emission_default",)


def test_a_bank_is_not_asked_about_disclosure_we_do_not_collect() -> None:
    """Банк — вне периметра методики, и срок раскрытия у него не спрашивается.

    Банк сдаёт отчётность в Банк России по формам 0409, а мы её не собираем
    вовсе: «нарушение срока раскрытия» мерило бы наш охват, а не эмитента.
    Прежде по этому основанию во «Внимании» стояли 51 банк и МФО.
    """
    verdict = route(
        (),
        unit=UNIT,
        quarantined=False,
        issuer_type=_type("out_of_scope"),
        type_marker="отрасль источника: Банки",
        reporting_unavailable="нормализованной отчётности у источника нет",
        latest_annual=date(2024, 12, 31),
        # После срока сдачи годовой отчётности: до него признак не срабатывает
        # вовсе, и проверять было бы нечего.
        today=date(2026, 9, 23),
        catalogue=catalogue_for(Standard.RSBU),
    )
    assert verdict.basket == "out_of_scope"
    assert "disclosure_overdue" in verdict.inapplicable
    assert "reporting_unavailable" in verdict.inapplicable


def test_every_event_ground_is_named_in_every_type() -> None:
    """Событийное основание обязано быть названо у каждого типа.

    Белый список безопаснее чёрного: новое основание по величинам само
    к банку не применится. Но новое **событийное** при этом молча выпало бы
    из маршрута такого эмитента, и заметить это было бы нечем — поэтому
    перечень сверяется с подгруппой «события и рейтинги» справочника.
    """
    routing = load_routing()
    attention = routing.basket("attention")
    events = {
        ground.code
        for ground in attention.grounds
        if attention.group_of(ground.code) == "event_risk"
    }
    review = {"emission_default", "rating_default", "risk_sector"}
    for kind in routing.issuer_types:
        missing = (events | review) - set(kind.grounds_apply)
        assert not missing, f"{kind.code}: не названы основания событий {missing}"


def test_every_type_names_a_queue_that_exists() -> None:
    """Очередь типа — объявленная корзина, а не выдуманный код."""
    routing = load_routing()
    codes = {basket.code for basket in routing.baskets}
    for kind in routing.issuer_types:
        assert kind.queue in codes
        assert kind.ground in {
            ground.code for ground in routing.basket(kind.queue).grounds
        }


def test_every_standard_names_its_debt_lines_and_metrics() -> None:
    """У каждого стандарта объявлены строки долга и величины из них.

    Признак внешний, и применить его без перечня нечем; а величина долга,
    оставленная при неизвестном долге, печаталась бы как чистая денежная
    позиция — то есть как довод в пользу эмитента.
    """
    for standard in Standard:
        rule = catalogue_for(standard).rule
        assert rule.debt_lines
        assert rule.debt_metrics
        assert rule.bound in rule.debt_metrics
