"""Тесты единой точки настроек."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".env").exists(),
    reason="нет .env: настройки читаются только в локальном контуре",
)


def test_paths_are_inside_repo() -> None:
    """Все рабочие каталоги вычисляются от корня репозитория."""
    from finlib.config import settings

    assert settings.base_dir == REPO_ROOT
    for path in (
        settings.data_dir,
        settings.raw_dir,
        settings.quarantine_dir,
        settings.output_dir,
        settings.methodology_dir,
        settings.prompts_dir,
        settings.sql_dir,
    ):
        assert path.is_relative_to(REPO_ROOT)


def test_dsn_kwargs_skip_empty_password() -> None:
    """Пустой пароль не попадает в аргументы подключения."""
    from finlib.config import settings

    kwargs = settings.dsn_kwargs
    assert kwargs["dbname"] == settings.db_name
    assert kwargs["user"] == settings.db_user
    if not settings.db_password:
        assert "password" not in kwargs
