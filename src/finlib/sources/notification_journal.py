"""Первый вывод уведомлений: неизменный отчёт и восстанавливаемый журнал."""

import base64
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from finlib.sources.default_notifications import Notice

MOSCOW = ZoneInfo("Europe/Moscow")
JOURNAL = "default_notification_journal.json"
RECEIPT = re.compile(r"\n<!-- fin-notices-v1: ([A-Za-z0-9+/=]+) -->\n\Z")
KINDS = {"first_seen", "grace_end", "status_default"}
Key = tuple[str, str]


@dataclass(frozen=True)
class Entry:
    """Ключ, первоначальное событие и точная опубликованная строка."""

    record_id: str
    kind: str
    event_on: str
    line: str
    report_id: str
    first_printed_on: str

    @property
    def key(self) -> Key:
        """Идентификатор самостоятельного вида события."""
        return self.record_id, self.kind


@dataclass
class Context:
    """Знание опубликованных отчётов и строки текущей атомарной публикации."""

    report_id: str
    printed_at: datetime
    known: dict[Key, Entry]
    ambiguous: tuple[tuple[date, date, str], ...] = ()
    preview: bool = False
    pending: dict[Key, Entry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Нормализует дату фактического вывода по Москве, не принимая время без пояса."""
        if self.printed_at.tzinfo is None:
            raise ValueError("время вывода должно иметь часовой пояс")
        self.printed_at = self.printed_at.astimezone(MOSCOW)

    def uncertain(self, notice: Notice) -> tuple[str, ...]:
        """Старые отчёты без id, в окне которых мог быть выведен этот ключ."""
        return tuple(name for since, until, name in self.ambiguous
                     if since < notice.day <= until)

    def claim(self, notice: Notice, line: str) -> bool:
        """Готовит первый вывод ключа, не фиксируя его до публикации отчёта."""
        key = notice.record_id, notice.kind
        if key in self.known or key in self.pending or self.uncertain(notice):
            return False
        self.pending[key] = Entry(
            notice.record_id, notice.kind, notice.day.isoformat(), line,
            self.report_id, self.printed_at.astimezone(MOSCOW).date().isoformat(),
        )
        return True


def _hash(text: str) -> str:
    """Хеш точных байтов текста в UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry(raw: dict) -> Entry:
    """Проверяет поля записи журнала либо квитанции публикации."""
    entry = Entry(**raw)
    if (not entry.record_id or entry.kind not in KINDS or not entry.line.startswith("- ")
            or "\n" in entry.line or Path(entry.report_id).name != entry.report_id):
        raise ValueError("неверная запись уведомления")
    date.fromisoformat(entry.event_on)
    date.fromisoformat(entry.first_printed_on)
    return entry


def receipt(text: str, report_id: str) -> tuple[Entry, ...] | None:
    """Читает квитанцию только вместе с подтверждённым полным текстом отчёта."""
    match = RECEIPT.search(text)
    if match is None:
        if "<!-- fin-notices-v1:" in text:
            raise ValueError(f"повреждена квитанция отчёта {report_id}")
        return None
    raw = json.loads(base64.b64decode(match[1], validate=True))
    body = text[:match.start()]
    if raw["report_id"] != report_id or raw["body_sha256"] != _hash(body):
        raise ValueError(f"текст отчёта {report_id} не совпадает с квитанцией")
    entries = tuple(_entry(item) for item in raw["entries"])
    if len({item.key for item in entries}) != len(entries):
        raise ValueError(f"повтор ключа в квитанции {report_id}")
    for item in entries:
        if item.report_id != report_id or body.splitlines().count(item.line) != 1:
            raise ValueError(f"строка уведомления не подтверждена отчётом {report_id}")
    return entries


def _legacy(path: Path, text: str) -> tuple[list[Entry], list[tuple[date, date, str]]]:
    """Читает доказанные ключи старого отчёта; строки без id оставляет неизвестными."""
    heading = re.search(
        r"^## Срочное[^\n]*\((\d{2}\.\d{2}\.\d{4}) → (\d{2}\.\d{2}\.\d{4})\):", text, re.M)
    if heading is None:
        return [], []
    since, until = (datetime.strptime(value, "%d.%m.%Y").date() for value in heading.groups())
    end = text.find("\n## ", heading.end())
    section = text[heading.end():end if end >= 0 else None]
    entries: list[Entry] = []
    ambiguous = []
    wording = {
        "запись впервые обнаружена": "first_seen",
        "льготный срок закончился": "grace_end",
        "впервые наблюдается переход «Технический дефолт» → «Дефолт»": "status_default",
    }
    for line in section.splitlines():
        if not line.startswith("- "):
            continue
        for phrase, kind in wording.items():
            match = re.search(r"запись ([^:]+): " + re.escape(phrase)
                              + r" (\d{2}\.\d{2}\.\d{4})", line)
            if match:
                event = datetime.strptime(match[2], "%d.%m.%Y").date()
                entries.append(Entry(match[1], kind, event.isoformat(), line,
                                     path.name, until.isoformat()))
                break
        else:
            # Рейтинговая строка и уточнение не доказывают вывод обязательства.
            if "уточнение в снимке" not in line and re.search(
                r": (?:купон|погашение|оферта|обязательство)\b", line, re.I,
            ):
                ambiguous.append((since, until, path.name))
    return entries, ambiguous


def load(root: Path, history: Iterable[Path] = ()) -> tuple[dict[Key, Entry], tuple]:
    """Восстанавливает журнал из отчётов и проверяет отсутствие осиротевших записей."""
    known: dict[Key, Entry] = {}
    ambiguous: list[tuple[date, date, str]] = []
    paths = set(root.glob("*.md")) | set(history)
    for path in sorted(paths):
        text = path.read_text(encoding="utf-8")
        entries = receipt(text, path.name)
        if entries is None:
            if not re.fullmatch(r"changes_\d{4}-\d{2}-\d{2}(?:_\d{4})?\.md", path.name):
                continue
            old, gaps = _legacy(path, text)
            entries = tuple(old)
            ambiguous.extend(gaps)
        for entry in entries:
            previous = known.get(entry.key)
            if previous is not None and previous.report_id != entry.report_id:
                # Старые повторы до появления журнала не становятся новым выводом.
                if receipt(text, path.name) is not None:
                    raise ValueError(f"ключ {entry.key} выведен в двух отчётах")
                continue
            known[entry.key] = entry
    journal = root / JOURNAL
    if journal.exists():
        raw = json.loads(journal.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise ValueError("неизвестная версия журнала уведомлений")
        for item in raw["entries"]:
            entry = _entry(item)
            if known.get(entry.key) != entry:
                raise ValueError(f"запись журнала {entry.key} не подтверждена сохранённым отчётом")
    return known, tuple(sorted(set(ambiguous)))


def _sync_dir(folder: Path) -> None:
    """Подтверждает запись имени файла на локальном диске."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, text: str, *, exclusive: bool = False) -> None:
    """Сохраняет целый файл; опубликованный отчёт никогда не заменяется."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if exclusive:
            os.link(temporary, path)
        else:
            temporary.replace(path)
        _sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_journal(root: Path, known: dict[Key, Entry]) -> None:
    """Пишет восстанавливаемый индекс уже опубликованных строк."""
    atomic_write(root / JOURNAL, json.dumps({
        "version": 1, "entries": [asdict(known[key]) for key in sorted(known)],
    }, ensure_ascii=False, indent=1))


def publish(
    path: Path, render: Callable[[Context], str], *, history: Iterable[Path] = (),
    printed_at: datetime | None = None,
) -> str:
    """Публикует отчёт с квитанцией, затем индекс; сбой индекса исправим по отчёту."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".default_notifications.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known, gaps = load(path.parent, history)
        if path.exists():
            # Возвращаются исходные байты, даже если изменились данные и имена.
            text = path.read_text(encoding="utf-8")
            _write_journal(path.parent, known)
            return text
        context = Context(path.name, printed_at or datetime.now(MOSCOW), known, gaps)
        body = render(context).rstrip() + "\n"
        entries = tuple(context.pending.values())
        raw = {"report_id": path.name, "body_sha256": _hash(body),
               "entries": [asdict(item) for item in entries]}
        payload = base64.b64encode(
            json.dumps(raw, ensure_ascii=False).encode("utf-8"),
        ).decode("ascii")
        text = body + f"\n<!-- fin-notices-v1: {payload} -->\n"
        receipt(text, path.name)
        atomic_write(path, text, exclusive=True)
        # Отчёт уже долговечен. При отказе здесь load восстановит все его ключи.
        known.update(context.pending)
        _write_journal(path.parent, known)
        return text
