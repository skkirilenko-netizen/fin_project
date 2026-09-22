"""Список наблюдения: один HTML-файл со всеми эмитентами и их корзинами.

    uv run python eval/watchlist_run.py            # data/output/watchlist_<дата>.html
    uv run python eval/watchlist_run.py --out ПУТЬ

**Это чтение, и ничего кроме.** Из интерфейса нельзя ни исправить корзину,
ни подтвердить комплект: решение о комплекте принимается командой с автором
и уходит в журнал, а страница, позволяющая менять оценку мышью, оставляет
решение без следа. Файл открывается в браузере и никуда не обращается —
ни к сети, ни к базе: локальный контур, данные не покидают машину.

**Величины и основания берёт боевой путь.** Показатели — расчёт по фактам
(`metrics.ifrs_store.compute_from_facts`), корзину и подгруппу — маршрутизация
(`scoring.routing.route`), стоп-факторы — оценка (`scoring.ifrs_store`).
Прогон собирает страницу и считает сводку.

**Технических кодов на странице нет.** Основание называется наименованием
из справочника маршрутизации, стоп-фактор — своим наименованием, величина —
через единую точку округления. Код показателя человеку ничего не говорит,
а предмет и величина говорят.
"""

import html
import json
import logging
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.ifrs_store import compute_from_facts  # noqa: E402
from finlib.metrics.ifrs_view import IfrsMetricsView  # noqa: E402
from finlib.normalize.ifrs_issuer_type import load_issuer_types  # noqa: E402
from finlib.normalize.ifrs_metrics import load_ifrs_metrics  # noqa: E402
from finlib.report.policy import load_policy, months_between  # noqa: E402
from finlib.scoring.ifrs_store import stop_factors_of  # noqa: E402
from finlib.scoring.routing import ROUTING_METRICS, load_routing, route  # noqa: E402

logger = logging.getLogger(__name__)

# Те же выборки, что у замера распределения: страница и замер обязаны
# показывать одно, иначе «в списке иначе, чем в отчёте» станет нормой.
_LATEST = """
SELECT f.inn, max(f.report_date) AS report_date, max(o.name) AS name
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
LEFT JOIN organization o ON o.inn = f.inn
WHERE f.standard = 'ifrs' AND s.is_actual AND s.status <> 'quarantine'
GROUP BY f.inn
"""

# **Выборка называет стандарт, и это не формальность.** Те же коды проверок
# нуля пишет доставка РСБУ, и без стандарта запись о комплекте РСБУ отправляла
# бы в разбор эмитента по его комплекту МСФО — ровно то смешение, о котором
# правило: всякая выборка по ИНН обязана называть стандарт. Поймано числами:
# после загрузки РСБУ корзина разбора выросла на двух эмитентов.
_ZERO_FAILED = """
SELECT DISTINCT d.inn, s.report_year
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE d.status = 'fail' AND s.standard = 'ifrs' AND d.check_code IN (
    'cbonds_identity_mismatch', 'cbonds_sections_mismatch', 'cbonds_zero_total'
)
"""

_OPERATING_PROFIT = """
SELECT f.value FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = 'ifrs' AND f.report_date = %(d)s
  AND f.line_code = 'ifrs.operating_profit' AND s.is_actual
  AND s.status <> 'quarantine'
ORDER BY source_rank(s.source)
LIMIT 1
"""

# Способ получения: документ эмитента или доставка агрегатора. Это свойство
# комплекта, а не организации, и у одного периода их бывает два.
_SOURCES = """
SELECT DISTINCT source FROM src_file
WHERE inn = %(inn)s AND standard = 'ifrs' AND is_actual
  AND status <> 'quarantine' AND report_year = %(year)s
"""

_ASSESSED = """
SELECT a.inn, a.class_code, a.report_date
FROM assessment a
WHERE a.standard = 'ifrs' AND a.class_code IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM src_file s
      WHERE s.inn = a.inn AND s.standard = 'ifrs' AND s.source <> 'cbonds'
        AND s.report_year = EXTRACT(YEAR FROM a.report_date)::int
  )
ORDER BY a.inn, a.report_date DESC
"""

SOURCE_NAMES = {"file": "PDF", "gir_bo": "ГИР БО", "cbonds": "Cbonds"}


def spv_issuers() -> set[str]:
    """ИНН с признаком финансирующей структуры из справочника эмитентов."""
    cards = Path("data/raw/cbonds/emitents.json")
    if not cards.exists():
        return set()
    found = json.loads(cards.read_text(encoding="utf-8"))
    return {inn for inn, card in found.items() if str(card.get("emitent_spv")) == "1"}


def rows_of(conn, today: date) -> tuple[list[dict], dict[str, int]]:
    """Строки списка наблюдения и сводка по корзинам и подгруппам."""
    policy = load_ifrs_metrics()
    routing = load_routing()
    types = load_issuer_types()
    view = IfrsMetricsView(policy)
    report_policy = load_policy()
    spv = spv_issuers()
    assessed: dict[str, str] = {}
    for row in fetch_all(_ASSESSED, {}, conn=conn):
        assessed.setdefault(row["inn"], row["class_code"])
    quarantined = {
        (row["inn"], row["report_year"]) for row in fetch_all(_ZERO_FAILED, {}, conn=conn)
    }

    rows: list[dict] = []
    for row in fetch_all(_LATEST, {}, conn=conn):
        inn, moment = row["inn"], row["report_date"]
        computed = compute_from_facts(inn, moment, conn, policy)
        profit = fetch_all(_OPERATING_PROFIT, {"inn": inn, "d": moment}, conn=conn)
        stops = stop_factors_of(inn, moment, computed, conn)
        verdict = route(
            computed,
            quarantined=(inn, moment.year) in quarantined,
            stop_factors=stops.triggered,
            financing_structure=inn in spv,
            operating_profit=Decimal(profit[0]["value"]) if profit else None,
            latest_annual=moment,
            assessed_class=assessed.get(inn),
            today=today,
            policy=policy,
            routing=routing,
            types=types,
        )
        basket = routing.basket(verdict.basket)
        ground_names = {item.code: item.name for item in basket.grounds}
        sources = [
            SOURCE_NAMES.get(item["source"], item["source"])
            for item in fetch_all(
                _SOURCES, {"inn": inn, "year": moment.year}, conn=conn
            )
        ]
        months = months_between(moment, today)
        values = []
        for code in ROUTING_METRICS:
            item = next(
                (entry for entry in computed if entry.code == code and entry.calculable),
                None,
            )
            if item is None:
                continue
            values.append((item.name, view.shown(code, item.value)))
        rows.append(
            {
                "name": (row["name"] or inn).strip(),
                "inn": inn,
                "basket": verdict.basket,
                "basket_name": verdict.basket_name,
                "order": basket.order,
                "subgroups": list(verdict.subgroup_names),
                "actions": list(verdict.actions),
                # Основание — наименование справочника, а под ним предмет
                # с величиной: без них корзина остаётся словом без опоры.
                "grounds": [
                    {
                        "name": ground_names.get(ground, ground),
                        "details": [
                            item.text
                            for item in verdict.findings
                            if item.ground == ground
                        ],
                    }
                    for ground in verdict.grounds
                ],
                "values": values,
                "sources": sorted(set(sources)),
                "report_date": f"{moment:%d.%m.%Y}",
                "months": months,
                "stale": months > report_policy.freshness.max_months,
                "assessed": assessed.get(inn, ""),
            }
        )
    rows.sort(key=lambda item: (item["order"], item["name"].lower()))
    summary = Counter(item["basket_name"] for item in rows)
    for item in rows:
        for name in item["subgroups"][:1]:
            summary[f"— {name}"] += 1
    return rows, dict(summary)


def render(rows: list[dict], summary: dict[str, int], routing, today: date) -> str:
    """Собирает страницу: сводка, фильтры, таблица. Только чтение."""
    baskets = [(item.code, item.name) for item in routing.ordered()]
    subgroups: list[str] = []
    for basket in routing.ordered():
        subgroups.extend(item.name for item in basket.groups)
    counts = "".join(
        f'<div class="card"><div class="num">{count}</div>'
        f'<div class="cap">{html.escape(name)}</div></div>'
        for name, count in summary.items()
    )
    options = "".join(
        f'<option value="{html.escape(code)}">{html.escape(name)}</option>'
        for code, name in baskets
    )
    group_options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>'
        for name in subgroups
    )
    body = "".join(_row_html(item) for item in rows)
    status = (
        f"структура правил {routing.status}"
        + (f" ({routing.approved_by})" if routing.approved_by else "")
        + f", пороги {routing.thresholds}"
    )
    return _PAGE.format(
        today=f"{today:%d.%m.%Y}",
        total=len(rows),
        status=html.escape(status),
        version=html.escape(routing.version),
        cards=counts,
        options=options,
        groups=group_options,
        rows=body,
    )


def _row_html(item: dict) -> str:
    """Одна строка таблицы."""
    grounds = "".join(
        f'<div class="g"><span class="gn">{html.escape(entry["name"])}</span>'
        + "".join(
            f'<span class="gd">{html.escape(text)}</span>' for text in entry["details"]
        )
        + "</div>"
        for entry in item["grounds"]
    )
    values = "".join(
        f'<div class="v"><span class="vn">{html.escape(name)}</span>'
        f'<span class="vv">{html.escape(shown)}</span></div>'
        for name, shown in item["values"]
    )
    subgroup = item["subgroups"][0] if item["subgroups"] else ""
    others = ", ".join(item["subgroups"][1:])
    action = item["actions"][0] if item["actions"] else ""
    fresh = (
        f'<span class="stale">{item["report_date"]} · {item["months"]} мес.</span>'
        if item["stale"]
        else f'{item["report_date"]} · {item["months"]} мес.'
    )
    assessed = (
        f'<span class="cls">класс {html.escape(item["assessed"])}</span>'
        if item["assessed"]
        else ""
    )
    return (
        f'<tr data-basket="{html.escape(item["basket"])}" '
        f'data-group="{html.escape(subgroup)}" '
        f'data-name="{html.escape(item["name"].lower())}">'
        f'<td class="nm">{html.escape(item["name"])} {assessed}</td>'
        f'<td class="inn">{html.escape(item["inn"])}</td>'
        f'<td class="bk b-{html.escape(item["basket"])}">'
        f'{html.escape(item["basket_name"])}</td>'
        f'<td class="sg">{html.escape(subgroup)}'
        + (f'<span class="act">{html.escape(action)}</span>' if action else "")
        + (f'<span class="oth">ещё: {html.escape(others)}</span>' if others else "")
        + f"</td><td class=\"gs\">{grounds}</td><td class=\"vs\">{values}</td>"
        f'<td class="src">{html.escape(", ".join(item["sources"]))}</td>'
        f'<td class="fr">{fresh}</td></tr>'
    )


_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Список наблюдения</title>
<style>
  :root {{
    --bg: #fbfbf9; --fg: #1c1b19; --mut: #6b6862; --line: #e2ded6;
    --review: #b3261e; --attention: #8a6100; --clear: #1f6b3a; --card: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #17181a; --fg: #ececec; --mut: #9c9a95; --line: #2e3033;
      --review: #ff8a80; --attention: #ffca6a; --clear: #7ad39a; --card: #1e2022;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--fg);
    font: 15px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  .wrap {{ max-width: 1240px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: var(--mut); font-size: 13px; margin-bottom: 18px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 14px; min-width: 132px;
  }}
  .num {{ font-size: 22px; font-weight: 600; }}
  .cap {{ color: var(--mut); font-size: 12px; }}
  .bar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
  select, input {{
    font: inherit; padding: 7px 10px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--card); color: var(--fg);
  }}
  input {{ min-width: 220px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{
    text-align: left; vertical-align: top; padding: 9px 10px;
    border-bottom: 1px solid var(--line); font-size: 13px;
  }}
  th {{
    position: sticky; top: 0; background: var(--bg); font-size: 12px;
    text-transform: uppercase; letter-spacing: .04em; color: var(--mut);
  }}
  .nm {{ font-weight: 600; min-width: 200px; }}
  .inn {{ font-variant-numeric: tabular-nums; color: var(--mut); }}
  .bk {{ font-weight: 600; white-space: nowrap; }}
  .b-review {{ color: var(--review); }}
  .b-attention {{ color: var(--attention); }}
  .b-clear {{ color: var(--clear); }}
  .sg {{ min-width: 150px; }}
  .act, .oth {{ display: block; color: var(--mut); font-size: 12px; }}
  .g {{ margin-bottom: 6px; }}
  .gn {{ display: block; }}
  .gd {{ display: block; color: var(--mut); font-size: 12px; }}
  .v {{ display: flex; justify-content: space-between; gap: 10px; }}
  .vn {{ color: var(--mut); }}
  .vv {{ font-variant-numeric: tabular-nums; }}
  .vs {{ min-width: 220px; }}
  .fr {{ white-space: nowrap; }}
  .stale {{ color: var(--review); }}
  .cls {{
    font-size: 11px; color: var(--mut); border: 1px solid var(--line);
    border-radius: 6px; padding: 1px 5px; white-space: nowrap;
  }}
  .foot {{ color: var(--mut); font-size: 12px; margin-top: 18px; }}
  @media (max-width: 720px) {{
    .vs, .gs {{ min-width: 0; }}
    th, td {{ padding: 8px 6px; font-size: 12px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Список наблюдения</h1>
  <div class="sub">
    {total} эмитентов, собрано {today}. Правила маршрутизации {version}:
    {status}. Страница только читает: корзина и основания получены расчётом,
    исправить их отсюда нельзя.
  </div>
  <div class="cards">{cards}</div>
  <div class="bar">
    <select id="basket"><option value="">все корзины</option>{options}</select>
    <select id="group"><option value="">все подгруппы</option>{groups}</select>
    <input id="search" type="search" placeholder="поиск по наименованию"
           autocomplete="off">
    <span id="shown" class="cap"></span>
  </div>
  <table>
    <thead><tr>
      <th>Эмитент</th><th>ИНН</th><th>Корзина</th><th>Подгруппа</th>
      <th>Основания</th><th>Величины маршрута</th><th>Источник</th>
      <th>Отчётность</th>
    </tr></thead>
    <tbody id="body">{rows}</tbody>
  </table>
  <div class="foot">
    Корзина по умолчанию упорядочена: разбор, внимание, без внимания.
    Величины печатаются той же разрядностью, что в заключении.
  </div>
</div>
<script>
  const rows = Array.from(document.querySelectorAll('#body tr'));
  const basket = document.getElementById('basket');
  const group = document.getElementById('group');
  const search = document.getElementById('search');
  const shown = document.getElementById('shown');
  function apply() {{
    const b = basket.value, g = group.value;
    const q = search.value.trim().toLowerCase();
    let visible = 0;
    for (const row of rows) {{
      const ok = (!b || row.dataset.basket === b)
        && (!g || row.dataset.group === g)
        && (!q || row.dataset.name.includes(q));
      row.hidden = !ok;
      if (ok) visible++;
    }}
    shown.textContent = 'показано ' + visible + ' из ' + rows.length;
  }}
  basket.addEventListener('change', apply);
  group.addEventListener('change', apply);
  search.addEventListener('input', apply);
  apply();
</script>
</body>
</html>
"""


def main() -> int:
    """Собирает файл списка наблюдения; 1 — если эмитентов не нашлось."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    out = Path(f"data/output/watchlist_{today:%Y-%m-%d}.html")
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    with connection() as conn:
        rows, summary = rows_of(conn, today)
    if not rows:
        print(
            "эмитентов с комплектом вне карантина нет: страница не собрана. "
            "Это не пустой список, а отсутствие данных."
        )
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, summary, load_routing(), today), encoding="utf-8")
    print(f"{out}: эмитентов {len(rows)}")
    for name, count in summary.items():
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
