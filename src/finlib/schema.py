"""Сверка фактической схемы базы с DDL.

База умеет молча разойтись с `sql/001_schema.sql`, и узнаём мы об этом
случайно. Дважды: колонка `llm_log.code_version` и внешние ключи разложения
оценки были объявлены в файле, но в рабочей базе отсутствовали —
`CREATE TABLE IF NOT EXISTS` не приносит того, чего не было при создании
таблицы, а `ALTER` для них никто не дописал.

Расхождение опасно тем же, чем неработающий контроль: всё выглядит
исправным. Запрос к отсутствующей колонке падает не там, где причина,
отсутствующий внешний ключ не падает вовсе — просто перестаёт что-то
гарантировать.

Эталон строится из самого DDL: файл применяется во временную схему внутри
точки сохранения, метаданные снимаются, точка откатывается. Так сверяется
то, что написано в файле, а не отдельно поддерживаемый снимок, который
разойдётся с ним третьим случаем.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from finlib.config import settings
from finlib.db import PgConnection, cursor

logger = logging.getLogger(__name__)

# Временная схема, в которой DDL применяется ради эталона. Имя постоянное:
# точка сохранения откатывается всегда, и остаться она не может.
PROBE_SCHEMA = "schema_probe"

# Обрамление файла: своя транзакция здесь не нужна и помешала бы откату.
_TRANSACTION = re.compile(r"^\s*(BEGIN|COMMIT)\s*;\s*$", re.MULTILINE | re.IGNORECASE)


class SchemaMismatchError(RuntimeError):
    """Фактическая схема базы разошлась с DDL."""

    def __init__(self, problems: list[str]) -> None:
        listed = "\n  ".join(problems)
        super().__init__(
            "схема базы не совпадает с sql/001_schema.sql:\n  "
            f"{listed}\n"
            "Примените схему: psql findb -f sql/001_schema.sql"
        )
        self.problems = problems


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    """Состав схемы: таблицы, колонки, ограничения, индексы, представления."""

    tables: frozenset[str]
    columns: frozenset[str]
    constraints: frozenset[str]
    indexes: frozenset[str]
    views: frozenset[str]

    def missing_from(self, other: "SchemaSnapshot") -> list[str]:
        """Чего из этого снимка нет в другом; перечень человекочитаемый."""
        parts = [
            ("таблица", self.tables - other.tables),
            ("колонка", self.columns - other.columns),
            ("ограничение", self.constraints - other.constraints),
            ("индекс", self.indexes - other.indexes),
            ("представление", self.views - other.views),
        ]
        return [f"{kind}: {item}" for kind, found in parts for item in sorted(found)]


_TABLES = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE'
"""

_COLUMNS = """
SELECT table_name, column_name, data_type, is_nullable,
       coalesce(numeric_precision::text, '') AS precision,
       coalesce(numeric_scale::text, '') AS scale
FROM information_schema.columns
WHERE table_schema = %(schema)s
"""

# Определение ограничения печатается целиком: расхождение бывает не только
# в наличии, но и в составе — ключ уникальности, потерявший стандарт,
# остаётся ключом и выглядит исправным.
_CONSTRAINTS = """
SELECT c.conrelid::regclass::text AS table_name, c.conname, c.contype::text,
       pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE n.nspname = %(schema)s
"""

_INDEXES = """
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE schemaname = %(schema)s
"""

_VIEWS = """
SELECT table_name FROM information_schema.views WHERE table_schema = %(schema)s
"""


def _snapshot(conn: PgConnection, schema: str) -> SchemaSnapshot:
    """Снимает состав схемы в виде, пригодном для сравнения.

    Имя схемы из текстов вычищается: эталон живёт во временной схеме,
    и без этого различались бы все определения по умолчанию и все индексы.
    """

    def clean(text: str) -> str:
        return text.replace(f"{schema}.", "").replace(f'"{schema}".', "")

    with cursor(conn) as cur:
        cur.execute(_TABLES, {"schema": schema})
        tables = {row["table_name"] for row in cur.fetchall()}

        cur.execute(_COLUMNS, {"schema": schema})
        columns = {
            f"{row['table_name']}.{row['column_name']} {row['data_type']}"
            f"{'(' + row['precision'] + ',' + row['scale'] + ')' if row['precision'] else ''}"
            f"{' NOT NULL' if row['is_nullable'] == 'NO' else ''}"
            for row in cur.fetchall()
        }

        cur.execute(_CONSTRAINTS, {"schema": schema})
        constraints = {
            f"{clean(row['table_name'])}.{row['conname']} {clean(row['definition'])}"
            for row in cur.fetchall()
        }

        cur.execute(_INDEXES, {"schema": schema})
        indexes = {
            f"{row['tablename']}.{row['indexname']} {clean(row['indexdef'])}"
            for row in cur.fetchall()
        }

        cur.execute(_VIEWS, {"schema": schema})
        views = {row["table_name"] for row in cur.fetchall()}

    return SchemaSnapshot(
        frozenset(tables),
        frozenset(columns),
        frozenset(constraints),
        frozenset(indexes),
        frozenset(views),
    )


def ddl_path() -> Path:
    """Путь к файлу схемы."""
    return settings.sql_dir / "001_schema.sql"


def expected(conn: PgConnection, path: Path | None = None) -> SchemaSnapshot:
    """Эталонный состав схемы: DDL применяется во временную схему и откатывается.

    Внутри точки сохранения, поэтому вызов безвреден для данных: что бы
    ни делал файл, после снятия метаданных всё откатывается.
    """
    source = _TRANSACTION.sub("", (path or ddl_path()).read_text(encoding="utf-8"))
    with cursor(conn) as cur:
        cur.execute("SAVEPOINT schema_probe_point")
        try:
            cur.execute(f"DROP SCHEMA IF EXISTS {PROBE_SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {PROBE_SCHEMA}")
            cur.execute(f"SET LOCAL search_path TO {PROBE_SCHEMA}")
            cur.execute(source)
            snapshot = _snapshot(conn, PROBE_SCHEMA)
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT schema_probe_point")
            cur.execute("SET LOCAL search_path TO public")
    return snapshot


def compare(conn: PgConnection, path: Path | None = None) -> list[str]:
    """Чего в базе не хватает против DDL и что в ней есть сверх него.

    Лишнее в базе тоже расхождение, но не ошибка: колонка, оставшаяся
    от прежней версии, работать не мешает. Она называется отдельно
    и вынесена в конец перечня.
    """
    reference = expected(conn, path)
    actual = _snapshot(conn, "public")
    problems = [f"нет в базе — {item}" for item in reference.missing_from(actual)]
    problems += [f"есть в базе сверх DDL — {item}" for item in actual.missing_from(reference)]
    return problems


# Сверка делается один раз на процесс: схема за время прогона не меняется,
# а регрессионный набор проводит через цикл полсотни организаций подряд.
_checked = False


def ensure_schema(conn: PgConnection, *, force: bool = False) -> None:
    """Сверяет схему базы с DDL и останавливает работу при расхождении.

    Останавливает именно при нехватке: работать с базой, где нет объявленного
    объекта, значит получать отказ не там, где причина, — а с отсутствующим
    внешним ключом не получать его вовсе. Лишнее в базе только называется
    в журнале: оно ничему не мешает.
    """
    global _checked
    if _checked and not force:
        return
    problems = compare(conn)
    missing = [item for item in problems if item.startswith("нет в базе")]
    extra = [item for item in problems if not item.startswith("нет в базе")]
    for item in extra:
        logger.warning("схема базы: %s", item)
    logger.info(
        "сверка схемы: расхождений %d, из них препятствующих работе %d",
        len(problems),
        len(missing),
    )
    if missing:
        raise SchemaMismatchError(missing)
    _checked = True
