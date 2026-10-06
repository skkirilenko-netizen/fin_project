"""Подтверждённое время доставки снимка, связанное с его точными байтами."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from finlib.sources.notification_journal import MOSCOW, atomic_write


def _digest(path: Path) -> str:
    """Хеш исходного снимка без изменения ответа источника."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(snapshot: Path, delivered_at: datetime) -> None:
    """Фиксирует время новой успешной доставки в отдельном файле."""
    if delivered_at.tzinfo is None:
        raise ValueError("время доставки должно иметь часовой пояс")
    day = snapshot.stem.removeprefix("defaults_ru_")
    path = snapshot.parent / "default_deliveries" / f"{day}.json"
    raw = {"snapshot": snapshot.name, "sha256": _digest(snapshot),
           "delivered_at": delivered_at.astimezone(MOSCOW).isoformat(),
           "evidence": "успешное получение и сохранение нового снимка"}
    atomic_write(path, json.dumps(raw, ensure_ascii=False, indent=1), exclusive=True)


def observed_at(snapshot: Path, evidence: Path | None = None) -> datetime | None:
    """Читает подтверждённое время; mtime исходного файла не используется."""
    day = snapshot.stem.removeprefix("defaults_ru_")
    path = evidence or snapshot.parent / "default_deliveries" / f"{day}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("snapshot") != snapshot.name or raw.get("sha256") != _digest(snapshot):
        raise ValueError(f"отметка доставки не подтверждает байты {snapshot.name}")
    moment = datetime.fromisoformat(raw["delivered_at"])
    if moment.tzinfo is None or not raw.get("evidence"):
        raise ValueError(f"отметка доставки {snapshot.name} без часового пояса или основания")
    return moment.astimezone(MOSCOW)
