"""Заключение уровня 2: уровень 1 плюс аудиторское заключение и долг по примечанию.

**Разделы уровня 1 собираются тем же кодом** (`report/aggregator.py`): вывод
маршрута, величины, тренд, рефинансирование и признаки изменения берутся
по данным агрегатора, как у базового заключения. Из документа эмитента
приходят два раздела — «Заключение аудитора» и «Долг по примечанию эмитента»,
— и ограничения уровня 1 заменяются своими: у уровня 2 заключение аудитора
и примечания рассмотрены.

**Документ ничего не считает.** Вид мнения читает приём документа
(`sources.ifrs_audit`), таблицу сроков и сверку — `sources.ifrs_debt_note`;
здесь они раскладываются по разделам, и каждое число идёт с кодом
(инвариант 3): код корзины — `debt_maturity.<корзина>`, код сверки —
`debt_maturity.reconciliation`.

**Карантин комплекта не снимает заключение аудитора** (решение владельца
29.09.2026): карантин — об арифметике форм, а не о тексте. Документ так
и говорит, называя основания. Величины сроков печатаются только при
прошедшей сверке суммы.
"""

import logging
from dataclasses import dataclass, field
from datetime import date

from finlib.metrics.display import money
from finlib.report.aggregator import BaseConclusion, Line, Part, build
from finlib.report.policy import AggregatorConclusion, Level2Conclusion
from finlib.sources.ifrs_debt_note import (
    Check,
    DebtNoteReading,
    PrintedBucket,
    Reconciliation,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Level2Document:
    """Прочитанное в документе эмитента для уровня 2 — готовыми величинами."""

    path: str
    report_date: date
    unit: str
    audit: object | None
    audit_policy: object | None
    # Основания карантина комплекта; пусто — комплект принят.
    quarantine: str = ""
    debt: DebtNoteReading = field(default_factory=DebtNoteReading)
    reconciliation: Reconciliation | None = None
    buckets: tuple[PrintedBucket, ...] = ()
    buckets_with_debt_like: tuple[PrintedBucket, ...] = ()
    # Коды хранения балансовой стоимости: займов и долгоподобных.
    carrying_codes: tuple[str, str] = ("", "")
    # Коды займов опоры сверки: «ifrs.long_term_borrowings + …».
    reference_codes: str = ""
    # Сверка суммы с долгоподобными по займам баланса документа у комплекта
    # в карантине (решение владельца 30.09.2026): опора агрегатора их
    # не включает. Пусто — не делалась.
    debt_like_check: Reconciliation | None = None


def build_level2(
    item: object,
    conn: object,
    level1: AggregatorConclusion,
    composition: Level2Conclusion,
    document: Level2Document,
    today: date,
    basket_name: str,
) -> BaseConclusion:
    """Собирает заключение уровня 2: разделы уровня 1 и два раздела документа."""
    base = build(item, conn, level1, today, basket_name)  # type: ignore[arg-type]
    by_code = {part.code: part for part in base.parts}
    from finlib.report.aggregator import _source

    # Абзац об источнике уровня 1 говорит «документ эмитента не разбирался»,
    # а уровень 2 его разбирал: абзац свой, доля совпавших — та же, из сверки.
    head = [
        _source(composition, conn),  # type: ignore[arg-type]
        *base.head[1:],
        composition.document.format(
            document=document.path.rsplit("/", 1)[-1], date=f"{document.report_date:%d.%m.%Y}"
        ),
    ]
    if document.quarantine:
        head.append(composition.quarantine.format(reasons=document.quarantine))
    parts: list[Part] = []
    for section in composition.sections:
        if section.code == "audit":
            part = Part(section.code, section.title)
            _audit(part, document)
        elif section.code == "debt":
            part = Part(section.code, section.title)
            _debt(part, document, composition)
        elif section.code == "limits":
            part = Part(section.code, section.title)
            part.paragraphs.extend(composition.limitations)
            if document.audit is not None and document.audit_policy is not None:
                part.paragraphs.extend(document.audit.limitations(document.audit_policy))  # type: ignore[attr-defined]
        else:
            source = by_code[section.code]
            part = Part(section.code, section.title, source.paragraphs, source.lines)
        parts.append(part)
    return BaseConclusion(base.inn, base.name, today, base.report_date, head, parts)


def _audit(part: Part, document: Level2Document) -> None:
    """Вид мнения и дословные цитаты разделов, объявленных методикой."""
    audit = document.audit
    if audit is None:
        part.paragraphs.append("Аудиторское заключение не читалось.")
        return
    part.paragraphs.append(_opinion(audit, document.audit_policy))
    signed = ", ".join(item for item in (audit.auditor, audit.signed_on) if item)  # type: ignore[attr-defined]
    if signed:
        part.paragraphs.append(f"Аудитор: {signed}.")
    if document.audit_policy is not None:
        part.paragraphs.extend(audit.quotes(document.audit_policy))  # type: ignore[attr-defined]


def _opinion(audit: object, policy: object | None) -> str:
    """Вид мнения, тип задания и разделы-признаки — наименованиями, а не кодами."""
    from finlib.sources.ifrs_audit import Determination, Engagement

    if audit.determination is not Determination.DETERMINED:  # type: ignore[attr-defined]
        said = audit.describe()  # type: ignore[attr-defined]
        return f"{said[:1].upper()}{said[1:]}."
    names = {item.code: item.name for item in getattr(policy, "sections", ())}
    sections = [names.get(code, code) for code in audit.sections]  # type: ignore[attr-defined]
    kind = (
        "Обзорная проверка"
        if audit.engagement is Engagement.REVIEW  # type: ignore[attr-defined]
        else "Аудит"
    )
    tail = f"; разделы заключения: {', '.join(sections)}" if sections else ""
    return f"{kind}; вид мнения — {audit.opinion_name.lower()}{tail}."  # type: ignore[attr-defined]


def _debt(part: Part, document: Level2Document, composition: Level2Conclusion) -> None:
    """Таблица сроков: основа, сверка, две величины и потоки.

    Потоки печатаются при сошедшейся сверке, а при отсутствии графы
    балансовой стоимости — как есть с оговоркой (решение владельца 30.09.2026):
    сверять там нечем, но и расхождения нет. Сумма с долгоподобными
    печатается потоками, только если сошлась сама.
    """
    wording = composition.debt
    reading = document.debt
    table = reading.table
    if table is None:
        reason = reading.refusal.value if reading.refusal is not None else "не читалась"
        part.paragraphs.append(wording.not_read.format(reason=reason))
        return
    part.paragraphs.append(
        _tidy(
            wording.basis.format(
                note=table.note.describe(),
                date=f"{document.report_date:%d.%m.%Y}",
                unit=document.unit,
            )
        )
    )
    check = document.reconciliation
    if check is None:
        part.paragraphs.append(wording.no_reference.format(against="опора не названа"))
        return
    names = "; ".join(check.debt_like)
    text = {
        Check.PASSED: wording.passed,
        Check.WITH_LEASE: wording.with_lease,
        Check.LOANS_ONLY: wording.loans_only,
        Check.FAILED: wording.failed,
        Check.NO_CARRYING: wording.no_carrying,
        Check.NO_REFERENCE: wording.no_reference,
    }[check.outcome]
    part.paragraphs.append(
        _tidy(
            text.format(
                against=check.against,
                value=_shown(check.table_value),
                loans=_shown(check.loans),
                reference=_shown(check.reference),
                unit=document.unit,
                names=names,
            )
        )
    )
    codes = document.carrying_codes
    if check.loans is not None:
        part.lines.append(
            Line(f"{wording.loans_label}, {document.unit}", money(check.loans), codes[0])
        )
    if check.debt_like and check.table_value is not None:
        part.lines.append(
            Line(
                f"{wording.with_debt_like_label.format(names=names)}, {document.unit}",
                money(check.table_value),
                " + ".join(codes),
            )
        )
    if check.reference is not None:
        part.lines.append(
            Line(f"Займы — {check.against}", money(check.reference), document.reference_codes)
        )
    loans_shown = check.loans_passed or check.outcome is Check.NO_CARRYING
    extra = document.debt_like_check
    if check.debt_like and extra is not None and extra.passed:
        if extra.reference is not None:
            part.lines.append(
                Line(
                    f"Займы — {extra.against}",
                    money(extra.reference),
                    document.reference_codes,
                )
            )
        part.paragraphs.append(
            _tidy(
                wording.passed.format(
                    against=extra.against,
                    value=_shown(extra.table_value),
                    reference=_shown(extra.reference),
                    unit=document.unit,
                )
            )
        )
    both_shown = (
        check.passed
        or check.outcome is Check.NO_CARRYING
        or (extra is not None and extra.passed)
    )
    if loans_shown:
        _flows(part, document.buckets, wording.loans_label, document, composition)
    if check.debt_like:
        if both_shown:
            _flows(
                part,
                document.buckets_with_debt_like,
                wording.with_debt_like_label.format(names=names),
                document,
                composition,
            )
        else:
            part.paragraphs.append(wording.debt_like_not_reconciled.format(names=names))
    unread = table.unread + tuple(row.name for row in table.of_kind(None))
    if unread:
        part.paragraphs.append(wording.unread_rows.format(names="; ".join(unread)))


def _flows(
    part: Part,
    buckets: tuple[PrintedBucket, ...],
    label: str,
    document: Level2Document,
    composition: Level2Conclusion,
) -> None:
    """Строки потоков: корзина либо графа как напечатана, с кодом хранения."""
    for bucket in buckets:
        if bucket.as_printed:
            said = composition.debt.as_printed.format(label=bucket.name)
            if said not in part.paragraphs:
                part.paragraphs.append(said)
        part.lines.append(
            Line(
                f"Потоки: {label}, {bucket.name}, {document.unit}",
                _shown(bucket.value),
                bucket.code,
            )
        )


def _shown(value: object) -> str:
    """Величина словами печати; нет величины — прочерк."""
    return money(value) if value is not None else "—"  # type: ignore[arg-type]


def _tidy(text: str) -> str:
    """Точка сокращения единицы не удваивается: «млн руб.», а не «млн руб..»."""
    return text.replace("..", ".")
