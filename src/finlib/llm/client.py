"""Клиент к локальной языковой модели через совместимый с OpenAI интерфейс.

Локальный контур: обращение идёт на LLM_BASE_URL из .env, внешние API
запрещены. Температура нулевая — заключение должно быть воспроизводимым.
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from finlib.config import settings

logger = logging.getLogger(__name__)

TEMPERATURE = Decimal(0)


class LLMUnavailableError(RuntimeError):
    """Модель недоступна: не запущена, не отвечает или вернула ошибку."""


@dataclass(frozen=True, slots=True)
class Completion:
    """Ответ модели вместе с тем, что нужно записать в журнал."""

    text: str
    model: str
    duration_ms: int


class LLMClient:
    """Обращение к модели с нулевой температурой."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.timeout_s = timeout_s if timeout_s is not None else settings.llm_timeout_s
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_s,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )

    def __enter__(self) -> "LLMClient":
        """Вход в контекст."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Закрывает соединение."""
        self.close()

    def close(self) -> None:
        """Закрывает соединение."""
        self._client.close()

    def complete(self, prompt: str) -> Completion:
        """Отправляет инструкцию модели и возвращает её ответ."""
        payload = {
            "model": self.model,
            "temperature": float(TEMPERATURE),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        started = time.monotonic()
        try:
            response = self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(
                f"модель {self.model} не ответила за {self.timeout_s} с: {exc}"
            ) from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(
                f"модель недоступна по адресу {self.base_url}: {exc}. "
                "Проверьте, запущен ли Ollama"
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise LLMUnavailableError(
                f"модель вернула HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMUnavailableError(
                f"неожиданная структура ответа модели: {response.text[:300]}"
            ) from exc

        logger.info("модель %s ответила за %d мс", self.model, duration_ms)
        return Completion(text=text, model=self.model, duration_ms=duration_ms)
