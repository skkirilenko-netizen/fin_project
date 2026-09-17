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

import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
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
    CheckCode.LINE_MAPPING: Wiring(
        WiringStatus.WIRED, date(2026, 9, 17), ("normalize/loader.py",)
    ),
    CheckCode.PERIOD_VALUE_MISMATCH: Wiring(
        WiringStatus.WIRED,
        date(2026, 8, 28),
        ("normalize/loader.py", "quality/context.py", "scoring/engine.py"),
    ),
    CheckCode.FACT_OVERWRITE: Wiring(
        WiringStatus.WIRED, date(2026, 8, 26), ("normalize/loader.py",)
    ),
    # --- ветка МСФО ----------------------------------------------------------
    # Определитель конвенции написан и покрыт тестами, но разбора форм, из
    # которого он вызывается, ещё нет: он появляется в задаче 23. Пока запись
    # честно говорит, что контроль не работает, — иначе его отсутствие
    # в журнале читалось бы как «нарушений не найдено».
    CheckCode.DIGIT_GROUPING_NOT_DETERMINED: Wiring(
        WiringStatus.NOT_WIRED,
        date(2026, 9, 17),
        reason=(
            "определитель конвенции готов, но разбора форм МСФО, из которого "
            "он вызывается, ещё нет"
        ),
        planned_in="задача 22: приём файла и определение параметров",
    ),
    CheckCode.DIGIT_GROUPING_IMPLAUSIBLE: Wiring(
        WiringStatus.NOT_WIRED,
        date(2026, 9, 17),
        reason=(
            "проверка правдоподобия конвенции требует разобранных форм: "
            "сверять сумму разделов с итогом пока не с чем"
        ),
        planned_in="задача 23: подключить контроль правдоподобия к разбору форм",
    ),
}


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


def unwired() -> dict[CheckCode, Wiring]:
    """Контроли, числящиеся неподключёнными, — с причиной и сроком."""
    return {code: item for code, item in REGISTRY.items() if not item.wired}


def summary() -> str:
    """Однострочная сводка для журнала и сводок прогона."""
    total = len(REGISTRY)
    idle = len(unwired())
    return f"контролей в реестре {total}, из них не подключено {idle}"
