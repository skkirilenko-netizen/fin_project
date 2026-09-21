"""Надзорные признаки ветки МСФО: перенесённые, непереносимые и недействующие.

Прежде раздел 4 документа по МСФО держался на одном аудиторском заключении:
движение капитала, выплаты акционерам и структурный сдвиг расчёт не выявлял
вовсе — признаков у ветки не было, — и отличить это от «ничего не нашлось»
было нечем. У Сегежи прирост эмиссионного дохода 81 057 млн при валюте баланса
141 745 млн в документ не попадал.
"""

from decimal import Decimal

from finlib.normalize.ifrs_signals import load_ifrs_signals
from finlib.scoring.engine import shares_of
from finlib.scoring.signals import evaluate_signals, structure_shifts

CATALOG = load_ifrs_signals()

# Величины годового комплекта ПАО «ФосАгро» за 2025 год, млн руб.
PHOSAGRO = {
    "ifrs.total_assets": Decimal(663888),
    "ifrs.total_equity": Decimal(239793),
    "ifrs.share_capital": Decimal(372),
    "ifrs.share_premium": Decimal(7494),
    "ifrs.total_comprehensive_income": Decimal(113841),
    "ifrs.dividends_paid": Decimal(-46620),
}
PHOSAGRO_BEFORE = {
    "ifrs.total_assets": Decimal(596322),
    "ifrs.total_equity": Decimal(164722),
    "ifrs.share_capital": Decimal(372),
    "ifrs.share_premium": Decimal(7494),
    "ifrs.total_comprehensive_income": Decimal(69128),
    "ifrs.dividends_paid": Decimal(-38000),
}


def fired(current: dict, previous: dict) -> dict[str, Decimal]:
    """Сработавшие признаки с их величинами."""
    return {
        item.code: item.value
        for item in evaluate_signals(current, previous, CATALOG, "млн руб.")
    }


# --- справочник ---------------------------------------------------------------


def test_every_signal_declares_its_origin_and_maturity() -> None:
    """У каждого признака объявлено происхождение порога и его зрелость."""
    for signal in CATALOG.signals:
        assert signal.origin.strip(), signal.code
        assert signal.calibration_status.strip(), signal.code
        # Сейчас предварительны все: наблюдений ветки два-три, и это
        # не выборка.
        assert signal.preliminary, signal.code


def test_inactive_signal_names_its_reason_and_never_fires() -> None:
    """Недействующий признак объявляет причину и не срабатывает.

    Отсечка, подогнанная под два наблюдения, выглядит работающим правилом,
    а меряет размер набора. Объявленное недействие — не молчание: удалённый
    признак нельзя отличить от забытого.
    """
    inactive = [item for item in CATALOG.signals if not item.active]
    assert inactive, "недействующие признаки объявлены — иначе проверять нечего"
    for signal in inactive:
        assert signal.inactive_reason.strip(), signal.code
    # Условие смещения долга выполняется, а признак не срабатывает.
    values = {
        "ifrs.total_assets": Decimal(1000),
        "ifrs.short_term_borrowings": Decimal(900),
        "ifrs.long_term_borrowings": Decimal(100),
    }
    assert "debt_shift_to_short_term" not in fired(values, values)


def test_signals_not_transferred_name_their_reason() -> None:
    """Непереносимый признак объявлен вместе с причиной.

    Перечень признаков ветки сравнивают с перечнем РСБУ, и отсутствующий
    признак без причины неотличим от забытого.
    """
    from finlib.scoring.signals import load_signals

    refused = {item.code: item for item in CATALOG.not_transferred}
    assert refused, "непереносимые признаки объявлены"
    for item in refused.values():
        assert item.reason.strip(), item.code
    # Каждый непереносимый признак существует в справочнике РСБУ: иначе
    # объявление говорило бы о признаке, которого нет.
    rsbu = {item.code for item in load_signals().signals}
    assert set(refused) <= rsbu


def test_revision_intensity_is_declared_inactive_without_a_threshold() -> None:
    """Интенсивность пересмотра объявлена недействующей, и отсечки у неё нет.

    Комплектов МСФО у эмитента один-два, а расхождений на паре комплектов 89:
    отсечка по такому знаменателю мерила бы размер нашего набора.
    """
    rule = CATALOG.revision_intensity
    assert not rule.active
    assert rule.threshold_per_set is None
    assert rule.inactive_reason.strip()


# --- перенесённые признаки ----------------------------------------------------


def test_dividends_paid_are_observed_directly() -> None:
    """Выплаты акционерам сверх порога существенности — надзорный признак.

    В РСБУ изъятие выводилось остатком «прирост капитала минус прибыль»,
    потому что другого следа у выплаты не было. В консолидированной отчётности
    выплата стоит отдельной строкой отчёта о движении денежных средств,
    и косвенный признак ей не нужен.
    """
    found = fired(PHOSAGRO, PHOSAGRO_BEFORE)
    assert found["equity_withdrawal"] == Decimal(-46620)
    by_code = {
        item.code: item
        for item in evaluate_signals(PHOSAGRO, PHOSAGRO_BEFORE, CATALOG, "млн руб.")
    }
    signal = by_code["equity_withdrawal"]
    assert signal.level.value == "supervisory"
    # Величина печатается по модулю и вместе с единицей комплекта: «46 620»
    # без единицы читатель прочтёт в тех единицах, которые предположит сам.
    assert "46 620 млн руб." in signal.message.replace(" ", " ")
    # Отсечка идёт рядом с величиной: тезис без неё проверить нечем.
    assert signal.details["threshold_shown"]

    # Выплата ниже порога существенности признака не даёт.
    modest = {**PHOSAGRO, "ifrs.dividends_paid": Decimal(-1000)}
    assert "equity_withdrawal" not in fired(modest, PHOSAGRO_BEFORE)


def test_capital_contribution_is_measured_by_the_balance_sheet() -> None:
    """Прирост капитала считается по балансу, а не по поступлению денег.

    У Сегежи по отчёту о движении денежных средств поступление от выпуска
    акций 51 012, а по балансу уставный капитал и эмиссионный доход выросли
    на 87 333: разница — неденежная часть, и терять её нельзя.
    """
    current = {
        "ifrs.total_assets": Decimal(141745),
        "ifrs.share_capital": Decimal(6285),
        "ifrs.share_premium": Decimal(115557),
    }
    previous = {
        "ifrs.total_assets": Decimal(206554),
        "ifrs.share_capital": Decimal(9),
        "ifrs.share_premium": Decimal(34500),
    }
    found = fired(current, previous)
    assert found["equity_contribution"] == Decimal(87333)


def test_unexplained_movement_of_equity_is_a_reconciliation() -> None:
    """Сошедшееся тождество признака не даёт, несошедшееся — даёт.

    Движение капитала за период объясняется совокупным доходом, вложениями
    акционеров и выплатами им. Что не объяснилось этими тремя — остаток,
    и он же признак: у ПК «Стройсервис» по РСБУ таким остатком оказалось
    изъятие 42 000 тыс. руб., которого в заключении не было вовсе.
    """
    previous = {
        "ifrs.total_assets": Decimal(1000),
        "ifrs.total_equity": Decimal(500),
        "ifrs.share_capital": Decimal(10),
        "ifrs.share_premium": Decimal(90),
    }
    # Тождество сходится: капитал вырос на совокупный доход минус выплаты.
    closes = {
        **previous,
        "ifrs.total_equity": Decimal(560),
        "ifrs.total_comprehensive_income": Decimal(100),
        "ifrs.dividends_paid": Decimal(-40),
    }
    assert "equity_movement_unexplained" not in fired(closes, previous)

    # Не сходится на 200 — это 20 % валюты баланса.
    breaks = {**closes, "ifrs.total_equity": Decimal(360)}
    found = fired(breaks, previous)
    assert found["equity_movement_unexplained"] == Decimal(-200)

    # Сравнивается модуль: необъяснённый прирост — такой же признак.
    grows = {**closes, "ifrs.total_equity": Decimal(760)}
    assert fired(grows, previous)["equity_movement_unexplained"] == Decimal(200)


def test_signal_without_its_values_does_not_fire() -> None:
    """Нераскрытая величина признака не даёт, а не даёт нулевой.

    У Сегежи строки выплат акционерам нет вовсе, и остаток движения капитала
    не считается: подстановка нуля здесь была бы утверждением, что выплат
    не было.
    """
    without = {
        "ifrs.total_assets": Decimal(1000),
        "ifrs.total_equity": Decimal(360),
        "ifrs.total_comprehensive_income": Decimal(100),
        "ifrs.share_capital": Decimal(10),
        "ifrs.share_premium": Decimal(90),
    }
    previous = {**without, "ifrs.total_equity": Decimal(500)}
    found = fired(without, previous)
    assert "equity_movement_unexplained" not in found
    assert "equity_withdrawal" not in found


def test_structure_shift_lines_exist_in_the_catalogue() -> None:
    """Каждая статья сдвига — позиция справочника, и база тоже.

    Наименование в формулировке своё: итог раздела назван по смыслу. Но код
    обязан существовать, иначе признак молча не сработает ни у кого —
    ноль срабатываний неотличим от невыполненного контроля.
    """
    from finlib.normalize.ifrs_lines import load_ifrs_lines

    catalog = load_ifrs_lines()
    rule = CATALOG.structure_shift
    assert catalog.get(rule.base) is not None, rule.base
    for code, name in rule.lines.items():
        assert catalog.get(code) is not None, code
        assert name.strip(), code


def test_rsbu_structure_shift_lines_exist_in_the_catalogue() -> None:
    """То же требование к перечню РСБУ: код обязан существовать."""
    from finlib.normalize.lines import ReportingType, load_lines
    from finlib.scoring.signals import load_signals

    catalog = load_lines()
    rule = load_signals().structure_shift
    assert catalog.get(rule.base, ReportingType.FULL) is not None
    for code, name in rule.lines.items():
        assert catalog.get(code, ReportingType.FULL) is not None, code
        assert name.strip(), code


def test_structure_shift_uses_the_ifrs_aggregates() -> None:
    """Структурный сдвиг мерится итогами разделов ветки и зовёт их по имени."""
    rule = CATALOG.structure_shift
    assert rule.base == "ifrs.total_assets"
    assert "ifrs.total_equity" in rule.lines

    names = dict(rule.lines)
    current = {
        "ifrs.total_assets": Decimal(1000),
        "ifrs.total_equity": Decimal(100),
    }
    previous = {
        "ifrs.total_assets": Decimal(1000),
        "ifrs.total_equity": Decimal(400),
    }
    found = structure_shifts(
        shares_of(current, rule), shares_of(previous, rule), names, CATALOG
    )
    assert [item.code for item in found] == ["structure_shift_ifrs.total_equity"]
    # Наименование статьи — из справочника позиций, а не набрано в коде.
    assert names["ifrs.total_equity"] in found[0].message
    # Сдвиг ниже отсечки признака не даёт.
    calm = {**previous, "ifrs.total_equity": Decimal(350)}
    assert structure_shifts(
        shares_of(calm, rule), shares_of(previous, rule), names, CATALOG
    ) == []
