"""Тип эмитента, применимость стоп-факторов и сверка с заключением.

**Тип определяется структурой отчётности, а текст подтверждает.** Статьи
расчётов с Принципалом или средств на счетах эскроу — это устройство
деятельности, а слова о них встречаются и у тех, кто такой деятельности
не ведёт: у ЛСР дважды сказано «кредитная организация», потому что банки
дают ему проектное финансирование, и по тексту он стал бы финансовым.

**Видов неприменимости три, и путать их нельзя.**

| Вид | Что меняется | Пример |
|---|---|---|
| по типу | не применяется к типу целиком | покрытие процентов у квазисуверенной |
| по обстановке | не применяется при условии | оборотный капитал при высоком покрытии |
| поправка | иначе считается сам показатель | ликвидность девелопера без эскроу |

Третий вид здесь не живёт: он относится к составу показателя, а не к оценке,
и объявлен в `methodology/ifrs_metrics.yaml`.

**Неприменение печатается всегда.** Оговорка берётся из методики дословно:
стоп-фактор, не применённый молча, меняет исход и не оставляет следа —
то же самое, что молча выбранный документ.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.ifrs_issuer_type import (
    IssuerTypePolicy,
    NotApplicable,
    load_issuer_types,
)

logger = logging.getLogger(__name__)


class Determination(StrEnum):
    """Как определён тип эмитента."""

    STRUCTURAL = "structural"
    DEFAULT = "default"
    AT_INTAKE = "at_intake"


class Consistency(StrEnum):
    """Согласуется ли стоп-фактор с аудиторским заключением."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    NOT_READABLE = "not_readable"


@dataclass(frozen=True, slots=True)
class TypeVerdict:
    """Тип эмитента вместе с основаниями и требованием подтверждения."""

    code: str
    name: str
    determination: Determination
    structural: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    needs_confirmation: bool = False

    def describe(self) -> str:
        """Однострочная сводка для отчёта."""
        if self.determination is Determination.DEFAULT:
            return f"{self.name} (признаков иного типа не найдено)"
        found = ", ".join(self.structural) or "—"
        markers = ", ".join(self.markers) or "—"
        tail = ", требует подтверждения" if self.needs_confirmation else ""
        return f"{self.name}: статьи {found}; подтверждения в тексте: {markers}{tail}"


@dataclass(frozen=True, slots=True)
class Applicability:
    """Применим ли стоп-фактор и почему нет."""

    stop_factor: str
    applicable: bool
    kind: str = ""
    limitation: str = ""
    rationale: str = ""

    def describe(self) -> str:
        """Однострочное описание для отчёта."""
        if self.applicable:
            return f"{self.stop_factor}: применяется"
        return f"{self.stop_factor}: не применяется ({self.kind})"


def determine_type(
    values: dict[str, Decimal],
    text: str,
    policy: IssuerTypePolicy | None = None,
) -> TypeVerdict:
    """Определяет тип эмитента по структуре отчётности и тексту.

    Структурный признак обязателен, текстовые маркеры — подтверждение.
    Без структурного признака тип не присваивается: слова о концессиях
    и эскроу встречаются и у тех, кто ни того ни другого не ведёт.
    """
    policy = policy or load_issuer_types()
    lowered = text.lower()
    for item in policy.types:
        if item.default or item.determined_at:
            continue
        structural = tuple(
            code for code in item.structural_any_of if values.get(code) is not None
        )
        if not structural:
            continue
        markers = tuple(
            marker for marker in item.markers if marker.lower() in lowered
        )
        if len(markers) < item.min_markers:
            logger.info(
                "тип %s не присвоен: статьи есть, подтверждений в тексте %d из %d",
                item.code,
                len(markers),
                item.min_markers,
            )
            continue
        return TypeVerdict(
            item.code,
            item.name,
            Determination.STRUCTURAL,
            structural,
            markers,
            item.confirmation == "required",
        )
    fallback = policy.fallback
    return TypeVerdict(fallback.code, fallback.name, Determination.DEFAULT)


def applicability(
    stop_factor: str,
    issuer_type: str,
    metrics: dict[str, Decimal | None],
    policy: IssuerTypePolicy | None = None,
) -> Applicability:
    """Применим ли стоп-фактор к этому эмитенту; оговорка — из методики.

    Оговорка не сочиняется здесь и не пересказывается: она объявлена
    справочником дословно, потому что печатается читателю.
    """
    policy = policy or load_issuer_types()
    for norm in policy.norms_for(stop_factor):
        if norm.kind == "by_type" and norm.type == issuer_type:
            return _refused(norm)
        if norm.kind == "by_context" and _holds(norm, metrics):
            return _refused(norm)
    return Applicability(stop_factor, True)


def consistency(
    stop_factor: str,
    audit_sections: tuple[str, ...],
    audit_readable: bool,
    policy: IssuerTypePolicy | None = None,
) -> tuple[Consistency, str]:
    """Сверяет стоп-фактор с аудиторским заключением.

    Совпадение и расхождение — разные ситуации: стоп-фактор с подтверждением
    аудитора и без него равно остаются в силе, но во втором случае внешнего
    свидетельства нет, и формулировки обязаны быть осторожнее. Нечитаемое
    заключение — третий исход: отсутствие подтверждения там ничего
    не означает.
    """
    policy = policy or load_issuer_types()
    rules = policy.audit_consistency
    if not audit_readable:
        return Consistency.NOT_READABLE, rules.not_readable_note
    wanted = rules.confirmed_by.get(stop_factor, ())
    if any(section in audit_sections for section in wanted):
        return Consistency.CONFIRMED, rules.confirmed_note
    return Consistency.UNCONFIRMED, rules.unconfirmed_note


def _refused(norm: NotApplicable) -> Applicability:
    """Отказ применить стоп-фактор вместе с оговоркой из методики."""
    logger.info("стоп-фактор %s не применён: %s", norm.stop_factor, norm.kind)
    return Applicability(
        norm.stop_factor, False, norm.kind, norm.limitation, norm.rationale
    )


def _holds(norm: NotApplicable, metrics: dict[str, Decimal | None]) -> bool:
    """Выполняется ли условие неприменимости по обстановке.

    Величины нет — условие не выполняется: неприменимость по обстановке
    обязана опираться на посчитанный показатель, а не на его отсутствие.
    """
    value = metrics.get(norm.when.metric)
    if value is None:
        return False
    threshold = Decimal(norm.when.value)
    if norm.when.condition == "gte":
        return value >= threshold
    if norm.when.condition == "gt":
        return value > threshold
    if norm.when.condition == "lte":
        return value <= threshold
    return value < threshold


def adjusted_metrics(issuer_type: str, policy=None) -> tuple:
    """Показатели, состав которых у этого типа другой.

    Поправка живёт в составе показателей, а не в оценке: у девелопера иначе
    считается сама ликвидность, и это не смягчение стоп-фактора.
    """
    from finlib.normalize.ifrs_issuer_type import load_ifrs_metrics

    return (policy or load_ifrs_metrics()).for_type(issuer_type)
