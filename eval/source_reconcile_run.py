"""Сверка агрегатора с документом МСФО: сколько величин Cbonds совпадает с PDF.

    uv run python eval/source_reconcile_run.py 9703024202 7838360491 ...
    uv run python eval/source_reconcile_run.py --review   # эмитенты «Разбора» с PDF
    uv run python eval/source_reconcile_run.py --review --write   # и запись

Документ проходит приём боевым циклом без записи
(`accept_ifrs_document(write=False)`), строка агрегатора читается из кэша
доставки тем путём, что её грузит загрузка (`cbonds_loader.read_row`), сверку
делает `quality.reconcile`. **Пишет прогон только с `--write`** и только
в `source_reconciliation` (схема согласована 29.09.2026): строка на величину
одной даты одного документа, отсутствие стороны — тоже строка. Оттуда
заключение уровня 1 берёт долю совпавших в отчётной колонке.

**Сверяются все отчётные даты документа**, а не только отчётная: колонка
прошлого года — та же величина, что строка агрегатора за прошлый год,
и её расхождение говорит о том же.
"""

import contextlib
import logging
import sys
from collections import Counter
from pathlib import Path

from finlib.db import connection, fetch_all
from finlib.pipeline import accept_ifrs_document
from finlib.quality.reconcile import (
    Outcome,
    aggregator_codes,
    aggregator_reading,
    reconcile,
    record,
)
from finlib.sources import cbonds
from finlib.sources.ifrs_inbox import text_of

logger = logging.getLogger(__name__)

ROOT = Path("data/raw/ifrs")

# Корзина каждого эмитента на последнюю точку прогона.
_LATEST = """
SELECT DISTINCT ON (inn) inn, basket FROM routing_history
WHERE kind = 'run' ORDER BY inn, as_of DESC, id DESC
"""

# Исходы, которые печатаются строкой таблицы: совпадение и отсутствие
# с одной стороны сводятся счётчиком.
_SHOWN = (Outcome.DIFFER, Outcome.SIGN)


def _review_with_pdf() -> list[str]:
    """ИНН «Разбора» с каталогом документов."""
    here = {path.name for path in ROOT.iterdir() if path.is_dir()}
    return sorted(
        row["inn"]
        for row in fetch_all(_LATEST, {})
        if row["basket"] == "review" and row["inn"] in here
    )


def _money(value) -> str:  # noqa: ANN001
    """Величина с разделителем разрядов; пусто — прочерк."""
    return "—" if value is None else f"{value:,.0f}".replace(",", " ")


def _document(
    inn: str,
    path: Path,
    rows: list[dict],
    kinds: dict[str, str],
    total: Counter[str],
    by_code: dict[str, Counter[str]],
    conn=None,  # noqa: ANN001 — соединение для записи; None — прогон без записи
) -> None:
    """Сверяет один документ по всем его отчётным датам и печатает итог."""
    codes = tuple(sorted(kinds))
    document = text_of(path)
    if not document.readable:
        print(f"## {inn} · {path.name}\n\nфайл не прочитан: {document.error}\n")
        return
    intake = accept_ifrs_document(
        document.text, inn=inn, raw_path=str(path), document=document, write=False
    )
    if not intake.accepted:
        print(f"## {inn} · {path.name}\n\nотклонён приёмом: {intake.reason}\n")
        return
    profile, extraction = intake.profile, intake.extraction
    print(
        f"## {inn} · {path.name}\n\nВид {profile.reporting_kind.value}, "
        f"единица документа {profile.unit_code}.\n"
    )
    for moment in sorted(set(profile.report_dates), reverse=True):
        reading = aggregator_reading(rows, inn, moment)
        if reading is None:
            print(f"- {moment:%d.%m.%Y}: строки агрегатора на дату нет\n")
            continue
        if reading.rejection is not None:
            print(
                f"- {moment:%d.%m.%Y}: строка агрегатора отвергнута загрузкой — "
                f"{reading.rejection}\n"
            )
            continue
        ours = {
            item.code: item.value for item in extraction.values if item.report_date == moment
        }
        found = reconcile(
            ours, profile.unit_code, reading.values, reading.unit_code, moment, codes
        )
        counted = Counter(item.outcome.value for item in found)
        total.update(counted)
        # **Отчётная колонка и сравнительная — разные вопросы.** Сравнительная
        # колонка документа бывает пересчитана эмитентом, а строка агрегатора
        # за прошлый год взята из прошлогодней отчётности: их расхождение —
        # пересмотр, а не ошибка источника.
        role = "отчётная" if moment == max(profile.report_dates) else "сравнительная"
        if conn is not None:
            record(
                inn,
                str(path),
                "reporting" if role == "отчётная" else "comparative",
                found,
                kinds,
                conn,
            )
        total.update({f"{role}|{name}": count for name, count in counted.items()})
        print(f"### {moment:%d.%m.%Y} — агрегатор в единице {reading.unit_code}\n")
        print(", ".join(f"{name} {count}" for name, count in counted.most_common()) + "\n")
        shown = [item for item in found if item.outcome in _SHOWN]
        if shown:
            print("| Код | Род у агрегатора | Документ | Агрегатор | Исход |")
            print("|---|---|---|---|---|")
        for item in found:
            by_code.setdefault(item.code, Counter())[item.outcome.value] += 1
        for item in shown:
            print(
                f"| {item.code} | {kinds[item.code]} "
                f"| {_money(item.document.value)} ({item.document.unit_code}) "
                f"| {_money(item.aggregator.value)} ({item.aggregator.unit_code}) "
                f"| {item.outcome.value} |"
            )
        print()


def main() -> int:
    """Сверяет документы названных эмитентов со строками агрегатора."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    wanted = [item for item in sys.argv[1:] if item.isdigit()]
    write = "--write" in sys.argv[1:]
    if "--review" in sys.argv[1:]:
        wanted = _review_with_pdf()
    if not wanted:
        print("назовите ИНН либо --review")
        return 1
    kinds = aggregator_codes()
    total: Counter[str] = Counter()
    by_code: dict[str, Counter[str]] = {}
    print("# Сверка агрегатора с документом МСФО\n")
    print(
        f"Эмитентов {len(wanted)}: {', '.join(wanted)}. Кодов агрегатора {len(kinds)}, "
        f"из них агрегатов источника "
        f"{sum(1 for kind in kinds.values() if kind == 'aggregate')}. Допуск — одна "
        "единица более грубой стороны. "
        + ("Сверка записана в `source_reconciliation`.\n" if write else "Прогон без записи.\n")
    )
    # Строки агрегатора — из кэша доставки; сеть не используется.
    rows = cbonds.msfo_universe()
    with contextlib.ExitStack() as stack:
        conn = stack.enter_context(connection()) if write else None
        for inn in wanted:
            for path in sorted((ROOT / inn).glob("*.pdf")):
                _document(inn, path, rows, kinds, total, by_code, conn)
    compared = sum(total[item.value] for item in (Outcome.MATCH, *_SHOWN))
    print("## Итог\n")
    print(
        f"Сверено величин с обеих сторон **{compared}**: совпало "
        f"**{total[Outcome.MATCH.value]}**, расходится {total[Outcome.DIFFER.value]}, "
        f"расходится только знаком {total[Outcome.SIGN.value]}. Нет в документе "
        f"{total[Outcome.NO_DOCUMENT.value]}, нет у агрегатора "
        f"{total[Outcome.NO_AGGREGATOR.value]}.\n"
    )
    for role in ("отчётная", "сравнительная"):
        matched = total[f"{role}|{Outcome.MATCH.value}"]
        seen = sum(total[f"{role}|{item.value}"] for item in (Outcome.MATCH, *_SHOWN))
        share = f" ({matched / seen * 100:.1f} %)" if seen else ""
        print(f"- колонка {role}: совпало {matched} из {seen}{share}")
    print()
    print("| Код | Род | Совпало | Расходится | Знаком | Нет в документе | Нет у агрегатора |")
    print("|---|---|---|---|---|---|---|")
    for code in sorted(kinds):
        seen = by_code.get(code, Counter())
        print(
            f"| {code} | {kinds[code]} | {seen[Outcome.MATCH.value]} "
            f"| {seen[Outcome.DIFFER.value]} | {seen[Outcome.SIGN.value]} "
            f"| {seen[Outcome.NO_DOCUMENT.value]} | {seen[Outcome.NO_AGGREGATOR.value]} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
