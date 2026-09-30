"""Уровень 2 на эмитентах с PDF: заключение аудитора, долг по примечанию, сверка.

    uv run python eval/level2_run.py [--methodology КАТАЛОГ] [--docx]

**Прогон без записи в базу.** Документ проходит приём с `write=False`,
статус комплекта (принят / карантин) и основания карантина читаются
из `src_file` и `dq_log`, займы агрегатора — из ответа Cbonds на диске
(`msfo_real_universe`); ответа на диске нет — опоры нет, в сеть прогон
не ходит. Строку маршрута для разделов уровня 1 даёт боевой путь
на сегодня, транзакция откатывается.

`--methodology` — каталог, из которого читаются `ifrs_note_lines.yaml`
и `report.yaml`: так прогон делается по диффу, поданному на согласование,
не применяя его. Без ключа — справочники проекта, и пока состав сроков
и раздела не утверждён, прогон называет отказ, а не берёт умолчание.

Таблица итогов печатается в конец: мнение аудитора определено или нет,
примечание о долге найдено, сроки разобраны (какая основа), сверка суммы
(с чем, прошла или нет).
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from finlib.db import connection, fetch_all
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.normalize.ifrs_note_lines import load_note_lines
from finlib.normalize.lines import load_lines
from finlib.pipeline import accept_ifrs_document
from finlib.quality.codes import check_name
from finlib.sources.ifrs_debt_note import (
    Check,
    printed_buckets,
    read_debt_note,
    reconcile_debt,
    stored_facts,
)
from finlib.sources.ifrs_inbox import form_headings, text_of
from finlib.sources.ifrs_notes import index_notes
from finlib.sources.ifrs_numbers import load_parsing_policy

logger = logging.getLogger(__name__)

ROOT = Path("data/raw/ifrs")
OUT = Path("data/output/level2")
# Эмитенты «Разбора», чьи PDF лежат на диске (план уровня 2, 29.09.2026).
ISSUERS = {
    "6685151087": "Брусника",
    "7717151380": "Автодор",
    "9731004688": "Самолёт",
    "7826087713": "О'КЕЙ",
    "7838360491": "ЛСР",
    "9703024202": "Сегежа",
}
MARK = (
    "ПРОБА. Документ собран по составу, поданному на согласование, "
    "и решением не является."
)

_STATUS = """
SELECT id, status FROM src_file
WHERE inn = %(inn)s AND standard = 'ifrs' AND source = 'file' AND is_actual
  AND raw_path = %(path)s
"""
_BLOCKING = """
SELECT DISTINCT check_code FROM dq_log
WHERE src_file_id = %(id)s AND status = 'fail'
ORDER BY check_code
"""


def _aggregator_rows() -> list[dict] | None:
    """Ответ агрегатора с диска; None — ответа нет, а сеть здесь не трогается."""
    from finlib.sources import cbonds

    if not cbonds._cache_path("msfo_real_universe").exists():
        return None
    return cbonds.msfo_universe()


def _row(inn: str, path: Path, method, policy, rows, conn) -> dict:  # noqa: ANN001
    """Один документ: приём без записи, заключение, сроки, сверка."""
    result: dict = {"inn": inn, "who": ISSUERS[inn], "file": path.name}
    document = text_of(path)
    if not document.readable:
        result["refused"] = f"файл не прочитан: {document.error}"
        return result
    intake = accept_ifrs_document(
        document.text, inn=inn, raw_path=str(path), document=document, write=False
    )
    if not intake.accepted:
        result["refused"] = f"отклонён приёмом: {intake.reason}"
        return result
    profile, extraction = intake.profile, intake.extraction
    report_date = max(profile.report_dates)
    result["date"] = report_date
    result["audit"] = intake.reading.audit
    headings = form_headings(document.text, load_ifrs_lines(), load_parsing_policy())
    index = index_notes(document.text, document, after=min(headings.values(), default=0))
    debt = read_debt_note(
        document.text, index, extraction, report_date, tuple(profile.report_dates),
        method, policy.maturity_table,
    )  # fmt: skip
    result["debt"] = debt
    stored = fetch_all(_STATUS, {"inn": inn, "path": str(path)}, conn=conn)
    quarantined = any(item["status"] == "quarantine" for item in stored)
    reasons = [
        check_name(item["check_code"])
        for entry in stored
        for item in fetch_all(_BLOCKING, {"id": entry["id"]}, conn=conn)
    ]
    result["stored"] = "карантин" if quarantined else ("принят" if stored else "не загружен")
    result["reasons"] = "; ".join(reasons)
    unit_name = load_lines().units.name_of(profile.unit_code)
    result["unit"] = unit_name
    if debt.table is None or method is None:
        return result
    codes = (*method.found_in, *method.lease_lines)
    if quarantined or not stored:
        against = "данные агрегатора"
        reading = None
        if rows is not None:
            from finlib.quality.reconcile import aggregator_reading

            reading = aggregator_reading(rows, inn, report_date)
        reference = {code: (reading.values.get(code) if reading else None) for code in codes}
        reference_unit = reading.unit_code if reading else None
    else:
        against = "баланс документа"
        reference = {
            item.code: item.value
            for item in extraction.values
            if item.report_date == report_date and item.code in codes
        }
        reference_unit = profile.unit_code
    check = reconcile_debt(
        debt.table, profile.unit_code, reference, reference_unit, against, method
    )
    result["check"] = check
    result["buckets"] = printed_buckets(debt.table, method)
    result["buckets_with_debt_like"] = printed_buckets(debt.table, method, ("debt", "debt_like"))
    result["facts"] = stored_facts(debt.table, method)
    result["reference_codes"] = " + ".join(method.found_in)
    result["carrying_codes"] = (
        method.storage.code("carrying", "debt", 0, None),
        method.storage.code("carrying", "debt_like", 0, None),
    )
    return result


def _docx(result: dict, conn, level1, composition, audit_policy, today, found) -> str:  # noqa: ANN001
    """Заключение уровня 2 в Word; возвращает путь либо причину отказа."""
    from finlib.report.aggregator import render
    from finlib.report.level2 import Level2Document, build_level2
    from finlib.scoring.routing import load_routing

    item = found.get(result["inn"])
    if item is None:
        return "в маршруте эмитента нет"
    document = Level2Document(
        path=result["file"],
        report_date=result["date"],
        unit=result["unit"],
        audit=result["audit"],
        audit_policy=audit_policy,
        quarantine=result["reasons"] if result["stored"] == "карантин" else "",
        debt=result["debt"],
        reconciliation=result.get("check"),
        buckets=result.get("buckets", ()),
        buckets_with_debt_like=result.get("buckets_with_debt_like", ()),
        carrying_codes=result.get("carrying_codes", ("", "")),
        reference_codes=result.get("reference_codes", ""),
    )
    basket = load_routing().basket(item.verdict.basket).name
    conclusion = build_level2(item, conn, level1, composition, document, today, basket)
    path = OUT / f"{result['inn']}_{result['date']:%Y-%m-%d}_уровень2_проба.docx"
    render(conclusion, composition, path, MARK)  # type: ignore[arg-type]
    return str(path)


def _audit_state(audit) -> str:  # noqa: ANN001
    """Определено ли мнение аудитора — словами таблицы итогов."""
    if audit is None:
        return "не читалось"
    if audit.opinion:
        kind = "обзорная проверка" if audit.engagement.value == "review" else "аудит"
        return f"определено: {audit.opinion_name} ({kind})"
    return f"нет — {audit.describe()}"


def main() -> int:
    """Прогон по шести эмитентам и таблица итогов."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--methodology", type=Path, default=None)
    parser.add_argument("--docx", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    notes_path = args.methodology / "ifrs_note_lines.yaml" if args.methodology else None
    method = load_note_lines(notes_path).debt_maturity
    policy = load_parsing_policy()
    rows = _aggregator_rows()
    if rows is None:
        print("ответа агрегатора на диске нет: сверка у карантинных комплектов без опоры\n")
    results = []
    with connection() as conn:
        for inn in ISSUERS:
            for path in sorted((ROOT / inn).glob("*.pdf")):
                results.append(_row(inn, path, method, policy, rows, conn))
        written: dict[str, str] = {}
        if args.docx:
            from finlib.normalize.ifrs_audit import load_audit_policy
            from finlib.report.policy import load_policy
            from finlib.scoring.routing_store import routing_rows

            report_path = args.methodology / "report.yaml" if args.methodology else None
            report = load_policy(report_path)
            if report.aggregator_conclusion is None or report.level2_conclusion is None:
                print("состав заключения уровня 1 или 2 в report.yaml не утверждён — docx нет\n")
            else:
                today = date.today()
                routed, _ = routing_rows(conn, today)
                found = {item.inn: item for item in routed}
                for result in results:
                    if "refused" in result:
                        continue
                    written[result["file"]] = _docx(
                        result, conn, report.aggregator_conclusion,
                        report.level2_conclusion, load_audit_policy(), today, found,
                    )  # fmt: skip
        conn.rollback()
    print("# Уровень 2: заключение аудитора и долг по примечанию\n")
    if method is None:
        print("Состав сроков погашения (`debt_maturity`) в методике не утверждён.\n")
    print(
        "| Эмитент | Документ | Комплект | Мнение аудитора | Примечание о долге "
        "| Сроки (основа) | Сверка суммы |"
    )
    print("|---|---|---|---|---|---|---|")
    for result in results:
        if "refused" in result:
            print(f"| {result['who']} | {result['file']} | — | {result['refused']} | — | — | — |")
            continue
        debt = result["debt"]
        note = debt.debt_note.describe() if debt.debt_note else (
            "нет ссылки из формы" if not debt.references else "по ссылке не найдено"
        )
        if debt.table is not None:
            table = debt.table
            terms = (
                f"{debt.basis.value}; примечание {table.note.number}, граф "
                f"{len(table.intervals)}, строк долга {len(table.of_kind('debt'))}"
                + (", есть графа балансовой" if table.has_carrying else ", графы балансовой нет")
            )
        else:
            terms = f"нет — {debt.refusal.value if debt.refusal else 'не читались'}"
        check = result.get("check")
        if check is None:
            checked = "—"
        else:
            value = f"{check.table_value:,}".replace(",", " ") if check.table_value else "—"
            ref = f"{check.reference:,}".replace(",", " ") if check.reference else "—"
            checked = f"с «{check.against}»: {check.outcome.value} ({value} / {ref})"
            if check.debt_like:
                loans = f"{check.loans:,}".replace(",", " ")
                checked += f"; займы {loans}, долгоподобные: {'; '.join(check.debt_like)}"
        stored = result["stored"] + (f" ({result['reasons']})" if result["reasons"] else "")
        print(
            f"| {result['who']} | {result['file']} ({result['date']:%d.%m.%Y}) | {stored} "
            f"| {_audit_state(result['audit'])} | {note} | {terms} | {checked} |"
        )
    for result in results:
        check = result.get("check")
        if "buckets" in result and check is not None and (
            check.loans_passed or check.outcome is Check.NO_CARRYING
        ):
            print(f"\n## {result['who']} — потоки по займам при печати, {result['unit']}\n")
            for bucket in result["buckets"]:
                mark = " (как напечатана)" if bucket.as_printed else ""
                print(
                    f"- {bucket.name}{mark}: "
                    + (f"{bucket.value:,}".replace(",", " ") if bucket.value is not None else "—")
                    + f" `{bucket.code}`"
                )
            print(f"\nК хранению (графы как напечатаны), {result['unit']}:\n")
            for fact in result["facts"]:
                print(f"- `{fact.code}` {fact.value:,} ({fact.label})".replace(",", " "))
        debt = result.get("debt")
        if debt is not None and debt.table is not None:
            odd = debt.table.unread + tuple(row.name for row in debt.table.of_kind(None))
            if odd:
                print(f"\n{result['who']}: не прочитано либо не опознано — {'; '.join(odd)}")
    if written:
        print("\n## Документы\n")
        for name, path in written.items():
            print(f"- {name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
