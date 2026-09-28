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
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document

from finlib.db import PgConnection
from finlib.metrics.display import digits, money
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
        composition.source,
        composition.no_class,
        _sentence(
            f"Отчётная дата величин {item.report_date:%d.%m.%Y}; дата формирования "
            f"{today:%d.%m.%Y}; единица — {item.unit or 'не названа'}"
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
    return BaseConclusion(item.inn, item.name, today, item.report_date, head, parts)


def _sentence(text: str) -> str:
    """Точка в конце, если её не поставило сокращение: «млн руб.» не «млн руб..»."""
    return text if text.endswith(".") else f"{text}."


def _route(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Вывод маршрута: корзина, подгруппа, действие и основания."""
    verdict = item.verdict
    part.paragraphs.append(
        f"Корзина: {basket_name}"
        + (f" ({verdict.subgroup_names[0]})" if verdict.subgroup_names else "")
        + "."
    )
    if verdict.actions:
        part.paragraphs.append(f"Действие: {verdict.actions[0]}")
    own = set(verdict.grounds)
    for entry in verdict.findings:
        mark = "" if entry.ground in own else " (сведение, корзину не называет)"
        # Код основания и предмета — привязка чисел формулировки (инвариант 3).
        part.paragraphs.append(f"— {entry.text}{mark} [{entry.ground}: {entry.subject}]")
    if not verdict.findings:
        part.paragraphs.append("Оснований нет.")


def _values(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Величины маршрута с кодом показателя."""
    if item.basis_note:
        part.paragraphs.append(
            f"База: скользящие двенадцать месяцев на {item.report_date:%d.%m.%Y} — "
            f"{item.basis_note}; баланс на дату."
        )
    if not item.shown_values:
        part.paragraphs.append("Величин маршрута нет: отчётность не рассчитана.")
    for code, name, shown in item.shown_values:
        part.lines.append(Line(name, shown, code))


def _trend(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Тренд скользящих двенадцати месяцев по строкам, объявленным методикой."""
    names = load_interim().trend.lines.get(item.standard.value, {}) if item.standard else {}
    points = ltm_trend(item.inn, item.standard, conn) if item.standard else []
    if not points or not names:
        part.paragraphs.append("Тренда нет: промежуточных комплектов у агрегатора нет.")
        return
    for point in points:
        for code in sorted(names):
            rolled = point.ltm[code]
            shown = money(rolled.value) if rolled.known else "не сложился"
            part.lines.append(Line(f"{names[code]}, LTM на {point.moment:%d.%m.%Y}", shown, code))


def _refinancing(part: Part, item, conn, composition, today, basket_name) -> None:  # noqa: ANN001
    """Платежи ближайшего года, оферты и денежные средства в единице комплекта."""
    gap = item.refinance
    if gap is None or gap.due is None:
        part.paragraphs.append("Графиков платежей на диске нет — «к погашению ноль» сказать нечем.")
        return
    part.paragraphs.append(
        _sentence(f"Окно — {gap.days} дней от дня формирования; единица — {gap.unit}")
    )
    part.lines.append(Line("Платежи по облигациям в окне", money(gap.due), "refinance.due"))
    if gap.offered is not None:
        part.lines.append(Line("Оферты в окне", money(gap.offered), "refinance.offered"))
    part.lines.append(
        Line(
            "Денежные средства",
            money(gap.cash) if gap.cash is not None else "не раскрыты",
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
        outcome = "сработал" if entry.fired else "не сработал"
        part.paragraphs.append(f"{feature.name}: {outcome}, справочно — в маршрут не идёт.")
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
