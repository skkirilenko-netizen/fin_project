"""Самодостаточный HTML согласованного интерфейса, без внешних ресурсов."""

from pathlib import Path

from finlib.report.watchlist_data import json_text

TEMPLATES = Path(__file__).with_name("templates")


def render(data: dict) -> str:
    """Встраивает инертные данные и локальный код в согласованный шаблон."""
    template = (TEMPLATES / "watchlist.html").read_text(encoding="utf-8")
    app = (TEMPLATES / "watchlist.js").read_text(encoding="utf-8")
    return template.replace("/*APP*/", app).replace("/*DATA*/", json_text(data))
