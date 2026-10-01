"""Отбор графиков по существу на сохранённых доставках: экономия запросов. Только диск.

    uv run python eval/flows_substantive_run.py

Для каждого дня доставки (`flows_delta_ГГГГ-ММ-ДД.json`) обновлённый
выпуск сравнивается с его прежней записью: последней из более ранних
выборок обновлённых (`emissions_changed_*.json`), иначе — из перечня
выпусков в обращении 22.09.2026. Поля — те же, что у доставки
(`scripts/flows_fetch.substantive_fields`). **Ограничение названо**:
прежние графики перезаписаны доставкой, и проверить, изменился ли сам
перезабранный график, нечем — мера говорит о записи выпуска, а не о графике.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flows_fetch  # noqa: E402

DAYS = ("2026-09-29", "2026-09-30", "2026-10-01")


def main() -> int:
    """Печатает по дням: спрошено, обновлённых, без существенных изменений, запросов."""
    fields = flows_fetch.substantive_fields()
    cache = flows_fetch.CACHE
    snapshots = {
        path.stem[len("emissions_changed_") :]: {
            str(item.get("id")): item
            for item in json.loads(path.read_text(encoding="utf-8")).get("items", [])
        }
        for path in sorted(cache.glob("emissions_changed_*.json"))
    }
    base = {
        str(item.get("id")): item
        for item in json.loads(
            (cache / "emissions_ru_outstanding.json").read_text(encoding="utf-8")
        ).get("items", [])
    }
    print(
        "| День доставки | Окно с | Спрошено | Обновлённых | Не по существу "
        "| Без прежней записи | Запросов сэкономлено |"
    )
    print("|---|---|---|---|---|---|---|")
    for day in DAYS:
        path = cache / f"flows_delta_{day}.json"
        if not path.exists():
            print(f"| {day} | доставки нет | | | | | |")
            continue
        delta = json.loads(path.read_text(encoding="utf-8"))
        since = delta["since"]
        current = snapshots.get(since, {})
        updated = [item for item in delta["asked"] if item["why"] == "обновлён"]
        same = without = 0
        for item in updated:
            emission = item["emission_id"]
            now = current.get(emission)
            before = None
            for stamp in sorted(snapshots):
                if stamp < since and emission in snapshots[stamp]:
                    before = snapshots[stamp][emission]
            before = before or base.get(emission)
            if now is None or before is None:
                without += 1
                continue
            same += int(flows_fetch.digest(now, fields) == flows_fetch.digest(before, fields))
        print(
            f"| {day} | {since} | {len(delta['asked'])} | {len(updated)} | {same} "
            f"| {without} | {2 * same} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
