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
from finlib.sources.ifrs_extract import Glue, _join

COST = "Себестоимость реализованной продукции"
FORM = "ifrs.statement_of_profit_or_loss"


def test_a_confirmed_name_glues_a_capitalised_continuation() -> None:
    """Склеенное подтверждено — строки склеиваются, и это отмечено."""
    keys = frozenset({match_key("Выручка Металлы")})
    assert _join(["Выручка"], "Металлы", keys) == ("Выручка Металлы", Glue.CONFIRMATION)


def test_without_a_confirmation_the_heading_stays_a_heading() -> None:
    """Без подтверждения — как прежде: «Выручка» над разбивкой не липнет."""
    assert _join(["Выручка"], "Металлы", frozenset()) == ("Металлы", Glue.NONE)
    # Подтверждено что-то другое — склейки тоже нет.
    other = frozenset({match_key("Выручка Металлы и прочее")})
    assert _join(["Выручка"], "Металлы", other) == ("Металлы", Glue.NONE)


def test_a_defined_term_ends_a_carried_name() -> None:
    """Строка из одного определённого термина — окончание переноса, не заголовок.

    ФосАгро 6м2026: «Повторная выплата ранее возвращенных дивидендов
    акционерам» / «Компании (2 017) (81)» — строка шла статьёй «Компании»;
    подтверждения годового комплекта у неё нет, и путь 7б её не склеивал.
    Склеивает строение: «Компания» и «Группа» пишутся с заглавной как
    определённые термины. Склейка по термину подтверждением не считается и считается своим числом.
    """
    head = "Повторная выплата ранее возвращенных дивидендов акционерам"
    assert _join([head], "Компании", frozenset()) == (f"{head} Компании", Glue.TERM)
    assert _join([COST], "Группы", frozenset()) == (f"{COST} Группы", Glue.TERM)
    # Термин склеивается только целой строкой: «Группы компаний» — начало
    # своего наименования, а не окончание чужого.
    assert _join(["Выручка"], "Группы компаний", frozenset()) == ("Группы компаний", Glue.NONE)


def test_a_confirmed_remainder_is_not_glued() -> None:
    """Остаток подтверждён сам по себе — значит, это своя статья, а не обрывок."""
    keys = frozenset({match_key(f"{COST} Группы"), match_key("Группы")})
    assert _join([COST], "Группы", keys) == ("Группы", Glue.NONE)


def test_a_footnote_mark_is_cut_from_the_name() -> None:
    """Знак сноски, прилипший к последнему слову, не часть наименования."""
    from finlib.sources.ifrs_extract import cut_footnote_mark, row_name

    name = "Уменьшение торговой и прочей дебиторской задолженности"
    assert cut_footnote_mark(f"{name}1") == name
    assert cut_footnote_mark("Возврат дивидендов2") == "Возврат дивидендов"
    # Число отделено пробелом либо стоит за заглавной — это не сноска.
    assert cut_footnote_mark("Облигации серии БО-П01") == "Облигации серии БО-П01"
    assert cut_footnote_mark("Облигации серии 1") == "Облигации серии 1"
    # Трёхзначный хвост — не знак сноски, а часть наименования.
    assert cut_footnote_mark("Код строки абв123") == "Код строки абв123"
    # Тем же правилом читается ключ подтверждения: одно определение имени.
    assert row_name(f"{name}1  7,047 28,142") == name


def test_ordinary_continuation_is_unchanged() -> None:
    """Продолжение со строчной склеивается по строению, а не по подтверждению."""
    joined = _join(["Авансы, выданные под строительство и"], "приобретение ОС", frozenset())
    assert joined == ("Авансы, выданные под строительство и приобретение ОС", Glue.NONE)


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
