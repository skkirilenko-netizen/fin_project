"""Уведомления об обязательствах по полным снимкам, без сети и позднего знания."""

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from finlib.sources.cbonds_events import GRACE_STATUSES, DefaultRecord, _as_date

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    """Полный успешно сохранённый снимок перечня обязательств."""

    day: date
    records: dict[str, DefaultRecord]


@dataclass(frozen=True)
class Notice:
    """Первое наступление вида события по идентификатору записи источника."""

    record_id: str
    emission_id: str
    kind: str
    day: date
    previous_snapshot: date | None = None


@dataclass(frozen=True)
class Correction:
    """Уточнение даты источником, не повторное уведомление об обязательстве."""

    record_id: str
    emission_id: str
    day: date
    field: str
    before: date | None
    after: date | None


def snapshots_at(root: Path, until: date) -> tuple[Snapshot, ...]:
    """Читает только полные датированные снимки, доступные не позже дня отчёта."""
    found = []
    for path in sorted(root.glob("defaults_ru_*.json")):
        try:
            day = date.fromisoformat(path.stem.removeprefix("defaults_ru_"))
            if day > until:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw["items"]
            total = int(raw["total"])
            ids = [str(item.get("id") or "") for item in items]
            if (raw.get("error") or raw.get("errors") or raw.get("success") is False
                    or raw.get("complete") is False or total != len(items)
                    or int(raw.get("count", total)) != total
                    or "" in ids or len(set(ids)) != total
                    or any(not item.get("emission_id") for item in items)):
                raise ValueError("снимок не полный или неуспешный")
            rows = {
                key: DefaultRecord(
                    emission_id=str(item["emission_id"]),
                    kind=str(item.get("type_name_rus") or "обязательство"),
                    status=str(item.get("status_name_rus") or "статус не назван"),
                    due=_as_date(item.get("estimated_date")),
                    when=_as_date(item.get("default_date")),
                    announced=_as_date(item.get("announcement_date")),
                    met=_as_date(item.get("actual_date")), amount=None,
                ) for key, item in zip(ids, items, strict=True)
            }
            found.append(Snapshot(day, rows))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as failure:
            logger.warning("снимок %s исключён: %s", path.name, failure)
    return tuple(found)


def timeline(
    snapshots: tuple[Snapshot, ...], until: date,
) -> tuple[tuple[Notice, ...], tuple[Correction, ...], dict[str, tuple[date, DefaultRecord]]]:
    """Восстанавливает события последовательно, сохраняя дату уже наступившего вида."""
    available = sorted((item for item in snapshots if item.day <= until), key=lambda item: item.day)
    if not available:
        return (), (), {}
    by_day = {item.day: item for item in available}
    notices: dict[tuple[str, str], Notice] = {}
    corrections: list[Correction] = []
    latest: dict[str, tuple[date, DefaultRecord]] = {}
    known: set[str] = set()
    deadlines: dict[str, date] = {}
    previous: Snapshot | None = None
    day = available[0].day
    while day <= until:
        snapshot = by_day.get(day)
        if snapshot is not None:
            for key, row in snapshot.records.items():
                if previous is not None and key not in known:
                    notices.setdefault((key, "first_seen"), Notice(
                        key, row.emission_id, "first_seen", day, previous.day))
                old = latest.get(key)
                if old is not None:
                    old_day, old_row = old
                    if (old_row.status.strip().lower() == "технический дефолт"
                            and row.status.strip().lower() == "дефолт"):
                        notices.setdefault((key, "status_default"), Notice(
                            key, row.emission_id, "status_default", day, old_day))
                    for field in ("due", "when", "announced", "met"):
                        before, after = getattr(old_row, field), getattr(row, field)
                        if before != after:
                            corrections.append(Correction(
                                key, row.emission_id, day, field, before, after))
                latest[key] = (day, row)
                # Уже известный срок исправляется независимо от нового статуса:
                # переход в «Дефолт» не должен сохранять отменённую дату льготы.
                if key in deadlines or row.status.strip().lower() in GRACE_STATUSES:
                    if row.when is not None:
                        deadlines[key] = row.when
                    else:
                        deadlines.pop(key, None)
            known.update(snapshot.records)
            previous = snapshot
        # Только сведения, уже наблюдавшиеся к этому дню: позднее исполнение
        # или перенос срока не могут удалить наступившее ранее уведомление.
        for key, (_, row) in latest.items():
            if deadlines.get(key) == day and (row.met is None or row.met > day):
                notices.setdefault((key, "grace_end"), Notice(
                    key, row.emission_id, "grace_end", day))
        day += timedelta(days=1)
    return tuple(notices.values()), tuple(corrections), latest


EVENTS = {
    "first_seen": "запись впервые обнаружена",
    "grace_end": "льготный срок закончился",
    "status_default": "впервые наблюдается переход «Технический дефолт» → «Дефолт»",
}


def said(
    notice: Notice, latest: tuple[date, DefaultRecord], last_snapshot: Snapshot | None = None,
) -> str:
    """Называет вид и дату события с датой и статусом последнего снимка записи."""
    return said_record((notice,), latest, last_snapshot)


def said_record(
    notices: tuple[Notice, ...], latest: tuple[date, DefaultRecord],
    last_snapshot: Snapshot | None = None, marks: tuple[str, ...] | None = None,
) -> str:
    """Одна строка на запись источника: переходы по датам и общий хвост снимка записи.

    **Запись — одно обязательство, переходы — его история.** Первое появление,
    конец льготы и переход в «Дефолт» остаются самостоятельными ключами
    журнала, а печатаются одной строкой: три строки об одной записи читались
    бы как три обязательства. `marks` — пометка к каждому переходу в том же
    порядке, что `notices` (время доставки подтверждающего снимка).
    """
    if not notices or len({item.record_id for item in notices}) != 1:
        raise ValueError("строка уведомления собирается по одной записи источника")
    order = list(EVENTS)
    paired = sorted(zip(notices, marks or ("",) * len(notices), strict=True),
                    key=lambda pair: (pair[0].day, order.index(pair[0].kind)))
    snapshot_day, row = latest
    steps = []
    for notice, mark in paired:
        step = f"{EVENTS[notice.kind]} {notice.day:%d.%m.%Y}{mark}"
        if notice.kind == "status_default" and notice.previous_snapshot is not None:
            step += (f"; предыдущий доступный снимок {notice.previous_snapshot:%d.%m.%Y}; "
                     "точная дата изменения статуса источником не установлена")
        steps.append(step)
    record_id = notices[0].record_id
    text = (f"{row.kind.lower()}, выпуск {row.emission_id}, запись {record_id}: "
            + "; ".join(steps))
    text += (f"; последний доступный снимок записи {snapshot_day:%d.%m.%Y}, "
             f"статус «{row.status}»")
    if last_snapshot is not None and record_id not in last_snapshot.records:
        text += (f"; в последнем полном снимке {last_snapshot.day:%d.%m.%Y} "
                 "запись отсутствует; это не подтверждение исполнения")
    text += (f"; дата исполнения в снимке {row.met:%d.%m.%Y}" if row.met is not None
             else "; дата исполнения в снимке отсутствует; это не доказательство нового неплатежа")
    if row.announced is not None:
        text += f"; дата объявления источника {row.announced:%d.%m.%Y}"
    else:
        text += "; дата объявления источника отсутствует"
    return text


def correction_said(item: Correction) -> str:
    """Печатает уточнение отдельно, не переименовывая и не повторяя событие."""
    names = {"due": "плановый срок", "when": "конец льготного срока",
             "announced": "дата объявления", "met": "дата исполнения"}
    before = item.before.strftime("%d.%m.%Y") if item.before else "не названа"
    after = item.after.strftime("%d.%m.%Y") if item.after else "не названа"
    return (f"выпуск {item.emission_id}, запись {item.record_id}: уточнение в снимке "
            f"{item.day:%d.%m.%Y}, {names[item.field]}: {before} → {after}; "
            "дата первоначального уведомления сохраняется")
