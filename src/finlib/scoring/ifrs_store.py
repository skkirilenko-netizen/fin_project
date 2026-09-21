"""Запись показателей и оценки МСФО в базу — теми же таблицами, что у РСБУ.

**Таблицы одни, стандарт входит в ключ.** Ряды по РСБУ и по МСФО несопоставимы,
и держать для них разные таблицы значило бы иметь две модели одного предмета:
ключ `metric_value` и `assessment` уже содержит стандарт, и этого достаточно.

Разложение оценки МСФО беднее разложения РСБУ, и пустые графы здесь означают
именно отсутствие предмета, а не потерю: уровня и динамики у показателя МСФО
нет — балл ставится по значению, потому что второй точки ряда у эмитента
ещё не бывает; флагов и надзорных сигналов ветка МСФО пока не считает.
Заполнять их нулями нельзя: ноль читался бы как «посчитано и получилось ноль».
"""

import logging
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection
from finlib.metrics.ifrs import MetricValue
from finlib.metrics.store import save_results
from finlib.normalize.ifrs_metrics import IfrsMetricsPolicy, load_ifrs_metrics
from finlib.quality.periods import PeriodConfidence
from finlib.scoring.definitions import Confidence, StopEffect
from finlib.scoring.engine import Assessment as StoredAssessment
from finlib.scoring.engine import GroupScore as StoredGroup
from finlib.scoring.ifrs import Assessment as IfrsAssessment
from finlib.scoring.metric_score import MetricScore as StoredMetric
from finlib.scoring.store import save_assessment
from finlib.standards import Standard

logger = logging.getLogger(__name__)


class _Result:
    """Строка расчёта в виде, который принимает запись показателей.

    Отдельный вид, а не подмена: у РСБУ результат несёт период, статус
    и доверие к периоду, а расчёт МСФО отдаёт значение с причиной отказа.
    Сводить их в одном классе значило бы объявить одинаковым то, что считается
    по-разному.
    """

    __slots__ = (
        "report_date",
        "metric_code",
        "value",
        "status",
        "confidence",
        "reason",
        "reason_code",
    )

    def __init__(self, item: MetricValue, report_date: date) -> None:
        self.report_date = report_date
        self.metric_code = item.code
        self.value = item.value
        self.status = _Status("ok" if item.calculable else "not_calculable")
        # Доверие к периоду у МСФО одно: комплект либо принят, либо в карантине
        # и в расчёт не идёт вовсе. Сравнительных периодов без своего комплекта
        # здесь пока не бывает.
        self.confidence = PeriodConfidence.VERIFIED
        # Причина без наименования: наименование подставляет тот, кто печатает
        # отказ, и в разделе «Ограничения анализа» выходило «Показатель
        # „Покрытие погашений“ не рассчитан: Покрытие погашений: не рассчитан…».
        self.reason = None if item.calculable else _reason_text(item)
        # Машинная причина — само значение перечисления: свободных строк
        # для неё в коде быть не должно.
        self.reason_code = item.reason.value if item.reason else None


class _Status:
    """Статус значения в виде, который ожидает запись: со полем `value`."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


def _reason_text(item: MetricValue) -> str:
    """Причина отказа словами, без наименования показателя."""
    from finlib.metrics.ifrs import REASON_TEXT

    text = REASON_TEXT.get(item.reason, "причина не названа")
    return f"{text} — {', '.join(item.missing)}" if item.missing else text


def save_metrics(
    inn: str,
    report_date: date,
    computed: tuple[MetricValue, ...],
    conn: PgConnection,
    policy: IfrsMetricsPolicy | None = None,
) -> int:
    """Пишет показатели МСФО за период; повторный расчёт не создаёт дублей."""
    policy = policy or load_ifrs_metrics()
    saved = save_results(
        inn,
        [_Result(item, report_date) for item in computed],
        conn,
        Standard.IFRS,
    )
    logger.info(
        "показатели МСФО: записано %d значений по ИНН %s за %s",
        saved,
        inn,
        report_date,
    )
    return saved


def save_ifrs_assessment(
    inn: str,
    report_date: date,
    assessment: IfrsAssessment,
    computed: tuple[MetricValue, ...],
    conn: PgConnection,
    policy: IfrsMetricsPolicy | None = None,
) -> int:
    """Пишет оценку МСФО и её разложение по группам и показателям."""
    policy = policy or load_ifrs_metrics()
    in_scoring = {item.code for group in assessment.groups for item in group.metrics}
    stored = StoredAssessment(
        inn=inn,
        standard=Standard.IFRS,
        report_date=report_date,
        total_score=assessment.score,
        class_code=assessment.class_code,
        class_name=assessment.class_name or None,
        no_class_reason=assessment.no_class_reason or None,
        # Узость основания у МСФО объявляется причиной отказа целиком:
        # отдельного случая «класс есть, а балла нет» здесь не возникает —
        # стоп-факторы ветки применяются к показателям, а не к классу.
        breadth_reason=None,
        class_before_stop=assessment.class_code,
        stop_factor_code=None,
        stop_factor_effect=StopEffect.NONE,
        # Уверенность в оценке у МСФО пока не ступенчатая: правило понижения
        # опирается на число периодов ряда, а ряда по МСФО ещё нет. Высокая
        # здесь означает «понижать не по чему», и расхождение мер долговой
        # нагрузки идёт рядом словами.
        confidence=Confidence.HIGH,
        confidence_reasons=list(assessment.divergence),
        groups=[
            StoredGroup(
                code=group.code,
                name=group.name,
                score=group.score,
                nominal_weight=group.nominal_weight,
                effective_weight=group.effective_weight,
                metrics_used=len(group.metrics),
                metrics_excluded=sum(
                    1
                    for item in computed
                    if item.group == group.code and item.code not in in_scoring
                ),
            )
            for group in assessment.groups
        ],
        metrics=[
            StoredMetric(
                metric_code=item.code,
                group_code=item.group,
                value=item.value,
                score=_score_of(item.code, assessment),
                # Уровня и динамики у показателя МСФО нет: балл ставится
                # по значению, второй точки ряда у эмитента пока не бывает.
                level=None,
                dynamics=None,
                periods_used=1 if item.calculable else 0,
                included=item.code in in_scoring,
                exclusion_reason=_exclusion_of(item, in_scoring),
                exclusion_kind=None,
                excluded_by_methodology=not item.in_scoring,
            )
            for item in computed
        ],
        metrics_version=policy.version,
        scoring_version=policy.version,
        flags_version="",
    )
    return save_assessment(stored, conn)


def _score_of(code: str, assessment: IfrsAssessment) -> Decimal | None:
    """Балл показателя из разложения оценки; None — в балл не входил."""
    for group in assessment.groups:
        for item in group.metrics:
            if item.code == code:
                return item.score
    return None


def _exclusion_of(item: MetricValue, in_scoring: set[str]) -> str | None:
    """Почему показатель не вошёл в балл; None — вошёл.

    Причина обязана быть безусловной и различать наше решение от неполноты
    данных: «в балл не идёт по методике» и «не рассчитан» — разные сведения.
    """
    if item.code in in_scoring:
        return None
    if not item.in_scoring:
        return "в балл не входит по методике: показатель описывает деятельность"
    if not item.calculable:
        return item.describe()
    return "шкала уровня для показателя не объявлена"
