"""Замер по примечаниям: указатель, сверка с оглавлением, переход по ссылке.

Отвечает на вопрос, от которого зависит устройство разбора примечаний:
**у какой доли строк форм есть ссылка на примечание**. Если ссылка стоит
почти везде, справочник строк примечаний нужен маленький — переход идёт
по номеру. Если ссылка редкость, опираться придётся на наименования,
и справочник будет другой.

Рядом — сверка указателя с оглавлением: оглавление даёт независимый
перечень примечаний, и расхождение означает потерю, а не придирку.
У Автодора и ЛСР есть страницы без текстового слоя, и примечание,
попавшее на такую страницу, иначе о себе не заявит.

    uv run python eval/ifrs_notes_run.py

Ничего не пишет ни в базу, ни на диск: это замер, а не загрузка.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import Rejection, form_headings, identify, text_of
from finlib.sources.ifrs_notes import index_notes, references_in
from finlib.sources.ifrs_numbers import load_parsing_policy

logger = logging.getLogger(__name__)

# Строки, ради расшифровки которых задача и делается. Доля ссылок по ним
# решает устройство разбора: если ссылка стоит у них, справочник строк
# примечаний нужен маленький — переход идёт по номеру. Общая доля по всем
# строкам формы на это не отвечает: у итогов и заголовков разделов
# примечаний не бывает вовсе, и они только разбавляют счёт.
KEY_CODES: tuple[str, ...] = (
    "ifrs.depreciation",
    "ifrs.finance_costs",
    "ifrs.interest_paid",
    "ifrs.long_term_borrowings",
    "ifrs.short_term_borrowings",
    "ifrs.cash",
)


@dataclass(frozen=True, slots=True)
class DocumentNotes:
    """Итог замера по одному документу."""

    name: str
    inn: str
    notes: int
    declared: int
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    mismatched: tuple[str, ...]
    rows: int
    rows_with_reference: int
    references_resolved: int
    references_dangling: tuple[str, ...]
    # Ключевые строки: те, ради расшифровки которых задача и делается.
    key_rows: tuple[tuple[str, tuple[int, ...], str], ...] = ()

    @property
    def share(self) -> float | None:
        """Доля строк форм, у которых нашлась ссылка; None — строк нет."""
        return None if not self.rows else self.rows_with_reference / self.rows


def _percent(value: float | None) -> str:
    """Доля процентом с запятой; прочерк — измерять было нечего."""
    return "—" if value is None else f"{value * 100:.1f} %".replace(".", ",")


def measure(path: Path, inn: str) -> DocumentNotes | str:
    """Проводит один документ через указатель примечаний и переход по ссылке."""
    document = text_of(path)
    if not document.readable:
        return f"файл не прочитан: {document.error}"
    profile = identify(document.text, any_currency=True, document=document)
    # Примечания начинаются после форм: до них нумерованные абзацы стоят
    # и в аудиторском заключении, и принимать их за примечания нельзя.
    # Отсчёт ведётся от **первой** формы, а не от последней: нумерованные
    # абзацы аудиторского заключения стоят до форм, а наименование формы
    # встречается и внутри примечаний — у Автодора движение денежных средств
    # упоминается в примечании, и отсчёт от последнего вхождения срезал
    # девятнадцать примечаний из тридцати.
    headings = form_headings(document.text, load_ifrs_lines(), load_parsing_policy())
    index = index_notes(
        document.text, document, after=min(headings.values(), default=0)
    )
    if isinstance(profile, Rejection):
        # Документ вне периметра методики примечания всё равно содержит,
        # и указатель по нему состоятелен: устройство документа от периметра
        # не зависит. Строки форм при этом не считаются — формы не разбирались,
        # и ноль здесь означал бы «ссылок нет», а не «не измеряли».
        return DocumentNotes(
            path.name,
            inn,
            len(index.notes),
            len(index.contents),
            tuple(item.describe() for item in index.missing),
            tuple(item.describe() for item in index.unexpected),
            tuple(item.describe() for item in index.mismatched),
            0,
            0,
            0,
            (),
        )

    extraction = extract(
        document.text,
        profile.report_dates,
        profile.grouping,
        columns=document.columns_of,
    )

    # Ссылка берётся из разбора строки, а не ищется в наименовании заново:
    # номер примечания отделяется от величин при разборе, и второе
    # определение того же понятия разошлось бы с первым.
    rows: list[tuple[str, tuple[int, ...]]] = []
    for form in extraction.forms.values():
        seen: set[str] = set()
        for item in form.values:
            if item.source_name in seen:
                continue
            seen.add(item.source_name)
            rows.append((item.source_name, item.note_reference))
        rows.extend(
            (row.source_name, row.note_reference) for row in form.unrecognised
        )

    with_reference = 0
    resolved = 0
    dangling: list[str] = []
    for name, from_row in rows:
        references = from_row or references_in(name)
        if not references:
            continue
        with_reference += 1
        known = [number for number in references if index.get(number) is not None]
        if known:
            resolved += 1
        else:
            dangling.append(f"«{name}» → {', '.join(str(item) for item in references)}")

    key_rows: list[tuple[str, tuple[int, ...], str]] = []
    for code in KEY_CODES:
        item = next(
            (
                value
                for form in extraction.forms.values()
                for value in form.values
                if value.code == code
            ),
            None,
        )
        if item is None:
            continue
        note = next(
            (
                index.get(number).title
                for number in item.note_reference
                if index.get(number) is not None
            ),
            "",
        )
        key_rows.append((code, item.note_reference, note))

    return DocumentNotes(
        path.name,
        inn,
        len(index.notes),
        len(index.contents),
        tuple(item.describe() for item in index.missing),
        tuple(item.describe() for item in index.unexpected),
        tuple(item.describe() for item in index.mismatched),
        len(rows),
        with_reference,
        resolved,
        tuple(dangling),
        tuple(key_rows),
    )


def main(argv: list[str] | None = None) -> int:
    """Печатает замер по примечаниям; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    load_ifrs_lines()
    policy = load_parsing_policy().notes
    print(
        f"правила примечаний: заголовок до {policy.heading_max_length} знаков, "
        f"номер до {policy.max_number}, разрыв до {policy.max_number_gap}, "
        f"оглавление от {policy.contents_min_entries} записей"
    )

    found: list[DocumentNotes] = []
    for folder in sorted(item for item in args.path.iterdir() if item.is_dir()):
        documents = [
            item for item in sorted(folder.iterdir()) if item.suffix.lower() == ".pdf"
        ]
        if not documents:
            continue
        outcome = measure(documents[0], folder.name)
        if isinstance(outcome, str):
            print(f"\n{folder.name}: замер не состоялся — {outcome}")
            continue
        found.append(outcome)

    if not found:
        print(
            "\nдокументов не найдено: доля строк со ссылкой не измерена. "
            "Печатать ноль здесь значило бы выдать отсутствие данных "
            "за результат."
        )
        return 1

    print("\nУКАЗАТЕЛЬ ПРИМЕЧАНИЙ И СВЕРКА С ОГЛАВЛЕНИЕМ")
    for item in found:
        print(
            f"  {item.inn}: найдено {item.notes}, объявлено оглавлением "
            f"{item.declared}, не найдено {len(item.missing)}, "
            f"нет в оглавлении {len(item.unexpected)}, "
            f"наименование разошлось у {len(item.mismatched)}"
        )
        for line in item.missing:
            print(f"      не найдено: {line}")
        for line in item.unexpected:
            print(f"      нет в оглавлении: {line}")
        for line in item.mismatched:
            print(f"      наименование разошлось: {line}")

    print("\nССЫЛКА ИЗ СТРОКИ ФОРМЫ В ПРИМЕЧАНИЕ")
    rows = sum(item.rows for item in found)
    with_reference = sum(item.rows_with_reference for item in found)
    resolved = sum(item.references_resolved for item in found)
    for item in found:
        print(
            f"  {item.inn}: строк форм {item.rows}, со ссылкой "
            f"{item.rows_with_reference} ({_percent(item.share)}), "
            f"ссылка ведёт к найденному примечанию у {item.references_resolved}"
        )
        for line in item.references_dangling:
            print(f"      ссылка в никуда: {line}")
    share = with_reference / rows if rows else None
    print(
        f"  всего: строк {rows}, со ссылкой {with_reference} ({_percent(share)}), "
        f"из них разрешились {resolved}"
    )

    print("\nКЛЮЧЕВЫЕ СТРОКИ И ИХ ПРИМЕЧАНИЯ")
    checked = 0
    referenced = 0
    for item in found:
        if not item.key_rows:
            continue
        print(f"  {item.inn}:")
        for code, references, title in item.key_rows:
            checked += 1
            if references:
                referenced += 1
            listed = ", ".join(str(number) for number in references) or "ссылки нет"
            print(f"      {code}: примечание {listed}" + (f" — {title}" if title else ""))
    if not checked:
        print("  ключевых строк не извлечено: измерять нечего")
    else:
        print(
            f"  ссылка есть у {referenced} из {checked} ключевых строк "
            f"({_percent(referenced / checked)})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
