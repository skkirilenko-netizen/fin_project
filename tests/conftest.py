"""Общие фикстуры тестов."""

from collections.abc import Iterator

import psycopg2
import pytest

from finlib.config import settings


def pytest_configure(config: pytest.Config) -> None:
    """Запрещает параллельный запуск: тесты загрузчика делят одну базу findb."""
    workers = config.getoption("numprocesses", default=None)
    if workers:
        raise pytest.UsageError(
            "тесты загрузчика работают в общей базе findb и на одних и тех же ключах; "
            "параллельный запуск даст взаимные блокировки транзакций"
        )


@pytest.fixture
def db_conn() -> Iterator[psycopg2.extensions.connection]:
    """Соединение с findb в транзакции, которая откатывается всегда.

    Коммита нет ни при каком исходе: откат стоит в finally, поэтому база
    остаётся чистой и после упавшего теста.
    """
    try:
        conn = psycopg2.connect(**settings.dsn_kwargs)
    except psycopg2.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"база findb недоступна: {exc}")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
