"""Проверка кандидатов набора МСФО по Cbonds и опись того, что источник знает.

Отвечает на два вопроса задачи 29. Первый: есть ли у названного кандидата
нормализованная отчётность и что она показывает. Второй: по каким признакам
эмитентов можно отобрать без выгрузки документа — то есть каким может быть
набор, который не стоит вечеров ручной работы.

**Рядом с перечнем признаков стоит перечень того, чего источник не знает.**
Иначе список отбираемого читается как список всех признаков ветки, тогда как
аудиторское мнение, обзорная проверка, конвенция разрядов и число отчётных
колонок в нормализованных данных отсутствуют по устройству.

Прогон в сеть не идёт, если ответы уже сохранены: `--refresh` обновляет.
"""

import argparse
import logging
from collections import Counter
from decimal import Decimal

from finlib.sources import cbonds

logger = logging.getLogger(__name__)

# Кандидаты первого списка — те, чья отчётность выгружается руками. Основание
# включения записано рядом: без него через месяц не восстановить, зачем брали.
CANDIDATES: dict[str, str] = {
    "9731004688": "Самолет — девелопер по эскроу, поправка ликвидности",
    "3900042827": "Эталон Груп — девелопер, второе наблюдение",
    "7724490000": "Почта России — квазисуверенная структура, читаемое заключение",
    "6685151087": "Брусника — девелопер, компактный документ",
    "7810245481": "Сэтл Групп — девелопер, прибыльный",
    "7708503727": "РЖД — квазисуверенная структура, запасной вариант",
}

# Чего в нормализованных данных нет вовсе. Перечень не декоративный: он
# отвечает на вопрос, какие признаки останутся ручной работой при любом
# размере набора.
NOT_IN_CBONDS: tuple[str, ...] = (
    "вид аудиторского мнения — поле «Аудированность» не заполнено ни у кого",
    "обзорная проверка — отличается от аудита только заключением, его нет",
    "конвенция разделителя разрядов — свойство вёрстки документа",
    "число отчётных колонок — свойство документа, у источника ряд дат",
    "капитализированные проценты, эскроу, примечания — только PDF",
)

FIELDS: dict[str, str] = {
    "ln104": "валюта",
    "ln105": "масштаб",
    "ln11": "активы",
    "ln20": "капитал",
    "ln23": "выручка",
    "ln26": "чистая прибыль",
    "ln36": "EBITDA",
    "ln34": "общий долг",
    "ln35": "чистый долг",
}


def _decimal(row: dict, code: str) -> Decimal | None:
    """Величина по коду; None — поле пусто."""
    raw = row.get(code)
    if raw in (None, ""):
        return None
    return Decimal(str(raw))


def _latest(rows: list[dict]) -> dict:
    """Запись за последнюю отчётную дату."""
    return sorted(rows, key=lambda item: item["date"])[-1]


def _by_inn(rows: list[dict]) -> dict[str, list[dict]]:
    """Записи, разложенные по ИНН; записи без ИНН отбрасываются."""
    found: dict[str, list[dict]] = {}
    for row in rows:
        inn = row.get("emitent_inn")
        if inn:
            found.setdefault(inn, []).append(row)
    return found


def candidates(rows: list[dict]) -> None:
    """Печатает, что источник знает о каждом кандидате."""
    known = _by_inn(rows)
    print("\nКАНДИДАТЫ ПЕРВОГО СПИСКА")
    found = 0
    for inn, why in CANDIDATES.items():
        mine = known.get(inn)
        if not mine:
            print(f"  {inn}: у источника нет — {why}")
            continue
        found += 1
        last = _latest(mine)
        dates = sorted(item["date"] for item in mine)
        print(f"  {last.get('emitent_name_rus')} (ИНН {inn}) — {why}")
        print(f"      периодов {len(mine)}: {dates[0]} .. {dates[-1]}")
        print(
            "      "
            + ", ".join(
                f"{name} {last.get(code)}" for code, name in FIELDS.items()
            )
        )
    print(f"  найдено у источника {found} из {len(CANDIDATES)} кандидатов")


def selectable(rows: list[dict]) -> None:
    """Печатает признаки, по которым эмитентов можно отобрать без документа."""
    known = _by_inn(rows)
    currency: Counter[str] = Counter()
    scale: Counter[str] = Counter()
    loss = negative_equity = with_ebitda = 0
    for mine in known.values():
        last = _latest(mine)
        currency[str(last.get("ln104"))] += 1
        scale[str(last.get("ln105"))] += 1
        profit = _decimal(last, "ln26")
        if profit is not None and profit < 0:
            loss += 1
        equity = _decimal(last, "ln20")
        if equity is not None and equity < 0:
            negative_equity += 1
        ebitda = _decimal(last, "ln36")
        if ebitda is not None and ebitda != 0:
            with_ebitda += 1

    print("\nЧТО ОТБИРАЕТСЯ ПО API")
    print(f"  эмитентов с отчётностью по МСФО: {len(known)}")
    print(f"  валюта: {dict(currency)}")
    print(f"  масштаб: {dict(scale)}")
    print(f"  убыток в последнем периоде: {loss} из {len(known)}")
    print(f"  отрицательный капитал: {negative_equity} из {len(known)}")
    print(f"  EBITDA раскрыта ненулевой: {with_ebitda} из {len(known)}")
    print(
        "  EBITDA у остальных равна нулю либо пуста — отбирать убыточных "
        "по ней нельзя, для этого служит чистая прибыль"
    )

    print("\nЧЕГО У ИСТОЧНИКА НЕТ")
    for item in NOT_IN_CBONDS:
        print(f"  {item}")


def main() -> None:
    """Точка входа прогона."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true", help="обновить сохранённые ответы"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = cbonds.msfo_universe(refresh=args.refresh)
    if not rows:
        print(
            "перечень эмитентов пуст: это не нулевое покрытие, а отсутствие "
            "измерения — ответ источника не получен"
        )
        return
    print(f"записей нормализованной отчётности: {len(rows)}")
    print(
        f"обращений к источнику {cbonds.pace.requested}, "
        f"ответов с диска {cbonds.pace.from_cache}"
    )
    candidates(rows)
    selectable(rows)


if __name__ == "__main__":
    main()
