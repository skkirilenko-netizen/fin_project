"""Базовое заключение по данным агрегатора: уровень 1 заключений по МСФО.

**Класса здесь нет, и документ говорит это о себе** (решение владельца
28.09.2026, вариант «без класса»). Состав величин агрегатора свой: покрытие
процентов считается только по примечаниям, эскроу девелопера у агрегатора
нет, и класс по такому составу был бы другим классом под тем же именем.
Вместо класса документ несёт вывод маршрута — корзину и основания, — величины,
тренд скользящих двенадцати месяцев, рефинансирование и признаки изменения.

**Документ ничего не считает.** Корзину, основания, величины и рефинансирование
даёт боевая маршрутизация (`routing_store.routing_rows`), тренд —
`routing_store.ltm_trend`, признаки изменения — `scoring.interim`. Здесь они
только раскладываются по разделам, и **каждое число идёт с кодом** строки
или показателя (инвариант 3): строка документа — это наименование, величина
и код, а не фраза с числом внутри.

**Только агрегатор.** Строка маршрута, в величинах которой есть документ
эмитента, базовым заключением не описывается: там величины первоисточника,
и заключение по ним — уровень 3, а не 1.

Модель в этой сборке не участвует: все формулировки предписаны методикой
(`report.yaml`, `aggregator_conclusion`), связок между ними нет.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from docx import Document

from finlib.db import PgConnection
from finlib.metrics.display import digits
from finlib.report.policy import AggregatorConclusion
from finlib.scoring.interim import issuer_series, load_interim, reference_readings
from finlib.scoring.routing_store import SOURCE_NAMES, RoutingRow, ltm_trend

logger = logging.getLogger(__name__)

# Источник, которым описывается уровень 1, — в том написании, в каком его
# несёт строка маршрута (`SOURCE_NAMES`). Другой источник в величинах строки
# означает, что заключение описывало бы не агрегатор.
AGGREGATOR = SOURCE_NAMES["cbonds"]


class NotAggregatorOnlyError(ValueError):
    """В величинах строки есть не только агрегатор: это не уровень 1."""


class UnitUnknownError(ValueError):
    """Денежную величину не перевести в единицу печати: её единица не названа."""


# Рубли без множителя: в них источник отдаёт объёмы выпусков и суммы событий
# дефолта, и так их называют формулировки маршрута. В справочнике единиц
# отчётности (`lines.yaml`, `units.names`) их нет намеренно — отчётность
# в рублях не составляется.
RUBLES = ("383", "руб.")


def converter(composition: AggregatorConclusion) -> Callable[[Decimal, str], tuple[str, str]]:
    """Перевод денежной величины в единицу печати документа: число и наименование.

    **Хранение в единице эмитента, печать — в одной на документ** (решение
    владельца 30.09.2026). Единица, которую не назвать кодом, — отказ, а не
    печать как есть: число в чужой единице и есть ошибка в тысячу раз.
    """
    from finlib.metrics.interim import in_unit
    from finlib.normalize.lines import load_lines

    names = dict(load_lines().units.names)
    by_name = {name: code for code, name in names.items()} | {RUBLES[1]: RUBLES[0]}
    target = composition.print_unit.okei
    shown_as = names[target]

    def to(value: Decimal, unit: str) -> tuple[str, str]:
        code = by_name.get(unit) or (unit if unit in names or unit == RUBLES[0] else None)
        converted = in_unit(value, code, target) if code is not None else None
        if converted is None:
            raise UnitUnknownError(f"единица «{unit}» не переводится в {shown_as}")
        return digits(converted, composition.print_unit.digits), shown_as

    return to


def _money(to: Callable[[Decimal, str], tuple[str, str]], value: Decimal, unit: str) -> str:
    """Денежная величина в единице печати с её наименованием."""
    number, name = to(value, unit)
    return f"{number} {name}"


@dataclass(frozen=True, slots=True)
class Line:
    """Строка документа: наименование, величина словами печати и код."""

    name: str
    shown: str
    code: str


@dataclass
class Part:
    """Раздел: заголовок, абзацы и строки с кодами."""

    code: str
    title: str
    paragraphs: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)


@dataclass
class BaseConclusion:
    """Собранное заключение уровня 1."""

    inn: str
    name: str
    formed_on: date
    report_date: date | None
    head: list[str]
    parts: list[Part]


def build(
    item: RoutingRow,
    conn: PgConnection,
    composition: AggregatorConclusion,
    today: date,
    basket_name: str,
) -> BaseConclusion:
    """Собирает заключение уровня 1 по строке маршрута."""
    if tuple(item.sources) != (AGGREGATOR,):
        raise NotAggregatorOnlyError(
            f"{item.name} ({item.inn}): величины из {', '.join(item.sources) or 'ничего'} — "
            "базовое заключение описывает только агрегатор"
        )
    head = [
        _source(composition, conn),
        composition.no_class,
        _sentence(
            f"Отчётная дата величин {item.report_date:%d.%m.%Y}; дата формирования "
            f"{today:%d.%m.%Y}; денежные величины — в "
            f"{_unit_names()[composition.print_unit.okei]}"
        )
        if item.report_date is not None
        else "Отчётности агрегатора у эмитента нет.",
    ]
    parts: list[Part] = []
    for section in composition.sections:
        part = Part(section.code, section.title)
        filler = _FILLERS[section.code]
        filler(part, item, conn, composition, today, basket_name)
        parts.append(part)
    _check_unit(head, parts, composition)
    return BaseConclusion(item.inn, item.name, today, item.report_date, head, parts)


class ForeignUnitError(ValueError):
    """В документе напечатана единица, отличная от единицы печати."""


def _check_unit(head: list[str], parts: list[Part], composition: AggregatorConclusion) -> None:
    """Единица печати одна на документ: чужая единица — отказ, а не документ.

    Проверка та же, что у прочих выходов (`display.foreign_units`): ошибка
    в тысячу раз не ловится ни одним контролем сходимости.
    """
    from finlib.metrics.display import foreign_units

    text = "\n".join(
        [
            *head,
            *(paragraph for part in parts for paragraph in part.paragraphs),
            *(f"{line.name} {line.shown}" for part in parts for line in part.lines),
        ]
    )
    wrong = foreign_units(text, _unit_names()[composition.print_unit.okei])
    if wrong:
        raise ForeignUnitError(
            f"в документе напечатаны единицы {', '.join(wrong)} при единице печати "
            f"{_unit_names()[composition.print_unit.okei]}"
        )


class ReconciliationMissingError(ValueError):
    """Сверки агрегатора с документами не записано: мере надёжности взяться неоткуда."""


def _source(composition: AggregatorConclusion, conn: PgConnection) -> str:
    """Абзац об источнике с долей совпавших из таблицы сверки.

    **Число берётся из записанной сверки, а не из текста методики** (решение
    владельца 29.09.2026): вписанное строкой «176 из 183» пережило бы перемер.
    Сверенных ноль — не «ничего не совпало», а отсутствие сверки, и сборка
    отказывается, а не печатает «0 из 0».
    """
    from finlib.quality.reconcile import reporting_share

    share = reporting_share(conn)
    if not share.compared:
        raise ReconciliationMissingError(
            "сверка агрегатора с документами не записана "
            "(eval/source_reconcile_run.py --review --write)"
        )
    return composition.source.format(
        issuers=share.issuers, matched=share.matched, compared=share.compared
    )


def _sentence(text: str) -> str:
    """Точка в конце, если её не поставило сокращение: «млн руб.» не «млн руб..»."""
    return text if text.endswith(".") else f"{text}."


def _route(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Вывод маршрута: корзина со старшим основанием, действие и основания."""
    from finlib.scoring.routing import load_routing

    verdict = item.verdict
    wording = composition.wording
    names = verdict.subgroup_names
    if len(names) > 1:
        head = wording.basket_senior.format(
            basket=basket_name, senior=names[0], others=", ".join(names[1:])
        )
    elif names:
        head = wording.basket_single.format(basket=basket_name, senior=names[0])
    else:
        head = wording.basket_plain.format(basket=basket_name)
    part.paragraphs.append(head)
    if verdict.actions:
        part.paragraphs.append(f"Действие: {verdict.actions[0]}")
    routing = load_routing()
    to = converter(composition)
    own = set(verdict.grounds)
    for entry in verdict.findings:
        mark = "" if entry.ground in own else " (сведение, корзину не называет)"
        # Код основания и предмета — привязка чисел формулировки (инвариант 3).
        text = entry.worded(routing, to)
        part.paragraphs.append(f"— {text}{mark} [{entry.ground}: {entry.subject}]")
    if not verdict.findings:
        part.paragraphs.append("Оснований нет.")
    settled = _rating_after_settlement(item, wording, routing)
    if settled:
        part.paragraphs.append(settled)


# Основания урегулированного дефолта: с любым из них и рейтингом категории
# дефолта документ называет обе даты и связь между ними.
_SETTLED = frozenset({"default_settled_recent", "default_settled_stale", "default_on_repaid_issue"})


def _rating_after_settlement(item, wording, routing) -> str:  # noqa: ANN001
    """Рейтинг категории дефолта рядом с урегулированием: обе даты и связь.

    Печатается, только когда в вердикте есть оба основания (решение владельца
    30.09.2026). Дата урегулирования — дата погашения выпуска, если он погашен,
    иначе дата исполнения последнего просроченного платежа.
    """
    grounds = {entry.ground for entry in item.verdict.findings}
    events = item.events
    if "rating_default" not in grounds or not grounds & _SETTLED or events is None:
        return ""
    rating = next(
        (
            entry
            for entry in events.live
            if entry.category in routing.events.review_categories
            and entry.assigned is not None
        ),
        None,
    )
    met = [record for record in events.records if record.met is not None]
    if rating is None or not met:
        return ""
    latest = max(met, key=lambda record: record.met)
    issue = next(
        (entry for entry in events.issues if entry.emission_id == latest.emission_id),
        None,
    )
    settled_on = latest.met
    if (
        issue is not None
        and issue.status in routing.events.repaid_statuses
        and issue.maturity is not None
    ):
        settled_on = issue.maturity
    template = (
        wording.rating_revised_after_settlement
        if rating.assigned > settled_on
        else wording.rating_after_settlement
    )
    return " ".join(
        template.format(
            issue=issue.name if issue is not None else latest.emission_id,
            settled_on=f"{settled_on:%d.%m.%Y}",
            point=rating.point,
            agency=rating.agency,
            rated_on=f"{rating.assigned:%d.%m.%Y}",
        ).split()
    )


def _values(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Величины маршрута с кодом показателя: денежные — в единице печати."""
    from finlib.scoring.routing import load_routing
    from finlib.scoring.routing_catalogue import catalogue_for

    if item.basis_note:
        part.paragraphs.append(
            f"База: скользящие двенадцать месяцев на {item.report_date:%d.%m.%Y} — "
            f"{item.basis_note}; баланс на дату."
        )
    if not item.shown_values:
        part.paragraphs.append("Величин маршрута нет: отчётность не рассчитана.")
        return
    catalogue = catalogue_for(item.standard)
    edge = load_routing().bound_meaningless_above
    to = converter(composition)
    for code, name, shown in item.shown_values:
        value = item.values.get(code)
        if value is not None and code in catalogue.money:
            shown = _money(to, value, item.unit)
        elif value is not None and code == catalogue.rule.bound and value > edge:
            # Оценка сверху, которая ничего не ограничивает, числом
            # не печатается: рядом стоит основание, называющее её
            # бессодержательной.
            shown = composition.wording.bound_meaningless
        part.lines.append(Line(name, shown, code))


def _trend(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Тренд скользящих двенадцати месяцев по строкам, объявленным методикой."""
    names = load_interim().trend.lines.get(item.standard.value, {}) if item.standard else {}
    points = ltm_trend(item.inn, item.standard, conn) if item.standard else []
    if not points or not names:
        part.paragraphs.append("Тренда нет: промежуточных комплектов у агрегатора нет.")
        return
    to = converter(composition)
    unit_names = _unit_names()
    for point in points:
        for code in sorted(names):
            rolled = point.ltm[code]
            shown = (
                _money(to, rolled.value, unit_names.get(point.unit_code or "", ""))
                if rolled.known
                else composition.wording.ltm_missing.format(reason=rolled.reason)
            )
            part.lines.append(Line(f"{names[code]}, LTM на {point.moment:%d.%m.%Y}", shown, code))


def _unit_names() -> dict[str, str]:
    """Наименования единиц отчётности по коду ОКЕИ."""
    from finlib.normalize.lines import load_lines

    return dict(load_lines().units.names)


def _refinancing(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Платежи ближайшего года, оферты и денежные средства в единице комплекта."""
    gap = item.refinance
    if gap is None or gap.due is None:
        part.paragraphs.append("Графиков платежей на диске нет — «к погашению ноль» сказать нечем.")
        return
    to = converter(composition)
    part.paragraphs.append(_sentence(f"Окно — {gap.days} дней от дня формирования"))
    part.lines.append(
        Line("Платежи по облигациям в окне", _money(to, gap.due, gap.unit), "refinance.due")
    )
    if gap.offered is not None:
        part.lines.append(
            Line("Оферты в окне", _money(to, gap.offered, gap.unit), "refinance.offered")
        )
    part.lines.append(
        Line(
            "Денежные средства",
            _money(to, gap.cash, gap.unit) if gap.cash is not None else "не раскрыты",
            "refinance.cash",
        )
    )


def _changes(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Признаки изменения, показываемые справочно по решению владельца."""
    policy = load_interim()
    for entry in reference_readings(policy, issuer_series(conn, item.inn), today):
        feature = entry.feature
        if entry.value is None:
            part.paragraphs.append(f"{feature.name}: мерить нечем ({entry.silence}).")
            continue
        outcomes = composition.wording.reference_outcomes
        part.paragraphs.append(
            composition.wording.reference_feature.format(
                name=feature.name, outcome=outcomes.fired if entry.fired else outcomes.quiet
            )
        )
        part.lines.append(
            Line(
                f"Доля изменения к прежнему комплекту ({entry.was.moment:%d.%m.%Y} → "  # type: ignore[union-attr]
                f"{entry.now.moment:%d.%m.%Y}), %",  # type: ignore[union-attr]
                digits(entry.value * 100, 1),
                feature.code,
            )
        )
        part.lines.append(
            Line("Отсечка, %", digits(entry.threshold * 100, 1), f"{feature.code}.frozen")
        )


def _limits(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Ограничения, предписанные методикой."""
    part.paragraphs.extend(composition.limitations)


_FILLERS = {
    "route": _route,
    "values": _values,
    "trend": _trend,
    "refinancing": _refinancing,
    "changes": _changes,
    "limits": _limits,
}


def render(
    conclusion: BaseConclusion, composition: AggregatorConclusion, path: Path, mark: str = ""
) -> Path:
    """Пишет заключение в Word: строки с кодом — таблицей из трёх граф."""
    document = Document()
    document.add_heading(composition.title, level=0)
    if mark:
        document.add_paragraph(mark)
    document.add_paragraph(f"{conclusion.name}, ИНН {conclusion.inn}")
    for text in conclusion.head:
        document.add_paragraph(text)
    for number, part in enumerate(conclusion.parts, start=1):
        document.add_heading(f"{number}. {part.title}", level=1)
        for text in part.paragraphs:
            document.add_paragraph(text)
        if part.lines:
            table = document.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            for cell, text in zip(
                table.rows[0].cells, ("Величина", "Значение", "Код"), strict=True
            ):
                cell.text = text
            for line in part.lines:
                cells = table.add_row().cells
                cells[0].text, cells[1].text, cells[2].text = line.name, line.shown, line.code
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path
