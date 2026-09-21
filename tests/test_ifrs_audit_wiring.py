"""Тест проводки аудиторского заключения: прочитанное обязано доходить до базы.

Шесть кодов заключения (задача 25) были написаны, покрыты тестами разбора
и не появились ни у одного комплекта: в `dq_log` по ним ноль записей на всю
базу. Причина не в разборе — заключение читал цикл, а базу наполнял прогон
приёма, и он передавал записи только извлечение. Довод с умолчанием
пропускается молча, и ноль записей выглядел как «оговорок нет».

Проверяется и то и другое: что записи появляются, и что ни один боевой
вызов записи не обходится без прочитанного в документе.
"""

import ast
from datetime import date
from pathlib import Path

import pytest

from finlib.db import execute, fetch_all
from finlib.normalize.ifrs_loader import load_extraction
from finlib.sources.ifrs_audit import AuditReport, Determination, Engagement
from finlib.sources.ifrs_document import DocumentReading
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import identify
from finlib.sources.ifrs_numbers import Grouping
from finlib.sources.ifrs_review import review

ROOT = Path(__file__).resolve().parent.parent

INN = "7736050003"
DATES = (date(2024, 12, 31), date(2023, 12, 31))

BALANCE = """
Консолидированный отчёт о финансовом положении
(в миллионах российских рублей)
Пояснения      31 декабря 2024 года      31 декабря 2023 года
Основные средства                       700 000        650 000
Итого внеоборотные активы               700 000        650 000
Запасы                                  300 000        280 000
Денежные средства и их эквиваленты      500 000        430 000
Итого оборотные активы                  800 000        710 000
Итого активы                          1 500 000      1 360 000
Акционерный капитал                     400 000        400 000
Нераспределённая прибыль                200 000        160 000
Итого капитал                           600 000        560 000
Долгосрочные кредиты и займы            500 000        500 000
Итого долгосрочные обязательства        500 000        500 000
Краткосрочные кредиты и займы           400 000        300 000
Итого краткосрочные обязательства       400 000        300 000
Итого обязательства                     900 000        800 000
Итого капитал и обязательства         1 500 000      1 360 000

Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка                               1 200 000      1 100 000
Себестоимость продаж                    (800 000)      (750 000)
Валовая прибыль                         400 000        350 000
Операционная прибыль                    300 000        260 000
Прибыль до налогообложения              260 000        220 000
Расход по налогу на прибыль              (52 000)       (44 000)
Прибыль за период                       208 000        176 000
"""

CONTENTS = """
Содержание
Консолидированный отчёт о финансовом положении 3
Консолидированный отчёт о прибыли или убытке 4
Примечания к консолидированной финансовой отчётности 5
"""

HEADER = (
    "\n(в миллионах российских рублей)\n"
    "по состоянию на 31 декабря 2024 года и 31 декабря 2023 года\n"
    + "\nПримечания к консолидированной финансовой отчётности.\n" * 40
)

# Мнение с оговоркой, как у годового комплекта ФосАгро: раздел «Основание
# для выражения мнения» назван, вид мнения модифицирован.
QUALIFIED = AuditReport(
    Determination.DETERMINED,
    engagement=Engagement.AUDIT,
    opinion="qualified",
    opinion_name="Мнение с оговоркой",
    modified=True,
    sections=("basis_for_opinion",),
)


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute(
        "DELETE FROM src_file WHERE inn = %(i)s AND standard = 'ifrs'",
        {"i": INN},
        conn=db_conn,
    )
    return db_conn


def prepared():
    """Документ, проведённый через приём, разбор и сверку."""
    profile = identify(CONTENTS + BALANCE + HEADER)
    assert profile.accepted, getattr(profile, "reason", "")
    extraction = extract(BALANCE, DATES, Grouping.RUSSIAN)
    return extraction, profile, review(extraction, profile)


def test_modified_opinion_reaches_the_journal(db_conn) -> None:
    """Мнение с оговоркой пишется в журнал комплекта вместе с видом мнения."""
    extraction, profile, decision = prepared()
    result = load_extraction(
        INN,
        extraction,
        profile,
        decision,
        db_conn,
        DocumentReading(audit=QUALIFIED, notes=(), issuer_type=None),
    )

    rows = fetch_all(
        "SELECT check_code, severity, message, details FROM dq_log "
        "WHERE src_file_id = %(id)s AND check_code LIKE 'audit%%'",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert [row["check_code"] for row in rows] == ["audit_opinion_modified"]
    assert "Мнение с оговоркой" in rows[0]["message"]
    assert rows[0]["details"]["opinion"] == "qualified"


def test_absent_report_is_a_record_of_its_own(db_conn) -> None:
    """Отсутствие заключения — своя запись, а не молчание.

    Комплект без записей о заключении выглядит проверенным и чистым, а это
    не то же самое, что «заключения нет».
    """
    extraction, profile, decision = prepared()
    result = load_extraction(
        INN,
        extraction,
        profile,
        decision,
        db_conn,
        DocumentReading(
            audit=AuditReport(Determination.ABSENT), notes=(), issuer_type=None
        ),
    )

    rows = fetch_all(
        "SELECT check_code FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code LIKE 'audit%%'",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert [row["check_code"] for row in rows] == ["audit_report_absent"]


def load_calls() -> list[tuple[str, int, bool]]:
    """Боевые вызовы записи комплекта и признак, передано ли прочитанное."""
    found: list[tuple[str, int, bool]] = []
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "eval").rglob("*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Name) and node.func.id == "load_extraction"
            ):
                continue
            passed = len(node.args) >= 6 or any(
                word.arg == "reading" for word in node.keywords
            )
            found.append((str(path.relative_to(ROOT)), node.lineno, passed))
    return found


def test_every_caller_passes_what_was_read_in_the_document() -> None:
    """Прочитанное в документе передаётся всеми боевыми вызовами записи.

    Проверка структурная: ищется довод у самого вызова в дереве разбора.
    Упоминание чтения в модуле не означает, что оно дошло до записи, — именно
    так шесть кодов заключения и не дошли ни до одного комплекта.
    """
    calls = load_calls()
    assert len(calls) >= 2, calls
    silent = {path for path, _, passed in calls if not passed}
    assert not silent, f"запись комплекта без прочитанного в документе: {sorted(silent)}"


def test_reading_is_obtained_by_reading() -> None:
    """Боевой код не набирает прочитанное руками, а читает его одной функцией.

    Иначе довод есть, а сведений в нём нет: `DocumentReading(audit=None, …)`
    удовлетворяет проверке выше и означает «не читали».
    """
    allowed = {"src/finlib/sources/ifrs_document.py"}
    built: list[str] = []
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "eval").rglob("*.py")]):
        relative = str(path.relative_to(ROOT))
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "DocumentReading"
            ):
                built.append(f"{relative}:{node.lineno}")
    assert not built, f"прочитанное набрано руками: {built}"
