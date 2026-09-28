"""Отчёт «что изменилось»: кто сменил корзину и почему.

Устройство утверждено 23.09.2026 (`docs/change_report.md`). Главное требование —
**читается за пять минут**: не перечень из 900 строк, а десяток изменений,
у каждого из которых названа причина.

**Изменение — смена вердикта, и сравнивается он кодами, а не текстами.**
Правка одного слова в методике дала бы 900 «изменений», и отчёт, в котором
их 900, не читается ни в какой день.

**Три причины изменения разводятся отпечатком входов.** Изменился отпечаток —
причина у эмитента; тот же при изменившихся версиях кода и методики — причина
у нас; тот же при тех же версиях — **беспричинное изменение**, то есть дефект
недетерминированности, и это остановка, а не строка отчёта.

**Наблюдение и пересчёт между собой не сравниваются.** Пересчёт знает меньше
по устройству — признаки карточки истории не имеют и в него не идут, — и
разница читалась бы как изменение у эмитента.

    uv run python eval/change_report_run.py                  # последняя пара точек
    uv run python eval/change_report_run.py --on 2026-05-06  # отчёт того дня
    uv run python eval/change_report_run.py --kind backfill

**Замер не считает сам**: корзины он берёт из истории, записанной боевой
маршрутизацией, и своей арифметики не имеет.
"""

import json
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.cbonds_events import SNAPSHOTS, events_of  # noqa: E402
from finlib.sources.moex_risk import risk_sectors  # noqa: E402

logger = logging.getLogger(__name__)

_DATES = """
SELECT DISTINCT as_of FROM routing_history WHERE kind = %(kind)s ORDER BY as_of
"""

_POINTS = """
SELECT h.inn, h.basket, h.subgroup, h.grounds, h.fingerprint, h.standard,
       h.report_date, r.code_version, r.methodology
FROM routing_history h LEFT JOIN routing_run r ON r.id = h.run_id
WHERE h.kind = %(kind)s AND h.as_of = %(as_of)s
"""

# Здоровье доставок того дня, о котором отчёт. Берётся последний прогон дня:
# прогон может быть повторён руками, и отчёт говорит о последнем.
_HEALTH = """
SELECT status, sources, note, finished_at FROM routing_run
WHERE as_of = %(as_of)s AND kind = %(kind)s
ORDER BY started_at DESC LIMIT 1
"""

# Наименования корзин и оснований берутся у справочника: технических кодов
# в отчёте нет — их читает не человек.
_NAMES: dict[str, str] = {}


def _basket_name(routing, code: str) -> str:  # noqa: ANN001
    """Наименование корзины по коду."""
    return routing.basket(code).name if code else "нет"


def _ground_names(routing) -> dict[str, str]:  # noqa: ANN001
    """Код основания → наименование, одним словарём на все корзины."""
    if not _NAMES:
        for basket in routing.baskets:
            for ground in basket.grounds:
                _NAMES[ground.code] = ground.name
        for ground in routing.reference:
            _NAMES[ground.code] = ground.name
    return _NAMES


def _why(  # noqa: ANN001
    routing, inn: str, since: date, until: date, appeared: set[str], before, after
) -> str:
    """Чем изменились данные: слой, в котором нашлась датированная запись.

    **Слой называется существующим перечнем** `routing.ground_sources` —
    второго не заводится. Причина, не нашедшаяся в слое, так и печатается:
    изменение с неназванной причиной читалось бы как объяснённое.
    """
    said: list[str] = []
    # **Раскрытие отчётности датированной записи не имеет**, и видно оно
    # по тому, что сменился отчётный период строки: это и есть причина,
    # а не следствие, и называть её основанием значило бы назвать следствие.
    if before["report_date"] != after["report_date"]:
        was = (
            f"{before['report_date']:%d.%m.%Y}"
            if before["report_date"]
            else "отчётности не было"
        )
        now = (
            f"{after['report_date']:%d.%m.%Y}"
            if after["report_date"]
            else "отчётность исчезла"
        )
        said.append(f"отчётный период: {was} → {now}")
    events = events_of(inn)
    for item in events.records:
        if item.moment is not None and since < item.moment <= until:
            what = "не исполнено" if not item.settled else "исполнено"
            said.append(
                f"{item.kind.lower()} {item.moment:%d.%m.%Y}, {what}"
            )
    for item in events.ratings:
        if item.assigned is not None and since < item.assigned <= until:
            said.append(f"{item.agency}: {item.point} {item.assigned:%d.%m.%Y}")
    risky = risk_sectors()
    for issue in events.issues:
        entry = risky.get(issue.isin) if issue.isin else None
        if entry is not None and entry.since is not None and since < entry.since <= until:
            said.append(
                f"{issue.name} переведён в {entry.board} {entry.since:%d.%m.%Y}"
            )
    if said:
        return "; ".join(dict.fromkeys(said))
    # **Окно двенадцати месяцев едет вместе с днём.** Платёж, до которого
    # оставалось тринадцать месяцев, через неделю в него попадает: данных
    # новых нет, а обязательств ближайшего года стало больше. Перечень таких
    # оснований объявлен методикой — это свойство самой меры.
    window = set(routing.refinancing.window_driven)
    if appeared and appeared <= window:
        return "в окно двенадцати месяцев вошли платежи по графику"
    # **Окно едет в обе стороны, и называть надо обе.** Платёж, попавший
    # в окно неделю назад, через год из него выходит тем же движением дня;
    # названное в одну сторону, оно во вторую печаталось «слой не назван» —
    # то есть одно и то же обстоятельство выглядело объяснённым и
    # необъяснённым в зависимости от знака.
    vanished = set(before["grounds"]) - set(after["grounds"])
    if vanished and vanished <= window:
        return "из окна двенадцати месяцев вышли платежи по графику"
    # Отчётность датированной записи не имеет — её раскрытие видно по тому,
    # что сменился отчётный период строки.
    if appeared:
        names = _ground_names(routing)
        return "появилось основание: " + ", ".join(
            names.get(code, code) for code in sorted(appeared)
        )
    return "данные изменились, слой не назван"


_NAMED: dict[str, str] = {}


def _named(inn: str) -> str:
    """Эмитент наименованием, а не ИНН: читает отчёт человек.

    Наименование берётся у справочника эмитентов; ИНН остаётся рядом — им
    строка и ищется в списке.
    """
    if not _NAMED:
        from finlib.scoring.routing_store import cards

        for key, card in cards().items():
            _NAMED[key] = str(card.get("name_rus") or "").strip()
    name = _NAMED.get(inn) or ""
    return f"{name} ({inn})" if name else inn


def _calendar(routing, row: dict, when: date) -> tuple:  # noqa: ANN001
    """Что решает календарь при тех же данных: срок раскрытия и давность.

    **День — тоже довод, и он меняется сам.** Срок сдачи годовой отчётности
    наступает 1 июня, давность дефолта истекает через три года, давность
    отзыва рейтинга — через год: в эти дни вердикт меняется при неизменных
    данных и неизменной методике. Это не изменение у эмитента и не наше:
    это календарь, и он назван отдельной причиной.

    Без него такая смена попала бы в беспричинные — то есть в остановку, —
    и первый же новый год объявил бы расчёт недетерминированным: на неделе
    05.01.2026 у 35 структурных эмитентов основание срока раскрытия
    перестаёт срабатывать разом.
    """
    latest = row["report_date"]
    return (
        routing.freshness.stale(latest, when),
        routing.freshness.cycles_behind(latest, when),
    )


def _urgent(routing, now, previous: date, until: date) -> None:  # noqa: ANN001
    """Срочное: дефолт и рейтинговое действие в тот же день, когда пришли.

    **Недельный отчёт не должен задерживать событие на неделю.** Корзину
    такой эмитент чаще всего меняет, и он есть в основном перечне, — но там
    он стоит наравне с изменившимся семь дней назад, а событие вчерашнего дня
    старше по сроку вмешательства. Поэтому оно печатается первым и за сутки,
    а не за неделю.

    Раздел показывается и тогда, когда корзина не изменилась: дефолт
    у эмитента, уже стоящего в «Разборе», — сведение, которое нельзя терять.
    """
    said: list[str] = []
    for inn in now:
        events = events_of(inn)
        for item in events.records:
            if item.moment is not None and previous < item.moment <= until:
                what = "не исполнено" if not item.settled else "исполнено"
                said.append(
                    f"- {_named(inn)}: {item.kind.lower()} "
                    f"{item.moment:%d.%m.%Y}, {what}"
                )
        # **Срочно не всякое рейтинговое действие, а то, по которому
        # действуют.** Подтверждение AAA не событие: агентство сказало
        # то же, что и раньше. Отбираются категории, которые методика
        # объявила основанием, и отзыв — исчезновение мнения. Остальные
        # видны в карточке эмитента, и место им там.
        watched = set(routing.events.review_categories) | set(
            routing.events.attention_categories
        )
        seen: set[tuple[str, str]] = set()
        for item in events.ratings:
            if item.assigned is None or not (previous < item.assigned <= until):
                continue
            # ESG-рейтинг о кредитоспособности не говорит, и в срочное
            # он не идёт: вид шкалы объявлен справочником источника.
            if not item.credit:
                continue
            if item.point.strip().lower() != "withdrawn" and (
                item.category not in watched
            ):
                continue
            # У агентства две шкалы — национальная и собственной
            # кредитоспособности, — и обе дают одно действие: две строки
            # об одном читались бы как два события.
            key = (item.agency, f"{item.assigned}")
            if key in seen:
                continue
            seen.add(key)
            said.append(
                f"- {_named(inn)}: {item.agency} — {item.point} "
                f"{item.assigned:%d.%m.%Y}"
            )
    # Счётчик считает то, что напечатано: перечень с повторами назвал бы
    # одно событие двумя.
    said = list(dict.fromkeys(said))
    print(f"## Срочное за сутки ({previous:%d.%m.%Y} → {until:%d.%m.%Y}): {len(said)}\n")
    if not said:
        print(
            "ни одного события. Это сведение, а не пустая строка: сутки "
            "без дефолтов и рейтинговых действий — обычное состояние рынка.\n"
        )
        return
    # Неисполненное обязательство старше рейтингового действия, а исполненное
    # младше обоих: порядок здесь — очередь вмешательства, а не алфавит.
    def weight(line: str) -> int:
        if "не исполнено" in line:
            return 0
        return 2 if "исполнено" in line else 1

    for line in sorted(said, key=weight):
        print(line)
    print()


def _read(conn, kind: str, moment: date) -> dict[str, dict]:  # noqa: ANN001
    """Точки истории на дату: ИНН → вердикт."""
    return {
        row["inn"]: row
        for row in fetch_all(_POINTS, {"kind": kind, "as_of": moment}, conn=conn)
    }


def main() -> int:
    """Печатает отчёт изменений между двумя соседними точками истории."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    kind = "backfill"
    if "--kind" in sys.argv:
        kind = sys.argv[sys.argv.index("--kind") + 1]
    routing = load_routing()
    with connection() as conn:
        dates = [row["as_of"] for row in fetch_all(_DATES, {"kind": kind}, conn=conn)]
        if len(dates) < 2:
            print(
                "# Отчёт изменений\n\nТочек истории меньше двух: сравнивать "
                "не с чем. Это не «изменений нет» — это отсутствие истории.\n"
            )
            return 1
        until = dates[-1]
        if "--on" in sys.argv:
            until = date.fromisoformat(sys.argv[sys.argv.index("--on") + 1])
            if until not in dates:
                print(f"# Отчёт изменений\n\nТочки {until} в истории нет.\n")
                return 1
        # **Сравнивается предыдущая точка, а не «вчера»**; `--since` называет
        # другую, когда надо посмотреть отчёт за неделю целиком: обычный день
        # чаще всего пуст, и по нему не видно, как отчёт выглядит с делом.
        if "--since" in sys.argv:
            since = date.fromisoformat(sys.argv[sys.argv.index("--since") + 1])
            if since not in dates:
                print(f"# Отчёт изменений\n\nТочки {since} в истории нет.\n")
                return 1
        else:
            # **По умолчанию неделя, а не день** (решение человека 23.09.2026):
            # медиана обычного дня — ноль, и пустой отчёт каждый день приучает
            # не открывать. Берётся ближайшая точка не позже недели назад;
            # если такой нет, берётся самая ранняя — сравнивать всё равно
            # с чем-то надо, и разрыв назван в заголовке.
            week = until - timedelta(days=7)
            earlier = [item for item in dates if item <= week] or [
                item for item in dates if item < until
            ]
            since = max(earlier)
        # Срочное смотрится за сутки, а не за неделю: дефолт и рейтинговое
        # действие показываются в тот же день, когда пришли.
        previous = max((item for item in dates if item < until), default=since)
        was, now = _read(conn, kind, since), _read(conn, kind, until)
        bonds = set(bond_issuers())
        health = fetch_all(_HEALTH, {"as_of": until, "kind": kind}, conn=conn)
        _report(routing, kind, since, until, was, now, bonds, previous, health)
    return 0


def _health(kind: str, rows: list) -> None:
    """Здоровье доставок — первым, до всяких изменений.

    **«Изменений нет» при недошедшей доставке и при полной — разные
    сведения, а выглядят одинаково.** Правило объявлено с заведения
    ежедневного прогона, и до 24.09.2026 отчёт его не исполнял: отказ
    источника лежал в журнале прогона и в отчёт не попадал вовсе.
    """
    if kind != "run":
        print(
            "*Это пересчёт назад, а не наблюдение: доставок в нём нет "
            "по устройству, и здоровье источников к нему не относится.*\n"
        )
        return
    if not rows:
        print(
            "> **Записи прогона за этот день нет.** Отчёт собран по истории, "
            "а чем она получена — неизвестно: это не «доставки прошли».\n"
        )
        return
    said = rows[0]
    sources = said.get("sources") or []
    failed = [
        item
        for item in sources
        # «cached» — стадия прошла, а файл дня не обновила: новых данных
        # не пришло, и молчать об этом значило бы выдать кэш за доставку.
        # «offline» — нет сети у нас: источник не спрашивался вовсе.
        if str(item.get("status"))
        in ("failed", "offline", "no_quota", "stopped", "cached")
    ]
    if failed:
        print("> **Доставка неполна, и список собран на том, что дошло.**\n>")
        for item in failed:
            why = item.get("why") or item.get("error") or "причина не записана"
            print(f"> - {item.get('name', item.get('code'))}: {why}")
        print(
            f">\n> Прогон завершён со статусом «{said.get('status')}»"
            + (f": {said['note']}" if said.get("note") else "")
            + ". Изменений ниже могло не быть просто потому, что новых "
            "данных не пришло.\n"
        )
        return
    names = ", ".join(
        f"{item.get('name', item.get('code'))} — {item.get('status')}"
        for item in sources
    )
    print(
        f"*Доставки дня: {names or 'ни одной не объявлено'}. Прогон — "
        f"«{said.get('status')}».*\n"
    )
    _off_hour(said.get("finished_at"))


def _snapshot_of(day: date) -> dict | None:
    """Снимок рейтингов за день; None — файла нет."""
    path = SNAPSHOTS / f"{day:%Y-%m-%d}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _reporting_of(day: date) -> dict | None:
    """Итог доставки отчётности агрегатора за день; None — доставки не было."""
    path = SNAPSHOTS.parent / f"reporting_delta_{day:%Y-%m-%d}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _new_reporting(found: dict | None, names: dict[str, str] | None = None) -> None:
    """Раздел «Новая отчётность»: новые комплекты и пересмотры дня.

    **Новый комплект и пересмотр — разные сведения.** Новый говорит, что
    об эмитенте стало известно больше; пересмотр — что изменилось уже
    известное, и число изменённых величин называется рядом. Смена корзины
    от нового комплекта видна ниже как изменение «у эмитента»; здесь —
    что именно пришло.
    """
    print("## Новая отчётность\n")
    if found is None:
        print("Доставки отчётности агрегатора за этот день не было.\n")
        return
    if names is None:
        cards = SNAPSHOTS.parent / "emitents.json"
        names = (
            {
                inn: str(card.get("name_rus") or inn)
                for inn, card in json.loads(cards.read_text(encoding="utf-8")).items()
            }
            if cards.exists()
            else {}
        )
    new = found.get("new") or []
    revised = found.get("revised") or []
    print(
        f"С {found.get('since')}: новых комплектов **{len(new)}**, пересмотров "
        f"**{len(revised)}**; отказов доставки {found.get('failed', 0)}.\n"
    )
    if new:
        print("| Эмитент | ИНН | Стандарт | Отчётная дата | Вид |")
        print("|---|---|---|---|---|")
        for item in new[:40]:
            print(
                f"| {names.get(item['inn'], item['inn'])} | {item['inn']} "
                f"| {'МСФО' if item['standard'] == 'ifrs' else 'РСБУ'} "
                f"| {item['period_end']} | {item['kind']} |"
            )
        if len(new) > 40:
            print(f"\nПоказаны 40 из {len(new)}.")
        print()
    if revised:
        print("| Эмитент | ИНН | Стандарт | Отчётная дата | Изменено величин |")
        print("|---|---|---|---|---|")
        for item in revised[:40]:
            print(
                f"| {names.get(item['inn'], item['inn'])} | {item['inn']} "
                f"| {'МСФО' if item['standard'] == 'ifrs' else 'РСБУ'} "
                f"| {item['period_end']} | {item['changed']} |"
            )
        if len(revised) > 40:
            print(f"\nПоказаны 40 из {len(revised)}.")
        print()


def _ratings_health(found: dict | None) -> None:
    """Полнота снимка рейтингов за день — в шапке, рядом со здоровьем доставок.

    **Доставка «прошла» ещё не значит, что снимок полон.** Эмитент без ответа
    уходит в `refused`, и снимок идёт дальше; маршрут берёт для него последнее
    наблюдение, а не делает из молчания отзыв. Но читатель обязан знать,
    что по части эмитентов сегодняшнего наблюдения нет: «рейтинговых
    изменений нет» у них значит «не спрашивали успешно».
    """
    if found is None:
        print(
            "> **Снимка рейтингов за этот день нет.** Маршрут построен "
            "по последнему снимку: рейтинговых действий дня в нём нет.\n"
        )
        return
    got = len(found.get("issuers") or {})
    refused = found.get("refused") or {}
    if not refused:
        return
    total = got + len(refused)
    print(
        f"> **Снимок рейтингов неполный: {got} из {total}.** По {len(refused)} "
        "эмитентам источник не ответил; для них взято последнее наблюдение, "
        "и рейтинговых действий дня по ним в отчёте нет.\n"
    )


# Час, на который поставлен агент. Прогон, пошедший не в свой час, —
# это либо ручной запуск, либо пропущенный календарный, выполненный
# при пробуждении или при загрузке агента.
SCHEDULED_HOUR = 10
# Сколько часов разницы считать своим часом: прогон идёт около минуты,
# но запуск бывает сдвинут системой.
HOUR_TOLERANCE = 1


def _off_hour(finished) -> None:  # noqa: ANN001
    """Прогон пошёл не в свой час — и отчёт обязан это сказать.

    **Судить о свежести надо по журналу прогона, а не по расписанию**
    (решение владельца 24.09.2026). Пропущенный запуск launchd выполняет
    при пробуждении машины либо при загрузке агента после включения:
    прогон дня состоится, но час его будет не 10:00, и данные в нём —
    того часа, когда он пошёл. Молчание об этом читатель понял бы как
    «данные утренние».
    """
    if finished is None:
        return
    hours = abs(finished.hour - SCHEDULED_HOUR)
    if hours <= HOUR_TOLERANCE:
        return
    print(
        f"> **Прогон пошёл не в свой час: {finished:%H:%M} вместо "
        f"{SCHEDULED_HOUR}:00.** Так выглядит пропущенный запуск, выполненный "
        "при пробуждении машины либо при загрузке агента после включения, "
        "либо запуск руками. Данные в отчёте — того часа, когда он пошёл: "
        "судить о свежести надо по этой строке, а не по расписанию.\n"
    )


def _report(routing, kind, since, until, was, now, bonds, previous,  # noqa: ANN001
            health) -> None:
    """Собирает и печатает сам отчёт."""
    names = _ground_names(routing)
    order = {basket.code: basket.order for basket in routing.baskets}
    print(f"# Что изменилось: {until:%d.%m.%Y}\n")
    _health(kind, health)
    if kind == "run":
        _ratings_health(_snapshot_of(until))
        _new_reporting(_reporting_of(until))
    print(
        f"Сравнение с {since:%d.%m.%Y} — **неделя, а не сутки**: медиана "
        "обычного дня ноль, и пустой отчёт каждый день приучает не открывать. "
        f"Род точек — {'пересчёт' if kind == 'backfill' else 'наблюдение'}; "
        "наблюдение с пересчётом не сравнивается вовсе.\n"
    )
    _urgent(routing, now, previous, until)
    # **Беспричинное изменение — остановка, а не строка.** Считается первым:
    # отчёт, начавшийся с перечня изменений, о нём умолчал бы.
    same_data = [
        inn
        for inn, row in now.items()
        if inn in was
        and row["basket"] != was[inn]["basket"]
        and row["fingerprint"] == was[inn]["fingerprint"]
    ]
    # **Календарь — четвёртая причина, и без неё третья лжёт.** Срок сдачи
    # отчётности наступает 1 июня, давность дефолта истекает через три года:
    # вердикт меняется при неизменных данных и неизменной методике, и такая
    # смена — не беспричинная.
    by_calendar = [
        inn
        for inn in same_data
        if _calendar(routing, was[inn], since) != _calendar(routing, now[inn], until)
    ]
    ours = [
        inn
        for inn in same_data
        if inn not in by_calendar
        and (
            now[inn]["code_version"] != was[inn]["code_version"]
            or now[inn]["methodology"] != was[inn]["methodology"]
        )
    ]
    causeless = [
        inn for inn in same_data if inn not in by_calendar and inn not in ours
    ]
    moved = [
        inn
        for inn, row in now.items()
        if inn in was
        and row["basket"] != was[inn]["basket"]
        and row["fingerprint"] != was[inn]["fingerprint"]
    ]
    logger.info(
        "смен корзины: у эмитента %d, при тех же данных %d",
        len(moved),
        len(same_data),
    )
    entered = sorted(set(now) - set(was))
    left = sorted(set(was) - set(now))

    print(f"## Сменили корзину: {len(moved)} из {len(now)}\n")
    if not moved:
        print("ни одного.\n")
    else:
        print("| Эмитент | Было | Стало | Почему |")
        print("|---|---|---|---|")
        # Порядок — по тяжести движения: ухудшения прежде улучшений.
        def weight(inn: str) -> tuple[int, int]:
            before, after = order[was[inn]["basket"]], order[now[inn]["basket"]]
            return (0 if after < before else 1, after)

        # **Отчёт читается за пять минут, и это требование, а не пожелание.**
        # Двадцать строк — предел, за которым перечень перестают читать
        # целиком; остальные называются числом, а не прячутся: «и ещё N»
        # говорит, что они есть, и по истории их видно полностью.
        shown = sorted(moved, key=weight)
        for inn in shown[:20]:
            before, after = was[inn], now[inn]
            appeared = set(after["grounds"]) - set(before["grounds"])
            print(
                f"| {_named(inn)} | {_basket_name(routing, before['basket'])} "
                f"| {_basket_name(routing, after['basket'])} "
                f"| {_why(routing, inn, since, until, appeared, before, after)} |"
            )
        if len(shown) > 20:
            print(f"\nи ещё {len(shown) - 20} — в истории видны полностью.")
    print()

    # **Новое основание без смены корзины — только старшей подгруппы.**
    # Новый дефолт у эмитента, уже стоящего во «Внимании», терять нельзя;
    # прочие показываются числом.
    senior = {
        ground.code
        for basket in routing.baskets
        for ground in basket.grounds
        if basket.group_of(ground.code) == "event_risk"
    }
    events_added = []
    other_added = 0
    for inn, row in now.items():
        if inn not in was or row["basket"] != was[inn]["basket"]:
            continue
        appeared = set(row["grounds"]) - set(was[inn]["grounds"])
        if appeared & senior:
            events_added.append((inn, appeared & senior))
        elif appeared:
            other_added += 1
    print(f"## Новое основание без смены корзины: {len(events_added)}\n")
    for inn, appeared in events_added[:20]:
        said = ", ".join(names.get(code, code) for code in sorted(appeared))
        print(f"- {_named(inn)}: {said}")
    print(f"\nПрочих изменений оснований {other_added} — показаны числом.\n")

    print(f"## Вошли в периметр: {len(entered)}   Вышли: {len(left)}\n")
    for inn in entered[:10]:
        mark = " (с выпусками в обращении)" if inn in bonds else ""
        print(f"- вошёл {_named(inn)}{mark}: {_basket_name(routing, now[inn]['basket'])}")
    for inn in left[:10]:
        print(f"- вышел {_named(inn)}: было {_basket_name(routing, was[inn]['basket'])}")
    print()

    print(f"## От календаря: {len(by_calendar)}\n")
    if by_calendar:
        print(
            "Данные те же и методика та же, а день другой: наступил срок сдачи "
            "отчётности либо истекла давность события. Это не изменение "
            "у эмитента и не наша правка — это календарь, и корзину он меняет "
            "по объявленному правилу.\n"
        )
        for inn in by_calendar[:10]:
            print(
                f"- {_named(inn)}: "
                f"{_basket_name(routing, was[inn]['basket'])} → "
                f"{_basket_name(routing, now[inn]['basket'])}"
            )
        # **Перечень, показанный не целиком, говорит об этом.** «От календаря:
        # 39» с десятью строками под ним читается как полный перечень,
        # и остальные двадцать девять пропадают молча.
        if len(by_calendar) > 10:
            print(
                f"\nПоказаны первые десять из {len(by_calendar)}; "
                "остальные видны в истории."
            )
        print()
    else:
        print("ни одного.\n")

    print(f"## Наши правки: {len(ours)}\n")
    if ours:
        print(
            "Отпечаток входов тот же, версия кода либо методики изменилась: "
            "это наша правка, а не изменение эмитента. Причина одна на всех "
            "и называется версией.\n"
        )
        print(Counter(now[inn]["basket"] for inn in ours).most_common())
    else:
        print("ни одной.\n")

    print(f"\n## Беспричинных изменений: {len(causeless)}\n")
    if causeless:
        print(
            "**Остановка.** Вердикт изменился при том же отпечатке входов "
            "и тех же версиях: у изменения нет причины, а значит, расчёт "
            "недетерминирован. Эмитенты: " + ", ".join(causeless[:20]) + "\n"
        )
    else:
        print("ни одного — вердикт воспроизводим.\n")


if __name__ == "__main__":
    sys.exit(main())
