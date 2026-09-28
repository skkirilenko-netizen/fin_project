"""Разведка уровня 2: примечание «Кредиты и займы» и таблица сроков погашения в нём.

    uv run python eval/ifrs_debt_note_probe.py > data/output/ifrs_debt_note_probe.md

**Только разведка, в боевой путь не идёт и ничего не пишет.** Примечание
ищется по ссылке из строки формы — номеру при «Долгосрочных / Краткосрочных
кредитах и займах», который разбор форм уже читает (`note_reference`),
и по указателю примечаний, построенному строением (`ifrs_notes.index_notes`).
Поиск по наименованию примечания не делается вовсе.

Таблица сроков внутри примечания ищется признаками периода в строке — годы
погашения либо интервалы «до / от … до / свыше … лет». Это поиск по тексту
**внутри уже найденного примечания**, и прогон печатает найденные строки
дословно: как выглядит таблица, решает чтение, а не правило.
"""

import logging
import re
import sys
from pathlib import Path

from finlib.pipeline import accept_ifrs_document
from finlib.sources.ifrs_inbox import form_headings, text_of
from finlib.sources.ifrs_notes import index_notes, lines_of

logger = logging.getLogger(__name__)

ROOT = Path("data/raw/ifrs")
ISSUERS = ("6685151087", "7717151380", "7826087713", "7838360491", "9703024202", "9731004688")
DEBT = ("ifrs.long_term_borrowings", "ifrs.short_term_borrowings")

# Признак строки таблицы сроков: год погашения либо интервал срока.
_PERIOD = re.compile(
    r"(20[2-4]\d\s*(?:г|год)?)|(до\s+(?:1|одного|3|6|12)\s)|(от\s+\d+\s+до\s+\d+)"
    r"|(свыше|более)\s+\d+\s+(?:лет|года|мес)|(в течение\s+\d+)|(менее\s+\d+)",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"\d[\d\s]{2,}")


def _probe(inn: str, path: Path) -> None:
    """Печатает ссылку, примечание и строки-кандидаты таблицы сроков."""
    document = text_of(path)
    print(f"## {inn} · {path.name}\n")
    if not document.readable:
        print(f"файл не прочитан: {document.error}\n")
        return
    intake = accept_ifrs_document(
        document.text, inn=inn, raw_path=str(path), document=document, write=False
    )
    if not intake.accepted:
        print(f"отклонён приёмом: {intake.reason}\n")
        return
    references = sorted(
        {
            number
            for item in intake.extraction.values
            if item.code in DEBT
            for number in item.note_reference
        }
    )
    print(f"Ссылки из строк долга формы: {references or 'нет'}.")
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.sources.ifrs_numbers import load_parsing_policy

    headings = form_headings(document.text, load_ifrs_lines(), load_parsing_policy())
    index = index_notes(document.text, document, after=min(headings.values(), default=0))
    print(f"Указатель: {index.describe()}.\n")
    if not references:
        print("Ссылки нет — примечание по строению не находится.\n")
        return
    for number in references:
        note = index.get(number)
        if note is None:
            print(f"- примечание {number}: в тексте не найдено\n")
            continue
        lines = lines_of(note, document.text)
        rows = [line for line in lines if _PERIOD.search(line) and _DIGITS.search(line)]
        print(f"### Примечание {note.describe()}: строк {len(lines)}\n")
        print(f"Строк с признаком срока и величинами: {len(rows)}.\n")
        for line in rows[:12]:
            print(f"    {line[:140]}")
        print()


def main() -> int:
    """Разведка по шести эмитентам «Разбора» с PDF."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    print("# Разведка: примечание о долге и таблица сроков погашения\n")
    for inn in ISSUERS:
        for path in sorted((ROOT / inn).glob("*.pdf")):
            _probe(inn, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
