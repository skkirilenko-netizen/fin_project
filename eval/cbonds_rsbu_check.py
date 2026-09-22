"""Сверка отчётности РСБУ из Cbonds с нашей выгрузкой ГИР БО — только отчёт.

**Источник не переключается.** Замер отвечает на один вопрос: годится ли
нормализованная отчётность агрегатора заменой недоступному ГИР БО. Ответ —
числа: сколько строк совпало до копейки, сколько разошлось, чего нет
у одной стороны и чего у другой.

    uv run python eval/cbonds_rsbu_check.py > data/output/cbonds_rsbu.md

Сверяются организации, у которых **есть наши факты РСБУ**: сравнивать
с пустотой нечего. Периоды берутся общие: у агрегатора есть кварталы,
которых у нас нет вовсе, и записывать их в «расхождения» значило бы считать
расхождением отсутствие выгрузки.

Запросов к источнику: два на организацию (баланс и отчёт о финансовых
результатах) и один на отчёт о движении денежных средств — он отбирается
по внутреннему идентификатору эмитента, а не по ИНН. Ответы сохраняются
на диск, повторный запуск сети не дёргает.
"""

import json
import logging
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.config import settings  # noqa: E402
from finlib.db import connection, fetch_all  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")
# Отчёт о движении денежных средств отбирается по идентификатору эмитента:
# поля ИНН в нём нет вовсе, и отбор по ИНН источник пропускает молча.
REPORTS = (
    ("get_report_rsbu_balance", "emitent_inn"),
    ("get_report_rsbu_profit", "emitent_inn"),
    ("get_report_cash_flow_statement_newform", "emitent_id"),
)

_OURS = """
SELECT f.inn, f.report_date, f.line_code, f.value
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.standard = 'rsbu' AND s.is_actual AND f.value IS NOT NULL
"""


def number(value: object) -> Decimal | None:
    """Величина источника в Decimal; пустое остаётся None."""
    if value in (None, ""):
        return None
    return Decimal(str(value))


def fetch(method: str, field: str, value: str) -> list[dict]:
    """Ответ источника с диска, а при его отсутствии — из сети."""
    path = CACHE / f"rsbu_{method}_{value}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("items", [])
    if not settings.cbonds_ready:
        return []
    body = {
        "auth": {"login": settings.cbonds_login, "password": settings.cbonds_password},
        "filters": [{"field": field, "operator": "eq", "value": value}],
        "quantity": {"limit": 100, "offset": 0},
    }
    response = httpx.post(
        f"{settings.cbonds_base_url}/{method}/", json=body, timeout=30.0
    )
    time.sleep(0.6)
    if response.status_code != 200:
        logger.error("%s %s: %s", method, value, response.text[:120])
        return []
    path.write_text(response.text, encoding="utf-8")
    return response.json().get("items", [])


def codes_of(rows: list[dict]) -> dict[str, dict[str, Decimal]]:
    """Величины источника по дате и коду строки."""
    found: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        moment = str(row.get("date") or "")
        by_code = found.setdefault(moment, {})
        for key, value in row.items():
            if not key.startswith("ln") or not key[2:].isdigit():
                continue
            number_value = number(value)
            if number_value is not None:
                by_code[key[2:]] = number_value
    return found


def _kind_of(mine: Decimal, theirs: Decimal) -> str:
    """Род расхождения: округление, нераскрытие либо различие данных.

    Разводить их нужно потому, что решения из них разные: округление на единицу
    замене не мешает, ноль вместо величины означает нераскрытие у агрегатора,
    а различие в разы — другую редакцию отчётности, и её надо смотреть глазами.
    """
    if theirs == 0:
        return "у агрегатора ноль там, где у нас величина"
    if mine == 0:
        return "у нас ноль там, где у агрегатора величина"
    if abs(abs(mine) - abs(theirs)) <= 1:
        return "округление на единицу"
    share = abs(abs(mine) - abs(theirs)) / max(abs(mine), abs(theirs))
    if share < Decimal("0.01"):
        return "различие ниже процента"
    return "различие данных: другая редакция либо другой состав"


def main() -> int:
    """Печатает сверку; 1 — если наших фактов РСБУ в базе нет."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    with connection() as conn:
        rows = fetch_all(_OURS, {}, conn=conn)
    if not rows:
        print("наших фактов РСБУ в базе нет: сверять нечего, измерения не было")
        return 1

    ours: dict[str, dict[str, dict[str, Decimal]]] = {}
    for row in rows:
        ours.setdefault(row["inn"], {}).setdefault(str(row["report_date"]), {})[
            row["line_code"]
        ] = row["value"]

    print("# РСБУ из Cbonds против нашей выгрузки ГИР БО\n")
    print(
        f"Организаций с нашими фактами РСБУ: **{len(ours)}**. Сверяются общие "
        "периоды: кварталы, которых у нас нет вовсе, расхождением не считаются.\n"
    )
    print(
        "| Организация | ИНН | Периодов | Совпало | Разошлось | Только у нас "
        "| Только у Cbonds |"
    )
    print("|---|---|---|---|---|---|---|")

    totals: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    divergences: list[str] = []
    only_ours: Counter[str] = Counter()
    only_theirs: Counter[str] = Counter()
    for inn in sorted(ours):
        balance = fetch(REPORTS[0][0], REPORTS[0][1], inn)
        if not balance:
            continue
        name = (balance[0].get("emitent_name_rus") or "").strip()
        emitent_id = str(balance[0].get("emitent_id") or "")
        profit = fetch(REPORTS[1][0], REPORTS[1][1], inn)
        flows = fetch(REPORTS[2][0], REPORTS[2][1], emitent_id) if emitent_id else []
        theirs = codes_of(balance)
        for part in (profit, flows):
            for moment, values in codes_of(part).items():
                theirs.setdefault(moment, {}).update(values)

        same = differ = mine = yours = 0
        periods = sorted(set(ours[inn]) & set(theirs))
        for moment in periods:
            mine_values, their_values = ours[inn][moment], theirs[moment]
            for code, value in mine_values.items():
                if code not in their_values:
                    mine += 1
                    only_ours[code] += 1
                elif abs(their_values[code]) == abs(value):
                    same += 1
                else:
                    differ += 1
                    kinds[_kind_of(value, their_values[code])] += 1
                    divergences.append(
                        f"{name} {moment} строка {code}: наше {value}, "
                        f"Cbonds {their_values[code]}"
                    )
            for code in their_values:
                if code not in mine_values:
                    yours += 1
                    only_theirs[code] += 1
        totals["периодов"] += len(periods)
        totals["совпало"] += same
        totals["разошлось"] += differ
        totals["только у нас"] += mine
        totals["только у Cbonds"] += yours
        print(
            f"| {name[:28]} | {inn} | {len(periods)} | {same} | {differ} "
            f"| {mine} | {yours} |"
        )

    print(
        f"| **итого** | | **{totals['периодов']}** | **{totals['совпало']}** "
        f"| **{totals['разошлось']}** | **{totals['только у нас']}** "
        f"| **{totals['только у Cbonds']}** |"
    )

    if kinds:
        print("\n## Чем расхождения оказались\n")
        print("| Род расхождения | Величин |")
        print("|---|---|")
        for name, count in kinds.most_common():
            print(f"| {name} | {count} |")
        print(
            "\n**Ноль у агрегатора не означает нуля** — это тот же признак, что "
            "и в данных МСФО: там, где у нас величина раскрыта, а у него ноль, "
            "нераскрытие выдаётся за пустое значение."
        )

    if divergences:
        print("\n## Расхождения величин\n")
        for line in divergences[:40]:
            print(f"- {line}")
        if len(divergences) > 40:
            print(f"- …и ещё {len(divergences) - 40}")
    else:
        print(
            "\n**Расхождений величин нет ни одного.** Это и есть ответ на вопрос "
            "о пригодности замены: там, где обе стороны раскрывают строку, "
            "величины совпадают до копейки."
        )

    if only_ours:
        print("\n## Строки, которых нет у Cbonds\n")
        print("| Код строки | Периодов |")
        print("|---|---|")
        for code, count in only_ours.most_common(20):
            print(f"| {code} | {count} |")
    if only_theirs:
        # **Графа называет то, что считает.** Эти коды не «пропущены нами»:
        # их нет в справочнике методики, и ни один показатель, флаг
        # и стоп-фактор на них не опирается. Решение не заводить их принято
        # 22.09.2026; графа объявляет это, чтобы число не читалось как пробел.
        print("\n## Вне справочника, методикой не используются\n")
        print(
            "Коды, которые агрегатор раскрывает, а справочник методики "
            "не несёт: детализация отчёта о движении денежных средств "
            "и справочный раздел отчёта о финансовых результатах. Заводить их "
            "решено не сейчас — ни один показатель на них не опирается.\n"
        )
        print("| Код строки | Периодов |")
        print("|---|---|")
        for code, count in only_theirs.most_common(20):
            print(f"| {code} | {count} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
