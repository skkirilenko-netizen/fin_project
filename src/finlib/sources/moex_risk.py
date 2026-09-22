"""Сектор повышенного риска Московской биржи. **Читает диск, не сеть.**

**Перевод выпуска в режим «Д» — решение биржи с датой, а не наше суждение.**
У ЕвроТранса режим TQCB кончается 05.08.2026, TQRD начинается 06.08.2026:
за две недели до того, как не исполнилось погашение Кириллицы, и задолго
до отчётной даты, по которой маршрут судит.

**Режимы риска объявлены поимённо, а не по слову в наименовании.** В словаре
рынка облигаций торгуемых режимов двенадцать, и «Д» стоит у двух: `TQRD`
(«Т+: Облигации Д») и `TQUD` (тот же в долларах). Отбор по вхождению буквы
однажды проглотил бы «Гособлигации», а отбор по слову «риск» не нашёл бы
ни одного: в наименовании режима его нет вовсе.

**Дата перевода берётся у карточки выпуска, а не у перечня.** Перечень
говорит, где выпуск торгуется сейчас; когда он туда переведён, стоит
в карточке: `history_from` у режима риска и `history_till` у прежнего.
Взять первое без второго было бы достаточно, но тогда исчезло бы,
откуда выпуск переведён, — а это и есть содержание события.

Доставка — `scripts/moex_fetch.py`.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/moex")
# Имя файла перечня: одно на доставку и на чтение, чтобы искать его
# не приходилось в двух местах.
TRADED = "bonds_traded"
# Режимы повышенного риска рынка облигаций, объявленные поимённо.
RISK_BOARDS: dict[str, str] = {
    "TQRD": "Т+: Облигации Д",
    "TQUD": "Т+: Облигации Д (USD)",
}


@dataclass(frozen=True, slots=True)
class RiskSector:
    """Выпуск в секторе повышенного риска: когда переведён и откуда."""

    isin: str
    board: str
    since: date | None
    came_from: str
    left_on: date | None
    # Наименование выпуска у агрегатора: биржа зовёт его сокращённо
    # («ЕвроТранс3»), а человек читает то же имя, что в прочих основаниях.
    name: str = ""

    @property
    def known(self) -> bool:
        """Есть ли дата перевода: без неё событие не датировано."""
        return self.since is not None


def _as_date(value: object) -> date | None:
    """Дата ISS; пустое и мусор остаются None."""
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def traded() -> dict[str, dict]:
    """Торгуемые облигации биржи по ISIN; пусто — перечня нет на диске."""
    path = CACHE / f"{TRADED}.json"
    if not path.exists():
        logger.warning("перечня торгуемых облигаций на диске нет: %s", path)
        return {}
    found = json.loads(path.read_text(encoding="utf-8")).get("securities") or {}
    names = found.get("columns") or []
    rows = [dict(zip(names, item, strict=False)) for item in found.get("data") or []]
    return {str(item.get("ISIN") or ""): item for item in rows if item.get("ISIN")}


def risk_sectors() -> dict[str, RiskSector]:
    """Выпуски в секторе риска по ISIN: что известно с диска.

    Выпуск без карточки в перечень попадает **с неизвестной датой**, а не
    выпадает из него: режим у него риска, и молчание об этом читалось бы
    как отсутствие события.
    """
    found: dict[str, RiskSector] = {}
    for isin, item in traded().items():
        board = str(item.get("BOARDID") or "")
        if board not in RISK_BOARDS:
            continue
        secid = str(item.get("SECID") or isin)
        path = CACHE / f"security_{secid}.json"
        since: date | None = None
        came_from = ""
        left_on: date | None = None
        if path.exists():
            answer = json.loads(path.read_text(encoding="utf-8")).get("boards") or {}
            names = answer.get("columns") or []
            boards = [
                dict(zip(names, entry, strict=False))
                for entry in answer.get("data") or []
            ]
            for entry in boards:
                code = str(entry.get("boardid") or "")
                if code == board:
                    since = _as_date(entry.get("history_from"))
                    continue
                # **Откуда переведён — из основного режима, а не из любого.**
                # У выпуска сорок с лишним режимов: РЕПО, размещение, адресные
                # сделки, — и торги в них кончаются когда угодно. Перевод
                # в сектор риска случается из безадресного «Т+», и только он
                # тут и важен: у ЕвроТранса TQCB кончается 05.08.2026, а РПС
                # тянется до сентября, и по нему вышло бы, что переводили
                # позже самого перевода.
                if not code.startswith("TQ") or code in RISK_BOARDS:
                    continue
                when = _as_date(entry.get("history_till"))
                if when is not None and (left_on is None or when > left_on):
                    left_on, came_from = when, code
        found[isin] = RiskSector(
            isin=isin,
            board=board,
            since=since,
            came_from=came_from,
            left_on=left_on,
        )
    return found
