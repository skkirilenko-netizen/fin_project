"""Тесты унифицированной модели статей МСФО (задача 21).

Справочник параллельный справочнику РСБУ: кодов строк, утверждённых
нормативным актом, в МСФО нет, позиция опознаётся наименованием, а состав
статей меняется от эмитента к эмитенту.
"""

import re

import pytest
import yaml
from pydantic import ValidationError

from finlib.normalize.ifrs_lines import (
    CODE_PATTERN,
    IfrsCatalog,
    default_path,
    load_ifrs_lines,
)
from finlib.normalize.lines import Operator, load_lines

CATALOG = load_ifrs_lines()


def raw() -> dict:
    """Справочник в исходном виде."""
    return yaml.safe_load(default_path().read_text(encoding="utf-8"))


# --- формат кодов -------------------------------------------------------------


def test_every_code_carries_the_prefix() -> None:
    """Код позиции начинается с `ifrs.` — и это не украшение.

    Четырёхзначное число в грамматике формул уже означает код строки РСБУ,
    а голое строчное имя — константу методики из thresholds.yaml. Код
    с точкой не может быть ни тем, ни другим.
    """
    pattern = re.compile(CODE_PATTERN)
    for position in CATALOG.positions:
        assert pattern.match(position.code), position.code
    for code in CATALOG.forms:
        assert pattern.match(code), code


def test_ifrs_code_is_not_a_valid_rsbu_code() -> None:
    """Код МСФО нельзя принять за код строки РСБУ ни при каком разборе."""
    for position in CATALOG.positions:
        assert not re.match(r"^\d{4}$", position.code)
        assert "." in position.code


def test_ifrs_codes_do_not_collide_with_methodology_constants() -> None:
    """Код позиции не сталкивается с именами констант методики.

    Константы в формулах — прописные имена без точки; позиция с точкой
    в это пространство имён не попадает.
    """
    from finlib.quality.thresholds import load_thresholds

    constants = set(load_thresholds().constants)
    assert not {item.code for item in CATALOG.positions} & constants


# --- связность справочника ----------------------------------------------------


def test_every_component_exists() -> None:
    """Все позиции состава итогов существуют.

    Итог, ссылающийся на несуществующую позицию, не сойдётся никогда,
    а контроль сходимости сообщит об этом как о дефекте отчётности —
    свалит нашу недоработку на эмитента.
    """
    known = {item.code for item in CATALOG.positions}
    for total in CATALOG.totals():
        for component in total.components:
            assert component.code in known, f"{total.code} → {component.code}"


def test_unknown_component_breaks_loading(tmp_path) -> None:
    """Ссылка на несуществующую позицию не даёт справочнику загрузиться."""
    broken = raw()
    broken["positions"][-1] = {
        "code": "ifrs.total_made_up",
        "name": "Придуманный итог",
        "form": "ifrs.statement_of_financial_position",
        "section": "assets",
        "is_total": True,
        "components": [{"code": "ifrs.nothing_like_this"}],
    }
    with pytest.raises(ValidationError, match="неизвестные позиции"):
        IfrsCatalog.model_validate(broken)


def test_totals_and_components_share_the_form(tmp_path) -> None:
    """Итог не может складываться из позиций другого раздела отчётности."""
    broken = raw()
    for position in broken["positions"]:
        if position["code"] == "ifrs.total_current_assets":
            position["components"].append({"code": "ifrs.revenue"})
    with pytest.raises(ValidationError, match="позиции другого раздела"):
        IfrsCatalog.model_validate(broken)


def test_total_without_components_is_rejected() -> None:
    """Итоговая позиция без состава проверять нечем."""
    broken = raw()
    for position in broken["positions"]:
        if position["code"] == "ifrs.total_assets":
            position["components"] = []
    with pytest.raises(ValidationError, match="без состава"):
        IfrsCatalog.model_validate(broken)


# --- синонимы -----------------------------------------------------------------


def test_aliases_do_not_overlap_inside_a_section() -> None:
    """Наименование принадлежит одной позиции раздела.

    Опознание идёт по наименованию: пересечение синонимов внутри раздела
    означает, что статья ляжет в ту позицию, которая встретилась раньше, —
    то есть произвольно.
    """
    owners: dict[tuple[str, str], list[str]] = {}
    for position in CATALOG.positions:
        for name in position.match_names:
            owners.setdefault((position.section, name), []).append(position.code)
    overlapping = {key: codes for key, codes in owners.items() if len(codes) > 1}
    assert not overlapping, overlapping


def test_same_name_in_two_sections_is_allowed_and_resolved() -> None:
    """«Кредиты и займы» стоят в балансе дважды, и различает их раздел.

    В РСБУ ту же работу делает код строки — 1410 и 1510. В МСФО кода нет,
    и запрет на повтор наименования заставлял опознавать обе строки одной
    позицией: краткосрочный долг затирал долгосрочный, и у ЛСР вместо
    328 256 выходило 35 876.
    """
    assert CATALOG.ambiguous_name("Кредиты и займы")
    assert CATALOG.match_by_name("Кредиты и займы") is None
    assert (
        CATALOG.match_by_name("Кредиты и займы", section="non_current_liabilities").code
        == "ifrs.long_term_borrowings"
    )
    assert (
        CATALOG.match_by_name("Кредиты и займы", section="current_liabilities").code
        == "ifrs.short_term_borrowings"
    )


def test_overlapping_aliases_break_loading() -> None:
    """Пересечение синонимов находится при загрузке, а не при разборе файла."""
    broken = raw()
    for position in broken["positions"]:
        if position["code"] == "ifrs.other_current_assets":
            position["aliases"].append({"name": "Запасы", "seen_at": "выдумка"})
    with pytest.raises(ValidationError, match="нескольким позициям"):
        IfrsCatalog.model_validate(broken)


def test_every_alias_names_the_issuer() -> None:
    """У синонима назван эмитент, у которого он встречен.

    Через полгода при решении, поднимать ли позицию в ядро, нужно видеть,
    откуда взялось написание, а не доверять памяти.
    """
    for position in CATALOG.positions:
        for alias in position.aliases:
            assert alias.seen_at.strip(), position.code


def test_name_is_matched_regardless_of_case_and_yo() -> None:
    """Опознание не зависит от регистра и написания «ё»."""
    assert CATALOG.match_by_name("ИТОГО ОБОРОТНЫЕ АКТИВЫ").code == "ifrs.total_current_assets"
    assert CATALOG.match_by_name("Учётная статья, которой нет") is None
    assert CATALOG.match_by_name("Денежные средства и их эквиваленты").code == "ifrs.cash"


# --- состав ядра --------------------------------------------------------------


def test_core_covers_the_declared_composition() -> None:
    """Ядро содержит позиции, объявленные составом задачи 21."""
    codes = {item.code for item in CATALOG.positions}
    required = {
        "ifrs.ppe",
        "ifrs.right_of_use_assets",
        "ifrs.intangible_assets",
        "ifrs.goodwill",
        "ifrs.investments_in_associates",
        "ifrs.deferred_tax_assets",
        "ifrs.inventories",
        "ifrs.trade_receivables",
        "ifrs.cash",
        "ifrs.long_term_borrowings",
        "ifrs.short_term_borrowings",
        "ifrs.long_term_lease_liabilities",
        "ifrs.short_term_lease_liabilities",
        "ifrs.deferred_tax_liabilities",
        "ifrs.trade_payables",
        "ifrs.advances_received",
        "ifrs.taxes_payable",
        "ifrs.share_capital",
        "ifrs.retained_earnings",
        "ifrs.non_controlling_interests",
        "ifrs.total_assets",
        "ifrs.total_equity",
        "ifrs.total_liabilities",
        "ifrs.revenue",
        "ifrs.profit_for_period",
        "ifrs.depreciation",
    }
    assert required <= codes, required - codes


def test_balance_sheet_totals_are_complete() -> None:
    """Обе стороны баланса сходятся к своим итогам."""
    assets = CATALOG.require("ifrs.total_assets")
    liabilities = CATALOG.require("ifrs.total_equity_and_liabilities")
    assert {item.code for item in assets.components} == {
        "ifrs.total_non_current_assets",
        "ifrs.total_current_assets",
    }
    assert {item.code for item in liabilities.components} == {
        "ifrs.total_equity",
        "ifrs.total_liabilities",
    }


def test_expense_items_carry_their_own_sign() -> None:
    """Расходная статья хранится со знаком, и оператор её складывает.

    Соглашение здесь не то же, что в РСБУ, и в этом весь смысл: отчётность
    по МСФО печатает состав итога со знаком, и оператор повторял бы минус
    вторым разом. `in_brackets` остаётся признаком печати, а `normal_sign`
    объявляет, какой знак у статьи нормален, — по нему знак выводится
    арифметикой, когда эмитент печатает расход без скобок.
    """
    gross = CATALOG.require("ifrs.gross_profit")
    cost = next(item for item in gross.components if item.code == "ifrs.cost_of_sales")
    assert cost.op is Operator.PLUS
    position = CATALOG.require("ifrs.cost_of_sales")
    assert position.in_brackets
    assert position.normal_sign == -1


def test_equity_may_be_negative() -> None:
    """Итог капитала правомерно отрицателен — и это не один случай.

    У Автодора −111 млн при рейтингах АА(RU)/ruAA+: капитала нет
    по устройству. У Сегежи −15 008 млн при накопленном убытке 138 955 млн.
    Формальное условие одно, экономический смысл противоположный.
    """
    from finlib.normalize.lines import Sign

    assert CATALOG.require("ifrs.total_equity").sign is Sign.ANY
    assert CATALOG.require("ifrs.retained_earnings").sign is Sign.ANY


# --- пороги -------------------------------------------------------------------


def test_materiality_and_core_candidate_declare_their_origin() -> None:
    """У обоих порогов объявлено, откуда взялась величина."""
    from decimal import Decimal

    assert CATALOG.materiality.origin.strip()
    assert CATALOG.core_candidate.origin.strip()
    # Доля — Decimal, как всякая величина методики: float для долей
    # не используется нигде, кроме визуализации.
    assert CATALOG.materiality.share_of_total_assets == Decimal("0.05")
    assert CATALOG.core_candidate.distinct_issuers >= 2


def test_every_form_declares_its_materiality_base() -> None:
    """База существенности объявлена у каждой формы — или объявлено её отсутствие.

    Молчание формы читалось бы как «базы нет», то есть наш пробел выглядел бы
    решением методики.
    """
    bases = CATALOG.materiality.bases
    assert set(bases) == set(CATALOG.forms)
    balance = bases["ifrs.statement_of_financial_position"]
    profit = bases["ifrs.statement_of_profit_or_loss"]
    flows = bases["ifrs.statement_of_cash_flows"]
    assert balance.base == "ifrs.total_assets"
    assert profit.base == "ifrs.revenue"
    # У потока базы нет, и причина названа словами: поток за период не доля
    # ни от запаса, ни от оборота.
    assert flows.base is None
    assert flows.no_base_reason and flows.no_base_reason.strip()


def test_the_other_side_of_the_balance_is_not_ranked() -> None:
    """Итог пассива — та же величина, что итог актива, и в перечень не идёт.

    Доля изменения базы в себе самой равна единице, и «Итого капитал
    и обязательства» стояло бы в перечне наибольших изменений у каждого
    эмитента, не говоря о нём ничего: у Сегежи оно заняло вторую строку.
    Обязательный состав валюту баланса называет.
    """
    codes = CATALOG.materiality.base_codes
    assert "ifrs.total_assets" in codes
    assert "ifrs.total_equity_and_liabilities" in codes
    assert "ifrs.revenue" in codes
    # Обычная статья в перечень не попадает: иначе правило отбрасывало бы
    # величины, о которых документ обязан говорить.
    assert "ifrs.cash_and_equivalents" not in codes


def test_rsbu_balance_total_is_not_ranked_twice() -> None:
    """У РСБУ то же правило и та же пара: 1600 и 1700."""
    from finlib.normalize.lines import load_lines

    codes = load_lines().materiality.base_codes
    assert {"1600", "1700", "2110"} <= codes
    assert "1230" not in codes


def test_form_without_a_declared_base_is_refused() -> None:
    """Форма, о базе существенности умолчавшая, справочник не загружает."""
    broken = raw()
    broken["materiality"]["bases"].pop("ifrs.statement_of_cash_flows")
    with pytest.raises(ValidationError, match="не объявили базу существенности"):
        IfrsCatalog.model_validate(broken)


def test_base_and_its_absence_cannot_be_declared_together() -> None:
    """Объявляется ровно одно: база или причина, по которой её нет.

    Оба сразу — противоречие: непонятно, мерится строка или нет; ни одного —
    молчание, которое читалось бы как «базы нет».
    """
    broken = raw()
    broken["materiality"]["bases"]["ifrs.statement_of_cash_flows"]["base"] = (
        "ifrs.total_assets"
    )
    with pytest.raises(ValidationError, match="молчание и оба сразу не допускаются"):
        IfrsCatalog.model_validate(broken)


# --- два справочника живут порознь --------------------------------------------


def test_catalogs_do_not_share_codes() -> None:
    """Коды двух справочников не пересекаются ни одним значением."""
    rsbu = {item.code for item in load_lines().lines}
    ifrs = {item.code for item in CATALOG.positions}
    assert not rsbu & ifrs


# --- подтверждённые специфические статьи --------------------------------------


def confirm(
    conn, code: str, inn: str, name: str, share: str = "0.30", row: int = 0
) -> None:
    """Подтверждение специфической статьи человеком на экране сверки.

    Место строки передаётся: ключ уникальности строится по строке комплекта,
    и без индекса подтверждения не сравниваются между собой вовсе.
    """
    from finlib.db import execute

    execute(
        "INSERT INTO ifrs_line_confirmation "
        "(code, inn, report_date, source_name, form_code, value, materiality_share, "
        " confirmed_by, row_index) VALUES (%(c)s, %(i)s, '2024-12-31', %(n)s, "
        "'ifrs.statement_of_financial_position', 1000, %(s)s, 'аналитик', %(r)s)",
        {"c": code, "i": inn, "n": name, "s": share, "r": row},
        conn=conn,
    )


def test_confirmation_keeps_the_wording_of_the_issuer(db_conn) -> None:
    """Подтверждение хранит наименование дословно, как в отчётности.

    Через полгода при решении, поднимать ли позицию в ядро, нужно видеть,
    одну ли вещь подтверждали у разных эмитентов под разными названиями:
    по коду этого не увидеть, код присваивали мы.
    """
    from finlib.db import fetch_all

    confirm(db_conn, "ifrs.escrow_accounts", "7736050003", "Средства на счетах эскроу")
    rows = fetch_all(
        "SELECT source_name, materiality_share, confirmed_by FROM ifrs_line_confirmation "
        "WHERE code = 'ifrs.escrow_accounts'",
        {},
        conn=db_conn,
    )
    assert rows[0]["source_name"] == "Средства на счетах эскроу"
    assert rows[0]["confirmed_by"] == "аналитик"


def test_core_candidate_counts_issuers_not_confirmations(db_conn) -> None:
    """Кандидат в ядро набирается эмитентами, а не повторами одного случая.

    Признак машинный и никого ни к чему не обязывает: поднятие позиции
    в ядро — решение человека и правка YAML руками. Признак лишь показывает,
    что пора посмотреть.
    """
    from finlib.db import fetch_all

    confirm(db_conn, "ifrs.principal_receivable", "7736050003", "Задолженность Принципала")
    confirm(
        db_conn,
        "ifrs.principal_receivable",
        "2100010824",
        "Дебиторская задолженность Принципала",
    )
    rows = fetch_all(
        "SELECT issuers, confirmations, source_names FROM ifrs_core_candidate "
        "WHERE code = 'ifrs.principal_receivable'",
        {},
        conn=db_conn,
    )
    assert rows[0]["issuers"] == 2
    assert rows[0]["confirmations"] == 2
    # Оба написания видны рядом: одну ли вещь подтверждали, решает человек.
    assert len(rows[0]["source_names"]) == 2
    assert rows[0]["issuers"] < CATALOG.core_candidate.distinct_issuers


def test_one_row_holds_one_decision(db_conn) -> None:
    """Одна строка комплекта — одно решение человека.

    Повторное подтверждение той же строки — исправление, а не второе
    наблюдение, и ключ уникальности строится **по строке**, а не по паре
    «строка, код». Прежде исправление ложилось рядом с ошибкой: у ФосАгро
    строка «права пользования» получила три кода за три присеста, разметка
    не применялась ни одним из них, и человек размечал её заново.
    """
    import psycopg2

    confirm(db_conn, "ifrs.option_liabilities", "7736050003", "Обязательства по опционам")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        confirm(db_conn, "ifrs.option_liabilities", "7736050003", "Обязательства по опционам")


def test_a_second_code_for_one_row_does_not_lie_beside_the_first(db_conn) -> None:
    """Другой код для той же строки — тоже исправление, а не вторая запись."""
    import psycopg2

    confirm(db_conn, "ifrs.option_liabilities", "7736050003", "Обязательства по опционам")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        confirm(db_conn, "ifrs.escrow_accounts", "7736050003", "Обязательства по опционам")


def test_one_code_may_belong_to_two_rows_of_a_form(db_conn) -> None:
    """Один код у двух строк формы правомерен: строки разные.

    «Прочие расходы» встречаются в форме дважды, и прежний ключ — по коду
    и наименованию — второе подтверждение отвергал, хотя это другая строка.
    """
    from finlib.db import fetch_all

    confirm(db_conn, "ifrs.option_liabilities", "7736050003", "Прочие расходы", row=4)
    confirm(db_conn, "ifrs.option_liabilities", "7736050003", "Прочие расходы", row=9)
    rows = fetch_all(
        "SELECT row_index FROM ifrs_line_confirmation WHERE inn = %(i)s "
        "AND code = 'ifrs.option_liabilities' ORDER BY row_index",
        {"i": "7736050003"},
        conn=db_conn,
    )
    assert [item["row_index"] for item in rows] == [4, 9]


def test_confirmed_codes_are_not_methodology(db_conn) -> None:
    """Подтверждённая статья в справочник методики не попадает.

    Методика правится руками и диффом; позиция, присвоенная во время работы,
    методикой не является и живёт в базе.
    """
    confirm(db_conn, "ifrs.escrow_accounts", "7736050003", "Средства на счетах эскроу")
    assert CATALOG.get("ifrs.escrow_accounts") is None
    assert "escrow" not in default_path().read_text(encoding="utf-8")


def test_ifrs_catalog_does_not_import_rsbu_notion_of_forms() -> None:
    """Раздел отчётности МСФО не притворяется формой по ОКУД.

    Формы РСБУ — семизначные коды приказа; разделы консолидированной
    отчётности утверждённых кодов не имеют, и называются они у эмитентов
    по-разному.
    """
    for code in CATALOG.forms:
        assert not re.match(r"^\d{7}$", code)
    assert CATALOG.match_form("Консолидированный отчёт о движении денежных средств") == (
        "ifrs.statement_of_cash_flows"
    )


def test_position_may_occur_in_two_forms() -> None:
    """Один код в двух формах правомерен, и вторая форма объявлена у позиции.

    Неденежные корректировки косвенного метода повторяют статьи отчёта
    о прибыли — налог, курсовые разницы, обесценение, — и шесть зеркал
    свёрнуты в позиции ядра. Величина при этом принадлежит форме строки:
    в ОПУ и в ОДДС это два разных факта.
    """
    flows = "ifrs.statement_of_cash_flows"
    for code in (
        "ifrs.income_tax",
        "ifrs.fx_gain_loss",
        "ifrs.impairment_losses",
        "ifrs.investment_income",
        "ifrs.other_finance_result",
        "ifrs.share_of_associates_result",
    ):
        position = CATALOG.require(code)
        assert position.occurs_in(position.form)
        assert position.occurs_in(flows), code
        # Опознание в обеих формах идёт по одному правилу — по самой позиции.
        assert CATALOG.match_by_name(position.name, form=flows) is position

    # Зеркала, у которых смысл различается, остались отдельными кодами:
    # нетто-результат — сальдо, а не расход; проценты у́же финансовых расходов.
    for code in (
        "ifrs.cf_adj_finance_result_net",
        "ifrs.cf_adj_interest_expense",
        "ifrs.cf_adj_interest_income",
    ):
        position = CATALOG.require(code)
        assert position.form == flows
        assert position.also_in_forms == ()


def test_second_form_does_not_cancel_the_form_rule() -> None:
    """Форма продолжает различать тёзок, у которых вторая форма не объявлена.

    «Прибыль до налогообложения» стоит и в отчёте о прибыли, и первой строкой
    косвенного метода, означая разное: опознание по наименованию без формы
    отдало бы произвольную из двух.
    """
    profit = CATALOG.match_by_name(
        "Прибыль до налогообложения", form="ifrs.statement_of_profit_or_loss"
    )
    flows = CATALOG.match_by_name(
        "Прибыль до налогообложения", form="ifrs.statement_of_cash_flows"
    )
    assert profit is not None and flows is not None
    assert profit.code != flows.code
    assert not profit.occurs_in("ifrs.statement_of_cash_flows")
