"""Настройки проекта: единая точка чтения .env и путей."""

import getpass
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Параметры подключения к БД, локальной модели и каталогов проекта."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "findb"
    db_user: str = getpass.getuser()
    db_password: str = ""

    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str
    # Заключение пишется большой моделью на локальной машине: минуты, не секунды.
    llm_timeout_s: float = 600.0
    embed_model: str = "bge-m3"

    # Источник отчётности. Домен уже менялся, поэтому в коде его нет.
    girbo_base_url: str = "https://bo.nalog.gov.ru"
    girbo_contact: str = ""

    http_timeout_s: float = 30.0
    http_retries: int = 3
    http_backoff_s: float = 1.0
    http_min_interval_s: float = 0.5

    @property
    def user_agent(self) -> str:
        """Вежливый User-Agent с контактом; без контакта — только имя и версия."""
        from finlib import __version__

        base = f"fin-analysis/{__version__}"
        return f"{base} (+{self.girbo_contact})" if self.girbo_contact else base

    @property
    def base_dir(self) -> Path:
        """Корень репозитория."""
        return BASE_DIR

    @property
    def data_dir(self) -> Path:
        """Каталог данных, не попадает в репозиторий."""
        return BASE_DIR / "data"

    @property
    def raw_dir(self) -> Path:
        """Каталог сырых ответов внешних источников."""
        return self.data_dir / "raw"

    @property
    def quarantine_dir(self) -> Path:
        """Каталог отчётности, не прошедшей контроли качества."""
        return self.data_dir / "quarantine"

    @property
    def output_dir(self) -> Path:
        """Каталог готовых заключений."""
        return self.data_dir / "output"

    @property
    def methodology_dir(self) -> Path:
        """Каталог справочников и нормативов методики."""
        return BASE_DIR / "methodology"

    @property
    def prompts_dir(self) -> Path:
        """Каталог текстов инструкций для модели."""
        return BASE_DIR / "prompts"

    @property
    def sql_dir(self) -> Path:
        """Каталог DDL и представлений."""
        return BASE_DIR / "sql"

    @property
    def dsn_kwargs(self) -> dict[str, Any]:
        """Аргументы psycopg2.connect; пустой пароль не передаётся."""
        kwargs: dict[str, Any] = {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
        }
        if self.db_password:
            kwargs["password"] = self.db_password
        return kwargs


settings = Settings()


def ensure_dirs() -> None:
    """Создаёт рабочие каталоги data/, если их нет."""
    for path in (settings.raw_dir, settings.quarantine_dir, settings.output_dir):
        path.mkdir(parents=True, exist_ok=True)
