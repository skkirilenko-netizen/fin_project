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

# Изменение за период: у него направление задаётся глаголом, а не знаком.
CHANGE_SUFFIXES: tuple[str, ...] = ("_chg_abs", "_chg_pct")

# Насколько далеко от числа ищется код или наименование показателя.
ANCHOR_WINDOW = 160

# Строка блока ДАННЫЕ: код, наименование, значения по периодам.
_DATA_LINE = re.compile(r"^(\d{4})\s{2}(.+?)\s{2}\|\s{2}(.+)$", re.MULTILINE)

# Строка блока ПОКАЗАТЕЛИ: код, наименование в кавычках, значения.
# Код производной величины начинается с цифры (1230_chg_pct), поэтому
# первый символ не обязан быть буквой; от строки блока ДАННЫЕ такую строку
# отличают кавычки и отсутствие разделителя «|» после наименования.
_METRIC_LINE = re.compile(r"^([a-z0-9][a-z0-9_]*)\s{2}«(.+?)»\s{2}(.+)$", re.MULTILINE)

# Строка баллов группы в блоке ОЦЕНКА.
_GROUP_LINE = re.compile(
    r"^\s{2}(.+?):\s([\d,.-]+) из 100, вес в оценке ([\d,.-]+) %", re.MULTILINE
)

# Строка вида «Ключ: значение» — реквизиты организации и сводные величины
# оценки: ИНН, ОГРН, общий балл. Без этого они оставались бы без якоря.
_LABELLED = re.compile(r"^([А-ЯЁA-Z][^:\n]{2,60}):\s*(.+)$", re.MULTILINE)

# Сокращение в скобках внутри ярлыка: «Основной вид деятельности (ОКВЭД)».
_ABBREVIATION = re.compile(r"\(([А-ЯЁA-Z]{2,10})\)")

# Ссылка на строку отчётности в прозе: «по строке 2120», «по строкам 1410, 1510».
# Число в ней — код, а не величина.
#
# Определение одно на два модуля намеренно. `verify.py` исключает такие
# упоминания из проверки чисел, `pairs.py` — из числа претендентов на тег,
# и расхождение между ними уже стоило ложных отказов: разбор шёл двумя
# модулями, и один не знал того, что отбросил другой.
LINE_REFERENCE = re.compile(
    r"\bстрок\w*\s*[(\[]?\s*\d{4}(?:\s*(?:,|и|или)\s*\d{4})*", re.IGNORECASE
)

_NUMBER_IN_VALUES = re.compile(
    r"[-−]?\d{1,3}(?:[    ]\d{3})+(?:[.,]\d+)?|[-−]?\d+(?:[.,]\d+)?"
)


@dataclass(frozen=True, slots=True)
class Anchor:
    """Код или наименование показателя и допустимые при нём значения.

    Значения хранятся со знаком, как они даны в блоках. Для изменения за
    период знак важен дважды: он допускает цитирование по модулю («сократилась
    на 59,6 %») и он же задаёт направление, с которым обязан быть согласован
    глагол при числе.
    """

    key: str
    kind: str
    values: frozenset[Decimal]
    is_change: bool = False

    @property
    def base(self) -> str | None:
        """Код величины, изменением которой якорь является."""
        if not self.is_change:
            return None
        for suffix in CHANGE_SUFFIXES:
            if self.key.endswith(suffix):
                return self.key[: -len(suffix)]
        return None


@dataclass
class AnchorIndex:
    """Указатель от кодов и наименований к их значениям."""

    anchors: dict[str, Anchor] = field(default_factory=dict)

    def add(
        self, key: str, kind: str, values: set[Decimal], *, is_change: bool = False
    ) -> None:
        """Добавляет якорь; повторный ключ расширяет набор значений."""
        normalized = key.strip().casefold()
        if not normalized:
            return
        existing = self.anchors.get(normalized)
        merged = set(values) | (set(existing.values) if existing else set())
        self.anchors[normalized] = Anchor(
            normalized,
            kind,
            frozenset(merged),
            is_change or (existing.is_change if existing else False),
        )

    def get(self, key: str) -> Anchor | None:
        """Якорь по коду или наименованию."""
        return self.anchors.get(key.strip().casefold())

    def calculated_codes(self) -> set[str]:
        """Коды показателей, у которых во входных блоках есть значение.

        Нерассчитанный показатель попадает в блок перечнем причин, а не строкой
        со значениями, и якоря не порождает — поэтому сюда не попадает.

        Строки отчётности сюда намеренно не входят: раскрытие у строки своё
        в каждом периоде, и «строка 1120 за 2025 год не раскрыта» — верное
        утверждение даже тогда, когда за 2024 год значение есть.
        """
        return {
            key
            for key, anchor in self.anchors.items()
            if anchor.kind == "metric" and anchor.values
        }

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
        is_change = code.endswith(CHANGE_SUFFIXES)
        index.add(code, "metric", parsed, is_change=is_change)
        index.add(name, "metric_name", parsed, is_change=is_change)
    for name, score, weight in _GROUP_LINE.findall(blocks):
        index.add(name, "group", _values_of(f"{score} {weight}"))
    for label, value in _LABELLED.findall(blocks):
        values = _values_of(value)
        index.add(label, "label", values)
        # Сокращение в скобках — такой же якорь, как и сам ярлык: блок даёт
        # «Основной вид деятельности (ОКВЭД): 70.22», а модель пишет
        # «ОКВЭД 70.22», и без этого якоря число привязывалось к соседнему
        # ярлыку — к ОГРН — и объявлялось чужим.
        short = _ABBREVIATION.search(label)
        if short is not None:
            index.add(short.group(1), "label", values)
    return index


def find_anchor(text: str, span: tuple[int, int], index: AnchorIndex) -> Anchor | None:
    """Код или наименование, к которому относится число.

    Приоритет такой. Код, приписанный к самому числу справа — «на 1,6 %
    (1600_chg_pct)», между числом и кодом только знаки и единицы измерения, —
    побеждает всегда: он приписан именно к этому числу. Иначе выигрывает
    ближайший код слева: по формату код ставится перед значениями
    («показатель (код) вырос с A до B»), и без этого правила второе число
    перечисления привязывалось бы к следующему показателю. Код справа,
    отделённый словами, берётся последним — когда слева нет ничего.
    """
    start = max(0, span[0] - ANCHOR_WINDOW)
    end = min(len(text), span[1] + ANCHOR_WINDOW)
    window = text[start:end].casefold()
    number_start, number_end = span[0] - start, span[1] - start

    before: tuple[int, Anchor] | None = None
    after: tuple[int, Anchor] | None = None
    attached: tuple[int, Anchor] | None = None
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
            tag = _is_tag(window, found, len(key))
            if end_of_key <= number_start:
                if tag and _binds_to_number_before(window, found):
                    continue  # тег принадлежит числу, к которому приписан
                distance = number_start - end_of_key
                if before is None or distance < before[0]:
                    before = (distance, anchor)
            elif found >= number_end:
                distance = found - number_end
                if after is None or distance < after[0]:
                    after = (distance, anchor)
                closes = window[end_of_key : end_of_key + 1] in (")", "]")
                if _is_attached(window[number_end:found], closes=closes) and (
                    tag or closes
                ) and (attached is None or distance < attached[0]):
                    attached = (distance, anchor)

    return (attached or before or after or (0, None))[1]


# Единицы измерения между числом и кодом: они часть самого числа, а не текст.
_UNITS = re.compile(
    r"тыс\.?|млн|млрд|руб\.?|дн(?:я|ей|ями|ях)?\.?|проц\w*|п\.\s?п\.|раз(?:а|ы)?",
    re.IGNORECASE,
)

# Любая буква — кириллица или латиница; цифры и знаки препинания не буквы.
_LETTER = re.compile(r"[^\W\d_]")


# Длиннее этого промежуток уже не «приписка к числу». Хватает на « тыс. руб. (».
MAX_ATTACHED_GAP = 16


# Хвост перед скобкой, который не считается словом: знаки и единицы измерения.
_TAIL = re.compile(
    r"(?:тыс\.?|млн|млрд|руб\.?|дн(?:я|ей|ями|ях)?\.?|[\s.,;:%()\[\]«»„“”—–\-])+$",
    re.IGNORECASE,
)


def _is_tag(window: str, position: int, length: int) -> bool:
    """Помечает ли код соседнее число — «(1600_chg_pct)» или «(2110_chg_abs: …)».

    Двоеточие после кода — та же пометка: «увеличилась на 39 851 тыс. руб.
    (2110_chg_abs: 39 851 тыс. руб.)» называет ту же величину дважды.
    А код, за которым внутри тех же скобок идёт число без двоеточия —
    «(nwc_chg_abs -1 248 224 574)», — приписан к этому числу, а не к соседнему,
    и тегом не является.
    """
    before = window[position - 1] if position > 0 else " "
    after = window[position + length] if position + length < len(window) else " "
    return before in "([" and (after in ")]" or after == ":")


def _binds_to_number_before(window: str, position: int) -> bool:
    """Приписан ли тег к числу слева — «снизилась на 1,6 % (1600_chg_pct)».

    Такой тег принадлежит своему числу и не может служить якорем следующему:
    иначе во фразе «на 1,6 % (1600_chg_pct) до 25 736 328 136 тыс. руб.»
    валюта баланса привязалась бы к коду процентного изменения.

    Но число слева не всегда величина. «Изменение за период по строке 2120
    (2120_chg_pct) — 1 832,1 %» называет перед тегом код строки, а не сумму,
    и тег принадлежит следующему числу. `verify.py` такие упоминания исключает
    из проверки целиком, а здесь они нужны затем, чтобы не отдать им чужой
    тег: без этого величина изменения привязывалась к самой строке, у которой
    значения совсем другие.
    """
    prefix = window[: max(0, position - 1)]
    trimmed = _TAIL.sub("", prefix)
    if not trimmed or not trimmed[-1].isdigit():
        return False
    return not any(
        match.end() == len(trimmed) for match in LINE_REFERENCE.finditer(trimmed)
    )


def _is_attached(gap: str, *, closes: bool = False) -> bool:
    """Приписан ли код к самому числу.

    Две формы приписки, обе встречались на живых ответах:
    «1,6 % (1600_chg_pct)» — код открывает свои скобки, и «(27 019 тыс. руб.,
    equity_chg_abs)» — число и код стоят в одних скобках, код вторым.
    Вторую форму выдаёт закрывающая скобка сразу за кодом (`closes`).

    Одного отсутствия слов между ними мало: «16 432 222 886 тыс. руб.
    Коэффициент автономии» тоже выглядело бы припиской, хотя это начало
    нового предложения о другом показателе.

    Перевод строки разрывает связь всегда: в таблице блока ДАННЫЕ за
    значениями одной строки сразу идёт код следующей, и без этого правила
    величина приписывалась бы соседней строке отчётности.
    """
    if not (gap.endswith(("(", "[")) or closes):
        return False
    if "\n" in gap or len(gap) > MAX_ATTACHED_GAP:
        return False
    return not _LETTER.search(_UNITS.sub("", gap))


def _overlaps_number(window: str, position: int, length: int) -> bool:
    """Не является ли найденный «код» частью другого числа или кода.

    Подчёркивание рядом означает, что найденный ключ — кусок более длинного
    кода: 1600 в «1600_chg_pct», 1300 в «structure_shift_1300». Кусок якорем
    не является, и без этого правила он побеждал бы целый код по близости —
    величина структурного сдвига привязывалась бы к строке баланса 1300,
    у которой значения совсем другие.
    """
    before = window[position - 1] if position > 0 else " "
    after = window[position + length] if position + length < len(window) else " "
    return before.isdigit() or after.isdigit() or "_" in (before, after)
