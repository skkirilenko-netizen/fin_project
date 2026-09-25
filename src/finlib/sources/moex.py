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


def fetch(
    path: str, name: str, params: dict[str, Any] | None = None, refresh: bool = False
) -> dict:
    """Ответ ISS по пути; сохранённый берётся с диска, сеть не дёргается.

    `path` — путь внутри `/iss`, `name` — имя файла кэша, оно же называет
    запрос человеку. `refresh` переспрашивает источник: ответ о текущем
    состоянии (перечень режимов торгов) с диска устаревает — с 22.09.2026
    по 25.09 сектор риска брался из кэша, и перевод выпуска не был бы виден.
    """
    where = CACHE / f"{name}.json"
    if where.exists() and not refresh:
        pace.from_cache += 1
        return json.loads(where.read_text(encoding="utf-8"), parse_float=Decimal)
    # **Обрыв связи ответом не является, и повтор здесь не роскошь.** Прогон
    # срезов — тысяча обращений, и падение на середине оставляет диск
    # с половиной дней: то же основание, по которому повтор заведён
    # у доставки Cbonds.
    #
    # **Сбой на стороне источника — тот же обрыв, а не отказ.** Ответ 5xx
    # означает, что источнику сейчас плохо, и через несколько секунд он
    # отвечает как ни в чём не бывало: проверено в ночь на 24.09.2026, когда
    # одиночный 502 остановил доставку на 2 946-м запросе, а тот же день
    # тремя пробами подряд отдался за секунду. Ответ 4xx повторять незачем:
    # он о нашем запросе, и вторая попытка даст то же самое.
    response = None
    for attempt in (1, 2, 3):
        pace.wait()
        pace.requested += 1
        logger.info("ISS %s (%s)", path, name)
        try:
            response = httpx.get(
                f"{BASE}/{path.lstrip('/')}",
                params=params or {},
                timeout=30.0,
                headers={"User-Agent": AGENT},
            )
            if response.status_code < 500:
                break
            logger.error(
                "ISS %s: источник ответил %d, попытка %d",
                path,
                response.status_code,
                attempt,
            )
            if attempt == 3:
                raise MoexError(
                    f"ISS {path}: {response.status_code} третий раз подряд"
                )
        except httpx.HTTPError as failure:
            logger.error(
                "ISS %s: обрыв связи (%s), попытка %d",
                path,
                type(failure).__name__,
                attempt,
            )
            if attempt == 3:
                raise MoexError(f"ISS {path}: связь обрывается третий раз") from failure
        time.sleep(5.0 * attempt)
    if response is None or response.status_code != 200:
        code = response.status_code if response is not None else "нет ответа"
        text = response.text[:200] if response is not None else ""
        raise MoexError(f"ISS {path}: {code} — {text}")
    found = json.loads(response.text, parse_float=Decimal)
    CACHE.mkdir(parents=True, exist_ok=True)
    where.write_text(response.text, encoding="utf-8")
    return found


def paged(
    path: str, name: str, block: str, params: dict[str, Any] | None = None
) -> list[dict]:
    """Все строки блока, страница за страницей; ответ собирается на диск один.

    **ISS отдаёт срез рынка страницами по сотне**, а число строк говорит
    в курсоре. Собранный ответ кладётся одним файлом: иначе перечитанный
    с диска окажется короче полученного из сети, и повторный прогон тихо
    потеряет выпуски — та же ошибка, что уже была у клиента Cbonds.
    """
    where = CACHE / f"{name}.json"
    if where.exists():
        pace.from_cache += 1
        found = json.loads(where.read_text(encoding="utf-8"), parse_float=Decimal)
        return found.get(block) or []
    collected: list[dict] = []
    start = 0
    while True:
        answer = fetch(path, f"{name}_p{start}", {**(params or {}), "start": start})
        page = rows(answer, block)
        collected.extend(page)
        cursor = rows(answer, f"{block}.cursor")
        total = int(cursor[0].get("TOTAL", len(collected))) if cursor else len(collected)
        if not page or len(collected) >= total:
            break
        start += len(page)
    CACHE.mkdir(parents=True, exist_ok=True)
    where.write_text(
        json.dumps({block: collected}, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    # Страницы больше не нужны: собранный ответ полон, и держать обе копии
    # значило бы хранить одно и то же дважды.
    for item in CACHE.glob(f"{name}_p*.json"):
        item.unlink()
    return collected


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
