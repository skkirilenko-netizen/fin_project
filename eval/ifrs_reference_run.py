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


def _parsed(path: Path, inn: str):
    """Разбор документа **боевым путём**; None — документ не принят.

    Прогон не повторяет шаги цикла: приём, извлечение, подтверждённое
    опознание и чтение документа делает `accept_ifrs_document`, а прогон
    только сравнивает ответ с эталоном. Иначе эталон сверялся бы со второй
    системой — ровно то, из-за чего он и понадобился.

    `write=False`: эмитент назван, потому что ранее подтверждённое опознание
    без него не находится, а база не трогается вовсе.
    """
    from finlib.pipeline import accept_ifrs_document
    from finlib.sources.ifrs_inbox import text_of

    document = text_of(path)
    if not document.readable:
        return None, None, f"файл не прочитан: {document.error}"
    intake = accept_ifrs_document(
        document.text, inn=inn, raw_path=str(path), document=document, write=False
    )
    if not intake.accepted:
        return None, None, intake.reason or "документ не принят"
    return document, intake, ""


def _note_value(intake, code: str) -> Decimal | None:
    """Величина из примечания — из того же чтения, что записал цикл.

    Прежде прогон читал примечания сам: указатель, ссылки, графы. Это второй
    разбор того же документа, и он мог бы отвечать иначе, чем тот, который
    наполняет базу, — тогда эталон подтверждал бы не то, что работает.
    """
    notes = getattr(intake.reading, "notes", ())
    return next(
        (item.value for item in notes if item.code == code and item.found), None
    )


def _from_document(check: dict) -> Outcome:
    """Проверка, которой база не нужна."""
    inn, issuer = check["inn"], check.get("issuer", "")
    kind, subject = check["kind"], check.get("subject") or check.get("code", kind := check["kind"])
    path = DOCUMENTS / inn / check["document"]
    if not path.exists():
        return Outcome(inn, issuer, kind, subject, str(check.get("expect")), "документа нет", False)
    _document, intake, reason = _parsed(path, inn)
    if intake is None:
        return Outcome(inn, issuer, kind, subject, str(check.get("expect")), reason, False)
    profile, extraction = intake.profile, intake.extraction
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
        value = _note_value(intake, check["code"])
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

_SIGNALS = """
SELECT s.signal_code, s.level FROM assessment_signal s
JOIN assessment a ON a.id = s.assessment_id
WHERE a.inn = %(inn)s AND a.standard = 'ifrs' AND a.report_date = %(date)s
"""

_GROUPS = """
SELECT g.group_name FROM assessment_group g
JOIN assessment a ON a.id = g.assessment_id
WHERE a.inn = %(inn)s AND a.standard = 'ifrs' AND a.report_date = %(date)s
"""

_AUDIT = """
SELECT meta -> 'audit' AS audit FROM src_file
WHERE inn = %(inn)s AND standard = 'ifrs' AND is_actual
  AND report_year = %(year)s AND status <> 'quarantine'
"""


def _proposed_caveat(audit: dict) -> str:
    """Вид оговорки: подтверждённый человеком либо предложенный машиной.

    Вид восстанавливается тем же чтением, что и в документе, а не сравнением
    слов здесь: формулировки и приметы правятся в методике, и второй разбор
    основания разошёлся бы с первым.
    """
    from finlib.normalize.ifrs_audit import load_audit_policy
    from finlib.sources.ifrs_audit import audit_from_meta

    found = audit_from_meta({"audit": audit})
    if found is None:
        return "заключения нет"
    policy = load_audit_policy()
    return found.caveat_kind or found.proposed_caveat_kind(policy) or "вид не назван"


def _from_database(check: dict) -> Outcome:
    """Проверка, которой нужна загруженная и подтверждённая база."""
    from datetime import date as date_type

    from finlib.db import connection, fetch_all, fetch_one

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
        if kind == "audit_signal":
            # Сигнал заключения в разделе 4 — он идёт не через `assessment_signal`,
            # а из сведений комплекта: формулировка берётся из методики в момент
            # сборки, а признак объявлен разделом заключения.
            row = fetch_one(_AUDIT, {"inn": inn, "year": moment.year}, conn=conn)
            sections = ((row or {}).get("audit") or {}).get("sections") or []
            return Outcome(
                inn, issuer, kind, subject, expected,
                ", ".join(sections) or "заключения нет", expected in sections,
            )
        if kind == "caveat_kind":
            row = fetch_one(_AUDIT, {"inn": inn, "year": moment.year}, conn=conn)
            audit = (row or {}).get("audit") or {}
            got = _proposed_caveat(audit)
            return Outcome(inn, issuer, kind, subject, expected, got, got == expected)
        if kind == "signal_absent":
            rows = fetch_all(_SIGNALS, {"inn": inn, "date": moment}, conn=conn)
            fired = sorted(item["signal_code"] for item in rows)
            code = check["code"]
            return Outcome(
                inn, issuer, kind, subject, str(expected),
                ", ".join(fired) or "признаков нет", code not in fired,
            )
        if kind == "group_out_of_score":
            rows = fetch_all(_GROUPS, {"inn": inn, "date": moment}, conn=conn)
            groups = sorted(item["group_name"] for item in rows)
            return Outcome(
                inn, issuer, kind, subject, f"{expected} вне балла",
                ", ".join(groups) or "оценки нет", expected not in groups,
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
