"""Сверка пар «число — код» в ответе модели.

Проверки одних чисел недостаточно. Модель может назвать верное число при
чужом коде: подставить значение текущей ликвидности к коэффициенту автономии.
Число совпадёт с входным, а утверждение будет ложным.

Поэтому каждое число в ответе привязывается к ближайшему коду или
наименованию показателя и сверяется со значениями именно этого показателя.
"""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from finlib.utils import to_decimal

logger = logging.getLogger(__name__)

# Насколько далеко от числа ищется код или наименование показателя.
ANCHOR_WINDOW = 160

# Строка блока ДАННЫЕ: код, наименование, значения по периодам.
_DATA_LINE = re.compile(r"^(\d{4})\s{2}(.+?)\s{2}\|\s{2}(.+)$", re.MULTILINE)

# Строка блока ПОКАЗАТЕЛИ: код, наименование в кавычках, значения.
_METRIC_LINE = re.compile(r"^([a-z][a-z0-9_]*)\s{2}«(.+?)»\s{2}(.+)$", re.MULTILINE)

# Строка баллов группы в блоке ОЦЕНКА.
_GROUP_LINE = re.compile(
    r"^\s{2}(.+?):\s([\d,.-]+) из 100, вес в оценке ([\d,.-]+) %", re.MULTILINE
)

# Строка вида «Ключ: значение» — реквизиты организации и сводные величины
# оценки: ИНН, ОГРН, общий балл. Без этого они оставались бы без якоря.
_LABELLED = re.compile(r"^([А-ЯЁA-Z][^:\n]{2,60}):\s*(.+)$", re.MULTILINE)

_NUMBER_IN_VALUES = re.compile(
    r"[-−]?\d{1,3}(?:[    ]\d{3})+(?:[.,]\d+)?|[-−]?\d+(?:[.,]\d+)?"
)


@dataclass(frozen=True, slots=True)
class Anchor:
    """Код или наименование показателя и допустимые при нём значения."""

    key: str
    kind: str
    values: frozenset[Decimal]


@dataclass
class AnchorIndex:
    """Указатель от кодов и наименований к их значениям."""

    anchors: dict[str, Anchor] = field(default_factory=dict)

    def add(self, key: str, kind: str, values: set[Decimal]) -> None:
        """Добавляет якорь; повторный ключ расширяет набор значений."""
        normalized = key.strip().casefold()
        if not normalized:
            return
        existing = self.anchors.get(normalized)
        merged = set(values) | (set(existing.values) if existing else set())
        self.anchors[normalized] = Anchor(normalized, kind, frozenset(merged))

    def get(self, key: str) -> Anchor | None:
        """Якорь по коду или наименованию."""
        return self.anchors.get(key.strip().casefold())

    @property
    def keys(self) -> list[str]:
        """Все известные якоря, длинные первыми: так совпадения точнее."""
        return sorted(self.anchors, key=len, reverse=True)


# Отчётная дата в части со значениями: подпись к величине, а не величина.
# Иначе якорь принимал бы 31.12 и 2025 как допустимые значения показателя.
_DATE_IN_VALUES = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")


def _values_of(text: str) -> set[Decimal]:
    """Числа из части строки со значениями."""
    found = set()
    for match in _NUMBER_IN_VALUES.finditer(_DATE_IN_VALUES.sub(" ", text)):
        value = to_decimal(match.group())
        if value is not None:
            found.add(value)
    return found


def build_index(blocks: str) -> AnchorIndex:
    """Строит указатель «код или наименование → допустимые значения» из блоков."""
    index = AnchorIndex()
    for code, name, values in _DATA_LINE.findall(blocks):
        parsed = _values_of(values)
        index.add(code, "line", parsed)
        index.add(name, "line_name", parsed)
    for code, name, values in _METRIC_LINE.findall(blocks):
        parsed = _values_of(values)
        index.add(code, "metric", parsed)
        index.add(name, "metric_name", parsed)
    for name, score, weight in _GROUP_LINE.findall(blocks):
        index.add(name, "group", _values_of(f"{score} {weight}"))
    for label, value in _LABELLED.findall(blocks):
        index.add(label, "label", _values_of(value))
    return index


def find_anchor(text: str, position: int, index: AnchorIndex) -> Anchor | None:
    """Ближайший к числу код или наименование в пределах окна.

    Ищется и слева, и справа: модель пишет как «строка 1600 — 25 736 328 136»,
    так и «25 736 328 136 тыс. руб. (строка 1600)». Побеждает ближайший.
    """
    start = max(0, position - ANCHOR_WINDOW)
    end = min(len(text), position + ANCHOR_WINDOW)
    window = text[start:end].casefold()
    offset = position - start

    before: tuple[int, Anchor] | None = None
    after: tuple[int, Anchor] | None = None
    for key in index.keys:
        search_from = 0
        while True:
            found = window.find(key, search_from)
            if found < 0:
                break
            search_from = found + 1
            if _overlaps_number(window, found, len(key)):
                continue
            anchor = index.get(key)
            if anchor is None:
                continue
            end_of_key = found + len(key)
            if end_of_key <= offset:
                distance = offset - end_of_key
                if before is None or distance < before[0]:
                    before = (distance, anchor)
            elif found >= offset:
                distance = found - offset
                if after is None or distance < after[0]:
                    after = (distance, anchor)

    # Предшествующий якорь важнее последующего: по требуемому формату код
    # ставится перед значениями — «показатель (код) вырос с A до B».
    # Иначе второе число перечисления привязывалось бы к следующему показателю.
    return (before or after or (0, None))[1]


def _overlaps_number(window: str, position: int, length: int) -> bool:
    """Не является ли найденный «код» частью другого числа."""
    before = window[position - 1] if position > 0 else " "
    after = window[position + length] if position + length < len(window) else " "
    return before.isdigit() or after.isdigit()
