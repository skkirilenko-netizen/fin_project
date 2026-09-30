"""Пакет для экспертной проверки списка: «Разбор», «Без внимания» и выборка.

    uv run python eval/expert_review_run.py [--seed ЧИСЛО]

Критерий выхода ROADMAP — «экспертная проверка не находит критичных
дефектов два раза подряд»; пакет — материал одной такой проверки.
**Только чтение**: строки даёт боевая маршрутизация на сегодня
(`routing_rows`), транзакция откатывается, в базу не пишется ничего.
Файл — `data/output/expert_review_<дата>.xlsx`.

Листы:

- «Разбор» — все эмитенты корзины с основаниями, величинами и ссылкой
  на карточку (`cards/<ИНН>.md` рядом с файлом);
- «Без внимания» — то же, на полный просмотр: у каждой строки графы
  вердикта эксперта (решение владельца 30.09.2026);
- «Выборка» — 30 из «Без внимания» на углублённую проверку, с основанием
  отбора и графами вердикта;
- «Сводка» — числа отбора, знаменатели и зерно.

**Состав выборки — решение владельца 30.09.2026.** Сперва все эмитенты
«Без внимания» с крупным долгом по облигациям — тем же, что у маршрута:
верхняя доля по объёму облигаций в обращении (`routing.yaml`,
`systemic.top_share`), тем же кодом (`routing_store._outstanding`,
`_top_share`). Затем до тридцати — эмитенты с наибольшим долгом
по отчётности: совокупный долг базы маршрута (`debt_total`, займы LTM-базы),
приведённый к рублям, потому что единица комплекта у эмитентов разная.
Это отбор для проверки, а не правило маршрута, и в методику он не вносится.
Эмитент без раскрытого долга в добор не идёт, и их число названо в сводке.

**Отбор детерминирован**: добор — по убыванию долга, ничья — по ИНН.
Зерно (по умолчанию число из даты, ГГГГММДД) печатается в сводке, но
случайности в этом отборе нет, и сводка говорит это прямо.
"""

import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchlist_csv import VALUES, _event_dates  # noqa: E402

from finlib.db import connection  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import (  # noqa: E402
    _outstanding,
    _top_share,
    routing_rows,
)
from finlib.sources.cbonds_events import OKEI_MULTIPLIER  # noqa: E402
from finlib.sources.market import universe  # noqa: E402

logger = logging.getLogger(__name__)

OUT = Path("data/output")
CARDS = OUT / "cards"
SAMPLE_SIZE = 30
# Долг по отчётности — величина маршрута своего стандарта: совокупные
# заёмные средства базы (LTM на промежуточную дату либо годовая).
DEBT = "debt_total"
BASKETS = (("review", "Разбор"), ("clear", "Без внимания"))

HEADER = (
    "ИНН",
    "наименование",
    "подгруппа",
    "действие",
    "коды оснований",
    "основания",
    "справочные основания",
    "отчётная дата",
    "стандарт",
    "база",
    "единица",
    "класс по документу",
    "тип эмитента",
    "крупный долг (верхняя доля по облигациям)",
    "объём облигаций в обращении, руб.",
    "даты событий дефолта",
    *VALUES,
    "карточка",
)
VERDICT = (
    "вердикт эксперта (согласен / не согласен)",
    "критичный дефект (да / нет)",
    "комментарий",
)


def _row(item, volume, systemic: bool) -> list:  # noqa: ANN001
    """Строка листа: те же поля, что у выгрузки списка наблюдения."""
    verdict = item.verdict
    printed = {code: shown for code, _, shown in item.shown_values}
    return [
        item.inn,
        item.name,
        "; ".join(verdict.subgroup_names),
        "; ".join(verdict.actions),
        "; ".join(entry.ground for entry in verdict.findings),
        " | ".join(entry.text for entry in verdict.findings),
        " | ".join(entry.text for entry in verdict.notes),
        f"{item.report_date:%Y-%m-%d}" if item.report_date is not None else "",
        item.standard.value if item.standard is not None else "",
        item.basis_note or item.basis,
        item.unit,
        item.assessed_class,
        item.issuer_type,
        "да" if systemic else "",
        f"{volume:,.0f}".replace(",", " ") if volume is not None else "",
        _event_dates(item),
        *[printed.get(code, "") for code in VALUES],
        "",
    ]


def _debt_rub(item) -> Decimal | None:  # noqa: ANN001
    """Совокупный долг базы маршрута в рублях; не раскрыт или единица неизвестна — None."""
    found = next((value for value in item.computed if value.code == DEBT), None)
    if found is None or found.value is None:
        return None
    multiplier = OKEI_MULTIPLIER.get(str(item.unit_code))
    return None if multiplier is None else found.value * multiplier


def _sheet(book: Workbook, title: str, rows: list, extra: tuple[str, ...] = ()) -> None:
    """Лист с шапкой, ИНН текстом и ссылкой на карточку, если она на диске."""
    sheet = book.create_sheet(title)
    sheet.append([*HEADER, *extra])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    link_col = len(HEADER)
    for number, (item, values) in enumerate(rows, start=2):
        sheet.append([*values, *[""] * len(extra)])
        # ИНН текстом: ведущий ноль у четверти организаций.
        sheet.cell(row=number, column=1).number_format = "@"
        card = CARDS / f"{item.inn}.md"
        if card.exists():
            cell = sheet.cell(row=number, column=link_col)
            cell.value = f"cards/{item.inn}.md"
            cell.hyperlink = f"cards/{item.inn}.md"
            cell.font = Font(color="0563C1", underline="single")
    sheet.freeze_panes = "C2"


def main() -> int:
    """Пишет пакет и печатает сводку."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else int(
        f"{today:%Y%m%d}"
    )
    routing = load_routing()
    with connection() as conn:
        rows, counts = routing_rows(conn, today)
        conn.rollback()
    volumes = {
        inn: total
        for inn in universe()
        if (total := _outstanding(inn)) is not None and total > 0
    }
    systemic = _top_share(volumes, routing.systemic.top_share)
    by_basket = {
        code: [item for item in rows if item.verdict.basket == code] for code, _ in BASKETS
    }

    book = Workbook()
    book.remove(book.active)
    for code, title in BASKETS:
        chosen = sorted(by_basket[code], key=lambda item: item.name)
        _sheet(
            book,
            title,
            [(item, _row(item, volumes.get(item.inn), item.inn in systemic)) for item in chosen],
            VERDICT if code == "clear" else (),
        )
    pool = sorted(
        (item for item in by_basket["clear"] if item.inn in systemic), key=lambda item: item.inn
    )
    others = [item for item in by_basket["clear"] if item.inn not in systemic]
    debts = {item.inn: _debt_rub(item) for item in others}
    ranked = sorted(
        (item for item in others if debts[item.inn] is not None),
        key=lambda item: (-debts[item.inn], item.inn),
    )
    unranked = len(others) - len(ranked)
    added = ranked[: max(SAMPLE_SIZE - len(pool), 0)]
    picked = [*pool, *added]
    _sheet(
        book,
        "Выборка",
        [(item, _row(item, volumes.get(item.inn), item.inn in systemic)) for item in picked],
        ("основание отбора", *VERDICT),
    )
    sample = book["Выборка"]
    for number, item in enumerate(picked, start=2):
        sample.cell(row=number, column=len(HEADER) + 1).value = (
            "крупный долг по облигациям"
            if item.inn in systemic
            else f"долг по отчётности, место {ranked.index(item) + 1}"
        )

    carded = sum(1 for item in rows if (CARDS / f"{item.inn}.md").exists())
    summary = [
        ("Дата маршрута", f"{today:%d.%m.%Y}"),
        ("Эмитентов в маршруте", len(rows)),
        ("из них с карточкой на диске", carded),
        ("«Разбор»", len(by_basket["review"])),
        ("«Без внимания»", len(by_basket["clear"])),
        (
            "Крупный долг",
            f"верхние {routing.systemic.top_share:.0%} эмитентов по объёму облигаций "
            f"в обращении (routing.yaml, systemic.top_share): {len(systemic)} "
            f"из {len(volumes)} с раскрытым объёмом",
        ),
        ("«Без внимания» с крупным долгом по облигациям", len(pool)),
        (
            "Добор по долгу по отчётности",
            f"{len(added)} с наибольшим совокупным долгом базы маршрута ({DEBT}, "
            f"в рублях) из {len(ranked)} «Без внимания» без крупного долга "
            f"по облигациям; долг не раскрыт у {unranked} — в добор не идут",
        ),
        ("В выборке", len(picked)),
        (
            "Нехватка выборки",
            f"нужно {SAMPLE_SIZE}, есть {len(picked)}" if len(picked) < SAMPLE_SIZE else "нет",
        ),
        ("Отбор", "решение владельца 30.09.2026; отбор для проверки, не правило маршрута"),
        ("Зерно", f"{seed}; в отборе не участвует — добор по убыванию долга, ничья по ИНН"),
        ("Источник", "routing_rows на дату, только чтение; величины — как на странице наблюдения"),
    ]
    sheet = book.create_sheet("Сводка", 0)
    for label, value in summary:
        sheet.append([label, value])
    sheet.column_dimensions["A"].width = 40
    sheet.column_dimensions["B"].width = 100

    out = OUT / f"expert_review_{today:%Y-%m-%d}.xlsx"
    book.save(str(out))
    print(f"{out}")
    for label, value in summary:
        print(f"- {label}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
