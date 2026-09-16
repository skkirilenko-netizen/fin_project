"""Регрессионный прогон набора организаций (задача 17).

Два контура, и отвечают они на разные вопросы.

**Быстрый** проводит каждую организацию через расчётный слой без модели:
загрузка, контроли качества, показатели, флаги, сигналы, класс. Минуты на весь
набор, поэтому запускается при каждой правке методики — это и есть
регрессионный прогон.

**Полный** добавляет текстовую часть и сборку документа. Обращение к модели
занимает минуты на организацию, и на пятидесяти организациях это часы, поэтому
контур запускается перед приёмкой этапа и для сравнения схем в задаче 18.

Отчёт у обоих один, и в шапке его стоят контролируемые параметры прогона:
версия кода, модель и версии всех справочников. Без них сравнение двух
прогонов ничего не значит — расхождение с одинаковой вероятностью означает
и правку методики, и смену модели.

Состав набора — `eval/regression_set.yaml`. Признаки покрытия там не объявлены,
а измеряются здесь по базе: объявленный признак описывал бы намерение,
а не отчётность.

    make regression            быстрый контур
    make regression-full       полный контур
    uv run python eval/regression_run.py --contour fast --fetch
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.db import fetch_all
from finlib.metrics.formula import NotCalculableReason
from finlib.pipeline import PipelineError, Stage, analyze
from finlib.standards import Standard
from finlib.version import code_version

logger = logging.getLogger(__name__)


class Contour(StrEnum):
    """Что входит в прогон."""

    FAST = "fast"
    FULL = "full"


CONTOUR_NAMES: dict[Contour, str] = {
    Contour.FAST: "быстрый: расчётный слой без модели",
    Contour.FULL: "полный: расчётный слой и текстовая часть",
}


# --- состав набора -----------------------------------------------------------


class SetEntry(BaseModel):
    """Организация набора: ИНН и то, зачем она включена."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inn: str = Field(pattern=r"^\d{10}$|^\d{12}$")
    reason: str = Field(min_length=1)


class SizeBound(BaseModel):
    """Размерная группа: верхняя граница выручки или её отсутствие."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    max_revenue: Decimal | None = None


class SizeGroups(BaseModel):
    """Разбивка набора по размеру; в расчёте показателей не участвует."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str = Field(min_length=1)
    unit_code: str = Field(pattern=r"^\d{3}$")
    bounds: tuple[SizeBound, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        """Границы идут по возрастанию, последняя группа открыта сверху."""
        limits = [item.max_revenue for item in self.bounds]
        if limits[-1] is not None:
            raise ValueError("последняя размерная группа обязана быть открытой сверху")
        named = [item for item in limits[:-1] if item is None]
        if named:
            raise ValueError("верхняя граница не задана у группы, кроме последней")
        if limits[:-1] != sorted(limits[:-1]):
            raise ValueError("границы размерных групп идут не по возрастанию")
        return self

    def group_of(self, revenue: Decimal | None) -> SizeBound | None:
        """Группа по выручке; без раскрытой выручки группы нет.

        Угадывать размер по валюте баланса мы не будем: у транзитной структуры
        активы не описывают ни оборот, ни размер деятельности.
        """
        if revenue is None:
            return None
        for item in self.bounds:
            if item.max_revenue is None or revenue <= item.max_revenue:
                return item
        return self.bounds[-1]  # pragma: no cover — последняя открыта сверху


class CoverageCodes(BaseModel):
    """Коды методики, по которым опознаются признаки покрытия."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    holding_flag: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class RegressionSet(BaseModel):
    """Состав регрессионного набора."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    organizations: tuple[SetEntry, ...] = Field(min_length=1)
    coverage: CoverageCodes
    size_groups: SizeGroups

    @model_validator(mode="after")
    def _check_set(self) -> Self:
        """ИНН не повторяются, а названный флаг существует в методике."""
        seen = [item.inn for item in self.organizations]
        if len(set(seen)) != len(seen):
            raise ValueError("один и тот же ИНН включён в набор дважды")
        from finlib.scoring.definitions import load_flags

        known = {item.code for item in load_flags().flags}
        if self.coverage.holding_flag not in known:
            raise ValueError(
                f"флаг «{self.coverage.holding_flag}» в методике не объявлен: "
                "набор измеряет покрытие по коду, которого нет"
            )
        return self

    @property
    def inns(self) -> list[str]:
        """ИНН набора в порядке файла."""
        return [item.inn for item in self.organizations]


def default_set_path() -> Path:
    """Путь к составу набора."""
    return Path(__file__).resolve().parent / "regression_set.yaml"


@lru_cache(maxsize=4)
def load_set(path: Path | None = None) -> RegressionSet:
    """Читает состав регрессионного набора."""
    source = Path(path) if path is not None else default_set_path()
    return RegressionSet.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )


# --- прогон ------------------------------------------------------------------


@dataclass
class OrgRun:
    """Итог прогона одной организации."""

    inn: str
    name: str
    ok: bool
    seconds: float
    stage: str | None = None
    reason: str | None = None
    report_date: str | None = None
    class_code: str | None = None
    no_class_reason: str | None = None
    confidence: str | None = None
    stop_factor: str | None = None
    signals: int = 0
    flags: int = 0
    quarantined: int = 0
    attempts: int | None = None
    document: str | None = None


_ASSESSMENT = """
SELECT a.report_date, a.class_code, a.no_class_reason, a.confidence,
       a.stop_factor_code,
       (SELECT count(*) FROM assessment_signal s WHERE s.assessment_id = a.id) AS signals,
       (SELECT count(*) FROM assessment_flag f WHERE f.assessment_id = a.id) AS flags
FROM assessment a
WHERE a.inn = %(inn)s AND a.standard = %(standard)s
ORDER BY a.report_date DESC
LIMIT 1
"""

# Попытка, на которой приняли ответ модели в этом прогоне. Отбор по времени
# обязателен: без него в метрику попала бы попытка прошлого прогона, и прогон
# отчитывался бы чужим результатом. Записи тестов в счёт не идут — они
# описывают не работу системы.
_ATTEMPTS = """
SELECT attempt FROM llm_log
WHERE inn = %(inn)s AND NOT is_test AND verified AND created_at >= %(since)s
ORDER BY id DESC LIMIT 1
"""

_ORG_NAME = "SELECT coalesce(short_name, name, inn) AS name FROM organization WHERE inn = %(inn)s"


def run_one(entry: SetEntry, contour: Contour, *, fetch: bool = False) -> OrgRun:
    """Проводит одну организацию через цикл и собирает её итог.

    Неудача одной организации прогон не останавливает: набор затем и нужен,
    чтобы увидеть все отказы разом. Но и молча она не проходит — этап
    и причина попадают в отчёт отдельными графами.
    """
    started = time.monotonic()
    since = datetime.now()
    ok, stage, reason, document = True, None, None, None
    if not fetch and not _is_loaded(entry.inn):
        # Иначе организация останавливалась бы на расчёте показателей с
        # причиной «ни одного не рассчитано», и по отчёту нельзя было бы
        # отличить пустую отчётность от незагруженной.
        return OrgRun(
            inn=entry.inn,
            name=_name_of(entry.inn),
            ok=False,
            seconds=0.0,
            stage=Stage.FETCH.value,
            reason="отчётность не загружена, а прогон идёт без обращения "
            "к источнику: повторите с --fetch",
        )
    try:
        result = analyze(
            entry.inn,
            with_llm=contour is Contour.FULL,
            from_cache_only=not fetch,
        )
        document = str(result.document) if result.document else None
    except PipelineError as exc:
        ok, stage, reason = False, exc.stage.value, exc.reason
    except Exception as exc:  # noqa: BLE001 — сбой прогонщика тоже итог прогона
        logger.exception("прогон ИНН %s прерван", entry.inn)
        ok, stage, reason = False, "сбой прогонщика", str(exc)
    seconds = round(time.monotonic() - started, 1)

    run = OrgRun(
        inn=entry.inn,
        name=_name_of(entry.inn),
        ok=ok,
        seconds=seconds,
        stage=stage,
        reason=reason,
        document=document,
    )
    _fill_from_db(run, contour, since)
    return run


def _name_of(inn: str) -> str:
    """Наименование организации; до загрузки его ещё нет."""
    found = fetch_all(_ORG_NAME, {"inn": inn})
    return found[0]["name"] if found else inn


def _is_loaded(inn: str) -> bool:
    """Есть ли у организации загруженная отчётность."""
    found = fetch_all(
        "SELECT count(*) AS n FROM src_file WHERE inn = %(inn)s", {"inn": inn}
    )
    return bool(found and int(found[0]["n"]))


def _fill_from_db(run: OrgRun, contour: Contour, since: datetime) -> None:
    """Дополняет итог тем, что записал расчёт."""
    params = {"inn": run.inn, "standard": Standard.RSBU.value}
    found = fetch_all(_ASSESSMENT, params)
    if found:
        row = found[0]
        run.report_date = f"{row['report_date']:%d.%m.%Y}"
        run.class_code = row["class_code"]
        run.no_class_reason = row["no_class_reason"]
        run.confidence = row["confidence"]
        run.stop_factor = row["stop_factor_code"]
        run.signals = int(row["signals"])
        run.flags = int(row["flags"])
    quarantined = fetch_all(
        "SELECT count(*) AS n FROM src_file WHERE inn = %(inn)s AND status = 'quarantine'",
        {"inn": run.inn},
    )
    run.quarantined = int(quarantined[0]["n"]) if quarantined else 0
    if contour is Contour.FULL:
        attempts = fetch_all(_ATTEMPTS, {"inn": run.inn, "since": since})
        run.attempts = int(attempts[0]["attempt"]) if attempts else None


# --- покрытие ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageItem:
    """Одно измерение покрытия: сколько организаций его дают."""

    name: str
    count: int
    detail: str


_BY_REPORTING_TYPE = """
SELECT DISTINCT inn, reporting_type FROM src_file
WHERE inn = ANY(%(inns)s) AND is_actual
"""

_NEGATIVE_EQUITY = """
SELECT DISTINCT inn FROM fact_report
WHERE inn = ANY(%(inns)s) AND line_code = '1300' AND value < 0
"""

_WITH_FLAG = """
SELECT DISTINCT a.inn FROM assessment_flag f
JOIN assessment a ON a.id = f.assessment_id
WHERE a.inn = ANY(%(inns)s) AND f.flag_code = %(flag)s
"""

_QUARANTINED = """
SELECT DISTINCT inn FROM src_file
WHERE inn = ANY(%(inns)s) AND status = 'quarantine'
"""

_SIGN_CHANGE = """
SELECT DISTINCT inn FROM metric_value
WHERE inn = ANY(%(inns)s) AND reason_code = %(reason)s
"""

_ACTIVITIES = """
SELECT left(okved, 2) AS class, count(*) AS n FROM organization
WHERE inn = ANY(%(inns)s) AND okved IS NOT NULL
GROUP BY 1 ORDER BY 1
"""

# Выручка за последний отчётный период каждой организации: по ней определяется
# размерная группа. Сравнительные колонки не берутся — размер описывает
# отчётный период, а не тот, что пришёл справочно.
_REVENUE = """
SELECT DISTINCT ON (inn) inn, value FROM fact_report
WHERE inn = ANY(%(inns)s) AND line_code = '2110' AND value IS NOT NULL
ORDER BY inn, report_date DESC
"""


def coverage(regression_set: RegressionSet) -> list[CoverageItem]:
    """Измеряет покрытие набора по базе, а не по объявлениям файла состава."""
    inns = regression_set.inns
    params = {"inns": inns}
    found: list[CoverageItem] = []

    by_type: dict[str, set[str]] = {}
    for row in fetch_all(_BY_REPORTING_TYPE, params):
        by_type.setdefault(row["reporting_type"], set()).add(row["inn"])
    for code, name in (("full", "Полный набор форм"), ("simplified", "Упрощённый набор форм")):
        hit = by_type.get(code, set())
        found.append(CoverageItem(name, len(hit), _listed(hit)))

    simple = (
        ("Отрицательный собственный капитал", _NEGATIVE_EQUITY, params),
        (
            "Признаки холдинговой структуры",
            _WITH_FLAG,
            {**params, "flag": regression_set.coverage.holding_flag},
        ),
        ("Отбракованные комплекты отчётности", _QUARANTINED, params),
        (
            "Смена знака показателя между периодами",
            _SIGN_CHANGE,
            {**params, "reason": NotCalculableReason.SIGN_CHANGE.value},
        ),
    )
    for name, query, args in simple:
        hit = {row["inn"] for row in fetch_all(query, args)}
        found.append(CoverageItem(name, len(hit), _listed(hit)))

    found.append(_size_coverage(regression_set))

    classes = fetch_all(_ACTIVITIES, params)
    listed = ", ".join(f"{row['class']} ({row['n']})" for row in classes)
    found.append(
        CoverageItem(
            "Виды деятельности (классы ОКВЭД)",
            len(classes),
            listed or "ОКВЭД не заполнен ни у одной организации",
        )
    )
    return found


def _size_coverage(regression_set: RegressionSet) -> CoverageItem:
    """Разбивка набора по размерным группам."""
    revenue = {
        row["inn"]: row["value"] for row in fetch_all(_REVENUE, {"inns": regression_set.inns})
    }
    counted: dict[str, int] = {}
    unknown = 0
    for inn in regression_set.inns:
        group = regression_set.size_groups.group_of(revenue.get(inn))
        if group is None:
            unknown += 1
            continue
        counted[group.name] = counted.get(group.name, 0) + 1
    detail = ", ".join(f"{name} — {count}" for name, count in counted.items())
    if unknown:
        detail = f"{detail}; без раскрытой выручки — {unknown}" if detail else (
            f"без раскрытой выручки — {unknown}"
        )
    return CoverageItem("Размерные группы", len(counted), detail or "нет данных")


def _listed(inns: set[str]) -> str:
    """ИНН измерения одной строкой; пусто — измерение не покрыто."""
    return ", ".join(sorted(inns)) if inns else "не покрыто"


# --- метрики прогона ---------------------------------------------------------

_QUALITY_FIRINGS = """
SELECT check_code, severity, count(*) AS n FROM dq_log
WHERE inn = ANY(%(inns)s) AND status = 'fail'
GROUP BY check_code, severity ORDER BY n DESC, check_code
"""

# Замечания постпроверки по видам: считаются только отклонённые ответы
# текущего прогона, то есть записанные после его начала.
_TEXT_FIRINGS = """
SELECT foreign_numbers FROM llm_log
WHERE inn = ANY(%(inns)s) AND NOT is_test AND NOT verified
  AND created_at >= %(since)s AND foreign_numbers IS NOT NULL
"""


def run_metrics(
    runs: list[OrgRun], regression_set: RegressionSet, contour: Contour, since: datetime
) -> dict:
    """Метрики прогона ровно теми величинами, которых требует задача 17."""
    inns = regression_set.inns
    finished = [item for item in runs if item.ok]
    with_class = [item for item in finished if item.class_code]
    classes: dict[str, int] = {}
    for item in with_class:
        classes[item.class_code] = classes.get(item.class_code, 0) + 1

    seconds = [item.seconds for item in runs]
    metrics: dict = {
        "организаций": len(runs),
        "прошли цикл": len(finished),
        "остановились": len(runs) - len(finished),
        "остановки по этапам": _by_stage(runs),
        "классы": dict(sorted(classes.items())),
        "без класса": len(finished) - len(with_class),
        "доля без класса": _share(len(finished) - len(with_class), len(finished)),
        "контроли качества": [
            {
                "контроль": row["check_code"],
                "уровень": row["severity"],
                "срабатываний": int(row["n"]),
            }
            for row in fetch_all(_QUALITY_FIRINGS, {"inns": inns})
        ],
        "секунд на организацию": {
            "среднее": round(sum(seconds) / len(seconds), 1) if seconds else 0.0,
            "наибольшее": max(seconds) if seconds else 0.0,
            "всего": round(sum(seconds), 1),
        },
    }
    if contour is Contour.FULL:
        metrics.update(_text_metrics(runs, inns, since))
    return metrics


def _text_metrics(runs: list[OrgRun], inns: list[str], since: datetime) -> dict:
    """Метрики текстового слоя: они есть только у полного контура.

    Доля документов считается от тех организаций, что дошли до текстовой части:
    организация, остановленная на контролях качества, о поведении модели
    не говорит ничего, и включать её в знаменатель значило бы смешивать
    два разных отказа.
    """
    # Дошедшей до текстовой части считается и та организация, что остановилась
    # на сборке документа: её текст постпроверку прошёл, а документ не собрался
    # по другой причине — например, по несогласованности с самим собой.
    after_text = {Stage.CONCLUSION.value, Stage.DOCUMENT.value}
    reached = [item for item in runs if item.ok or item.stage in after_text]
    documents = [item for item in runs if item.document]
    attempts = [item.attempts for item in runs if item.attempts is not None]
    violations: dict[str, int] = {}
    for row in fetch_all(_TEXT_FIRINGS, {"inns": inns, "since": since}):
        payload = row["foreign_numbers"]
        if not isinstance(payload, dict):
            continue
        for section, items in payload.items():
            for item in items or ():
                kind = item.get("violation") or item.get("rule") or section
                violations[kind] = violations.get(kind, 0) + 1
    return {
        "дошли до текстовой части": len(reached),
        "документов собрано": len(documents),
        "доля прошедших контроли текста": _share(len(documents), len(reached)),
        "замечания постпроверки": dict(
            sorted(violations.items(), key=lambda item: -item[1])
        ),
        "попыток до принятия": {
            "среднее": round(sum(attempts) / len(attempts), 2) if attempts else None,
            "наибольшее": max(attempts) if attempts else None,
        },
    }


def _by_stage(runs: list[OrgRun]) -> dict[str, int]:
    """Сколько организаций остановилось на каждом этапе."""
    found: dict[str, int] = {}
    for item in runs:
        if item.stage:
            found[item.stage] = found.get(item.stage, 0) + 1
    return found


def _share(part: int, whole: int) -> str:
    """Доля в процентах; знаменатель ноль — доли нет."""
    return "—" if not whole else f"{part / whole * 100:.1f} %".replace(".", ",")


# --- отчёт -------------------------------------------------------------------


@dataclass
class Report:
    """Отчёт о прогоне: параметры, организации, покрытие, метрики."""

    started: datetime
    parameters: dict
    organizations: list[dict]
    coverage: list[dict]
    metrics: dict = field(default_factory=dict)


def parameters(regression_set: RegressionSet, contour: Contour, started: datetime) -> dict:
    """Контролируемые параметры прогона.

    Версия модели входит в их состав наравне с версиями справочников: смена
    модели меняет поведение текстового слоя целиком, и прогон, сделанный другой
    моделью, с прежним несопоставим.
    """
    from finlib.metrics.definitions import load_metrics
    from finlib.normalize.lines import load_lines
    from finlib.quality.thresholds import load_thresholds
    from finlib.report.policy import load_policy
    from finlib.scoring.definitions import load_flags, load_scoring
    from finlib.scoring.signals import load_signals

    return {
        "прогон": f"{started:%d.%m.%Y %H:%M}",
        "контур": CONTOUR_NAMES[contour],
        "версия кода": code_version(),
        "модель": settings.llm_model if contour is Contour.FULL else "не привлекалась",
        "состав набора": regression_set.version,
        "справочники": {
            "metrics": load_metrics().version,
            "scoring": load_scoring().version,
            "flags": load_flags().version,
            "signals": load_signals().version,
            "report": load_policy().version,
            "lines": load_lines().version,
            "thresholds": load_thresholds().version,
        },
    }


def render(report: Report) -> str:
    """Отчёт таблицей, пригодной для представления руководству."""
    lines = ["# Регрессионный прогон", "", "## Параметры прогона", ""]
    for name, value in report.parameters.items():
        if isinstance(value, dict):
            listed = ", ".join(f"{key} {item}" for key, item in value.items())
            lines.append(f"- {name}: {listed}")
        else:
            lines.append(f"- {name}: {value}")

    lines += ["", "## Организации", ""]
    lines.append(
        "| ИНН | Организация | Период | Итог | Класс | Уверенность | "
        "Сигналов | Флагов | Карантин | Секунд |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for item in report.organizations:
        outcome = "пройдено" if item["ok"] else f"остановлено: {item['stage']}"
        lines.append(
            f"| {item['inn']} | {item['name']} | {item['report_date'] or '—'} | "
            f"{outcome} | {item['class_code'] or 'нет'} | {item['confidence'] or '—'} | "
            f"{item['signals']} | {item['flags']} | {item['quarantined']} | "
            f"{item['seconds']} |"
        )

    lines += ["", "## Покрытие набора", "", "| Измерение | Организаций | Состав |", "|---|---|---|"]
    for item in report.coverage:
        lines.append(f"| {item['name']} | {item['count']} | {item['detail']} |")

    lines += ["", "## Метрики прогона", ""]
    lines += _metric_lines(report.metrics)

    stopped = [item for item in report.organizations if not item["ok"]]
    if stopped:
        lines += ["", "## Остановки", ""]
        for item in stopped:
            lines.append(
                f"- {item['inn']} {item['name']}: этап «{item['stage']}» — {item['reason']}"
            )
    return "\n".join(lines) + "\n"


def _metric_lines(metrics: dict, prefix: str = "") -> list[str]:
    """Метрики списком; вложенные словари разворачиваются с отступом.

    Пустая величина печатается словом «нет»: заголовок без содержимого
    читается как недосчитанная метрика, а не как отсутствие срабатываний.
    """
    lines: list[str] = []
    for name, value in metrics.items():
        if isinstance(value, dict | list) and not value:
            lines.append(f"{prefix}- {name}: нет")
        elif isinstance(value, dict):
            lines.append(f"{prefix}- {name}:")
            lines += _metric_lines(value, prefix + "  ")
        elif isinstance(value, list):
            lines.append(f"{prefix}- {name}:")
            for item in value:
                listed = ", ".join(f"{key}: {part}" for key, part in item.items())
                lines.append(f"{prefix}  - {listed}")
        else:
            lines.append(f"{prefix}- {name}: {value if value is not None else '—'}")
    return lines


def output_dir() -> Path:
    """Каталог отчётов о прогонах: данные, а не исходники."""
    return settings.output_dir / "regression"


def save(report: Report, contour: Contour) -> tuple[Path, Path]:
    """Пишет отчёт машиночитаемым и читаемым видом рядом.

    JSON нужен для сравнения двух прогонов между собой — ради этого набор
    и заводится; markdown нужен человеку.
    """
    target = output_dir()
    target.mkdir(parents=True, exist_ok=True)
    stem = f"{report.started:%Y-%m-%d_%H%M}_{contour.value}"
    machine = target / f"{stem}.json"
    human = target / f"{stem}.md"
    machine.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    human.write_text(render(report), encoding="utf-8")
    return machine, human


def run(
    contour: Contour = Contour.FAST,
    *,
    fetch: bool = False,
    regression_set: RegressionSet | None = None,
    only: list[str] | None = None,
) -> Report:
    """Прогоняет набор и собирает отчёт."""
    regression_set = regression_set if regression_set is not None else load_set()
    entries = [
        item
        for item in regression_set.organizations
        if only is None or item.inn in set(only)
    ]
    if not entries:
        raise ValueError("ни одна из названных организаций в набор не входит")
    # Покрытие и метрики считаются по прогнанным организациям, а не по всему
    # составу: иначе отчёт о части набора выглядел бы отчётом о наборе.
    subset = (
        regression_set
        if only is None
        else regression_set.model_copy(update={"organizations": tuple(entries)})
    )
    started = datetime.now()
    runs: list[OrgRun] = []
    for number, entry in enumerate(entries, start=1):
        logger.info("[%d/%d] ИНН %s", number, len(entries), entry.inn)
        runs.append(run_one(entry, contour, fetch=fetch))

    found = parameters(subset, contour, started)
    if only is not None:
        found["состав набора"] = (
            f"{regression_set.version}, прогнана часть: "
            f"{len(entries)} из {len(regression_set.organizations)}"
        )
    return Report(
        started=started,
        parameters=found,
        organizations=[asdict(item) for item in runs],
        coverage=[asdict(item) for item in coverage(subset)],
        metrics=run_metrics(runs, subset, contour, started),
    )


def main(argv: list[str] | None = None) -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contour",
        choices=[item.value for item in Contour],
        default=Contour.FAST.value,
        help="fast — без модели, full — с генерацией текста",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="обратиться к источнику; по умолчанию прогон идёт по загруженным данным",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="ИНН",
        help="прогнать только названные организации набора",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    contour = Contour(args.contour)
    report = run(contour, fetch=args.fetch, only=args.only)
    machine, human = save(report, contour)
    print(render(report))
    print(f"Отчёт: {human}")
    print(f"Машиночитаемый вид: {machine}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
