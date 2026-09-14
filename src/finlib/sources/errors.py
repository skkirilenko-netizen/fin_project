"""Ошибки слоя источников: внятные причины вместо молчаливых заглушек."""


class SourceError(Exception):
    """Базовая ошибка получения отчётности."""


class SourceUnavailableError(SourceError):
    """Источник недоступен: сеть, таймаут или ошибка на стороне сервера."""


class OrganizationNotFoundError(SourceError):
    """Организация с таким ИНН в источнике не найдена."""

    def __init__(self, inn: str) -> None:
        super().__init__(f"ИНН {inn} в ГИР БО не найден")
        self.inn = inn


class CreditOrganizationError(SourceError):
    """Кредитная организация: отчётность сдаётся в Банк России, методика неприменима."""

    def __init__(self, inn: str) -> None:
        super().__init__(
            f"ИНН {inn} принадлежит кредитной организации: отчётность по формам 0409 "
            "сдаётся в Банк России, анализ по РСБУ не проводится"
        )
        self.inn = inn


class ReportsNotPublishedError(SourceError):
    """Организация найдена, но опубликованной отчётности нет."""

    def __init__(self, inn: str) -> None:
        super().__init__(f"для ИНН {inn} в ГИР БО нет опубликованной отчётности")
        self.inn = inn
