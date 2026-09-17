"""Тесты сверки фактической схемы базы с DDL.

База умеет молча разойтись с `sql/001_schema.sql`: дважды объявленное
в файле отсутствовало в рабочей базе — колонка `llm_log.code_version`
и внешние ключи разложения оценки. `CREATE TABLE IF NOT EXISTS` не приносит
того, чего не было при создании таблицы, и узнавали мы об этом случайно.
"""

import pytest

from finlib.db import fetch_all
from finlib.schema import (
    PROBE_SCHEMA,
    SchemaMismatchError,
    SchemaSnapshot,
    compare,
    ensure_schema,
    expected,
)


def test_working_database_matches_the_ddl(db_conn) -> None:
    """Рабочая база совпадает с файлом схемы полностью."""
    problems = compare(db_conn)
    assert problems == [], problems


def test_probe_schema_leaves_nothing_behind(db_conn) -> None:
    """Эталон снимается во временной схеме и не остаётся в базе.

    DDL применяется внутри точки сохранения: что бы он ни делал, после
    снятия метаданных всё откатывается. Иначе сверка меняла бы то,
    что проверяет.
    """
    expected(db_conn)
    rows = fetch_all(
        "SELECT count(*) AS n FROM information_schema.schemata "
        "WHERE schema_name = %(name)s",
        {"name": PROBE_SCHEMA},
        conn=db_conn,
    )
    assert rows[0]["n"] == 0


def test_missing_column_is_named(db_conn) -> None:
    """Нехватка колонки называется с таблицей и типом.

    Сравнение делается на снимках, а не порчей рабочей базы: снимок —
    то же, что вернула бы база, потерявшая колонку.
    """
    reference = expected(db_conn)
    lost = SchemaSnapshot(
        reference.tables,
        frozenset(
            item for item in reference.columns if not item.startswith("llm_log.checked_numbers")
        ),
        reference.constraints,
        reference.indexes,
        reference.views,
    )
    problems = reference.missing_from(lost)
    assert len(problems) == 1
    assert "llm_log.checked_numbers" in problems[0]
    assert problems[0].startswith("колонка")


def test_missing_foreign_key_is_named(db_conn) -> None:
    """Пропавший внешний ключ виден: он не падает сам по себе никогда.

    Отсутствие колонки рано или поздно уронит запрос, отсутствие внешнего
    ключа не уронит ничего — просто перестанет что-то гарантировать.
    """
    reference = expected(db_conn)
    lost = SchemaSnapshot(
        reference.tables,
        reference.columns,
        frozenset(
            item
            for item in reference.constraints
            if "assessment_metric_assessment_id_fkey" not in item
        ),
        reference.indexes,
        reference.views,
    )
    problems = reference.missing_from(lost)
    assert len(problems) == 1
    assert "FOREIGN KEY" in problems[0]
    assert problems[0].startswith("ограничение")


def test_narrowed_unique_key_is_caught(db_conn) -> None:
    """Ключ уникальности, потерявший стандарт, остаётся ключом — и это ловится.

    Расхождение бывает не только в наличии объекта: ключ `fact_report_uniq`
    без `standard` выглядит исправным и молча даёт значениям двух стандартов
    затирать друг друга. Поэтому сравнивается определение целиком.
    """
    reference = expected(db_conn)
    narrowed = frozenset(
        item.replace(", standard,", ",") if "fact_report_uniq" in item else item
        for item in reference.constraints
    )
    lost = SchemaSnapshot(
        reference.tables, reference.columns, narrowed, reference.indexes, reference.views
    )
    problems = reference.missing_from(lost)
    assert any("fact_report_uniq" in item for item in problems)


def test_ensure_schema_raises_with_a_readable_list(db_conn, monkeypatch) -> None:
    """Расхождение останавливает работу и называет, чего именно нет."""
    import finlib.schema as module

    monkeypatch.setattr(
        module, "compare", lambda conn, path=None: ["нет в базе — колонка: llm_log.foo text"]
    )
    with pytest.raises(SchemaMismatchError) as info:
        ensure_schema(db_conn, force=True)
    assert "llm_log.foo" in str(info.value)
    assert "psql findb -f sql/001_schema.sql" in str(info.value)
    assert info.value.problems


def test_extra_objects_do_not_stop_the_work(db_conn, monkeypatch, caplog) -> None:
    """Лишнее в базе называется в журнале, но работать не мешает.

    Колонка, оставшаяся от прежней версии, ничему не мешает: останавливать
    из-за неё работу значило бы требовать чистой базы там, где достаточно
    полной.
    """
    import logging

    import finlib.schema as module

    monkeypatch.setattr(
        module,
        "compare",
        lambda conn, path=None: ["есть в базе сверх DDL — колонка: llm_log.old text"],
    )
    with caplog.at_level(logging.WARNING, logger="finlib.schema"):
        ensure_schema(db_conn, force=True)
    assert any("llm_log.old" in item for item in caplog.messages)
