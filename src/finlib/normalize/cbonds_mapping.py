"""Справочник сопоставления полей Cbonds с позициями модели.

**Сопоставление — методика, а не деталь загрузчика.** Решение о том, чем
считать «Краткосрочный долг» агрегатора, участвует в суждении об эмитенте:
от него зависит долг, а от долга — долговая нагрузка. Поэтому оно объявлено
диффом в `methodology/cbonds_mapping.yaml`, а здесь только чтение и проверки
состава.

**Род поля объявляется обязательно** (`kind`): точное соответствие, агрегат
или сверка. Агрегат обязан сказать, что он покрывает и где это видно;
агрегат, который всё-таки грузится, обязан назвать причину — без неё
величина неизвестного состава попадала бы в факты молча.

**Перечень полей и правило — разные способы объявить состав, и выбор между
ними не в удобстве.** У МСФО имена полей источника с нашими позициями ничем
не связаны, и перечень необходим. У РСБУ поле названо кодом строки,
утверждённым приказом 66н, и перечень был бы вторым экземпляром справочника
строк — расхождение двух перечней вопрос времени. Поэтому объявляется
правило, а состав решает `lines.yaml`.
"""

import logging
import re
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)

# Формы нашей модели под короткими именами справочника: писать полный код
# позиции у каждого поля значило бы повторить его тридцать раз.
FORMS: dict[str, str] = {
    "position": "ifrs.statement_of_financial_position",
    "profit_or_loss": "ifrs.statement_of_profit_or_loss",
    "cash_flow": "ifrs.statement_of_cash_flows",
}


class FieldDef(BaseModel):
    """Поле источника и позиция модели, которой оно отвечает."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^ifrs\.[a-z][a-z0-9_]*$")
    form: str = Field(min_length=1)
    kind: str = Field(pattern="^(exact|aggregate|control)$")
    loaded: bool = True
    covers: tuple[str, ...] = ()
    seen_at: str | None = None

    @model_validator(mode="after")
    def _form_is_known(self) -> Self:
        """Форма названа коротким именем из перечня, а не произвольно."""
        if self.form not in FORMS:
            raise ValueError(f"{self.code}: форма {self.form} не объявлена")
        return self

    @model_validator(mode="after")
    def _aggregate_declares_itself(self) -> Self:
        """Агрегат объявляет состав и место наблюдения.

        Поле шире нашей позиции — это решение о данных, и оно обязано быть
        видно в справочнике: у Черкизово нематериальные активы агрегатора
        включают гудвил, и без объявления это выглядело бы ошибкой разбора.
        """
        if self.kind != "aggregate":
            return self
        if not self.covers or not (self.seen_at or "").strip():
            raise ValueError(
                f"{self.code}: агрегат объявлен без состава либо без наблюдения"
            )
        return self

    @property
    def form_code(self) -> str:
        """Полный код формы нашей модели."""
        return FORMS[self.form]


# Откуда берётся допуск сверки. **Своего числа здесь нет и быть не должно:**
# допуск на округление при проверке сходимости итогов объявлен в
# `thresholds.yaml` (блок `rounding`), и второй экземпляр той же величины
# однажды разойдётся с первым. Пусто — сверка требует точного равенства.
TOLERANCE_SOURCES: frozenset[str] = frozenset({"thresholds.rounding"})


class SumControl(BaseModel):
    """Сверка итога с суммой частей."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: str = Field(min_length=1)
    parts: tuple[str, ...] = Field(min_length=2)
    check: str = Field(min_length=1)
    tolerance_from: str | None = None
    origin: str | None = None

    @model_validator(mode="after")
    def _tolerance_is_taken_from_methodology(self) -> Self:
        """Допуск берётся из объявленного места, а не задаётся числом."""
        if self.tolerance_from and self.tolerance_from not in TOLERANCE_SOURCES:
            raise ValueError(
                f"допуск {self.tolerance_from} неизвестен; допустимы: "
                + ", ".join(sorted(TOLERANCE_SOURCES))
            )
        return self


class EqualityControl(BaseModel):
    """Сверка двух полей между собой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    check: str = Field(min_length=1)
    tolerance_from: str | None = None

    @model_validator(mode="after")
    def _tolerance_is_taken_from_methodology(self) -> Self:
        """Допуск берётся из объявленного места, а не задаётся числом."""
        if self.tolerance_from and self.tolerance_from not in TOLERANCE_SOURCES:
            raise ValueError(
                f"допуск {self.tolerance_from} неизвестен; допустимы: "
                + ", ".join(sorted(TOLERANCE_SOURCES))
            )
        return self


class ZeroTotal(BaseModel):
    """Ноль итога при ненулевой деятельности: признак нераскрытия."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    totals: tuple[str, ...] = Field(min_length=1)
    activity: tuple[str, ...] = Field(min_length=1)
    check: str = Field(min_length=1)


class ZeroReading(BaseModel):
    """Как контроли сходимости читают величины доставки агрегатора.

    **Ноль у агрегатора не означает нуля**, и для контроля это значит «итог
    не проверяем», а не «итог не сошёлся»: слагаемое, о котором неизвестно,
    ноль это или прочерк, нельзя ни складывать, ни считать раскрытым.
    Отдельно объявлены строки, которых у источника нет вовсе: их отсутствие —
    свойство его набора полей, а не нераскрытие эмитентом.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_not_disclosed: bool
    lines_not_delivered: tuple[str, ...] = ()
    origin: str = Field(min_length=1)


class Controls(BaseModel):
    """Сверки вида отчёта.

    Сверки суммой — перечень, а не поимённые поля: у баланса РСБУ их две
    (актив по разделам и пассив по разделам), у МСФО две других. Поле на каждую
    заставляло бы заводить новое имя всякий раз, а загрузчик — знать эти имена
    наизусть.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: EqualityControl
    sums: tuple[SumControl, ...] = Field(min_length=1)


class Delivery(BaseModel):
    """Доставка вида отчёта: метод источника и поле отбора.

    Комплект РСБУ приходит тремя доставками — баланс, отчёт о финансовых
    результатах и отчёт о движении денежных средств, — и сводится по паре
    «ИНН, дата». Поле отбора объявлено у каждой: у отчёта о движении денежных
    средств поля ИНН нет вовсе, отбор идёт по идентификатору эмитента,
    а неподдерживаемое поле Cbonds пропускает молча.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(min_length=1)
    filter_field: str = Field(min_length=1)
    cache: str = Field(min_length=1)


class FieldRule(BaseModel):
    """Правило, по которому имя поля означает код строки.

    Перечень полей и правило — не одно и то же. У МСФО перечень необходим:
    имена полей источника с нашими позициями ничем не связаны. У РСБУ поле
    названо кодом строки, утверждённым приказом 66н, и перечень был бы
    вторым экземпляром справочника строк — с неизбежным расхождением.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: str = Field(min_length=1)
    catalog: str = Field(pattern="^rsbu_lines$")
    origin: str = Field(min_length=1)


class ReportDef(BaseModel):
    """Вид отчёта источника целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    standard: str = Field(pattern="^(rsbu|ifrs)$")
    name: str = Field(min_length=1)
    deliveries: tuple[Delivery, ...] = Field(min_length=1)
    annual_only: bool
    # Валюта и единица — либо поле источника, либо объявленное правило
    # с причиной. Умолчания нет ни у одной: ошибка здесь тихая, баланс
    # сойдётся, и неверными окажутся только сами величины.
    currency_field: str | None = None
    currency: str | None = None
    currency_origin: str = ""
    unit_field: str | None = None
    units: dict[str, str] = Field(default_factory=dict)
    unit_from: str | None = Field(default=None, pattern="^forms$")
    unit_origin: str = ""
    # Признак стандарта и требование консолидации: у РСБУ их нет по устройству,
    # и это объявляется, а не подразумевается.
    standard_field: str | None = None
    consolidated_marks: tuple[str, ...] = ()
    standalone_marks: tuple[str, ...] = ()
    consolidation_required: bool = True
    consolidation_origin: str = ""
    reporting_type: str = Field(default="full", pattern="^(full|simplified)$")
    reporting_type_origin: str = ""
    # Знак величины — решение о данных, и оно объявляется у каждого вида
    # отчёта: молча изменённый знак не ловится ни одним контролем сходимости.
    sign_rule: str = Field(pattern="^(as_reported|expense_magnitude)$")
    sign_origin: str = Field(min_length=1)
    fields: dict[str, FieldDef] = Field(default_factory=dict)
    field_rule: FieldRule | None = None
    controls: Controls
    zero_total: ZeroTotal
    zero_reading: ZeroReading | None = None
    reported: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _marks_are_not_empty(self) -> Self:
        """Признак консолидации не бывает пустой строкой.

        Пустая строка входит в любой текст — тот же дефект, что маркер рубля
        и звёздочка сноски. Здесь он дал бы «консолидированная» у каждой
        записи, и различить два стандарта стало бы нечем.
        """
        for name in (*self.consolidated_marks, *self.standalone_marks):
            if not name.strip():
                raise ValueError("признак стандарта объявлен пустой строкой")
        return self

    @model_validator(mode="after")
    def _one_way_to_know_each_thing(self) -> Self:
        """Валюта, единица, состав полей — по одному объявлению у каждого.

        Два объявления одной величины однажды расходятся, а ни одного —
        означает умолчание, которого здесь быть не должно: ошибка в валюте
        и в единице тихая. Причина обязательна у объявленного правила:
        «RUB» без основания неотличимо от догадки.
        """
        if bool(self.currency_field) == bool(self.currency):
            raise ValueError(
                f"{self.name}: валюта объявляется либо полем источника, "
                "либо правилом с причиной — ровно одним из двух"
            )
        if self.currency and not self.currency_origin.strip():
            raise ValueError(f"{self.name}: валюта объявлена правилом без причины")
        if bool(self.unit_field) == bool(self.unit_from):
            raise ValueError(
                f"{self.name}: единица объявляется либо полем источника, "
                "либо правилом форм — ровно одним из двух"
            )
        if self.unit_field and not self.units:
            raise ValueError(f"{self.name}: поле единицы названо без перечня единиц")
        if self.unit_from and not self.unit_origin.strip():
            raise ValueError(f"{self.name}: единица объявлена правилом без причины")
        if bool(self.fields) == bool(self.field_rule):
            raise ValueError(
                f"{self.name}: состав полей объявляется либо перечнем, "
                "либо правилом — ровно одним из двух"
            )
        if self.field_rule and not self.reporting_type_origin.strip():
            raise ValueError(
                f"{self.name}: вид отчётности принят без причины, а он решает, "
                "по какому набору строк комплект проверяется"
            )
        return self

    @model_validator(mode="after")
    def _consolidation_is_declared(self) -> Self:
        """Требование консолидации объявлено вместе со способом проверки.

        Требовать консолидацию и не назвать признака, по которому она видна, —
        то же, что контроль без входа: он отвечал бы одно и то же. Не требовать
        её молча нельзя по обратной причине: неконсолидированная отчётность
        по МСФО относится к другому предмету.
        """
        if self.consolidation_required:
            if not self.standard_field or not self.consolidated_marks:
                raise ValueError(
                    f"{self.name}: консолидация требуется, а признака стандарта "
                    "либо его написаний не объявлено"
                )
        elif not self.consolidation_origin.strip():
            raise ValueError(
                f"{self.name}: консолидация не требуется, и не сказано почему"
            )
        return self

    @model_validator(mode="after")
    def _controls_name_known_fields(self) -> Self:
        """Сверка ссылается на поля, которые справочник знает.

        У вида отчёта с перечнем полей это буквально перечень; у вида
        с правилом — соответствие правилу: иначе сверка назвала бы поле,
        которого источник не отдаёт, и не выполнялась бы никогда.
        """
        named = {
            self.controls.identity.left,
            self.controls.identity.right,
            *(item.total for item in self.controls.sums),
            *(part for item in self.controls.sums for part in item.parts),
            *self.zero_total.totals,
            *self.zero_total.activity,
        }
        if self.field_rule is not None:
            pattern = re.compile(self.field_rule.pattern)
            stray = {name for name in named if not pattern.match(name)}
        else:
            stray = named - (set(self.fields) | set(self.reported.values()))
        if stray:
            raise ValueError(f"сверка ссылается на неизвестные поля: {sorted(stray)}")
        return self

    def check_codes(self) -> frozenset[str]:
        """Коды контролей, которые объявляет вид отчёта."""
        return frozenset(
            {
                self.controls.identity.check,
                *(item.check for item in self.controls.sums),
                self.zero_total.check,
            }
        )

    def controls_declared(self) -> int:
        """Сколько сверок объявлено: знаменатель для сводки проверенного.

        Число в коде было бы тем самым «ноль срабатываний неотличим
        от невыполненного», только в знаменателе: сверок стало больше,
        а счётчик остался бы прежним.
        """
        return 2 + len(self.controls.sums)

    def code_of(self, name: str) -> str | None:
        """Код строки по имени поля источника; None — поле кодом не является."""
        if self.field_rule is None:
            item = self.fields.get(name)
            return item.code if item is not None else None
        found = re.match(self.field_rule.pattern, name)
        return found.group(1) if found else None

    def loaded_fields(self) -> dict[str, FieldDef]:
        """Поля, которые становятся фактами."""
        return {
            name: item
            for name, item in self.fields.items()
            if item.kind != "control" and item.loaded
        }


class Substitution(BaseModel):
    """Замена величины, объявленная методикой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: str | tuple[str, ...]
    fallback: str | tuple[str, ...]
    shown_as: str | None = None
    origin: str = Field(min_length=1)


class CbondsMapping(BaseModel):
    """Справочник сопоставления целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    reports: dict[str, ReportDef] = Field(min_length=1)
    substitutions: dict[str, Substitution] = Field(min_length=1)

    def report(self, name: str) -> ReportDef:
        """Вид отчёта по имени; неизвестный — ошибка, а не умолчание."""
        found = self.reports.get(name)
        if found is None:
            raise KeyError(
                f"вида отчёта {name} в справочнике сопоставления нет: "
                "поля источника наизусть загрузчик не знает"
            )
        return found


def load_cbonds_mapping(path: Path | None = None) -> CbondsMapping:
    """Читает справочник сопоставления полей Cbonds."""
    source = path or settings.methodology_dir / "cbonds_mapping.yaml"
    mapping = CbondsMapping.model_validate(
        yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    )
    for name, report in mapping.reports.items():
        logger.info(
            "сопоставление %s (%s): полей %d, из них грузится %d, агрегатов %d",
            name,
            mapping.version,
            len(report.fields),
            len(report.loaded_fields()),
            sum(1 for item in report.fields.values() if item.kind == "aggregate"),
        )
    return mapping
