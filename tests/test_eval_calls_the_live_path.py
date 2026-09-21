"""Замеры не считают сами: они зовут боевой путь и сравнивают с эталоном.

Трижды за два дня ответ замера был принят за доказательство, а боевой путь
при этом не работал:

- стоп-факторы жили в `eval/ifrs_scoring_run.py` по своему перечню величин,
  а расчёт по фактам звал оценку с пустым перечнем исключённых;
- классы задачи 27 считались там же по своему словарю величин, собранному
  в обход записи фактов;
- тип эмитента ЛСР выходил девелоперским в замере и обычным корпоративным
  в цикле: замер собирал величины сам, без подтверждённого человеком
  опознания.

Причина одна: `eval/` был не наблюдением за системой, а второй системой
рядом. Два пути к одному ответу расходятся, и увидеть это можно только
сравнив два прогона — чего никто не делал.

Отсюда правило: **модуль замера вправе вызывать функции цикла и читать базу,
но не вправе звать примитивы расчёта и опознания.** Проверка структурная —
по дереву разбора, а не по вхождению подстроки: упоминание имени в строке
или комментарии нарушением не является.
"""

import ast

from finlib.config import settings

# **Функции, а не модули.** Перечисление, загрузчик методики и тип данных
# из того же модуля замеру нужны и ничего не считают: `NotCalculableReason`
# описывает причину отказа, `load_metrics` читает методику. Запрещено именно
# то, что **даёт ответ**, потому что второй такой ответ неминуемо разойдётся
# с первым.
FORBIDDEN: dict[str, str] = {
    # Расчёт величин и оценки.
    "evaluate": "вычисление формулы",
    "parse_formula": "разбор формулы",
    "interpolate": "балл по шкале",
    "compute_all": "расчёт показателей",
    "compute_metric": "расчёт показателя",
    "compute_derived": "расчёт производных величин",
    "assess": "балл и класс",
    "evaluate_stop_factors": "стоп-факторы",
    "evaluate_signals": "надзорные признаки",
    "structure_shifts": "структурный сдвиг",
    "revision_intensity": "интенсивность пересмотра",
    "Inputs": "вход расчёта, собранный вручную",
    # Опознание и разбор документа.
    "normalize_name": "приведение наименования — ключ опознания",
    "determine_type": "тип эмитента",
    "identify": "приём документа",
    "extract": "разбор форм",
    "review": "экран сверки",
    "read_audit_report": "чтение аудиторского заключения",
    "read_document": "чтение документа помимо таблиц",
    "note_values": "величины примечаний",
    "accrued_interest": "начисленные проценты из примечаний",
    "load_confirmed": "ранее подтверждённое опознание",
    "load_extraction": "запись комплекта",
    "as_addend": "подстановка нуля в слагаемое",
}

# Замеры, которым разрешено больше, и почему. Перечень пуст намеренно:
# исключение здесь — это возвращённая вторая система, и оно должно стоить
# отдельного решения. Пустой перечень тоже объявление.
ALLOWED: dict[str, set[str]] = {}


def _imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Имена, ввезённые из пакета, вместе со строкой ввоза."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if not (node.module or "").startswith("finlib"):
                continue
            found += [(alias.asname or alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.Import):
            found += [
                (alias.asname or alias.name, node.lineno)
                for alias in node.names
                if alias.name.startswith("finlib")
            ]
    return found


def _attribute_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Обращения вида `модуль.функция(...)`: ввоз модуля имя не прячет.

    Берутся только обращения к **ввезённому модулю**: `intake.review` —
    поле итога цикла, а не вызов экрана сверки, и считать его нарушением
    значило бы запретить читать ответ боевого пути.
    """
    modules = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
        if isinstance(node, ast.Import)
        or (node.module or "").startswith("finlib")
    }
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            found.append((node.attr, node.lineno))
    return found


def test_eval_does_not_compute_by_itself() -> None:
    """Ни один замер не зовёт примитив расчёта или опознания напрямую."""
    offenders: list[str] = []
    for path in sorted((settings.base_dir / "eval").glob("*.py")):
        allowed = ALLOWED.get(path.name, set())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, line in _imported_names(tree) + _attribute_calls(tree):
            if name in FORBIDDEN and name not in allowed:
                offenders.append(f"{path.name}:{line} — {name} ({FORBIDDEN[name]})")
    assert not offenders, (
        "замер считает сам вместо того, чтобы звать боевой путь:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_live_path_is_reachable_from_the_runs() -> None:
    """Прогоны ветки МСФО зовут именно цикл, а не свою последовательность шагов.

    Обратная сторона запрета: перечень запрещённого можно удовлетворить,
    не вызывая ничего вовсе, — и замер тогда мерил бы пустоту. Поэтому
    проверяется и вызов цикла.
    """
    runs = {
        "ifrs_intake_run.py": "accept_ifrs_document",
        "regression_run.py": "analyze",
    }
    for name, entry in runs.items():
        source = (settings.base_dir / "eval" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert entry in called, f"{name} не зовёт {entry}"


def test_the_deleted_second_systems_are_gone() -> None:
    """Замеры, которые были второй системой, удалены, а не оставлены рядом.

    Оставленный модуль однажды запускают снова, и его ответ опять принимают
    за доказательство. Перечень назван поимённо: удалённое нельзя отличить
    от забытого, если о нём не сказано.
    """
    deleted = (
        "ifrs_scoring_run.py",  # свой расчёт показателей, оценки и стоп-факторов
        "ifrs_type_run.py",  # свой словарь величин для типа эмитента
        "ifrs_criteria_run.py",  # свои перечни статей и кодов Cbonds
        "ifrs_notes_run.py",  # свой перечень строк и своё чтение примечаний
        "ifrs_audit_run.py",  # своё чтение аудиторского заключения
    )
    for name in deleted:
        assert not (settings.base_dir / "eval" / name).exists(), name
