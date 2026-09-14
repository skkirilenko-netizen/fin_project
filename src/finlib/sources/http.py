"""Транспорт к внешним источникам: таймауты, ретраи, пауза, вежливый User-Agent."""

import logging
import time
from typing import Any

import httpx

from finlib.config import settings
from finlib.sources.errors import SourceUnavailableError

logger = logging.getLogger(__name__)

# Коды, при которых повтор имеет смысл: перегрузка и временные сбои шлюза.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class PoliteClient:
    """HTTP-клиент с паузой между запросами и повторами по экспоненте."""

    def __init__(
        self,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        timeout_s: float | None = None,
        retries: int | None = None,
        backoff_s: float | None = None,
        min_interval_s: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.girbo_base_url).rstrip("/")
        self.timeout_s = timeout_s if timeout_s is not None else settings.http_timeout_s
        self.retries = retries if retries is not None else settings.http_retries
        self.backoff_s = backoff_s if backoff_s is not None else settings.http_backoff_s
        self.min_interval_s = (
            min_interval_s if min_interval_s is not None else settings.http_min_interval_s
        )
        if not settings.girbo_contact:
            logger.warning(
                "GIRBO_CONTACT не задан: источник не сможет связаться с владельцем запросов"
            )
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_s,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )
        self._last_request_at: float | None = None

    def __enter__(self) -> "PoliteClient":
        """Вход в контекст."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Закрывает соединение."""
        self.close()

    def close(self) -> None:
        """Закрывает соединение."""
        self._client.close()

    def _wait_turn(self) -> None:
        """Выдерживает минимальный интервал между запросами к источнику."""
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        """Пауза перед повтором: Retry-After источника важнее собственной экспоненты."""
        own = self.backoff_s * (2**attempt)
        if response is None:
            return own
        header = response.headers.get("Retry-After")
        if header is None:
            return own
        try:
            return max(own, float(header))
        except ValueError:
            return own

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """Забирает ответ источника как есть; исчерпав повторы, поднимает SourceUnavailableError."""
        last_reason = ""
        for attempt in range(self.retries + 1):
            self._wait_turn()
            response: httpx.Response | None = None
            try:
                self._last_request_at = time.monotonic()
                response = self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                last_reason = f"таймаут {self.timeout_s} с: {exc}"
            except httpx.TransportError as exc:
                last_reason = f"сетевая ошибка: {exc}"
            else:
                if response.status_code < 400:
                    return response.content
                last_reason = f"HTTP {response.status_code}"
                if response.status_code not in RETRYABLE_STATUS:
                    raise SourceUnavailableError(f"{self.base_url}{path}: {last_reason}")

            if attempt < self.retries:
                delay = self._retry_delay(attempt, response)
                logger.warning(
                    "%s%s: %s, повтор %d из %d через %.1f с",
                    self.base_url, path, last_reason, attempt + 1, self.retries, delay,
                )
                time.sleep(delay)

        raise SourceUnavailableError(
            f"{self.base_url}{path}: {last_reason}; исчерпаны {self.retries} повтора"
        )
