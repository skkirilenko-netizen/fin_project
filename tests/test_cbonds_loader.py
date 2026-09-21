"""Загрузка комплекта из нормализованных данных агрегатора.

Здесь проверяется не арифметика — она общая, — а **вход и приоритет**: чем
становится строка источника, что с ней не проходит, и чья величина остаётся
в базе, когда та же позиция уже пришла из документа.

Строки источника здесь синтетические и собраны по образцу настоящего ответа:
настоящие лежат в `data/raw/cbonds/`, в репозиторий не коммитятся, и тест,
опирающийся на них, у другого разработчика не запустится.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.db import execute, fetch_all, fetch_one
from finlib.normalize.cbonds_loader import load_row
from finlib.normalize.cbonds_mapping import load_cbonds_mapping
from finlib.quality.codes import CheckCode

INN = "7736050003"
YEAR = 2025

# Строка сходится сама с собой: актив равен пассиву, разделы дают итог,
# долг равен сумме срочностей. Величины — порядка настоящих.
ROW: dict[str, object] = {
    "id": "1",
    "emitent_inn": INN,
    "emitent_name_rus": "Проба",
    "date": "2025-12-31",
    "ln104": "RUB",
    "ln105": "1000000",
    "ln102": "МСФО(к)",
    "ln3": "14681",
    "ln4": "65070",
    "ln6": "217976",
    "ln10": "453959",
    "ln11": "671935",
    "ln14": "209394",
    "ln17": "119062",
    "ln19": "163597",
    "ln20": "239793",
    "ln21": "671935",
    "ln23": "573628",
    "ln26": "114205",
    "ln34": "328456",
    "ln36": "211480",
    "ln37": "170768",
    "ln38": "268545",
    "ln79": "40712",
    "update_time": "2026-09-16T09:23:54",
}


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute(
        "DELETE FROM src_file WHERE inn = %(i)s AND standard = 'ifrs'",
        {"i": INN},
        conn=db_conn,
    )
    return db_conn


def facts_of(db_conn) -> dict[str, dict]:
    """Факты комплекта по кодам позиций."""
    return {
        row["line_code"]: row
        for row in fetch_all(
            "SELECT line_code, form_code, value, recognition, source_line_code, "
            "period_role FROM fact_report WHERE inn = %(i)s AND standard = 'ifrs' "
            "AND report_date = %(d)s",
            {"i": INN, "d": date(YEAR, 12, 31)},
            conn=db_conn,
        )
    }


def test_row_becomes_a_set_with_its_own_source_and_recognition(db_conn) -> None:
    """Строка источника становится комплектом со своим способом и опознанием.

    Стандарт остаётся `ifrs` — это та же отчётность, — а способ получения
    и сила опознания свои: величины нормализованы агрегатором, а не прочитаны
    из отчётности, и доверие к ним третье.
    """
    outcome = load_row(ROW, db_conn)
    assert outcome.accepted and not outcome.quarantined
    # Фактов столько, сколько полей строки нашлось: пустое поле фактом
    # не становится, и нуля вместо него не подставляется.
    assert outcome.facts == 15

    row = fetch_one(
        "SELECT source, standard, unit_code, status, meta FROM src_file "
        "WHERE inn = %(i)s AND standard = 'ifrs' AND report_year = %(y)s",
        {"i": INN, "y": YEAR},
        conn=db_conn,
    )
    assert row["source"] == "cbonds"
    assert row["standard"] == "ifrs"
    # Единица объявлена источником построчно: миллионы — код ОКЕИ 385.
    assert row["unit_code"] == "385"
    assert row["status"] == "loaded"
    # Величины, посчитанные самим агрегатором, хранятся с комплектом, но
    # фактами не становятся: состав у них его, а не наш.
    assert row["meta"]["cbonds"]["reported"]["ebitda"] == "211480"

    facts = facts_of(db_conn)
    assert facts["ifrs.total_assets"]["value"] == Decimal(671935)
    assert {item["recognition"] for item in facts.values()} == {"cbonds"}
    # Поле источника сохраняется рядом с кодом позиции: по нему проверяется
    # сопоставление, и `NULL` в этой графе не используется.
    assert facts["ifrs.total_assets"]["source_line_code"] == "ln11"
    # Агрегат, который методика не грузит, фактом не стал.
    assert "ifrs.intangible_assets" not in facts
    # Величины агрегатора, посчитанные им самим, тоже не факты.
    assert "ifrs.ebitda" not in facts


def test_document_value_is_not_overwritten_by_the_aggregator(db_conn) -> None:
    """Величина первоисточника величиной агрегатора не затирается.

    У ГК «Автодор» операционная прибыль 2024 года равна 2 272 по сравнительной
    графе отчёта за 2025 год и 448 по строке агрегатора: первое — последняя
    редакция эмитента, второе — прочтение отчёта того года. Без правила
    приоритета величина агрегатора приходит отчётной и затирала бы
    пересмотренную сравнительную.
    """
    # Величина документа: сила опознания справочника и роль сравнительная —
    # худшая из возможных, и всё равно она старше агрегатора.
    execute(
        "INSERT INTO src_file (inn, standard, report_year, source, reporting_type, "
        "unit_code, unit_source, status) VALUES (%(i)s, 'ifrs', %(y)s, 'file', "
        "'full', '385', 'explicit', 'loaded') RETURNING id",
        {"i": INN, "y": YEAR},
        conn=db_conn,
    )
    document = fetch_one(
        "SELECT id FROM src_file WHERE inn = %(i)s AND standard = 'ifrs' "
        "AND source = 'file'",
        {"i": INN},
        conn=db_conn,
    )
    execute(
        "INSERT INTO fact_report (src_file_id, inn, standard, report_date, "
        "form_code, line_code, source_line_code, value, value_status, "
        "period_role, recognition) VALUES (%(s)s, %(i)s, 'ifrs', %(d)s, "
        "'ifrs.statement_of_profit_or_loss', 'ifrs.operating_profit', "
        "'ifrs.operating_profit', 2272, 'ok', 'previous', 'catalog')",
        {"s": document["id"], "i": INN, "d": date(YEAR, 12, 31)},
        conn=db_conn,
    )

    outcome = load_row({**ROW, "ln37": "448"}, db_conn)
    assert outcome.accepted
    facts = facts_of(db_conn)
    assert facts["ifrs.operating_profit"]["value"] == Decimal(2272)
    assert facts["ifrs.operating_profit"]["recognition"] == "catalog"
    # Расхождение не замалчивается: оно содержательный сигнал о пересмотре.
    assert any("operating_profit" in item for item in outcome.mismatches)
    journal = fetch_all(
        "SELECT check_code, message FROM dq_log WHERE inn = %(i)s "
        "AND check_code = %(c)s",
        {"i": INN, "c": CheckCode.CBONDS_VALUE_MISMATCH.value},
        conn=db_conn,
    )
    assert journal and "448" in journal[0]["message"]


def test_zero_that_breaks_the_identity_sends_the_set_to_quarantine(db_conn) -> None:
    """Ноль, ломающий тождество отчётности, ставит комплект в карантин.

    Ноль у агрегатора не означает нуля: источник пишет ноль и там, где
    величина не раскрыта. Стоп-фактор по такому капиталу был бы утверждением
    об эмитенте, сделанным по нераскрытой величине.
    """
    broken = {**ROW, "ln20": "0"}
    outcome = load_row(broken, db_conn)
    assert outcome.accepted, "строка комплектом стала: причина в данных, а не в приёме"
    assert outcome.quarantined
    codes = {code for code, _ in outcome.failures}
    assert CheckCode.CBONDS_ZERO_TOTAL.value in codes
    row = fetch_one(
        "SELECT status FROM src_file WHERE inn = %(i)s AND standard = 'ifrs' "
        "AND source = 'cbonds'",
        {"i": INN},
        conn=db_conn,
    )
    assert row["status"] == "quarantine"


def test_passed_checks_reach_the_journal_with_their_denominator(db_conn) -> None:
    """Пройденная проверка идёт в журнал наравне со сработавшей.

    Иначе сводка комплекта, прошедшего чисто, не содержит ни одной записи,
    и по документу нельзя сказать, что именно проверено: ноль срабатываний
    неотличим от невыполненного контроля.
    """
    load_row(ROW, db_conn)
    rows = fetch_all(
        "SELECT check_code, status, message FROM dq_log WHERE inn = %(i)s "
        "ORDER BY check_code",
        {"i": INN},
        conn=db_conn,
    )
    by_code = {item["check_code"]: item for item in rows}
    for code in (
        CheckCode.CBONDS_IDENTITY_MISMATCH,
        CheckCode.CBONDS_SECTIONS_MISMATCH,
        CheckCode.CBONDS_ZERO_TOTAL,
        CheckCode.CBONDS_DEBT_SPLIT_MISMATCH,
    ):
        assert by_code[code.value]["status"] == "pass", code
    summary = by_code[CheckCode.CBONDS_FIELD_MAPPING.value]["message"]
    assert "полей справочника" in summary and "величин записано" in summary


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"ln104": "USD"}, "валюта"),
        ({"ln105": None}, "единица измерения"),
        ({"ln105": "0"}, "единица измерения"),
        ({"ln102": "МСФО"}, "неконсолидированная"),
        ({"ln102": "РСБУ"}, "стандарт отчётности не опознан"),
        ({"date": "2025-06-30"}, "не годовой"),
    ],
)
def test_unfit_row_does_not_become_a_set(db_conn, change: dict, reason: str) -> None:
    """Строка, не пригодная к загрузке, комплектом не становится вовсе.

    Причина называется словами и кодом контроля: строка без комплекта молчать
    не вправе — иначе её пропажа неотличима от того, что её не было.

    **«МСФО» и «МСФО(к)» не объединяются**: неконсолидированная отчётность
    относится к отдельному юридическому лицу, и сравнение идёт строкой целиком,
    потому что «МСФО» входит в «МСФО(к)» подстрокой.
    """
    outcome = load_row({**ROW, **change}, db_conn)
    assert not outcome.accepted
    assert reason in outcome.rejection.reason
    assert not fetch_all(
        "SELECT 1 FROM src_file WHERE inn = %(i)s AND standard = 'ifrs' "
        "AND source = 'cbonds'",
        {"i": INN},
        conn=db_conn,
    )
    journal = fetch_all(
        "SELECT check_code FROM dq_log WHERE inn = %(i)s AND check_code = %(c)s",
        {"i": INN, "c": CheckCode.CBONDS_SET_REJECTED.value},
        conn=db_conn,
    )
    assert journal, "отказ приёма не попал в журнал"


def test_mapping_declares_the_kind_of_every_field() -> None:
    """У каждого поля объявлен род, а у агрегата — состав и наблюдение.

    Поле шире нашей позиции — решение о данных, и оно обязано быть видно
    в справочнике: у Черкизово нематериальные активы агрегатора включают
    гудвил, и без объявления это выглядело бы ошибкой разбора.
    """
    report = load_cbonds_mapping().report("report_msfo_real")
    assert report.fields
    for name, item in report.fields.items():
        assert item.kind in ("exact", "aggregate", "control"), name
        if item.kind == "aggregate":
            assert item.covers and item.seen_at
    # Агрегат, который всё-таки грузится, — один, и он назван: без него
    # не считается долг, то есть величина маршрута.
    loaded = [
        name
        for name, item in report.fields.items()
        if item.kind == "aggregate" and item.loaded
    ]
    assert loaded == ["ln14"]
