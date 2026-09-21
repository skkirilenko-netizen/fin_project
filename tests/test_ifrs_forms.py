"""Величина позиции берётся из формы, объявленной у позиции.

Один код правомерно стоит в двух формах МСФО: неденежные корректировки отчёта
о движении денежных средств повторяют статьи отчёта о прибыли. В базе это два
разных факта — ключ `fact_report` форму содержит, — а читающий по коду без
формы получал то из двух, что пришло позже. У ПАО «Сегежа Групп» налог
на прибыль равен −4 784 в отчёте о прибыли и +4 784 в потоке: величины
различаются знаком, и выбор зависел от порядка строк в ответе базы.
"""

from decimal import Decimal

from finlib.normalize.ifrs_forms import own_form, pick_by_form
from finlib.standards import Standard

PROFIT = "ifrs.statement_of_profit_or_loss"
FLOWS = "ifrs.statement_of_cash_flows"


def row(code: str, form: str, value: str) -> dict:
    """Строка фактов в том виде, в каком её отдаёт база."""
    return {"line_code": code, "form_code": form, "value": Decimal(value)}


def test_declared_form_wins_over_the_other() -> None:
    """При двух формах берётся та, что объявлена у позиции, — и всегда она."""
    rows = [row("ifrs.income_tax", FLOWS, "4784"), row("ifrs.income_tax", PROFIT, "-4784")]
    chosen, foreign = pick_by_form(rows, Standard.IFRS)
    assert [item["value"] for item in chosen] == [Decimal("-4784")]
    assert foreign == ()
    # Порядок строк в ответе базы не определён, и выбор от него не зависит.
    chosen_again, _ = pick_by_form(list(reversed(rows)), Standard.IFRS)
    assert [item["value"] for item in chosen_again] == [Decimal("-4784")]


def test_value_from_a_foreign_form_is_taken_and_named() -> None:
    """Единственная величина берётся и тогда, когда форма чужая, — но называется.

    Отбросить её было бы хуже: у эмитента, раскрывшего амортизацию в отчёте
    о прибыли, а не корректировкой потока, EBITDA не посчиталась бы вовсе,
    а присвоение, сделанное человеком, перестало бы работать молча.
    """
    chosen, foreign = pick_by_form(
        [row("ifrs.depreciation", PROFIT, "40000")], Standard.IFRS
    )
    assert [item["value"] for item in chosen] == [Decimal(40000)]
    assert foreign == ("ifrs.depreciation",)


def test_unknown_code_is_not_judged() -> None:
    """Код, которого в справочнике нет, считается своим.

    Специфическая статья с кодом, присвоенным человеком, — норма ветки,
    и отбросить её значило бы потерять факт из-за неполноты справочника.
    """
    assert own_form("ifrs.assigned_by_a_human", FLOWS, Standard.IFRS)
    chosen, foreign = pick_by_form(
        [row("ifrs.assigned_by_a_human", FLOWS, "12")], Standard.IFRS
    )
    assert len(chosen) == 1
    assert foreign == ()


def test_rsbu_is_not_asked_about_forms() -> None:
    """У РСБУ код принадлежит одной форме по устройству нумерации."""
    rows = [row("1600", "0710001", "100")]
    chosen, foreign = pick_by_form(rows, Standard.RSBU)
    assert chosen == rows
    assert foreign == ()
