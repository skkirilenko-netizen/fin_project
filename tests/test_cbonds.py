"""Клиент Cbonds: отказы, кэш и защита от молча пропущенного отбора."""

import json
from pathlib import Path

import pytest

from finlib.sources import cbonds


def _no_network(*args, **kwargs):
    """Заглушка сети: обращение к источнику в тесте — уже провал."""
    raise AssertionError("тест не должен ходить в сеть")


def test_метод_выводится_из_имени_отчёта() -> None:
    """Имя метода строится из поля `report` номенклатуры, а не подбирается."""
    assert cbonds.report_method("report_msfo_real") == "get_report_msfo_real"


def test_сохранённый_ответ_читается_без_сети(tmp_path: Path, monkeypatch) -> None:
    """Повторный запуск берёт ответ с диска и в источник не идёт."""
    monkeypatch.setattr(cbonds, "CACHE", tmp_path)
    monkeypatch.setattr(cbonds.httpx, "post", _no_network)
    (tmp_path / "проба.json").write_text(
        json.dumps({"items": [{"emitent_inn": "7717151380"}], "total": 1}),
        encoding="utf-8",
    )
    found = cbonds.fetch("get_report_msfo_real", "проба")
    assert found["items"][0]["emitent_inn"] == "7717151380"


def test_без_доступа_отказ_а_не_пустота(tmp_path: Path, monkeypatch) -> None:
    """Нет логина — исключение: пустой ответ означал бы «эмитента нет»."""
    monkeypatch.setattr(cbonds, "CACHE", tmp_path)
    monkeypatch.setattr(cbonds.settings, "cbonds_login", "")
    monkeypatch.setattr(cbonds.settings, "cbonds_password", "")
    with pytest.raises(cbonds.CbondsUnavailableError):
        cbonds.fetch("get_report_msfo_real", "чего_нет_на_диске")


def test_молча_пропущенный_отбор_обнаруживается() -> None:
    """Записи не отвечают отбору — источник его не сделал, это отказ.

    Проверено на живом источнике: запрос эмитентов по наименованию
    принимается и возвращает весь справочник, начиная с Ленинградской
    области. Ответ выглядит исправным.
    """
    filters = ({"field": "emitent_inn", "operator": "eq", "value": "9731004688"},)
    with pytest.raises(cbonds.FilterIgnoredError):
        cbonds._verify_applied(filters, [{"emitent_inn": "7717151380"}])


def test_выполненный_отбор_проходит() -> None:
    """Записи отвечают отбору — возражений нет."""
    filters = ({"field": "emitent_inn", "operator": "eq", "value": "9731004688"},)
    cbonds._verify_applied(filters, [{"emitent_inn": "9731004688"}])


def test_пустой_ответ_отбору_не_противоречит() -> None:
    """Эмитента у источника нет — проверять нечего, и это не отказ."""
    filters = ({"field": "emitent_inn", "operator": "eq", "value": "9731004688"},)
    cbonds._verify_applied(filters, [])


def test_предел_частоты_берётся_из_ответа() -> None:
    """Счётчик обращений и объявленный предел — разные величины, обе нужны."""
    pace = cbonds.Pace()
    assert pace.per_minute_max == cbonds.DEFAULT_PER_MINUTE
    assert pace.requested == 0 and pace.from_cache == 0


def test_счётчики_не_общие_у_двух_замеров() -> None:
    """Отметки времени у каждого счётчика свои: иначе замеры сложатся."""
    first, second = cbonds.Pace(), cbonds.Pace()
    first.stamps.append(1.0)
    assert second.stamps == []
