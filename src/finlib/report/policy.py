"""Правила состава документа: обязательные величины, вопросы, предложения.

Состав разделов не может зависеть от того, что модель сочтёт заслуживающим
упоминания. Экспертная оценка показала, чем это кончается: раздел «Фактическая
база» без совокупного долга и выручки, вопросы о строках, которых применённый
набор форм не предусматривает, и документ без вывода о дальнейших действиях.

Здесь читается `methodology/report.yaml`: перечни машинные, формулировки
предписанные. Ни один текст отсюда не переписывается — ни моделью, ни кодом.
"""

import logging
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.standards import Standard

logger = logging.getLogger(__name__)


class Trigger(StrEnum):
    """Машинные признаки, по которым выводится предложение о действиях.

    Признак — факт расчёта, а не суждение: сработал сигнал, сработал
    стоп-фактор, класс не присвоен, комплект отбракован, данные устарели.
    """

    ALWAYS = "always"
    SUPERVISORY_SIGNAL = "supervisory_signal"
    ATTENTION_SIGNAL = "attention_signal"
    STOP_FACTOR = "stop_factor"
    NO_CLASS = "no_class"
    FLAG_CONFLICT = "flag_conflict"
    STALE_DATA = "stale_data"
    QUARANTINED_SET = "quarantined_set"
    BLOCKING_CHECK_FAILED = "blocking_check_failed"


class QuestionSubject(StrEnum):
    """Основания вопросов к организации, в порядке тяжести последствий."""

    SUPERVISORY_SIGNAL = "supervisory_signal"
    # Планы руководства при объявленной неопределённости непрерывности:
    # аудитор указывает, что они раскрыты, но их исполнимости не оценивает —
    # это и есть вопрос к организации, а не к отчётности.
    GOING_CONCERN_PLANS = "going_concern_plans"
    STOP_FACTOR = "stop_factor"
    FLAG_CONFLICT = "flag_conflict"
    ATTENTION_SIGNAL = "attention_signal"
    QUARANTINED_SET = "quarantined_set"
    MISSING_METRIC = "missing_metric"


class Freshness(BaseModel):
    """Допустимый разрыв между отчётной датой и днём формирования документа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_months: int = Field(gt=0)
    origin: str = Field(min_length=1)
    statement: str = Field(min_length=1)

    def stale(self, months: int) -> bool:
        """Превышен ли разрыв."""
        return months > self.max_months

    def message(self, months: int) -> str:
        """Предписанная оговорка с подставленным разрывом."""
        return " ".join(self.statement.split()).replace("{months}", str(months))


class FactBase(BaseModel):
    """Обязательный состав раздела «Фактическая база» одного стандарта."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[str, ...] = Field(min_length=1)
    metrics: tuple[str, ...] = Field(min_length=1)
    top_changes: int = Field(ge=0)
    origin: str = Field(min_length=1)

    def check_lines(self, standard: Standard) -> None:
        """Проверяет, что строки названы кодами **своего** стандарта.

        Проверка разведена по стандартам, а не снята: у РСБУ код утверждён
        приказом и состоит из четырёх цифр, у МСФО таких кодов нет вовсе,
        и статья называется позицией унифицированной модели. Снятая проверка
        без замены — это ровно ноль срабатываний: перечень с опечаткой
        не нашёл бы ни одной величины и прошёл бы как выполненный.

        Позиция МСФО ищется в обоих справочниках ветки: статьи форм живут
        в `ifrs_lines.yaml`, а величины примечаний — в `ifrs_note_lines.yaml`,
        и `ifrs.interest_expense_accrued` стоит именно там. Искать только
        в первом значило бы запретить обязательную величину, которая у ветки
        главная: без неё покрытие процентов не совпадает ни с одной строкой
        отчёта о прибыли или убытке.
        """
        if standard is Standard.RSBU:
            for code in self.lines:
                if not (code.isdigit() and len(code) == 4):
                    raise ValueError(f"«{code}» не похож на код строки отчётности")
            return

        from finlib.normalize.ifrs_lines import load_ifrs_lines
        from finlib.normalize.ifrs_note_lines import load_note_lines

        known = {item.code for item in load_ifrs_lines().positions}
        known |= {item.code for item in load_note_lines().lines}
        unknown = [code for code in self.lines if code not in known]
        if unknown:
            raise ValueError(
                "в справочниках МСФО нет позиций: " + ", ".join(unknown)
            )

    def required(
        self, lines: frozenset[str] | set[str], metrics: frozenset[str] | set[str]
    ) -> tuple[str, ...]:
        """Обязательные величины, существующие у этой организации.

        Отсутствующие отбрасываются: требовать назвать нераскрытую строку
        или нерассчитанный показатель значило бы требовать выдумать величину.
        """
        return (
            *(code for code in self.lines if code in lines),
            *(code for code in self.metrics if code in metrics),
        )


class Risks(BaseModel):
    """Предписанные тексты раздела «Риски и надзорные сигналы».

    Раздел собирается расчётом целиком. Тексты живут в методике, а не в коде:
    свободных строк в документе быть не должно ровно по той же причине,
    по какой их не должно быть в контролях качества.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intro: str = Field(min_length=1)
    none_found: str = Field(min_length=1)
    stop_factor_intro: str = Field(min_length=1)
    # Вступление при нескольких сработавших: согласование со числом — часть
    # формулировки, и подбирать форму слова в коде мы не будем.
    stop_factors_intro: str = Field(min_length=1)
    # Оговорка к стоп-фактору, ограничение которого слабее присвоенного класса.
    cap_not_binding: str = Field(min_length=1)
    cap_not_binding_named: str = Field(min_length=1)
    caps_not_binding_named: str = Field(min_length=1)

    @staticmethod
    def _folded(text: str) -> str:
        """Складчатая формулировка справочника одной строкой."""
        return " ".join(text.split())

    @property
    def intro_text(self) -> str:
        """Вступление раздела."""
        return self._folded(self.intro)

    @property
    def none_found_text(self) -> str:
        """Оговорка при отсутствии срабатываний."""
        return self._folded(self.none_found)

    @property
    def stop_factor_text(self) -> str:
        """Вступление к стоп-фактору."""
        return self._folded(self.stop_factor_intro)

    def stop_factors_text(self, count: int) -> str:
        """Вступление к стоп-факторам по их числу."""
        return self._folded(
            self.stop_factor_intro if count == 1 else self.stop_factors_intro
        )

    @property
    def cap_not_binding_text(self) -> str:
        """Оговорка об ограничении, слабее присвоенного класса."""
        return self._folded(self.cap_not_binding)

    def cap_not_binding_of(self, names: tuple[str, ...]) -> str:
        """Та же оговорка с перечнем стоп-факторов — для картины рисков."""
        listed = ", ".join(f"«{name}»" for name in names)
        text = (
            self.cap_not_binding_named
            if len(names) == 1
            else self.caps_not_binding_named
        )
        return self._folded(text).format(names=listed)


class Questions(BaseModel):
    """Сколько вопросов задавать и в каком порядке их основания."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_count: int = Field(gt=0)
    max_count: int = Field(gt=0)
    subject_order: tuple[QuestionSubject, ...] = Field(min_length=1)
    origin: str = Field(min_length=1)
    texts: dict[QuestionSubject, str] = Field(min_length=1)
    # Где раскрыты планы руководства: названное аудитором примечание либо,
    # если он его не назвал, примечания вообще. Выдумывать номер нельзя.
    plans_where: dict[str, str] = Field(min_length=2)
    none_found: str = Field(min_length=1)

    def plans_where_text(self, note: int | None) -> str:
        """Место раскрытия планов руководства словами методики."""
        if note is None:
            return " ".join(self.plans_where["unknown"].split())
        return " ".join(self.plans_where["known"].split()).format(note=note)

    @model_validator(mode="after")
    def _check_counts(self) -> Self:
        """Нижняя граница не выше верхней, основания не повторяются."""
        if self.min_count > self.max_count:
            raise ValueError(
                f"вопросов не может быть от {self.min_count} до {self.max_count}"
            )
        if len(set(self.subject_order)) != len(self.subject_order):
            raise ValueError("основания вопросов в порядке повторяются")
        return self

    @model_validator(mode="after")
    def _check_texts(self) -> Self:
        """У каждого основания есть предписанная формулировка вопроса.

        Основание без формулировки означало бы вопрос, который некому задать:
        расчёт нашёл обстоятельство, а сказать о нём нечем.
        """
        missing = [item for item in self.subject_order if item not in self.texts]
        if missing:
            listed = ", ".join(item.value for item in missing)
            raise ValueError(f"нет формулировок вопросов для оснований: {listed}")
        return self

    def question(self, subject: QuestionSubject, **values: str) -> str:
        """Предписанный вопрос по основанию с подставленными величинами."""
        return " ".join(self.texts[subject].split()).format(**values)

    @property
    def none_found_text(self) -> str:
        """Оговорка при отсутствии оснований."""
        return " ".join(self.none_found.split())


class FactBaseSection(BaseModel):
    """Предписанные тексты раздела «Фактическая база»."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intro: dict[Standard, str]
    extra_intro: str = Field(min_length=1)

    @model_validator(mode="after")
    def _every_standard_has_its_wording(self) -> Self:
        """Вступление объявлено для каждого стандарта отчётности.

        Кодов строк, утверждённых нормативным актом, в консолидированной
        отчётности нет, и обещать их читателю нельзя. Пропуск формулировки
        для стандарта — не повод молча напечатать чужую.
        """
        missing = set(Standard) - set(self.intro)
        if missing:
            listed = ", ".join(sorted(item.value for item in missing))
            raise ValueError(
                f"вступление «Фактической базы» не задано для стандартов: {listed}"
            )
        return self

    def intro_text(self, standard: Standard = Standard.RSBU) -> str:
        """Вступление раздела одной строкой, по стандарту отчётности."""
        return " ".join(self.intro[standard].split())

    @property
    def extra_intro_text(self) -> str:
        """Вступление к величинам сверх обязательных."""
        return " ".join(self.extra_intro.split())


class StatementsWording(BaseModel):
    """Как называется отчётность стандарта — в падежах, нужных формулировкам.

    «Бухгалтерская отчётность» в заключении по МСФО — утверждение о другом
    предмете. Словоформы объявлены методикой, а не выводятся кодом: падеж —
    свойство языка, и подбирать его программой значило бы завести в коде
    правило русской морфологии.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    genitive: str = Field(min_length=1)
    accusative: str = Field(min_length=1)


class OtherIssuers(BaseModel):
    """Правило «заключение об одной организации не называет другую».

    Родовые слова наименований и наименьшая длина опознавательного слова
    объявлены методикой, а не подобраны в коде: «Группа» стоит в наименовании
    у четырёх эмитентов набора, и признаком она быть не может.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    generic_words: tuple[str, ...] = Field(min_length=1)
    min_word_length: int = Field(ge=2)
    origin: str = Field(min_length=1)

    def phrase_of(self, name: str) -> str:
        """Опознавательная часть наименования: без формы и родовых слов.

        **Берётся часть целиком, а не отдельные слова.** Слово наименования
        бывает обычным словом языка: у «ДОМ.РФ Ипотечный агент» это «дом»,
        и запрет на него блокировал бы всякий документ. Оставшаяся часть
        как целое — «дом рф ипотечный агент» — в чужом заключении не стоит,
        а «лср» и «черкизово» стоят ровно там, где наша пометка протекла.

        Перечень неопознавательных слов целиком в методике: организационная
        форма стоит у большинства организаций, и по ней их не различить.
        """
        generic = {item.casefold() for item in self.generic_words}
        words = [
            word
            for word in re.findall(r"[\w’']+", name.casefold())
            if word not in generic
        ]
        phrase = " ".join(words)
        return phrase if len(phrase.replace(" ", "")) >= self.min_word_length else ""

    def rank_of(self, subject: QuestionSubject) -> int:
        """Место основания в порядке; неизвестное уходит в конец."""
        order = list(self.subject_order)
        return order.index(subject) if subject in order else len(order)


class Action(BaseModel):
    """Предложение по дальнейшим действиям с предписанной формулировкой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    when: tuple[Trigger, ...] = Field(min_length=1)
    text: str = Field(min_length=1)
    # Разделы, на которые предложение ссылается словами. Объявлены, потому что
    # ссылка на пустой раздел — противоречие: у ФосАгро «Запрос пояснений»
    # отсылал к «Рискам и надзорным сигналам», где расчёт не выявил ничего.
    # Сверяется при сборке документа.
    refers_to: tuple[int, ...] = ()

    def fires(self, triggers: set[Trigger]) -> bool:
        """Выполнен ли хотя бы один признак."""
        return Trigger.ALWAYS in self.when or bool(triggers & set(self.when))

    @property
    def message(self) -> str:
        """Формулировка одной строкой, как она уйдёт в документ."""
        return " ".join(self.text.split())


class ReportPolicy(BaseModel):
    """Справочник правил состава документа."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    freshness: Freshness
    risks: Risks
    # Состав фактической базы объявлен по стандартам: перечень одного,
    # применённый к фактам другого, нашёл бы ноль величин и отбросил их все
    # как отсутствующие — проверка состава прошла бы, не проверив ничего.
    fact_base: dict[Standard, FactBase]
    fact_base_section: FactBaseSection
    statements_wording: dict[Standard, StatementsWording]
    other_issuers: OtherIssuers
    questions: Questions
    actions: tuple[Action, ...] = Field(min_length=1)

    def fill(self, text: str, standard: Standard) -> str:
        """Подставляет наименование отчётности стандарта в предписанный текст.

        Подстановка явная, а не через `format`: предписанные тексты содержат
        и другие фигурные скобки, и общий разбор шаблона однажды съел бы
        подстановку вопроса.
        """
        words = self.statements_wording[standard]
        return text.replace("{statements_genitive}", words.genitive).replace(
            "{statements_accusative}", words.accusative
        )

    @model_validator(mode="after")
    def _check_codes(self) -> Self:
        """Коды предложений уникальны, состав объявлен у каждого стандарта."""
        codes = [item.code for item in self.actions]
        if len(set(codes)) != len(codes):
            raise ValueError("коды предложений по действиям повторяются")
        missing = [item.value for item in Standard if item not in self.fact_base]
        if missing:
            raise ValueError(
                "состав фактической базы не объявлен у стандартов: "
                + ", ".join(missing)
            )
        # Наименование отчётности обязано быть объявлено у каждого стандарта:
        # молча напечатать чужое — то же, что обещать читателю коды строк там,
        # где их нет.
        unnamed = [
            item.value for item in Standard if item not in self.statements_wording
        ]
        if unnamed:
            raise ValueError(
                "наименование отчётности не объявлено у стандартов: "
                + ", ".join(unnamed)
            )
        for standard, composition in self.fact_base.items():
            composition.check_lines(standard)
        return self

    def fact_base_of(self, standard: Standard) -> FactBase:
        """Обязательный состав для этого стандарта."""
        return self.fact_base[standard]

    def actions_for(self, triggers: set[Trigger]) -> list[Action]:
        """Предложения, условия которых выполнены, в порядке справочника."""
        return [item for item in self.actions if item.fires(triggers)]


def default_path() -> Path:
    """Путь к справочнику правил состава документа."""
    return settings.methodology_dir / "report.yaml"


@lru_cache(maxsize=8)
def load_policy(path: Path | None = None) -> ReportPolicy:
    """Читает правила состава документа."""
    source = Path(path) if path is not None else default_path()
    return ReportPolicy.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )


def months_between(earlier, later) -> int:
    """Полных месяцев между отчётной датой и днём формирования документа."""
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)
