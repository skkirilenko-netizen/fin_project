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

import hashlib
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


# Код маршрута: правка документа или замера маршрута не меняет, и коммит
# такой правки не должен объявлять «наши правки» в отчёте изменений.
ROUTE_CODE = ("src/finlib",)


@lru_cache(maxsize=1)
def methodology_digest() -> str:
    """Отпечаток содержимого справочников методики: имена и байты всех YAML.

    **Объявленная версия справочника правкой не поднимается** — она «1.0.0»
    с 22.09.2026 при десятках правок, — и отчёт изменений по ней не видел
    наших изменений вовсе. Отпечаток содержимого меняется с любой правкой.
    """
    found = hashlib.sha256()
    for path in sorted(settings.methodology_dir.glob("**/*.yaml")):
        found.update(str(path.relative_to(settings.methodology_dir)).encode("utf-8"))
        found.update(path.read_bytes())
    return found.hexdigest()[:12]


@lru_cache(maxsize=1)
def route_code() -> str:
    """Коммит кода маршрута: последний, менявший `src/finlib`, с суффиксом при правках.

    **Признак «грязного» дерева считается по коду маршрута, а не по всему
    дереву**: неотслеживаемый каталог вне кода делал «-dirty» каждый прогон.
    """
    revision = _git("log", "-1", "--format=%h", "--", *ROUTE_CODE)
    if not revision:
        return UNKNOWN
    changes = _git("status", "--porcelain", "--", *ROUTE_CODE)
    return f"{revision}{DIRTY_SUFFIX}" if changes else revision


def route_fingerprint() -> dict[str, str]:
    """Чем сделан маршрут: отпечаток методики и коммит кода маршрута."""
    return {"content": methodology_digest(), "route_code": route_code()}
