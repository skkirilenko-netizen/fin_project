"""Тесты кэша сырых ответов: контрольная сумма и отсутствие обращений к сети."""

import json
from pathlib import Path

import httpx
import pytest
from probes import SEARCH_FOUND, read_probe

from finlib.sources.cache import RawCache, checksum
from finlib.sources.girbo import GirboSource
from finlib.sources.http import PoliteClient


def test_write_then_read(tmp_path: Path) -> None:
    """Записанный ответ читается байт в байт вместе с метаданными."""
    cache = RawCache("girbo", root=tmp_path)
    content = '{"значение": 1.5}'.encode()
    written = cache.write("search_123", content, url="https://example.invalid/x")
    assert not written.from_cache

    read = cache.read("search_123")
    assert read is not None
    assert read.content == content
    assert read.from_cache
    assert read.checksum == checksum(content)
    assert read.url == "https://example.invalid/x"


def test_meta_holds_checksum(tmp_path: Path) -> None:
    """Рядом с ответом лежит метафайл с контрольной суммой и размером."""
    cache = RawCache("girbo", root=tmp_path)
    content = b'{"a": 1}'
    path = cache.write("k", content, url="u").path
    meta = json.loads(path.with_suffix(path.suffix + ".meta.json").read_text(encoding="utf-8"))
    assert meta["checksum"] == checksum(content)
    assert meta["size"] == len(content)
    assert meta["url"] == "u"


def test_miss_on_absent_key(tmp_path: Path) -> None:
    """Пустой кэш даёт промах, а не пустой ответ."""
    assert RawCache("girbo", root=tmp_path).read("нет-такого") is None


def test_corrupted_content_is_a_miss(tmp_path: Path) -> None:
    """Расхождение контрольной суммы считается промахом: подменённый файл не используется."""
    cache = RawCache("girbo", root=tmp_path)
    path = cache.write("k", b'{"a": 1}', url="u").path
    path.write_bytes(b'{"a": 2}')
    assert cache.read("k") is None


def test_second_run_does_not_touch_network(tmp_path: Path) -> None:
    """Повторный запуск берёт ответ из кэша и в сеть не ходит."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=read_probe(SEARCH_FOUND))

    def build() -> GirboSource:
        client = PoliteClient(
            base_url="https://example.invalid",
            transport=httpx.MockTransport(handler),
            backoff_s=0.0,
            min_interval_s=0.0,
        )
        return GirboSource(client=client, cache=RawCache("girbo", root=tmp_path), journal=False)

    with build() as source:
        source.find_organization("7736050003")
    assert calls["n"] == 1

    with build() as source:
        source.find_organization("7736050003")
    assert calls["n"] == 1, "второй запуск обратился к источнику"


def test_force_refresh_goes_to_network(tmp_path: Path) -> None:
    """force_refresh обходит кэш и перезапрашивает источник."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=read_probe(SEARCH_FOUND))

    client = PoliteClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(handler),
        backoff_s=0.0,
        min_interval_s=0.0,
    )
    with GirboSource(
        client=client, cache=RawCache("girbo", root=tmp_path), journal=False
    ) as source:
        source.find_organization("7736050003")
        source.find_organization("7736050003", force_refresh=True)
    assert calls["n"] == 2


@pytest.mark.parametrize("probe", [SEARCH_FOUND])
def test_probe_files_are_present(probe: Path) -> None:
    """Пробы источника лежат в репозитории: тесты не зависят от сети."""
    assert probe.exists(), f"нет пробы {probe}"
