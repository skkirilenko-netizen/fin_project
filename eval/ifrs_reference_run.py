"""Прогон эталонных величин ветки МСФО: любое расхождение — остановка.

**Зачем.** Тесты ветки синтетические: они проверяют механизм, а вёрстку
настоящего документа не повторяют. Ровно так прошёл регресс 21.09.2026 —
сноска ЛСР об эскроу не извлекалась вовсе, а тест был зелёным, потому что
приписывал сноску после таблицы, тогда как у ЛСР она напечатана выше неё.
Синтетический тест отвечает «механизм работает», и это не то же самое,
что «документ разбирается».

Состав эталонов — `eval/ifrs_reference.yaml`, правится руками: у каждой
величины объявлено, чей документ, где найдена и почему важна.

    uv run python eval/ifrs_reference_run.py        # make ifrs-reference

В базу не пишет ничего. Часть проверок читает базу — тип эмитента и отказ
показателя без неё не существуют, — и это объявлено у каждой проверки
полем `source`.

Выход 1 при любом расхождении: величина, однажды проверенная глазами,
меняться молча не вправе.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DOCUMENTS = Path("data/raw/ifrs")


@dataclass(frozen=True, slots=True)
class Outcome:
    """Итог одной проверки."""

    inn: str
    issuer: str
    kind: str
    subject: str
    expected: str
    actual: str
    ok: bool

    def describe(self) -> str:
        """Строка отчёта."""
        mark = "✓" if self.ok else "РАСХОЖДЕНИЕ"
        return (
            f"  {mark} {self.issuer or self.inn} — {self.subject}: "
            f"ожидалось {self.expected}, получено {self.actual}"
        )


def _parsed(path: Path):
    """Разбор документа: профиль и извлечение; None — документ не принят."""
    from finlib.sources.ifrs_extract import extract
    from finlib.sources.ifrs_inbox import identify, text_of

    document = text_of(path)
    if not document.readable:
        return None, None, f"файл не прочитан: {document.error}"
    profile = identify(document.text, document=document)
    if not getattr(profile, "accepted", False):
        return None, None, getattr(profile, "reason", "документ не принят")
    extraction = extract(
        document.text,
        profile.dates_by_form,
        profile.grouping,
        columns=document.columns_of,
        layouts=profile.columns_by_form,
    )
    return document, (profile, extraction), ""


def _note_value(document, profile, extraction, code: str) -> Decimal | None:
    """Величина из примечания по ссылке из строки формы."""
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.sources.ifrs_inbox import form_headings
    from finlib.sources.ifrs_notes import index_notes, note_values
    from finlib.sources.ifrs_numbers import load_parsing_policy

    headings = form_headings(document.text, load_ifrs_lines(), load_parsing_policy())
    index = index_notes(document.text, after=min(headings.values(), default=0))
    target = profile.report_dates[0]
    rows = {
        item.code: item.note_reference
        for form in extraction.forms.values()
        for item in form.values
        if item.report_date == target
    }
    found, _ = note_values(
        index, rows, document.text, profile.grouping, len(profile.report_dates)
    )
    return found.get(code)


def _from_document(check: dict) -> Outcome:
    """Проверка, которой база не нужна."""
    inn, issuer = check["inn"], check.get("issuer", "")
    kind, subject = check["kind"], check.get("subject") or check.get("code", kind := check["kind"])
    path = DOCUMENTS / inn / check["document"]
    if not path.exists():
        return Outcome(inn, issuer, kind, subject, str(check.get("expect")), "документа нет", False)
    document, parsed, reason = _parsed(path)
    if parsed is None:
        return Outcome(inn, issuer, kind, subject, str(check.get("expect")), reason, False)
    profile, extraction = parsed
    target = profile.report_dates[0]
    expected = str(check.get("expect"))

    if kind == "note_contains":
        got = next((note for note in extraction.notes if expected in note), "")
        shown = "сноска с величиной" if got else f"сносок {len(extraction.notes)}, величины нет"
        return Outcome(inn, issuer, kind, subject, expected, shown, bool(got))
    if kind == "value":
        value = extraction.value_of(check["code"], target)
        return Outcome(
            inn, issuer, kind, subject, expected, str(value),
            value is not None and value == Decimal(expected),
        )
    if kind == "note_value":
        value = _note_value(document, profile, extraction, check["code"])
        return Outcome(
            inn, issuer, kind, subject, expected, str(value),
            value is not None and value == Decimal(expected),
        )
    if kind == "grouping":
        got = profile.grouping.value
        return Outcome(inn, issuer, kind, subject, expected, got, got == expected)
    if kind == "reporting_kind":
        got = profile.reporting_kind.value
        return Outcome(inn, issuer, kind, subject, expected, got, got == expected)
    return Outcome(inn, issuer, kind, subject, expected, f"вид проверки {kind} неизвестен", False)


_TYPE = """
SELECT meta ->> 'issuer_type' AS issuer_type FROM src_file
WHERE inn = %(inn)s AND standard = 'ifrs' AND is_actual
  AND report_year = %(year)s AND status <> 'quarantine'
"""

_METRIC = """
SELECT status, reason_code FROM metric_value
WHERE inn = %(inn)s AND standard = 'ifrs' AND report_date = %(date)s
  AND metric_code = %(code)s
"""

_STOPS = """
SELECT stop_factor_codes FROM assessment
WHERE inn = %(inn)s AND standard = 'ifrs' AND report_date = %(date)s
"""


def _from_database(check: dict) -> Outcome:
    """Проверка, которой нужна загруженная и подтверждённая база."""
    from datetime import date as date_type

    from finlib.db import connection, fetch_one

    inn, issuer = check["inn"], check.get("issuer", "")
    kind = check["kind"]
    subject = check.get("subject") or check.get("code", kind)
    expected = check["expect"]
    moment = date_type.fromisoformat(check["report_date"])
    with connection() as conn:
        if kind == "issuer_type":
            row = fetch_one(_TYPE, {"inn": inn, "year": moment.year}, conn=conn)
            got = (row or {}).get("issuer_type") or "комплекта нет"
            return Outcome(inn, issuer, kind, subject, expected, got, got == expected)
        if kind == "metric_refused":
            row = fetch_one(
                _METRIC, {"inn": inn, "date": moment, "code": check["code"]}, conn=conn
            )
            if row is None:
                return Outcome(inn, issuer, kind, subject, expected, "показателя нет", False)
            got = f"{row['status']}/{row['reason_code']}"
            ok = row["status"] == "not_calculable" and row["reason_code"] == expected
            return Outcome(inn, issuer, kind, subject, expected, got, ok)
        if kind == "stop_factors":
            row = fetch_one(_STOPS, {"inn": inn, "date": moment}, conn=conn)
            got = sorted((row or {}).get("stop_factor_codes") or [])
            return Outcome(
                inn, issuer, kind, subject, ", ".join(sorted(expected)),
                ", ".join(got) or "оценки нет", got == sorted(expected),
            )
    return Outcome(inn, issuer, kind, subject, str(expected), f"вид {kind} неизвестен", False)


def main(argv: list[str] | None = None) -> int:
    """Прогоняет эталонные величины; 1 при любом расхождении."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default="eval/ifrs_reference.yaml",
        help="Состав эталонных величин",
    )
    parser.add_argument("--verbose", action="store_true", help="Подробный журнал")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    catalog = yaml.safe_load(Path(args.path).read_text(encoding="utf-8"))
    checks = catalog["checks"]
    print(f"Эталонные величины ветки МСФО, состав {catalog['version']}")
    print(f"Проверок объявлено: {len(checks)}\n")

    outcomes: list[Outcome] = []
    for check in checks:
        if not check.get("why", "").strip():
            raise SystemExit(
                f"проверка {check.get('kind')} у {check.get('inn')} объявлена без "
                "основания: через полгода её нельзя будет отличить от случайной"
            )
        source = check["source"]
        found = _from_document(check) if source == "document" else _from_database(check)
        outcomes.append(found)
        print(found.describe())

    failed = [item for item in outcomes if not item.ok]
    skipped = catalog.get("not_checked") or []
    print(
        f"\nПроверено величин {len(outcomes)}, расхождений {len(failed)}; "
        f"объявлено непроверяемых {len(skipped)}"
    )
    for item in skipped:
        print(f"  не проверяется — {item['issuer']}, {item['subject']}: "
              f"{' '.join(item['reason'].split())[:140]}")
    if failed:
        print("\nОстановка: эталонная величина изменилась.")
        for item in failed:
            print(item.describe())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
