"""Перечень дефолтов по стране: ежедневная доставка.

    uv run python scripts/defaults_fetch.py

**Дата события приходит только отсюда.** `get_emission_default` отдаёт
по каждому дефолту плановый срок, дату дефолта, дату фактического исполнения
и неисполненную сумму; отбор по стране даёт весь перечень за четыре запроса.
Признак дефолта в карточке выпуска отстаёт и даты не несёт.

**Файл дня — отдельный, как у снимка рейтингов.** Прежде перечень лежал одним
файлом от 22.09.2026 и прогоном не обновлялся: дефолт, случившийся позже,
маршрут не увидел бы никогда. Повторный запуск того же дня берёт файл дня
с диска и в сеть не идёт.
"""

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import cbonds  # noqa: E402

METHOD = "get_emission_default"


def main() -> int:
    """Забирает перечень дефолтов на сегодня; отказ источника — исключение."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    today = date.today()
    found = cbonds.fetch(
        METHOD,
        f"defaults_ru_{today:%Y-%m-%d}",
        filters=(
            {"field": "emission_emitent_country_id", "operator": "eq", "value": "1"},
        ),
        limit=1000,
    )
    print(
        f"перечень дефолтов на {today}: записей {len(found.get('items', []))} "
        f"из {found.get('total')}, запросов {cbonds.pace.requested}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
