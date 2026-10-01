"""Общие фикстуры тестов."""

import os
from collections.abc import Iterator

# **Тесты работают в своей базе, а не в боевой** (решение владельца
# 01.10.2026). База задаётся до первого чтения настроек: переменная
# окружения старше .env. Создаётся она `make test-db` — схема из sql/
# и пробы ГИР БО с диска.
TEST_DB = os.environ.get("TEST_DB_NAME", "findb_test")
os.environ["DB_NAME"] = TEST_DB

import psycopg2  # noqa: E402
import pytest  # noqa: E402

from finlib.config import settings  # noqa: E402

PRODUCTION_DB = "findb"


def pytest_configure(config: pytest.Config) -> None:
    """Отказ стартовать на боевой базе и при параллельном запуске.

    **Боевая база тестам закрыта.** Тест, проверявший журнал без вызова
    записи, проходил на findb за счёт 13 записей прошлых доставок — на пустой
    тестовой базе это стало видно сразу (`test_unfit_row_does_not_become_a_set`).
    """
    if settings.db_name == PRODUCTION_DB:
        raise pytest.UsageError(
            f"тесты не запускаются на боевой базе {PRODUCTION_DB}: "
            "задайте TEST_DB_NAME или создайте findb_test — make test-db"
        )
    try:
        psycopg2.connect(**settings.dsn_kwargs).close()
    except psycopg2.OperationalError as failure:
        raise pytest.UsageError(
            f"тестовой базы {settings.db_name} нет: make test-db ({failure})"
        ) from failure
    workers = config.getoption("numprocesses", default=None)
    if workers:
        raise pytest.UsageError(
            "тесты загрузчика работают в общей тестовой базе и на одних и тех же ключах; "
            "параллельный запуск даст взаимные блокировки транзакций"
        )


_REAL_ROWS = "SELECT count(*) AS n, coalesce(max(id), 0) AS last FROM llm_log WHERE NOT is_test"


def _real_log_state() -> tuple[int, int] | None:
    """Сколько боевых записей в журнале и какая последняя; None — базы нет."""
    try:
        with psycopg2.connect(**settings.dsn_kwargs) as conn, conn.cursor() as cur:
            cur.execute(_REAL_ROWS)
            found = cur.fetchone()
            return int(found[0]), int(found[1])
    except psycopg2.Error:  # pragma: no cover
        return None


def journal_problem(
    before: tuple[int, int] | None, after: tuple[int, int] | None
) -> str | None:
    """Что прогон сделал с боевыми записями журнала; None — ничего.

    Проверок две, и они разные. Число записей ловит удаление. Наибольший
    идентификатор ловит добавление: тест, забывший `is_test=True`, пишет
    боевую запись, и она навсегда остаётся в доказательной базе — так туда
    попала запись модели `test-model`.
    """
    if before is None or after is None:
        return None
    if after[0] < before[0]:
        return (
            f"прогон тестов удалил боевые записи llm_log: было {before[0]}, "
            f"стало {after[0]}. Тесты вправе удалять только записи с is_test = true"
        )
    if after[1] > before[1]:
        return (
            f"прогон тестов добавил боевые записи llm_log (после id {before[1]}): "
            "запись, сделанная тестом, обязана помечаться is_test = true. "
            "Передайте is_test=True в generate_conclusion или build_report"
        )
    return None


@pytest.fixture(scope="session", autouse=True)
def journal_is_not_erased() -> Iterator[None]:
    """Следит, чтобы прогон тестов не трогал боевые записи журнала.

    `llm_log` — доказательная база системы: по ней видно, что предъявлялось
    модели и что она отвечала. Однажды прогон pytest её уже уничтожил.
    Тесты помечают свои записи `is_test` и вправе убирать только их; попытка
    убрать чужие роняет весь прогон, а не проходит незамеченной.

    Добавление сторожится так же, как удаление: незапомеченная запись теста
    неотличима от рабочего прогона и портит статистику отказов навсегда,
    потому что понять задним числом, чем она сделана, уже нельзя.
    """
    before = _real_log_state()
    yield
    problem = journal_problem(before, _real_log_state())
    if problem is not None:
        raise AssertionError(problem)


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
