"""Локальная копия снимков и истории уведомлений с проверкой каждого файла.

    uv run python scripts/snapshots_backup.py --to ПУТЬ
    uv run python scripts/snapshots_backup.py --to ПУТЬ --check
    uv run python scripts/snapshots_backup.py --to ПУТЬ --verify

Место называет владелец. --check сравнивает с исходниками; --verify проверяет
опись и файлы копии даже после утраты исходников. Оба режима только читают.
Дополнение неполного снимка рейтингов законно меняет файл; принять замену
сохранённой версии можно только явно, через --accept-changed.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
DATA = Path("data")
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class Source:
    """Каталог и маска сохраняемых файлов; обязательность относится к снимкам."""

    folder: str
    pattern: str
    required: bool = False


SOURCES = (
    Source("raw/cbonds/ratings", "*.json", required=True),
    Source("raw/cbonds", "defaults_ru_????-??-??.json", required=True),
    Source("raw/cbonds/default_deliveries", "????-??-??.json"),
    Source("output", "changes_????-??-??*.md"),
    Source("output", "default_notification_journal.json"),
)
Metadata = dict[str, int | str]
Manifest = dict[str, Metadata]


def digest(path: Path) -> str:
    """Возвращает sha256 содержимого файла."""
    found = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            found.update(block)
    return found.hexdigest()


def metadata(path: Path) -> Metadata:
    """Размер и хеш файла для описи."""
    return {"size": path.stat().st_size, "sha256": digest(path)}


def _target(where: Path, key: str) -> Path:
    """Разрешает только нормальный относительный путь внутри копии."""
    relative = Path(key)
    if (not key or relative.is_absolute() or ".." in relative.parts
            or relative.as_posix() != key):
        raise ValueError(f"недопустимый путь в описи: {key!r}")
    target = where / relative
    if not target.resolve().is_relative_to(where.resolve()):
        raise ValueError(f"путь выходит из каталога копии: {key!r}")
    return target


def read_manifest(where: Path) -> Manifest:
    """Читает и проверяет опись, не исправляя повреждённые сведения."""
    raw = json.loads((where / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("опись пуста либо имеет неверный формат")
    for key, item in raw.items():
        _target(where, key)
        if (not isinstance(item, dict) or type(item.get("size")) is not int
                or item["size"] < 0 or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
            raise ValueError(f"неверные размер или sha256 в описи: {key}")
    return raw


def inventory(data: Path) -> tuple[dict[str, Path], int]:
    """Перечисляет снимки и артефакты публикации; отсутствие снимков — ошибка."""
    found: dict[str, Path] = {}
    errors = 0
    for source in SOURCES:
        folder = data / source.folder
        files = sorted(folder.glob(source.pattern))
        if not files and source.required:
            logger.error("снимков нет: %s/%s", folder, source.pattern)
            errors += 1
        for item in files:
            relative = item.relative_to(data)
            key = (relative.relative_to("raw").as_posix()
                   if relative.parts[0] == "raw" else relative.as_posix())
            found[key] = item
    return found, errors


def verify(where: Path, manifest: Manifest) -> tuple[int, int]:
    """Проверяет все записи описи независимо от наличия исходных файлов."""
    matched = errors = 0
    for key, expected in manifest.items():
        try:
            target = _target(where, key)
            if not target.is_file() or metadata(target) != expected:
                logger.error("файл копии отсутствует или повреждён: %s", key)
                errors += 1
            else:
                matched += 1
        except (OSError, ValueError) as failure:
            logger.error("не проверен %s: %s", key, failure)
            errors += 1
    return matched, errors


def _copy(item: Path, target: Path) -> None:
    """Публикует проверенную копию файла атомарной заменой."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(item, temporary)
        if metadata(temporary) != metadata(item):
            raise ValueError(f"исходник изменился во время копирования: {item}")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_manifest(where: Path, manifest: Manifest) -> None:
    """Сохраняет новую опись целиком, оставляя прежнюю при сбое записи."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=where,
                                     delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(manifest, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(where / MANIFEST)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Копирует или проверяет файлы; любая неполнота даёт ненулевой исход."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--verify", action="store_true")
    parser.add_argument("--accept-changed", action="store_true")
    args = parser.parse_args(argv)
    if args.accept_changed and (args.check or args.verify):
        parser.error("--accept-changed применим только к копированию")
    where = args.to
    if not where.parent.is_dir():
        logger.error("каталога %s нет: том не смонтирован либо путь неверен", where.parent)
        return 1
    manifest: Manifest = {}
    try:
        if (where / MANIFEST).exists() or args.check or args.verify:
            manifest = read_manifest(where)
        if args.verify:
            matched, errors = verify(where, manifest)
            print(f"{where}: проверено {matched} из {len(manifest)}, ошибок {errors}")
            return int(bool(errors))
        files, errors = inventory(DATA)
        if not files:
            logger.error("проверка не выполнена: исходных файлов нет")
            return 1
        copied = 0
        for key, item in files.items():
            mine = metadata(item)
            target = _target(where, key)
            changed = key in manifest and mine != manifest[key]
            corrupt = key in manifest and target.exists() and metadata(target) != manifest[key]
            if changed or corrupt:
                logger.error("%s: %s", key, "исходник изменился" if changed else "копия повреждена")
                if not args.accept_changed:
                    errors += 1
                    continue
                logger.warning("%s: заменяю по --accept-changed", key)
            if args.check:
                if key not in manifest:
                    logger.error("исходник не включён в опись: %s", key)
                    errors += 1
                continue
            if not target.is_file() or metadata(target) != mine:
                _copy(item, target)
                copied += 1
            manifest[key] = mine
        if args.check:
            for key in manifest.keys() - files.keys():
                logger.error("исходник из описи отсутствует: %s", key)
                errors += 1
        if not manifest:
            logger.error("опись не создана: ни один файл не сохранён")
            return 1
        matched, broken = verify(where, manifest)
        errors += broken
        if not args.check:
            _write_manifest(where, manifest)
        print(f"{where}: скопировано {copied}, проверено {matched} из {len(manifest)}, "
              f"ошибок {errors}")
        return int(bool(errors))
    except (OSError, ValueError, TypeError) as failure:
        logger.error("копия не подтверждена: %s", failure)
        return 1


if __name__ == "__main__":
    sys.exit(main())
