"""Основной вид деятельности эмитентов: поиск по ИНН в ГИР БО.

    uv run python scripts/okved_fetch.py             # добор недостающих
    uv run python scripts/okved_fetch.py --limit 50  # частями

**Зачем.** У холдинга отчётность РСБУ описывает управляющую компанию, а не
группу: величины верны, а обслуживание долга зависит от дочерних обществ,
которых в этой отчётности нет. Отличает холдинг основной вид деятельности
(ОКВЭД2 64.20 и 70.10), и у агрегатора его нет вовсе — поле `nace` карточки
равно нулю у всех 977 эмитентов. В ответе поиска ГИР БО он есть (`okved2`),
и один запрос на организацию его приносит.

**Спрашивается только то, чего нет.** Ответ поиска кэшируется на диск
(`data/raw/girbo/search_{ИНН}.json`), повторный запуск сеть не дёргает,
а организация с уже известным видом деятельности не спрашивается вовсе.

**Организация, которой в базе нет, не заводится.** Вид деятельности — реквизит
организации, а не отдельное сведение: заводить строку ради него значило бы
иметь в базе организацию без единого факта. Такие называются в итоге числом:
ноль спрошенных при неизвестном числе пропущенных ничего не значит.

**Отказ поиска — это исход, а не сбой.** Организации, которой ГИР БО не знает
(регистрация вне России, кредитная организация, свежая регистрация), вид
деятельности взять неоткуда, и она считается отдельно от ошибок связи.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.db import connection, execute, fetch_all  # noqa: E402
from finlib.sources.errors import SourceError  # noqa: E402
from finlib.sources.girbo import GirboSource, OrganizationNotFoundError  # noqa: E402

logger = logging.getLogger(__name__)

# Организации без известного вида деятельности. Заведённые у нас — те,
# о ком есть что спрашивать: вид деятельности живёт рядом с реквизитами.
_WANTED = """
SELECT inn, name FROM organization
WHERE okved IS NULL OR okved = ''
ORDER BY inn
"""

_SAVE = """
UPDATE organization SET okved = %(okved)s, updated_at = now()
WHERE inn = %(inn)s
"""

# Сколько отказов подряд считать отказом источника, а не организаций. Каждая
# попытка стоит трёх повторов по тридцать секунд: на семистах организациях
# это девятнадцать часов ожидания ответа, которого нет.
GIVE_UP_AFTER = 5


def main() -> int:
    """Доносит вид деятельности до полного набора; 1 — спрашивать некого."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0

    with connection() as conn:
        wanted = fetch_all(_WANTED, {}, conn=conn)
        if not wanted:
            print("вид деятельности известен у всех организаций базы")
            return 1
        chosen = wanted[:limit] if limit else wanted
        print(f"организаций без вида деятельности {len(wanted)}, спрошено будет {len(chosen)}")
        done = unknown = failed = in_a_row = 0
        stopped = ""
        with GirboSource(journal=False) as source:
            for item in chosen:
                inn = item["inn"]
                try:
                    organization = source.find_organization(inn)
                except OrganizationNotFoundError:
                    unknown += 1
                    continue
                except SourceError as failure:
                    failed += 1
                    in_a_row += 1
                    logger.error("%s: %s", inn, str(failure)[:120])
                    # **Недоступный источник прогон останавливает.** Каждая
                    # попытка стоит трёх повторов по тридцать секунд, и семьсот
                    # организаций подряд — это девятнадцать часов ожидания
                    # ответа, которого нет. Ошибка одной организации прогон
                    # не прекращает: подряд идущие отказы означают источник,
                    # одиночный — организацию.
                    if in_a_row >= GIVE_UP_AFTER:
                        stopped = (
                            f"источник не отвечает: {in_a_row} отказов подряд. "
                            "Прогон остановлен, спрошенное записано"
                        )
                        break
                    continue
                in_a_row = 0
                if not organization.okved:
                    unknown += 1
                    continue
                execute(
                    _SAVE, {"inn": inn, "okved": organization.okved}, conn=conn
                )
                done += 1
                if done % 50 == 0:
                    conn.commit()
                    print(f"  ...{done} из {len(chosen)}", flush=True)
        conn.commit()
    print(
        f"вид деятельности записан у {done}, источник не знает организацию "
        f"либо вида {unknown}, отказов связи {failed}"
    )
    if stopped:
        print(stopped)
    return 1 if stopped and not done else 0


if __name__ == "__main__":
    sys.exit(main())
