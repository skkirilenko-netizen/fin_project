"""Отчёт «что изменилось»: кто сменил корзину и почему.

Устройство утверждено 23.09.2026 (`docs/change_report.md`). Главное требование —
**читается за пять минут**: не перечень из 900 строк, а десяток изменений,
у каждого из которых названа причина.

**Изменение — смена вердикта, и сравнивается он кодами, а не текстами.**
Правка одного слова в методике дала бы 900 «изменений», и отчёт, в котором
их 900, не читается ни в какой день.

**Четыре категории причины.** При том же отпечатке переход календарной
границы проверяется первым; затем разные версии дают категорию «у нас»,
даже если данные изменились одновременно. При тех же версиях разные входы —
«у эмитента»; те же входы без календарной причины — беспричинная смена.
Категория «у нас» не доказывает конкретный коммит.

**Наблюдение и пересчёт между собой не сравниваются.** Пересчёт знает меньше
по устройству — признаки карточки истории не имеют и в него не идут, — и
разница читалась бы как изменение у эмитента.

    uv run python eval/change_report_run.py                  # последняя пара точек
    uv run python eval/change_report_run.py --on 2026-05-06  # отчёт того дня
    uv run python eval/change_report_run.py --kind backfill

**Замер не считает сам**: корзины он берёт из истории, записанной боевой
маршрутизацией, и своей арифметики не имеет.
"""

import io
import json
import logging
import sys
from collections import Counter
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.sources import (  # noqa: E402
    cbonds_events,
    default_deliveries,
    default_notifications,
    notification_journal,
)
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.cbonds_events import (  # noqa: E402
    SNAPSHOTS,
    DefaultRecord,
    events_of,
)
from finlib.sources.moex_risk import risk_sectors  # noqa: E402

logger = logging.getLogger(__name__)

# **Наблюдение читается последней точкой дня** (`routing_day`): повтор дня
# пишется рядом с точкой прогона по расписанию, а отчёт говорит о последнем
# маршруте. Точка по расписанию остаётся для аудита (решение владельца
# 29.09.2026). Пересчёт повторов не знает и читается как прежде.
_SOURCE = {"run": "routing_day", "backfill": "routing_history"}

_DATES = """
SELECT DISTINCT as_of FROM {source} WHERE kind = %(kind)s OR %(kind)s = 'run'
ORDER BY as_of
"""

_POINTS = """
SELECT h.inn, h.basket, h.subgroup, h.grounds, h.grounds_all, h.fingerprint, h.standard,
       h.report_date, h.inputs, r.code_version, r.methodology
FROM {source} h LEFT JOIN routing_run r ON r.id = h.run_id
WHERE (h.kind = %(kind)s OR %(kind)s = 'run') AND h.as_of = %(as_of)s
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
    routing,
    inn: str,
    since: date,
    until: date,
    appeared: set[str],
    before,
    after,
    layers: set[str] | None = None,
) -> str:
    """Чем изменились данные: слой, в котором нашлась датированная запись.

    **Слой называется существующим перечнем** `routing.ground_sources` —
    второго не заводится. Причина, не нашедшаяся в слое, так и печатается:
    изменение с неназванной причиной читалось бы как объяснённое.

    `layers` — слои решающего основания: сведения прочих слоёв не печатаются
    (у Республики Саха рейтинг АКРА стоял рядом с переводом вне периметра
    и читался причиной).
    """

    def wanted(word: str) -> bool:
        return layers is None or any(word in item for item in layers)

    said: list[str] = []
    # **Раскрытие отчётности датированной записи не имеет**, и видно оно
    # по тому, что сменился отчётный период строки: это и есть причина,
    # а не следствие, и называть её основанием значило бы назвать следствие.
    if before["report_date"] != after["report_date"] and wanted("отчётность"):
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
    snapshot = cbonds_events.read_snapshot(until)
    events = events_of(inn, snapshot=snapshot.issuers, observed=snapshot.observed)
    if wanted("выпуск"):
        emissions = {item.emission_id for item in events.issues} | {
            item.emission_id for item in events.records}
        snapshots = default_notifications.snapshots_at(cbonds_events.CACHE, until)
        notices, _, latest = default_notifications.timeline(snapshots, until)
        for item in notices:
            if item.emission_id in emissions and since < item.day <= until:
                said.append(default_notifications.said(item, latest[item.record_id],
                                                        snapshots[-1] if snapshots else None))
    for item in events.ratings:
        if item.assigned is not None and since < item.assigned <= until and wanted("рейтинг"):
            said.append(f"{item.agency}: {item.point} {item.assigned:%d.%m.%Y}")
    risky = risk_sectors()
    for issue in events.issues:
        entry = risky.get(issue.isin) if issue.isin else None
        if (
            entry is not None
            and entry.since is not None
            and since < entry.since <= until
            and wanted("биржа")
        ):
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
    vanished = {
        code for code in set(before["grounds"]) - set(after["grounds"])
        if layers is None or routing.source_of(code) in layers
    }
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


def _urgent(routing, now, previous: date, until: date,  # noqa: ANN001
            notice_context: notification_journal.Context | None = None,
            delivery_evidence: Path | None = None) -> None:
    """Срочное: три вида событий обязательства по полным снимкам и рейтинговые действия.

    **Недельный отчёт не должен задерживать событие на неделю.** Корзину
    такой эмитент чаще всего меняет, и он есть в основном перечне, — но там
    он стоит наравне с изменившимся семь дней назад, а событие вчерашнего дня
    старше по сроку вмешательства. Поэтому оно печатается первым и за сутки,
    а не за неделю.

    Раздел показывается и тогда, когда корзина не изменилась: дефолт
    у эмитента, уже стоящего в «Разборе», — сведение, которое нельзя терять.
    """
    said: list[tuple[int, str]] = []
    snapshots = default_notifications.snapshots_at(cbonds_events.CACHE, until)
    notices, corrections, latest = default_notifications.timeline(snapshots, until)
    updates: list[str] = []
    ratings_snapshot = cbonds_events.read_snapshot(until)
    owners: dict[str, set[str]] = {}
    for inn in now:
        events = events_of(inn, snapshot=ratings_snapshot.issuers,
                           observed=ratings_snapshot.observed)
        emissions = {item.emission_id for item in events.issues} | {
            item.emission_id for item in events.records}
        for emission in emissions:
            owners.setdefault(emission, set()).add(_named(inn))
        for group in _by_record(
            [item for item in notices
             if item.emission_id in emissions and previous < item.day <= until],
            notice_context,
        ):
            text = default_notifications.said_record(group, latest[group[0].record_id],
                                                      snapshots[-1] if snapshots else None)
            line = f"- {_named(inn)}: {text}"
            if _claimed(notice_context, group, line):
                said.append((_order(group), line))
        for item in corrections:
            if item.emission_id in emissions and previous < item.day <= until:
                updates.append(f"- {_named(inn)}: {default_notifications.correction_said(item)}")
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
                (
                    URGENT_RATING,
                    f"- {_named(inn)}: {item.agency} — {item.point} "
                    f"{item.assigned:%d.%m.%Y}",
                )
            )
    if notice_context is not None:
        for group in _by_record(
            [item for item in notices
             if previous < item.day <= until and item.emission_id not in owners],
            notice_context,
        ):
            text = default_notifications.said_record(group, latest[group[0].record_id],
                                                      snapshots[-1] if snapshots else None)
            line = f"- эмитент не установлен: {text}"
            if _claimed(notice_context, group, line):
                said.append((_order(group), line))
    # Счётчик считает то, что напечатано, — строки, то есть записи источника
    # и рейтинговые действия, а не виды событий: перечень с повторами назвал
    # бы одно событие двумя.
    said = list(dict.fromkeys(said))
    print(f"## Срочное за сутки ({previous:%d.%m.%Y} → {until:%d.%m.%Y}): {len(said)}\n")
    if not snapshots:
        print("Полных снимков обязательств на дату отчёта нет: их события не установлены.\n")
    else:
        if snapshots[-1].day < until:
            print(
                f"Полного снимка обязательств за {until:%d.%m.%Y} нет; "
                f"последний полный снимок — {snapshots[-1].day:%d.%m.%Y}. "
                "Новые появления и смены статуса после него не установлены.\n"
            )
        if not any(snapshot.day <= previous for snapshot in snapshots):
            print(
                "Полного снимка обязательств до начала окна нет: "
                "первое появление и смена статуса для начального снимка не установлены.\n"
            )
    if not said:
        print(
            "По доступным сведениям срочных событий не выявлено. "
            "Это не подтверждение отсутствия событий при неполной доставке источников.\n"
        )
    # Порядок — очередь вмешательства, а не алфавит; очередь объявлена
    # у `_record_said`. Сортировка устойчива: внутри ступени — как пришло.
    for _, line in sorted(said, key=lambda pair: pair[0]):
        print(line)
    if updates:
        print("\nУточнения сведений источника — отдельно от срочных событий:\n")
        print("\n".join(dict.fromkeys(updates)))
    print()
    if notice_context is not None:
        _late_notifications(notice_context, notices, snapshots, owners,
                            previous, delivery_evidence)


def _by_record(
    items: list[default_notifications.Notice],
    context: notification_journal.Context | None,
) -> list[tuple[default_notifications.Notice, ...]]:
    """Уведомления по записям источника; с журналом — только ещё не выведенные ключи.

    **Одна строка — одна запись источника.** Ключ журнала прежний — (id записи,
    вид события), — и каждый переход регистрируется своим ключом, а печатаются
    переходы одной записи вместе: отдельная строка на вид события читалась
    бы как отдельное обязательство.
    """
    groups: dict[str, list[default_notifications.Notice]] = {}
    for item in items:
        if context is None or context.claimable(item):
            groups.setdefault(item.record_id, []).append(item)
    return [tuple(group) for group in groups.values()]


def _claimed(
    context: notification_journal.Context | None,
    group: tuple[default_notifications.Notice, ...], line: str,
) -> bool:
    """Регистрирует каждый ключ записи с общей строкой; без журнала — просто печать."""
    if context is None:
        return True
    return all([context.claim(item, line) for item in group])


def _order(group: tuple[default_notifications.Notice, ...]) -> int:
    """Место записи в очереди: объявленный дефолт впереди неподтверждённого."""
    return min(URGENT_DECLARED if item.kind == "status_default" else URGENT_UNCONFIRMED
               for item in group)


def _late_notifications(
    context: notification_journal.Context,
    notices: tuple[default_notifications.Notice, ...],
    snapshots: tuple[default_notifications.Snapshot, ...],
    owners: dict[str, set[str]], previous: date, delivery_evidence: Path | None = None,
) -> None:
    """Печатает ещё не выведенные старые ключи с исходными сведениями и временем доставки."""
    lines: list[str] = []
    gaps: set[str] = set()
    candidates = [item for item in notices if item.day <= previous
                  and (item.record_id, item.kind) not in context.known]
    for item in candidates:
        gaps.update(context.uncertain(item))
    for group in _by_record(candidates, context):
        deliveries = []
        for item in group:
            available = tuple(snapshot for snapshot in snapshots if snapshot.day <= item.day)
            _, _, original = default_notifications.timeline(available, item.day)
            observed, _ = original[item.record_id]
            snapshot_path = cbonds_events.CACHE / f"defaults_ru_{observed:%Y-%m-%d}.json"
            proof = (delivery_evidence / f"{observed:%Y-%m-%d}.json"
                     if delivery_evidence else None)
            delivered = default_deliveries.observed_at(snapshot_path, proof)
            deliveries.append(f"{delivered:%d.%m.%Y %H:%M:%S} МСК" if delivered is not None
                              else "точное время доставки неизвестно")
        # **Хвост строки — состояние записи на снимке последнего перехода**:
        # более позднее состояние не подменяет известного к событию.
        last = max(group, key=lambda item: item.day)
        available = tuple(snapshot for snapshot in snapshots if snapshot.day <= last.day)
        _, _, original = default_notifications.timeline(available, last.day)
        # Одна доставка у всех переходов — одна пометка в конце, как у строки
        # с одним переходом; разные — пометка у каждого перехода.
        same = len(set(deliveries)) == 1
        marks = None if same else tuple(f" (снимок доставлен: {item})" for item in deliveries)
        text = default_notifications.said_record(group, original[last.record_id],
                                                  available[-1] if available else None, marks)
        names = ", ".join(sorted(owners.get(last.emission_id, set()))) or "эмитент не установлен"
        first = ("дата первого вывода не зафиксирована (предпросмотр)" if context.preview
                 else f"впервые выведено {context.printed_at:%d.%m.%Y}")
        delivery = f"; снимок доставлен: {deliveries[0]}" if same else ""
        line = f"- {names}: {text}{delivery}; {first}"
        if _claimed(context, group, line):
            lines.append(line)
    print(f"## Доставлено с опозданием: {len(lines)}\n")
    print("\n".join(lines) if lines else
          "Не выявлено новых пропущенных ключей по доступным сведениям.")
    if gaps:
        print("\nПервый вывод части прежних ключей не установлен: строки без id записи в "
              "сохранённых отчётах " + ", ".join(sorted(gaps)) + ". Эти ключи не объявлены новыми.")
    print()


# Очередь вмешательства в «Срочном»: дефолт, объявленный источником, —
# первым; объявленный неплатёж в льготный срок, исполнение по которому
# не подтверждено, — за ним; рейтинговое действие; исполненное — последним.
URGENT_DECLARED, URGENT_UNCONFIRMED, URGENT_RATING, URGENT_SETTLED = 0, 1, 2, 3


def _announced_in(item: DefaultRecord, since: date, until: date) -> bool:
    """Объявлена ли запись в этом окне: по дню объявления, а не по дате события.

    **Неплатёж — событие дня объявления** (решение владельца 28.09.2026; тот же
    день, с которого его видит маршрут, `DefaultRecord.known_on`). Прежде
    отбор шёл по дате события, а она у технического дефолта — конец льготного
    срока: неплатёж, объявленный 10.09, попадал в «Срочное» 23.09, через две
    недели после того, как корзина по нему уже сменилась.
    """
    return item.known_on is not None and since < item.known_on <= until


def _record_said(item: DefaultRecord, until: date) -> tuple[int, str]:
    """Строка о записи перечня дефолтов и её место в очереди «Срочного».

    **«Не исполнено» — не одно сведение, а три.** Прежде любая неисполненная
    запись печаталась «не исполнено» с датой события, а дата события
    у технического дефолта — конец льготного срока (`DefaultRecord.in_grace`).
    23.09.2026 пять купонов стояли «купон 23.09.2026, не исполнено»: платёж был
    09.09, неплатёж объявлен 09.09–21.09, в перечне на день отчёта запись была
    ещё «Технический дефолт» — исполнения источник не подтвердил и не
    опроверг, дефолтом он объявил их перечнем 25.09. Строки:

    - исполнено к дню отчёта — с датой исполнения;
    - дефолт объявлен источником (статус записи не льготный);
    - неплатёж объявлен, идёт льготный срок — «исполнение не подтверждено,
      дефолт источником не объявлен», с концом льготного срока.

    Плановый срок и день объявления неплатежа называются у каждой.
    """
    kind = item.kind.lower() or "обязательство"
    moment = item.moment
    assert moment is not None, "строка печатается только о датированной записи"
    if item.met is not None and item.met <= until:
        return URGENT_SETTLED, f"{kind} {moment:%d.%m.%Y}, исполнено {item.met:%d.%m.%Y}"
    history = []
    if item.due is not None:
        history.append(f"плановый срок {item.due:%d.%m.%Y}")
    if item.announced is not None:
        history.append(f"неплатёж объявлен {item.announced:%d.%m.%Y}")
    elif item.seen is not None:
        # **Без объявления — с первого появления в перечне** (решение
        # владельца 01.10.2026), и день появления называется прямо.
        history.append(f"объявления нет, в перечне с {item.seen:%d.%m.%Y}")
    told = f" ({', '.join(history)})" if history else ""
    if item.declared:
        return URGENT_DECLARED, f"{kind}: дефолт объявлен источником{told}"
    if item.when is None:
        grace = "конец льготного срока не назван"
    elif item.when == until:
        grace = "льготный срок истекает сегодня"
    elif item.when < until:
        grace = f"льготный срок истёк {item.when:%d.%m.%Y}"
    else:
        grace = f"льготный срок до {item.when:%d.%m.%Y}"
    # **Формулировка — по статусу источника** (решение владельца 01.10.2026):
    # «технический дефолт; льготный срок до ДД.ММ», а не наше «неплатёж».
    status = item.status.strip().lower() or "неплатёж"
    return (
        URGENT_UNCONFIRMED,
        f"{kind}: {status}; {grace}, исполнение не подтверждено{told}",
    )


def _read(conn, kind: str, moment: date) -> dict[str, dict]:  # noqa: ANN001
    """Точки истории на дату: ИНН → вердикт."""
    return {
        row["inn"]: row
        for row in fetch_all(
            _POINTS.format(source=_SOURCE[kind]),
            {"kind": kind, "as_of": moment},
            conn=conn,
        )
    }


_SCHEDULED = """
SELECT h.inn, h.basket, h.subgroup, h.grounds, h.grounds_all, h.fingerprint, h.standard,
       h.report_date, h.inputs, r.code_version, r.methodology
FROM routing_history h LEFT JOIN routing_run r ON r.id = h.run_id
WHERE h.kind = %(kind)s AND h.as_of = %(as_of)s
"""


def _read_scheduled(conn, kind: str, moment: date) -> dict[str, dict]:  # noqa: ANN001
    """Точки прогона по расписанию на дату (без повторов дня): ИНН → вердикт."""
    return {
        row["inn"]: row
        for row in fetch_all(_SCHEDULED, {"kind": kind, "as_of": moment}, conn=conn)
    }


def _render_main(notice_context: notification_journal.Context | None = None,
                 delivery_evidence: Path | None = None) -> int:
    """Печатает отчёт изменений между двумя соседними точками истории."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    kind = "backfill"
    if "--kind" in sys.argv:
        kind = sys.argv[sys.argv.index("--kind") + 1]
    routing = load_routing()
    with connection() as conn:
        dates = [
            row["as_of"]
            for row in fetch_all(
                _DATES.format(source=_SOURCE[kind]), {"kind": kind}, conn=conn
            )
        ]
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
        # **Сутки сравниваются с прогоном по расписанию**, а не с повтором дня
        # (решение владельца 01.10.2026): повтор — наша правка, и сравнение
        # с ним прятало бы её в «вчера».
        was_day = _read_scheduled(conn, kind, previous)
        bonds = set(bond_issuers())
        health = fetch_all(_HEALTH, {"as_of": until, "kind": kind}, conn=conn)
        _report(
            routing, kind, since, until, was, now, bonds, previous, health, was_day,
            notice_context, delivery_evidence,
        )
    return 0


def main() -> int:
    """Сохраняет неизменный отчёт с журналом либо печатает предварительный просмотр."""
    output = Path(sys.argv[sys.argv.index("--output") + 1]) if "--output" in sys.argv else None
    evidence = (Path(sys.argv[sys.argv.index("--delivery-evidence") + 1])
                if "--delivery-evidence" in sys.argv else None)
    kind = sys.argv[sys.argv.index("--kind") + 1] if "--kind" in sys.argv else "backfill"
    if output is not None:
        def render(context: notification_journal.Context) -> str:
            """Собирает полный текст перед атомарной публикацией."""
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = _render_main(context if kind == "run" else None, evidence)
            if result:
                raise ValueError("отчёт не сохранён: сборка не завершена")
            return buffer.getvalue()
        notification_journal.publish(output, render)
        print(output)
        return 0
    context = None
    if kind == "run":
        root = SNAPSHOTS.parents[2] / "output"
        known, gaps = notification_journal.load(root)
        context = notification_journal.Context(
            "preview.md", datetime.now(notification_journal.MOSCOW),
            known, gaps, preview=True,
        )
        print("> Предварительный просмотр: отчёт и первый вывод уведомлений не сохраняются.\n")
    return _render_main(context, evidence)


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
    # Три причины и три разных сведения: источник промолчал, сети не было
    # у нас, до эмитента снимок не дошёл, потому что прервался.
    reasons = [str(why) for why in refused.values()]
    not_asked = sum(1 for why in reasons if why.startswith("не запрошен"))
    offline = sum(1 for why in reasons if why.startswith("нет сети"))
    silent = len(reasons) - not_asked - offline
    parts = [
        f"{text} — {count}"
        for count, text in (
            (silent, "источник не ответил"),
            (offline, "не было сети у нас"),
            (not_asked, "не запрошены: снимок прерван"),
        )
        if count
    ]
    print(
        f"> **Снимок рейтингов неполный: {got} из {total}.** Эмитентов без "
        f"наблюдения дня: {'; '.join(parts)}. Для них взято последнее "
        "наблюдение, и рейтинговых действий дня по ним в отчёте нет; "
        "остаток добирает повторный запуск снимка.\n"
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


# Причины смены корзины в порядке печати.
CAUSES = (
    ("issuer", "у эмитента"),
    ("ours", "у нас (код или методика)"),
    ("calendar", "от календаря"),
    ("causeless", "беспричинные"),
)


def _versions(row: dict) -> tuple:
    """Чем сделана точка: отпечаток методики и коммит кода маршрута.

    **Объявленная версия справочника правкой не поднималась** («1.0.0»
    с 22.09.2026), и прежде «наши правки» не срабатывали вовсе. Точки,
    записанные до отпечатка, сравниваются как прежде — версией кода
    и объявленной методикой.
    """
    said = row.get("methodology") or {}
    if isinstance(said, str):
        said = json.loads(said)
    if said.get("route_code"):
        return (said.get("content"), said.get("route_code"))
    return (json.dumps(said, sort_keys=True), row.get("code_version"))


def _classify(routing, was: dict, now: dict, since: date, until: date) -> dict:  # noqa: ANN001
    """Смены корзины по причинам: у эмитента, у нас, от календаря, беспричинные.

    **Смена кода или методики старше смены данных** (решение владельца
    01.10.2026): при разных версиях причина у эмитента от нашей не
    отделяется, и смена считается нашей — иначе правка выдавалась бы
    за движение рынка, как 93 смены 30.09.2026.
    """
    found: dict[str, list[str]] = {cause: [] for cause, _ in CAUSES}
    for inn, row in now.items():
        if inn not in was or row["basket"] == was[inn]["basket"]:
            continue
        before = was[inn]
        same = row["fingerprint"] == before["fingerprint"]
        if same and _calendar(routing, before, since) != _calendar(routing, row, until):
            found["calendar"].append(inn)
        elif _versions(row) != _versions(before):
            found["ours"].append(inn)
        elif not same:
            found["issuer"].append(inn)
        else:
            found["causeless"].append(inn)
    return found


def _decisive(routing, before: dict, after: dict) -> set[str]:  # noqa: ANN001
    """Основания, сменившие корзину: новые основания новой корзины либо ушедшие старой.

    **Графа «Почему» называет основание, которое сменило корзину**, а не
    первое датированное событие эмитента (решение владельца 01.10.2026):
    у Республики Саха (Якутия) стоял рейтинг, а корзину сменил перевод
    вне периметра по типу эмитента.
    """
    def own(row: dict) -> set[str]:
        return {ground.code for ground in routing.basket(row["basket"]).grounds}

    appeared = set(after["grounds"]) - set(before["grounds"])
    vanished = set(before["grounds"]) - set(after["grounds"])
    # При улучшении решающим было снятие старшего основания.
    # Вне периметра — решение о типе, а не улучшение состояния.
    if (
        before["basket"] != "out_of_scope"
        and after["basket"] != "out_of_scope"
        and routing.basket(after["basket"]).order > routing.basket(before["basket"]).order
        and vanished & own(before)
    ):
        return vanished & own(before)
    # Корзину назвало появившееся основание новой корзины — в какую бы
    # сторону ни шёл переход: перевод вне периметра не «лучше» и не «хуже»
    # «Внимания», он про тип эмитента.
    return (
        (appeared & own(after))
        or (vanished & own(before))
        or appeared
        or vanished
        or (set(after["grounds"]) & own(after))
    )


def _why_decisive(routing, inn: str, since: date, until: date, before, after) -> str:  # noqa: ANN001
    """Решающее основание со своим слоем и датированное сведение этого слоя."""
    decisive = _decisive(routing, before, after)
    names = _ground_names(routing)
    if not decisive:
        return _why(routing, inn, since, until, set(), before, after)
    layers = {routing.source_of(code) for code in decisive}
    said = ", ".join(
        f"{names.get(code, code)} ({routing.source_of(code)})" for code in sorted(decisive)
    )
    if decisive <= set(before["grounds"]) - set(after["grounds"]):
        said = "ушло основание: " + said
    appeared = set(after["grounds"]) - set(before["grounds"])
    evidence = _why(
        routing, inn, since, until, appeared & decisive, before, after, layers
    )
    if evidence.startswith("появилось основание") or evidence.startswith("данные"):
        evidence = ""
    return f"{said}; {evidence}" if evidence else said


def _moves_table(routing, groups: dict, was, now, since, until, order) -> None:  # noqa: ANN001
    """Таблица смен: эмитент, было, стало, причина, решающее основание."""
    labels = dict(CAUSES)
    rows = [(inn, cause) for cause, items in groups.items() for inn in items]

    # Порядок — по тяжести движения: ухудшения прежде улучшений.
    def weight(item: tuple[str, str]) -> tuple[int, int]:
        inn = item[0]
        before, after = order[was[inn]["basket"]], order[now[inn]["basket"]]
        return (0 if after < before else 1, after)

    print("| Эмитент | Было | Стало | Причина | Почему |")
    print("|---|---|---|---|---|")
    # **Отчёт читается за пять минут, и это требование, а не пожелание.**
    # Двадцать строк — предел, за которым перечень перестают читать
    # целиком; остальные называются числом, а не прячутся.
    shown = sorted(rows, key=weight)
    for inn, cause in shown[:20]:
        before, after = was[inn], now[inn]
        print(
            f"| {_named(inn)} | {_basket_name(routing, before['basket'])} "
            f"| {_basket_name(routing, after['basket'])} | "
            f"{_our_change(before, after) if cause == 'ours' else labels[cause]} "
            f"| {_why_decisive(routing, inn, since, until, before, after)} |"
        )
    if len(shown) > 20:
        print(f"\nи ещё {len(shown) - 20} — в истории видны полностью.")
    print()


def no_bonds_left(was: dict, now: dict) -> list[str]:
    """ИНН, у которых за окно появилось основание «нет выпусков в обращении»."""
    return sorted(
        inn
        for inn, row in now.items()
        if inn in was
        and "no_bonds_outstanding" in row["grounds"]
        and "no_bonds_outstanding" not in was[inn]["grounds"]
    )


def _our_change(before: dict, after: dict) -> str:
    """Категория причины с наблюдавшимися версиями без недоказанной атрибуции."""
    old = before.get("code_version") or "не записана"
    new = after.get("code_version") or "не записана"
    return (
        f"у нас (код или методика); версии прогонов: {old} → {new}; "
        "точная правка не установлена"
    )


def _action_of(row: dict) -> dict[str, str]:
    """Сохранённое действие точки; текущая методика не подменяет историческое."""
    inputs = row.get("inputs") or {}
    if isinstance(inputs, str):
        inputs = json.loads(inputs)
    action = inputs.get("action") or {}
    return action if isinstance(action, dict) else {}


def _subgroup_changes(routing, was: dict, now: dict) -> None:  # noqa: ANN001
    """Печатает смену действия при прежней корзине, не техническое переименование."""
    changed: list[tuple[str, dict, dict]] = []
    unknown: list[str] = []
    common = set(was) & set(now)
    for inn in sorted(common):
        before, after = was[inn], now[inn]
        if (before["basket"] != after["basket"]
                or before.get("subgroup", "") == after.get("subgroup", "")):
            continue
        old, new = _action_of(before), _action_of(after)
        if not old.get("code") or not new.get("code"):
            unknown.append(inn)
        elif old["code"] != new["code"]:
            changed.append((inn, old, new))
    print(f"## За сутки сменилось действие при прежней корзине: {len(changed)} из {len(common)}\n")
    for inn, old, new in changed:
        print(
            f"- {_named(inn)}: корзина «{_basket_name(routing, now[inn]['basket'])}» прежняя; "
            f"подгруппа «{old.get('subgroup_name') or old.get('subgroup')}» → "
            f"«{new.get('subgroup_name') or new.get('subgroup')}»; "
            f"действие «{old.get('text', old['code'])}» → «{new.get('text', new['code'])}»"
        )
    if unknown:
        print("\nСмена действия не установлена: исторический код действия не сохранён:\n")
        print("\n".join(f"- {_named(inn)}" for inn in unknown))
    print()


def _report(routing, kind, since, until, was, now, bonds, previous,  # noqa: ANN001
            health, was_day=None, notice_context=None, delivery_evidence=None) -> None:
    """Собирает и печатает сам отчёт."""
    if was_day is None:
        was_day = was
    names = _ground_names(routing)
    order = {basket.code: basket.order for basket in routing.baskets}
    print(f"# Что изменилось: {until:%d.%m.%Y}\n")
    # **Повторный прогон называет себя в шапке** (решение владельца
    # 29.09.2026): его отчёт лежит рядом с отчётом прогона по расписанию,
    # а не вместо него, и читатель обязан знать, какой перед ним.
    if "--note" in sys.argv:
        print(f"> **{sys.argv[sys.argv.index('--note') + 1]}**\n")
    _health(kind, health)
    if kind == "run":
        _ratings_health(_snapshot_of(until))
        _new_reporting(_reporting_of(until))
    print(
        f"**Суточные смены** — против прогона по расписанию {previous:%d.%m.%Y}; "
        f"**недельная сводка** — против {since:%d.%m.%Y}, отдельным разделом "
        "(решение владельца 01.10.2026). "
        f"Род точек — {'пересчёт' if kind == 'backfill' else 'наблюдение'}; "
        "наблюдение с пересчётом не сравнивается вовсе.\n"
    )
    _urgent(routing, now, previous, until, notice_context, delivery_evidence)
    daily = _classify(routing, was_day, now, previous, until)
    weekly = _classify(routing, was, now, since, until)
    logger.info(
        "смен корзины за сутки %d, за неделю %d",
        sum(len(items) for items in daily.values()),
        sum(len(items) for items in weekly.values()),
    )
    entered = sorted(set(now) - set(was))
    left = sorted(set(was) - set(now))
    by_calendar, ours, causeless = (
        weekly["calendar"],
        weekly["ours"],
        sorted(set(weekly["causeless"]) | set(daily["causeless"])),
    )

    changed = sum(len(items) for items in daily.values())
    if not was_day:
        print("## Суточные смены не установлены\n")
        print(
            f"Точек прогона по расписанию {previous:%d.%m.%Y} нет: "
            "сравнивать не с чем; это не ноль изменений.\n"
        )
    else:
        print(f"## За сутки сменили корзину: {changed} из {len(now)}\n")
    if was_day and not changed:
        print("ни одного.\n")
    elif was_day:
        _moves_table(routing, daily, was_day, now, previous, until, order)
    if was_day:
        _subgroup_changes(routing, was_day, now)

    print(f"## За неделю ({since:%d.%m.%Y} → {until:%d.%m.%Y}): сводка\n")
    print("| Причина | Смен корзины |\n|---|---|")
    for cause, label in CAUSES:
        print(f"| {label} | {len(weekly[cause])} |")
    data_too = [inn for inn in weekly["ours"] if now[inn]["fingerprint"] != was[inn]["fingerprint"]]
    print(
        f"\nИз наших правок у {len(data_too)} изменились и данные: при смене "
        "кода или методики причина у эмитента от нашей не отделяется, и такая "
        "смена считается нашей — иначе правка выдавалась бы за движение рынка.\n"
    )
    if weekly["issuer"]:
        print("Смены у эмитента за неделю:\n")
        _moves_table(
            routing, {"issuer": weekly["issuer"]}, was, now, since, until, order
        )

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
    events_removed = []
    other_added = 0
    for inn, row in now.items():
        if inn not in was or row["basket"] != was[inn]["basket"]:
            continue
        current_grounds = set(row.get("grounds_all", row["grounds"]))
        prior_grounds = set(was[inn].get("grounds_all", was[inn]["grounds"]))
        appeared = current_grounds - prior_grounds
        vanished = prior_grounds - current_grounds
        if appeared & senior:
            events_added.append((inn, appeared & senior))
        if vanished & senior:
            events_removed.append((inn, vanished & senior))
        if (appeared | vanished) - senior:
            other_added += 1
    period = f"за неделю {since:%d.%m.%Y} → {until:%d.%m.%Y}"
    print(f"## Новое основание без смены корзины: {len(events_added)} ({period})\n")
    for inn, appeared in events_added[:20]:
        said = ", ".join(names.get(code, code) for code in sorted(appeared))
        print(f"- {_named(inn)}: {said}")
    if len(events_added) > 20:
        print(f"\nПоказаны первые двадцать из {len(events_added)}; остальные видны в истории.")
    print(f"\n## Ушло основание без смены корзины: {len(events_removed)} ({period})\n")
    for inn, vanished in events_removed[:20]:
        said = ", ".join(names.get(code, code) for code in sorted(vanished))
        print(f"- {_named(inn)}: ушло основание: {said}")
    if len(events_removed) > 20:
        print(f"\nПоказаны первые двадцать из {len(events_removed)}; остальные видны в истории.")
    print(f"\nПрочих изменений оснований {other_added} ({period}) — показаны числом.\n")

    print(f"## Вошли в периметр: {len(entered)}   Вышли: {len(left)} ({period})\n")
    for inn in entered[:10]:
        mark = " (с выпусками в обращении)" if inn in bonds else ""
        print(f"- вошёл {_named(inn)}{mark}: {_basket_name(routing, now[inn]['basket'])}")
    for inn in left[:10]:
        print(
            f"- вышел {_named(inn)}: было {_basket_name(routing, was[inn]['basket'])}; "
            "причина выхода из истории не установлена — проверить журнал исключений"
        )
    print()
    # **Выход по периметру — своей строкой** (решение владельца 09.10.2026):
    # эмитент остаётся в списке, но «Вне периметра методики» по основанию
    # `no_bonds_outstanding`, и среди прочих смен корзины его причину не видно.
    gone = no_bonds_left(was, now)
    print(f"Вышли: нет выпусков в обращении — {len(gone)} ({period})\n")
    for inn in gone[:20]:
        print(
            f"- вышел: нет выпусков в обращении — {_named(inn)}: было "
            f"{_basket_name(routing, was[inn]['basket'])}"
        )
    if len(gone) > 20:
        print(f"\nПоказаны первые двадцать из {len(gone)}; остальные видны в истории.")
    print()

    print(f"## От календаря: {len(by_calendar)} ({period})\n")
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

    print(f"## Наши правки: {len(ours)} ({period})\n")
    if ours:
        print(
            "Версия кода либо отпечаток методики изменились; данные могли "
            "измениться одновременно. Это категория «у нас», а не доказательство "
            "конкретного коммита: точная правка не установлена.\n"
        )
        for basket, count in Counter(now[inn]["basket"] for inn in ours).most_common():
            print(f"- {_basket_name(routing, basket)}: {count}")
    else:
        print("ни одной.\n")

    print(f"\n## Беспричинных изменений: {len(causeless)} (суточное и недельное окна)\n")
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
