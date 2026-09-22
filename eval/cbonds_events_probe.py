"""Разведка событийного слоя Cbonds: выпуски и рейтинги. **Только чтение.**

    uv run python eval/cbonds_events_probe.py > data/output/cbonds_events.md

Отвечает на вопросы, заданные до кода: какие поля отдают вновь открытые
методы, есть ли статус выпуска и признак дефолта, как выглядит рейтинг после
отзыва, отвечают ли методы, которых в письме источника не было. В базу
не пишет ничего и правил маршрута не заводит: решение о них принимает человек.

**Отказ и пустота различаются.** Метод, которого нет у нашей подписки, отвечает
404 «invalid resource name»; метод, который отбор молча пропускает, ловится
проверкой применённости фильтра (`_verify_applied`). Оба исхода называются,
а не сводятся к «данных нет».
"""

import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.sources import cbonds  # noqa: E402

logger = logging.getLogger(__name__)

# Эмитенты, по которым эксперт нашёл пропуски слоя отчётности: дефолты между
# отчётными датами, отзыв рейтинга, групповой контур.
ISSUERS: dict[str, str] = {
    "5029169023": "ЕвроТранс",
    "4004021785": "Кириллица",
    "7730176955": "Антерра",
    "1435133520": "ЖКХ РС(Я)",
    "5003077160": "Группа Русагро",
    "7703370008": "Мечел",
    "7420000133": "Уральская кузница",
    "9731004688": "Самолёт",
}

# Методы, о доступе к которым письмо источника сказало, и методы, о которых
# не сказало: второе тоже надо знать — «не названо» не значит «нет».
GRANTED = ("get_emissions", "get_rating_emitent_maxdate", "get_rating_emission_maxdate")
UNNAMED = ("get_emission_guarantors", "get_flow_new", "get_offert")

# Поля выпуска, которые отвечают на вопрос «что с ним сейчас и когда платить».
EMISSION_FIELDS = (
    "isin_code",
    "document_rus",
    "status_name_rus",
    "status_id",
    "has_default",
    "has_unsettled_default",
    "maturity_date",
    "offert_date",
    "offert_date_put",
    "offert_date_call",
    "outstanding_volume",
    "announced_volume_new",
    "guaranteed_bonds",
    "guarantor_name_rus",
    "currency_name_rus",
)

RATING_FIELDS = (
    "agency_name_rus",
    "scale_point_name",
    "scale_point_description_rus",
    "forecast_name_rus",
    "rating_date",
    "scale_name_rus",
    "scale_id",
    "scale_point_id",
)


def ask(method: str, inn: str, field: str = "emitent_inn") -> tuple[list[dict], str]:
    """Ответ метода по эмитенту либо причина отказа словами."""
    try:
        found = cbonds.fetch(
            method,
            f"probe_{method}_{field}_{inn}",
            filters=({"field": field, "operator": "eq", "value": inn},),
            limit=100,
        )
    except cbonds.FilterIgnoredError:
        return [], "отбор по этому полю источник пропускает молча"
    except cbonds.CbondsError as failure:
        text = str(failure)
        if "invalid resource name" in text:
            return [], "метода нет у подписки (404 invalid resource name)"
        return [], f"ошибка источника: {text[:120]}"
    return found.get("items", []), ""


def main() -> int:
    """Печатает разведку; 1 — если не ответил ни один метод."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    print("# Событийный слой Cbonds: что отдают вновь открытые методы\n")

    print("## Доступность методов\n")
    print("| Метод | Исход | Записей у ЕвроТранса |")
    print("|---|---|---|")
    available: list[str] = []
    for method in (*GRANTED, *UNNAMED):
        items, refusal = ask(method, "5029169023")
        if refusal:
            print(f"| `{method}` | {refusal} | — |")
            continue
        available.append(method)
        print(f"| `{method}` | отвечает, отбор по ИНН применён | {len(items)} |")
    if not available:
        print("\n**Не ответил ни один метод: разведки не было, а не «данных нет».**")
        return 1

    if "get_emissions" in available:
        print("\n## Выпуски: статус, дефолт, погашения, поручитель\n")
        print(
            "Поля отвечают на все заданные вопросы: `status_name_rus` — состояние "
            "выпуска, `has_default` и `has_unsettled_default` — дефолт и его "
            "урегулированность, `maturity_date` и `offert_date_put` — погашения "
            "и оферты, `outstanding_volume` — объём в обращении, "
            "`guarantor_name_rus` — поручитель. Отдельный метод поручителей "
            "не нужен: поле есть в самом выпуске.\n"
        )
        statuses: Counter[str] = Counter()
        for inn, label in ISSUERS.items():
            items, refusal = ask("get_emissions", inn)
            if refusal:
                print(f"**{label}** ({inn}): {refusal}\n")
                continue
            statuses.update(str(item.get("status_name_rus")) for item in items)
            defaults = [item for item in items if str(item.get("has_default")) == "1"]
            unsettled = [
                item for item in items if str(item.get("has_unsettled_default")) == "1"
            ]
            outstanding = [
                item
                for item in items
                if str(item.get("status_name_rus") or "").lower().startswith("в обращ")
            ]
            print(
                f"**{label}** ({inn}): выпусков {len(items)}, из них в обращении "
                f"{len(outstanding)}, с дефолтом {len(defaults)}, "
                f"с неурегулированным дефолтом {len(unsettled)}."
            )
            for item in items[:6]:
                fields = {key: item.get(key) for key in EMISSION_FIELDS}
                name = fields.get("document_rus") or fields.get("isin_code") or "—"
                print(
                    f"  - {name}: статус «{fields['status_name_rus']}», дефолт "
                    f"{fields['has_default']}, неурегулированный "
                    f"{fields['has_unsettled_default']}, погашение "
                    f"{fields['maturity_date']}, оферта {fields['offert_date_put']}, "
                    f"в обращении {fields['outstanding_volume']} "
                    f"{fields['currency_name_rus']}, поручитель "
                    f"{fields['guarantor_name_rus'] or '—'}"
                )
            if len(items) > 6:
                print(f"  - …и ещё {len(items) - 6}")
            print("")
        print("### Какие статусы встретились\n")
        print("| Статус | Выпусков |")
        print("|---|---|")
        for name, count in statuses.most_common():
            print(f"| {name} | {count} |")

    if "get_rating_emitent_maxdate" in available:
        print("\n## Рейтинги: агентство, значение, прогноз, дата, отзыв\n")
        print(
            "Отзыв — **не отдельный признак, а значение шкалы**: "
            "`scale_point_name = Withdrawn`. Значит, «отозван» и «понижен» "
            "различаются только точкой шкалы, а история понижений из метода "
            "`…_maxdate` не восстанавливается вовсе — он отдаёт последнее "
            "значение по каждой паре «агентство, шкала».\n"
        )
        for inn, label in ISSUERS.items():
            items, refusal = ask("get_rating_emitent_maxdate", inn)
            if refusal:
                print(f"**{label}** ({inn}): {refusal}\n")
                continue
            print(f"**{label}** ({inn}): записей {len(items)}\n")
            print("| Агентство | Значение | Прогноз | Дата | Шкала |")
            print("|---|---|---|---|---|")
            for item in items:
                print(
                    f"| {item.get('agency_name_rus')} | {item.get('scale_point_name')} "
                    f"| {item.get('forecast_name_rus') or '—'} "
                    f"| {str(item.get('rating_date') or '')[:10]} "
                    f"| {item.get('scale_name_rus')} |"
                )
            print("")

    print(
        f"\nЗапросов к источнику: {cbonds.pace.requested}, ответов с диска: "
        f"{cbonds.pace.from_cache}. Все ответы сохранены в `data/raw/cbonds/`."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
