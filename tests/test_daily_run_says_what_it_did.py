"""Ежедневный прогон делает всё и говорит правду о том, чего не сделал.

**Отчёт, молчащий об отказе доставки, врёт молчанием.** «Изменений нет»
при недошедших данных и при полных — разные сведения, а выглядят одинаково.
Правило объявлено с заведения ежедневного прогона; до 24.09.2026 отчёт
его не исполнял — отказ лежал в журнале прогона и в отчёт не попадал.

Здесь проверяется устройство, а не прогон: состав стадий, состав выходов
и то, как отчёт печатает здоровье доставок. Сам прогон ходит в сеть и в базу,
и держать его в тестах нельзя.
"""

import ast
import io
import sys
from contextlib import redirect_stdout

from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "eval"))

from change_report_run import _health  # noqa: E402

DAILY = settings.base_dir / "scripts" / "daily_run.py"


def _source() -> str:
    """Текст ежедневного прогона."""
    return DAILY.read_text(encoding="utf-8")


def test_the_daily_run_builds_every_output() -> None:
    """Прогон собирает список, выгрузку и карточки, а не часть из них.

    **Карточка без прогона устаревает молча.** Список ссылается на карточки,
    и вчерашняя карточка выглядит так же, как сегодняшняя, — открывший её
    прочтёт вчерашнюю корзину как сегодняшнюю.
    """
    text = _source()
    for name in ("watchlist_run.py", "watchlist_csv.py", "issuer_card_run.py"):
        assert name in text, f"ежедневный прогон не собирает {name}"
    assert "change_report_run.py" in text, "прогон не строит отчёт изменений"


def test_the_order_puts_the_list_before_the_cards() -> None:
    """Список собирается раньше карточек, и порядок этот — часть дела.

    Список ставит ссылку только на лежащую карточку, а карточка ссылается
    на свежайший собранный список: собранные раньше списка карточки сослались
    бы на вчерашний.
    """
    text = _source()
    assert text.index("watchlist_run.py") < text.index("issuer_card_run.py")


def test_the_market_series_is_recomputed_after_delivery() -> None:
    """Ряд спредов пересчитывается после доставки, а не читается вчерашний."""
    tree = ast.parse(_source())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "market_series" in called, "ряд не пересчитывается в прогоне"


def _function(name: str) -> ast.FunctionDef:
    """Функция ежедневного прогона по имени."""
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"в прогоне нет функции {name}")


def _calls(node: ast.AST) -> list[str]:
    """Имена вызываемых функций в порядке появления в тексте."""
    found = [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    ]
    return [item.func.id for item in sorted(found, key=lambda item: item.lineno)]


def test_the_run_record_is_opened_before_any_delivery() -> None:
    """Строка прогона пишется при старте, а не после доставок.

    25.09.2026 прогон оборвался на снимке рейтингов, и будь обрыв вне
    перехвата стадии, журнал не сказал бы ничего: «прогона не было»
    и «прогон упал» выглядели бы одинаково.
    """
    called = _calls(_function("main"))
    assert "_open_run" in called and "_deliver_and_route" in called
    assert called.index("_open_run") < called.index("_deliver_and_route")
    assert "_close_failed" in called, "оборвавшийся прогон не закрывает свою строку"


def test_a_broken_delivery_is_written_to_the_log() -> None:
    """Обрыв доставки идёт в журнал процесса с трассировкой, а не одной строкой."""
    stage = _function("_run_stage")
    logged = [
        item
        for item in ast.walk(stage)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "exception"
    ]
    assert logged, "исключение доставки не пишется в daily_run.log"


def _said(rows: list) -> str:
    """Что отчёт печатает о здоровье доставок."""
    out = io.StringIO()
    with redirect_stdout(out):
        _health("run", rows)
    return out.getvalue()


def test_a_failed_delivery_stands_at_the_top_of_the_report() -> None:
    """Отказ источника назван вместе с причиной, а не пропущен."""
    text = _said(
        [
            {
                "status": "failed",
                "note": "источник отказал на доставке «снимок рейтингов»",
                "sources": [
                    {"code": "ratings", "name": "снимок рейтингов",
                     "status": "failed", "error": "HTTP 503"},
                    {"code": "moex", "name": "сектор риска", "status": "ok"},
                ],
            }
        ]
    )
    assert "Доставка неполна" in text
    assert "снимок рейтингов" in text and "HTTP 503" in text
    # И прямо сказано, что пустой перечень изменений мог выйти от этого.
    assert "новых\nданных не пришло" in text or "новых данных не пришло" in text


def test_a_full_delivery_is_named_too() -> None:
    """Полная доставка называется тоже: молчание читалось бы как отказ."""
    text = _said(
        [
            {
                "status": "done",
                "note": "эмитентов 900",
                "sources": [
                    {"code": "ratings", "name": "снимок рейтингов", "status": "ok"}
                ],
            }
        ]
    )
    assert "снимок рейтингов — ok" in text
    assert "Доставка неполна" not in text


def test_a_day_without_a_run_record_says_so() -> None:
    """Записи прогона нет — это не «доставки прошли», и так и написано."""
    text = _said([])
    assert "Записи прогона за этот день нет" in text
    assert "это не «доставки прошли»" in text
