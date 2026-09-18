"""Загрузка справочника строк форм РСБУ с проверкой целостности."""

import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from finlib.config import settings


class ReportingType(StrEnum):
    """Набор строк отчётности; значения совпадают с CHECK в src_file."""

    FULL = "full"
    SIMPLIFIED = "simplified"


class Measure(StrEnum):
    """Вид величины: на дату или за период.

    От вида зависит, с чем величина сравнивается. Балансовая стоит на дату,
    и началом периода ей служит конец предыдущего **годового** периода.
    Величина за период сравнивается только с сопоставимым по длительности
    отрезком прошлого года: полугодие с полугодием, год с годом.

    Определение одно на оба стандарта: в МСФО кодов строк нет, а деление
    на величины на дату и за период то же самое.
    """

    STOCK = "stock"
    FLOW = "flow"


class Sign(StrEnum):
    """Допустимый знак значения строки."""

    POSITIVE = "positive"
    ANY = "any"


class Operator(StrEnum):
    """Оператор вхождения строки в состав итоговой."""

    PLUS = "+"
    MINUS = "-"


def normalize_name(text: str) -> str:
    """Приводит наименование строки к виду, пригодному для сопоставления."""
    lowered = text.casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", lowered).split())


def _normalize_unit(text: str) -> str:
    """Приводит подпись единицы измерения к виду, пригодному для сравнения."""
    return normalize_name(text)


class Component(BaseModel):
    """Слагаемое итоговой строки с оператором."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^\d{4}$")
    op: Operator = Operator.PLUS


class NameMisprint(BaseModel):
    """Написание строки, которое встречается в выгрузке из-за опечатки источника.

    От `name_aliases` отличается сутью, а не механикой: алиас — законный
    вариант наименования, опечатка — дефект шаблона выгрузки. Причина
    обязательна: через год строку «Проценты по уплате» в справочнике иначе
    не отличить от принятого наименования, и её примут за вариант нормы.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class LineDef(BaseModel):
    """Определение строки формы отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^\d{4}$")
    reporting_type: ReportingType = ReportingType.FULL
    name: str = Field(min_length=1)
    name_aliases: tuple[str, ...] = ()
    # Опечатки шаблона источника: опознаются наравне с наименованием,
    # но объявлены отдельно и с причиной.
    name_misprints: tuple[NameMisprint, ...] = ()
    form: str = Field(pattern=r"^\d{7}$")
    section: str = Field(min_length=1)
    sign: Sign = Sign.POSITIVE
    # Вид величины, когда строка отличается от своей формы: остатки денежных
    # средств стоят в отчёте за период, а являются величинами на дату.
    # Умолчание задаёт форма (`measures.by_form`), и здесь его нет: пустое
    # поле означает «как у формы», а не «неизвестно».
    measure: Measure | None = None
    in_brackets: bool = False
    is_total: bool = False
    components: tuple[Component, ...] = ()
    # Поля укрупнённых строк упрощённых форм.
    code_allowed: tuple[str, ...] = ()
    aggregates: tuple[str, ...] = ()
    same_meaning_as_full: bool | None = None
    # Безусловная оговорка о содержании строки: верна для любой организации,
    # сдавшей отчётность в этом наборе. Идёт в раздел «Ограничения анализа».
    note: str | None = None
    # Сопоставление с другим набором отчётности и прочее описание методики.
    # **В промпт не передаётся никогда**: сказанное о наборе, которым
    # организация не пользуется, модель выдаёт за факт о самой организации.
    methodology_note: str | None = None

    @property
    def key(self) -> tuple[ReportingType, str]:
        """Ключ строки в справочнике."""
        return (self.reporting_type, self.code)

    @property
    def match_names(self) -> tuple[str, ...]:
        """Нормализованные наименования, по которым строка опознаётся."""
        names = (
            self.name,
            *self.name_aliases,
            *(item.name for item in self.name_misprints),
        )
        return tuple(dict.fromkeys(normalize_name(name) for name in names))

    def accepts_code(self, code: str) -> bool:
        """Допустим ли такой код строки в отчётности."""
        return code == self.code if not self.code_allowed else code in self.code_allowed

    @model_validator(mode="after")
    def _check_components(self) -> Self:
        """Состав есть только у итоговых строк и только непустой."""
        if self.is_total and not self.components:
            raise ValueError(f"итоговая строка {self.code} без состава")
        if not self.is_total and self.components:
            raise ValueError(f"строка {self.code} не итоговая, но имеет состав")
        codes = [component.code for component in self.components]
        if self.code in codes:
            raise ValueError(f"строка {self.code} входит в состав самой себя")
        if len(set(codes)) != len(codes):
            raise ValueError(f"в составе строки {self.code} повторяются коды")
        return self

    @model_validator(mode="after")
    def _check_reporting_type_fields(self) -> Self:
        """Поля укрупнения заполняются только у упрощённого набора."""
        if self.reporting_type is ReportingType.FULL:
            if self.code_allowed or self.aggregates or self.same_meaning_as_full is not None:
                raise ValueError(
                    f"строка {self.code} полного набора не может иметь полей укрупнения"
                )
            return self

        if not self.aggregates:
            raise ValueError(f"упрощённая строка {self.code} без перечня aggregates")
        if not self.code_allowed:
            raise ValueError(f"упрощённая строка {self.code} без перечня code_allowed")
        if self.code not in self.code_allowed:
            raise ValueError(
                f"канонический код {self.code} отсутствует в code_allowed этой же строки"
            )
        if self.same_meaning_as_full is False and not (self.note or "").strip():
            raise ValueError(
                f"упрощённая строка {self.code} отличается по смыслу от полного набора, "
                "но не снабжена пояснением note"
            )
        return self


class FormDef(BaseModel):
    """Форма отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)


class ReportingTypeDef(BaseModel):
    """Набор строк отчётности и состав его форм."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    forms: tuple[str, ...] = Field(min_length=1)


class IgnoredCode(BaseModel):
    """Код источника, который методика не использует осознанно."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str | None = None
    pattern: str | None = None
    form: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_selector(self) -> Self:
        """Задан ровно один способ отбора: конкретный код либо шаблон."""
        if (self.code is None) == (self.pattern is None):
            raise ValueError("игнорируемый код задаётся либо code, либо pattern, но не обоими")
        if self.pattern is not None:
            re.compile(self.pattern)
        return self

    def matches(self, code: str, form: str) -> bool:
        """Подпадает ли код формы под это правило."""
        if self.form is not None and self.form != form:
            return False
        if self.code is not None:
            return self.code == code
        return re.fullmatch(str(self.pattern), code) is not None


class UnitSource(StrEnum):
    """Откуда известна единица измерения комплекта."""

    FORM_STANDARD = "form_standard"
    EXPLICIT = "explicit"
    UNKNOWN = "unknown"


class UnitsDef(BaseModel):
    """Единица измерения, определяемая формой отчётности.

    Ответ ГИР БО единицы не содержит, и прежде она принималась как
    предположение. Это дефект данных: ошибка в тысячу раз не ловится ни одним
    контролем — баланс сойдётся, коэффициенты будут верны. Форма единицу задаёт
    однозначно, поэтому определение идёт от неё, а не от полезной нагрузки.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    okei_code: str = Field(pattern=r"^\d{3}$")
    name: str = Field(min_length=1)
    multiplier: Decimal = Field(gt=0)
    forms: tuple[str, ...] = Field(min_length=1)
    origin: str = Field(min_length=1)

    def matches_declared(self, declared: str | None) -> bool:
        """Совпадает ли единица, объявленная источником, с единицей справочника.

        Сравнение по написанию, а не по коду: ОКЕИ в выгрузке нет, есть
        подпись «Тыс. руб.» в шапке. Несовпадение означает не другую единицу,
        а неизвестную: сопоставить её с кодом ОКЕИ нечем, и угадывать нельзя.
        """
        if not declared:
            return False
        return _normalize_unit(declared) == _normalize_unit(self.name)

    def source_for(self, form_codes: Sequence[str]) -> UnitSource:
        """Определена ли единица для набора форм комплекта.

        Пустой перечень форм — не «определено по умолчанию», а неизвестность:
        комплект без форм единицы не имеет.
        """
        if not form_codes:
            return UnitSource.UNKNOWN
        known = set(self.forms)
        if all(code in known for code in form_codes):
            return UnitSource.FORM_STANDARD
        return UnitSource.UNKNOWN


class MeasuresDef(BaseModel):
    """Вид величины по формам отчётности.

    Объявляется формой, а не выводится из кода строки: правило «код на 1 —
    баланс» молча сломается на первой же форме с другой нумерацией и на МСФО,
    где кодов нет вовсе. Строка, отличающаяся от своей формы, объявляет вид
    сама — так устроены остатки денежных средств в отчёте о движении.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    by_form: dict[str, Measure] = Field(min_length=1)
    annual_end: str = Field(pattern=r"^\d{2}-\d{2}$")
    annual_end_origin: str = Field(min_length=1)
    origin: str = Field(min_length=1)

    @property
    def annual_month_day(self) -> tuple[int, int]:
        """Месяц и день, которыми кончается годовой период."""
        month, day = self.annual_end.split("-")
        return int(month), int(day)

    def is_annual_end(self, moment: date) -> bool:
        """Кончается ли годовой период этой датой."""
        return (moment.month, moment.day) == self.annual_month_day

    def previous_annual_end(self, moment: date) -> date:
        """Конец годового периода, предшествующего этой дате.

        У годовой отчётности это прошлый год, у промежуточной — конец
        прошлого года: и там и там последний день годового периода,
        завершившегося до отчётной даты.
        """
        month, day = self.annual_month_day
        return date(moment.year - 1, month, day)


class LinesCatalog(BaseModel):
    """Справочник строк всех форм с индексами по коду и наименованию."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    units: UnitsDef
    measures: MeasuresDef
    forms: dict[str, FormDef]
    reporting_types: dict[ReportingType, ReportingTypeDef]
    lines: tuple[LineDef, ...]
    ignored_codes: tuple[IgnoredCode, ...] = ()

    _index: dict[tuple[ReportingType, str], LineDef] = PrivateAttr(default_factory=dict)
    _by_name: dict[tuple[ReportingType, str, str], tuple[LineDef, ...]] = PrivateAttr(
        default_factory=dict
    )

    # --- проверки целостности ------------------------------------------------

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Строит индексы и проверяет справочник целиком."""
        self._index = self._build_index()
        self._by_name = self._build_name_index()
        self._check_components_exist()
        self._check_aggregates()
        self._check_meaning_declared()
        self._check_measures_declared()
        _check_no_cycles(self._index)
        return self

    def _check_measures_declared(self) -> None:
        """У каждой формы объявлен вид величины.

        Форма без объявления означала бы, что вид её строк выводится
        умолчанием кода, а умолчание здесь недопустимо: от вида зависит,
        с каким периодом величина сравнивается, и ошибка тихая — число
        получается настоящее, а сравнение чужое.
        """
        missing = sorted(set(self.forms) - set(self.measures.by_form))
        if missing:
            raise ValueError(
                "вид величины не объявлен для форм: " + ", ".join(missing)
            )

    def _build_index(self) -> dict[tuple[ReportingType, str], LineDef]:
        """Индекс по паре (набор, код) с проверкой уникальности и форм."""
        index: dict[tuple[ReportingType, str], LineDef] = {}
        for line in self.lines:
            if line.key in index:
                raise ValueError(
                    f"код строки {line.code} встречается дважды в наборе {line.reporting_type}"
                )
            if line.form not in self.forms:
                raise ValueError(f"строка {line.code} ссылается на неизвестную форму {line.form}")
            type_def = self.reporting_types.get(line.reporting_type)
            if type_def is None:
                raise ValueError(f"строка {line.code} ссылается на неизвестный набор строк")
            if line.form not in type_def.forms:
                raise ValueError(
                    f"форма {line.form} не входит в набор {line.reporting_type} "
                    f"(строка {line.code})"
                )
            index[line.key] = line
        return index

    def _build_name_index(self) -> dict[tuple[ReportingType, str, str], tuple[LineDef, ...]]:
        """Индекс наименований упрощённого набора.

        Для полного набора индекс не строится: там наименования неоднозначны
        («Заёмные средства» — и 1410, и 1510), а ключом служит код строки.

        Одно наименование на две строки допускается в единственном случае:
        когда перечни допустимых кодов у них не пересекаются и код разводит
        тёзок однозначно. Так устроен «БАЛАНС» упрощённой формы — итог актива
        и итог пассива печатаются одним словом, а различаются кодом 1600
        и 1700. Пересекись перечни — выбор стал бы догадкой, и справочник
        не загрузится.
        """
        by_name: dict[tuple[ReportingType, str, str], tuple[LineDef, ...]] = {}
        for line in self.lines:
            if line.reporting_type is ReportingType.FULL:
                continue
            for name in line.match_names:
                key = (line.reporting_type, line.form, name)
                current = by_name.get(key, ())
                for existing in current:
                    if existing.code == line.code:
                        continue
                    shared = set(_allowed_codes(existing)) & set(_allowed_codes(line))
                    if shared:
                        raise ValueError(
                            f"наименование «{name}» опознаёт сразу строки "
                            f"{existing.code} и {line.code} формы {line.form}, "
                            f"а коды {', '.join(sorted(shared))} допускают обе"
                        )
                by_name[key] = (*current, line)
        return by_name

    def _check_components_exist(self) -> None:
        """Состав ссылается на существующие строки того же набора и той же формы."""
        for line in self.lines:
            for component in line.components:
                target = self._index.get((line.reporting_type, component.code))
                if target is None:
                    raise ValueError(
                        f"в составе строки {line.code} указан отсутствующий код {component.code}"
                    )
                if target.form != line.form:
                    raise ValueError(
                        f"строка {component.code} формы {target.form} входит в состав "
                        f"строки {line.code} формы {line.form}"
                    )

    def _check_aggregates(self) -> None:
        """Укрупняемые строки существуют в полном наборе и не делятся между строками."""
        seen: dict[tuple[str, str], str] = {}
        for line in self.lines:
            if line.reporting_type is ReportingType.FULL:
                continue
            for code in line.aggregates:
                source = self._index.get((ReportingType.FULL, code))
                if source is None:
                    raise ValueError(
                        f"упрощённая строка {line.code} укрупняет код {code}, "
                        "отсутствующий в полном наборе"
                    )
                if source.form != line.form:
                    raise ValueError(
                        f"упрощённая строка {line.code} формы {line.form} укрупняет код {code} "
                        f"формы {source.form}"
                    )
                owner = seen.get((line.form, code))
                if owner is not None:
                    raise ValueError(
                        f"код {code} укрупняется сразу строками {owner} и {line.code}"
                    )
                seen[(line.form, code)] = line.code

    def _check_meaning_declared(self) -> None:
        """Совпадение смысла при одинаковом коде объявлено явно и не противоречит составу."""
        for line in self.lines:
            if line.reporting_type is ReportingType.FULL:
                continue
            twin = self._index.get((ReportingType.FULL, line.code))
            if twin is None:
                if line.same_meaning_as_full is not None:
                    raise ValueError(
                        f"кода {line.code} нет в полном наборе, "
                        "признак same_meaning_as_full неприменим"
                    )
                continue
            if line.same_meaning_as_full is None:
                raise ValueError(
                    f"код {line.code} есть в обоих наборах, "
                    "требуется явный признак same_meaning_as_full"
                )
            if line.same_meaning_as_full and tuple(line.aggregates) != (line.code,):
                raise ValueError(
                    f"строка {line.code} объявлена совпадающей по смыслу с полным набором, "
                    f"но укрупняет {', '.join(line.aggregates)}"
                )
            if not line.same_meaning_as_full and normalize_name(line.name) == normalize_name(
                twin.name
            ):
                raise ValueError(
                    f"строка {line.code} объявлена отличной по смыслу, "
                    "но носит то же наименование, что и строка полного набора"
                )

    # --- выборки -------------------------------------------------------------

    def get(
        self, code: str, reporting_type: ReportingType = ReportingType.FULL
    ) -> LineDef | None:
        """Возвращает определение строки или None, если кода нет в наборе."""
        return self._index.get((reporting_type, code))

    def measure_of(
        self, code: str, reporting_type: ReportingType = ReportingType.FULL
    ) -> Measure:
        """Вид величины строки: на дату или за период.

        Умолчание берётся от формы, собственное объявление строки его
        перекрывает. Неизвестный код — величина за период: строки, которых
        нет в справочнике, в формулах не встречаются, а гадать о виде
        незачем — вопрос задаётся только о том, что в формуле стоит.
        """
        line = self.get(code, reporting_type) or self.get(code)
        if line is None:
            return Measure.FLOW
        if line.measure is not None:
            return line.measure
        return self.measures.by_form[line.form]

    def require(self, code: str, reporting_type: ReportingType = ReportingType.FULL) -> LineDef:
        """Возвращает определение строки, иначе поднимает KeyError."""
        line = self._index.get((reporting_type, code))
        if line is None:
            raise KeyError(f"код строки {code} отсутствует в наборе {reporting_type}")
        return line

    def has(self, code: str, reporting_type: ReportingType = ReportingType.FULL) -> bool:
        """Проверяет наличие кода в наборе."""
        return (reporting_type, code) in self._index

    def match_by_name(
        self,
        name: str,
        reporting_type: ReportingType,
        form: str,
        source_code: str | None = None,
    ) -> LineDef | None:
        """Опознаёт строку упрощённой формы по наименованию; неопознанное даёт None.

        Код строки — подсказка, а не ключ: он берётся в расчёт только чтобы
        развести строки-тёзки («БАЛАНС» актива и пассива). Когда наименование
        опознаёт одну строку, код не спрашивается вовсе — в упрощённых формах
        он меняется между периодами.
        """
        if reporting_type is ReportingType.FULL:
            raise ValueError(
                "опознание по наименованию определено только для упрощённых форм: "
                "в полных формах наименования повторяются, ключом служит код строки"
            )
        found = self._by_name.get((reporting_type, form, normalize_name(name)), ())
        if len(found) == 1:
            return found[0]
        if source_code is None:
            return None
        matching = [line for line in found if line.accepts_code(source_code)]
        return matching[0] if len(matching) == 1 else None

    def is_ignored(self, code: str, form: str) -> bool:
        """Объявлен ли код заведомо игнорируемым.

        Игнорируемый код — принятое решение методики, неизвестный — сигнал
        проверить справочник. Первый в журнал качества не пишется.
        """
        return any(rule.matches(code, form) for rule in self.ignored_codes)

    def ignore_reason(self, code: str, form: str) -> str | None:
        """Обоснование, по которому код игнорируется."""
        for rule in self.ignored_codes:
            if rule.matches(code, form):
                return rule.reason
        return None

    def candidates_for_code(
        self, code: str, reporting_type: ReportingType, form: str
    ) -> tuple[LineDef, ...]:
        """Строки набора, которые допускают такой код в отчётности.

        В упрощённых формах перечни допустимых кодов пересекаются: код 1190
        входит и в «Материальные внеоборотные активы», и в «Нематериальные,
        финансовые и другие внеоборотные активы». Разрешать неоднозначность
        обязан вызывающий, молчаливый выбор запрещён.
        """
        return tuple(
            line
            for line in self.lines
            if line.reporting_type is reporting_type
            and line.form == form
            and line.accepts_code(code)
        )

    def for_type(self, reporting_type: ReportingType) -> tuple[LineDef, ...]:
        """Строки одного набора в порядке справочника."""
        return tuple(line for line in self.lines if line.reporting_type is reporting_type)

    def for_form(
        self, form: str, reporting_type: ReportingType = ReportingType.FULL
    ) -> tuple[LineDef, ...]:
        """Строки одной формы одного набора."""
        return tuple(
            line
            for line in self.lines
            if line.form == form and line.reporting_type is reporting_type
        )

    def totals(
        self, form: str | None = None, reporting_type: ReportingType = ReportingType.FULL
    ) -> tuple[LineDef, ...]:
        """Итоговые строки набора: всех форм либо одной."""
        return tuple(
            line
            for line in self.lines
            if line.is_total
            and line.reporting_type is reporting_type
            and (form is None or line.form == form)
        )

    def forms_of(self, reporting_type: ReportingType) -> tuple[str, ...]:
        """Формы, входящие в набор отчётности."""
        return self.reporting_types[reporting_type].forms

    def codes(self, reporting_type: ReportingType = ReportingType.FULL) -> frozenset[str]:
        """Множество известных кодов строк набора."""
        return frozenset(code for kind, code in self._index if kind is reporting_type)


def _allowed_codes(line: LineDef) -> tuple[str, ...]:
    """Коды отчётности, которые строка допускает."""
    return line.code_allowed or (line.code,)


def _check_no_cycles(index: dict[tuple[ReportingType, str], LineDef]) -> None:
    """Проверяет отсутствие циклов в составе итоговых строк."""
    visiting: set[tuple[ReportingType, str]] = set()
    visited: set[tuple[ReportingType, str]] = set()

    def visit(key: tuple[ReportingType, str], path: tuple[str, ...]) -> None:
        if key in visited:
            return
        if key in visiting:
            chain = " -> ".join((*path, key[1]))
            raise ValueError(f"цикл в составе итоговых строк: {chain}")
        visiting.add(key)
        for component in index[key].components:
            visit((key[0], component.code), (*path, key[1]))
        visiting.discard(key)
        visited.add(key)

    for key in index:
        visit(key, ())


def default_path() -> Path:
    """Путь к справочнику строк по умолчанию."""
    return settings.methodology_dir / "lines.yaml"


@lru_cache(maxsize=8)
def load_lines(path: Path | None = None) -> LinesCatalog:
    """Читает и проверяет справочник строк; результат кэшируется по пути."""
    source = Path(path) if path is not None else default_path()
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    return LinesCatalog.model_validate(raw)
