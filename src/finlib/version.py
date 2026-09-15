"""Версия кода, которой сделан прогон.

Журнал обращений к модели — доказательная база системы, но записи разных
версий кода несопоставимы: правка инструкции, постпроверки или состава блоков
меняет поведение текстового слоя целиком. Статистика, смешивающая их, описывает
не систему, а историю её разработки — и по ней нельзя судить, стало лучше
или хуже.

Поэтому каждое обращение помечается версией кода, а разбор журнала считает
только текущую. Версия — короткий git-хеш рабочего дерева; незакоммиченные
правки помечаются суффиксом, потому что хеш их не описывает.
"""

import logging
import subprocess
from functools import lru_cache

from finlib.config import settings

logger = logging.getLogger(__name__)

# Версия не определена: каталог не под git или git недоступен. Записи с ней
# в статистику не попадут — это верно, происхождение такого прогона неизвестно.
UNKNOWN = "unknown"

# Рабочее дерево отличается от коммита. Хеш такой прогон не описывает,
# и признак не даёт выдать его за воспроизводимый.
DIRTY_SUFFIX = "-dirty"

_TIMEOUT_S = 5.0


def _git(*args: str) -> str | None:
    """Выполняет команду git в каталоге проекта; None — git недоступен."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=settings.base_dir,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("версия кода не определена: %s", error)
        return None
    if done.returncode != 0:
        logger.warning("версия кода не определена: %s", done.stderr.strip())
        return None
    return done.stdout.strip()


@lru_cache(maxsize=1)
def code_version() -> str:
    """Версия кода прогона: короткий git-хеш, с суффиксом при правках."""
    revision = _git("rev-parse", "--short", "HEAD")
    if not revision:
        return UNKNOWN
    # Изменённые отслеживаемые файлы и новые неотслеживаемые. Содержимое data/
    # сюда не попадает: оно в .gitignore.
    changes = _git("status", "--porcelain")
    return f"{revision}{DIRTY_SUFFIX}" if changes else revision
