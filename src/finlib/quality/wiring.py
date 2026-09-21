"""Реестр контролей: где каждый вызывается и вызывается ли вообще.

Третий случай конструкции «написано, но не вызывается» — после `check_text`,
который месяцами не выполнялся в боевом прогоне, и `check_calculated`,
сторожившего разделы, которых модель больше не пишет. Оба раза код был верен,
тесты зелены, а в боевом пути его никто не звал, и нули в замерах читались
как чистый результат.

Память об этом не работает. Поэтому здесь реестр: у каждого контроля
объявлено, из каких модулей он вызывается, а контроль без боевого вызова
числится `not_wired` — с датой заведения, причиной и задачей, в которой
подключается. Тест сверяет реестр с исходниками в обе стороны: действующий
контроль обязан иметь вызов, а числящийся неподключённым обязан его не иметь.
Вторая сторона не менее важна: устаревший `not_wired` — это контроль,
который работает, но которому не верят.
"""

import ast
import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from finlib.quality.codes import CheckCode

logger = logging.getLogger(__name__)


class WiringStatus(StrEnum):
    """Подключён ли контроль к боевому пути."""

    WIRED = "wired"
    NOT_WIRED = "not_wired"


@dataclass(frozen=True, slots=True)
class Wiring:
    """Где контроль вызывается и с какого дня существует."""

    status: WiringStatus
    since: date
    called_from: tuple[str, ...] = ()
    # Для неподключённого: почему вызова ещё нет и где он появится.
    reason: str | None = None
    planned_in: str | None = None

    @property
    def wired(self) -> bool:
        """Короткая форма для отбора."""
        return self.status is WiringStatus.WIRED


# Модули, в которых ищется вызов. Определение кода и сам реестр исключены:
# упоминание контроля в них вызовом не является.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"codes.py", "wiring.py"}


REGISTRY: dict[CheckCode, Wiring] = {
    # --- контроли качества отчётности ---------------------------------------
    CheckCode.BALANCE_EQUALITY: Wiring(
        WiringStatus.WIRED, date(2026, 8, 20), ("quality/checks.py",)
    ),
    CheckCode.SECTION_SUM: Wiring(
        WiringStatus.WIRED, date(2026, 8, 20), ("quality/checks.py",)
    ),
    CheckCode.PROFIT_CHAIN: Wiring(
        WiringStatus.WIRED, date(2026, 8, 20), ("quality/checks.py",)
    ),
    CheckCode.PERIOD_REVISED: Wiring(
        WiringStatus.WIRED, date(2026, 8, 20), ("quality/checks.py",)
    ),
    CheckCode.MANDATORY_FIELDS: Wiring(
        WiringStatus.WIRED, date(2026, 8, 20), ("quality/checks.py",)
    ),
    CheckCode.JUMP_DETECTION: Wiring(
        WiringStatus.WIRED, date(2026, 8, 20), ("quality/checks.py",)
    ),
    CheckCode.RETAINED_EARNINGS_LINK: Wiring(
        WiringStatus.WIRED, date(2026, 8, 20), ("quality/checks.py",)
    ),
    CheckCode.UNIT_NOT_DETERMINED: Wiring(
        WiringStatus.WIRED,
        date(2026, 9, 10),
        ("quality/checks.py", "sources/inbox.py"),
    ),
    CheckCode.BALANCE_MAGNITUDE: Wiring(
        WiringStatus.WIRED, date(2026, 9, 10), ("quality/checks.py",)
    ),
    CheckCode.PERIOD_MAGNITUDE_SHIFT: Wiring(
        WiringStatus.WIRED, date(2026, 9, 10), ("quality/checks.py",)
    ),
    # --- записи получения и загрузки ----------------------------------------
    CheckCode.CREDIT_ORGANIZATION: Wiring(
        WiringStatus.WIRED, date(2026, 8, 25), ("sources/girbo.py",)
    ),
    CheckCode.LINE_NOT_RECOGNIZED: Wiring(
        WiringStatus.WIRED, date(2026, 8, 28), ("normalize/loader.py",)
    ),
    CheckCode.FILE_INN_NOT_DETERMINED: Wiring(
        WiringStatus.WIRED, date(2026, 9, 5), ("sources/inbox.py",)
    ),
    CheckCode.FILE_PERIOD_NOT_DETERMINED: Wiring(
        WiringStatus.WIRED, date(2026, 9, 5), ("sources/inbox.py",)
    ),
    CheckCode.FILE_REPORTING_TYPE_UNKNOWN: Wiring(
        WiringStatus.WIRED, date(2026, 9, 5), ("sources/inbox.py",)
    ),
    CheckCode.FILE_NOT_PARSED: Wiring(
        WiringStatus.WIRED, date(2026, 9, 5), ("sources/inbox.py",)
    ),
    CheckCode.AMBIGUOUS_LINE_CODE: Wiring(
        WiringStatus.WIRED,
        date(2026, 8, 28),
        ("normalize/loader.py", "quality/context.py"),
    ),
    CheckCode.MULTIPLE_SOURCE_CODES: Wiring(
        WiringStatus.WIRED,
        date(2026, 8, 28),
        ("normalize/loader.py", "quality/context.py"),
    ),
    CheckCode.UNKNOWN_LINE_CODE: Wiring(
        WiringStatus.WIRED, date(2026, 8, 28), ("normalize/loader.py",)
    ),
    # Сводка сопоставления пишется обоими загрузчиками, а прогонщик контролей
    # той же записью сообщает, что к комплекту другого стандарта контроли
    # РСБУ не применялись.
    CheckCode.LINE_MAPPING: Wiring(
        WiringStatus.WIRED,
        date(2026, 9, 17),
        ("normalize/loader.py", "normalize/ifrs_loader.py", "quality/runner.py"),
    ),
    CheckCode.PERIOD_VALUE_MISMATCH: Wiring(
        WiringStatus.WIRED,
        date(2026, 8, 28),
        (
            "normalize/loader.py",
            "normalize/ifrs_loader.py",
            "quality/context.py",
            "scoring/engine.py",
        ),
    ),
    # Расхождение знака при равной величине разводится обоими загрузчиками:
    # признак один на два стандарта, и определение у него одно
    # (`quality/values.py::sign_only_difference`).
    CheckCode.SIGN_CONVENTION_MISMATCH: Wiring(
        WiringStatus.WIRED,
        date(2026, 9, 18),
        ("normalize/loader.py", "normalize/ifrs_loader.py"),
    ),
    # Сводка столкновений периодов: знаменатель к правилу приоритета.
    CheckCode.PERIOD_PRIORITY: Wiring(
        WiringStatus.WIRED, date(2026, 9, 18), ("normalize/ifrs_loader.py",)
    ),
    CheckCode.FACT_OVERWRITE: Wiring(
        WiringStatus.WIRED,
        date(2026, 8, 26),
        ("normalize/loader.py", "normalize/ifrs_loader.py"),
    ),
    # --- ветка МСФО ----------------------------------------------------------
    # Определитель конвенции написан и покрыт тестами, но разбора форм, из
    # которого он вызывается, ещё нет: он появляется в задаче 23. Пока запись
    # честно говорит, что контроль не работает, — иначе его отсутствие
    # в журнале читалось бы как «нарушений не найдено».
    # Приём файла МСФО подключён к циклу задачей 23: `pipeline.
    # accept_ifrs_document` проводит документ через определение параметров,
    # извлечение форм и экран сверки. До этого все восемь отказов числились
    # неподключёнными — код был написан, покрыт тестами и никем не вызывался.
    CheckCode.FILE_TEXT_LAYER_MISSING: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_inbox.py",)
    ),
    CheckCode.FILE_NOT_STATEMENTS: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_inbox.py",)
    ),
    CheckCode.FINANCIAL_INSTITUTION: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_inbox.py",)
    ),
    CheckCode.FILE_CURRENCY_NOT_DETERMINED: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_inbox.py",)
    ),
    CheckCode.FILE_CURRENCY_NOT_ROUBLE: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_inbox.py",)
    ),
    CheckCode.FILE_PERIODS_NOT_DETERMINED: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_inbox.py",)
    ),
    CheckCode.DIGIT_GROUPING_NOT_DETERMINED: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_inbox.py",)
    ),
    # Нулевой пункт задачи 23: проверка правдоподобия конвенции вызывается
    # с экрана сверки по разобранным формам — сверять сумму разделов
    # с итогом теперь есть с чем.
    CheckCode.DIGIT_GROUPING_IMPLAUSIBLE: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("sources/ifrs_review.py",)
    ),
    # Длительность граф формы: отказ на приёме, если графы приведены
    # за период иной длительности, чем период комплекта.
    CheckCode.FILE_COLUMN_SPAN_MISMATCH: Wiring(
        WiringStatus.WIRED, date(2026, 9, 20), ("sources/ifrs_inbox.py",)
    ),
    # Отброшенная без объяснения графа: нарушение, а не норма. Считается
    # на экране сверки по разобранным формам, потому что видно её только
    # там — в самих величинах потери не видно вовсе.
    CheckCode.EXTRA_COLUMNS_DROPPED: Wiring(
        WiringStatus.WIRED, date(2026, 9, 20), ("sources/ifrs_review.py",)
    ),
    # --- сведения из аудиторского заключения (задачи 25 и 27) ---------------
    # Подключены задачей 27: заключение читается в цикле приёма документа
    # МСФО, и его сведения идут в журнал комплекта. До этого числились
    # неподключёнными — писать их было некуда, а молчание журнала читалось бы
    # как «оговорок нет».
    #
    # **Достижимость вызова не означает, что довод дошёл.** Реестр считал эти
    # шесть подключёнными с 18.09.2026, и вызов действительно стоял в записи
    # комплекта — но заключение приходило в него названным доводом
    # с умолчанием, а базу наполнял прогон приёма, который его не передавал.
    # На 21.09.2026 в `dq_log` по всем шести ноль записей на 14 комплектов,
    # при том что у ФосАгро мнение с оговоркой. Реестр этого не ловит
    # и поймать не может: довод — не вызов. Сторожит его отдельная проверка
    # строением (`tests/test_ifrs_audit_wiring.py`), а прочитанное в документе
    # стало обязательным позиционным доводом записи.
    **{
        code: Wiring(
            WiringStatus.WIRED, date(2026, 9, 18), ("normalize/ifrs_loader.py",)
        )
        for code in (
            CheckCode.AUDIT_OPINION_MODIFIED,
            CheckCode.AUDIT_GOING_CONCERN,
            CheckCode.AUDIT_STATEMENTS_RESTATED,
            CheckCode.AUDIT_REPORT_NOT_READABLE,
            CheckCode.AUDIT_REPORT_ABSENT,
            CheckCode.AUDIT_REVIEW_ENGAGEMENT,
        )
    },
    # --- основания экрана сверки МСФО (задача 28) ---------------------------
    # Свои коды вместо кодов бухгалтерских контролей: документ по МСФО называл
    # основания наименованиями другого предмета, а два основания делили один код.
    **{
        code: Wiring(
            WiringStatus.WIRED, date(2026, 9, 21), ("sources/ifrs_review.py",)
        )
        for code in (
            CheckCode.IFRS_TOTAL_MISMATCH,
            CheckCode.IFRS_UNRECOGNISED_POSITION,
            CheckCode.IFRS_MATERIAL_ITEM,
            CheckCode.IFRS_REPORTING_KIND,
            CheckCode.IFRS_LOST_PAGE,
        )
    },
    # --- величины примечаний (задача 28) ------------------------------------
    # Величина, объявленная в примечании, идёт в факты, а отказ её извлечения —
    # в журнал: у Норникеля капитализированные проценты раскрыты прозой,
    # и показатель, которому величины не хватило, обязан назвать причину.
    # Рядом стоит сводка — сколько величин взято из скольких объявленных.
    **{
        code: Wiring(
            WiringStatus.WIRED, date(2026, 9, 21), ("normalize/ifrs_loader.py",)
        )
        for code in (
            CheckCode.NOTE_VALUES,
            CheckCode.NOTE_VALUE_NOT_EXTRACTED,
        )
    },
}


def _imports_of(path: Path) -> set[str]:
    """Модули пакета, которые импортирует этот файл."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover — синтаксис ловит линтер
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {item for item in found if item.startswith("finlib.")}


@lru_cache(maxsize=1)
def reachable_modules() -> frozenset[str]:
    """Модули, достижимые от цикла обработки по импортам.

    Упоминания кода мало: третий случай был именно в том, что код написан,
    верен и покрыт тестами, а из цикла не вызывается вовсе. Достижимость
    считается статически от `finlib.pipeline` — точки входа всякой работы
    с данными, включая CLI и регрессионный прогон. Импорты внутри функций
    учитываются наравне с верхними: половина модулей проекта импортируется
    именно так, ради разрыва циклов.
    """
    start = "finlib.pipeline"
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = SOURCE_ROOT.parent / (name.replace(".", "/") + ".py")
        if not path.exists():
            continue
        queue.extend(_imports_of(path) - seen)
    return frozenset(seen)


def module_name(relative: str) -> str:
    """Имя модуля по пути внутри пакета: `quality/checks.py` → `finlib.quality.checks`."""
    return "finlib." + relative.removesuffix(".py").replace("/", ".")


def calls_in_sources(code: CheckCode) -> tuple[str, ...]:
    """Модули пакета, в которых контроль действительно упоминается.

    Упоминание — это `CheckCode.X` либо строковое значение кода в любых
    кавычках: часть контролей называется в SQL-запросах, а там кавычки
    одинарные. Определение кода и сам реестр не в счёт: назвать контроль
    там ещё не значит вызвать его.
    """
    marks = (f"CheckCode.{code.name}", f'"{code.value}"', f"'{code.value}'")
    found: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path.name in EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        if any(mark in text for mark in marks):
            found.append(str(path.relative_to(SOURCE_ROOT)))
    return tuple(found)


def live_calls(code: CheckCode) -> tuple[str, ...]:
    """Места вызова, достижимые от цикла обработки.

    Разница с `calls_in_sources` и есть суть реестра: код, упомянутый
    в модуле, который из цикла не зовётся, в боевом пути не выполняется —
    и его молчание не означает отсутствия нарушений.
    """
    live = reachable_modules()
    return tuple(
        item for item in calls_in_sources(code) if module_name(item) in live
    )


def unwired() -> dict[CheckCode, Wiring]:
    """Контроли, числящиеся неподключёнными, — с причиной и сроком."""
    return {code: item for code, item in REGISTRY.items() if not item.wired}


def summary() -> str:
    """Однострочная сводка для журнала и сводок прогона."""
    total = len(REGISTRY)
    idle = len(unwired())
    return f"контролей в реестре {total}, из них не подключено {idle}"
