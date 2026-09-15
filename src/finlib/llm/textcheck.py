"""Контроль утверждений сгенерированного текста.

Постпроверка чисел (`verify.py`) отвечает на вопрос «откуда взялось число».
Здесь проверяется другое: не противоречит ли текст сам себе и расчёту.
Экспертная оценка трёх пилотных документов дала шесть методологических
ошибок интерпретации при чистом расчётном ядре — все в свободном тексте.

Блокирующее нарушение отменяет ответ целиком; предупреждение пишется
в журнал и в замечания повторной попытки, но документу не мешает.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from finlib.llm.cleanup import has_identifiers
from finlib.llm.direction import Direction, mentions_direction
from finlib.metrics.display import round_to

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    """Последствие нарушения для документа."""

    BLOCKING = "blocking"
    WARNING = "warning"


class TextRule(StrEnum):
    """Правила контроля текста; значение совпадает с кодом в журнале."""

    TECHNICAL_IDENTIFIER = "technical_identifier"
    CLASS_STATED_BOTH_WAYS = "class_stated_both_ways"
    DELTA_MISMATCH = "delta_mismatch"
    TEMPLATE_NOT_APPLICABLE = "template_not_applicable"
    FREE_INTERPRETATION = "free_interpretation"
    QUESTION_OUT_OF_FORM_SET = "question_out_of_form_set"
    DAYS_DIRECTION = "days_direction"
    FLAG_CONFLICT_NOT_STATED = "flag_conflict_not_stated"


SEVERITY: dict[TextRule, Severity] = {
    TextRule.TECHNICAL_IDENTIFIER: Severity.BLOCKING,
    TextRule.CLASS_STATED_BOTH_WAYS: Severity.BLOCKING,
    TextRule.DELTA_MISMATCH: Severity.BLOCKING,
    TextRule.TEMPLATE_NOT_APPLICABLE: Severity.BLOCKING,
    TextRule.FREE_INTERPRETATION: Severity.BLOCKING,
    TextRule.QUESTION_OUT_OF_FORM_SET: Severity.BLOCKING,
    TextRule.DAYS_DIRECTION: Severity.WARNING,
    TextRule.FLAG_CONFLICT_NOT_STATED: Severity.WARNING,
}


@dataclass(frozen=True, slots=True)
class TextIssue:
    """Нарушение правила с местом, где оно найдено."""

    rule: TextRule
    message: str
    context: str = ""

    @property
    def severity(self) -> Severity:
        """Блокирует ли нарушение документ."""
        return SEVERITY[self.rule]

    @property
    def blocking(self) -> bool:
        """Короткая форма для отбора."""
        return self.severity is Severity.BLOCKING

    def describe(self) -> str:
        """Человеческое объяснение для журнала и повторной попытки."""
        return self.message


@dataclass
class TextContext:
    """Что нужно знать о расчёте, чтобы проверить утверждения текста.

    Собирается из тех же данных, что и документ: проверка обязана опираться
    на расчёт, а не на то, как текст выглядит.
    """

    # Строки, предусмотренные применённым набором форм.
    known_lines: frozenset[str] = frozenset()
    # Показатели, у которых отрицательный знаменатель отменил расчёт.
    refused_metrics: dict[str, str] = field(default_factory=dict)
    # Показатели, измеряемые в днях: у них «рост» и «ускорение» расходятся.
    days_metrics: frozenset[str] = frozenset()
    # Шаблонные блоки, условие применения которых не выполнено: текст блока
    # и причина, по которой он появиться не вправе.
    forbidden_templates: dict[str, str] = field(default_factory=dict)
    # Конфликт флага и стоп-фактора, который обязан быть зафиксирован.
    flag_conflict: str | None = None


_CLASS_ASSIGNED = re.compile(r"\bкласс\w*\s*[«\"'(]?\s*([A-E])\b", re.IGNORECASE)
_CLASS_REFUSED = re.compile(r"класс\w*\s+не\s+присво\w+", re.IGNORECASE)

# «снизился с 0,41 до 0,31» — пара уровней в одном утверждении.
_FROM_TO = re.compile(
    r"\bс\s+(-?\d[\d\s ]*(?:[.,]\d+)?)\s+до\s+(-?\d[\d\s ]*(?:[.,]\d+)?)",
    re.IGNORECASE,
)

# Абсолютное изменение, названное рядом: «изменение 0,11», «(-0,06, ...)».
# Процент сюда не попадает: «сократилась на 59,6 %» — темп, а не разность
# уровней, и сравнивать его с разностью нельзя. Поэтому за числом
# не должно стоять «%».
_DELTA = re.compile(
    r"(?:изменени\w+|составил\w*|\()\s*(-?\d[\d\s\u00a0]*(?:[.,]\d+)?)(?!\s*%)",
    re.IGNORECASE,
)

# Насколько далеко после пары уровней искать величину изменения.
DELTA_WINDOW = 40

# Ссылка на строку отчётности в тексте вопроса.
_LINE_REFERENCE = re.compile(r"\bстрок\w*\s*[(\[]?\s*(\d{4})(?:\s*(?:,|и|или)\s*(\d{4}))*")
_LINE_CODE = re.compile(r"\b(\d{4})\b")

# Номер года: код строки отчётности начинается с 1, 2, 4 и годом быть не может.
YEAR_MIN, YEAR_MAX = 1900, 2100

# Ускорение и замедление: для показателя в днях они противоположны росту.
# Истолкование величины: глагол оценки рядом с наименованием показателя.
_INTERPRETS = re.compile(
    r"указыва\w+|свидетельств\w+|означа\w+|говорит\s+о|отража\w+|"
    r"подтвержда\w+|характеризу\w+|демонстрир\w+",
    re.IGNORECASE,
)

# Насколько далеко после наименования искать истолкование.
INTERPRETATION_WINDOW = 160

_FASTER = re.compile(r"ускор\w+|быстрее", re.IGNORECASE)
_SLOWER = re.compile(r"замедл\w+|медленнее", re.IGNORECASE)


def check_text(sections: dict[int, str], context: TextContext) -> list[TextIssue]:
    """Проверяет разделы заключения по всем правилам.

    **Разделы передаются очищенными** — такими, какими их увидит читатель.
    Очистку делает вызывающий (`verify`), а не эта функция: иначе правило
    «технических идентификаторов нет» проверяло бы собственную очистку
    и не могло сработать никогда. Здесь оно сторожит именно то, что очистка
    выполнена, — забытый вызов виден сразу.

    sections — номер раздела и его текст. Правила, привязанные к разделу,
    проверяются только в нём: вопрос о строке вне набора форм плох именно
    в «Вопросах к организации», а не всюду.

    Согласованность состава отчётности между «Ограничениями» и «Происхождением
    документа» проверяется не здесь, а в `report/consistency.py`: она о данных
    документа, а не о тексте модели, и нужна даже при `--no-llm`.
    """
    whole = "\n".join(sections.values())
    found: list[TextIssue] = []
    found += _no_identifiers(sections)
    found += _class_is_stated_once(sections)
    found += _deltas_match(whole)
    found += _templates_are_applicable(whole, context)
    found += _no_free_interpretation(whole, context)
    found += _questions_stay_in_the_form_set(sections, context)
    found += _days_direction(whole, context)
    found += _flag_conflict_is_stated(whole, context)
    return found


def _no_identifiers(sections: dict[int, str]) -> list[TextIssue]:
    """Технических идентификаторов в разделах заключения быть не должно.

    Код — механизм постпроверки, а не часть текста. Снимает его
    `cleanup.strip_identifiers`; здесь проверяется, что снятие выполнено
    и ничего не пропустило.
    """
    found: list[TextIssue] = []
    for number, text in sorted(sections.items()):
        leftovers = has_identifiers(text)
        if leftovers:
            found.append(
                TextIssue(
                    TextRule.TECHNICAL_IDENTIFIER,
                    f"раздел {number}: технические идентификаторы в тексте "
                    f"({', '.join(leftovers[:5])})",
                )
            )
    return found


def _class_is_stated_once(sections: dict[int, str]) -> list[TextIssue]:
    """Класс либо присвоен, либо нет — не оба утверждения разом."""
    text = sections.get(1, "") or "\n".join(sections.values())
    assigned = _CLASS_ASSIGNED.search(text)
    refused = _CLASS_REFUSED.search(text)
    if assigned and refused:
        return [
            TextIssue(
                TextRule.CLASS_STATED_BOTH_WAYS,
                f"в тексте одновременно назван класс {assigned.group(1)} "
                f"и сказано, что класс не присвоен",
                context=_around(text, assigned.start()),
            )
        ]
    return []


def _deltas_match(text: str) -> list[TextIssue]:
    """Заявленное изменение равно разности приведённых уровней.

    Проверяется утверждение, а не данные: расчёт уже согласован единой точкой
    округления, но модель вправе назвать уровни и дельту, между собой
    не согласованные.
    """
    found: list[TextIssue] = []
    for match in _FROM_TO.finditer(text):
        first, second = _number(match.group(1)), _number(match.group(2))
        if first is None or second is None:
            continue
        tail = text[match.end() : match.end() + DELTA_WINDOW]
        delta = _DELTA.search(tail)
        if delta is None:
            continue
        declared = _number(delta.group(1))
        if declared is None:
            continue
        scale = max(_places(match.group(1)), _places(match.group(2)))
        expected = round_to(second - first, scale)
        if round_to(abs(declared), scale) != abs(expected):
            found.append(
                TextIssue(
                    TextRule.DELTA_MISMATCH,
                    f"заявленное изменение {delta.group(1)} не равно разности "
                    f"приведённых уровней ({expected})",
                    context=_around(text, match.start()),
                )
            )
    return found


def _templates_are_applicable(text: str, context: TextContext) -> list[TextIssue]:
    """Шаблонный блок не выводится при невыполнении условия применения."""
    found: list[TextIssue] = []
    for fragment, reason in context.forbidden_templates.items():
        if fragment and fragment in text:
            found.append(
                TextIssue(
                    TextRule.TEMPLATE_NOT_APPLICABLE,
                    f"приведён шаблонный блок, условие которого не выполнено: {reason}",
                    context=fragment[:80],
                )
            )
    return found


def _no_free_interpretation(text: str, context: TextContext) -> list[TextIssue]:
    """При отменённом знаменателе интерпретация берётся только из справочника.

    Показатель, расчёт которого отменён отрицательным знаменателем,
    интерпретации не имеет: «меньше — лучше» превращается в похвалу
    за катастрофу. Модель такую ошибку уже допускала.
    """
    found: list[TextIssue] = []
    for code, name in context.refused_metrics.items():
        for match in re.finditer(re.escape(name), text, re.IGNORECASE):
            # Смотреть нужно вперёд: «Финансовый рычаг заметно вырос» —
            # глагол стоит после наименования, а не перед ним.
            window = text[match.start() : match.end() + INTERPRETATION_WINDOW]
            if (
                mentions_direction(window) is None
                and _INTERPRETS.search(window) is None
            ):
                continue
            found.append(
                TextIssue(
                    TextRule.FREE_INTERPRETATION,
                    f"«{name}» истолкован свободно, хотя расчёт отменён "
                    f"отрицательным знаменателем (код {code})",
                    context=_around(text, match.start()),
                )
            )
            break
    return found


def _questions_stay_in_the_form_set(
    sections: dict[int, str], context: TextContext
) -> list[TextIssue]:
    """Вопрос о строке, не предусмотренной набором форм, ответа не имеет."""
    questions = sections.get(6, "")
    if not questions or not context.known_lines:
        return []
    found: list[TextIssue] = []
    unknown: set[str] = set()
    for match in _LINE_REFERENCE.finditer(questions):
        span = questions[match.start() : match.end() + 40]
        for code in _LINE_CODE.findall(span):
            if YEAR_MIN <= int(code) <= YEAR_MAX:
                continue  # «за 2024 год» — не код строки
            if code not in context.known_lines:
                unknown.add(code)
    if unknown:
        listed = ", ".join(sorted(unknown))
        found.append(
            TextIssue(
                TextRule.QUESTION_OUT_OF_FORM_SET,
                f"вопросы адресованы строкам вне применённого набора форм: {listed}",
            )
        )
    return found


def _days_direction(text: str, context: TextContext) -> list[TextIssue]:
    """«Снижение» показателя в днях означает ускорение, а не замедление.

    В тексте по Газпрому: «снижение оборачиваемости дебиторской задолженности
    до 124,2 дня, несмотря на ускорение» — сокращение периода оборота и есть
    ускорение, формулировка внутренне противоречива.
    """
    found: list[TextIssue] = []
    for name in context.days_metrics:
        for match in re.finditer(re.escape(name), text, re.IGNORECASE):
            window = text[match.start() : match.end() + INTERPRETATION_WINDOW]
            direction = mentions_direction(window)
            if direction is None:
                continue
            speeds_up = _FASTER.search(window) is not None
            slows_down = _SLOWER.search(window) is not None
            if direction is Direction.DECLINE and slows_down:
                message = "сокращение периода оборота означает ускорение, а не замедление"
            elif direction is Direction.GROWTH and speeds_up:
                message = "рост периода оборота означает замедление, а не ускорение"
            else:
                continue
            found.append(
                TextIssue(
                    TextRule.DAYS_DIRECTION,
                    f"«{name}»: {message}",
                    context=_around(text, match.start()),
                )
            )
            break
    return found


def _flag_conflict_is_stated(text: str, context: TextContext) -> list[TextIssue]:
    """Конфликт флага и стоп-фактора зафиксирован в тексте, если возник."""
    if context.flag_conflict is None:
        return []
    if context.flag_conflict.casefold() in text.casefold():
        return []
    return [
        TextIssue(
            TextRule.FLAG_CONFLICT_NOT_STATED,
            "конфликт флага и стоп-фактора в тексте не зафиксирован",
        )
    ]


def _number(raw: str) -> Decimal | None:
    """Разбирает число русского написания."""
    from finlib.utils import to_decimal

    return to_decimal(raw)


def _places(raw: str) -> int:
    """Знаков после запятой в написании числа."""
    cleaned = raw.replace(",", ".")
    return len(cleaned.split(".")[1]) if "." in cleaned else 0


def _around(text: str, position: int, width: int = 60) -> str:
    """Окружение места, где найдено нарушение."""
    start = max(0, position - width)
    end = min(len(text), position + width)
    return " ".join(text[start:end].split())


def blocking(issues: Sequence[TextIssue]) -> list[TextIssue]:
    """Только блокирующие нарушения."""
    return [item for item in issues if item.blocking]
