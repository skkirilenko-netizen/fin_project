"""Склейка продолжения с заглавной буквы по подтверждению годового комплекта.

Решение владельца 29.09.2026, третий путь. У промежуточного ФосАгро
себестоимость набрана двумя строками — «Себестоимость реализованной
продукции» без величин и «Группы» с величинами, — и 194 587 стояли статьёй
«Группы». Правило по строению строки либо не ловит ФосАгро, либо ломает
«Выручку» над разбивкой у Норникеля; подтверждение человека различает их.
"""

from datetime import date

from finlib.db import execute, fetch_one
from finlib.sources.ifrs_confirmed import glue_keys, match_key
from finlib.sources.ifrs_extract import _join

COST = "Себестоимость реализованной продукции"
FORM = "ifrs.statement_of_profit_or_loss"


def test_a_confirmed_name_glues_a_capitalised_continuation() -> None:
    """Склеенное подтверждено — строки склеиваются, и это отмечено."""
    keys = frozenset({match_key(f"{COST} Группы")})
    assert _join([COST], "Группы", keys) == (f"{COST} Группы", True)


def test_without_a_confirmation_the_heading_stays_a_heading() -> None:
    """Без подтверждения — как прежде: «Выручка» над разбивкой не липнет."""
    assert _join(["Выручка"], "Металлы", frozenset()) == ("Металлы", False)
    assert _join([COST], "Группы", frozenset()) == ("Группы", False)
    # Подтверждено что-то другое — склейки тоже нет.
    other = frozenset({match_key("Выручка Металлы и прочее")})
    assert _join(["Выручка"], "Металлы", other) == ("Металлы", False)


def test_a_confirmed_remainder_is_not_glued() -> None:
    """Остаток подтверждён сам по себе — значит, это своя статья, а не обрывок."""
    keys = frozenset({match_key(f"{COST} Группы"), match_key("Группы")})
    assert _join([COST], "Группы", keys) == ("Группы", False)


def test_ordinary_continuation_is_unchanged() -> None:
    """Продолжение со строчной склеивается по строению, а не по подтверждению."""
    joined = _join(["Авансы, выданные под строительство и"], "приобретение ОС", frozenset())
    assert joined == ("Авансы, выданные под строительство и приобретение ОС", False)


def _set(conn, inn: str, kind: str, period_end: date) -> int:  # noqa: ANN001
    """Комплект документа названного вида."""
    row = fetch_one(
        "INSERT INTO src_file (inn, standard, report_year, period_end, source, "
        "form_codes, correction_version, is_actual, reporting_type, reporting_kind, "
        "unit_code, unit_source, status) VALUES (%(i)s, 'ifrs', %(y)s, %(p)s, "
        "'file', '{}', 0, true, 'full', %(k)s, '385', 'explicit', 'loaded') "
        "RETURNING id",
        {"i": inn, "y": period_end.year, "p": period_end, "k": kind},
        conn=conn,
    )
    assert row is not None
    return int(row["id"])


def test_only_annual_confirmations_glue(db_conn) -> None:  # noqa: ANN001
    """Склейку разрешает подтверждённое на годовом комплекте, а не на промежуточном."""
    inn = "7736050003"
    execute("DELETE FROM src_file WHERE inn = %(i)s", {"i": inn}, conn=db_conn)
    execute("DELETE FROM ifrs_line_confirmation WHERE inn = %(i)s", {"i": inn}, conn=db_conn)
    execute(
        "INSERT INTO organization (inn) VALUES (%(i)s) ON CONFLICT DO NOTHING",
        {"i": inn},
        conn=db_conn,
    )
    _set(db_conn, inn, "full", date(2025, 12, 31))
    _set(db_conn, inn, "interim", date(2026, 6, 30))
    # Комплект узнаётся по отчётной дате подтверждения: `src_file_id`
    # у подтверждений не заполняется.
    for moment, name in (
        (date(2025, 12, 31), f"{COST} Группы"),
        (date(2026, 6, 30), "Прочие доходы Группы"),
        (date(2024, 12, 31), "Прочие расходы Группы"),
    ):
        execute(
            "INSERT INTO ifrs_line_confirmation (code, inn, report_date, "
            "source_name, form_code, confirmed_by) VALUES ('ifrs.cost_of_sales', "
            "%(i)s, %(d)s, %(n)s, %(f)s, 'аналитик')",
            {"i": inn, "d": moment, "n": name, "f": FORM},
            conn=db_conn,
        )
    keys = glue_keys(inn, conn=db_conn)
    assert keys == {FORM: frozenset({match_key(f"{COST} Группы")})}
