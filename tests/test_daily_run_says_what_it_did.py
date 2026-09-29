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
import os
import sys
from contextlib import redirect_stdout
from datetime import date

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


def _stage_run(tmp_path, monkeypatch, body: str, marker_exists: bool) -> dict:  # noqa: ANN001
    """Прогоняет стадию-заглушку и отдаёт её исход."""
    sys.path.insert(0, str(settings.base_dir / "scripts"))
    import daily_run

    marker = tmp_path / "result.json"
    if marker_exists:
        marker.write_text("{}", encoding="utf-8")
        old = 1_700_000_000
        os.utime(marker, (old, old))
    script = tmp_path / "stage.py"
    script.write_text(body.replace("MARKER", str(marker)), encoding="utf-8")
    monkeypatch.setattr(daily_run, "_marker", lambda stage: marker)
    stage = daily_run.Stage(
        code="probe", name="проба", script=str(script), every=7,
        source="cbonds", why="тест",
    )
    return daily_run._run_stage(stage, dry=False)


def test_a_stage_that_left_its_file_untouched_is_not_done(tmp_path, monkeypatch) -> None:
    """Стадия, не обновившая файл дня, пишет «cached» с датой файла, а не «done».

    С 22.09.2026 стадия выпусков писала «done», не сделав ни одного запроса:
    ответы брались из кэша, и признаки дефолта застыли.
    """
    said = _stage_run(tmp_path, monkeypatch, "pass\n", marker_exists=True)
    assert said["status"] == "cached"
    assert said["file_date"] == f"{date.fromtimestamp(1_700_000_000):%Y-%m-%d}"


def test_a_stage_without_any_file_is_not_done(tmp_path, monkeypatch) -> None:
    """Файла доставки нет вовсе — тоже не «done»."""
    said = _stage_run(tmp_path, monkeypatch, "pass\n", marker_exists=False)
    assert said["status"] == "cached" and said["file_date"] is None


def test_a_stage_that_wrote_its_file_is_done(tmp_path, monkeypatch) -> None:
    """Стадия, переписавшая файл, — «done», в том числе с частотой раз в неделю."""
    body = "from pathlib import Path\nPath('MARKER').write_text('{\"new\": 1}')\n"
    said = _stage_run(tmp_path, monkeypatch, body, marker_exists=True)
    assert said["status"] == "done"


def test_a_failed_stage_stays_failed(tmp_path, monkeypatch) -> None:
    """Отказ не подменяется «кэшем»: он старше."""
    said = _stage_run(tmp_path, monkeypatch, "raise RuntimeError('нет')\n", True)
    assert said["status"] == "failed"


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


def test_a_repeated_run_does_not_overwrite_the_scheduled_report() -> None:
    """Отчёт прогона по расписанию главный; иной прогон пишет свой рядом.

    29.09.2026 ручной повтор в 12:02 переписал утренний отчёт, и восстановить
    его оказалось нечем.
    """
    import importlib.util
    from datetime import datetime

    spec = importlib.util.spec_from_file_location("daily_run", DAILY)
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    day = date(2026, 9, 29)
    main = daily.report_path(day, datetime(2026, 9, 29, 10, 0, 1), "")
    again = daily.report_path(day, datetime(2026, 9, 29, 12, 2), "Повторный прогон")
    assert main.name == "changes_2026-09-29.md"
    assert again.name == "changes_2026-09-29_1202.md"
    # Шапку повторного печатает сам отчёт по переданной строке.
    assert '"--note", said' in _source()


def _daily():  # noqa: ANN202
    """Модуль ежедневного прогона."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("daily_run", DAILY)
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    return daily


def test_a_repeat_does_not_overwrite_the_scheduled_point(db_conn) -> None:  # noqa: ANN001
    """Повтор не затирает плановую точку: пишет рядом, день читается последней.

    29.09.2026 ручной повтор переписал точки маршрута дня, и утренний отчёт
    изменений восстановить стало нечем (решение владельца: плановые точки
    хранить отдельно; маршрут дня и отчёт — последняя точка, аудит — плановая).
    """
    daily = _daily()
    assert daily.point_kind(scheduled=True) == "run"
    assert daily.point_kind(scheduled=False) == "repeat"
    day, inn = date(2000, 1, 3), "0000000001"
    rows, latest = _write_day(
        db_conn,
        daily,
        day,
        inn,
        (("run", "attention"), ("repeat", "review"), ("repeat", "clear")),
    )
    assert rows == [("run", "attention"), ("repeat", "review"), ("repeat", "clear")]
    assert latest == "clear"


def test_a_manual_run_before_the_scheduled_one_is_a_repeat(db_conn) -> None:  # noqa: ANN001
    """Ручной прогон до планового — повтор, и последней точкой дня становится плановый.

    Поправка владельца 29.09.2026: всякий прогон не по расписанию пишет
    `repeat`, даже первый за день, а последняя точка дня выбирается по номеру
    прогона, а не по виду: иначе ручной прогон в 9:00 занял бы место плановой
    точки либо остался бы «последним» после планового в 10:00.
    """
    daily = _daily()
    day, inn = date(2000, 1, 4), "0000000001"
    rows, latest = _write_day(
        db_conn, daily, day, inn, (("repeat", "review"), ("run", "attention"))
    )
    assert rows == [("repeat", "review"), ("run", "attention")]
    assert latest == "attention"


def _write_day(db_conn, daily, day: date, inn: str, points: tuple) -> tuple:  # noqa: ANN001
    """Пишет точки дня по порядку прогонов; отдаёт записанное и последнюю корзину."""
    from finlib.db import execute, fetch_all, fetch_one

    for kind, basket in points:
        run_id = fetch_one(
            "INSERT INTO routing_run (kind, as_of, status) VALUES ('run', %(d)s, 'done') "
            "RETURNING id",
            {"d": day},
            conn=db_conn,
        )["id"]
        execute(
            daily.point_sql(kind),
            {
                "run": run_id, "kind": kind, "inn": inn, "as_of": day,
                "standard": None, "basket": basket, "subgroup": "", "grounds": [],
                "grounds_all": [], "inputs": "{}", "fingerprint": "f",
                "report_date": None,
            },
            conn=db_conn,
        )
    rows = fetch_all(
        "SELECT kind, basket FROM routing_history WHERE inn = %(i)s AND as_of = %(d)s "
        "ORDER BY id",
        {"i": inn, "d": day},
        conn=db_conn,
    )
    latest = fetch_one(
        "SELECT basket FROM routing_day WHERE inn = %(i)s AND as_of = %(d)s",
        {"i": inn, "d": day},
        conn=db_conn,
    )
    return [(row["kind"], row["basket"]) for row in rows], latest["basket"]
