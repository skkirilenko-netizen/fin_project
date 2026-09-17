"""Данные Cbonds: чтение сохранённых ответов, без обращения к сети.

Cbonds отдаёт **нормализованную** отчётность по ИНН: пять строк отчёта
о прибылях, три сальдо потоков, полтора десятка строк баланса. Величины
основных форм у него и у нас сходятся — на пяти разобранных эмитентах
из 97 сверенных величин разошлись пять, и все пять различаются
определением показателя, а не ошибкой разбора.

**Ценность здесь в обратном: в том, чего Cbonds не различает.** Всё, что
не легло в его полтора десятка кодов, сворачивается в «прочие», и доля
свёрнутого у разных эмитентов отличается на два порядка — от 0,3 % валюты
баланса у ЛСР до 86 % у Автодора. Это и есть машинный признак того, где
разметка нужна, а где справочник Cbonds закрывает форму целиком.

**Клиента здесь нет намеренно.** Читаются сохранённые ответы из
`data/raw/cbonds/`; получение остаётся ручным до решения по источнику.
Правило проекта — кэшировать сырой ответ и не ходить в сеть повторно —
это соблюдает, а решение о том, становится ли Cbonds источником основных
форм, ещё не принято.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")

# Коды «прочего» по разделам баланса. Именно они и означают «Cbonds этой
# статьи не различает»: всё, что не легло в его именованные коды, здесь.
OTHER_CODES: dict[str, str] = {
    "ln5": "current_assets",
    "ln9": "non_current_assets",
    "ln15": "current_liabilities",
    "ln18": "non_current_liabilities",
}

# Код валюты баланса: доля «прочего» считается от него.
TOTAL_ASSETS = "ln11"


@dataclass(frozen=True, slots=True)
class OtherShare:
    """Сколько величины раздела Cbonds не различает."""

    section: str
    amount: Decimal
    share_of_assets: Decimal

    def describe(self) -> str:
        """Строка для отчёта."""
        return f"{self.section}: {self.amount} ({self.share_of_assets:.1%} активов)"


def cached(inn: str) -> list[dict]:
    """Сохранённые записи эмитента; пусто — ответа на диске нет.

    Отсутствие файла и пустой ответ — разные вещи, и путать их нельзя:
    первое означает, что мы не спрашивали, второе — что эмитента нет
    у источника. Пустой список возвращается в обоих случаях, но в журнал
    идут разные сообщения.
    """
    path = CACHE / f"msfo_real_{inn}.json"
    if not path.exists():
        logger.info("ответа Cbonds по ИНН %s на диске нет: источник не спрашивали", inn)
        return []
    found = json.loads(path.read_text(), parse_float=Decimal)
    items = found.get("items", [])
    if not items:
        logger.info("Cbonds не знает ИНН %s: ответ пуст", inn)
    return items


def _value(item: dict, code: str) -> Decimal | None:
    """Величина по коду; None — поле пусто либо нечисловое."""
    raw = item.get(code)
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None


def other_shares(inn: str, report_date: date) -> dict[str, OtherShare]:
    """Доля «прочего» Cbonds по разделам баланса на эту дату.

    Пусто — данных нет: ни записи за период, ни валюты баланса. Ноль
    и отсутствие здесь снова разные вещи, и ноль не подставляется.
    """
    wanted = report_date.isoformat()
    item = next((row for row in cached(inn) if row.get("date") == wanted), None)
    if item is None:
        return {}
    assets = _value(item, TOTAL_ASSETS)
    if assets is None or assets == 0:
        return {}
    found: dict[str, OtherShare] = {}
    for code, section in OTHER_CODES.items():
        amount = _value(item, code)
        if amount is None:
            continue
        found[section] = OtherShare(section, amount, abs(amount) / abs(assets))
    return found
