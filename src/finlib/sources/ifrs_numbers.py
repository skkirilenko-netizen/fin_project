"""Разделитель разрядов документа МСФО: определение и разбор чисел.

Отдельное число конвенции не выдаёт. «663,888» — это 663 888 при запятой
в роли разделителя разрядов и 663,888 при запятой в роли десятичного знака;
оба прочтения осмысленны и различаются в тысячу раз. Выбрать между ними
можно только по документу целиком, поэтому конвенция определяется по выборке
чисел, а не по первому попавшемуся.

Ошибка здесь не ловится ни одним контролем сходимости: применённая ко всем
числам одинаково, неверная конвенция оставляет баланс сошедшимся, разделы
сошедшимися и коэффициенты верными — неверными окажутся все абсолютные
величины. Это тот же класс дефекта, что единица измерения в РСБУ, и потому
то же отношение: правило, а не предположение, и карантин при неопределённости.
"""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from finlib.config import settings
from finlib.normalize.lines import normalize_name
from finlib.utils import marked_by

logger = logging.getLogger(__name__)


class Grouping(StrEnum):
    """Конвенция записи чисел документа.

    Названа целиком, а не двумя независимыми полями: разделитель разрядов
    и десятичный знак связаны — запятая не может быть одновременно тем
    и другим.
    """

    # Разряды — пробел, десятичный знак — запятая: «1 234 567,89».
    RUSSIAN = "russian"
    # Разряды — запятая, десятичный знак — точка: «1,234,567.89».
    ENGLISH = "english"
    # Разделителей разрядов в документе нет вовсе: «1234567».
    PLAIN = "plain"


class GroupingUndetermined(StrEnum):
    """Почему конвенция не определена; значения совпадают с кодами методики."""

    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"
    # Улики есть, но их горстка против сотни неоднозначных чисел: у ФосАгро
    # шесть против ста пятидесяти трёх. Большинством это не является,
    # и голосование здесь ничего не решает.
    OUTWEIGHED = "outweighed"


# Пробелы, которыми верстают разряды: обычный, неразрывный, узкий неразрывный.
SPACES = "    "

# Число с разделителями разрядов пробелом: «1 234 567».
_SPACE_GROUPED = re.compile(rf"\d{{1,3}}(?:[{SPACES}]\d{{3}})+(?![\d,.])")

# Число с двумя и более запятыми: «1,234,567». Однозначное свидетельство —
# десятичных знаков в числе двух не бывает.
_COMMA_GROUPED = re.compile(r"\d{1,3}(?:,\d{3}){2,}(?![\d.])")

# Число с двумя и более точками: «1.234.567».
_DOT_GROUPED = re.compile(r"\d{1,3}(?:\.\d{3}){2,}(?![\d,])")

# Дробная часть, в которой не три цифры: разделитель здесь точно десятичный.
_COMMA_DECIMAL = re.compile(r"\d,(?:\d{1,2}|\d{4,})(?![\d])")
_DOT_DECIMAL = re.compile(r"\d\.(?:\d{1,2}|\d{4,})(?![\d])")

# Неоднозначное число: ровно три цифры после разделителя и он один.
# «663,888» — это и 663 888, и 663,888, и выбрать нельзя.
_COMMA_AMBIGUOUS = re.compile(r"(?<![\d,.])\d{1,3},\d{3}(?![\d,.])")
_DOT_AMBIGUOUS = re.compile(r"(?<![\d,.])\d{1,3}\.\d{3}(?![\d,.])")

# Число, содержащее оба разделителя сразу: «11,266.5» и «11.266,5». Улика
# бесспорная — один и тот же знак не бывает в одном числе и разрядным,
# и десятичным, — и потому пригодная там, где остальные улики ложны.
_BOTH_ENGLISH = re.compile(r"\d{1,3}(?:,\d{3})+\.\d+")
_BOTH_RUSSIAN = re.compile(r"\d{1,3}(?:\.\d{3})+,\d+")


def decisive_evidence(text: str) -> tuple[int, int]:
    """Сколько в тексте бесспорных улик за русскую и за английскую конвенцию.

    Улика бесспорная — число с обоими разделителями сразу. Такие числа
    ищутся по всему документу, а не только в таблицах форм: они не бывают
    ложными, и отбирать их по месту незачем. У ФосАгро их семь, все
    английские, и стоят они в таблице дивидендов — «11,266.5», — которую
    голосование из выборки как раз исключает.
    """
    return len(_BOTH_RUSSIAN.findall(text)), len(_BOTH_ENGLISH.findall(text))


class ArithmeticResolution(BaseModel):
    """Разрешение неоднозначности сходимостью итогов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    min_totals: int = Field(ge=1)
    origin: str = Field(min_length=1)


class GroupingPolicy(BaseModel):
    """Пороги определения конвенции."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    not_money_rows: tuple[str, ...] = Field(min_length=1)
    min_evidence: int = Field(ge=1)
    min_decisive: Decimal = Field(gt=0, le=1)
    origin: str = Field(min_length=1)
    arithmetic_resolution: ArithmeticResolution
    reasons: dict[GroupingUndetermined, str]


class AssetsToRevenue(BaseModel):
    """Границы отношения валюты баланса к выручке."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: Decimal = Field(gt=0)
    max: Decimal = Field(gt=0)
    origin: str = Field(min_length=1)


class PlausibilityPolicy(BaseModel):
    """Правила проверки правдоподобия выбранной конвенции."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assets_to_revenue: AssetsToRevenue
    thousand_factor: Decimal = Field(gt=0)
    thousand_tolerance: Decimal = Field(gt=0, lt=1)


class TextLayerPolicy(BaseModel):
    """Порог, ниже которого документ считается сканом без текстового слоя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_characters: int = Field(ge=1)
    origin: str = Field(min_length=1)

    @property
    def reason(self) -> str:
        """Причина отказа: OCR финансовых таблиц отложен, и это сказано прямо."""
        return (
            "В документе нет текстового слоя: извлечено меньше знаков, чем "
            f"требует методика ({self.min_characters}). Вероятно, подан скан; "
            "распознавание текста не реализовано."
        )


class DocumentKindPolicy(BaseModel):
    """Признаки того, что документ — финансовая отчётность."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_forms: int = Field(ge=1)
    required_forms: tuple[str, ...] = Field(min_length=1)
    cores: dict[str, tuple[str, ...]]
    heading_max_length: int = Field(ge=20)
    heading_wrap_lines: int = Field(ge=0)
    lookahead_lines: int = Field(ge=5)
    min_table_rows: int = Field(ge=1)
    heading_to_table_lines: int = Field(ge=1)
    contents_list_entries: int = Field(ge=2)
    contents_list_window: int = Field(ge=2)
    contents_list_origin: str = Field(min_length=1)
    table_rows_origin: str = Field(min_length=1)
    table_end_gap: int = Field(ge=2)
    table_end_origin: str = Field(min_length=1)
    reasons: dict[str, str]


class HeaderWindow(BaseModel):
    """Сколько знаков после заголовка формы считается её шапкой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    characters: int = Field(ge=100)
    origin: str = Field(min_length=1)


class FinancialInstitutionPolicy(BaseModel):
    """Признаки организации вне периметра методики."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    markers: tuple[str, ...] = Field(min_length=1)
    unclassified_balance_markers: tuple[str, ...] = Field(min_length=1)
    reasons: dict[str, str]


class CurrencyPolicy(BaseModel):
    """Как в документе объявляется валюта отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rouble_markers: tuple[str, ...] = Field(min_length=1)
    foreign_markers: dict[str, str]
    reasons: dict[str, str]


class UnitsPolicy(BaseModel):
    """Как в документе объявляется единица измерения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    markers: dict[str, str]
    reasons: dict[str, str]


class PeriodsPolicy(BaseModel):
    """Сколько отчётных дат допускает модель."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_count: int = Field(ge=1)
    max_count: int = Field(ge=1)
    reasons: dict[str, str]


class WordBreaksPolicy(BaseModel):
    """Какие одинокие буквы склеиваются со следующим словом.

    Извлекатель вставляет пробел внутрь слова, и наименование перестаёт
    опознаваться. Склеивается только то, что словом не бывает: перечень
    однобуквенных слов закрыт, и всё, чего в нём нет, одинокой буквой
    в наименовании стоять не может.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    single_letter_words: tuple[str, ...] = Field(min_length=1)
    origin: str = Field(min_length=1)
    # Знак сноски, прилипший к последнему слову наименования: сколько цифр
    # он занимает и после скольких строчных букв подряд стоит.
    footnote_mark_max_digits: int = Field(gt=0)
    footnote_mark_after_letters: int = Field(gt=0)
    # Определённые термины отчётности («Группа», «Компания»): строка
    # из одного такого слова — окончание перенесённого наименования,
    # а не заголовок раздела.
    defined_terms: tuple[str, ...] = Field(min_length=1)


class ColumnSpansPolicy(BaseModel):
    """Как в шапке формы объявляется длительность её граф.

    Графы различаются не только датой: промежуточная форма ФосАгро печатает
    рядом полугодие и квартал. Длительность объявлена словами, и перечень
    написаний живёт в методике — угадывать её по числу граф нельзя.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    markers: dict[str, int]
    reasons: dict[str, str]
    origin: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ColumnLayout:
    """Разметка граф формы: сколько их всего и какие из них наши.

    `total` — граф с величинами в шапке, `taken` — сколько из них берётся
    (число отчётных дат формы), `offset` — с какой графы они начинаются.
    `spans` — длительности граф в порядке объявления, `months` — длительность
    комплекта, которой они сверялись.

    **Прочитана разметка или нет, объявляется полем, а не выводится из чисел.**
    `total == taken` бывает и там, где шапку прочли, и там, где читать оказалось
    нечего, а последствия разные: во втором случае лишние графы, если они
    есть в строках, отбрасываются вслепую, и это нарушение.
    """

    total: int
    taken: int
    offset: int = 0
    spans: tuple[int, ...] = ()
    months: int | None = None

    @property
    def read(self) -> bool:
        """Прочитана ли длительность граф из шапки."""
        return bool(self.spans)

    @property
    def wider(self) -> bool:
        """Граф в шапке больше, чем берётся."""
        return self.total > self.taken

    def describe(self) -> str:
        """Однострочное описание для журнала."""
        spans = ", ".join(f"{item} мес." for item in self.spans) or "не объявлена"
        return (
            f"граф {self.total}, берутся {self.taken} с {self.offset + 1}-й; "
            f"длительность граф: {spans}; период комплекта "
            f"{self.months if self.months is not None else '—'} мес."
        )


class ReportingKindPolicy(BaseModel):
    """Виды отчётности и оговорки, которые они влекут."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    markers: dict[str, str]
    default: str = Field(min_length=1)
    limitations: dict[str, str]


class NotesPolicy(BaseModel):
    """Как опознаются примечания и как они сверяются с оглавлением."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    heading_max_length: int = Field(ge=20)
    max_number: int = Field(ge=1)
    max_number_gap: int = Field(ge=1)
    continuation_markers: tuple[str, ...] = Field(min_length=1)
    contents_min_entries: int = Field(ge=1)
    title_match_ratio: Decimal = Field(gt=0, le=1)
    origin: str = Field(min_length=1)


class IssuerNamePolicy(BaseModel):
    """Как опознаётся наименование эмитента в документе.

    Опора структурная: наименование стоит на титульном листе и повторяется
    колонтитулом каждой страницы. Одно упоминание признаком не считается —
    в тексте отчётности называются и другие организации (дочерние, банки,
    контрагенты), и по единственному упоминанию эмитента от них не отличить.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    legal_forms: tuple[str, ...] = Field(min_length=1)
    min_occurrences: int = Field(ge=2)
    max_length: int = Field(ge=10)
    origin: str = Field(min_length=1)


class ExtractionCompletenessPolicy(BaseModel):
    """Когда извлечение считается полным.

    Мера отдельная от полноты справочника намеренно: потерянная страница
    и незаведённая позиция лечатся по-разному, и один показатель на оба
    дефекта скрывал бы, какой из них сработал.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pages_without_text_share: Decimal = Field(ge=0)
    max_partial_forms: int = Field(ge=0)
    required_totals: dict[str, tuple[str, ...]] = Field(min_length=1)
    origin: str = Field(min_length=1)


class ParsingPolicy(BaseModel):
    """Правила разбора файла консолидированной отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    text_layer: TextLayerPolicy
    document_kind: DocumentKindPolicy
    financial_institution: FinancialInstitutionPolicy
    header_window: HeaderWindow
    currency: CurrencyPolicy
    units: UnitsPolicy
    periods: PeriodsPolicy
    word_breaks: WordBreaksPolicy
    column_spans: ColumnSpansPolicy
    reporting_kind: ReportingKindPolicy
    digit_grouping: GroupingPolicy
    grouping_plausibility: PlausibilityPolicy
    notes: NotesPolicy
    issuer_name: IssuerNamePolicy
    extraction_completeness: ExtractionCompletenessPolicy


def default_path() -> Path:
    """Путь к справочнику правил разбора."""
    return settings.methodology_dir / "ifrs_parsing.yaml"


@lru_cache(maxsize=8)
def load_parsing_policy(path: Path | None = None) -> ParsingPolicy:
    """Читает правила разбора файла МСФО."""
    source = Path(path) if path is not None else default_path()
    return ParsingPolicy.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )


@dataclass(frozen=True, slots=True)
class GroupingDetection:
    """Итог определения конвенции по документу.

    Счётчики хранятся вместе с решением, а не только решение: ноль
    свидетельств против ноля проверенных чисел — разные вещи, и по журналу
    их надо различать.
    """

    convention: Grouping | None
    reason: GroupingUndetermined | None = None
    russian_evidence: int = 0
    english_evidence: int = 0
    ambiguous: int = 0
    numbers_seen: int = 0
    samples: tuple[str, ...] = field(default_factory=tuple)
    # Сами числа-свидетельства, по нескольку с каждой стороны. Счётчик
    # говорит, сколько улик нашлось, и не говорит, чего они стоят: «1.5»
    # в русском документе — улика за английскую конвенцию ровно до тех пор,
    # пока не видно, что это ставка процента или номер пункта. Отказ, причину
    # которого нельзя проверить глазами, заставляет гадать о документе,
    # которого не видно.
    russian_samples: tuple[str, ...] = field(default_factory=tuple)
    english_samples: tuple[str, ...] = field(default_factory=tuple)
    ambiguous_samples: tuple[str, ...] = field(default_factory=tuple)
    # Чем разрешена неоднозначность, если она была: голосованием или
    # сходимостью итогов. Способ называется, потому что доверие к нему разное:
    # голосование опирается на разметку чисел, арифметика — на сам документ.
    resolved_by: str = "vote"

    @property
    def determined(self) -> bool:
        """Определена ли конвенция."""
        return self.convention is not None

    def describe(self) -> str:
        """Однострочная сводка для журнала."""
        if self.convention is not None:
            return (
                f"конвенция {self.convention.value}: свидетельств за русскую "
                f"{self.russian_evidence}, за английскую {self.english_evidence}, "
                f"неоднозначных чисел {self.ambiguous}, чисел просмотрено "
                f"{self.numbers_seen}"
            )
        return (
            f"конвенция не определена ({self.reason.value if self.reason else '—'}): "
            f"за русскую {self.russian_evidence}, за английскую "
            f"{self.english_evidence}, неоднозначных {self.ambiguous}, "
            f"чисел просмотрено {self.numbers_seen}"
        )

    def evidence(self) -> tuple[str, ...]:
        """Сами числа, на которых построено решение, — построчно.

        Счётчик отвечает «сколько», а разбираться приходится с «какие»:
        отказ по конвенции читается только вместе с числами, которые его
        вызвали. Строка с числом и есть то место документа, куда надо
        посмотреть.
        """
        found: list[str] = []
        for name, items in (
            ("за русскую", self.russian_samples),
            ("за английскую", self.english_samples),
            ("допускают оба прочтения", self.ambiguous_samples),
        ):
            if items:
                found.append(f"{name}: " + ", ".join(f"«{item}»" for item in items))
        return tuple(found)


# Сколько чисел-свидетельств показывать при отказе. Больше десятка человек
# глазами не разбирает, меньше трёх не даёт увидеть повтор.
SAMPLE_LIMIT = 10


def _few(found: list[str]) -> tuple[str, ...]:
    """Несколько разных чисел из найденных: повторы места не занимают."""
    return tuple(dict.fromkeys(item.strip() for item in found))[:SAMPLE_LIMIT]


# Любое число документа — для счётчика просмотренного.
_ANY_NUMBER = re.compile(rf"\d[\d{SPACES},.]*\d|\d")

# Числа, которые денежными величинами не являются и за конвенцию голосовать
# не вправе. Каждая маска отвечает своему источнику ложных улик, найденному
# на настоящих документах: у Автодора одна улика за английскую конвенцию
# против семисот сорока за русскую, у Норникеля три против восьмисот
# пятидесяти семи. Снижать порог из-за них значило бы подбирать отсечку
# под наблюдаемые данные.
_NOT_MONEY: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Ссылка на стандарт: «МСФО (IAS) 34», «IFRS 9», «IAS 1».
    (
        "ссылка на стандарт",
        re.compile(r"(?:мсфо|ias|ifrs|фсбу)\s*\(?[a-zа-я]*\)?\s*\d+", re.IGNORECASE),
    ),
    # Номер пункта или подпункта в начале строки: «12.», «12.5 Порядок».
    ("номер пункта", re.compile(r"(?m)^\s*\d+(?:\.\d+)*\.?(?=\s)")),
    # Дата целиком: «31.12.2025», «31/12/2025».
    ("дата", re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")),
    # Номер примечания, пояснения или страницы: «Пояснения 12», «стр. 13-75».
    (
        "ссылка на примечание",
        re.compile(
            r"(?:примечани\w*|пояснени\w*|стр\.?|страниц\w*)\s*\d+(?:\s*[-–]\s*\d+)?",
            re.IGNORECASE,
        ),
    ),
    # Процент или ставка: «12,5 %», «7.25%».
    ("процент", re.compile(r"\d[\d\s.,]*\s*%")),
    # Год: «2024», «2025 г.». Идёт последней — маска узкая, и убирать год
    # раньше даты нельзя, иначе от даты останутся обрывки.
    ("год", re.compile(r"\b(?:19|20)\d{2}\b")),
)


def drop_not_money_rows(
    lines: list[str], policy: GroupingPolicy
) -> tuple[list[str], int]:
    """Убирает строки, величины которых приведены не в единице отчётности.

    Прибыль на акцию, номинал, количество акций стоят внутри форм и законны,
    но за конвенцию разрядов голосовать не вправе: они печатаются в рублях
    с копейками, тогда как суммы в той же форме идут миллионами с пробелами.
    """
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if marked_by(line, policy.not_money_rows, normalize_name):
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def ballot(text: str) -> tuple[str, dict[str, int]]:
    """Текст для голосования за конвенцию и счётчик исключённого.

    За конвенцию голосуют только денежные величины. Всё прочее — номера
    пунктов, ссылки на стандарты, даты, проценты, номера примечаний —
    исключается до подсчёта: это источник ложных улик, и снижать из-за них
    порог нельзя.

    Счётчик исключённого возвращается вместе с текстом: сколько чисел
    отброшено и по какой причине, видно в журнале. Молчаливая чистка
    выборки — то же, что молчаливое изменение порога.
    """
    removed: dict[str, int] = {}
    cleaned = text
    for name, pattern in _NOT_MONEY:
        cleaned, count = pattern.subn(" ", cleaned)
        if count:
            removed[name] = count
    return cleaned, removed


def detect_grouping(
    text: str, policy: GroupingPolicy | None = None
) -> GroupingDetection:
    """Определяет конвенцию записи чисел по документу целиком.

    Свидетельством считается только число, разметка которого не допускает
    двух прочтений: пробел между группами по три цифры, две и более запятых
    в одном числе, дробная часть не из трёх цифр. Число вида «663,888»
    свидетельством не является — именно оно и создаёт неоднозначность.
    """
    policy = policy or load_parsing_policy().digit_grouping

    for_russian = _SPACE_GROUPED.findall(text) + _COMMA_DECIMAL.findall(text)
    for_english = (
        _COMMA_GROUPED.findall(text)
        + _DOT_GROUPED.findall(text)
        + _DOT_DECIMAL.findall(text)
    )
    unclear = _COMMA_AMBIGUOUS.findall(text) + _DOT_AMBIGUOUS.findall(text)
    russian, english, ambiguous = len(for_russian), len(for_english), len(unclear)
    seen = len(_ANY_NUMBER.findall(text))
    samples = tuple(_ANY_NUMBER.findall(text)[:5])

    counts = {
        "russian_evidence": russian,
        "english_evidence": english,
        "ambiguous": ambiguous,
        "numbers_seen": seen,
        "samples": samples,
        "russian_samples": _few(for_russian),
        "english_samples": _few(for_english),
        "ambiguous_samples": _few(unclear),
    }

    total = russian + english
    if total == 0:
        # Свидетельств нет вовсе. Если и неоднозначных чисел нет, разделителей
        # разрядов в документе не встречается — конвенция не нужна, числа
        # читаются как есть. Если неоднозначные есть, выбирать между двумя
        # прочтениями нельзя.
        if ambiguous:
            return _undetermined(GroupingUndetermined.AMBIGUOUS, counts)
        return GroupingDetection(Grouping.PLAIN, **counts)

    # Конвенцией считается то, что набрало порог улик, — и порог один
    # на обе стороны. Прежде победителю требовались три улики, а отказ
    # наступал от одной улики против: правило требовало единогласия, которого
    # не даёт ни одна вёрстка. Одно-два числа чужой разметки — та же опечатка
    # составителя, что и одно число своей, и мерить их разными мерками нельзя.
    russian_is_convention = russian >= policy.min_evidence
    english_is_convention = english >= policy.min_evidence
    if russian_is_convention and english_is_convention:
        # Конвенции в документе действительно две. Разбирать такой документ
        # нельзя: часть чисел неминуемо будет прочитана неверно.
        return _undetermined(GroupingUndetermined.CONFLICTING, counts)
    if not russian_is_convention and not english_is_convention:
        return _undetermined(GroupingUndetermined.INSUFFICIENT, counts)

    winner = Grouping.RUSSIAN if russian_is_convention else Grouping.ENGLISH
    votes = russian if russian_is_convention else english
    total = votes + min(russian, english)
    # Улики считаются не сами по себе, а против неоднозначных чисел.
    # У ФосАгро их шесть против ста пятидесяти трёх — три процента, — и все
    # шесть оказались ложными: слипшаяся строка «5 573,628 507,689» читается
    # как число с пробелом между разрядами. У остальных разобранных эмитентов
    # неоднозначных чисел нет вовсе, и доля улик равна единице. Между тремя
    # процентами и сотней порог можно ставить где угодно; он посередине.
    if ambiguous and Decimal(total) / Decimal(total + ambiguous) < policy.min_decisive:
        return _undetermined(GroupingUndetermined.OUTWEIGHED, counts)
    # Улики противоположной стороны отброшены как случайность вёрстки —
    # и названы: прочитанные вопреки собственной разметке, эти числа
    # заслуживают взгляда человека, а молчание о них было бы тем же
    # молчаливым изменением порога.
    disregarded = english if winner is Grouping.RUSSIAN else russian
    if disregarded:
        logger.info(
            "конвенция %s: улик противоположной стороны %d, отброшены как "
            "случайность вёрстки — %s",
            winner.value,
            disregarded,
            ", ".join(
                counts["english_samples"]
                if winner is Grouping.RUSSIAN
                else counts["russian_samples"]
            ),
        )
    return GroupingDetection(winner, **counts)


def _undetermined(
    reason: GroupingUndetermined, counts: dict
) -> GroupingDetection:
    """Конвенция не определена; причина названа кодом методики."""
    return GroupingDetection(None, reason, **counts)


@dataclass(frozen=True, slots=True)
class PlausibilityCheck:
    """Итог проверки правдоподобия выбранной конвенции."""

    plausible: bool
    checked: int
    problems: tuple[str, ...] = ()

    def describe(self) -> str:
        """Однострочная сводка со счётчиком проверенного.

        Число проверенных величин стоит рядом с числом нарушений: ноль
        расхождений при нуле проверок означает, что проверять было нечем,
        а не что конвенция подтвердилась.
        """
        if not self.checked:
            return "правдоподобие конвенции не проверялось: сверять нечего"
        if self.plausible:
            return f"правдоподобие конвенции подтверждено, сверено величин: {self.checked}"
        return (
            f"правдоподобие конвенции не подтверждено ({len(self.problems)} "
            f"из {self.checked}): {'; '.join(self.problems)}"
        )


def check_plausibility(
    totals: dict[str, Decimal],
    revenue: Decimal | None = None,
    policy: PlausibilityPolicy | None = None,
) -> PlausibilityCheck:
    """Проверяет, что разобранные величины согласуются между собой.

    Неверно выбранная конвенция расходится на три порядка, то есть заметно.
    Проверяются две вещи:

    1. Сумма разделов против итога — та же арифметика, что у контролей
       сходимости, но здесь она сторожит не отчётность, а наше прочтение:
       при неверной конвенции часть чисел прочитана в тысячу раз иначе.
    2. Валюта баланса против порядка величин выручки. У действующей
       организации они соотносятся в пределах разумного, а подмена конвенции
       даёт отношение, отличающееся от истинного в тысячу раз.

    totals — разобранные итоги по кодам позиций справочника МСФО; revenue —
    выручка, если форма о прибыли или убытке разобрана.
    """
    policy = policy or load_parsing_policy().grouping_plausibility
    problems: list[str] = []
    checked = 0

    assets = totals.get("ifrs.total_assets")
    parts = (
        totals.get("ifrs.total_non_current_assets"),
        totals.get("ifrs.total_current_assets"),
    )
    if assets is not None and all(item is not None for item in parts):
        checked += 1
        computed = sum(parts, start=Decimal(0))
        if _off_by_thousand(computed, assets, policy):
            problems.append(
                f"сумма разделов актива {computed} отличается от итога {assets} "
                "кратно тысяче: часть чисел прочитана по другой конвенции"
            )

    equity_and_liabilities = totals.get("ifrs.total_equity_and_liabilities")
    if assets is not None and equity_and_liabilities is not None:
        checked += 1
        if _off_by_thousand(equity_and_liabilities, assets, policy):
            problems.append(
                f"пассив {equity_and_liabilities} отличается от актива {assets} "
                "кратно тысяче: формы прочитаны по-разному"
            )

    if assets is not None and revenue is not None and revenue != 0:
        checked += 1
        ratio = abs(assets / revenue)
        bounds = policy.assets_to_revenue
        if not bounds.min <= ratio <= bounds.max:
            problems.append(
                f"валюта баланса относится к выручке как {ratio:.1f}, что вне "
                f"границ правдоподобия [{bounds.min}; {bounds.max}]"
            )

    return PlausibilityCheck(not problems, checked, tuple(problems))


def _off_by_thousand(
    left: Decimal, right: Decimal, policy: PlausibilityPolicy
) -> bool:
    """Отличаются ли величины кратно тысяче.

    Проверяется именно кратность, а не всякое крупное расхождение: несошедшийся
    итог бывает дефектом отчётности и ловится контролем сходимости, а отношение
    ровно в тысячу раз означает разное прочтение чисел.
    """
    if right == 0 or left == 0:
        return False
    ratio = abs(left / right)
    if ratio < 1:
        ratio = 1 / ratio
    return abs(ratio - policy.thousand_factor) <= policy.thousand_factor * (
        policy.thousand_tolerance
    )


def parse_amount(token: str, convention: Grouping) -> Decimal | None:
    """Разбирает число документа по определённой конвенции.

    Конвенция передаётся обязательно: числа без неё не читаются вовсе.
    Значение по умолчанию здесь означало бы ровно то, ради отказа от чего
    написан весь модуль, — угадывание.

    **Круглые скобки означают минус.** В формах МСФО так печатают расход
    и всякую вычитаемую величину, и терять их нельзя: «Себестоимость (160 017)»
    приходила как +160 017, складывалась с выручкой вместо вычитания,
    и недостача по валовой прибыли равнялась ровно удвоенной себестоимости.

    Знак хранится в самой величине, а не выводится из справочника: у одного
    эмитента статья печатается в скобках, у другого без них, и соглашение,
    завязанное на справочник, разошлось бы с документом.
    """
    cleaned = token.strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()").strip()
    cleaned = cleaned.replace("−", "-").replace("–", "-")
    if not cleaned:
        return None
    if negative and not cleaned.startswith("-"):
        cleaned = f"-{cleaned}"

    if convention is Grouping.RUSSIAN:
        for space in SPACES:
            cleaned = cleaned.replace(space, "")
        cleaned = cleaned.replace(",", ".")
    elif convention is Grouping.ENGLISH:
        cleaned = cleaned.replace(",", "")
        for space in SPACES:
            cleaned = cleaned.replace(space, "")
    else:
        for space in SPACES:
            cleaned = cleaned.replace(space, "")
        cleaned = cleaned.replace(",", ".")

    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:  # pragma: no cover — форма уже проверена
        return None
