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
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.ifrs_view import IfrsMetricsView  # noqa: E402
from finlib.normalize.ifrs_metrics import load_ifrs_metrics  # noqa: E402
from finlib.normalize.lines import load_lines  # noqa: E402
from finlib.report.policy import load_policy, months_between  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

def _coverage(item) -> str:
    """Строка покрытия: что проверено и чего нет.

    **Пустая графа читается как «ничего не проверяли».** У эмитента без
    оснований проверено всё, что маршрут умеет: три величины и события.
    «Долг ✓ (оценка сверху)» отличается от «долг ✓» намеренно — вывод
    по границе доказателен, но это граница, а не величина.
    """
    parts = []
    if item.values.get("net_debt_ebitda") is not None:
        parts.append("долг ✓")
    elif item.values.get("net_debt_op_profit") is not None:
        parts.append("долг ✓ (оценка сверху)")
    else:
        parts.append("долг — нет данных")
    parts.append(
        "капитал ✓" if item.values.get("equity_ratio") is not None
        else "капитал — нет данных"
    )
    parts.append(
        "ликвидность ✓" if item.values.get("cur_liq") is not None
        else "ликвидность — нет данных"
    )
    parts.append("события — нет данных")
    return "проверено: " + ", ".join(parts)


# Четыре исхода правила давности дефолта. Считаются все четыре вместе
# со знаменателем: у правила с четырьмя исходами ноль срабатываний одного
# из них ничего не значит без остальных трёх.
_DEFAULT_OUTCOME_NAMES: dict[str, str] = {
    "emission_default": "дефолт не улажен, до 3 лет: разбор",
    "default_unsettled_stale": "не улажен, старше: вопрос",
    "default_settled_recent": "улажен, до 3 лет: история",
    "default_settled_stale": "улажен, старше: справочно",
}
_DEFAULT_OUTCOMES = frozenset(_DEFAULT_OUTCOME_NAMES)


def rows_of(conn, today: date) -> tuple[list[dict], dict[str, int]]:
    """Строки списка наблюдения и сводка по корзинам и подгруппам.

    Входы и вердикт берёт `scoring.routing_store.routing_rows` — одно место
    на список и на замер распределения: прежде оба собирали величины сами,
    и расхождение «в списке иначе, чем в отчёте» увидеть было бы нечем.
    """
    routing = load_routing()
    view = IfrsMetricsView(load_ifrs_metrics())
    units = load_lines().units
    report_policy = load_policy()
    found, counts = routing_rows(conn, today)

    rows: list[dict] = []
    for item in found:
        verdict = item.verdict
        basket = routing.basket(verdict.basket)
        ground_names = {entry.code: entry.name for entry in basket.grounds}
        months = months_between(item.report_date, today)
        unit = units.name_of(item.unit_code) if item.unit_code else ""
        # **Давность видна всегда.** Более сильное основание её не гасит:
        # признак берётся у вердикта, а не у корзины, и печатается в своей
        # графе даже тогда, когда корзину назвало другое основание.
        overdue = any(
            entry.ground in ("disclosure_overdue", "reporting_two_cycles_old")
            for entry in verdict.findings
        )
        rows.append(
            {
                "name": item.name,
                "inn": item.inn,
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
                            entry.text
                            for entry in verdict.findings
                            if entry.ground == ground
                        ],
                    }
                    for ground in verdict.grounds
                ],
                # **Справочное обстоятельство корзины не называет, но строка
                # о нём молчать не вправе.** Урегулированный дефолт
                # десятилетней давности человек найдёт в карточке сам,
                # и молчание маршрута прочтёт как недосмотр.
                "notes": [entry.text for entry in verdict.notes],
                # **Чистый долг и EBITDA называются порознь.** Отрицательное
                # отношение означает либо чистую денежную позицию, либо убыток,
                # и по одному отношению их не различить.
                # Единица — комплекта, а не стандарта: консолидированная
                # отчётность составляется в миллионах, и «тыс. руб.» у неё —
                # ошибка в тысячу раз, которую не ловит ни один контроль.
                "values": [
                    (
                        view.require(code).name,
                        view.shown(code, value, money=unit or None),
                    )
                    for code, value in item.values.items()
                ],
                "sources": list(item.sources),
                # **Источник, стандарт и контур.** Единица у коэффициентов
                # не информативна, а контур — да: отдельная отчётность
                # управляющей компании и консолидированная группы описывают
                # разные предметы. Пока третий стандарт не заведён,
                # неконсолидированная отчётность по МСФО отбраковывается
                # на приёме, и контур у всех строк один — это честнее, чем
                # печатать графу, которая не различает.
                "origin": " · ".join(
                    (", ".join(item.sources), "МСФО", "консолидированная")
                ),
                "unit": unit,
                # Строка покрытия для «Без внимания»: перечислено то, что
                # проверено, и названо то, чего у нас нет.
                "coverage": _coverage(item),
                "report_date": f"{item.report_date:%d.%m.%Y}",
                "months": months,
                "stale": report_policy.freshness.stale(months),
                "overdue": overdue,
                "assessed": item.assessed_class,
            }
        )
    rows.sort(key=lambda item: (item["order"], item["name"].lower()))
    summary = Counter(item["basket_name"] for item in rows)
    for item in rows:
        for name in item["subgroups"][:1]:
            summary[f"— {name}"] += 1
    summary["исключено поглощённых"] = counts["исключено поглощённых"]
    # Знаменатель правила поручителя: «ноль корзин, взятых у поручителя»
    # без числа самих финансирующих структур неотличим от невыполненного.
    summary["финансирующих структур"] = counts["финансирующих структур"]
    summary["— корзина взята у поручителя"] = counts[
        "из них корзина взята у поручителя"
    ]
    summary["пар с поручителем в списке"] = counts["пар с поручителем в списке"]
    summary["— поднято по поручителю"] = counts["поднято по поручителю"]
    # Верхний десяток по объёму долга и то, у скольких из них покрытие
    # неполное: «ноль затронутых» без числа самих системно значимых
    # неотличим от невыполненного правила.
    summary["системно значимых"] = counts["системно значимых"]
    summary["— с неполным покрытием"] = sum(
        1
        for item in found
        if any(
            entry.ground == "systemic_partial_cover" for entry in item.verdict.findings
        )
    )
    # **Правило давности дефолта называет все четыре исхода и знаменатель.**
    # Ноль давних дефолтов при неизвестном числе эмитентов с признаком
    # неотличим от невыполненного правила, а исходов у правила четыре:
    # разбор, вопрос об урегулировании, кредитная история, справочное.
    marked = 0
    for item in found:
        outcomes = {entry.ground for entry in item.verdict.findings} | {
            entry.ground for entry in item.verdict.notes
        }
        if not outcomes & _DEFAULT_OUTCOMES:
            continue
        marked += 1
    summary["эмитентов с признаком дефолта"] = marked
    for ground, name in _DEFAULT_OUTCOME_NAMES.items():
        summary[f"— {name}"] = sum(
            1
            for item in found
            if any(
                entry.ground == ground
                for entry in tuple(item.verdict.findings) + tuple(item.verdict.notes)
            )
        )
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
    """Одна строка таблицы: главное основание, остальные свёрнуто.

    **Одно главное основание в строке.** Перечень из четырёх формулировок
    читается как список дел, а не как ответ на вопрос «что с эмитентом»:
    главное стоит открыто, остальные — строкой «ещё N» под ним.
    """
    said = [text for entry in item["grounds"] for text in entry["details"]]
    main = said[0] if said else ""
    # Справочное стоит после оснований корзины: оно ничего не решает,
    # но и потеряться не должно.
    rest = said[1:] + list(item["notes"])
    grounds = (
        f'<div class="gn">{html.escape(main)}</div>'
        + (
            '<details class="more"><summary>ещё '
            f'{len(rest)}</summary>'
            + "".join(f'<span class="gd">{html.escape(text)}</span>' for text in rest)
            + "</details>"
            if rest
            else ""
        )
        if main
        # **Строка покрытия у «Без внимания».** Пустая графа читается
        # как «ничего не проверяли», тогда как проверено всё, что маршрут
        # умеет: величины и события.
        else f'<div class="cover">{html.escape(item["coverage"])}</div>'
        + "".join(f'<span class="gd">{html.escape(text)}</span>' for text in rest)
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
        if item["stale"] or item["overdue"]
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
        + f'</td><td class="gs">{grounds}</td><td class="vs">{values}</td>'
        # **Источник, стандарт и контур — одна графа.** Единица у коэффициентов
        # не значит ничего, а вот чья это отчётность и какого она контура —
        # значит: отдельная отчётность управляющей компании и консолидированная
        # группы описывают разные предметы.
        f'<td class="src">{html.escape(item["origin"])}</td>'
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
