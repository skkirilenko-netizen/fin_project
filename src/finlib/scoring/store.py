"""Запись оценки со всем разложением одной транзакцией."""

import json
import logging
import re
from datetime import date

from finlib.db import PgConnection, cursor, execute, execute_many, fetch_all, fetch_one
from finlib.scoring.engine import Assessment
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_UPSERT_ASSESSMENT = """
INSERT INTO assessment (
    inn, standard, report_date, total_score, class_code, class_name, no_class_reason,
    breadth_reason,
    class_before_stop, stop_factor_code, stop_factor_effect, stop_factor_audit,
    confidence, confidence_reasons,
    metrics_version, scoring_version, flags_version
) VALUES (
    %(inn)s, %(standard)s, %(report_date)s, %(total_score)s, %(class_code)s, %(class_name)s,
    %(no_class_reason)s, %(breadth_reason)s, %(class_before_stop)s,
    %(stop_factor_code)s, %(stop_factor_effect)s, %(stop_factor_audit)s,
    %(confidence)s, %(confidence_reasons)s, %(metrics_version)s, %(scoring_version)s,
    %(flags_version)s
)
ON CONFLICT (inn, standard, report_date) DO UPDATE SET
    total_score = EXCLUDED.total_score,
    class_code = EXCLUDED.class_code,
    class_name = EXCLUDED.class_name,
    no_class_reason = EXCLUDED.no_class_reason,
    breadth_reason = EXCLUDED.breadth_reason,
    class_before_stop = EXCLUDED.class_before_stop,
    stop_factor_code = EXCLUDED.stop_factor_code,
    stop_factor_effect = EXCLUDED.stop_factor_effect,
    stop_factor_audit = EXCLUDED.stop_factor_audit,
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
    periods_used, included, exclusion_reason, exclusion_kind
) VALUES (
    %(assessment_id)s, %(metric_code)s, %(group_code)s, %(value)s, %(score)s,
    %(level_score)s, %(dynamics_score)s, %(periods_used)s, %(included)s,
    %(exclusion_reason)s, %(exclusion_kind)s
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


_INSERT_SIGNAL = """
INSERT INTO assessment_signal (
    assessment_id, signal_code, signal_name, level, value, message, details
) VALUES (
    %(assessment_id)s, %(signal_code)s, %(signal_name)s, %(level)s, %(value)s,
    %(message)s, %(details)s
)
"""


# Перенос строки справочника сворачивается, неразрывный пробел — нет: он стоит
# между разрядами числа, и обычное `" ".join(text.split())` его съедало.
# Величина при этом печаталась в формулировке иначе, чем в основании сигнала.
_FOLDED = re.compile(r"[^\S ]+")


def _one_line(text: str) -> str:
    """Свёртка многострочной формулировки в одну строку."""
    return _FOLDED.sub(" ", text).strip()


def save_assessment(assessment: Assessment, conn: PgConnection) -> int:
    """Пишет оценку и всё разложение; повторный расчёт заменяет прежнее."""
    params = {
        "inn": assessment.inn,
        "standard": assessment.standard.value,
        "report_date": assessment.report_date,
        "total_score": assessment.total_score,
        "class_code": assessment.class_code,
        "class_name": assessment.class_name,
        "no_class_reason": assessment.no_class_reason,
        "breadth_reason": assessment.breadth_reason,
        "class_before_stop": assessment.class_before_stop,
        "stop_factor_code": assessment.stop_factor_code,
        "stop_factor_effect": assessment.stop_factor_effect.value,
        "stop_factor_audit": assessment.stop_factor_audit,
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
    for table in (
        "assessment_group",
        "assessment_metric",
        "assessment_flag",
        "assessment_signal",
    ):
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
                "exclusion_kind": item.exclusion_kind.value if item.exclusion_kind else None,
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
    execute_many(
        _INSERT_SIGNAL,
        [
            {
                "assessment_id": assessment_id,
                "signal_code": item.code,
                "signal_name": item.name,
                "level": item.level.value,
                "value": item.value,
                "message": _one_line(item.message),
                "details": json.dumps(item.details, ensure_ascii=False, default=str),
            }
            for item in assessment.signals
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
