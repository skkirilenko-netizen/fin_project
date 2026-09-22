"""ISS Московской биржи: публичный источник рыночных данных.

**Ключа не требует и потому проверяется прежде, чем на него опираются.**
Разведка отвечает на один вопрос: есть ли у ISS то, чего нет у снимков
агрегатора, — глубина истории торгов. У Cbonds её около сорока дней,
и если у биржи она годами, рыночный слой строится на ней.

**Ответы кэшируются на диск в исходном виде** — `data/raw/moex/`, — как
у всякого внешнего источника: повторный прогон сети не дёргает. Имя файла
называет запрос, а не хэш от него: по `history_RU000A10E6U4_2026-03.json`
видно, что спрашивали.

**Частота ограничена нами, а не источником.** ISS предела не объявляет,
и это не повод его не держать: публичный источник без ключа тем более
не место для тысячи запросов в минуту.

**Формат ответа — колонки и строки порознь.** ISS отдаёт `{"history":
{"columns": [...], "data": [[...]]}}`, и превращать это в словари — дело
читающего: `rows()` делает одно и то же во всех вызовах, чтобы имя колонки
писалось в одном месте.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/moex")
BASE = "https://iss.moex.com/iss"
# Контакт в `User-Agent` обязателен по тем же основаниям, что у ГИР БО:
# источник вправе знать, кто его спрашивает.
AGENT = "fin-analysis/1.0 (local research; contact in .env)"
# Наш предел, а не объявленный источником: две пробы в секунду.
MIN_INTERVAL_S = 0.5


class MoexError(Exception):
    """ISS ответил не тем, о чём спрашивали."""


@dataclass(slots=True)
class Pace:
    """Счётчики обращений: сколько ушло в сеть, сколько взято с диска."""

    requested: int = 0
    from_cache: int = 0
    stamps: list[float] = field(default_factory=list)

    def wait(self) -> None:
        """Выжидает наш собственный интервал между запросами."""
        if self.stamps:
            pause = MIN_INTERVAL_S - (time.monotonic() - self.stamps[-1])
            if pause > 0:
                time.sleep(pause)
        self.stamps.append(time.monotonic())


pace = Pace()


def fetch(path: str, name: str, params: dict[str, Any] | None = None) -> dict:
    """Ответ ISS по пути; сохранённый берётся с диска, сеть не дёргается.

    `path` — путь внутри `/iss`, `name` — имя файла кэша, оно же называет
    запрос человеку.
    """
    where = CACHE / f"{name}.json"
    if where.exists():
        pace.from_cache += 1
        return json.loads(where.read_text(encoding="utf-8"), parse_float=Decimal)
    pace.wait()
    pace.requested += 1
    logger.info("ISS %s (%s)", path, name)
    response = httpx.get(
        f"{BASE}/{path.lstrip('/')}",
        params=params or {},
        timeout=30.0,
        headers={"User-Agent": AGENT},
    )
    if response.status_code != 200:
        raise MoexError(f"ISS {path}: {response.status_code} — {response.text[:200]}")
    found = json.loads(response.text, parse_float=Decimal)
    CACHE.mkdir(parents=True, exist_ok=True)
    where.write_text(response.text, encoding="utf-8")
    return found


def rows(answer: dict, block: str) -> list[dict]:
    """Строки блока ответа словарями; пустой блок — пустой перечень.

    **Имя колонки пишется в одном месте.** ISS отдаёт колонки и данные
    порознь, и разбор по индексу колонки в каждом вызове — второй путь
    к одному ответу, расходящийся при первой же смене порядка колонок.
    """
    found = answer.get(block)
    if not found:
        return []
    names = found.get("columns") or []
    return [dict(zip(names, item, strict=False)) for item in found.get("data") or []]
