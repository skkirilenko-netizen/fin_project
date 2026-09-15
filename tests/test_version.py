"""Версия кода в журнале обращений к модели и отбор по ней.

Записи разных версий кода несопоставимы: правка инструкции, постпроверки или
состава блоков меняет поведение текстового слоя целиком, и среднее число
попыток по смеси версий описывает историю разработки, а не систему.
"""

import importlib.util
import re
import subprocess

import pytest

from finlib.config import settings
from finlib.db import execute
from finlib.version import DIRTY_SUFFIX, UNKNOWN, code_version

VERSION = re.compile(rf"^([0-9a-f]{{7,40}}({DIRTY_SUFFIX})?|{UNKNOWN})$")

# Заведомо чужая версия: такой записи в выборке текущей версии быть не должно.
OTHER = "0000000"

_INSERT = """
INSERT INTO llm_log (inn, report_date, model, verified, attempt, duration_ms,
                     code_version, is_test)
VALUES (%(inn)s, '2025-12-31', 'test-model', true, 1, 1000, %(version)s, false)
"""


def stats_module():
    """Загружает eval/llm_stats.py: инструмент разработки, не часть пакета."""
    path = settings.base_dir / "eval" / "llm_stats.py"
    spec = importlib.util.spec_from_file_location("llm_stats", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- сама версия -------------------------------------------------------------


def test_version_looks_like_a_git_revision() -> None:
    """Версия — короткий хеш, возможно с признаком незакоммиченных правок."""
    assert VERSION.match(code_version()), code_version()


def test_version_matches_the_repository() -> None:
    """Версия берётся из git, а не сочиняется."""
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=settings.base_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:  # pragma: no cover — каталог не под git
        pytest.skip("каталог проекта не под git")
    assert code_version().startswith(done.stdout.strip())


def test_dirty_tree_is_marked() -> None:
    """Правки рабочего дерева отмечены: хеш их не описывает."""
    changes = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=settings.base_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if changes.returncode != 0:  # pragma: no cover — каталог не под git
        pytest.skip("каталог проекта не под git")
    assert code_version().endswith(DIRTY_SUFFIX) is bool(changes.stdout.strip())


def test_unknown_version_when_git_is_unavailable(monkeypatch) -> None:
    """Без git версия не выдумывается, а объявляется неизвестной."""
    monkeypatch.setattr(
        "finlib.version._git", lambda *args: None
    )
    code_version.cache_clear()
    try:
        assert code_version() == UNKNOWN
    finally:
        code_version.cache_clear()


# --- отбор в статистике ------------------------------------------------------


def test_statistics_count_only_the_current_version(db_conn) -> None:
    """Записи прежних версий в сводку не идут, но и не пропадают молча.

    Транзакция теста откатывается: боевой журнал не меняется, а отбор
    проверяется на настоящих запросах.
    """
    stats = stats_module()
    # Отсчёт ведётся от того, что уже лежит в журнале: боевые записи текущей
    # версии в базе есть, и подменять их тест не вправе.
    before = stats.collect(conn=db_conn)["totals"]["calls"]
    before_all = stats.collect(all_versions=True, conn=db_conn)["totals"]["calls"]
    execute(_INSERT, {"inn": "7736050003", "version": code_version()}, conn=db_conn)
    execute(_INSERT, {"inn": "7736050003", "version": OTHER}, conn=db_conn)

    current = stats.collect(conn=db_conn)
    assert current["version"] == code_version()
    # Прибавилась одна запись из двух: вторая — чужой версии.
    assert current["totals"]["calls"] == before + 1
    discarded = {row["version"]: row["calls"] for row in current["other_versions"]}
    assert discarded.get(OTHER) == 1

    everything = stats.collect(all_versions=True, conn=db_conn)
    assert everything["totals"]["calls"] == before_all + 2
    assert everything["other_versions"] == []


def test_discarded_records_are_named_in_the_summary(db_conn) -> None:
    """Сколько записей отброшено — видно в тексте сводки."""
    stats = stats_module()
    execute(_INSERT, {"inn": "7736050003", "version": OTHER}, conn=db_conn)
    text = stats.render(stats.collect(conn=db_conn))
    assert "отброшено записей" in text
    assert OTHER in text
    assert code_version() in text
