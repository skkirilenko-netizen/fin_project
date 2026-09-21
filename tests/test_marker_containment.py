"""Вхождение маркера проверяется одной функцией, и обход запрещён строением.

Дважды за две недели проверка, построенная на вхождении маркера, отвечала «да»
на любой текст, потому что маркер после приведения обращался в пустую строку:
сперва знак валюты «₽» («валюта определена» у каждого комплекта), потом знак
сноски «*» (58 «сносок» из заголовков и колонтитулов). Ноль срабатываний
заметен глазом; единица срабатываний на каждом документе не заметна ничем.

Одного исправления мало: правило обязано держаться строением, а не памятью.
Поэтому здесь два теста — на поведение функции и на то, что её не обходят.
"""

import ast
from pathlib import Path

import pytest

from finlib.config import settings
from finlib.utils import EmptyMarkerError, marked_by, markers_found

# Где проверка вхождения законна: сама функция и её тест.
ALLOWED = {"utils.py"}

# Способы привести строку к сравнимому виду. Вхождение по приведённой строке —
# ровно то место, где пустой маркер проходит незамеченным. `strip` в перечень
# не входит намеренно: он не стирает знаки, а срезает пробелы, и на нём
# дефекта этого рода не бывает.
PREPARERS = {"normalize_name", "casefold", "lower", "upper"}


def _sources() -> list[Path]:
    """Модули пакета, кроме объявленных исключений."""
    root = settings.base_dir / "src" / "finlib"
    return [item for item in sorted(root.rglob("*.py")) if item.name not in ALLOWED]


def _prepared_names(tree: ast.AST) -> set[str]:
    """Имена, которым присвоен результат приведения строки.

    Ищется присваивание вида `lowered = text.casefold()` или
    `normalized = normalize_name(text)`: именно такие переменные и стоят
    справа от `in` в проверках вхождения.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value.func
        name = (
            call.attr
            if isinstance(call, ast.Attribute)
            else call.id
            if isinstance(call, ast.Name)
            else ""
        )
        if name not in PREPARERS:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found.add(target.id)
    return found


def _is_prepared(node: ast.expr, prepared: set[str]) -> bool:
    """Приведённая ли это строка: переменная приведения либо сам вызов."""
    if isinstance(node, ast.Name):
        return node.id in prepared
    if isinstance(node, ast.Call):
        call = node.func
        if isinstance(call, ast.Attribute):
            return call.attr in PREPARERS
        if isinstance(call, ast.Name):
            return call.id in PREPARERS
    return False


def test_containment_refuses_an_empty_marker() -> None:
    """Пустой маркер — ошибка, а не совпадение.

    Знак валюты и знак сноски после приведения пусты, и проверка отвечала бы
    «да» на любой текст. Предмет, опознаваемый знаком, проверяется строением
    строки, а не вхождением.
    """
    with pytest.raises(EmptyMarkerError, match="пуст"):
        markers_found("любой текст", ("₽",), _normalized)
    with pytest.raises(EmptyMarkerError):
        markers_found("любой текст", ("",))
    with pytest.raises(EmptyMarkerError):
        markers_found("любой текст", ("   ",))


def _normalized(text: str) -> str:
    """Приведение, на котором знаки обращаются в пустую строку."""
    from finlib.normalize.lines import normalize_name

    return normalize_name(text)


def test_containment_prepares_text_and_markers_alike() -> None:
    """Текст и маркер приводятся одинаково — иначе сравнение мимо.

    Приведённый маркер против всего лишь опущенного в нижний регистр текста —
    тот же дефект, только тише: сравнение не срабатывает никогда.
    """
    assert markers_found("Отчёт О ПРИБЫЛИ", ("о прибыли",), _normalized) == (
        "о прибыли",
    )
    assert marked_by("ПАО «ФосАгро»", ("фосагро",), str.casefold)
    assert not marked_by("ПАО «ФосАгро»", ("акрон",), str.casefold)
    # Найденные маркеры возвращаются перечнем: тезис без основания проверить
    # нечем, и вызывающему нужно назвать, что сработало.
    assert markers_found("эскроу и долевое строительство", ("эскроу", "концессия")) == (
        "эскроу",
    )


def test_no_module_checks_containment_on_a_prepared_string() -> None:
    """Прямое `in` по приведённой строке запрещено строением.

    Одного исправления мало: правило, которое держится памятью, воспроизводит
    дефект на следующем перечне маркеров. Проверка структурная — по дереву
    разбора, а не по вхождению подстроки в исходник: упоминание `in lowered`
    в комментарии нарушением не является, а обход через переменную с другим
    именем — является.
    """
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prepared = _prepared_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for operator, right in zip(node.ops, node.comparators, strict=True):
                if not isinstance(operator, ast.In | ast.NotIn):
                    continue
                if not _is_prepared(right, prepared):
                    continue
                # Слева либо приведённая строка, либо переменная-маркер,
                # либо написанный тут же литерал — все три случая проверка
                # вхождения, которой место в одной функции.
                if _is_prepared(node.left, prepared) or isinstance(
                    node.left, ast.Name | ast.Constant
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "проверка вхождения по приведённой строке в обход finlib.utils."
        f"markers_found: {offenders}"
    )
