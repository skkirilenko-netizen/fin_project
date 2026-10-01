"""«Срочное»: неплатёж — событие дня объявления, и «не исполнено» — три сведения.

23.09.2026 пять купонов стояли в «Срочном» строкой «купон 23.09.2026,
не исполнено»: платёж был 09.09, неплатёж объявлен 09.09–21.09, 23.09
истекал льготный срок, а в перечне на день отчёта запись была ещё
«Технический дефолт». Дефолтом источник объявил их перечнем 25.09.
А в день объявления неплатежа в «Срочном» их не было вовсе: отбор шёл
по дате события, то есть по концу льготного срока.
"""

import sys
from datetime import date

from finlib.config import settings
from finlib.sources.cbonds_events import DefaultRecord

sys.path.insert(0, str(settings.base_dir / "eval"))

import change_report_run as report  # noqa: E402

ANNOUNCED = date(2026, 9, 10)


def _record(status: str, met: date | None = None) -> DefaultRecord:
    """Купон с платежом 09.09, объявлением 10.09 и концом льготы 23.09."""
    return DefaultRecord(
        emission_id="1754011",
        kind="Купон",
        status=status,
        due=date(2026, 9, 9),
        when=date(2026, 9, 23),
        announced=ANNOUNCED,
        met=met,
        amount=None,
    )


def test_urgent_picks_the_day_of_announcement_not_the_grace_end() -> None:
    """Запись попадает в «Срочное» в день объявления, а в конец льготы — нет."""
    item = _record("Технический дефолт")
    assert report._announced_in(item, date(2026, 9, 9), ANNOUNCED)
    assert not report._announced_in(item, date(2026, 9, 22), date(2026, 9, 23))


def test_announced_nonpayment_in_grace_is_unconfirmed_not_a_default() -> None:
    """Неплатёж объявлен, льгота идёт — «исполнение не подтверждено», не дефолт."""
    order, text = report._record_said(_record("Технический дефолт"), ANNOUNCED)
    assert order == report.URGENT_UNCONFIRMED
    assert text.startswith("купон: технический дефолт; льготный срок до 23.09.2026")
    assert "исполнение не подтверждено" in text
    assert "плановый срок 09.09.2026" in text and "неплатёж объявлен 10.09.2026" in text
    assert "не исполнено" not in text


def test_grace_ending_today_is_said_so() -> None:
    """Льготный срок истекает в день отчёта — так и сказано."""
    _, text = report._record_said(_record("Технический дефолт"), date(2026, 9, 23))
    assert "льготный срок истекает сегодня" in text


def test_a_default_declared_by_the_source_is_said_so() -> None:
    """Запись «Дефолт» — объявлен источником, и она первая в очереди."""
    order, text = report._record_said(_record("Дефолт"), ANNOUNCED)
    assert order == report.URGENT_DECLARED
    assert text.startswith("купон: дефолт объявлен источником")
    assert "не подтверждено" not in text


def test_a_settled_record_names_the_day_it_was_met() -> None:
    """Исполнено к дню отчёта — с датой исполнения, последним в очереди."""
    order, text = report._record_said(
        _record("Технический дефолт", met=date(2026, 9, 22)), date(2026, 9, 23)
    )
    assert order == report.URGENT_SETTLED
    assert text == "купон 23.09.2026, исполнено 22.09.2026"


def test_a_later_settlement_is_not_known_on_the_day() -> None:
    """Исполнение позже дня отчёта в этот день ещё не случилось."""
    order, _ = report._record_said(
        _record("Технический дефолт", met=date(2026, 9, 22)), ANNOUNCED
    )
    assert order == report.URGENT_UNCONFIRMED


def test_the_queue_puts_declared_before_unconfirmed_before_ratings() -> None:
    """Очередь: объявленный дефолт, неплатёж в льготе, рейтинг, исполненное."""
    assert (
        report.URGENT_DECLARED
        < report.URGENT_UNCONFIRMED
        < report.URGENT_RATING
        < report.URGENT_SETTLED
    )


def test_the_offer_delay_is_a_grace_status_too() -> None:
    """«Просрочка исполнения оферты» — льготный статус, «Неисполнение оферты» — нет."""
    assert not _record("Просрочка исполнения оферты").declared
    assert _record("Неисполнение оферты").declared
    assert not _record("Технический дефолт").declared


def test_without_an_announcement_the_record_is_known_from_first_seen() -> None:
    """Объявления нет — запись видна с первого появления в перечне, а не с конца льготы.

    01.10.2026: у 8 из 26 записей, появившихся в снимках 28.09–01.10,
    объявления нет, и прежде они датировались датой дефолта — на 8–14 дней
    позже, чем пришли.
    """
    item = DefaultRecord(
        emission_id="1822321",
        kind="Купон",
        status="Технический дефолт",
        due=date(2026, 9, 18),
        when=date(2026, 10, 2),
        announced=None,
        met=None,
        amount=None,
        seen=date(2026, 9, 29),
    )
    assert item.known_on == date(2026, 9, 29)
    assert report._announced_in(item, date(2026, 9, 28), date(2026, 9, 29))
    _, text = report._record_said(item, date(2026, 9, 29))
    assert "в перечне с 29.09.2026" in text
