"""Сборка оценки: баллы групп, класс, стоп-факторы, уверенность.

Класс — фиксированная арифметика по методике (инвариант 2). Языковая модель
его не определяет и пересматривать не вправе.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, fetch_all
from finlib.metrics.definitions import MetricDef, MetricsCatalog, load_metrics
from finlib.metrics.engine import load_period_values, reporting_type_of
from finlib.quality.periods import PeriodConfidence
from finlib.quality.thresholds import Thresholds, load_thresholds
from finlib.scoring.definitions import (
    Confidence,
    FlagsCatalog,
    ScoringCatalog,
    StopEffect,
    StopFactorPolicy,
    load_flags,
    load_scoring,
)
from finlib.scoring.flags import FlagHit, evaluate_flags
from finlib.scoring.metric_score import MetricScore, score_metric
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_SELECT_SERIES = """
SELECT metric_code, report_date, value, status, confidence
FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date <= %(upto)s
ORDER BY metric_code, report_date
"""

_SELECT_LATEST_PERIOD = """
SELECT max(report_date) AS report_date FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND status = 'ok'
"""


@dataclass(frozen=True, slots=True)
class GroupScore:
    """Балл группы с разложением."""

    code: str
    name: str
    score: Decimal | None
    nominal_weight: Decimal
    effective_weight: Decimal
    metrics_used: int
    metrics_excluded: int


@dataclass
class Assessment:
    """Оценка финансового состояния со всем разложением."""

    inn: str
    standard: Standard
    report_date: date
    total_score: Decimal | None
    class_code: str
    class_name: str
    class_before_stop: str
    stop_factor_code: str | None
    stop_factor_effect: StopEffect
    confidence: Confidence
    confidence_reasons: list[str] = field(default_factory=list)
    groups: list[GroupScore] = field(default_factory=list)
    metrics: list[MetricScore] = field(default_factory=list)
    flags: list[FlagHit] = field(default_factory=list)
    metrics_version: str = ""
    scoring_version: str = ""
    flags_version: str = ""

    @property
    def limited_by_stop_factor(self) -> bool:
        """Изменил ли стоп-фактор класс, посчитанный по баллу."""
        return self.class_code != self.class_before_stop

    def summary(self) -> str:
        """Однострочная сводка для CLI."""
        score = f"{self.total_score:.1f}" if self.total_score is not None else "—"
        parts = [
            f"ИНН {self.inn}, период {self.report_date:%d.%m.%Y}",
            f"балл {score}",
            f"класс {self.class_code}",
            f"уверенность {self.confidence.value}",
        ]
        if self.limited_by_stop_factor:
            parts.append(
                f"ограничен стоп-фактором {self.stop_factor_code} "
                f"с класса {self.class_before_stop}"
            )
        if self.flags:
            parts.append("флаги: " + ", ".join(item.code for item in self.flags))
        return "; ".join(parts)


def latest_period(
    inn: str, conn: PgConnection | None = None, standard: Standard = Standard.RSBU
) -> date | None:
    """Самый свежий период, за который есть рассчитанные показатели."""
    rows = fetch_all(_SELECT_LATEST_PERIOD, {"inn": inn, "standard": standard.value}, conn=conn)
    return rows[0]["report_date"] if rows and rows[0]["report_date"] else None


def load_metric_series(
    inn: str, upto: date, conn: PgConnection | None = None, standard: Standard = Standard.RSBU
) -> tuple[dict[str, list[Decimal]], dict[str, PeriodConfidence]]:
    """Ряды рассчитанных показателей до указанного периода включительно."""
    series: dict[str, list[Decimal]] = defaultdict(list)
    confidences: dict[str, PeriodConfidence] = {}
    params = {"inn": inn, "standard": standard.value, "upto": upto}
    for row in fetch_all(_SELECT_SERIES, params, conn=conn):
        if row["status"] != "ok" or row["value"] is None:
            continue
        series[row["metric_code"]].append(row["value"])
        if row["report_date"] == upto:
            confidences[row["metric_code"]] = PeriodConfidence(row["confidence"])
    return dict(series), confidences


def _group_scores(
    metric_scores: list[MetricScore], scoring: ScoringCatalog
) -> list[GroupScore]:
    """Баллы групп и перерасчёт весов после исключения пустых групп."""
    by_group: dict[str, list[MetricScore]] = defaultdict(list)
    for item in metric_scores:
        by_group[item.group_code].append(item)

    raw: list[tuple[str, Decimal | None, int, int]] = []
    for code, policy in scoring.groups.items():
        items = by_group.get(code, [])
        used = [item for item in items if item.included and item.score is not None]
        score = (
            sum(item.score for item in used) / Decimal(len(used)) if used else None
        )
        raw.append((code, score, len(used), len(items) - len(used)))
        _ = policy

    live_weight = sum(
        scoring.groups[code].weight for code, score, _, _ in raw if score is not None
    )
    result: list[GroupScore] = []
    for code, score, used, excluded in raw:
        nominal = scoring.groups[code].weight
        effective = (
            nominal / live_weight if score is not None and live_weight > 0 else Decimal(0)
        )
        result.append(
            GroupScore(
                code=code,
                name=scoring.groups[code].name,
                score=score,
                nominal_weight=nominal,
                effective_weight=effective,
                metrics_used=used,
                metrics_excluded=excluded,
            )
        )
    return result


def _total_score(groups: list[GroupScore]) -> Decimal | None:
    """Взвешенная сумма баллов групп."""
    live = [item for item in groups if item.score is not None]
    if not live:
        return None
    return sum(item.score * item.effective_weight for item in live)


def _stop_factor(
    metric_scores: list[MetricScore],
    catalog: MetricsCatalog,
    scoring: ScoringCatalog,
) -> tuple[StopFactorPolicy | None, list[str]]:
    """Находит сработавший стоп-фактор с самым тяжёлым последствием."""
    triggered: list[tuple[StopFactorPolicy, str]] = []
    values = {item.metric_code: item.value for item in metric_scores}
    for policy in scoring.stop_factors:
        for code in policy.metrics:
            metric: MetricDef | None = catalog.get(code)
            value = values.get(code)
            if metric is None or metric.stop_factor is None or value is None:
                continue
            if metric.stop_factor.triggered(value):
                triggered.append((policy, code))
                break
    if not triggered:
        return None, []
    order = {StopEffect.LOWEST_CLASS: 0, StopEffect.CAP_AT_CLASS: 1}
    triggered.sort(key=lambda item: order[item[0].effect])
    return triggered[0][0], [code for _, code in triggered]


def _apply_stop_factor(
    class_code: str, policy: StopFactorPolicy | None, scoring: ScoringCatalog
) -> str:
    """Применяет последствие стоп-фактора к классу."""
    if policy is None:
        return class_code
    if policy.effect is StopEffect.LOWEST_CLASS:
        return scoring.lowest_class
    # Ограничение снижает класс только если тот выше потолка; ниже не поднимает.
    if (
        policy.effect is StopEffect.CAP_AT_CLASS
        and policy.cap
        and scoring.rank_of(class_code) < scoring.rank_of(policy.cap)
    ):
        return policy.cap
    return class_code


def _confidence(
    groups: list[GroupScore],
    confidences: dict[str, PeriodConfidence],
    flags: list[FlagHit],
    scoring: ScoringCatalog,
) -> tuple[Confidence, list[str]]:
    """Уверенность в оценке; на класс не влияет."""
    reasons: list[str] = []
    policy = scoring.confidence

    rule = policy.rule("incomplete_group")
    if rule is not None and rule.threshold is not None:
        for group in groups:
            total = group.metrics_used + group.metrics_excluded
            if total and Decimal(group.metrics_excluded) / Decimal(total) > rule.threshold:
                reasons.append(
                    f"в группе «{group.name}» не рассчитано "
                    f"{group.metrics_excluded} показателей из {total}"
                )
                break

    unverified = sorted(
        code
        for code, level in confidences.items()
        if level is not PeriodConfidence.VERIFIED
    )
    if unverified:
        reasons.append(
            "показатели опираются на периоды, не проверенные блокирующими контролями: "
            + ", ".join(unverified[:5])
        )

    lowering = [item.code for item in flags if item.lowers_confidence]
    if lowering:
        reasons.append("сработали флаги: " + ", ".join(lowering))

    return policy.level_after(len(reasons)), reasons


def assess(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
    standard: Standard = Standard.RSBU,
    catalog: MetricsCatalog | None = None,
    scoring: ScoringCatalog | None = None,
    flags_catalog: FlagsCatalog | None = None,
    thresholds: Thresholds | None = None,
) -> Assessment | None:
    """Считает оценку финансового состояния за период."""
    catalog = catalog if catalog is not None else load_metrics()
    scoring = scoring if scoring is not None else load_scoring()
    flags_catalog = flags_catalog if flags_catalog is not None else load_flags()
    thresholds = thresholds if thresholds is not None else load_thresholds()

    target = report_date or latest_period(inn, conn, standard)
    if target is None:
        logger.info("для ИНН %s нет рассчитанных показателей", inn)
        return None

    series, confidences = load_metric_series(inn, target, conn, standard)
    reporting_type = reporting_type_of(inn, target, conn, standard)
    jump = thresholds.jump_detection.factor

    metric_scores = [
        score_metric(
            metric,
            series.get(metric.code, []),
            scoring.metric_score,
            jump,
            scoring.calibration_points.scale_for(metric.code),
        )
        for metric in catalog.for_type(reporting_type)
    ]

    groups = _group_scores(metric_scores, scoring)
    total = _total_score(groups)
    by_score = scoring.class_for(total) if total is not None else scoring.require_class(
        scoring.lowest_class
    )

    policy, triggered = _stop_factor(metric_scores, catalog, scoring)
    final_code = _apply_stop_factor(by_score.code, policy, scoring)

    period_values = load_period_values(inn, conn, standard).get(target)
    flags = (
        evaluate_flags(period_values.values, thresholds.constants, flags_catalog)
        if period_values is not None
        else []
    )

    confidence, reasons = _confidence(groups, confidences, flags, scoring)

    assessment = Assessment(
        inn=inn,
        standard=standard,
        report_date=target,
        total_score=total,
        class_code=final_code,
        class_name=scoring.require_class(final_code).name,
        class_before_stop=by_score.code,
        stop_factor_code=policy.code if policy is not None else None,
        stop_factor_effect=policy.effect if policy is not None else StopEffect.NONE,
        confidence=confidence,
        confidence_reasons=reasons,
        groups=groups,
        metrics=metric_scores,
        flags=flags,
        metrics_version=catalog.version,
        scoring_version=scoring.version,
        flags_version=flags_catalog.version,
    )
    logger.info("оценка: %s (стоп-факторы: %s)", assessment.summary(), triggered or "нет")
    return assessment
