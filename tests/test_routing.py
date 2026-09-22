"""Маршрутизация: корзина по обстоятельствам, а не по нашему усмотрению.

Структура правил утверждена человеком, пороги остаются предварительными,
и тест закрепляет не пороги, а **развод оснований по корзинам**. Стоп-фактор
с ограничением средним и стоп-фактор с ограничением низшим — разные
обстоятельства, и первое отправляло к человеку половину универсума, пока
разводом не занялись.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.ifrs import MetricValue
from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.scoring.routing import RoutingPolicy, load_routing, route


def metric(code: str, value: str, name: str = "показатель") -> MetricValue:
    """Рассчитанный показатель для маршрута."""
    return MetricValue(
        code=code, name=name, group="debt", in_scoring=True, value=Decimal(value)
    )


def healthy() -> tuple[MetricValue, ...]:
    """Величины маршрута, при которых оснований нет ни одного."""
    return (
        metric("net_debt_ebitda", "1.0", "Чистый долг / EBITDA"),
        metric("equity_ratio", "0.6", "Коэффициент автономии"),
        metric("cur_liq", "2.5", "Текущая ликвидность"),
    )


def routed(**kwargs) -> tuple[str, tuple[str, ...]]:
    """Корзина и основания при здоровых величинах и названных обстоятельствах."""
    computed = kwargs.pop("computed", healthy())
    verdict = route(
        computed,
        quarantined=kwargs.pop("quarantined", False),
        today=kwargs.pop("today", date(2026, 5, 1)),
        latest_annual=kwargs.pop("latest_annual", date(2025, 12, 31)),
        **kwargs,
    )
    return verdict.basket, verdict.grounds


def test_healthy_issuer_needs_no_one() -> None:
    """Все величины в пределах шкал — корзина «Без внимания»."""
    assert routed() == ("clear", ())


def test_capped_stop_factor_is_attention() -> None:
    """Ограничение класса средним — обстоятельство внимания, а не разбора.

    Норма неприменимости отрицательного оборотного капитала объявлена
    по покрытию процентов, которого у нормализованных данных нет вовсе:
    признак срабатывает у всех, и разбор по нему означал бы разбор половины
    универсума.
    """
    assert routed(stop_factors=("negative_nwc",)) == (
        "attention",
        ("stop_factor_capped",),
    )


def test_severe_stop_factor_is_review() -> None:
    """Ограничение класса низшим и неустойчивым — разбор."""
    for code in ("negative_equity", "going_concern_uncertainty"):
        basket, grounds = routed(stop_factors=(code,))
        assert (basket, grounds) == ("review", ("stop_factor_severe",))


def test_severity_is_taken_from_the_methodology() -> None:
    """Стоп-фактор без объявленной градации молча штатным не становится."""
    with pytest.raises(ValueError, match="градация"):
        routed(stop_factors=("нет такого стоп-фактора",))


def test_missing_values_are_attention_not_review() -> None:
    """Нехватка данных — не риск: она называет поле и идёт во внимание."""
    basket, grounds = routed(computed=(metric("equity_ratio", "0.6"),))
    assert basket == "attention"
    assert grounds == ("data_insufficient",)


def test_assessed_class_outweighs_normalised_values() -> None:
    """Присвоенный нами класс D или E — разбор, а класс выше — нет."""
    assert routed(assessed_class="E") == ("review", ("assessed_class_low",))
    assert routed(assessed_class="B") == ("clear", ())


def test_value_past_the_last_calibration_point_is_review() -> None:
    """Балл уровня ноль означает конец шкалы, и одной такой величины хватает."""
    policy = load_ifrs_metrics()
    edge = policy.calibration_points.metrics["net_debt_ebitda"].points[0][0]
    computed = (
        metric("net_debt_ebitda", str(edge + 1), "Чистый долг / EBITDA"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    assert routed(computed=computed) == ("review", ("level_off_scale",))


def test_lower_band_and_off_scale_are_not_counted_twice() -> None:
    """Одна величина даёт одно основание: ноль балла не сопровождается нижней частью."""
    policy = load_ifrs_metrics()
    edge = policy.calibration_points.metrics["equity_ratio"].points[0][0]
    computed = (
        metric("net_debt_ebitda", "1.0"),
        metric("equity_ratio", str(edge - 1), "Коэффициент автономии"),
        metric("cur_liq", "2.5"),
    )
    basket, grounds = routed(computed=computed)
    assert (basket, grounds) == ("review", ("level_off_scale",))


def test_stop_factor_speaks_for_its_metric() -> None:
    """Величина не повторяет стоп-фактор: обстоятельство одно, решение одно.

    Отрицательный чистый оборотный капитал означает ликвидность ниже единицы,
    и её положение за концом шкалы о новом не говорит. Иначе правило о тяжести
    отменялось бы следующим правилом — у 82 эмитентов набора ровно так
    и выходило.
    """
    computed = (
        metric("net_debt_ebitda", "1.0"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "0.3", "Текущая ликвидность"),
    )
    assert routed(computed=computed, stop_factors=("negative_nwc",)) == (
        "attention",
        ("stop_factor_capped",),
    )
    # Без стоп-фактора та же величина основание даёт: правило гасит повтор,
    # а не саму проверку.
    assert routed(computed=computed) == ("review", ("level_off_scale",))


def test_matching_metric_code_needs_no_declaration() -> None:
    """Совпадение кода показателя действует само — это тот же показатель."""
    computed = (
        metric("net_debt_ebitda", "1.0"),
        metric("equity_ratio", "-0.2", "Коэффициент автономии"),
        metric("cur_liq", "2.5"),
    )
    assert routed(computed=computed, stop_factors=("negative_autonomy",)) == (
        "review",
        ("stop_factor_severe",),
    )


def test_attention_is_split_by_nature_of_the_circumstance() -> None:
    """Внимание показывается по старшей подгруппе, остальные называются."""
    verdict = route(
        (metric("equity_ratio", "0.6"),),
        quarantined=False,
        latest_annual=date(2024, 12, 31),
        today=date(2026, 6, 2),
    )
    assert verdict.basket == "attention"
    assert verdict.subgroups == ("data_gap", "disclosure")
    assert verdict.actions[0] == "добрать данные"
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_nwc",),
        latest_annual=date(2024, 12, 31),
        today=date(2026, 6, 2),
    )
    assert verdict.subgroups == ("value_risk", "disclosure")
    assert verdict.subgroup == "value_risk"


def test_review_has_no_subgroups() -> None:
    """Корзина без подгрупп их не выдумывает: показывать было бы нечего."""
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_equity",),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "review"
    assert verdict.subgroups == ()
    assert verdict.subgroup == ""


def test_overdue_disclosure_is_its_own_ground() -> None:
    """Срок раскрытия нарушен — внимание, и величинам это не приписывается."""
    basket, grounds = routed(
        latest_annual=date(2024, 12, 31), today=date(2026, 6, 2)
    )
    assert (basket, grounds) == ("attention", ("disclosure_overdue",))


def test_status_and_maturity_travel_with_the_verdict() -> None:
    """Утверждённая структура и зрелость порогов — два разных сведения.

    Согласие с составом корзин не делает величины калиброванными, и вердикт
    обязан нести оба: умолчание о зрелости выдало бы предварительный порог
    за проверенный.
    """
    policy = load_routing()
    verdict = route(healthy(), quarantined=False, latest_annual=date(2025, 12, 31),
                    today=date(2026, 5, 1))
    assert verdict.status == policy.status
    assert verdict.thresholds == policy.thresholds
    if policy.status != "approved":
        assert "черновик" in verdict.describe()
    elif policy.thresholds == "preliminary":
        assert "пороги предварительны" in verdict.describe()


def test_approval_and_maturity_are_declared_with_their_reasons() -> None:
    """Утверждение называет автора, предварительность — причину."""
    policy = load_routing()
    raw = policy.model_dump()
    raw["approved_by"] = None
    with pytest.raises(ValueError, match="кем"):
        RoutingPolicy.model_validate(raw)
    raw = policy.model_dump()
    raw["thresholds_origin"] = ""
    raw["thresholds"] = "preliminary"
    with pytest.raises(ValueError, match="почему"):
        RoutingPolicy.model_validate(raw)


def test_bound_below_the_threshold_proves_the_criterion() -> None:
    """Вывод по границе — доказательство, и пробелом он не считается.

    При положительной операционной прибыли амортизация неотрицательна, поэтому
    отношение к EBITDA не выше отношения к операционной прибыли: граница ниже
    порога означает, что и показатель ниже. Дефект был не в правиле, а в печати:
    строка писала «не считается» и границы не показывала.
    """
    computed = (
        metric("net_debt_op_profit", "2.3", "Чистый долг / EBITDA, оценка сверху"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    assert routed(computed=computed) == ("clear", ())


def test_bound_above_the_threshold_gives_no_conclusion() -> None:
    """Граница выше порога вывода не даёт: корзина — внимание.

    «Не выше 13,4x» при пороге 5,0x не означает, что показатель выше порога:
    настоящее значение бывает и ниже. Это отсутствие вывода, а не плохая
    величина, и корзина у него внимание, а не разбор.
    """
    computed = (
        metric("net_debt_op_profit", "13.4", "Чистый долг / EBITDA, оценка сверху"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    basket, grounds = routed(computed=computed)
    assert basket == "attention"
    assert set(grounds) == {"data_insufficient", "bound_above_threshold"}


def test_branch_mutes_the_stop_factor_of_its_business_model() -> None:
    """В пяти отраслях отрицательный оборотный капитал основания не даёт.

    Гасится основание маршрута, а не стоп-фактор: класс методика ограничивает
    по-прежнему. Гашение считается — правило, гасящее молча, неотличимо
    от невыполненного.
    """
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_nwc",),
        branch="Электроэнергетика",
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "clear"
    assert verdict.muted == ("negative_nwc",)
    # Отрасль вне перечня гасителем не служит.
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_nwc",),
        branch="Производство лекарств и биотехнологии",
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "attention"
    assert verdict.muted == ()


def test_group_member_is_not_softer_than_attention() -> None:
    """Если кто-то в группе в разборе, её член не мягче внимания.

    Соразмерно, а не выравниванием: корзина разбора не переносится — она
    сказана о том эмитенте, у которого обстоятельство найдено.
    """
    verdict = route(
        healthy(),
        quarantined=False,
        group_under_review=("Мечел", "Мечел"),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("group_under_review",)


def test_non_positive_ebitda_is_its_own_ground() -> None:
    """Неположительная EBITDA не делает долговую нагрузку хорошей.

    Отношение чистого долга к неположительной EBITDA отрицательно, и шкала
    читает его как низкую нагрузку: эмитент с убытком оставался бы без
    внимания. Знак объявлен своим основанием, и величина отношения своего
    основания уже не даёт — обстоятельство одно.
    """
    computed = (
        metric("net_debt_ebitda", "-2.0", "Чистый долг / EBITDA"),
        metric("ebitda", "-500", "EBITDA"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    basket, grounds = routed(computed=computed)
    assert basket == "attention"
    assert grounds == ("negative_ebitda",)


def test_two_cycles_without_reporting_open_their_own_queue() -> None:
    """Давность старше двух циклов раскрытия уводит из корзин тяжести.

    По числам такой давности маршрут не строится: они описывают организацию,
    которой могло не стать, и вопрос к ней другой — о статусе. Очередь
    старше любого основания тяжести, в том числе стоп-фактора.
    """
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_equity",),
        latest_annual=date(2022, 12, 31),
        today=date(2026, 9, 22),
    )
    assert verdict.basket == "status_unknown"
    assert verdict.grounds == ("reporting_two_cycles_old",)
    # Обстоятельство тяжести при этом не исчезает: человек, которому комплект
    # передают, обязан видеть и его.
    assert any(item.ground == "stop_factor_severe" for item in verdict.findings)


def test_one_cycle_behind_stays_in_the_severity_baskets() -> None:
    """Один пропущенный цикл очередь статуса не открывает."""
    verdict = route(
        healthy(),
        quarantined=False,
        latest_annual=date(2024, 12, 31),
        today=date(2026, 9, 22),
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("disclosure_overdue",)


def test_default_on_an_issue_is_a_review_ground() -> None:
    """Дефолт по выпуску — обстоятельство разбора, и дата события называется.

    Годовая отчётность события между отчётными датами не видит: у ЕвроТранса
    числа за 2025 год спокойны, а по двенадцати выпускам стоит неурегулированный
    дефолт.
    """
    from finlib.sources.cbonds_events import Issue, IssuerEvents

    events = IssuerEvents(
        inn="5029169023",
        issues=(
            Issue(
                emission_id="1505283",
                name="БО-03",
                isin="RU000A106UB7",
                status="дефолт по погашению",
                default=True,
                unsettled=True,
                maturity=date(2026, 8, 22),
                offer=None,
                outstanding=Decimal(300000000),
                updated=date(2026, 9, 4),
            ),
        ),
        issues_known=True,
    )
    verdict = route(
        healthy(),
        quarantined=False,
        events=events,
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "review"
    assert verdict.grounds == ("emission_default",)
    # **Перечня событий у этого эмитента на диске нет, и строка говорит именно
    # это.** «Погашение 22.08.2026» было бы сведением об источнике, тогда как
    # дело в недошедшей доставке.
    assert "перечня событий дефолта на диске нет" in verdict.details[0]


def test_settled_default_is_not_a_current_circumstance() -> None:
    """Урегулированный дефолт прошлого в разбор не отправляет.

    У ДВМП признак дефолта стоит по еврооблигациям, погашенным около десяти лет
    назад: событие настоящее, но давно улаженное, и судить по нему о нынешнем
    эмитенте значило бы мерить его прошлым.
    """
    from finlib.sources.cbonds_events import Issue, IssuerEvents

    events = IssuerEvents(
        inn="2540047110",
        issues=(
            Issue(
                emission_id="9",
                name="еврооблигации",
                isin="XS0000000000",
                status="погашена",
                default=True,
                unsettled=False,
                maturity=date(2016, 5, 2),
                offer=None,
                outstanding=None,
                updated=date(2024, 3, 11),
            ),
        ),
        issues_known=True,
        records=(record("9", "2016-06-03", met="2016-07-01"),),
        records_known=True,
    )
    verdict = route(
        healthy(),
        quarantined=False,
        events=events,
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "clear"


def test_only_credit_ratings_reach_the_grades() -> None:
    """ESG-рейтинг о кредитоспособности не говорит и в градацию не идёт.

    При первом прогоне в категориях оказались «ESG-A-», «ESG-II(c)» и «5»:
    точки шкал, к кредитоспособности не относящихся. Кредитная шкала объявлена
    методикой по идентификатору, а не по вхождению «ESG» в наименование.
    """
    from finlib.sources.cbonds_events import IssuerEvents, Rating

    esg = Rating(
        agency="Эксперт РА",
        scale="ESG рейтинг",
        point="ESG-C",
        category="C",
        outlook="",
        assigned=date(2026, 1, 1),
        credit=False,
    )
    credit = Rating(
        agency="Эксперт РА",
        scale="Национальная российская рейтинговая шкала",
        point="ruC",
        category="C",
        outlook="",
        assigned=date(2026, 5, 12),
        credit=True,
    )
    verdict = route(
        healthy(),
        quarantined=False,
        events=IssuerEvents(inn="1", ratings=(esg,), ratings_known=True),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "clear"
    verdict = route(
        healthy(),
        quarantined=False,
        events=IssuerEvents(inn="1", ratings=(credit,), ratings_known=True),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.grounds == ("rating_default",)


def issue(name: str, status: str, maturity: date, *, unsettled: bool):
    """Выпуск с признаком дефолта: улаженным либо нет."""
    from finlib.sources.cbonds_events import Issue

    return Issue(
        emission_id=name,
        name=name,
        isin=f"RU{name}",
        status=status,
        default=True,
        unsettled=unsettled,
        maturity=maturity,
        offer=None,
        outstanding=None,
        updated=date(2026, 9, 1),
    )


def record(
    emission: str,
    when: str,
    *,
    met: str | None = None,
    kind: str = "Купон",
    status: str = "Дефолт",
    amount: str | None = None,
):
    """Событие дефолта: дата, вид, факт исполнения и неисполненная сумма."""
    from finlib.sources.cbonds_events import DefaultRecord

    return DefaultRecord(
        emission_id=emission,
        kind=kind,
        status=status,
        due=date.fromisoformat(when),
        when=date.fromisoformat(when),
        announced=None,
        met=date.fromisoformat(met) if met else None,
        amount=Decimal(amount) if amount else None,
    )


def undated_record(emission: str):
    """Событие дефолта, у которого нет ни одной даты."""
    from finlib.sources.cbonds_events import DefaultRecord

    return DefaultRecord(
        emission_id=emission,
        kind="Купон",
        status="Дефолт",
        due=None,
        when=None,
        announced=None,
        met=None,
        amount=None,
    )


def with_issues(*issues, records=()):
    """События эмитента: выпуски и события дефолтов по ним."""
    from finlib.sources.cbonds_events import IssuerEvents

    return IssuerEvents(
        inn="1",
        issues=issues,
        issues_known=True,
        records=tuple(records),
        records_known=bool(records),
    )


def verdict_for(events, today: date = date(2026, 9, 22)):
    """Вердикт при здоровых величинах и названных событиях."""
    return route(
        healthy(),
        quarantined=False,
        events=events,
        latest_annual=date(2025, 12, 31),
        today=today,
    )


def test_stale_unsettled_default_asks_about_settlement() -> None:
    """Неурегулированный дефолт старше трёх лет — вопрос, а не разбор.

    У ДВМП погашение БО-01 было должно состояться 27.02.2018: признак стоит,
    но обстоятельством настоящего он не является — с тех пор сменились и долг,
    и собственник. Разбор по такому признаку разбирал бы прошлое.
    """
    verdict = verdict_for(
        with_issues(
            issue("БО-01", "дефолт по погашению", date(2018, 2, 27), unsettled=True),
            issue("БО-02", "дефолт по погашению", date(2017, 11, 28), unsettled=True),
            records=(
                record("БО-01", "2018-03-15", kind="Погашение"),
                record("БО-02", "2017-12-12", kind="Погашение"),
            ),
        )
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("default_unsettled_stale",)
    assert "2018" in verdict.details[0]
    assert "урегулирования" in verdict.details[0]


def test_unsettled_default_without_a_date_stays_in_review() -> None:
    """Неизвестная давность корзину не понижает.

    Перечня событий может не быть на диске вовсе, и тогда дата неизвестна:
    понизить корзину по неизвестной давности значило бы принять решение
    по отсутствию данных.
    """
    verdict = verdict_for(
        with_issues(issue("БО-001Р-03", "в обращении", date(2032, 3, 14), unsettled=True))
    )
    assert verdict.basket == "review"
    assert verdict.grounds == ("emission_default",)


def test_fresh_unsettled_default_names_the_amount() -> None:
    """Разбор называет вид события, дату и неисполненную сумму.

    «Дефолт по погашению» говорит, что случилось; «купон 03.08.2026,
    не исполнено 86 602 000» говорит, сколько именно не заплатили, — и этого
    сведения нет больше нигде.
    """
    verdict = verdict_for(
        with_issues(
            issue("БО-001Р-07", "в обращении", date(2027, 3, 31), unsettled=True),
            records=(record("БО-001Р-07", "2026-08-17", amount="86602000"),),
        )
    )
    assert verdict.basket == "review"
    # Разряды разделяет неразрывный пробел — единая точка округления;
    # обычный пробел здесь означал бы, что число набрано вторым способом.
    assert "86 602 000" in verdict.details[0]
    assert "17.08.2026" in verdict.details[0]


def test_events_outweigh_the_card_flag() -> None:
    """События первичны, признак карточки — только при их отсутствии.

    Событие подробнее и датировано; признак карточки отстаёт, и это видно
    на ДВМП: обязательства 2018 года, а признак стоит бессрочно. Поэтому
    выпуск с признаком, о котором события нет, обстоятельства не создаёт,
    пока события есть у других выпусков.
    """
    events = with_issues(
        issue("БО-01", "дефолт по погашению", date(2018, 2, 27), unsettled=True),
        issue("БО-05", "в обращении", date(2031, 3, 27), unsettled=True),
        records=(record("БО-01", "2018-03-15", kind="Погашение"),),
    )
    verdict = verdict_for(events)
    assert verdict.basket == "attention"
    assert verdict.grounds == ("default_unsettled_stale",)
    assert "2018" in verdict.details[0]


def test_an_undated_event_is_not_made_old_by_a_dated_one() -> None:
    """Событие без даты давности не имеет, и датированное его не закрывает."""
    verdict = verdict_for(
        with_issues(
            issue("БО-01", "дефолт по погашению", date(2018, 2, 27), unsettled=True),
            records=(
                record("БО-01", "2018-03-15", kind="Погашение"),
                undated_record("БО-02"),
            ),
        )
    )
    assert verdict.basket == "review"
    assert verdict.grounds == ("emission_default",)


def test_recently_settled_default_is_credit_history() -> None:
    """Улаженный недавно дефолт — кредитная история, то есть внимание."""
    verdict = verdict_for(
        with_issues(
            issue("001P-02", "погашена", date(2025, 11, 25), unsettled=False),
            records=(record("001P-02", "2025-11-25", met="2025-12-02"),),
        )
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("default_settled_recent",)
    assert "2025" in verdict.details[0]


def test_stale_settled_default_is_a_note_and_not_a_silence() -> None:
    """Улаженный давно дефолт корзины не называет, но и не исчезает.

    Человек, открывший строку, найдёт признак в карточке источника сам,
    и молчание маршрута он прочтёт как недосмотр.
    """
    verdict = verdict_for(
        with_issues(
            issue("еврооблигации", "погашена", date(2016, 5, 2), unsettled=False),
            records=(record("еврооблигации", "2016-06-03", met="2016-08-01"),),
        )
    )
    assert verdict.basket == "clear"
    assert verdict.grounds == ()
    assert len(verdict.notes) == 1
    assert verdict.notes[0].ground == "default_settled_stale"
    assert "2016" in verdict.notes[0].text


def test_reference_ground_is_not_a_basket_ground() -> None:
    """Справочное основание корзину не называет — ни одну.

    Основание, объявленное и справочным, и основанием корзины, читалось бы
    как правило, а было бы порядком проверок.
    """
    routing = load_routing()
    in_baskets = {
        ground.code for basket in routing.baskets for ground in basket.grounds
    }
    assert {ground.code for ground in routing.reference} & in_baskets == set()
    for ground in routing.reference:
        assert routing.say(ground.code, year="2016", years=3, issue="выпуск")


def test_unsettled_event_outweighs_a_later_settled_one() -> None:
    """Давность считается по неисполненному, а не по тому, что позже улажено.

    У ТГК-2 семь событий, из них два без даты исполнения: они и называют
    давность. Событие, которое позже и улажено, обстоятельством не является.
    """
    from finlib.sources.cbonds_events import default_event

    found = default_event(
        (
            record("БО-01", "2013-10-17"),
            record("БО-02", "2019-04-26", met="2019-05-14"),
        ),
        unsettled=True,
    )
    assert found.when == date(2013, 10, 17)
    assert "купон" in found.origin


def refinance(due: str | None, cash: str | None):
    """Платежи года и денежные средства в единице комплекта."""
    from finlib.scoring.routing import Refinance

    return Refinance(
        due=Decimal(due) if due is not None else None,
        cash=Decimal(cash) if cash is not None else None,
        unit="тыс. руб.",
        months=12,
    )


def test_payments_above_cash_are_their_own_ground() -> None:
    """Срочность долга в балансе не видна, и это отдельное обстоятельство.

    «Долг 40 млрд» у эмитента с погашением через восемь лет и с погашением
    в марте означает разное, а балансовые коэффициенты их не различают.
    """
    verdict = route(
        healthy(),
        quarantined=False,
        refinance=refinance("7552350", "223194"),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("refinancing_gap",)
    # Разряды разделяет неразрывный пробел — единая точка округления.
    assert "223 194 тыс. руб." in verdict.details[0]


def test_payments_within_cash_give_no_ground() -> None:
    """Денежных средств хватает — обстоятельства нет."""
    verdict = route(
        healthy(),
        quarantined=False,
        refinance=refinance("100", "900"),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "clear"


def test_payments_without_cash_are_a_gap_and_not_a_risk() -> None:
    """Платежи есть, денежных средств нет — это пробел данных, а не риск.

    Так устроены семь эмитентов группы Роснефти: агрегатор не раскрывает
    им ни денежных средств, ни долга. Отношением обстоятельство не выражается,
    и объявлять по нему риск значило бы судить по величине, которой нет.
    """
    verdict = route(
        healthy(),
        quarantined=False,
        refinance=refinance("10424", None),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("data_insufficient",)
    assert "денежные средства" in verdict.details[0]


def test_financing_structure_takes_the_basket_of_its_guarantor() -> None:
    """SPV оценивается не собой, а тем, кто отвечает по её долгу.

    У финансирующей структуры «прочие» — внутригрупповые займы, а отрицательный
    капитал бывает устройством: её величины описывают договор, а не
    деятельность. Основания при этом берутся у поручителя — человеку нужны
    они, — а объяснение остаётся справочным.
    """
    from finlib.scoring.routing import led_by_guarantor

    routing = load_routing()
    spv = route(
        healthy(),
        quarantined=False,
        financing_structure=True,
        guarantor="Головная компания",
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert spv.basket == "review"
    assert "поручитель" in spv.details[0].lower()

    backer = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_nwc",),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert backer.basket == "attention"

    led = led_by_guarantor(spv, "Головная компания", backer, "Группа", routing)
    assert led.basket == backer.basket
    assert led.grounds == backer.grounds
    assert led.subgroup_names == backer.subgroup_names
    assert led.notes[0].text == "SPV группы Группа: корзина поручителя Головная компания"


def test_the_exchange_moving_an_issue_to_the_risk_sector_is_a_review_ground() -> None:
    """Перевод выпуска в сектор повышенного риска — решение биржи с датой.

    Биржа не предполагает и не считает: она перевела бумагу в другой режим
    и день перевода назвала. Обстоятельство при этом о выпуске, и называется
    каждый переведённый выпуск.
    """
    from finlib.sources.moex_risk import RiskSector

    moved = RiskSector(
        isin="RU000A1061K1",
        board="TQRD",
        since=date(2026, 8, 6),
        came_from="TQCB",
        left_on=date(2026, 8, 5),
        name="ЕвроТранс, БО-001Р-03",
    )
    verdict = route(
        healthy(),
        quarantined=False,
        risk_sector=(moved,),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 9, 22),
    )
    assert verdict.basket == "review"
    assert verdict.grounds == ("risk_sector",)
    assert "06.08.2026" in verdict.details[0]
    assert "TQCB" in verdict.details[0]


def test_an_undated_transfer_says_so_instead_of_inventing_a_date() -> None:
    """Даты перевода нет — формулировка говорит это, а не молчит о ней."""
    from finlib.sources.moex_risk import RiskSector

    verdict = route(
        healthy(),
        quarantined=False,
        risk_sector=(
            RiskSector(
                isin="RU000A105SZ2",
                board="TQRD",
                since=None,
                came_from="",
                left_on=None,
                name="выпуск",
            ),
        ),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 9, 22),
    )
    assert verdict.basket == "review"
    assert "даты перевода биржа не приводит" in verdict.details[0]


def test_large_debt_is_not_left_clear_on_an_upper_bound() -> None:
    """Эмитенту верхнего десятка «Без внимания» даётся только при полном покрытии.

    Оценка сверху прохождение критерия доказывает, а величину не заменяет,
    и у эмитента с крупным долгом в обращении цена этой замены выше всех
    прочих. Обстоятельство здесь о нашем знании, а не о нём.
    """
    computed = (
        metric("net_debt_op_profit", "2.0", "Чистый долг / операционная прибыль"),
        metric("equity_ratio", "0.6", "Коэффициент автономии"),
        metric("cur_liq", "2.5", "Текущая ликвидность"),
    )
    verdict = route(
        computed,
        quarantined=False,
        systemic_volume=Decimal("3902500000000"),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "attention"
    assert "systemic_partial_cover" in verdict.grounds
    assert "покрытие неполное" in verdict.details[0]


def test_large_debt_with_full_cover_stays_clear() -> None:
    """Величины рассчитаны — крупный долг сам обстоятельством не является."""
    verdict = route(
        healthy(),
        quarantined=False,
        systemic_volume=Decimal("1714000000000"),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "clear"


def test_a_guarantor_under_review_is_not_softer_than_attention() -> None:
    """Обстоятельство поручителя говорит и о заёмщике, но корзины не переносит.

    То же соразмерно, что у группового контура: разбор сказан о поручителе,
    а не о том, за кого он отвечает. Поручительство при этом объявлено
    договором, а не выведено из принадлежности к группе.
    """
    verdict = route(
        healthy(),
        quarantined=False,
        guarantor_under_review="Головная компания",
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("guarantor_under_review",)
    assert verdict.details[0] == "Поручитель Головная компания в разборе"


def test_an_offeror_is_not_a_guarantor() -> None:
    """Оферент отвечает за выкуп бумаги, а не за долг.

    Обязательство о ликвидности кредитным качеством не является, и брать
    по нему чужую корзину нельзя. Единственная финансирующая структура списка
    имеет ровно одну такую запись — и физическим лицом.
    """
    import json

    from finlib.sources import cbonds_events

    routing = load_routing()
    assert set(routing.events.guarantee_statuses) & set(
        routing.events.offer_statuses
    ) == set()
    records = {
        "items": [
            {
                "guarantor_inn": "7700000001",
                "guarantor_name_rus": "Поручитель",
                "status_name_rus": "Поручитель",
                "emission_document_rus": "БО-01",
            },
            {
                "guarantor_inn": "770400325504",
                "guarantor_name_rus": "Панфилов Алексей Юрьевич",
                "status_name_rus": "Оферент",
                "emission_document_rus": "БО-02",
            },
        ]
    }
    where = cbonds_events.CACHE
    try:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            cbonds_events.CACHE = Path(folder)
            (cbonds_events.CACHE / "guarantors_1.json").write_text(
                json.dumps(records, ensure_ascii=False), encoding="utf-8"
            )
            found = cbonds_events.guarantees_of(
                "1", frozenset(routing.events.guarantee_statuses)
            )
    finally:
        cbonds_events.CACHE = where
    assert [item.name for item in found] == ["Поручитель"]


def test_without_records_there_is_no_event_date() -> None:
    """Перечня событий нет — давности нет, и это не ноль лет."""
    from finlib.sources.cbonds_events import default_event

    found = default_event((), unsettled=True)
    assert not found.known
    assert found.origin == ""


def test_settled_events_outweigh_the_card_flag_but_are_counted() -> None:
    """Все события исполнены — обстоятельства нет, а расхождение считается.

    У Росгеологии карточка эмитента объявляет неурегулированный дефолт, а все
    семь событий исполнены. Событие первично: оно датировано и подробнее.
    Расхождение при этом не исчезает — оно противоречие внутри агрегатора
    и идёт в перечень к нему, а не вопросом к эмитенту.
    """
    events = with_issues(
        issue("001Р-02", "досрочно погашена", date(2026, 11, 15), unsettled=True),
        records=(record("001Р-02", "2025-07-07", met="2025-07-21"),),
    )
    assert not events.unsettled_default
    assert events.settled_only
    assert events.sources_disagree
    verdict = verdict_for(events)
    assert verdict.basket == "attention"
    assert verdict.grounds == ("default_settled_recent",)
