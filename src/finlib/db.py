"""Подключение к PostgreSQL, транзакции и базовые операции на сыром SQL."""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import extensions as pg_ext
from psycopg2.extras import RealDictCursor, execute_batch

from finlib.config import settings

logger = logging.getLogger(__name__)

# Классы psycopg2 названы в нижнем регистре, здесь используются как типы.
PgConnection = pg_ext.connection
PgCursor = pg_ext.cursor

Params = Sequence[Any] | dict[str, Any] | None


@contextmanager
def connection() -> Iterator[PgConnection]:
    """Соединение с БД: commit при штатном выходе, rollback при исключении."""
    conn = psycopg2.connect(**settings.dsn_kwargs)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Ошибка в транзакции, выполнен откат")
        raise
    finally:
        conn.close()


@contextmanager
def cursor(conn: PgConnection | None = None, *, dict_rows: bool = True) -> Iterator[PgCursor]:
    """Курсор поверх переданного соединения либо поверх собственного."""
    factory = RealDictCursor if dict_rows else None
    if conn is not None:
        with conn.cursor(cursor_factory=factory) as cur:
            yield cur
    else:
        with connection() as own_conn, own_conn.cursor(cursor_factory=factory) as cur:
            yield cur


def execute(sql: str, params: Params = None, conn: PgConnection | None = None) -> int:
    """Выполняет запрос без выборки, возвращает число затронутых строк."""
    with cursor(conn, dict_rows=False) as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_many(
    sql: str,
    seq_of_params: Sequence[Sequence[Any] | dict[str, Any]],
    conn: PgConnection | None = None,
    page_size: int = 500,
) -> int:
    """Выполняет запрос пачкой параметров, возвращает число переданных наборов."""
    if not seq_of_params:
        return 0
    with cursor(conn, dict_rows=False) as cur:
        execute_batch(cur, sql, seq_of_params, page_size=page_size)
        return len(seq_of_params)


def fetch_all(
    sql: str, params: Params = None, conn: PgConnection | None = None
) -> list[dict[str, Any]]:
    """Возвращает все строки выборки списком словарей."""
    with cursor(conn) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def fetch_one(
    sql: str, params: Params = None, conn: PgConnection | None = None
) -> dict[str, Any] | None:
    """Возвращает первую строку выборки либо None."""
    with cursor(conn) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None


def run_sql_file(path: Path, conn: PgConnection | None = None) -> None:
    """Выполняет SQL-файл целиком в одной транзакции."""
    sql = Path(path).read_text(encoding="utf-8")
    logger.info("Применяется SQL-файл %s", path)
    with cursor(conn, dict_rows=False) as cur:
        cur.execute(sql)
