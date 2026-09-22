"""Стоп-факторы ветки МСФО: объявлены методикой и проведены в расчёт по фактам.

Механизм задачи 26 существовал и **не вызывался**: перечень стоп-факторов стоял
в методике, величины, по которым они проверяются, — в `eval/ifrs_scoring_run.py`
(замер удалён 21.09.2026: он был второй системой, а не наблюдением),
а расчёт по фактам звал оценку с пустым перечнем исключённых и писал
`stop_factor_code = NULL`. В замере стоп-фактор менял исход, в документе его
не было вовсе, и отличить «ни один не сработал» от «не проверяли» было нечем.

Проверяется поток величин, а не упоминания: объявленный стоп-фактор обязан
опираться на существующий показатель, срабатывать на величине и доходить
до записанной оценки.
"""

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.metrics.ifrs import MetricValue
from finlib.normalize.ifrs_issuer_type import load_issuer_types
from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.scoring.ifrs import assess, evaluate_stop_factors

ROOT = Path(__file__).resolve().parent.parent


def value(code: str, amount: Decimal) -> MetricValue:
    """Рассчитанный показатель с величиной — по справочнику показателей ветки."""
    metric = {item.code: item for item in load_ifrs_metrics().metrics}[code]
    return MetricValue(
        code=code,
        name=metric.name,
        group=metric.group,
        in_scoring=metric.in_scoring,
        value=amount,
    )


def test_every_declared_stop_factor_names_an_existing_metric() -> None:
    """Стоп-фактор проверяется по показателю, который в справочнике есть.

    Условие, ссылающееся на величину, которой не бывает, не срабатывает
    никогда и от невыполненного неотличимо: так норма неприменимости
    ссылалась на `interest_cover`, которого в ветке нет.

    То же требование к стоп-фактору по разделу заключения, только предмет
    у него другой: раздел, которого в справочнике заключения нет, не будет
    объявлен ни у одного комплекта.
    """
    from finlib.normalize.ifrs_audit import load_audit_policy

    codes = {item.code for item in load_ifrs_metrics().metrics}
    sections = {item.code for item in load_audit_policy().sections}
    types = load_issuer_types()
    for factor in types.stop_factors:
        if factor.by_audit_section:
            assert factor.section in sections, f"{factor.code}: {factor.section}"
            continue
        assert factor.metric in codes, f"{factor.code}: {factor.metric}"
    for norm in types.not_applicable:
        if norm.when is not None:
            assert norm.when.metric in codes, f"{norm.stop_factor}: {norm.when.metric}"


def test_every_declared_cap_is_a_known_class() -> None:
    """Последствие стоп-фактора называет класс, который в методике есть."""
    classes = {item.code for item in load_ifrs_metrics().classes}
    for factor in load_issuer_types().stop_factors:
        assert factor.cap in classes, f"{factor.code}: {factor.cap}"


def test_negative_equity_lowers_the_class_to_the_lowest() -> None:
    """Отрицательный капитал опускает класс до низшего, а не понижает балл."""
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(-15008)),
        value("equity_ratio", Decimal("0.36")),
        value("net_debt_ebitda", Decimal("1.78")),
        value("ffo_to_debt", Decimal("0.36")),
        value("interest_cover_accrued", Decimal("4.38")),
        value("cur_liq", Decimal("0.81")),
    )
    stops = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert "negative_equity" in stops.triggered
    assert stops.cap == policy.classes[-1].code

    result = assess(metrics, policy, stops)
    assert result.class_code == policy.classes[-1].code
    # Класс до применения стоп-фактора сохраняется: класс E у набравшего
    # по баллу B и класс E у набравшего E — разные сведения.
    assert result.class_before_stop != result.class_code
    assert result.stop_factor_code == "negative_equity"


def test_interest_cover_below_one_caps_the_class_at_the_middle() -> None:
    """Покрытие процентов ниже единицы ограничивает класс средним."""
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(255)),
        value("equity_ratio", Decimal("0.0018")),
        value("interest_cover_accrued", Decimal("-1.99")),
        value("cur_liq", Decimal("0.45")),
    )
    stops = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert "interest_cover_below_one" in stops.triggered
    assert stops.cap == "C"


def test_cover_below_one_is_proven_by_the_sign() -> None:
    """Неположительный числитель при положительных процентах доказывает вывод.

    У ПАО «Сегежа Групп» операционный убыток 50 628 при начисленных процентах
    19 059: отношение считается и равно −2,66, но вывод от него не зависит —
    он следует из знака. Правило работает и там, где отношение не посчитано:
    доказательство не в величине.
    """
    policy = load_ifrs_metrics()
    metric = {item.code: item for item in policy.metrics}["interest_cover_accrued"]
    refused = MetricValue(
        metric.code,
        metric.name,
        metric.group,
        metric.in_scoring,
        value=None,
        numerator=Decimal(-50628),
        denominator=Decimal(19059),
    )
    stops = evaluate_stop_factors((refused,), "corporate", (), False, policy)
    assert "interest_cover_below_one" in stops.triggered
    assert stops.cap == "C"

    # Положительный числитель ничего не доказывает: там считается отношение.
    positive = MetricValue(
        metric.code,
        metric.name,
        metric.group,
        metric.in_scoring,
        value=None,
        numerator=Decimal(135605),
        denominator=Decimal(30974),
    )
    assert not evaluate_stop_factors(
        (positive,), "corporate", (), False, policy
    ).triggered


def test_negative_cover_is_printed_as_a_word() -> None:
    """Отрицательное покрытие печатается словом, а не числом.

    «−2,66» выглядит кратностью и читается как «покрыто наоборот», тогда
    как смысл один: операционной прибыли нет вовсе. Слово объявлено методикой
    вместе с основанием, и печатает его одна функция — та же, что приложение.
    """
    from finlib.metrics.ifrs_view import IfrsMetricsView

    view = IfrsMetricsView(load_ifrs_metrics())
    unit = "млн руб."
    assert (
        view.shown("interest_cover_accrued", Decimal("-2.656"), unit) == "отрицательно"
    )
    assert view.shown("interest_cover_accrued", Decimal("4.378"), unit) == "4,38"
    # У показателя, которому замена не объявлена, печатается число.
    assert view.shown("cur_liq", Decimal("-0.5"), unit) == "-0,50"


def test_inapplicable_stop_factor_does_not_lower_the_class() -> None:
    """Неприменимый стоп-фактор оценку не понижает, а оговорку называет.

    У ФосАгро оборотный капитал отрицателен, но покрытие процентов выше порога,
    при котором это объясняется возобновляемым краткосрочным финансированием.
    """
    policy = load_ifrs_metrics()
    metrics = (
        value("nwc", Decimal(-50569)),
        value("interest_cover_accrued", Decimal("4.38")),
        value("equity", Decimal(239793)),
        value("equity_ratio", Decimal("0.36")),
    )
    stops = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert "negative_nwc" not in stops.triggered
    assert "nwc" in stops.excluded
    assert stops.limitation_of("nwc")
    assert stops.cap is None


def test_going_concern_section_is_a_stop_factor_of_its_own() -> None:
    """Неопределённость непрерывности ограничивает класс, а не только журнал.

    Прежде соразмерность выходила перевёрнутой: у Сегежи аудитор объявил
    сомнение в способности продолжать деятельность, а уверенность в оценке
    стояла высшей и эскалации не возникало, тогда как у ФосАгро
    с нераскрытыми сегментами — средней и с эскалацией.
    """
    policy = load_ifrs_metrics()
    metrics = (value("equity", Decimal(255)), value("equity_ratio", Decimal("0.36")))
    declared = evaluate_stop_factors(
        metrics, "corporate", ("going_concern_uncertainty",), True, policy
    )
    assert "going_concern_uncertainty" in declared.triggered
    assert declared.cap == "D"
    silent = evaluate_stop_factors(metrics, "corporate", (), True, policy)
    assert silent.triggered == ()
    assert silent.cap is None
    # Величины у такого стоп-фактора нет, и «не рассчитана» о ней не пишется:
    # условие здесь — слова аудитора, а не пробел данных.
    check = next(
        item for item in declared.checks if item.code == "going_concern_uncertainty"
    )
    assert "величина не рассчитана" not in check.describe()
    assert "раздел заключения объявлен аудитором" in check.describe()


def test_audit_consistency_is_asked_of_the_measured_factor() -> None:
    """С заключением сверяется стоп-фактор, выведенный из величин.

    Вопрос сверки — видит ли аудитор то же, что видим мы. Спросив его
    о стоп-факторе, условием которого служит сам раздел заключения, документ
    отвечал «внешнего подтверждения нет» ровно у того обстоятельства,
    которое аудитор и объявил.
    """
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(255)),
        value("nwc", Decimal(-42662)),
        value("interest_cover_accrued", Decimal("-2.656")),
    )
    stops = evaluate_stop_factors(
        metrics, "corporate", ("going_concern_uncertainty",), True, policy
    )
    assert stops.code == "going_concern_uncertainty"
    assert stops.audit_state == "confirmed"


def test_confirmation_by_the_audit_report_is_recorded() -> None:
    """Сверка сработавшего стоп-фактора с заключением идёт вместе с ним.

    У Сегежи аудитор объявил существенную неопределённость в отношении
    непрерывности деятельности — это внешнее подтверждение стоп-фактора,
    и без него формулировки обязаны быть осторожнее.
    """
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(-15008)),
        value("interest_cover_accrued", Decimal("-1.99")),
    )
    confirmed = evaluate_stop_factors(
        metrics, "corporate", ("going_concern_uncertainty",), True, policy
    )
    assert confirmed.audit_state == "confirmed"
    alone = evaluate_stop_factors(metrics, "corporate", (), True, policy)
    assert alone.audit_state == "unconfirmed"
    unreadable = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert unreadable.audit_state == "not_readable"


def test_every_declared_factor_reports_its_outcome() -> None:
    """Исход называется у каждого стоп-фактора, включая не сработавшие.

    Класс у Сегежи выходит низшим и по баллу, и по стоп-фактору: балл 0,1 ниже
    любой границы. Без перечня проверенного прогон не показывал, сработали ли
    стоп-факторы вообще, — то есть не отвечал на вопрос, ради которого
    снимался карантин.
    """
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(255)),
        value("equity_ratio", Decimal("0.002")),
        value("nwc", Decimal(-42662)),
        value("interest_cover_accrued", Decimal("-2.656")),
    )
    stops = evaluate_stop_factors(
        metrics, "corporate", ("going_concern_uncertainty",), True, policy
    )
    assert len(stops.checks) == stops.checked == 5
    outcomes = {item.code: item.verdict for item in stops.checks}
    assert outcomes == {
        "negative_equity": "not_triggered",
        "negative_autonomy": "not_triggered",
        "negative_nwc": "triggered",
        "interest_cover_below_one": "triggered",
        # Раздел заключения объявлен аудитором — условие проверяется им,
        # а не величиной показателя.
        "going_concern_uncertainty": "triggered",
    }
    # У каждого исхода стоит величина, по которой он получен, а у сработавшего
    # ещё и ограничение класса.
    by_code = {item.code: item for item in stops.checks}
    assert by_code["negative_nwc"].value == Decimal(-42662)
    assert by_code["negative_nwc"].cap == "C"
    assert by_code["negative_equity"].value == Decimal(255)
    assert "непрерывности" in stops.audit_note


def test_checked_count_stands_next_to_the_triggered() -> None:
    """Число проверенных стоп-факторов идёт рядом с числом сработавших."""
    policy = load_ifrs_metrics()
    stops = evaluate_stop_factors(
        (value("equity", Decimal(239793)),), "corporate", (), False, policy
    )
    assert stops.checked == len(load_issuer_types().stop_factors)
    assert stops.triggered == ()
    assert "проверено" in stops.describe()


def test_assessment_cannot_be_made_without_checking_stop_factors() -> None:
    """Оценка не считается, не получив стоп-факторов: довод обязательный.

    Довод с умолчанием означал бы, что расчёт можно вызвать, не проверив
    ни одного стоп-фактора, — ровно так ветка и работала.
    """
    with pytest.raises(TypeError):
        assess((), load_ifrs_metrics())  # type: ignore[call-arg]


def test_no_production_call_assesses_with_empty_stop_factors() -> None:
    """Боевой вызов оценки не подставляет пустые стоп-факторы.

    Проверка структурная и по потоку величин: `StopFactors()` без доводов
    означает «проверено ноль», и в расчёте по фактам такого вызова быть
    не должно. Запись оценки — исключение: там довод необязателен ради
    вызовов, которым оценка уже передана.
    """
    allowed = {"src/finlib/scoring/ifrs_store.py"}
    found: list[str] = []
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "eval").rglob("*.py")]):
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "")
            if name == "assess" and len(node.args) >= 3:
                third = node.args[2]
                empty = isinstance(third, ast.Tuple) and not third.elts
                bare = (
                    isinstance(third, ast.Call)
                    and getattr(third.func, "id", "") == "StopFactors"
                    and not third.args
                    and not third.keywords
                )
                if empty or bare:
                    found.append(f"{relative}:{node.lineno}")
            if (
                name == "StopFactors"
                and not node.args
                and not node.keywords
                and relative not in allowed
            ):
                found.append(f"{relative}:{node.lineno}")
    assert not found, f"оценка без проверенных стоп-факторов: {found}"
