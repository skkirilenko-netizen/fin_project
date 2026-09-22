"""Единица комплекта называется в каждом выходе, и проверка у них одна.

**Самая тихая из найденных ошибок, найденная во второй раз.** У ФосАгро
документ печатал «663 888 тыс. руб.» там, где отчётность в миллионах, —
и для документа поставили контроль (`report/consistency.py::check_unit`).
Список наблюдения и выгрузка печатали мимо него: 22.09.2026 повторная проверка
нашла 39 строк, где миллионы подписаны тысячами, — у Мечела, АФК «Система»,
Уральской Стали. Ни один контроль сходимости этого не видит: баланс сходится,
разделы сходятся, коэффициенты верны, и неверны только абсолютные величины,
ровно в тысячу раз.

Причина была одна и находилась в единой точке печати: `format_metric`
подставляла «тыс. руб.» умолчанием, когда единицу не передали. Умолчание,
ошибка которого тиха, умолчанием быть не должно — поэтому денежная величина
без названной единицы теперь не печатается вовсе, а `route`, `shown`
и блоки модели получили единицу обязательным доводом.

Здесь проверяется и то и другое: отказ в единой точке печати и то, что
**каждый выход единицу называет**. Проверка выходов структурная, по дереву
разбора: упоминание единицы в комментарии выходом её не называет.
"""

import ast
from decimal import Decimal

import pytest

from finlib.config import settings
from finlib.metrics.definitions import Unit
from finlib.metrics.display import (
    UnitNotNamedError,
    foreign_units,
    format_metric,
)

# Выходы проекта, печатающие денежные величины. Перечень поимённый: новый
# выход обязан быть здесь назван, иначе он повторит историю списка —
# «в документе проверка стоит, в списке её нет».
OUTPUTS = {
    "src/finlib/report/composition.py": "разделы заключения",
    "src/finlib/report/appendix.py": "приложение заключения",
    "src/finlib/llm/context.py": "блоки языковой модели",
    "src/finlib/scoring/theses.py": "предписанные тезисы",
    "src/finlib/scoring/routing.py": "основания маршрута",
    "src/finlib/cli.py": "вывод терминала",
    # **Величины строк списка печатает маршрут, а не страница.** С появлением
    # маршрута по РСБУ справочников показателей стало два, и страница,
    # набирающая величину сама, печатала бы наименования чужого справочника —
    # тот же дефект, что был в приложении по МСФО. Печать переехала сюда,
    # и перечень выходов обязан переехать вместе с ней: иначе он проверял бы
    # файл, который денег больше не печатает, и не проверял бы тот, который
    # печатает.
    "src/finlib/scoring/routing_store.py": "величины строк списка и выгрузки",
    "eval/watchlist_csv.py": "выгрузка для контура",
}

# Единицы, при которых `money` не нужен: они не денежные, и приписывать им
# единицу комплекта было бы ошибкой обратного знака.
NOT_MONEY = {"PERCENT", "RATIO", "DAYS"}


# --- единая точка печати ----------------------------------------------------


def test_money_without_a_named_unit_is_not_printed() -> None:
    """Денежная величина без единицы комплекта не печатается вовсе."""
    with pytest.raises(UnitNotNamedError):
        format_metric(Decimal("663888"), Unit.THOUSAND_RUB)
    with pytest.raises(UnitNotNamedError):
        format_metric(Decimal("663888"), Unit.THOUSAND_RUB, money="")


def test_the_named_unit_is_the_one_printed() -> None:
    """Печатается названная единица комплекта, а не единица стандарта."""
    assert format_metric(
        Decimal("663888"), Unit.THOUSAND_RUB, money="млн руб."
    ).endswith("млн руб.")
    assert format_metric(
        Decimal("663888"), Unit.THOUSAND_RUB, money="тыс. руб."
    ).endswith("тыс. руб.")


def test_a_non_money_value_needs_no_unit() -> None:
    """Коэффициент, процент и дни единицы комплекта не требуют.

    Обратная ошибка того же рода: «0,82 млн руб.» у текущей ликвидности —
    такое же утверждение в чужой единице.
    """
    assert format_metric(Decimal("0.82"), Unit.RATIO) == "0,82"
    assert format_metric(Decimal("63.0971"), Unit.DAYS) == "63,1 дн."


def test_a_foreign_unit_is_seen_in_any_text() -> None:
    """Сверку чужой единицы делает одна функция на все выходы."""
    assert foreign_units("выручка 663 888 тыс. руб.", "млн руб.") == ["тыс. руб."]
    assert foreign_units("выручка 663 888 млн руб.", "млн руб.") == []
    # Единицу назвать нечем — чужой оказывается любая, и это верно: печатать
    # деньги в таком выходе нельзя вовсе.
    assert foreign_units("выручка 663 888 млн руб.", "") == ["млн руб."]


# --- каждый выход называет единицу ------------------------------------------


def _money_calls_without_a_unit(tree: ast.AST) -> list[int]:
    """Строки, где денежная величина печатается без названной единицы.

    Вызов считается денежным, если единица не названа явно как неденежная:
    `Unit.RATIO`, `Unit.PERCENT`, `Unit.DAYS`. Всё остальное — величина,
    единица которой известна только комплекту.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
        )
        if name == "format_metric":
            unit = node.args[1] if len(node.args) > 1 else None
            if (
                isinstance(unit, ast.Attribute)
                and unit.attr in NOT_MONEY
            ):
                continue
            named = any(word.arg == "money" for word in node.keywords)
            if not named:
                found.append(node.lineno)
        elif name == "shown":
            # У `IfrsMetricsView.shown` единица — третий довод, позиционный
            # или по имени: показатель бывает и денежным, и коэффициентом,
            # и различить их здесь нечем — значит, называть надо всегда.
            named = len(node.args) >= 3 or any(
                word.arg == "money" for word in node.keywords
            )
            if not named:
                found.append(node.lineno)
    return found


def test_every_output_names_the_unit_of_the_set() -> None:
    """Ни один выход не печатает деньги, не назвав единицу комплекта."""
    offenders: list[str] = []
    for name, what in OUTPUTS.items():
        path = settings.base_dir / name
        assert path.exists(), f"выход {name} ({what}) не найден: перечень устарел"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{name}:{line} — {what}" for line in _money_calls_without_a_unit(tree)]
    assert not offenders, (
        "денежная величина печатается без единицы комплекта:\n  "
        + "\n  ".join(offenders)
    )


def test_the_check_is_not_satisfied_by_emptiness() -> None:
    """Обратная сторона запрета: выход обязан деньги печатать.

    Перечень выходов можно удовлетворить, не печатая величин вовсе, — и тогда
    проверка мерила бы пустоту. Поэтому у каждого выхода требуется хотя бы
    один вызов единой точки печати.
    """
    silent: list[str] = []
    for name, what in OUTPUTS.items():
        text = (settings.base_dir / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        calls = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                getattr(node.func, "attr", "") in ("format_metric", "shown", "money")
                or getattr(node.func, "id", "") in ("format_metric", "shown", "money")
            )
        )
        if not calls:
            silent.append(f"{name} — {what}")
    assert not silent, (
        "выход объявлен, а единую точку печати не зовёт:\n  " + "\n  ".join(silent)
    )


def test_the_mandatory_argument_cannot_be_skipped_silently() -> None:
    """У `route` и `shown` единица — довод без умолчания.

    То же основание, по которому обязателен `standards` у `compute_metric`
    и `stops` у `assess`: довод, который можно молча не передать, неотличим
    от непереданного.
    """
    import inspect

    from finlib.metrics.ifrs_view import IfrsMetricsView
    from finlib.scoring.routing import route

    assert (
        inspect.signature(route).parameters["unit"].default is inspect.Parameter.empty
    )
    money = inspect.signature(IfrsMetricsView.shown).parameters["money"]
    assert money.default is inspect.Parameter.empty
