"""История корзин: отпечаток входов и знание на прошлую дату.

**Отпечаток разводит три причины изменения**, и на нём держится весь отчёт
изменений: изменился отпечаток — причина у эмитента; тот же при изменившихся
версиях — причина у нас; тот же при тех же версиях — беспричинное изменение,
то есть дефект недетерминированности.

**Пересчёт назад знает меньше, чем наблюдение, и это его свойство, а не
дефект.** Признаки карточки истории не имеют вовсе, и подставлять сегодняшние
в прошлый год значило бы выдать нынешнее знание за наблюдение.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.scoring.routing import load_routing
from finlib.scoring.routing_store import UnknownInputError, _known_at, fingerprint
from finlib.sources.cbonds_events import (
    DefaultRecord,
    Issue,
    IssuerEvents,
    Rating,
)
from finlib.standards import Standard


def issue(name: str, *, unsettled: bool, status: str = "в обращении") -> Issue:
    """Выпуск эмитента с признаком дефолта либо без него."""
    return Issue(
        emission_id=name,
        name=name,
        isin=f"RU{name}",
        status=status,
        default=unsettled,
        unsettled=unsettled,
        maturity=date(2028, 1, 1),
        offer=None,
        outstanding=None,
        updated=date(2026, 9, 1),
    )


def record(emission: str, when: date, *, met: date | None = None) -> DefaultRecord:
    """Событие дефолта с датой."""
    return DefaultRecord(
        emission_id=emission,
        kind="Купон",
        status="Дефолт",
        due=when,
        when=when,
        announced=None,
        met=met,
        amount=Decimal("1000"),
    )


def rating(point: str, assigned: date | None) -> Rating:
    """Рейтинговое действие с датой; None — даты нет."""
    return Rating(
        agency="АКРА",
        scale="Национальная рейтинговая шкала",
        point=point,
        category=point.replace("ru", ""),
        outlook="Стабильный",
        assigned=assigned,
        order=5,
        credit=True,
    )


# --- отпечаток входов --------------------------------------------------------


def test_the_same_inputs_give_the_same_fingerprint() -> None:
    """Отпечаток воспроизводим: иначе каждый прогон был бы изменением."""
    inputs = {"unit": "тыс. руб.", "quarantined": False, "stop_factors": ("a", "b")}
    assert fingerprint(inputs) == fingerprint(dict(reversed(list(inputs.items()))))


def test_a_changed_input_changes_the_fingerprint() -> None:
    """Изменился довод — изменился отпечаток: это и есть «причина у эмитента»."""
    before = fingerprint({"unit": "тыс. руб.", "stop_factors": ()})
    after = fingerprint({"unit": "тыс. руб.", "stop_factors": ("negative_equity",)})
    assert before != after


def test_the_day_of_the_route_is_not_an_input() -> None:
    """День расчёта в отпечаток не входит: иначе он отличался бы всегда.

    Справочники — тоже не данные: их изменение и есть «причина у нас»,
    и называется оно версией методики, а не отпечатком.
    """
    first = fingerprint({"today": date(2026, 9, 23), "unit": "тыс. руб."})
    second = fingerprint({"today": date(2026, 9, 24), "unit": "тыс. руб."})
    assert first == second


def test_an_input_the_fingerprint_cannot_name_is_an_error() -> None:
    """Умолчание запрещено: пропущенный довод даёт беспричинное изменение.

    Оно попадёт в остановку, и объяснить его будет нечем — лучше упасть
    на новом доводе, чем молча его не заметить.
    """

    class Unknown:
        """Довод, которого отпечаток не знает."""

    with pytest.raises(UnknownInputError):
        fingerprint({"strange": Unknown()})


# --- знание на прошлую дату --------------------------------------------------


def test_an_event_after_the_date_is_not_known_yet() -> None:
    """Событие позже названного дня в пересчёт не идёт.

    Иначе история говорила бы о дефолте за месяцы до того, как он случился, —
    то есть предсказывала бы, а не наблюдала.
    """
    events = IssuerEvents(
        inn="1",
        issues=(issue("БО-01", unsettled=False),),
        issues_known=True,
        records=(record("БО-01", date(2026, 9, 1)),),
        records_known=True,
    )
    assert _known_at(events, date(2026, 8, 1)).records == ()
    assert len(_known_at(events, date(2026, 9, 23)).records) == 1


def test_the_card_flag_does_not_reach_the_past() -> None:
    """Признак карточки истории не имеет и в пересчёт не идёт вовсе.

    Сегодняшний признак дефолта, применённый к прошлому году, объявил бы
    эмитента дефолтным весь год.
    """
    events = IssuerEvents(
        inn="1",
        issues=(issue("БО-01", unsettled=True, status="дефолт по погашению"),),
        issues_known=True,
    )
    known = _known_at(events, date(2025, 1, 1))
    assert known.defaulted == ()
    assert not known.unsettled_default


def test_a_rating_action_is_known_from_its_own_date() -> None:
    """Рейтинг восстанавливается на один шаг назад, и не больше.

    До даты действия известно лишь то, что нынешнего рейтинга не было;
    какой был — неизвестно, и оснований по рейтингу на ту дату не ставится.
    """
    events = IssuerEvents(
        inn="1",
        ratings=(rating("ruA", date(2026, 5, 1)), rating("ruB", None)),
        ratings_known=True,
    )
    assert _known_at(events, date(2026, 4, 1)).ratings == ()
    assert len(_known_at(events, date(2026, 6, 1)).ratings) == 1


def test_the_disclosure_lag_is_declared_per_standard() -> None:
    """Дата известности отчётности объявлена сроком закона, а не догадкой.

    ФЗ № 402-ФЗ статья 18 — три месяца на годовую бухгалтерскую;
    ФЗ № 208-ФЗ статья 4 — 120 дней на годовую консолидированную
    и 60 на промежуточную.
    """
    known = load_routing().history.known_from
    assert known.days(Standard.RSBU, interim=False) == 90
    assert known.days(Standard.IFRS, interim=False) == 120
    assert known.days(Standard.IFRS, interim=True) == 60
    # Предварительность объявлена: настоящей даты раскрытия у массовых данных
    # нет вовсе, и выдавать срок за факт нельзя.
    assert known.status == "preliminary"
