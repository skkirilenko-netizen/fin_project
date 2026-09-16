"""Регрессионный прогон набора организаций (задача 17).

Два контура, и отвечают они на разные вопросы.

**Быстрый** проводит каждую организацию через расчётный слой без модели:
загрузка, контроли качества, показатели, флаги, сигналы, класс, сборка
расчётной справки. Минуты на весь набор, поэтому запускается при каждой правке
методики — это и есть регрессионный прогон.

**Полный** добавляет текстовую часть. Обращение к модели занимает минуты
на организацию, и на полусотне организаций это часы, поэтому контур
запускается перед приёмкой этапа и для сравнения схем в задаче 18.

Отчёт у обоих один, и в шапке его стоят контролируемые параметры прогона:
версия кода, модель и версии всех справочников. Без них сравнение двух
прогонов ничего не значит — расхождение с одинаковой вероятностью означает
и правку методики, и смену модели.

**Заявленная категория — гипотеза, а не факт.** Состав (`eval/sample_40.csv`)
говорит, зачем организация включена; признаки меряются по базе после прогона
и сопоставляются с заявленным. Отчёт показывает заявленное покрытие против
фактического, а не пересказывает файл состава.

**Ожидаемый отказ — тоже успех.** Кредитные организации вне периметра
методики, и отказ по ним подтверждает правило, а не нарушает его. Ожидаемый
исход объявлен в составе, графа «итог совпал с ожиданием» сверяет его
с тем, что вышло.

    make regression            быстрый контур
    make regression-full       полный контур
    uv run python eval/regression_run.py --contour fast --fetch
"""

import argparse
import csv
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
from finlib.quality.codes import CheckCode
from finlib.standards import Standard
from finlib.version import code_version

logger = logging.getLogger(__name__)


class Contour(StrEnum):
    """Что входит в прогон."""

    FAST = "fast"
    FULL = "full"


# Этап, которым помечается сбой самого прогонщика: он не является ни отказом
# методики, ни исходом цикла.
RUNNER_FAILURE = "сбой прогонщика"

CONTOUR_NAMES: dict[Contour, str] = {
    Contour.FAST: "быстрый: расчётный слой без модели",
    Contour.FULL: "полный: расчётный слой и текстовая часть",
}


class RunStatus(StrEnum):
    """Участвует ли организация в прогоне."""

    RUN = "run"
    # Организация оставлена в составе, но в прогон не идёт: однотипные случаи
    # сверх нужного только удлиняют прогон, а нового не показывают.
    RESERVE = "reserve"


class ExpectedOutcome(StrEnum):
    """Чего мы ждём от организации."""

    ANALYSIS = "analysis"
    # Организация вне периметра методики: отказ по ней — правильный исход.
    REFUSAL = "refusal"
    # Отчётность сдана, но контроли её отбраковывают, и это ожидаемо:
    # у специализированного финансового общества или холдинговой компании
    # выручки нет по устройству, а обязательность строки 2110 — правило
    # для работающей организации. Отбраковка здесь подтверждает контроль,
    # а не опровергает его; ослаблять контроль ради таких организаций нельзя:
    # нераскрытая выручка у работающей организации — серьёзный сигнал.
    QUARANTINE_EXPECTED = "quarantine_expected"


class Feature(StrEnum):
    """Признак, измеряемый по базе после прогона."""

    SIMPLIFIED_FORMS = "simplified_forms"
    FULL_FORMS = "full_forms"
    LARGE_BALANCE = "large_balance"
    NEGATIVE_EQUITY = "negative_equity"
    LOSS_TWO_YEARS = "loss_two_years"
    SIGN_CHANGE = "sign_change"
    HOLDING_STRUCTURE = "holding_structure"
    INCOMPLETE_DISCLOSURE = "incomplete_disclosure"
    SINGLE_REPORT_YEAR = "single_report_year"
    NEAR_ZERO_REVENUE = "near_zero_revenue"
    # Выручка не раскрыта ни за один период: не «около нуля», а отсутствует.
    # Отличать эти два признака обязательно — оборот около нуля организация
    # всё-таки показала, а здесь показывать нечего.
    REVENUE_NOT_DISCLOSED = "revenue_not_disclosed"
    QUARANTINED = "quarantined"
    OUT_OF_SCOPE = "out_of_scope"


FEATURE_NAMES: dict[Feature, str] = {
    Feature.SIMPLIFIED_FORMS: "упрощённые формы",
    Feature.FULL_FORMS: "полные формы",
    Feature.LARGE_BALANCE: "баланс выше отсечки крупных",
    Feature.NEGATIVE_EQUITY: "отрицательный собственный капитал",
    Feature.LOSS_TWO_YEARS: "убыток два периода подряд",
    Feature.SIGN_CHANGE: "смена знака показателя",
    Feature.HOLDING_STRUCTURE: "флаг холдинговой структуры",
    Feature.INCOMPLETE_DISCLOSURE: "неполное раскрытие",
    Feature.SINGLE_REPORT_YEAR: "один отчётный год",
    Feature.NEAR_ZERO_REVENUE: "обороты около нуля",
    Feature.REVENUE_NOT_DISCLOSED: "выручка не раскрыта",
    Feature.QUARANTINED: "отбракованный комплект",
    Feature.OUT_OF_SCOPE: "вне периметра методики",
}

# Значения графы category_status в составе: заявленное состояние гипотезы.
# Фактическое состояние считает прогон, а не файл.
CATEGORY_STATUS = frozenset(
    {"гипотеза", "подтверждено косвенно", "подтверждено", "не подтверждено"}
)


# --- состав набора -----------------------------------------------------------


def _inn_is_valid(inn: str) -> bool:
    """Проверяет контрольные разряды ИНН.

    Опечатка в ИНН даёт не ошибку прогона, а тихий пропуск: источник просто
    не находит организацию, и набор оказывается меньше, чем считает.
    """
    digits = [int(item) for item in inn]
    if len(digits) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        control = (
            sum(w * d for w, d in zip(weights, digits[:9], strict=True)) % 11 % 10
        )
        return control == digits[9]
    if len(digits) == 12:
        first = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        second = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        eleventh = sum(w * d for w, d in zip(first, digits[:10], strict=True)) % 11 % 10
        twelfth = sum(w * d for w, d in zip(second, digits[:11], strict=True)) % 11 % 10
        return eleventh == digits[10] and twelfth == digits[11]
    return False


class SetEntry(BaseModel):
    """Организация набора: ИНН, заявленная категория и основание включения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category_id: int = Field(gt=0)
    category: str = Field(min_length=1)
    name: str = Field(min_length=1)
    inn: str = Field(pattern=r"^\d{10}$|^\d{12}$")
    ogrn: str | None = None
    reg_year: int | None = None
    reason: str = Field(min_length=1)
    category_status: str = Field(min_length=1)
    run_status: RunStatus = RunStatus.RUN
    expected_outcome: ExpectedOutcome = ExpectedOutcome.ANALYSIS

    @model_validator(mode="after")
    def _check_entry(self) -> Self:
        """ИНН сходится по контрольным разрядам, статус гипотезы известен."""
        if not _inn_is_valid(self.inn):
            raise ValueError(f"ИНН {self.inn} не сходится по контрольным разрядам")
        if self.category_status not in CATEGORY_STATUS:
            listed = ", ".join(sorted(CATEGORY_STATUS))
            raise ValueError(
                f"статус категории «{self.category_status}» неизвестен; "
                f"допустимы: {listed}"
            )
        return self

    @property
    def in_run(self) -> bool:
        """Идёт ли организация в прогон."""
        return self.run_status is RunStatus.RUN


class Category(BaseModel):
    """Категория состава и признак, которым она подтверждается."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    feature: Feature | None = None
    # Причина, по которой машинного признака нет. Категория без признака
    # и без причины — недосмотр, поэтому одно из двух обязательно.
    manual: str | None = None

    @model_validator(mode="after")
    def _check_category(self) -> Self:
        """Признак либо объявлен, либо объявлено, почему его нет."""
        if (self.feature is None) == (self.manual is None):
            raise ValueError(
                f"у категории {self.id} должно быть объявлено ровно одно: "
                "признак feature или причина manual"
            )
        return self


class Threshold(BaseModel):
    """Описательный порог признака: происхождение обязательно."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Decimal
    origin: str = Field(min_length=1)


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
        if [item for item in limits[:-1] if item is None]:
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
    """Коды методики, по которым опознаются признаки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    holding_flag: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class RegressionSet(BaseModel):
    """Настройки измерения и состав набора."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    coverage: CoverageCodes
    features: dict[str, Threshold]
    categories: tuple[Category, ...] = Field(min_length=1)
    size_groups: SizeGroups
    # Состав приходит из CSV: он правится руками и часто, а правила
    # измерения — редко и с обоснованием.
    organizations: tuple[SetEntry, ...] = Field(default=(), exclude=True)

    @model_validator(mode="after")
    def _check_set(self) -> Self:
        """Флаг существует в методике, категории и пороги согласованы."""
        from finlib.scoring.definitions import load_flags

        known = {item.code for item in load_flags().flags}
        if self.coverage.holding_flag not in known:
            raise ValueError(
                f"флаг «{self.coverage.holding_flag}» в методике не объявлен: "
                "набор измеряет покрытие по коду, которого нет"
            )
        for name in ("large_balance", "incomplete_disclosure", "near_zero_revenue"):
            if name not in self.features:
                raise ValueError(f"не объявлен порог признака «{name}»")
        ids = [item.id for item in self.categories]
        if len(set(ids)) != len(ids):
            raise ValueError("номер категории встречается дважды")
        return self

    def with_organizations(self, entries: tuple[SetEntry, ...]) -> "RegressionSet":
        """Тот же набор с иным составом: нужно для прогона части."""
        copy = self.model_copy(update={"organizations": entries})
        copy._check_organizations()
        return copy

    def _check_organizations(self) -> None:
        """Состав согласован с категориями: номер и наименование совпадают."""
        seen = [item.inn for item in self.organizations]
        duplicates = {item for item in seen if seen.count(item) > 1}
        if duplicates:
            raise ValueError(f"ИНН включён в состав дважды: {', '.join(sorted(duplicates))}")
        by_id = {item.id: item for item in self.categories}
        for entry in self.organizations:
            category = by_id.get(entry.category_id)
            if category is None:
                raise ValueError(
                    f"{entry.inn}: категории {entry.category_id} нет в настройках набора"
                )
            if category.name != entry.category:
                raise ValueError(
                    f"{entry.inn}: категория {entry.category_id} названа "
                    f"«{entry.category}», а в настройках — «{category.name}»"
                )

    def category_of(self, entry: SetEntry) -> Category:
        """Категория организации."""
        return next(item for item in self.categories if item.id == entry.category_id)

    @property
    def running(self) -> tuple[SetEntry, ...]:
        """Организации, идущие в прогон; резерв остаётся в составе."""
        return tuple(item for item in self.organizations if item.in_run)

    @property
    def inns(self) -> list[str]:
        """ИНН прогоняемых организаций."""
        return [item.inn for item in self.running]

    def threshold(self, name: str) -> Decimal:
        """Величина описательного порога."""
        return self.features[name].value


def default_set_path() -> Path:
    """Путь к настройкам набора."""
    return Path(__file__).resolve().parent / "regression_set.yaml"


def default_sample_path() -> Path:
    """Путь к составу набора."""
    return Path(__file__).resolve().parent / "sample_40.csv"


def read_sample(path: Path | None = None) -> tuple[SetEntry, ...]:
    """Читает состав набора из CSV.

    Формат — точка с запятой и кавычки вокруг полей с ней внутри: файл
    правится в таблице, а не в редакторе, и разделитель выбран под неё.
    """
    source = Path(path) if path is not None else default_sample_path()
    found: list[SetEntry] = []
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            found.append(
                SetEntry(
                    category_id=int(row["cat_id"]),
                    category=row["category"].strip(),
                    name=row["name"].strip(),
                    inn=row["inn"].strip(),
                    ogrn=row["ogrn"].strip() or None,
                    reg_year=int(row["reg_year"]) if row["reg_year"].strip() else None,
                    reason=row["expected_behavior"].strip(),
                    category_status=row["category_status"].strip(),
                    run_status=RunStatus(row["run_status"].strip()),
                    expected_outcome=ExpectedOutcome(row["expected_outcome"].strip()),
                )
            )
    return tuple(found)


@lru_cache(maxsize=4)
def load_set(path: Path | None = None, sample: Path | None = None) -> RegressionSet:
    """Читает настройки набора и его состав."""
    source = Path(path) if path is not None else default_set_path()
    settings_part = RegressionSet.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )
    return settings_part.with_organizations(read_sample(sample))


# --- прогон ------------------------------------------------------------------


@dataclass
class OrgRun:
    """Итог прогона одной организации."""

    inn: str
    name: str
    category_id: int
    category: str
    expected: str
    ok: bool
    seconds: float
    # Доходило ли дело до цикла. Организация, отчётность которой не загружена,
    # отказом методики не считается: иначе прогон без данных отчитывался бы
    # ожидаемыми отказами по всем, кому положено отказать.
    attempted: bool = True
    stage: str | None = None
    reason: str | None = None
    as_expected: bool = False
    report_date: str | None = None
    class_code: str | None = None
    no_class_reason: str | None = None
    confidence: str | None = None
    stop_factor: str | None = None
    signals: int = 0
    flags: int = 0
    sets: int = 0
    quarantined: int = 0
    attempts: int | None = None
    document: str | None = None
    features: list[str] = field(default_factory=list)
    category_confirmed: bool | None = None


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
# отчитывался бы чужим результатом. Записи тестов в счёт не идут.
_ATTEMPTS = """
SELECT attempt FROM llm_log
WHERE inn = %(inn)s AND NOT is_test AND verified AND created_at >= %(since)s
ORDER BY id DESC LIMIT 1
"""

_ORG_NAME = "SELECT coalesce(short_name, name, inn) AS name FROM organization WHERE inn = %(inn)s"

_SETS = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE status = 'quarantine') AS quarantined
FROM src_file WHERE inn = %(inn)s
"""


def run_one(entry: SetEntry, contour: Contour, *, fetch: bool = False) -> OrgRun:
    """Проводит одну организацию через цикл и собирает её итог.

    Неудача одной организации прогон не останавливает: набор затем и нужен,
    чтобы увидеть все отказы разом. Но и молча она не проходит — этап
    и причина попадают в отчёт отдельными графами.
    """
    started = time.monotonic()
    since = datetime.now()
    if not fetch and not _is_loaded(entry.inn):
        # Иначе организация останавливалась бы на расчёте показателей
        # с причиной «ни одного не рассчитано», и по отчёту нельзя было бы
        # отличить пустую отчётность от незагруженной.
        return _empty(
            entry,
            Stage.FETCH.value,
            "отчётность не загружена, а прогон идёт без обращения "
            "к источнику: повторите с --fetch",
        )

    ok, stage, reason, document = True, None, None, None
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
        ok, stage, reason = False, RUNNER_FAILURE, str(exc)
    seconds = round(time.monotonic() - started, 1)

    run = OrgRun(
        inn=entry.inn,
        name=_name_of(entry.inn) or entry.name,
        category_id=entry.category_id,
        category=entry.category,
        expected=entry.expected_outcome.value,
        ok=ok,
        seconds=seconds,
        stage=stage,
        reason=reason,
        document=document,
    )
    _fill_from_db(run, contour, since)
    return run


def _empty(entry: SetEntry, stage: str, reason: str) -> OrgRun:
    """Итог организации, до цикла не дошедшей."""
    return OrgRun(
        inn=entry.inn,
        name=_name_of(entry.inn) or entry.name,
        category_id=entry.category_id,
        category=entry.category,
        expected=entry.expected_outcome.value,
        ok=False,
        seconds=0.0,
        attempted=False,
        stage=stage,
        reason=reason,
    )


def _name_of(inn: str) -> str | None:
    """Наименование организации по базе; до загрузки его ещё нет."""
    found = fetch_all(_ORG_NAME, {"inn": inn})
    return found[0]["name"] if found else None


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
    sets = fetch_all(_SETS, {"inn": run.inn})
    if sets:
        run.sets = int(sets[0]["total"])
        run.quarantined = int(sets[0]["quarantined"])
    if contour is Contour.FULL:
        attempts = fetch_all(_ATTEMPTS, {"inn": run.inn, "since": since})
        run.attempts = int(attempts[0]["attempt"]) if attempts else None


# --- признаки ----------------------------------------------------------------

_BY_REPORTING_TYPE = """
SELECT DISTINCT inn, reporting_type FROM src_file
WHERE inn = ANY(%(inns)s) AND is_actual
"""

_LINE_LATEST = """
SELECT DISTINCT ON (inn) inn, value FROM fact_report
WHERE inn = ANY(%(inns)s) AND line_code = %(code)s AND value IS NOT NULL
ORDER BY inn, report_date DESC
"""

_NEGATIVE_EQUITY = """
SELECT DISTINCT inn FROM fact_report
WHERE inn = ANY(%(inns)s) AND line_code = '1300' AND value < 0
"""

# Убыток за два последних отчётных периода подряд: берутся две последние даты
# с раскрытым финансовым результатом.
_LOSS_TWO_YEARS = """
SELECT inn FROM (
    SELECT inn, value, row_number() OVER (PARTITION BY inn ORDER BY report_date DESC) AS rn
    FROM fact_report
    WHERE inn = ANY(%(inns)s) AND line_code = '2400' AND value IS NOT NULL
) ranked
WHERE rn <= 2
GROUP BY inn
HAVING count(*) = 2 AND max(value) < 0
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

# Выручка не раскрыта ни за один загруженный период. Считается по фактам,
# а не по показателям: организация, у которой все комплекты отбракованы,
# до расчёта показателей не доходит вовсе, а признак у неё есть.
_REVENUE_NOT_DISCLOSED = """
SELECT inn FROM fact_report
WHERE inn = ANY(%(inns)s)
GROUP BY inn
HAVING count(*) FILTER (WHERE line_code = '2110' AND value IS NOT NULL) = 0
"""

_SIGN_CHANGE = """
SELECT DISTINCT inn FROM metric_value
WHERE inn = ANY(%(inns)s) AND reason_code = %(reason)s
"""

_OUT_OF_SCOPE = """
SELECT DISTINCT inn FROM dq_log
WHERE inn = ANY(%(inns)s) AND check_code = %(code)s
"""

_SINGLE_YEAR = """
SELECT inn FROM src_file
WHERE inn = ANY(%(inns)s) AND is_actual
GROUP BY inn HAVING count(DISTINCT report_year) = 1
"""

# Доля показателей, не рассчитанных за последний период организации.
_NOT_CALCULABLE_SHARE = """
WITH latest AS (
    SELECT inn, max(report_date) AS report_date FROM metric_value
    WHERE inn = ANY(%(inns)s) GROUP BY inn
)
SELECT m.inn,
       count(*) FILTER (WHERE m.status <> 'ok')::numeric / count(*) AS share
FROM metric_value m JOIN latest l ON l.inn = m.inn AND l.report_date = m.report_date
GROUP BY m.inn
"""

_ACTIVITIES = """
SELECT left(okved, 2) AS class, count(*) AS n FROM organization
WHERE inn = ANY(%(inns)s) AND okved IS NOT NULL AND okved <> ''
GROUP BY 1 ORDER BY 1
"""


def features_of(regression_set: RegressionSet) -> dict[str, set[Feature]]:
    """Измеряет признаки каждой организации по базе.

    Это ответ на вопрос «что организация оказалась на деле», и он не зависит
    от того, зачем её включили в набор.
    """
    inns = regression_set.inns
    params = {"inns": inns}
    found: dict[str, set[Feature]] = {inn: set() for inn in inns}

    for row in fetch_all(_BY_REPORTING_TYPE, params):
        feature = (
            Feature.SIMPLIFIED_FORMS
            if row["reporting_type"] == "simplified"
            else Feature.FULL_FORMS
        )
        found.setdefault(row["inn"], set()).add(feature)

    simple = (
        (Feature.NEGATIVE_EQUITY, _NEGATIVE_EQUITY, params),
        (Feature.LOSS_TWO_YEARS, _LOSS_TWO_YEARS, params),
        (Feature.QUARANTINED, _QUARANTINED, params),
        (Feature.REVENUE_NOT_DISCLOSED, _REVENUE_NOT_DISCLOSED, params),
        (Feature.SINGLE_REPORT_YEAR, _SINGLE_YEAR, params),
        (
            Feature.HOLDING_STRUCTURE,
            _WITH_FLAG,
            {**params, "flag": regression_set.coverage.holding_flag},
        ),
        (
            Feature.SIGN_CHANGE,
            _SIGN_CHANGE,
            {**params, "reason": NotCalculableReason.SIGN_CHANGE.value},
        ),
        (
            Feature.OUT_OF_SCOPE,
            _OUT_OF_SCOPE,
            {**params, "code": CheckCode.CREDIT_ORGANIZATION.value},
        ),
    )
    for feature, query, args in simple:
        for row in fetch_all(query, args):
            found.setdefault(row["inn"], set()).add(feature)

    balance = _latest_line(inns, "1600")
    for inn, value in balance.items():
        if value >= regression_set.threshold("large_balance"):
            found.setdefault(inn, set()).add(Feature.LARGE_BALANCE)

    revenue = _latest_line(inns, "2110")
    for inn in inns:
        total = balance.get(inn)
        got = revenue.get(inn)
        if total is None or total <= 0 or got is None:
            # Нераскрытая выручка — не нулевой оборот, а отсутствие сведений
            # о нём. Признак у неё свой, и смешивать их нельзя: организация
            # с оборотом около нуля его всё-таки показала.
            continue
        if got / total < regression_set.threshold("near_zero_revenue"):
            found[inn].add(Feature.NEAR_ZERO_REVENUE)

    for row in fetch_all(_NOT_CALCULABLE_SHARE, params):
        if row["share"] >= regression_set.threshold("incomplete_disclosure"):
            found.setdefault(row["inn"], set()).add(Feature.INCOMPLETE_DISCLOSURE)
    return found


def _latest_line(inns: list[str], code: str) -> dict[str, Decimal]:
    """Значение строки за последний период, по организациям."""
    return {
        row["inn"]: row["value"]
        for row in fetch_all(_LINE_LATEST, {"inns": inns, "code": code})
    }


# --- покрытие и категории ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageItem:
    """Одно измерение покрытия: сколько организаций его дают."""

    name: str
    count: int
    detail: str


# Измерения, которых требует задача 17. Размерные группы и виды деятельности
# считаются отдельно: это распределения, а не «есть или нет».
COVERAGE_FEATURES: tuple[Feature, ...] = (
    Feature.SIMPLIFIED_FORMS,
    Feature.FULL_FORMS,
    Feature.NEGATIVE_EQUITY,
    Feature.HOLDING_STRUCTURE,
    Feature.QUARANTINED,
    Feature.SIGN_CHANGE,
)


def coverage(
    regression_set: RegressionSet, measured: dict[str, set[Feature]]
) -> list[CoverageItem]:
    """Покрытие набора по измерениям задачи 17."""
    found: list[CoverageItem] = []
    for feature in COVERAGE_FEATURES:
        hit = {inn for inn, items in measured.items() if feature in items}
        found.append(CoverageItem(FEATURE_NAMES[feature], len(hit), _listed(hit)))
    found.append(_size_coverage(regression_set))
    classes = fetch_all(_ACTIVITIES, {"inns": regression_set.inns})
    listed = ", ".join(f"{row['class']} ({row['n']})" for row in classes)
    found.append(
        CoverageItem(
            "виды деятельности (классы ОКВЭД)",
            len(classes),
            listed or "ОКВЭД не заполнен ни у одной организации",
        )
    )
    return found


def _size_coverage(regression_set: RegressionSet) -> CoverageItem:
    """Разбивка набора по размерным группам."""
    revenue = _latest_line(regression_set.inns, "2110")
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
        tail = f"без раскрытой выручки — {unknown}"
        detail = f"{detail}; {tail}" if detail else tail
    return CoverageItem("размерные группы", len(counted), detail or "нет данных")


def _listed(inns: set[str]) -> str:
    """ИНН измерения одной строкой; пусто — измерение не покрыто."""
    return ", ".join(sorted(inns)) if inns else "не покрыто"


@dataclass(frozen=True, slots=True)
class CategoryCheck:
    """Заявленная категория против фактического поведения."""

    id: int
    name: str
    declared: int
    confirmed: int
    unconfirmed: str
    arrived: str


def categories_check(
    regression_set: RegressionSet,
    measured: dict[str, set[Feature]],
    runs: list[OrgRun],
) -> list[CategoryCheck]:
    """Сверяет заявленную категорию с признаками, измеренными по базе.

    Признак категории — необходимый, а не достаточный: полные формы сдают
    и средние организации, и крупные. Поэтому «пришли из других категорий» —
    не ошибка состава, а сведение о том, что признак шире категории.
    """
    by_expectation = {item.inn: item for item in runs}
    found: list[CategoryCheck] = []
    for category in regression_set.categories:
        declared = [
            item
            for item in regression_set.running
            if item.category_id == category.id
        ]
        if category.feature is None:
            confirmed = [
                item
                for item in declared
                if by_expectation.get(item.inn) is not None
                and by_expectation[item.inn].as_expected
            ]
            note = category.manual or ""
            found.append(
                CategoryCheck(
                    category.id,
                    category.name,
                    len(declared),
                    len(confirmed),
                    _listed({item.inn for item in declared} - {item.inn for item in confirmed}),
                    note,
                )
            )
            continue
        confirmed = [
            item for item in declared if category.feature in measured.get(item.inn, set())
        ]
        others = {
            inn
            for inn, items in measured.items()
            if category.feature in items
            and inn not in {entry.inn for entry in declared}
        }
        found.append(
            CategoryCheck(
                category.id,
                category.name,
                len(declared),
                len(confirmed),
                _listed({item.inn for item in declared} - {item.inn for item in confirmed}),
                _listed(others),
            )
        )
    return found


# --- метрики прогона ---------------------------------------------------------

_QUALITY_FIRINGS = """
SELECT check_code, severity, count(*) AS n FROM dq_log
WHERE inn = ANY(%(inns)s) AND status = 'fail'
GROUP BY check_code, severity ORDER BY n DESC, check_code
"""

_SIGNAL_FIRINGS = """
SELECT s.signal_code, s.level, count(*) AS n
FROM assessment_signal s JOIN assessment a ON a.id = s.assessment_id
WHERE a.inn = ANY(%(inns)s)
GROUP BY s.signal_code, s.level ORDER BY n DESC, s.signal_code
"""

_FLAG_FIRINGS = """
SELECT f.flag_code, f.level, count(*) AS n
FROM assessment_flag f JOIN assessment a ON a.id = f.assessment_id
WHERE a.inn = ANY(%(inns)s)
GROUP BY f.flag_code, f.level ORDER BY n DESC, f.flag_code
"""

_LOADED = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE status = 'quarantine') AS quarantined,
       count(DISTINCT inn) AS organizations
FROM src_file WHERE inn = ANY(%(inns)s)
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
    params = {"inns": inns}
    expected_refusal = [item for item in runs if item.expected == ExpectedOutcome.REFUSAL]
    # Организации, у которых ожидается отбраковка, в долю остановок не входят:
    # их остановка — подтверждение контроля, а не отказ методики. Считаются
    # они отдельной графой, иначе доля дошедших до оценки занижалась бы на них.
    expected_quarantine = [
        item for item in runs if item.expected == ExpectedOutcome.QUARANTINE_EXPECTED
    ]
    analysed = [item for item in runs if item.expected == ExpectedOutcome.ANALYSIS]
    finished = [item for item in analysed if item.ok]
    with_class = [item for item in finished if item.class_code]
    classes: dict[str, int] = {}
    for item in with_class:
        classes[item.class_code] = classes.get(item.class_code, 0) + 1

    loaded = fetch_all(_LOADED, params)
    seconds = [item.seconds for item in runs]
    metrics: dict = {
        "организаций в прогоне": len(runs),
        "в резерве": len(regression_set.organizations) - len(regression_set.running),
        "итог совпал с ожиданием": f"{len([i for i in runs if i.as_expected])} из {len(runs)}",
        "ожидался отказ": len(expected_refusal),
        "ожидалась отбраковка": len(expected_quarantine),
        "отбраковано, как и ожидалось": len(
            [item for item in expected_quarantine if item.as_expected]
        ),
        "прошли цикл": len(finished),
        "остановились": len(analysed) - len(finished),
        "остановки по этапам": _by_stage(analysed),
        "комплектов загружено": int(loaded[0]["total"]) if loaded else 0,
        "комплектов в карантине": int(loaded[0]["quarantined"]) if loaded else 0,
        "организаций с данными": int(loaded[0]["organizations"]) if loaded else 0,
        "классы": dict(sorted(classes.items())),
        "без класса": len(finished) - len(with_class),
        "доля без класса": _share(len(finished) - len(with_class), len(finished)),
        "контроли качества": [
            {
                "контроль": row["check_code"],
                "уровень": row["severity"],
                "срабатываний": int(row["n"]),
            }
            for row in fetch_all(_QUALITY_FIRINGS, params)
        ],
        "сигналы": [
            {
                "сигнал": row["signal_code"],
                "уровень": row["level"],
                "организаций": int(row["n"]),
            }
            for row in fetch_all(_SIGNAL_FIRINGS, params)
        ],
        "флаги": [
            {
                "флаг": row["flag_code"],
                "уровень": row["level"],
                "организаций": int(row["n"]),
            }
            for row in fetch_all(_FLAG_FIRINGS, params)
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
    """Отчёт о прогоне: параметры, организации, покрытие, категории, метрики."""

    started: datetime
    parameters: dict
    organizations: list[dict]
    coverage: list[dict]
    categories: list[dict]
    metrics: dict = field(default_factory=dict)


def parameters(
    regression_set: RegressionSet, contour: Contour, started: datetime
) -> dict:
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
        "настройки набора": regression_set.version,
        "организаций в составе": len(regression_set.organizations),
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
        "| ИНН | Организация | Категория | Период | Итог | Класс | Уверенность | "
        "Сигналов | Флагов | Карантин | Секунд |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for item in report.organizations:
        lines.append(
            f"| {item['inn']} | {item['name']} | {item['category_id']} | "
            f"{item['report_date'] or '—'} | {_outcome(item)} | "
            f"{item['class_code'] or 'нет'} | {item['confidence'] or '—'} | "
            f"{item['signals']} | {item['flags']} | {item['quarantined']} | "
            f"{item['seconds']} |"
        )

    lines += [
        "",
        "## Заявленное покрытие против фактического",
        "",
        "| № | Категория | Заявлено | Подтверждено | Не подтверждено | "
        "Признак есть у других |",
        "|---|---|---|---|---|---|",
    ]
    for item in report.categories:
        lines.append(
            f"| {item['id']} | {item['name']} | {item['declared']} | "
            f"{item['confirmed']} | {item['unconfirmed']} | {item['arrived']} |"
        )

    lines += [
        "",
        "## Покрытие набора",
        "",
        "| Измерение | Организаций | Состав |",
        "|---|---|---|",
    ]
    for item in report.coverage:
        lines.append(f"| {item['name']} | {item['count']} | {item['detail']} |")

    lines += ["", "## Метрики прогона", ""]
    lines += _metric_lines(report.metrics)

    stopped = [item for item in report.organizations if not item["ok"]]
    if stopped:
        lines += ["", "## Остановки", ""]
        for item in stopped:
            mark = ""
            if item["as_expected"]:
                mark = (
                    " (ожидалась отбраковка)"
                    if item["expected"] == ExpectedOutcome.QUARANTINE_EXPECTED.value
                    else " (ожидался отказ)"
                )
            lines.append(
                f"- {item['inn']} {item['name']}{mark}: этап «{item['stage']}» — "
                f"{item['reason']}"
            )
    return "\n".join(lines) + "\n"


def _outcome(item: dict) -> str:
    """Итог организации словами, с учётом того, чего от неё ждали."""
    if item["ok"]:
        return "пройдено"
    if item["as_expected"]:
        if item["expected"] == ExpectedOutcome.QUARANTINE_EXPECTED.value:
            return "отбраковано, как и ожидалось"
        return "отказ, как и ожидался"
    return f"остановлено: {item['stage']}"


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


def _finalize(runs: list[OrgRun], measured: dict[str, set[Feature]], regression_set) -> None:
    """Проставляет признаки, совпадение с ожиданием и подтверждение категории."""
    by_inn = {item.inn: item for item in regression_set.running}
    for run in runs:
        features = sorted(measured.get(run.inn, set()))
        run.features = [FEATURE_NAMES[Feature(item)] for item in features]
        entry = by_inn[run.inn]
        if entry.expected_outcome is ExpectedOutcome.REFUSAL:
            # Корректный отказ — успех прогона, а не дефект. «Корректный»
            # значит отказ самого цикла: ни сбой прогонщика, ни незагруженная
            # отчётность им не считаются. Иначе прогон без данных отчитывался
            # бы ожидаемым отказом по организации, до которой не дошёл.
            run.as_expected = (
                run.attempted and not run.ok and run.stage != RUNNER_FAILURE
            )
        elif entry.expected_outcome is ExpectedOutcome.QUARANTINE_EXPECTED:
            # Ожидается не любая остановка, а именно отбраковка: отчётность
            # загружена, и все её комплекты отбракованы контролями. Организация,
            # которая внезапно прошла цикл, ожиданию не соответствует — это
            # сведение о том, что гипотеза устарела, а не успех.
            run.as_expected = (
                run.attempted
                and not run.ok
                and run.stage != RUNNER_FAILURE
                and run.sets > 0
                and run.quarantined == run.sets
            )
        else:
            run.as_expected = run.ok
        category = regression_set.category_of(entry)
        run.category_confirmed = (
            run.as_expected
            if category.feature is None
            else category.feature in measured.get(run.inn, set())
        )


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
        for item in regression_set.running
        if only is None or item.inn in set(only)
    ]
    if not entries:
        raise ValueError("ни одна из названных организаций в прогон не входит")
    # Покрытие и метрики считаются по прогнанным организациям, а не по всему
    # составу: иначе отчёт о части набора выглядел бы отчётом о наборе.
    subset = (
        regression_set
        if only is None
        else regression_set.with_organizations(tuple(entries))
    )
    started = datetime.now()
    runs: list[OrgRun] = []
    for number, entry in enumerate(entries, start=1):
        logger.info("[%d/%d] %s %s", number, len(entries), entry.inn, entry.name)
        runs.append(run_one(entry, contour, fetch=fetch))

    measured = features_of(subset)
    _finalize(runs, measured, subset)

    found = parameters(subset, contour, started)
    if only is not None:
        found["настройки набора"] = (
            f"{regression_set.version}, прогнана часть: "
            f"{len(entries)} из {len(regression_set.running)}"
        )
    return Report(
        started=started,
        parameters=found,
        organizations=[asdict(item) for item in runs],
        coverage=[asdict(item) for item in coverage(subset, measured)],
        categories=[asdict(item) for item in categories_check(subset, measured, runs)],
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
