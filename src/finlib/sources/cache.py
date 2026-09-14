"""Кэш сырых ответов источника на диске с контрольной суммой."""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from finlib.config import settings

logger = logging.getLogger(__name__)

META_SUFFIX = ".meta.json"


def checksum(content: bytes) -> str:
    """Контрольная сумма сырого ответа."""
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """Сырой ответ источника вместе с его метаданными."""

    content: bytes
    path: Path
    url: str
    checksum: str
    fetched_at: str
    from_cache: bool


class RawCache:
    """Хранилище сырых ответов: data/raw/<источник>/<ключ>.json плюс метафайл."""

    def __init__(self, source: str, root: Path | None = None) -> None:
        self.root = (root or settings.raw_dir) / source

    def path_for(self, key: str) -> Path:
        """Путь к файлу ответа по ключу."""
        return self.root / f"{key}.json"

    def read(self, key: str) -> CachedResponse | None:
        """Читает ответ из кэша, сверяя контрольную сумму; при расхождении — промах."""
        path = self.path_for(key)
        meta_path = path.with_suffix(path.suffix + META_SUFFIX)
        if not path.exists() or not meta_path.exists():
            return None
        content = path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        actual = checksum(content)
        if actual != meta.get("checksum"):
            logger.warning(
                "контрольная сумма %s не совпала с записанной, кэш считается промахом", path
            )
            return None
        return CachedResponse(
            content=content,
            path=path,
            url=meta.get("url", ""),
            checksum=actual,
            fetched_at=meta.get("fetched_at", ""),
            from_cache=True,
        )

    def write(self, key: str, content: bytes, url: str) -> CachedResponse:
        """Сохраняет сырой ответ и метаданные, не изменяя содержимое."""
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = checksum(content)
        fetched_at = datetime.now(UTC).isoformat()
        path.write_bytes(content)
        path.with_suffix(path.suffix + META_SUFFIX).write_text(
            json.dumps(
                {
                    "url": url,
                    "checksum": digest,
                    "fetched_at": fetched_at,
                    "size": len(content),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("сырой ответ сохранён: %s (%d байт)", path, len(content))
        return CachedResponse(
            content=content,
            path=path,
            url=url,
            checksum=digest,
            fetched_at=fetched_at,
            from_cache=False,
        )
