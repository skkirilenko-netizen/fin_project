"""Тест проводки: правка разбора обязана доходить до экрана разметки.

Правка по графам чужой длительности была сделана и **выглядела** недействующей:
в присесте разметки строка снова показывала «Расход по налогу на прибыль 11
(6 766) (21 930)» с квартальными величинами. Причина оказалась не в правке,
а в том, что присест держал в памяти разбор, загруженный при запуске.

Но проверить это стало нечем: правка проверялась на `extract`, а до разметки
шла через `load_issuer`, и пропусти кто-нибудь `layouts=` в этой связке —
ни один тест не упал бы. Здесь проверяется именно связка.
"""

import ast
from pathlib import Path

from finlib.sources.ifrs_inbox import Rejection
from finlib.sources.ifrs_markup import candidates, load_issuer
from finlib.sources.ifrs_numbers import Grouping

ROOT = Path(__file__).resolve().parent.parent

# Разбор, которому не нужна разметка граф, — объявляется вместе с причиной.
# Умолчание здесь безопасно ровно по одной причине: сравниваются итоги внутри
# одного периода, и при любой графе сходимость та же. Молчаливого исключения
# быть не должно: без записи оно неотличимо от забытого довода.
LAYOUTS_NOT_NEEDED = {
    "src/finlib/sources/ifrs_inbox.py": (
        "разрешение конвенции арифметикой: графы ещё не разобраны, "
        "а сходимость проверяется в пределах одного периода"
    ),
}

# Промежуточный отчёт с четырьмя графами: полугодие и квартал рядом, как
# у ФосАгро. Числа английской конвенцией — при русской «281 388 298 556»
# неразличимо, четыре это величины или две.
DOCUMENT = """
Консолидированный промежуточный сокращенный отчет о прибыли или убытке
за три и шесть месяцев, закончившихся 30 июня 2026 года
(в миллионах российских рублей)
Млн руб. Прим.
Шесть месяцев,
закончившихся
30 июня
Три месяца,
закончившихся
30 июня
2026 2025 2026 2025
Выручка 5 281,388 298,556 149,929 139,166
Себестоимость продаж (194,587) (158,932) (100,381) (77,882)
Валовая прибыль 78,095 126,562 45,683 55,619
Административные расходы 7 (27,303) (21,000) (15,419) (10,719)
Прибыль до налогообложения 25,419 97,472 24,898 37,378
Расход по налогу на прибыль 11 (6,766) (21,930) (6,466) (9,492)
Неведомая статья 1,000 900 500 450

Консолидированный промежуточный сокращенный отчет о финансовом положении
по состоянию на 30 июня 2026 года
(в миллионах российских рублей)
Млн руб. Прим.
30 июня
2026 года
31 декабря
2025 года
Основные средства 12 700,000 650,000
Нематериальные активы 13 3,666 3,657
Итого внеоборотные активы 703,666 653,657
Запасы 16 200,000 180,000
Денежные средства и их эквиваленты 18 96,334 86,343
Итого оборотные активы 296,334 266,343
Итого активы 1,000,000 920,000
"""

PADDING = "\nПримечания к консолидированной финансовой отчётности.\n" * 40


def issuer_of(tmp_path: Path):
    """Комплект, проведённый через приём и разбор ровно так, как в разметке."""
    path = tmp_path / "interim.txt"
    path.write_text(DOCUMENT + PADDING, encoding="utf-8")
    found = load_issuer(path, "7736050003", Grouping.ENGLISH)
    assert not isinstance(found, Rejection), getattr(found, "reason", "")
    return found


def test_markup_sees_the_declared_span(tmp_path: Path) -> None:
    """Разметка получает величины объявленной длительности, а не последние графы."""
    issuer = issuer_of(tmp_path)
    layout = issuer.profile.layout_of("ifrs.statement_of_profit_or_loss")
    assert (layout.total, layout.taken, layout.offset) == (4, 2, 0)

    values = issuer.extraction.totals(issuer.report_date)
    assert values["ifrs.revenue"] == 281388
    assert values["ifrs.income_tax"] == -6766


def test_queue_shows_the_declared_span_too(tmp_path: Path) -> None:
    """В очередь строка идёт с чистым наименованием и своими величинами.

    Прежде наименование кончалось перед **нашей** графой, и величины чужой
    длительности оставались в нём: «Расход по налогу на прибыль 11 (6,766)
    (21,930)» справочник не узнавал вовсе, а в очереди стояли квартальные
    числа.
    """
    issuer = issuer_of(tmp_path)
    queue = candidates([issuer])
    unknown = [item for item in queue if "Неведомая" in item.source_name]
    assert len(unknown) == 1
    row = unknown[0]
    assert row.source_name == "Неведомая статья"
    assert row.values[:2] == (1000, 900)
    # Квартальных величин в очереди нет вовсе: они не наш период.
    assert all(500 not in item.values for item in queue)


def extract_calls() -> list[tuple[str, int, bool]]:
    """Все боевые вызовы `extract` и признак, передана ли разметка граф."""
    found: list[tuple[str, int, bool]] = []
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "eval").rglob("*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "finlib.sources.ifrs_extract"
            and any(alias.name == "extract" for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imported:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "extract"):
                continue
            passed = any(word.arg == "layouts" for word in node.keywords)
            found.append((str(path.relative_to(ROOT)), node.lineno, passed))
    return found


def test_every_caller_passes_the_column_layout() -> None:
    """Разметка граф передаётся во всех вызовах разбора, кроме объявленных.

    Проверка структурная, а не текстовая: ищется довод у самого вызова
    в дереве разбора. Упоминание `layouts` в модуле не означает, что оно
    дошло до вызова, — тот же класс дефекта, против которого заведён реестр
    контролей.

    Сверяется в обе стороны: вызов без разметки обязан стоять в перечне
    объявленных, а объявленный — обязан существовать. Устаревшее объявление
    означает разрешение, выданное вызову, которого больше нет.
    """
    calls = extract_calls()
    assert len(calls) >= 5, calls

    silent = {path for path, _, passed in calls if not passed}
    assert silent == set(LAYOUTS_NOT_NEEDED), (
        f"без разметки граф: {sorted(silent)}, объявлено: "
        f"{sorted(LAYOUTS_NOT_NEEDED)}"
    )
