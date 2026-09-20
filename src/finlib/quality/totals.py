"""Сходимость итога с суммой его состава — арифметика, общая для всех наборов.

Контроль сходимости проверяет равенство суммы, а не природу кодов: ему
безразлично, четырёхзначный ли это код строки РСБУ или позиция `ifrs.*`
унифицированной модели. Поэтому арифметика живёт здесь и работает с любым
справочником, у которого у итоговой строки есть состав с операторами.

Здесь же обе трактовки нераскрытого значения, и путать их нельзя. Для
**проверки арифметики итога** нераскрытая строка считается нулём — иначе
контроль разваливается на любой упрощённой форме, где организация раскрывает
три строки из семи. Для **расчёта коэффициентов** отсутствие остаётся NULL,
и подстановка запрещена; `metrics/` этот модуль не импортирует, что
проверяется тестом.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from finlib.normalize.lines import Operator
from finlib.quality.values import as_addend

logger = logging.getLogger(__name__)


class Addend(Protocol):
    """Слагаемое итога: код и оператор вхождения."""

    @property
    def code(self) -> str: ...

    @property
    def op(self) -> Operator: ...


class Total(Protocol):
    """Итоговая строка любого справочника: свой код и состав."""

    @property
    def code(self) -> str: ...

    @property
    def components(self) -> Sequence[Addend]: ...


class TotalVerdict(StrEnum):
    """Исход сверки итога с суммой состава.

    Четыре исхода контроля различаются здесь же: «не выполнялся» и
    «не проверяемо» — разные вещи, и первое не наша вина, а второе наша.
    """

    MATCHED = "matched"
    MISMATCHED = "mismatched"
    # Итог не раскрыт либо не раскрыто ни одно слагаемое: проверять нечего.
    NOTHING_TO_CHECK = "nothing_to_check"
    # Слагаемое не загружено, потому что мы отказались угадывать его
    # принадлежность: это наш пробел, а не дефект отчётности.
    NOT_VERIFIABLE = "not_verifiable"


@dataclass(frozen=True, slots=True)
class TotalCheck:
    """Результат сверки одного итога."""

    verdict: TotalVerdict
    code: str
    reason: str = ""
    total: Decimal | None = None
    computed: Decimal | None = None
    difference: Decimal | None = None
    tolerance: Decimal | None = None
    undisclosed: tuple[str, ...] = ()
    blocked: dict[str, str] = field(default_factory=dict)
    components: tuple[str, ...] = ()

    @property
    def details(self) -> dict[str, object]:
        """Подробности для журнала качества."""
        if self.total is None:
            return {}
        return {
            "total": str(self.total),
            "computed": str(self.computed),
            "difference": str(self.difference),
            "tolerance": str(self.tolerance),
            "components": list(self.components),
            "undisclosed_components": list(self.undisclosed),
        }


@dataclass(frozen=True, slots=True)
class Composition:
    """Итог и один из его составов — вход для арифметики сходимости.

    Нужна там, где состав берётся не из самой позиции: запасной состав
    при разметке и тождество распределения на экране сверки. Определение
    одно на оба случая: два одинаковых переходника неминуемо разойдутся.
    """

    code: str
    components: tuple

    @property
    def is_total(self) -> bool:
        """Состав бывает только у итога."""
        return True


def _with_inferred_signs(
    line: Total,
    value_of: Callable[[str], Decimal | None],
    blocked_reason: Callable[[str], str | None],
    normal_sign_of: Callable[[str], int],
) -> tuple[Decimal, list[str]]:
    """Сумма состава с выведенным знаком у вычитаемых статей.

    Правится знак только там, где он противоречит нормальному: статья
    с `normal_sign = −1`, пришедшая положительной. Перебирать знаки у всех
    слагаемых нельзя — так сумма подберётся к любому итогу, и проверка
    перестанет быть проверкой.
    """
    total = Decimal(0)
    fixed: list[str] = []
    for component in line.components:
        if blocked_reason(component.code) is not None:
            continue
        value = as_addend(value_of(component.code))
        if normal_sign_of(component.code) < 0 and value > 0:
            value = -value
            fixed.append(component.code)
        total += value if component.op is Operator.PLUS else -value
    return total, fixed


def check_total(
    line: Total,
    value_of: Callable[[str], Decimal | None],
    blocked_reason: Callable[[str], str | None],
    tolerance_of: Callable[[Decimal], Decimal],
    normal_sign_of: Callable[[str], int] | None = None,
) -> TotalCheck:
    """Сверяет итог с суммой его состава.

    value_of отдаёт величину по коду (None — не раскрыта), blocked_reason —
    причину, по которой код не загружен вовсе (None — загружен), tolerance_of
    считает допуск на округление от величины итога. Ни один из трёх не знает
    о природе кодов, и справочник сюда не передаётся.
    """
    listed = tuple(f"{item.op.value}{item.code}" for item in line.components)

    blocked_total = blocked_reason(line.code)
    if blocked_total is not None:
        return TotalCheck(
            TotalVerdict.NOT_VERIFIABLE, line.code, blocked_total, components=listed
        )

    total = value_of(line.code)
    if total is None:
        return TotalCheck(
            TotalVerdict.NOTHING_TO_CHECK,
            line.code,
            f"итог {line.code} не раскрыт",
            components=listed,
        )

    computed = Decimal(0)
    undisclosed: list[str] = []
    blocked: dict[str, str] = {}
    for component in line.components:
        reason = blocked_reason(component.code)
        if reason is not None:
            blocked[component.code] = reason
            continue
        value = value_of(component.code)
        if value is None:
            undisclosed.append(component.code)
        # Единственное место, где нераскрытое значение становится нулём,
        # — и пользуются им только контроли сходимости.
        amount = as_addend(value)
        computed += amount if component.op is Operator.PLUS else -amount

    if blocked:
        reasons = "; ".join(f"{code} — {reason}" for code, reason in sorted(blocked.items()))
        return TotalCheck(
            TotalVerdict.NOT_VERIFIABLE,
            line.code,
            f"слагаемые итога {line.code} не загружены ({reasons}), "
            "сумму проверить нельзя",
            blocked=blocked,
            components=listed,
        )

    if len(undisclosed) == len(line.components):
        return TotalCheck(
            TotalVerdict.NOTHING_TO_CHECK,
            line.code,
            "ни одно слагаемое не раскрыто",
            components=listed,
        )

    difference = computed - total
    tolerance = tolerance_of(total)

    if abs(difference) > tolerance and normal_sign_of is not None:
        # Эмитент может печатать расход без скобок, и тогда знак величины
        # приходится выводить арифметикой: вычитаемая статья, пришедшая
        # положительной, пробуется с обратным знаком. Пробуется именно она,
        # а не любая: перебор всех знаков подобрал бы сумму к чему угодно.
        corrected, fixed = _with_inferred_signs(
            line, value_of, blocked_reason, normal_sign_of
        )
        if fixed and abs(corrected - total) <= tolerance:
            logger.info(
                "итог %s сошёлся после вывода знака у %s",
                line.code,
                ", ".join(fixed),
            )
            computed, difference = corrected, corrected - total

    verdict = (
        TotalVerdict.MATCHED if abs(difference) <= tolerance else TotalVerdict.MISMATCHED
    )
    return TotalCheck(
        verdict,
        line.code,
        total=total,
        computed=computed,
        difference=difference,
        tolerance=tolerance,
        undisclosed=tuple(undisclosed),
        components=listed,
    )
