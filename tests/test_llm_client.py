"""Тесты клиента к модели. Сеть не используется."""

import json

import httpx
import pytest

from finlib.llm.client import LLMClient, LLMUnavailableError


def make_client(handler, **kwargs) -> LLMClient:
    """Клиент поверх поддельного транспорта."""
    return LLMClient(
        base_url="http://model.invalid/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def answer(text: str) -> httpx.Response:
    """Ответ, совместимый с интерфейсом OpenAI."""
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_returns_model_text() -> None:
    """Текст ответа извлекается из структуры."""
    with make_client(lambda request: answer("Заключение")) as client:
        result = client.complete("инструкция")
    assert result.text == "Заключение"
    assert result.model == "test-model"
    assert result.duration_ms >= 0


def test_temperature_is_zero() -> None:
    """Температура нулевая: заключение должно быть воспроизводимым."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return answer("текст")

    with make_client(handler) as client:
        client.complete("инструкция")
    assert seen[0]["temperature"] == 0
    assert seen[0]["model"] == "test-model"
    assert seen[0]["messages"][0]["content"] == "инструкция"
    assert seen[0]["stream"] is False


def test_unavailable_model_gives_clear_error() -> None:
    """Недоступная модель даёт внятную ошибку, а не заглушку."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("отказано в соединении", request=request)

    with (
        make_client(handler) as client,
        pytest.raises(LLMUnavailableError, match="Ollama"),
    ):
        client.complete("инструкция")


def test_timeout_is_reported() -> None:
    """Таймаут называется таймаутом."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("время вышло", request=request)

    with (
        make_client(handler, timeout_s=1.0) as client,
        pytest.raises(LLMUnavailableError, match="не ответила"),
    ):
        client.complete("инструкция")


def test_http_error_is_reported() -> None:
    """Ошибка на стороне модели не превращается в пустой ответ."""
    with (
        make_client(lambda request: httpx.Response(500, text="сбой")) as client,
        pytest.raises(LLMUnavailableError, match="HTTP 500"),
    ):
        client.complete("инструкция")


def test_unexpected_payload_is_reported() -> None:
    """Неожиданная структура ответа выявляется, а не проглатывается."""
    with (
        make_client(lambda request: httpx.Response(200, json={"foo": "bar"})) as client,
        pytest.raises(LLMUnavailableError, match="структура ответа"),
    ):
        client.complete("инструкция")
