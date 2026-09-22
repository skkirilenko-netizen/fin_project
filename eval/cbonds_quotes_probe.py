"""Разведка: есть ли у подписки торги и котировки. **Только чтение.**

    uv run python eval/cbonds_quotes_probe.py

Cbonds сообщил, что «торги и котировки — в `get_emissions`». Проверено:
в записи выпуска 209 полей, и торгов касаются только площадки
(`ss_trading_grounds`) и объёмы размещения — цен и сделок среди них нет.
Значит, котировки живут в отдельном методе, и вопрос один: в каком именно
и открыт ли он подписке.

**Перебор имён здесь — последнее средство, и он больше не нужен.** Справочник
методов лежит в репозитории — `docs/cbonds/openapi.yaml`, 211 методов с полями
и фильтрами, — и читать следует его: перебор нашёл `get_tradings_new`, а
`get_emission_default` он же однажды объявил несуществующим, потому что проба
оборвалась на транспорте. Проба оставлена как есть: она показывает, **что
из объявленного доступно подписке**, а этого в справочнике нет.

**Три исхода, и смешивать их нельзя**: `invalid operation name` — метода нет
вовсе, `invalid resource name` — метод есть, доступа к нему нет, обрыв связи —
о методе не сказано ничего.

**Обрыв связи ответом не считается.** Запрос, оборвавшийся на транспорте,
о методе не говорит ничего, и записать его как «метода нет» значило бы
закрыть вопрос отсутствием сведений.

**Спрашивается один выпуск, а не метод целиком.** Клиент дочитывает ответ
до конца — страница за страницей, пока не собраны все записи, — и запрос
без отбора к методу торгов означал бы обход всей истории котировок
по тридцать запросов в минуту. Первая проба именно так и провисела
восемнадцать минут, не напечатав ни строки. Отбор по выпуску делает ответ
конечным, а неподдерживаемый отбор клиент отвергает на первой же странице.
"""

import logging
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.sources import cbonds  # noqa: E402

logger = logging.getLogger(__name__)

# Имена, которые могут нести котировки. Перечень — предположение, и он
# объявлен предположением: источник справочника методов не отдаёт вовсе
# (`nomenclature` описывает только отчётность, 284 записи, ни одной о торгах).
# Выпуск, по которому спрашиваются котировки: ЕвроТранс, БО-001Р-03 —
# бумага в обращении у эмитента, чей рыночный сигнал и есть вопрос.
EMISSION = "1435958"

CANDIDATES: tuple[str, ...] = (
    "get_tradings",
    "get_trading",
    "get_quotes",
    "get_emission_quotes",
    "get_tradings_new",
    "get_bond_quotes",
    "get_prices",
    "get_emission_tradings",
)


def probe(method: str) -> str:
    """Ответ источника о методе словами; обрыв связи повторяется однажды."""
    for attempt in (1, 2):
        try:
            found = cbonds.fetch(
                method,
                f"probe_{method}",
                filters=(
                    {"field": "emission_id", "operator": "eq", "value": EMISSION},
                ),
                limit=100,
            )
        except cbonds.FilterIgnoredError:
            return "метод есть, но отбор по выпуску не поддерживает"
        except cbonds.CbondsError as failure:
            text = str(failure)
            if "invalid operation name" in text:
                return "метода нет"
            if "invalid resource name" in text:
                return "метод есть, доступа нет"
            return f"отказ: {text[:90]}"
        except httpx.HTTPError as failure:
            logger.error("%s: обрыв связи (%s)", method, type(failure).__name__)
            if attempt == 2:
                return "обрыв связи: о методе не сказано ничего"
            time.sleep(15.0)
            continue
        items = found.get("items") or []
        fields = sorted(items[0]) if items else []
        return (
            f"отвечает: записей {found.get('total')}, полей {len(fields)} — "
            + ", ".join(fields[:14])
        )
    return "обрыв связи: о методе не сказано ничего"


def main() -> int:
    """Печатает исход по каждому имени; 1 — если не ответил ни один."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    answered = 0
    for method in CANDIDATES:
        verdict = probe(method)
        answered += verdict.startswith("отвечает")
        print(f"{method:24} {verdict}")
        time.sleep(3.0)
    print(
        f"\nИмён проверено {len(CANDIDATES)}, ответили {answered}. "
        "Перечень имён — предположение: справочника методов источник не отдаёт, "
        "и «ответили ноль» означает, что верное имя не угадано, а не что "
        "котировок у подписки нет."
    )
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main())
