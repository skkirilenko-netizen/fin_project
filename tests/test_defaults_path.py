"""Перечень дефолтов берётся свежайшей доставкой, а не первым файлом.

До 25.09.2026 перечень лежал одним файлом от 22.09 и прогоном не обновлялся:
дефолт, случившийся позже, маршрут не увидел бы никогда.
"""

from pathlib import Path

from finlib.sources import cbonds_events


def test_the_freshest_dated_delivery_wins(tmp_path: Path, monkeypatch) -> None:
    """Датированный файл дня старше ручной доставки без даты."""
    monkeypatch.setattr(cbonds_events, "CACHE", tmp_path)
    monkeypatch.setattr(cbonds_events, "DEFAULTS", tmp_path / "defaults_ru.json")
    assert cbonds_events.defaults_path() is None
    (tmp_path / "defaults_ru.json").write_text("{}", encoding="utf-8")
    assert cbonds_events.defaults_path() == tmp_path / "defaults_ru.json"
    for day in ("2026-09-25", "2026-09-26"):
        (tmp_path / f"defaults_ru_{day}.json").write_text("{}", encoding="utf-8")
    assert cbonds_events.defaults_path() == tmp_path / "defaults_ru_2026-09-26.json"
