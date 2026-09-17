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


class GroupingPolicy(BaseModel):
    """Пороги определения конвенции."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_evidence: int = Field(ge=1)
    min_share: Decimal = Field(gt=0, le=1)
    origin: str = Field(min_length=1)
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
    reasons: dict[str, str]


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


class ReportingKindPolicy(BaseModel):
    """Виды отчётности и оговорки, которые они влекут."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    markers: dict[str, str]
    default: str = Field(min_length=1)
    limitations: dict[str, str]


class ParsingPolicy(BaseModel):
    """Правила разбора файла консолидированной отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    text_layer: TextLayerPolicy
    document_kind: DocumentKindPolicy
    financial_institution: FinancialInstitutionPolicy
    currency: CurrencyPolicy
    units: UnitsPolicy
    periods: PeriodsPolicy
    reporting_kind: ReportingKindPolicy
    digit_grouping: GroupingPolicy
    grouping_plausibility: PlausibilityPolicy


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


# Любое число документа — для счётчика просмотренного.
_ANY_NUMBER = re.compile(rf"\d[\d{SPACES},.]*\d|\d")


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

    russian = len(_SPACE_GROUPED.findall(text)) + len(_COMMA_DECIMAL.findall(text))
    english = (
        len(_COMMA_GROUPED.findall(text))
        + len(_DOT_GROUPED.findall(text))
        + len(_DOT_DECIMAL.findall(text))
    )
    ambiguous = len(_COMMA_AMBIGUOUS.findall(text)) + len(_DOT_AMBIGUOUS.findall(text))
    seen = len(_ANY_NUMBER.findall(text))
    samples = tuple(_ANY_NUMBER.findall(text)[:5])

    counts = {
        "russian_evidence": russian,
        "english_evidence": english,
        "ambiguous": ambiguous,
        "numbers_seen": seen,
        "samples": samples,
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

    winner = Grouping.RUSSIAN if russian >= english else Grouping.ENGLISH
    votes = max(russian, english)
    share = Decimal(votes) / Decimal(total)

    if share < policy.min_share:
        # Смешанные конвенции в одном документе не разбираются: это не выбор
        # большинством, а отказ — часть чисел неминуемо будет прочитана неверно.
        return _undetermined(GroupingUndetermined.CONFLICTING, counts)
    if votes < policy.min_evidence:
        return _undetermined(GroupingUndetermined.INSUFFICIENT, counts)
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

    Круглые скобки вокруг числа — способ печати расходной величины, как
    в формах РСБУ: знак берётся из справочника статей, а не из скобок,
    поэтому здесь скобки только снимаются.
    """
    cleaned = token.strip().strip("()").strip()
    cleaned = cleaned.replace("−", "-").replace("–", "-")
    if not cleaned:
        return None

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
