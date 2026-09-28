"""«Срочное»: срок без подтверждения исполнения — не неплатёж, объявленный источником.

23.09.2026 пять купонов стояли в «Срочном» строкой «купон 23.09.2026,
не исполнено»: платёж был 09.09, неплатёж объявлен 09.09–21.09, 23.09
истекал льготный срок, а в перечне на день отчёта запись была ещё
«Технический дефолт». Дефолтом источник объявил их перечнем 25.09.
"""

import sys
from datetime import date

from finlib.config import settings
from finlib.sources.cbonds_events import DefaultRecord

sys.path.insert(0, str(settings.base_dir / "eval"))

import change_report_run as report  # noqa: E402

UNTIL = date(2026, 9, 23)


def _record(status: str, met: date | None = None) -> DefaultRecord:
    """Купон с платежом 09.09, объявлением 10.09 и концом льготы 23.09."""
    return DefaultRecord(
        emission_id="1754011",
        kind="Купон",
        status=status,
        due=date(2026, 9, 9),
        when=date(2026, 9, 23),
        announced=date(2026, 9, 10),
        met=met,
        amount=None,
    )


def test_grace_end_today_is_unconfirmed_not_a_default() -> None:
    """Льготный срок истекает сегодня, дефолта нет — «исполнение не подтверждено»."""
    order, text = report._record_said(_record("Технический дефолт"), UNTIL)
    assert order == report.URGENT_UNCONFIRMED
    assert text.startswith("купон: срок сегодня — конец льготного срока")
    assert "исполнение не подтверждено" in text
    assert "дефолт источником не объявлен" in text
    assert "плановый срок 09.09.2026" in text and "неплатёж объявлен 10.09.2026" in text
    assert "не исполнено" not in text


def test_a_default_declared_by_the_source_is_said_so() -> None:
    """Запись «Дефолт» — объявлен источником, и она первая в очереди."""
    order, text = report._record_said(_record("Дефолт"), UNTIL)
    assert order == report.URGENT_DECLARED
    assert text.startswith("купон 23.09.2026: дефолт объявлен источником")
    assert "не подтверждено" not in text


def test_a_settled_record_names_the_day_it_was_met() -> None:
    """Исполнено — с датой исполнения, последним в очереди."""
    order, text = report._record_said(
        _record("Технический дефолт", met=date(2026, 9, 22)), UNTIL
    )
    assert order == report.URGENT_SETTLED
    assert text == "купон 23.09.2026, исполнено 22.09.2026"


def test_the_queue_puts_declared_before_unconfirmed_before_ratings() -> None:
    """Очередь вмешательства: объявленный дефолт, неподтверждённый срок, рейтинг, исполненное."""
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
