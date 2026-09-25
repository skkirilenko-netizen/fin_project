"""Проба: применяет ли Cbonds отбор «не раньше даты» (`ge`), или пропускает молча.

    uv run python eval/cbonds_ge_probe.py get_emissions updating_date 2026-09-24 \
        --also emitent_country=1

**Неподдерживаемое поле отбора Cbonds пропускает молча** и отдаёт всё подряд;
клиент сверяет с записями только отбор `eq` (`cbonds._verify_applied`).
Поэтому здесь два запроса, и второй — отрицательный контроль:

1. `поле ≥ дата` — все записи обязаны удовлетворять условию;
2. `поле ≥ 2030-01-01` — записей быть не должно вовсе. Пришли записи —
   отбор пропущен, и первый ответ ничего не доказывает, даже если сошёлся.

Ответы кладутся на диск в исходном виде (`probe_ge_*`) и печатаются сводкой:
сколько записей, сколько нарушают условие, какой разброс значений поля.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import cbonds  # noqa: E402

FUTURE = "2030-01-01"


def _ask(method: str, field: str, since: str, also: list[dict]) -> dict:
    """Один отбор `ge` с сохранением ответа; имя файла называет запрос."""
    extra = "_".join(f"{item['field']}-{item['value']}" for item in also)
    return cbonds.fetch(
        method,
        f"probe_ge_{method}_{field}_{since}{'_' + extra if extra else ''}",
        filters=(*also, {"field": field, "operator": "ge", "value": since}),
        limit=1000,
        refresh=True,
    )


def _said(found: dict, field: str, since: str) -> str:
    """Сводка ответа: записей, нарушений условия, разброс поля."""
    items = found.get("items", [])
    values = sorted(str(item.get(field) or "") for item in items)
    wrong = [value for value in values if value[:10] < since]
    span = f"{values[0]} … {values[-1]}" if values else "—"
    return (
        f"записей {len(items)} из {found.get('total')}, нарушают условие "
        f"{len(wrong)}, значения поля {span}"
    )


def main() -> int:
    """Два запроса и вывод; 1 — если отбор не подтвердился."""
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("method")
    parser.add_argument("field")
    parser.add_argument("since")
    parser.add_argument("--also", action="append", default=[])
    args = parser.parse_args()
    also = [
        {"field": key, "operator": "eq", "value": value}
        for key, value in (item.split("=", 1) for item in args.also)
    ]
    real = _ask(args.method, args.field, args.since, also)
    control = _ask(args.method, args.field, FUTURE, also)
    print(f"{args.method}, {args.field} ≥ {args.since}: {_said(real, args.field, args.since)}")
    print(
        f"{args.method}, {args.field} ≥ {FUTURE} (контроль): "
        f"{_said(control, args.field, FUTURE)}"
    )
    print(f"запросов {cbonds.pace.requested}")
    applied = not control.get("items") and all(
        str(item.get(args.field) or "")[:10] >= args.since for item in real.get("items", [])
    )
    print("отбор применяется" if applied else "ОТБОР НЕ ПОДТВЕРЖДЁН")
    return 0 if applied else 1


if __name__ == "__main__":
    sys.exit(main())
