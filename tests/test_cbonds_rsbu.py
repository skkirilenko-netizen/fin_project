"""Доставка отчётности РСБУ от агрегатора: правило полей, знак, единица, допуск.

Проверяется не арифметика — она общая с ветвью МСФО, — а **объявленные
различия**: состав полей задан правилом, а не перечнем; знак расходной статьи
приводится к величине расхода; единица берётся правилом форм; допуск сверки
взят из методики контролей. Каждое из четырёх — решение о данных, и молча
изменённое, оно не ловится ни одним контролем сходимости.

Строки источника синтетические и собраны по образцу настоящего ответа:
настоящие лежат в `data/raw/cbonds/` и в репозиторий не коммитятся.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.db import execute, fetch_all
from finlib.normalize.cbonds_loader import load_row, resolve_fields
from finlib.normalize.cbonds_mapping import load_cbonds_mapping
from finlib.quality.codes import CheckCode

INN = "7736050003"
REPORT = "report_rsbu"

# Баланс сходится, разделы дают итоги, расходные статьи приходят отрицательными —
# так их и печатает агрегатор.
ROW: dict[str, object] = {
    "id": "1",
    "emitent_inn": INN,
    "emitent_name_rus": "Проба",
    "date": "2025-12-31",
    "ln1100": "600",
    "ln1200": "400",
    "ln1600": "1000",
    "ln1300": "300",
    "ln1400": "500",
    "ln1500": "200",
    "ln1700": "1000",
    "ln2110": "900",
    "ln2120": "-700",
    "ln2400": "120",
    "ln4111": "50",
    "update_time": "2026-09-22T09:00:00",
}


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute(
        "DELETE FROM src_file WHERE inn = %(i)s AND standard = 'rsbu'",
        {"i": INN},
        conn=db_conn,
    )
    return db_conn


def facts_of(db_conn) -> dict[str, dict]:
    """Факты комплекта по кодам строк."""
    return {
        row["line_code"]: row
        for row in fetch_all(
            "SELECT line_code, form_code, value, recognition, source_line_code "
            "FROM fact_report WHERE inn = %(i)s AND standard = 'rsbu' "
            "AND report_date = %(d)s",
            {"i": INN, "d": date(2025, 12, 31)},
            conn=db_conn,
        )
    }


def test_field_names_are_line_codes_and_the_catalog_decides() -> None:
    """Поле `ln<код>` означает строку, а форму и состав решает справочник.

    Перечень полей здесь был бы вторым экземпляром справочника строк:
    расхождение двух перечней — вопрос времени. Код, которого справочник
    не несёт, не грузится и считается отдельно.
    """
    report = load_cbonds_mapping().report(REPORT)
    fields, outside = resolve_fields(ROW, report)
    assert fields["ln1600"].code == "1600"
    assert fields["ln1600"].form_code == "0710001"
    assert fields["ln2110"].form_code == "0710002"
    # Детализация отчёта о движении денежных средств методикой не заведена.
    assert "ln4111" not in fields
    assert outside == 1


def test_expense_is_stored_as_the_amount_of_expense(db_conn) -> None:
    """Расходная статья приходит отрицательной, а хранится величиной расхода.

    Знак задаёт оператор в составе итога, и правило объявлено методикой
    вместе с замером: у строк с `in_brackets` знак агрегатора обратен нашему
    в 280 случаях против 29.
    """
    load_row(ROW, db_conn, report_name=REPORT)
    facts = facts_of(db_conn)
    assert facts["2120"]["value"] == Decimal("700.000")
    assert facts["2110"]["value"] == Decimal("900.000")


def test_the_set_is_written_as_rsbu_with_the_unit_of_its_forms(db_conn) -> None:
    """Стандарт берётся у вида отчёта, единица — правилом форм.

    Единица не объявляется здесь вторым местом: правило «форма задаёт
    единицу» живёт в `lines.yaml`, и второй экземпляр той же величины
    однажды разошёлся бы с первым.
    """
    outcome = load_row(ROW, db_conn, report_name=REPORT)
    row = fetch_all(
        "SELECT standard, unit_code, unit_source, reporting_type, form_codes "
        "FROM src_file WHERE id = %(id)s",
        {"id": outcome.src_file_id},
        conn=db_conn,
    )[0]
    assert row["standard"] == "rsbu"
    assert row["unit_code"] == "384"
    assert row["unit_source"] == "form_standard"
    assert row["reporting_type"] == "full"
    assert set(row["form_codes"]) == {"0710001", "0710002"}


def test_rounding_tolerance_comes_from_the_methodology(db_conn) -> None:
    """Расхождение суммы разделов на единицу — округление, а не дефект.

    Допуск взят из `thresholds.yaml`, блок `rounding`: тот же, которым
    пользуются контроли сходимости РСБУ. Точное равенство отправляло
    в карантин восемь организаций набора за округление составителя.
    """
    rounded = ROW | {"ln1200": "399"}
    outcome = load_row(rounded, db_conn, report_name=REPORT)
    assert not outcome.quarantined
    broken = ROW | {"ln1200": "395"}
    outcome = load_row(broken, db_conn, report_name=REPORT)
    assert outcome.quarantined
    assert any(
        code == CheckCode.CBONDS_SECTIONS_MISMATCH.value
        for code, _ in outcome.failures
    )


def test_zero_does_not_overwrite_a_non_disclosure(db_conn) -> None:
    """Ноль агрегатора поверх нераскрытой строки не пишется.

    Ровно этот ноль у агрегатора и означает нераскрытие, а запись утверждала бы
    раскрытый ноль. Ненулевая величина предъявляется: она сведение, которого
    у нас не было, а решает о ней правило приоритета.
    """
    execute(
        "INSERT INTO organization (inn, name) VALUES (%(i)s, 'Проба') "
        "ON CONFLICT (inn) DO NOTHING",
        {"i": INN},
        conn=db_conn,
    )
    first = fetch_all(
        "INSERT INTO src_file (inn, standard, report_year, source, reporting_type, "
        "unit_code, unit_source, status) VALUES (%(i)s, 'rsbu', 2025, 'file', "
        "'full', '384', 'form_standard', 'loaded') RETURNING id",
        {"i": INN},
        conn=db_conn,
    )[0]
    execute(
        "INSERT INTO fact_report (src_file_id, inn, standard, report_date, "
        "form_code, line_code, source_line_code, value, value_status, "
        "period_role, recognition) VALUES (%(f)s, %(i)s, 'rsbu', %(d)s, "
        "'0710001', '1300', '1300', NULL, 'not_disclosed', 'current', 'catalog')",
        {"f": first["id"], "i": INN, "d": date(2025, 12, 31)},
        conn=db_conn,
    )
    outcome = load_row(ROW | {"ln1300": "0", "ln1700": "700"}, db_conn, report_name=REPORT)
    facts = facts_of(db_conn)
    assert facts["1300"]["value"] is None
    assert "1300" in outcome.zeros_for_undisclosed


def test_declaring_a_thing_twice_is_refused() -> None:
    """Валюта и единица объявляются одним способом: двумя — они разойдутся."""
    mapping = load_cbonds_mapping().model_dump()
    rsbu = mapping["reports"][REPORT]
    from finlib.normalize.cbonds_mapping import CbondsMapping

    both = dict(mapping)
    both["reports"] = dict(mapping["reports"])
    both["reports"][REPORT] = rsbu | {"currency_field": "ln104"}
    with pytest.raises(ValueError, match="валюта"):
        CbondsMapping.model_validate(both)
    neither = dict(mapping)
    neither["reports"] = dict(mapping["reports"])
    neither["reports"][REPORT] = rsbu | {"unit_from": None}
    with pytest.raises(ValueError, match="единица"):
        CbondsMapping.model_validate(neither)


def test_zero_makes_the_total_unverifiable_not_failed(db_conn) -> None:
    """Ноль среди слагаемых даёт «не проверяем», а не провал.

    Ноль у агрегатора означает и нераскрытие, и слагаемое, о котором это
    неизвестно, нельзя ни складывать, ни считать раскрытым. Прежде такой
    состав объявлялся расхождением: из 534 провалов по комплектам агрегатора
    234 имели среди слагаемых нули — то есть эмитенту приписывалось то, чего
    в его отчётности нет.
    """
    row = ROW | {"ln1200": "0", "ln1600": "1000"}
    outcome = load_row(row, db_conn, report_name=REPORT)
    assert not outcome.quarantined
    assert any(
        code == CheckCode.CBONDS_SECTIONS_MISMATCH.value
        for code, _ in outcome.unchecked
    )
    assert not outcome.failures


def test_zero_total_with_activity_is_still_blocking(db_conn) -> None:
    """Ноль итога при ненулевой деятельности смягчению не подлежит.

    Это и есть признак нераскрытия, и он блокирующий: стоп-фактор по такому
    капиталу был бы утверждением об эмитенте, сделанным по нераскрытой
    величине.
    """
    row = ROW | {"ln1300": "0"}
    outcome = load_row(row, db_conn, report_name=REPORT)
    assert outcome.quarantined
    assert any(
        code == CheckCode.CBONDS_ZERO_TOTAL.value for code, _ in outcome.failures
    )


def test_controls_read_the_aggregator_zeros_as_not_disclosed(db_conn) -> None:
    """Контроли сходимости читают ноль доставки так же, как загрузчик.

    Правило объявлено у вида отчёта, а применяется в двух местах — в проверках
    загрузчика и в контролях качества. Второе место важнее: именно контроли
    ставят карантин, и без правила комплект уходил в него за ноль, значение
    которого неизвестно.
    """
    from finlib.quality.checks import section_sum
    from finlib.quality.context import build_context

    outcome = load_row(ROW | {"ln1240": "0"}, db_conn, report_name=REPORT)
    context = build_context(outcome.src_file_id, db_conn)
    found = [item for item in section_sum(context) if item.line_code == "1200"]
    assert found and all(item.status.value != "fail" for item in found)
