"""Документ говорит о своём комплекте и одним голосом.

Два дефекта, найденных чтением первого заключения по МСФО глазами.

**Чужой комплект.** «Ключевой вывод» по годовой отчётности ФосАгро перечислял
четыре отказа блокирующих контролей — все четыре относились к промежуточному
комплекту, — а первым вопросом к организации стояло «чем объясняются
расхождения в комплекте за 2026 год». Комплект промежуточный, к заключению
отношения не имеет, и условия экрана сверки в нём наши.

**Два голоса об одном показателе.** «Ограничения анализа» сообщали, что
величина в отчётности есть и извлечение за нами, а «Вопросы к организации»
тут же просили у организации расшифровки того же показателя.
"""

from datetime import date
from decimal import Decimal

from finlib.metrics.definitions import load_metrics
from finlib.quality.refusals import Kind
from finlib.report.composition import questions
from finlib.report.data import MetricRow, ReportData
from finlib.report.policy import load_policy
from finlib.scoring.definitions import load_scoring
from finlib.standards import Standard

NOW = date(2025, 12, 31)


def row(code: str, reason_code: str) -> MetricRow:
    """Нерассчитанный показатель с машинной причиной отказа."""
    return MetricRow(
        code=code,
        name="Покрытие погашений ближайших 12 месяцев денежными средствами",
        unit="ratio",
        group_name="Обслуживание долга",
        values={NOW: None},
        reasons={NOW: "величина пока не извлекается"},
        reason_codes={NOW: reason_code},
        included=False,
        score=None,
        level_score=None,
        dynamics_score=None,
        exclusion_reason="не рассчитан",
        exclusion_kind="no_data",
    )


def data_with(metrics: list[MetricRow], checks: list[dict]) -> ReportData:
    """Минимальный набор данных документа: показатели и журнал контролей."""
    return ReportData(
        inn="7736216869",
        report_date=NOW,
        standard=Standard.IFRS,
        organization={"inn": "7736216869", "meta": None},
        unit_name="млн руб.",
        assessment=None,
        metrics=metrics,
        checks=checks,
    )


def test_our_gap_does_not_become_a_question() -> None:
    """Из нашего пробела вопрос к организации не следует.

    Семейство отказа объявлено методикой: из `data_missing` следует запрос,
    из `our_gap` — прямое «запрашивать нечего». Документ обязан говорить
    о показателе одним голосом.
    """
    gap = row("debt_maturity_cover", "not_extracted_yet")
    assert gap.refusal_kind(NOW) is Kind.OUR_GAP

    found = questions(
        data_with([gap], []), load_policy(), load_metrics(), load_scoring(), []
    )
    assert all("Покрытие погашений" not in item for item in found)


def test_missing_disclosure_still_becomes_a_question() -> None:
    """Нераскрытая величина вопросом остаётся: это пробел отчётности."""
    missing = row("interest_cover_accrued", "missing_input")
    assert missing.refusal_kind(NOW) is Kind.DATA_MISSING

    found = questions(
        data_with([missing], []), load_policy(), load_metrics(), load_scoring(), []
    )
    assert any("Покрытие погашений" in item for item in found)


def test_blocking_failures_belong_to_the_documents_set() -> None:
    """Отказы контролей чужого комплекта в «Ключевой вывод» не идут."""
    own = {
        "check_code": "ifrs_total_mismatch",
        "severity": "blocking",
        "status": "fail",
        "report_date": NOW,
        "report_year": NOW.year,
        "runs": 1,
        "line_codes": [],
    }
    foreign = {**own, "check_code": "reporting_kind", "report_year": NOW.year + 1}
    data = data_with([], [own, foreign])
    assert [item["check_code"] for item in data.blocking_failures] == [
        "ifrs_total_mismatch"
    ]


def test_quarantine_of_another_period_is_not_a_question() -> None:
    """Отбракованный комплект другого периода вопросом не становится.

    Он назван в «Ограничениях анализа» и в приложении — замалчивания нет,
    а спрашивать организацию о чужом периоде в заключении по этому периоду
    не о чем.
    """
    from finlib.report.document import _question_texts

    data = data_with([], [])
    data.sources = [
        {"report_year": NOW.year + 1, "status": "quarantine"},
        {"report_year": NOW.year, "status": "loaded"},
    ]
    found = _question_texts(data)
    assert all(str(NOW.year + 1) not in item for item in found)


def test_quarantine_of_the_documents_period_is_a_question() -> None:
    """Отбракованный комплект **этого** периода спрашивается как прежде."""
    from finlib.report.document import _question_texts

    data = data_with([], [])
    data.sources = [{"report_year": NOW.year, "status": "quarantine"}]
    found = _question_texts(data)
    assert any(str(NOW.year) in item for item in found)


def test_metric_values_are_untouched_by_the_scope_rule() -> None:
    """Правило касается вопросов и «Ключевого вывода», а не самих величин."""
    kept = row("cur_liq", "missing_input")
    kept.values[NOW] = Decimal("0.812")
    assert kept.values[NOW] == Decimal("0.812")
