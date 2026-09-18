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
from decimal import Decimal
from pathlib import Path

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.normalize.ifrs_note_lines import load_note_lines
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import Rejection, form_headings, identify, text_of
from finlib.sources.ifrs_notes import index_notes, references_in, value_from_notes
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
    # Покрытие процентов: что даёт форма и что даёт примечание.
    operating_profit: Decimal | None = None
    interest_in_form: Decimal | None = None
    interest_accrued: Decimal | None = None
    interest_parts: tuple[str, ...] = ()
    refusal: str = ""
    gaps: tuple[int, ...] = ()
    lost_pages: tuple[int, ...] = ()

    def cover(self, interest: Decimal | None) -> Decimal | None:
        """Покрытие процентов при этой величине знаменателя."""
        if self.operating_profit is None or not interest:
            return None
        return (self.operating_profit / interest).quantize(Decimal("0.01"))

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
        profile.dates_by_form,
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

    # Покрытие процентов по действительной стоимости долга: знаменатель
    # собирается из примечаний по ссылке из строки формы, а не берётся
    # из самой формы. Отказ — исход, а не неудача.
    note_lines = load_note_lines()
    by_code = {
        item.code: item
        for form in extraction.forms.values()
        for item in form.values
        if item.report_date == profile.report_dates[0]
    }
    finance = by_code.get("ifrs.finance_costs")
    operating = by_code.get("ifrs.operating_profit")
    # Ссылки берутся у всех строк формы, названных в `found_in`: у ФосАгро
    # начисленный процентный расход стоит в примечании о финансовых расходах,
    # а капитализированный — в примечании о кредитах и облигациях, и ссылка
    # туда идёт от строки долга, а не от строки расходов.
    def references_for(line) -> tuple[int, ...]:
        found: list[int] = []
        for code in line.found_in:
            item = by_code.get(code)
            if item is not None:
                found.extend(item.note_reference)
        return tuple(dict.fromkeys(found))

    outcomes = {
        line.code: value_from_notes(
            line,
            index,
            references_for(line),
            document.text,
            profile.grouping,
            len(profile.report_dates),
        )
        for line in note_lines.for_form_line("ifrs.finance_costs")
    }
    accrued: Decimal | None = None
    parts: list[str] = []
    refusal = ""
    expense = outcomes.get("ifrs.interest_expense_accrued")
    capitalised = outcomes.get("ifrs.interest_capitalised")
    if expense is None or not expense.found:
        refusal = expense.describe() if expense is not None else "строка не заведена"
    elif _net_of_capitalised(expense, note_lines) and not (
        capitalised is not None and capitalised.found
    ):
        # Капитализированные проценты обязательны там, где строка формы
        # объявила себя очищенной от них: без них знаменатель занижен,
        # а выглядит полным.
        refusal = (
            capitalised.describe()
            if capitalised is not None
            else "капитализированные проценты не заведены"
        )
    else:
        accrued = expense.value
        parts.append(f"{expense.code} {expense.value} (прим. {expense.note})")
        if capitalised is not None and capitalised.found:
            accrued += capitalised.value
            parts.append(
                f"{capitalised.code} {capitalised.value} (прим. {capitalised.note})"
            )

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
        operating.value if operating is not None else None,
        abs(finance.value) if finance is not None else None,
        accrued if accrued else None,
        tuple(parts),
        refusal,
        gaps=index.gaps,
        lost_pages=index.lost_pages,
    )


def _net_of_capitalised(outcome, note_lines) -> bool:
    """Объявила ли себя строка формы очищенной от капитализированных процентов.

    У Норникеля «Расходы по процентам, за вычетом капитализированных
    процентов» — 537, а капитализировано 1 110, и величина раскрыта прозой
    примечания об основных средствах, а не строкой таблицы. Знаменатель
    без них занижен втрое, и показатель получает отказ.
    """
    return any(
        marker.lower() in " ".join(outcome.rows).lower()
        for marker in note_lines.interest_cover.requires_capitalised_when_net
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

    # Каждый документ папки, а не первый попавшийся: выбор молча — тот самый
    # дефект, при котором документ исчезает из замера без сообщения.
    found: list[DocumentNotes] = []
    for folder in sorted(item for item in args.path.iterdir() if item.is_dir()):
        documents = [
            item
            for item in sorted(folder.iterdir())
            if item.suffix.lower() in (".pdf", ".txt", ".md")
        ]
        if not documents:
            print(f"\n{folder.name}: документов нет — замер по папке не проводился")
            continue
        for document in documents:
            outcome = measure(document, f"{folder.name}/{document.name[:24]}")
            if isinstance(outcome, str):
                print(f"\n{folder.name} / {document.name}: замер не состоялся — {outcome}")
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
            f"наименование разошлось у {len(item.mismatched)}; "
            f"пропусков нумерации {len(item.gaps)}, "
            f"страниц без текстового слоя внутри примечаний {len(item.lost_pages)}"
        )
        for line in item.missing:
            print(f"      не найдено: {line}")
        for line in item.unexpected:
            print(f"      нет в оглавлении: {line}")
        for line in item.mismatched:
            print(f"      наименование разошлось: {line}")
        if item.gaps:
            print(
                "      пропуски нумерации: "
                + ", ".join(str(number) for number in item.gaps)
            )
        if item.lost_pages:
            print(
                "      страницы без текстового слоя внутри примечаний: "
                + ", ".join(str(number) for number in item.lost_pages)
            )

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

    print("\nПОКРЫТИЕ ПРОЦЕНТОВ: ФОРМА ПРОТИВ ПРИМЕЧАНИЙ")
    counted = 0
    for item in found:
        if item.operating_profit is None and item.interest_in_form is None:
            continue
        form_cover = item.cover(item.interest_in_form)
        note_cover = item.cover(item.interest_accrued)
        if item.interest_accrued is not None:
            counted += 1
        print(
            f"  {item.inn}: операционная прибыль {item.operating_profit}; "
            f"проценты по форме {item.interest_in_form}, "
            f"начисленные {item.interest_accrued if item.interest_accrued else '—'}"
        )
        print(
            f"      покрытие по форме {form_cover if form_cover is not None else '—'}, "
            f"по начисленным {note_cover if note_cover is not None else '—'}"
        )
        for part in item.interest_parts:
            print(f"      {part}")
        if item.refusal:
            print(f"      {item.refusal}")
    measured = [
        item
        for item in found
        if item.operating_profit is not None or item.interest_in_form is not None
    ]
    print(
        f"  показатель посчитан у {counted} из {len(measured)}, "
        f"отказ у {len(measured) - counted}"
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
