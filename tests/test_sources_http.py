"""Тесты транспорта: повторы, 429, таймауты. Сеть не используется."""

import httpx
import pytest

from finlib.sources.errors import SourceUnavailableError
from finlib.sources.http import PoliteClient


def make_client(handler, **kwargs) -> PoliteClient:
    """Клиент поверх поддельного транспорта, без пауз."""
    params = {"backoff_s": 0.0, "min_interval_s": 0.0, "retries": 2}
    params.update(kwargs)
    return PoliteClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(handler),
        **params,
    )


def test_returns_raw_bytes() -> None:
    """Успешный ответ отдаётся как есть, без преобразований."""
    payload = '{"значение": 1.5}'.encode()
    with make_client(lambda r: httpx.Response(200, content=payload)) as client:
        assert client.get_bytes("/x") == payload


def test_user_agent_is_sent() -> None:
    """Запрос уходит с вежливым User-Agent."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, content=b"{}")

    with make_client(handler) as client:
        client.get_bytes("/x")
    assert seen and seen[0].startswith("fin-analysis/")


def test_retries_then_succeeds() -> None:
    """Временный сбой повторяется и в итоге отдаёт ответ."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b'{"ok": true}')

    with make_client(handler) as client:
        assert client.get_bytes("/x") == b'{"ok": true}'
    assert calls["n"] == 3


def test_too_many_requests_is_retried() -> None:
    """429 обрабатывается как временный отказ, а не как фатальный."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"{}")

    with make_client(handler) as client:
        client.get_bytes("/x")
    assert calls["n"] == 2


def test_retry_after_is_respected() -> None:
    """Пауза источника из Retry-After не игнорируется."""
    with make_client(lambda r: httpx.Response(429, headers={"Retry-After": "7"})) as client:
        assert client._retry_delay(0, httpx.Response(429, headers={"Retry-After": "7"})) == 7.0
        assert client._retry_delay(0, httpx.Response(429)) == 0.0


def test_client_error_is_not_retried() -> None:
    """Ошибка запроса повторов не заслуживает — сразу внятная ошибка."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with make_client(handler) as client, pytest.raises(SourceUnavailableError, match="HTTP 404"):
        client.get_bytes("/x")
    assert calls["n"] == 1


def test_timeout_exhausts_retries_and_raises() -> None:
    """Недоступный источник даёт ошибку, а не заглушку."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("время вышло", request=request)

    with make_client(handler) as client, pytest.raises(SourceUnavailableError, match="таймаут"):
        client.get_bytes("/x")


def test_network_error_message_names_source() -> None:
    """В тексте ошибки видно, какой адрес не ответил."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет маршрута", request=request)

    with (
        make_client(handler) as client,
        pytest.raises(SourceUnavailableError, match="example.invalid"),
    ):
        client.get_bytes("/x")
