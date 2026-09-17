"""Постпроверка ответа модели на посторонние числа.

Инвариант 3: каждое число в заключении привязано к коду строки или показателя.
Модель не вычисляет (инвариант 1), поэтому любое число в её ответе обязано
встречаться во входных блоках. Число, которого там нет, — признак того, что
модель посчитала сама или выдумала, и такой ответ пользователю не показывается.
"""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from finlib.llm.claims import FalseClaim, find_false_claims
from finlib.llm.cleanup import strip_identifiers
from finlib.llm.direction import agrees, stated_direction
from finlib.llm.pairs import (
    LINE_REFERENCE,
    Anchor,
    AnchorIndex,
    build_index,
    find_anchor,
)
from finlib.llm.textcheck import TextContext, TextIssue, blocking, check_text
from finlib.llm.verdict import VerdictClaim, find_verdict_claims, parse_verdict
from finlib.llm.wording import Wording, find_forbidden
from finlib.utils import to_decimal

logger = logging.getLogger(__name__)

# Числа, разрешённые без привязки к входным данным: они не несут
# содержательной информации об организации.
ALWAYS_ALLOWED: frozenset[Decimal] = frozenset(
    {Decimal(0), Decimal(1), Decimal(100)}
)

# Диапазон, в котором число считается номером года.
YEAR_MIN, YEAR_MAX = 1990, 2100

# Число с русским оформлением: разряды пробелами, запятая как десятичный знак.
_NUMBER = re.compile(
    r"[-−]?\d{1,3}(?:[    ]\d{3})+(?:[.,]\d+)?"  # с разделителями разрядов
    r"|[-−]?\d+(?:[.,]\d+)?"  # без них
)

# Номер пункта списка или раздела: число в начале строки перед точкой или
# скобкой, в том числе после решёток заголовка Markdown.
_LIST_ITEM = re.compile(r"^[\s#>*-]*(\d{1,2})[.)]\s", re.MULTILINE)

# Ссылка на раздел заключения: «см. раздел 5». Номером величины не является.
_SECTION_REFERENCE = re.compile(r"\bраздел\w*\s+\d{1,2}", re.IGNORECASE)

# Рассуждение модели: в заключение не идёт и в проверке не участвует.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Дата: числом отчётности не является, разбирать её на части нельзя.
_DATE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")

# Сравнение перед числом: «ниже 1,0», «не достигает 100 %», «против 0».
# После такого слова число перестаёт быть проходным: 0, 1 и 100 сами по себе
# ничего не утверждают, но как объект сравнения превращаются в порог,
# а абсолютных порогов методика не содержит.
_COMPARISON = re.compile(
    r"(?:ниже|выше|меньше|больше|менее|более|превыша\w*|превыси\w*|достига\w*"
    r"|порог\w*|против|сравнени\w*\s+с)\W{0,3}$",
    re.IGNORECASE,
)

# Насколько далеко назад смотреть в поисках слова сравнения.
_COMPARISON_LOOKBEHIND = 24

# Код формы по ОКУД: ссылка на форму, а не величина.
_FORM_CODE = re.compile(r"\b0\d{6}\b")

# Реквизит нормативного документа: «приказ № 84н», «приложение 5 к приказу
# 66н». Величиной отчётности не является. Формулировка приходит из наших же
# блоков — наименование набора форм содержит ссылку на приказ 66н.
_DOC_NUMBER = re.compile(
    r"№\s?\d+[а-яёa-z]?"
    r"|приказ\w*\s+(?:от\s+[\d.]+\s+)?(?:№\s*)?\d+[а-яё]?"
    r"|приложени\w+\s+\d+",
    re.IGNORECASE,
)

# Ссылка на строку отчётности: «по строкам 1210, 1410, 1510». Это перечень
# кодов, а не величин, и код может быть любым — в том числе отсутствующим
# в блоках: именно об отсутствии строки модель и говорит.
#
# Определение живёт в pairs.py и берётся оттуда: разбор пары «число — код»
# идёт двумя модулями, и одинаковое понимание того, что такое ссылка
# на строку, у них обязано быть общим, а не совпадающим по случайности.
_LINE_REFERENCE = LINE_REFERENCE


class Violation(StrEnum):
    """Чем именно плохо число в ответе."""

    NOT_IN_BLOCKS = "not_in_blocks"
    NO_ANCHOR = "no_anchor"
    WRONG_ANCHOR = "wrong_anchor"
    WRONG_DIRECTION = "wrong_direction"


@dataclass(frozen=True, slots=True)
class ForeignNumber:
    """Число из ответа, не прошедшее проверку."""

    text: str
    value: Decimal
    context: str
    violation: Violation = Violation.NOT_IN_BLOCKS
    anchor: str | None = None
    # Значение из блоков, с которым число сошлось, — со знаком. Нужно для
    # wrong_direction: модель вправе назвать величину изменения по модулю,
    # и направление видно только по знаку входного значения.
    actual: Decimal | None = None

    def describe(self) -> str:
        """Человеческое объяснение, чем число плохо."""
        if self.violation is Violation.NO_ANCHOR:
            return f"{self.text} — приведено без кода показателя"
        if self.violation is Violation.WRONG_ANCHOR:
            return f"{self.text} — не является значением «{self.anchor}»"
        if self.violation is Violation.WRONG_DIRECTION:
            actual = self.actual if self.actual is not None else self.value
            movement = "рост" if actual > 0 else "снижение"
            return (
                f"{self.text} — направление названо неверно: «{self.anchor}» "
                f"означает {movement}"
            )
        return f"{self.text} — отсутствует во входных данных"


@dataclass
class VerificationResult:
    """Итог постпроверки: числа, формулировки и утверждения о состоянии."""

    verified: bool
    foreign: list[ForeignNumber] = field(default_factory=list)
    checked: int = 0
    wordings: list[Wording] = field(default_factory=list)
    claims: list[FalseClaim] = field(default_factory=list)
    verdicts: list[VerdictClaim] = field(default_factory=list)
    statements: list[TextIssue] = field(default_factory=list)

    @property
    def foreign_values(self) -> list[str]:
        """Посторонние числа строками — для записи в журнал."""
        return [item.text for item in self.foreign]

    @property
    def problems(self) -> list[str]:
        """Все замечания одним перечнем, человеческими формулировками."""
        return [
            item.describe()
            for item in (
                *self.foreign,
                *self.wordings,
                *self.claims,
                *self.verdicts,
                *self.statements,
            )
        ]

    def summary(self) -> str:
        """Однострочная сводка."""
        if self.verified:
            return f"проверка пройдена, сверено чисел: {self.checked}"
        parts = [f"посторонних чисел {len(self.foreign)} из {self.checked}"]
        if self.wordings:
            parts.append(f"отсылок к нормативу {len(self.wordings)}")
        if self.claims:
            parts.append(f"ложных утверждений о нерасчёте {len(self.claims)}")
        if self.verdicts:
            parts.append(f"расхождений с оценкой {len(self.verdicts)}")
        if self.statements:
            blocked = sum(1 for item in self.statements if item.blocking)
            parts.append(
                f"нарушений в утверждениях {len(self.statements)} "
                f"(блокирующих {blocked})"
            )
        return f"проверка не пройдена: {'; '.join(parts)}"


def strip_reasoning(text: str) -> str:
    """Убирает блок рассуждения модели.

    Рассуждающие модели выводят черновик в <think>. В заключение он не идёт,
    и в постпроверке не участвует: иначе числа из черновика считались бы
    посторонними, а ход мысли попал бы в документ.
    """
    return _THINK.sub("", text).strip()


def extract_numbers(text: str) -> list[tuple[str, Decimal]]:
    """Все числа текста в исходном написании и в виде Decimal."""
    found: list[tuple[str, Decimal]] = []
    for match in _NUMBER.finditer(text):
        value = to_decimal(match.group())
        if value is not None:
            found.append((match.group(), value))
    return found


def _list_item_spans(text: str) -> list[tuple[int, int]]:
    """Позиции номеров пунктов списка."""
    return [match.span(1) for match in _LIST_ITEM.finditer(text)]


def _decimal_places(text: str) -> int:
    """Сколько знаков после запятой в написании числа."""
    cleaned = text.replace(",", ".")
    return len(cleaned.split(".")[1]) if "." in cleaned else 0


def _rounded_forms(values: set[Decimal]) -> dict[int, set[Decimal]]:
    """Входные числа, округлённые до каждой встречающейся разрядности.

    Модель вправе процитировать значение с меньшей точностью, чем оно дано.
    Округление до той же разрядности — не вычисление, а цитирование; при этом
    перевод единиц или иная арифметика совпадения не дадут.
    """
    forms: dict[int, set[Decimal]] = {}
    for places in range(0, 7):
        quant = Decimal(1).scaleb(-places)
        forms[places] = {value.quantize(quant) for value in values}
    return forms


def allowed_values(blocks: str) -> set[Decimal]:
    """Числа, которые модели позволено называть, — из входных блоков."""
    return {value for _, value in extract_numbers(blocks)}


# Блоки готовых формулировок. Их текст модель обязана привести дословно
# и сокращать не вправе, а кодов в нём нет и быть не может: доля активов
# в тексте флага — часть фразы, а не значение показателя.
_READY_BLOCKS = ("ФЛАГИ", "ОГРАНИЧЕНИЯ")


def quotable_values(blocks: str) -> set[Decimal]:
    """Числа из готовых формулировок: их цитирование кода не требует."""
    found: set[Decimal] = set()
    for section in blocks.split("=== "):
        header = section.split("\n", 1)[0]
        if any(name in header for name in _READY_BLOCKS):
            found.update(value for _, value in extract_numbers(section))
    return found


def verify(
    response: str,
    blocks: str,
    *,
    require_anchor: bool = True,
    thresholds: dict[str, frozenset[Decimal]] | None = None,
    text_context: TextContext | None = None,
) -> VerificationResult:
    """Сверяет пары «число — код» в ответе с входными блоками.

    Число обязано не только встречаться во входных данных, но и стоять при том
    показателе, которому принадлежит: верное значение при чужом коде — ложное
    утверждение, а не опечатка. При require_anchor число без кода рядом тоже
    считается нарушением: проверить его не с чем.

    В thresholds передаются пороги стоп-факторов по коду показателя —
    единственные числа-ориентиры, объявленные методикой. Называть их разрешено,
    но только при своём показателе; всякий другой порог модель выдумала.
    """
    thresholds = thresholds or {}
    text = strip_reasoning(response)
    allowed = allowed_values(blocks)
    quotable = quotable_values(blocks)
    rounded = _rounded_forms(allowed)
    index = build_index(blocks)
    skip = (
        _list_item_spans(text)
        + [match.span() for match in _DATE.finditer(text)]
        + [match.span() for match in _FORM_CODE.finditer(text)]
        + [match.span() for match in _DOC_NUMBER.finditer(text)]
        + [match.span() for match in _LINE_REFERENCE.finditer(text)]
        + [match.span() for match in _SECTION_REFERENCE.finditer(text)]
    )

    foreign: list[ForeignNumber] = []
    checked = 0
    for match in _NUMBER.finditer(text):
        span = match.span()
        if any(start <= span[0] and span[1] <= end for start, end in skip):
            continue  # номер пункта списка
        value = to_decimal(match.group())
        if value is None:
            continue
        if index.get(match.group()) is not None:
            continue  # это сам код строки, ссылка на показатель, а не величина
        if text[span[1] : span[1] + 1] == "_":
            continue  # начало кода производной величины: 1230 в 1230_chg_pct
        checked += 1
        if _is_year(value):
            continue
        # 0, 1 и 100 сами по себе ничего не утверждают, но как объект
        # сравнения превращаются в порог: «что ниже 1,0». Год под это правило
        # не подпадает — «по сравнению с 2024 годом» сравнивает периоды.
        trivial = abs(value) in ALWAYS_ALLOWED
        compared = trivial and _is_compared(text, span)
        if trivial and not compared:
            continue
        if value in quotable:
            continue  # число из готовой формулировки, приведённой дословно

        # Якорь ищется первым: он задаёт, с чем именно сверять число.
        # Общий набор чисел блоков — запасная проверка для числа без якоря.
        anchor = find_anchor(text, span, index)
        if anchor is not None:
            actual = _matched_value(value, match.group(), anchor, exact=compared)
            if actual is None:
                # Порог стоп-фактора при своём показателе назвать разрешено:
                # методика объявляет его прямо, и он содержателен вне отрасли.
                if value in thresholds.get(anchor.key, frozenset()):
                    continue
                violation = (
                    Violation.WRONG_ANCHOR
                    if _is_allowed(value, match.group(), allowed, rounded)
                    else Violation.NOT_IN_BLOCKS
                )
                foreign.append(_foreign(text, match, value, violation, anchor.key))
                continue
            if _misstates_direction(text, span, actual, anchor, index):
                foreign.append(
                    _foreign(
                        text,
                        match,
                        value,
                        Violation.WRONG_DIRECTION,
                        anchor.key,
                        actual,
                    )
                )
            continue

        if not _is_allowed(value, match.group(), allowed, rounded):
            foreign.append(_foreign(text, match, value, Violation.NOT_IN_BLOCKS))
        elif require_anchor:
            foreign.append(_foreign(text, match, value, Violation.NO_ANCHOR))

    wordings = find_forbidden(text)
    claims = find_false_claims(text, index, index.calculated_codes())
    verdicts = find_verdict_claims(text, parse_verdict(blocks))
    # Правила текста применяются к очищенному тексту — тому, что увидит
    # читатель. Коды к этому моменту свою работу сделали: пара «число — код»
    # уже сверена выше.
    raw_sections = sections_of(text)
    statements = (
        check_text(
            {number: strip_identifiers(body) for number, body in raw_sections.items()},
            text_context,
            # Правило о составе «Фактической базы» смотрит в размеченный текст:
            # коды показателей — механизм проверки, и очистка их уже сняла.
            raw_sections=raw_sections,
        )
        if text_context is not None
        else []
    )
    # Предупреждение попадает в журнал и в замечания повторной попытки,
    # но ответ не отменяет: блокирует только блокирующее.
    result = VerificationResult(
        verified=not (
            foreign or wordings or claims or verdicts or blocking(statements)
        ),
        foreign=foreign,
        checked=checked,
        wordings=wordings,
        claims=claims,
        verdicts=verdicts,
        statements=statements,
    )
    logger.info("постпроверка: %s", result.summary())
    return result


def classify_numbers(response: str, blocks: str) -> dict[str, int]:
    """Раскладка всех чисел ответа по тому, как их видит постпроверка.

    Диагностика, а не проверка: показывает, сколько чисел вообще подлежит
    сверке и что отсеяно как дата, код или номер пункта. По ней видно,
    выросло ли покрытие или просто изменился текст.
    """
    text = strip_reasoning(response)
    index = build_index(blocks)
    skips = {
        "номер пункта или раздела": _list_item_spans(text),
        "дата": [match.span() for match in _DATE.finditer(text)],
        "код формы по ОКУД": [match.span() for match in _FORM_CODE.finditer(text)],
        "реквизит документа": [match.span() for match in _DOC_NUMBER.finditer(text)],
        "ссылка на строку": [match.span() for match in _LINE_REFERENCE.finditer(text)],
        "ссылка на раздел": [
            match.span() for match in _SECTION_REFERENCE.finditer(text)
        ],
    }
    counts = dict.fromkeys(skips, 0)
    counts["сам код строки или показателя"] = 0
    counts["тривиальное (0, 1, 100, год)"] = 0
    counts["подлежит сверке по паре «число + код»"] = 0

    for match in _NUMBER.finditer(text):
        span = match.span()
        hit = next(
            (
                name
                for name, spans in skips.items()
                if any(start <= span[0] and span[1] <= end for start, end in spans)
            ),
            None,
        )
        if hit:
            counts[hit] += 1
            continue
        if index.get(match.group()) is not None or text[span[1] : span[1] + 1] == "_":
            counts["сам код строки или показателя"] += 1
            continue
        value = to_decimal(match.group())
        if value is not None and (_is_year(value) or abs(value) in ALWAYS_ALLOWED):
            counts["тривиальное (0, 1, 100, год)"] += 1
            continue
        counts["подлежит сверке по паре «число + код»"] += 1
    return counts


def _foreign(
    text: str,
    match: re.Match[str],
    value: Decimal,
    violation: Violation,
    anchor: str | None = None,
    actual: Decimal | None = None,
) -> ForeignNumber:
    """Собирает запись о непрошедшем числе."""
    return ForeignNumber(
        text=match.group(),
        value=value,
        context=_context_of(text, match.span()),
        violation=violation,
        anchor=anchor,
        actual=actual,
    )


def _matched_value(
    value: Decimal, text: str, anchor: Anchor, *, exact: bool = False
) -> Decimal | None:
    """Значение якоря, с которым сошлось число; None — если не сошлось ни с одним.

    У изменения за период направление задаёт глагол, а не знак: «сократилась
    на 59,6 %» — правильный русский, «сократилась на −59,6 %» — нет. Поэтому
    изменение узнаётся и по модулю, но возвращается всегда со знаком: знак
    нужен, чтобы проверить глагол.

    При exact послабление на округление не действует. Оно нужно для цитирования
    с меньшей точностью, но у тривиального числа в роли порога вырождается:
    0,82, округлённое до нуля знаков, равно единице, и «ликвидность ниже 1»
    сошлось бы со значением самой ликвидности.
    """
    places = _decimal_places(text)
    quant = Decimal(1).scaleb(-places)
    for item in anchor.values:
        if value == item:
            return item
        if not exact and value == item.quantize(quant):
            return item
        if anchor.is_change:
            if value == -item:
                return item
            if not exact and value == (-item).quantize(quant):
                return item
    return None


def _misstates_direction(
    text: str, span: tuple[int, int], actual: Decimal, anchor: Anchor, index: AnchorIndex
) -> bool:
    """Назван ли при изменении глагол, противоречащий его знаку.

    Проверяется только изменение за период — у него знак и есть направление.
    Отрицательная база проверку отменяет: у убытка и отрицательного капитала
    «рост убытка» означает падение самого показателя, и судить по глаголу
    нельзя.
    """
    if not anchor.is_change:
        return False
    base = anchor.base
    if base is not None:
        source = index.get(base)
        if source is not None and any(item < 0 for item in source.values):
            return False
    return not agrees(actual, stated_direction(text, span[0]))


def _is_compared(text: str, span: tuple[int, int]) -> bool:
    """Стоит ли число объектом сравнения — «ниже 1,0», «против 0»."""
    start = max(0, span[0] - _COMPARISON_LOOKBEHIND)
    return _COMPARISON.search(text[start : span[0]]) is not None


def _is_year(value: Decimal) -> bool:
    """Номер года: величиной отчётности не является ни при каких условиях."""
    return value == value.to_integral_value() and YEAR_MIN <= int(value) <= YEAR_MAX


def _is_allowed(
    value: Decimal, text: str, allowed: set[Decimal], rounded: dict[int, set[Decimal]]
) -> bool:
    """Встречается ли число во входных блоках хотя бы где-нибудь."""
    if value in allowed:
        return True
    places = _decimal_places(text)
    return value in rounded.get(places, set())


def _context_of(text: str, span: tuple[int, int], width: int = 40) -> str:
    """Окружение числа — чтобы в журнале было видно, где оно появилось."""
    start = max(0, span[0] - width)
    end = min(len(text), span[1] + width)
    return " ".join(text[start:end].split())


# Заголовок раздела заключения: «### 3. Аналитическая интерпретация».
_SECTION = re.compile(r"^#{1,6}\s*(\d)\.\s*.+?$", re.MULTILINE)


def sections_of(text: str) -> dict[int, str]:
    """Разбивает ответ на разделы по их номерам.

    Правила, привязанные к разделу, проверяются только в нём. Берутся лишь
    разделы, которые пишет модель: остальные собирает расчёт, и написанное
    моделью на их месте в документ не попадает вовсе — проверять там нечего,
    а правило состава сработало бы на тексте, который никто не прочтёт.
    """
    from finlib.report.sections import EXPECTED

    matches = list(_SECTION.finditer(text))
    if not matches:
        return {0: text}
    written = {number for number, _ in EXPECTED}
    found: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        number = int(match.group(1))
        if number in written:
            found[number] = text[match.end() : end]
    return found
