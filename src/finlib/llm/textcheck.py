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
from typing import TYPE_CHECKING

from finlib.llm.cleanup import has_identifiers
from finlib.llm.direction import Direction, mentions_direction
from finlib.metrics.display import round_to

if TYPE_CHECKING:
    from finlib.report.policy import Questions

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
    FACT_BASE_INCOMPLETE = "fact_base_incomplete"
    QUESTION_COUNT = "question_count"
    DAYS_DIRECTION = "days_direction"
    FLAG_CONFLICT_NOT_STATED = "flag_conflict_not_stated"
    QUESTION_DUPLICATE = "question_duplicate"
    QUESTION_ABOUT_DISCLOSURE = "question_about_disclosure"
    RISK_WITHOUT_VALUE = "risk_without_value"
    THESIS_NOT_QUOTED = "thesis_not_quoted"
    GROUP_HEADING_MISSING = "group_heading_missing"


SEVERITY: dict[TextRule, Severity] = {
    TextRule.TECHNICAL_IDENTIFIER: Severity.BLOCKING,
    TextRule.CLASS_STATED_BOTH_WAYS: Severity.BLOCKING,
    TextRule.DELTA_MISMATCH: Severity.BLOCKING,
    TextRule.TEMPLATE_NOT_APPLICABLE: Severity.BLOCKING,
    TextRule.FREE_INTERPRETATION: Severity.BLOCKING,
    TextRule.QUESTION_OUT_OF_FORM_SET: Severity.BLOCKING,
    # Состав фактической базы задан методикой и передан модели перечнем:
    # пропуск обязательной величины — не вкусовое расхождение, а раздел,
    # на который последующие опираться не могут.
    TextRule.FACT_BASE_INCOMPLETE: Severity.BLOCKING,
    TextRule.QUESTION_COUNT: Severity.BLOCKING,
    # Тезис предписан методикой и приводится дословно. Пересказ и объединение
    # тезисов молча искажают содержание: модель слила три отказа расчёта
    # в один и сложила вместе строки, которых не хватило разным показателям, —
    # получилось утверждение, которого расчёт не делал. Числа при этом верны,
    # и проверка чисел такое пропускает.
    TextRule.THESIS_NOT_QUOTED: Severity.BLOCKING,
    # Наименование группы открывает абзац и относит стоящие в нём утверждения
    # к группе показателей — той самой, по которой раскладывается балл.
    # Сравнение документов 17.09.2026 показало, что этой разметки в тексте
    # модели нет ни у одной из четырёх организаций: инструкция прямо запрещала
    # переносить наименования групп, и модель ей следовала. Запрет снят,
    # а требование стало проверяемым: тезисы сохранялись дословно, и
    # thesis_not_quoted пропускал потерю структуры целиком.
    TextRule.GROUP_HEADING_MISSING: Severity.BLOCKING,
    TextRule.DAYS_DIRECTION: Severity.WARNING,
    TextRule.FLAG_CONFLICT_NOT_STATED: Severity.WARNING,
    # Дубль вопроса и вопрос о нераскрытии портят перечень, но документу
    # не мешают: отклонять из-за них верное во всём остальном заключение
    # дороже, чем оставить замечание в журнале и в повторной попытке.
    TextRule.QUESTION_DUPLICATE: Severity.WARNING,
    TextRule.QUESTION_ABOUT_DISCLOSURE: Severity.WARNING,
    TextRule.RISK_WITHOUT_VALUE: Severity.WARNING,
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
    # Коды величин, обязательных в разделе «Фактическая база».
    fact_base: tuple[str, ...] = ()
    # Правила вопросов к организации: сколько их и в каком порядке основания.
    questions: "Questions | None" = None
    # Предписанные тезисы, которые модель обязана привести дословно.
    # Пустой перечень означает, что схема тезисов не применялась.
    theses: tuple[str, ...] = ()
    # Наименования групп показателей, по которым у организации есть тезисы.
    # Каждое обязано открывать свой абзац раздела 3 — в обоих режимах сборки.
    thesis_groups: tuple[str, ...] = ()


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

# Номера разделов, к которым привязаны правила. Заданы здесь, а не числами
# по месту: разделы перечислены в prompts/conclusion.md, и расхождение
# с ними должно быть видно в одном месте.
FACT_BASE_SECTION = 2
RISKS_SECTION = 4
QUESTIONS_SECTION = 6

# Вопрос: всё до знака вопроса, начиная с предыдущего.
_QUESTION = re.compile(r"[^?\n]+\?")

# Вопрос о нераскрытии: ответа по существу у него нет.
_NOT_DISCLOSED = re.compile(
    r"не\s+раскрыт\w*|отсутств\w+\s+раскрыт\w*|почему\s+.{0,40}не\s+указан\w*",
    re.IGNORECASE,
)

_HAS_DIGIT = re.compile(r"\d")

# Строка раздела о рисках короче этой длины тезисом не считается: заголовок
# или связка числа не требуют.
RISK_PARAGRAPH_MIN = 80

# Длина основы слова при сравнении вопросов: окончания отбрасываются.
QUESTION_STEM = 5

# Сколько первых знаков абзаца считается его началом. Наименование группы
# стоит здесь: либо первым словом, либо в связке «переходя к…». Дальше идут
# сами утверждения, и слова наименования встречаются в них постоянно.
GROUP_HEADING_WINDOW = 80

# Слова, по которым вопросы неразличимы.
_STOP_WORDS = frozenset(
    {
        "какие",
        "каким",
        "какой",
        "чего",
        "чем",
        "что",
        "этой",
        "этого",
        "организации",
        "организация",
        "период",
        "периода",
        "отчётности",
        "отчетности",
    }
)


def check_text(
    sections: dict[int, str],
    context: TextContext,
    raw_sections: dict[int, str] | None = None,
) -> list[TextIssue]:
    """Проверяет разделы заключения по всем правилам.

    **Разделы передаются очищенными** — такими, какими их увидит читатель.
    Очистку делает вызывающий (`verify`), а не эта функция: иначе правило
    «технических идентификаторов нет» проверяло бы собственную очистку
    и не могло сработать никогда. Здесь оно сторожит именно то, что очистка
    выполнена, — забытый вызов виден сразу.

    sections — номер раздела и его текст. Правила, привязанные к разделу,
    проверяются только в нём: вопрос о строке вне набора форм плох именно
    в «Вопросах к организации», а не всюду.

    raw_sections — те же разделы до снятия разметки. Нужны одному правилу:
    состав «Фактической базы» задан кодами, а в очищенном тексте кодов
    показателей уже нет. Остальные правила работают по очищенному тексту.

    Согласованность состава отчётности между «Ограничениями» и «Происхождением
    документа» проверяется не здесь, а в `report/consistency.py`: она о данных
    документа, а не о тексте модели, и нужна в обоих режимах сборки.
    """
    whole = "\n".join(sections.values())
    marked = raw_sections if raw_sections is not None else sections
    found: list[TextIssue] = []
    found += _no_identifiers(sections)
    found += _class_is_stated_once(sections)
    found += _deltas_match(whole)
    found += _templates_are_applicable(whole, context)
    found += _no_free_interpretation(whole, context)
    found += _questions_stay_in_the_form_set(sections, context)
    found += _fact_base_is_complete(marked, context)
    found += _questions_are_sound(sections, context)
    found += _risks_name_values(sections)
    found += _days_direction(whole, context)
    found += _flag_conflict_is_stated(whole, context)
    found += _theses_are_quoted(marked, context)
    found += _groups_are_headed(sections, context)
    return found


def _groups_are_headed(
    sections: dict[int, str], context: TextContext
) -> list[TextIssue]:
    """Каждая группа показателей открывает свой абзац наименованием.

    Раздел 3 разложен по группам показателей — тем же, по которым считается
    балл, — и наименование группы в начале абзаца это отношение называет.
    Без него перечень утверждений остаётся верным, а деление на группы
    пропадает: читатель не видит, о ликвидности идёт речь или о структуре
    капитала, и раздел перестаёт соответствовать разложению балла
    в приложении.

    Наименование ищется в начале абзаца, а не где угодно в разделе: слова
    наименования стоят и в самих утверждениях — «коэффициент текущей
    ликвидности» содержит «ликвидность», — и проверка по всему тексту
    пропускала бы ровно тот случай, ради которого написана.

    Сравниваются основы слов, а не точное написание: русские наименования
    склоняются, и «Структура капитала» приходит как «к структуре капитала».
    Расчёт ставит наименование первым словом абзаца, модель вправе ввести
    его оборотом — засчитывается и то и другое.
    """
    if not context.thesis_groups:
        return []
    text = sections.get(THESES_SECTION, "")
    if not text:
        return []
    openings = [
        _stems(paragraph[:GROUP_HEADING_WINDOW])
        for paragraph in text.split("\n")
        if paragraph.strip()
    ]
    missing = [
        name
        for name in context.thesis_groups
        if not any(_stems(name) <= opening for opening in openings)
    ]
    if not missing:
        return []
    return [
        TextIssue(
            TextRule.GROUP_HEADING_MISSING,
            f"группы показателей не названы в разделе «Аналитическая "
            f"интерпретация»: {', '.join(missing)}",
            context=missing[0],
        )
    ]


# Раздел, который собирается из предписанных тезисов.
THESES_SECTION = 3

# Обычный пробельный набор — без неразрывного пробела, которым разделены
# разряды числа: `\s` захватил бы и его, и сравнение тезиса с текстом
# перестало бы быть посимвольным там, где оно нужнее всего.
_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")


def _theses_are_quoted(
    sections: dict[int, str], context: TextContext
) -> list[TextIssue]:
    """Каждый предписанный тезис приведён дословно.

    Проверка состава утверждений, а не чисел: утверждение, не выводимое
    из переданных тезисов, отклоняется. Числа при этом могут быть верны —
    и были. Модель слила три отказа расчёта в один и сложила вместе строки,
    которых не хватило разным показателям: «не рассчитаны ликвидность,
    оборотный капитал и обеспеченность собственными средствами: не раскрыты
    строки 1550 и 1170», тогда как 1170 не хватило только третьему. Расчёт
    такого утверждения не делал, а проверка чисел его пропустила: коды строк
    в прозе числами не считаются.

    Сравнивается размеченный текст: коды показателей — часть тезиса.
    Первая буква сравнивается без регистра — после связки она строчная.
    """
    if not context.theses:
        return []
    text = _WHITESPACE.sub(" ", sections.get(THESES_SECTION, ""))
    missing = [
        item
        for item in context.theses
        if _WHITESPACE.sub(" ", item)[1:] not in text
    ]
    if not missing:
        return []
    return [
        TextIssue(
            TextRule.THESIS_NOT_QUOTED,
            f"предписанных тезисов приведено не дословно: {len(missing)} "
            f"из {len(context.theses)}",
            context=missing[0][:160],
        )
    ]


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
        raw = delta.group(1).strip()
        # Четырёхзначное целое в скобках — код строки отчётности, а не
        # величина изменения: «(строка 1300)» дельтой не является.
        if _LINE_CODE.fullmatch(raw.replace("\u00a0", "").replace(" ", "")):
            continue
        declared = _number(raw)
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


def _fact_base_is_complete(
    sections: dict[int, str], context: TextContext
) -> list[TextIssue]:
    """Раздел «Фактическая база» называет все обязательные величины.

    Состав раздела задан методикой и передан модели перечнем: валюта баланса,
    собственный капитал, выручка, финансовый результат, совокупный и чистый
    долг, чистый оборотный капитал. Прежде раздел по ПАО «Газпром» не содержал
    ни долга, ни выручки, зато содержал сведения о нераскрытии одной строки,
    и разделы, на этих величинах построенные, опирались на не сказанное.

    Смотрит в размеченный текст: коды показателей снимает очистка, и в том,
    что увидит читатель, искать их поздно.
    """
    if not context.fact_base:
        return []
    text = sections.get(FACT_BASE_SECTION, "")
    if not text:
        return []
    missing = [code for code in context.fact_base if code not in text]
    if not missing:
        return []
    return [
        TextIssue(
            TextRule.FACT_BASE_INCOMPLETE,
            f"в разделе «Фактическая база» не названы обязательные величины: "
            f"{', '.join(missing)}",
        )
    ]


def _questions_are_sound(
    sections: dict[int, str], context: TextContext
) -> list[TextIssue]:
    """Вопросов столько, сколько задано методикой, и они не повторяются.

    Вопрос о том, почему не раскрыта строка, содержательного ответа не имеет:
    в упрощённой форме строки нет вовсе, а в полной нераскрытие само по себе
    правомерно. Спрашивать нужно по существу обстоятельства.
    """
    policy = context.questions
    text = sections.get(QUESTIONS_SECTION, "")
    if policy is None or not text:
        return []

    questions = [item.strip() for item in _QUESTION.findall(text) if item.strip()]
    found: list[TextIssue] = []
    if not policy.min_count <= len(questions) <= policy.max_count:
        found.append(
            TextIssue(
                TextRule.QUESTION_COUNT,
                f"вопросов к организации {len(questions)}, а методика требует "
                f"от {policy.min_count} до {policy.max_count}",
            )
        )

    seen: dict[str, str] = {}
    for question in questions:
        key = _question_key(question)
        if key and key in seen:
            found.append(
                TextIssue(
                    TextRule.QUESTION_DUPLICATE,
                    "вопросы повторяют друг друга",
                    context=question[:120],
                )
            )
            break
        seen[key] = question

    about_disclosure = [item for item in questions if _NOT_DISCLOSED.search(item)]
    if about_disclosure:
        found.append(
            TextIssue(
                TextRule.QUESTION_ABOUT_DISCLOSURE,
                "вопрос о том, почему строка не раскрыта, содержательного "
                "ответа не имеет: спрашивать нужно о существе обстоятельства",
                context=about_disclosure[0][:120],
            )
        )
    return found


def _risks_name_values(sections: dict[int, str]) -> list[TextIssue]:
    """Каждый тезис раздела о рисках привязан к величине.

    Раздел называется «Риски и надзорные сигналы», и утверждение без числа
    в нём неотличимо от общего рассуждения: проверить его нечем.
    """
    text = sections.get(RISKS_SECTION, "")
    if not text:
        return []
    loose = [
        line.strip()
        for line in text.split("\n")
        if len(line.strip()) >= RISK_PARAGRAPH_MIN and not _HAS_DIGIT.search(line)
    ]
    if not loose:
        return []
    return [
        TextIssue(
            TextRule.RISK_WITHOUT_VALUE,
            f"в разделе о рисках {len(loose)} утверждений не опираются "
            f"ни на одну величину",
            context=loose[0][:120],
        )
    ]


def _stems(text: str) -> frozenset[str]:
    """Основы значимых слов: окончания русских наименований отбрасываются."""
    return frozenset(
        word[:QUESTION_STEM]
        for word in re.findall(r"[а-яёa-z]{4,}", text.casefold())
    )


def _question_key(text: str) -> str:
    """Огрублённый вид вопроса для поиска дублей.

    Сравниваются значимые слова без окончаний: «чем объясняется рост
    задолженности» и «чем объясняется рост задолженностей» — один вопрос.
    """
    words = sorted(
        {
            word[:QUESTION_STEM]
            for word in re.findall(r"[а-яёa-z]{4,}", text.casefold())
            if word not in _STOP_WORDS
        }
    )
    return " ".join(words)


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
