"""Запись оценки со всем разложением одной транзакцией."""

import json
import logging
from datetime import date

from finlib.db import PgConnection, cursor, execute, execute_many, fetch_all, fetch_one
from finlib.scoring.engine import Assessment
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_UPSERT_ASSESSMENT = """
INSERT INTO assessment (
    inn, standard, report_date, total_score, class_code, class_name, class_before_stop,
    stop_factor_code, stop_factor_effect, confidence, confidence_reasons,
    metrics_version, scoring_version, flags_version
) VALUES (
    %(inn)s, %(standard)s, %(report_date)s, %(total_score)s, %(class_code)s, %(class_name)s,
    %(class_before_stop)s, %(stop_factor_code)s, %(stop_factor_effect)s, %(confidence)s,
    %(confidence_reasons)s, %(metrics_version)s, %(scoring_version)s, %(flags_version)s
)
ON CONFLICT (inn, standard, report_date) DO UPDATE SET
    total_score = EXCLUDED.total_score,
    class_code = EXCLUDED.class_code,
    class_name = EXCLUDED.class_name,
    class_before_stop = EXCLUDED.class_before_stop,
    stop_factor_code = EXCLUDED.stop_factor_code,
    stop_factor_effect = EXCLUDED.stop_factor_effect,
    confidence = EXCLUDED.confidence,
    confidence_reasons = EXCLUDED.confidence_reasons,
    metrics_version = EXCLUDED.metrics_version,
    scoring_version = EXCLUDED.scoring_version,
    flags_version = EXCLUDED.flags_version,
    computed_at = now()
RETURNING id
"""

_INSERT_GROUP = """
INSERT INTO assessment_group (
    assessment_id, group_code, group_name, score, nominal_weight, effective_weight,
    metrics_used, metrics_excluded
) VALUES (
    %(assessment_id)s, %(group_code)s, %(group_name)s, %(score)s, %(nominal_weight)s,
    %(effective_weight)s, %(metrics_used)s, %(metrics_excluded)s
)
"""

_INSERT_METRIC = """
INSERT INTO assessment_metric (
    assessment_id, metric_code, group_code, value, score, level_score, dynamics_score,
    periods_used, included, exclusion_reason
) VALUES (
    %(assessment_id)s, %(metric_code)s, %(group_code)s, %(value)s, %(score)s,
    %(level_score)s, %(dynamics_score)s, %(periods_used)s, %(included)s, %(exclusion_reason)s
)
"""

_INSERT_FLAG = """
INSERT INTO assessment_flag (
    assessment_id, flag_code, flag_name, level, affects_class, message, details
) VALUES (
    %(assessment_id)s, %(flag_code)s, %(flag_name)s, %(level)s, %(affects_class)s,
    %(message)s, %(details)s
)
"""


def save_assessment(assessment: Assessment, conn: PgConnection) -> int:
    """Пишет оценку и всё разложение; повторный расчёт заменяет прежнее."""
    params = {
        "inn": assessment.inn,
        "standard": assessment.standard.value,
        "report_date": assessment.report_date,
        "total_score": assessment.total_score,
        "class_code": assessment.class_code,
        "class_name": assessment.class_name,
        "class_before_stop": assessment.class_before_stop,
        "stop_factor_code": assessment.stop_factor_code,
        "stop_factor_effect": assessment.stop_factor_effect.value,
        "confidence": assessment.confidence.value,
        "confidence_reasons": json.dumps(assessment.confidence_reasons, ensure_ascii=False),
        "metrics_version": assessment.metrics_version,
        "scoring_version": assessment.scoring_version,
        "flags_version": assessment.flags_version,
    }
    with cursor(conn, dict_rows=False) as cur:
        cur.execute(_UPSERT_ASSESSMENT, params)
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("не удалось записать оценку")
    assessment_id = int(row[0])

    # Разложение переписывается целиком: это снимок расчёта, а не история.
    for table in ("assessment_group", "assessment_metric", "assessment_flag"):
        execute(f"DELETE FROM {table} WHERE assessment_id = %(id)s", {"id": assessment_id},
                conn=conn)

    execute_many(
        _INSERT_GROUP,
        [
            {
                "assessment_id": assessment_id,
                "group_code": item.code,
                "group_name": item.name,
                "score": item.score,
                "nominal_weight": item.nominal_weight,
                "effective_weight": item.effective_weight,
                "metrics_used": item.metrics_used,
                "metrics_excluded": item.metrics_excluded,
            }
            for item in assessment.groups
        ],
        conn=conn,
    )
    execute_many(
        _INSERT_METRIC,
        [
            {
                "assessment_id": assessment_id,
                "metric_code": item.metric_code,
                "group_code": item.group_code,
                "value": item.value,
                "score": item.score,
                "level_score": item.level,
                "dynamics_score": item.dynamics,
                "periods_used": item.periods_used,
                "included": item.included,
                "exclusion_reason": item.exclusion_reason,
            }
            for item in assessment.metrics
        ],
        conn=conn,
    )
    execute_many(
        _INSERT_FLAG,
        [
            {
                "assessment_id": assessment_id,
                "flag_code": item.code,
                "flag_name": item.name,
                "level": item.level,
                "affects_class": item.affects_class,
                "message": item.message,
                "details": json.dumps(item.details, ensure_ascii=False, default=str),
            }
            for item in assessment.flags
        ],
        conn=conn,
    )
    logger.info("оценка записана: %s", assessment.summary())
    return assessment_id


def load_assessment(
    inn: str,
    report_date: date,
    conn: PgConnection | None = None,
    standard: Standard = Standard.RSBU,
) -> dict | None:
    """Читает оценку вместе с разложением: чем защищать вопрос «почему класс такой»."""
    header = fetch_one(
        "SELECT * FROM assessment WHERE inn = %(i)s AND standard = %(s)s AND report_date = %(d)s",
        {"i": inn, "s": standard.value, "d": report_date},
        conn=conn,
    )
    if header is None:
        return None
    params = {"id": header["id"]}
    header["groups"] = fetch_all(
        "SELECT * FROM assessment_group WHERE assessment_id = %(id)s ORDER BY group_code",
        params,
        conn=conn,
    )
    header["metrics"] = fetch_all(
        "SELECT * FROM assessment_metric WHERE assessment_id = %(id)s ORDER BY metric_code",
        params,
        conn=conn,
    )
    header["flags"] = fetch_all(
        "SELECT * FROM assessment_flag WHERE assessment_id = %(id)s ORDER BY flag_code",
        params,
        conn=conn,
    )
    return header
