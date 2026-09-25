"""Отчётность агрегатора в ежедневном прогоне: МСФО целиком, РСБУ — отбором.

    uv run python scripts/reporting_fetch.py                  # с прошлого прогона
    uv run python scripts/reporting_fetch.py --since 2026-09-22

**МСФО — справочник целиком, два запроса** (`get_report_msfo_real`,
`limit` 5 000). Отбор по дате обновления источник у этого метода пропускает
молча (проба 25.09.2026: контроль «≥ 2030-01-01» вернул все 6 214 строк),
а `update_time` переписан у всех строк разом, поэтому **пересмотр здесь
устанавливается только сравнением величин** до загрузки и после.

**РСБУ — отбором по дате** (решение владельца 25.09.2026): `created_at ≥ дата`
называет новые комплекты, `update_time ≥ дата` — пересмотренные; у отчёта
о движении денежных средств поля называются `created_at` и `updated_at`,
отбора по стране у него нет, и эмитент опознаётся по идентификатору
источника. Названные эмитенты догружаются боевым путём
(`pipeline.accept_cbonds_report`, три запроса на эмитента). ГИР БО остаётся
первоисточником РСБУ: приоритет источников при записи фактов не меняется.

**Новый комплект и пересмотр различаются по базе, а не по словам источника**:
новый — ключа комплекта (ИНН, стандарт, отчётная дата) от агрегатора
до загрузки не было; пересмотр — был, и хоть одна величина агрегатора
изменилась. Итог дня — файл `reporting_delta_ГГГГ-ММ-ДД.json`, его читает
отчёт изменений (раздел «Новая отчётность»).
"""

import json
import logging
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.pipeline import accept_cbonds_report  # noqa: E402
from finlib.sources import cbonds  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = cbonds.CACHE
CARDS = CACHE / "emitents.json"
SINCE = CACHE / "reporting_since.json"

# Величины агрегатора по ключу комплекта: до загрузки и после.
_VALUES = """
SELECT f.inn, f.report_date, f.form_code, f.line_code, f.value
FROM fact_report f
WHERE f.standard = %(standard)s AND f.recognition = 'cbonds'
  AND f.inn = ANY(%(inns)s)
"""

_SETS = """
SELECT inn, period_end FROM src_file
WHERE source = 'cbonds' AND standard = %(standard)s AND inn = ANY(%(inns)s)
"""

# Эмитенты, чья РСБУ агрегатора у нас уже есть: круг догрузки шире перечня
# эмитентов с выпусками — загружено 804 организации.
_RSBU_KNOWN = "SELECT DISTINCT inn FROM src_file WHERE source = 'cbonds' AND standard = 'rsbu'"

# Отбор по дате: метод, поле, отбор по стране есть ли.
RSBU_WINDOWS = (
    ("get_report_rsbu_balance", "created_at", True),
    ("get_report_rsbu_balance", "update_time", True),
    ("get_report_rsbu_profit", "created_at", True),
    ("get_report_rsbu_profit", "update_time", True),
    ("get_report_cash_flow_statement_newform", "created_at", False),
    ("get_report_cash_flow_statement_newform", "updated_at", False),
)


def _window(method: str, field: str, since: str, by_country: bool) -> list[dict]:
    """Записи метода, обновлённые с даты; запись раньше даты — отбор пропущен."""
    filters = [{"field": field, "operator": "ge", "value": since}]
    if by_country:
        filters.insert(0, {"field": "emitent_country_id", "operator": "eq", "value": "1"})
    found = cbonds.fetch(
        method,
        f"window_{method}_{field}_{since}",
        filters=tuple(filters),
        limit=1000,
        refresh=True,
    )
    items = found.get("items", [])
    early = [item for item in items if str(item.get(field) or "")[:10] < since]
    if early:
        raise cbonds.FilterIgnoredError(
            f"{method}: отбор {field} ≥ {since} не применён, {len(early)} записей раньше"
        )
    return items


def rsbu_issuers(since: str, known: set[str], by_id: dict[str, str]) -> set[str]:
    """ИНН, у которых РСБУ агрегатора появилась либо обновилась с даты."""
    found: set[str] = set()
    for method, field, by_country in RSBU_WINDOWS:
        for item in _window(method, field, since, by_country):
            inn = str(item.get("emitent_inn") or "") or by_id.get(
                str(item.get("emitent_id") or ""), ""
            )
            if inn in known:
                found.add(inn)
    return found


def _state_of(conn, standard: str, inns: list[str]) -> tuple[set, dict]:  # noqa: ANN001
    """Ключи комплектов агрегатора и его величины по ключу — снимок базы."""
    sets = {
        (row["inn"], row["period_end"])
        for row in fetch_all(_SETS, {"standard": standard, "inns": inns}, conn=conn)
    }
    values: dict = defaultdict(dict)
    for row in fetch_all(_VALUES, {"standard": standard, "inns": inns}, conn=conn):
        values[(row["inn"], row["report_date"])][(row["form_code"], row["line_code"])] = (
            row["value"]
        )
    return sets, values


def compare(before: tuple[set, dict], after: tuple[set, dict], standard: str) -> tuple[list, list]:
    """Новые комплекты и пересмотры: сравнение снимков базы до и после."""
    sets_before, values_before = before
    sets_after, values_after = after
    new = [
        {"inn": inn, "standard": standard, "period_end": f"{moment}",
         "kind": "годовой" if f"{moment}".endswith("12-31") else "промежуточный"}
        for inn, moment in sorted(sets_after - sets_before)
    ]
    revised = []
    for key in sorted(sets_after & sets_before):
        was, now = values_before.get(key, {}), values_after.get(key, {})
        changed = sum(1 for code, value in now.items() if code in was and was[code] != value)
        if changed:
            revised.append(
                {"inn": key[0], "standard": standard, "period_end": f"{key[1]}",
                 "changed": changed}
            )
    return new, revised


def main() -> int:
    """Доставка отчётности агрегатора за день; 1 — если справочника эмитентов нет."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not CARDS.exists():
        print("карточек эмитентов на диске нет: перечень брать негде")
        return 1
    today = date.today()
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    since = (
        sys.argv[sys.argv.index("--since") + 1]
        if "--since" in sys.argv
        else json.loads(SINCE.read_text(encoding="utf-8"))["since"]
        if SINCE.exists()
        else f"{today}"
    )
    by_id = {str(card.get("id") or ""): inn for inn, card in cards.items()}
    failed = 0

    # --- МСФО: справочник целиком -------------------------------------------
    rows = cbonds.msfo_universe(refresh=True)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        inn = str(row.get("emitent_inn") or "").strip()
        if inn:
            grouped[inn].append(row)
    ifrs_inns = sorted(grouped)
    with connection() as conn:
        before_ifrs = _state_of(conn, "ifrs", ifrs_inns)
    for inn in ifrs_inns:
        try:
            accept_cbonds_report(inn, rows=grouped[inn])
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("МСФО %s: загрузка оборвалась", inn)
    with connection() as conn:
        after_ifrs = _state_of(conn, "ifrs", ifrs_inns)
    new_ifrs, revised_ifrs = compare(before_ifrs, after_ifrs, "ifrs")

    # --- РСБУ: отбором по дате ---------------------------------------------
    with connection() as conn:
        known = set(cards) | {row["inn"] for row in fetch_all(_RSBU_KNOWN, {}, conn=conn)}
    rsbu_inns = sorted(rsbu_issuers(since, known, by_id))
    with connection() as conn:
        before_rsbu = _state_of(conn, "rsbu", rsbu_inns)
    for inn in rsbu_inns:
        try:
            accept_cbonds_report(inn, report="report_rsbu", refresh=True)
        except (cbonds.CbondsError, httpx.TransportError):
            failed += 1
            logger.exception("РСБУ %s: доставка оборвалась", inn)
    with connection() as conn:
        after_rsbu = _state_of(conn, "rsbu", rsbu_inns)
    new_rsbu, revised_rsbu = compare(before_rsbu, after_rsbu, "rsbu")

    (CACHE / f"reporting_delta_{today}.json").write_text(
        json.dumps(
            {
                "since": since,
                "ifrs_issuers": len(ifrs_inns),
                "rsbu_issuers": rsbu_inns,
                "new": new_ifrs + new_rsbu,
                "revised": revised_ifrs + revised_rsbu,
                "failed": failed,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    if not failed:
        SINCE.write_text(json.dumps({"since": f"{today}"}), encoding="utf-8")
    print(
        f"отчётность с {since}: МСФО эмитентов {len(ifrs_inns)}, новых комплектов "
        f"{len(new_ifrs)}, пересмотров {len(revised_ifrs)}; РСБУ эмитентов "
        f"{len(rsbu_inns)}, новых {len(new_rsbu)}, пересмотров {len(revised_rsbu)}; "
        f"отказов {failed}, запросов {cbonds.pace.requested}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
