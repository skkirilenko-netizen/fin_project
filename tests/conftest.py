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


def _real_log_rows() -> int | None:
    """Сколько в журнале обращений к модели боевых записей; None — базы нет."""
    try:
        with psycopg2.connect(**settings.dsn_kwargs) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM llm_log WHERE NOT is_test")
            return int(cur.fetchone()[0])
    except psycopg2.Error:  # pragma: no cover
        return None


@pytest.fixture(scope="session", autouse=True)
def journal_is_not_erased() -> Iterator[None]:
    """Следит, чтобы прогон тестов не стёр боевые записи журнала.

    `llm_log` — доказательная база системы: по ней видно, что предъявлялось
    модели и что она отвечала. Однажды прогон pytest её уже уничтожил.
    Тесты помечают свои записи `is_test` и вправе убирать только их; попытка
    убрать чужие роняет весь прогон, а не проходит незамеченной.
    """
    before = _real_log_rows()
    yield
    after = _real_log_rows()
    if before is not None and after is not None and after < before:
        raise AssertionError(
            f"прогон тестов удалил боевые записи llm_log: было {before}, стало {after}. "
            "Тесты вправе удалять только записи с is_test = true"
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
