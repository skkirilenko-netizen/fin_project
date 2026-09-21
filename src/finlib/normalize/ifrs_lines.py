"""Справочник статей консолидированной отчётности по МСФО.

Справочник параллельный справочнику РСБУ, а не его продолжение. Причина
в природе данных: кодов строк, утверждённых нормативным актом, в МСФО нет,
позиция опознаётся наименованием через синонимы, а состав статей меняется
от эмитента к эмитенту. В РСБУ первичен код, наименования повторяются
(«Заёмные средства» — и 1410, и 1510), а состав фиксирован формой.

Общее у двух справочников — арифметика состава итогов, и она вынесена
в `quality/totals.py`: контроли сходимости проверяют равенство суммы,
а не природу кодов.

Подтверждённые специфические статьи живут не здесь, а в таблице
`ifrs_line_confirmation`: методика правится руками и диффом, а позиция,
присвоенная во время работы, методикой не является.
"""

import logging
from collections import Counter
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.normalize.lines import Operator, Sign, normalize_name

logger = logging.getLogger(__name__)

# Код позиции: префикс обязателен. Четырёхзначное число в грамматике формул
# уже означает код строки РСБУ, голое строчное имя — константу методики;
# код с точкой не может быть ни тем, ни другим и в fact_report.line_code
# с первого взгляда отличим от кода РСБУ.
CODE_PATTERN = r"^ifrs\.[a-z][a-z0-9_]*$"


class Alias(BaseModel):
    """Наименование, под которым позиция встречена в отчётности эмитента.

    Эмитент назван не для порядка: через полгода при решении, поднимать ли
    специфическую статью в ядро, нужно видеть, откуда взялось написание,
    а не доверять памяти.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    seen_at: str = Field(min_length=1)


class IfrsComponent(BaseModel):
    """Слагаемое итоговой позиции с оператором."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=CODE_PATTERN)
    op: Operator = Operator.PLUS


class IfrsPosition(BaseModel):
    """Позиция унифицированной модели статей."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=CODE_PATTERN)
    name: str = Field(min_length=1)
    form: str = Field(pattern=CODE_PATTERN)
    # Формы, в которых та же позиция встречается ещё. **Один код в двух формах
    # правомерен, а величина принадлежит форме.** Неденежные корректировки
    # косвенного метода по определению повторяют статьи отчёта о прибыли —
    # налог, курсовые разницы, обесценение, — и двойники справочника для них
    # хуже, чем одна позиция, объявившая обе формы: по паре зеркал расходятся
    # и наименования, и состав синонимов.
    #
    # Форма при этом не перестаёт различать: перечень объявлен у позиции,
    # а не отменён вовсе. «Прибыль до налогообложения» стоит и в ОПУ,
    # и первой строкой косвенного метода, означая разное, — и обе остаются
    # разными позициями, потому что вторая форма у них не объявлена.
    also_in_forms: tuple[str, ...] = ()
    section: str = Field(min_length=1)
    sign: Sign = Sign.POSITIVE
    in_brackets: bool = False
    is_total: bool = False
    # Обычный знак величины в отчётности: −1 у вычитаемых статей
    # (себестоимость, расходы, налог, собственные акции), +1 у прочих.
    # Подсказка для случая, когда эмитент печатает расход без скобок:
    # знак тогда выводится арифметикой, а нормальный знак говорит, какую
    # инверсию пробовать первой.
    normal_sign: int = 1
    components: tuple[IfrsComponent, ...] = ()
    # Запасные составы итога: та же величина, набранная иначе. Эмитенты
    # раскрывают отчёт о прибылях по-разному, и промежуточной строки может
    # не быть вовсе: у Сегежи валовой прибыли нет, операционный убыток
    # набирается прямо из выручки и расходов. Один состав на всех означал бы,
    # что у такого эмитента арифметика ОПУ не проверяется ничем.
    alternative_components: tuple[tuple[IfrsComponent, ...], ...] = ()
    # Распределение позиции: на что она делится, а не из чего набирается.
    # Это **тождество, а не второй способ получить величину**: прибыль
    # за период равна сумме причитающегося акционерам материнской компании
    # и неконтролирующим долям (МСФО (IAS) 1.81B), и это отдельная проверка.
    # Запасным составом такое объявить нельзя: сошедшееся распределение
    # закрыло бы собой несошедшуюся цепочку прибыли, то есть скрыло бы
    # дефект вместо того, чтобы его показать.
    split_into: tuple[IfrsComponent, ...] = ()
    # Складывается ли позиция из нескольких строк раздела. По умолчанию да:
    # «Прочие доходы» и «Прочие расходы» у ЛСР — две строки одного сальдо,
    # и сумма их и есть величина позиции.
    #
    # Но бывает наоборот: одна и та же формулировка стоит в форме дважды,
    # у разных итогов. У Акрона «Собственникам Компании» напечатано и под
    # прибылью, и под общим совокупным доходом; сумма 75 439 не существует
    # нигде — ни в отчётности, ни в природе. Раздел таких строк не различает:
    # они стоят ниже всех опознанных итогов, и раздела у них нет вовсе.
    # Тогда несколько строк — спор, а не сумма: величина не берётся, строки
    # уходят на разметку.
    summable: bool = True
    aliases: tuple[Alias, ...] = ()
    # Безусловная оговорка о содержании позиции: верна для любого эмитента
    # и идёт в раздел «Ограничения анализа».
    note: str | None = None

    @property
    def match_names(self) -> tuple[str, ...]:
        """Нормализованные наименования, по которым позиция опознаётся.

        Собственное наименование входит в перечень наравне с синонимами:
        оно и есть первое из них.
        """
        names = (self.name, *(item.name for item in self.aliases))
        return tuple(dict.fromkeys(normalize_name(name) for name in names))

    def occurs_in(self, form: str) -> bool:
        """Встречается ли позиция в этой форме — своей или объявленной второй.

        Правило одно на все места, где спрашивают про форму: прежде проверка
        стояла и в справочнике, и в разборе (`found.form == form_code`),
        и свёрнутое зеркало опознавалось справочником, но отбрасывалось
        разбором — величина терялась молча.
        """
        return form == self.form or form in self.also_in_forms

    @property
    def compositions(self) -> tuple[tuple[IfrsComponent, ...], ...]:
        """Все составы итога: основной первым, запасные следом."""
        if not self.components:
            return ()
        return (self.components, *self.alternative_components)

    @model_validator(mode="after")
    def _total_has_components(self) -> Self:
        """Состав есть только у итоговых позиций и только непустой."""
        if self.is_total and not self.components:
            raise ValueError(f"итоговая позиция {self.code} объявлена без состава")
        if self.components and not self.is_total:
            raise ValueError(f"позиция {self.code} не итоговая, но имеет состав")
        if self.alternative_components and not self.components:
            raise ValueError(
                f"позиция {self.code} объявила запасной состав без основного"
            )
        if any(not item for item in self.alternative_components):
            raise ValueError(f"у позиции {self.code} пустой запасной состав")
        return self


class IgnoredSubject(BaseModel):
    """Предмет, который методика осознанно не использует.

    **Игнорируемое наименование — принятое решение, неопознанное —
    недоработка**, и различать их обязательно: то же различие, что у РСБУ
    между `ignored_codes` и `unknown_line_code`. Причина потому и обязательна:
    без неё через полгода не отличить решение от забытой строки.

    Перечень написаний может быть пуст: предмет объявлен, а строка в разобранных
    комплектах не встречена. Это честнее выдуманного написания — игнорировать
    нечего до первой встречи.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    names: tuple[Alias, ...] = ()

    @property
    def match_names(self) -> tuple[str, ...]:
        """Нормализованные написания, по которым строка игнорируется."""
        return tuple(dict.fromkeys(normalize_name(item.name) for item in self.names))


class FormDef(BaseModel):
    """Раздел консолидированной отчётности.

    Кодов ОКУД у них нет: это не формы, утверждённые приказом, и называются
    они у эмитентов по-разному — отсюда синонимы и здесь.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()

    @property
    def match_names(self) -> tuple[str, ...]:
        """Нормализованные наименования раздела."""
        return tuple(
            dict.fromkeys(normalize_name(item) for item in (self.name, *self.aliases))
        )


class MaterialityBase(BaseModel):
    """Чем мерится существенность строки этой формы — или почему нечем."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base: str | None = None
    no_base_reason: str | None = None
    # Позиции, равные базе по величине: та же величина с другой стороны
    # формы. Устройство то же, что в справочнике РСБУ.
    same_as_base: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_declared(self) -> Self:
        """Объявлено ровно одно из двух: база или причина её отсутствия."""
        if (self.base is None) == (self.no_base_reason is None):
            raise ValueError(
                "у формы объявляется либо база существенности, либо причина, "
                "по которой её нет; молчание и оба сразу не допускаются"
            )
        return self


class Materiality(BaseModel):
    """Порог существенности специфической статьи и база его применения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    share_of_total_assets: Decimal = Field(gt=0, lt=1)
    origin: str = Field(min_length=1)
    # База по формам. Форма, о базе умолчавшая, существенность измерить
    # не даёт — и это не то же самое, что база, объявленная отсутствующей:
    # первое — наш пробел, второе — свойство формы.
    bases: dict[str, MaterialityBase] = Field(min_length=1)

    @property
    def base_codes(self) -> frozenset[str]:
        """Базы и равные им позиции: величины, которые не ранжируются.

        У баланса таких две: итог актива и итог пассива — одна величина,
        напечатанная дважды. Перечень нужен перечню наибольших изменений:
        доля изменения базы в себе самой равна единице, и статья выходила
        наверх у каждого эмитента, ничего о нём не говоря.
        """
        found: set[str] = set()
        for item in self.bases.values():
            if item.base is not None:
                found.add(item.base)
            found.update(item.same_as_base)
        return frozenset(found)


class CoreCandidate(BaseModel):
    """Когда подтверждённая статья становится кандидатом в ядро."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distinct_issuers: int = Field(ge=2)
    origin: str = Field(min_length=1)


class IfrsCatalog(BaseModel):
    """Унифицированная модель статей консолидированной отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    forms: dict[str, FormDef]
    positions: tuple[IfrsPosition, ...] = Field(min_length=1)
    # Наименования, которые методика не использует осознанно. Пусто быть
    # не обязано: перечень объявлен, и ноль в нём означает «ничего
    # не игнорируем», а не «не смотрели».
    ignored: tuple[IgnoredSubject, ...] = ()
    materiality: Materiality
    core_candidate: CoreCandidate

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Справочник связен: коды уникальны, состав существует, синонимы врозь."""
        self._check_codes_are_unique()
        self._check_forms_exist()
        self._check_components_exist()
        self._check_aliases_do_not_overlap()
        self._check_ignored_are_not_positions()
        self._check_materiality_bases()
        return self

    def _check_materiality_bases(self) -> None:
        """У каждой формы объявлена база существенности либо её отсутствие.

        Молчание формы читалось бы как «базы нет», то есть наш пробел
        выглядел бы решением методики. База, названная позицией, которой
        в справочнике нет, дала бы то же самое молча.
        """
        listed = set(self.materiality.bases)
        silent = set(self.forms) - listed
        if silent:
            raise ValueError(
                "формы не объявили базу существенности: "
                + ", ".join(sorted(silent))
            )
        stray = listed - set(self.forms)
        if stray:
            raise ValueError(
                "база существенности объявлена у неизвестных форм: "
                + ", ".join(sorted(stray))
            )
        known = {item.code for item in self.positions}
        for form, declared in sorted(self.materiality.bases.items()):
            if declared.base is not None and declared.base not in known:
                raise ValueError(
                    f"база существенности формы {form} — неизвестная позиция "
                    f"{declared.base}"
                )

    def _check_ignored_are_not_positions(self) -> None:
        """Наименование не может быть и позицией, и игнорируемым.

        Такое наименование опознавалось бы и отбрасывалось одновременно,
        а какое из двух случится — зависело бы от порядка проверок в коде.
        Решение должно быть одно, и противоречие обязано находиться
        при загрузке справочника, а не при разборе отчётности.
        """
        known = {name for item in self.positions for name in item.match_names}
        clashing = sorted(
            name
            for subject in self.ignored
            for name in subject.match_names
            if name in known
        )
        if clashing:
            raise ValueError(
                "наименования объявлены и позицией, и игнорируемыми: "
                + ", ".join(f"«{name}»" for name in clashing)
            )

    def _check_codes_are_unique(self) -> None:
        """Один код — одна позиция."""
        repeated = [
            code
            for code, count in Counter(item.code for item in self.positions).items()
            if count > 1
        ]
        if repeated:
            raise ValueError(f"коды позиций повторяются: {', '.join(sorted(repeated))}")

    def _check_forms_exist(self) -> None:
        """Позиция объявлена в разделе, который есть в справочнике."""
        unknown = {
            form
            for item in self.positions
            for form in (item.form, *item.also_in_forms)
        } - set(self.forms)
        if unknown:
            raise ValueError(
                f"позиции ссылаются на неизвестные разделы: {', '.join(sorted(unknown))}"
            )

    def _check_components_exist(self) -> None:
        """Все слагаемые итогов существуют и лежат в том же разделе.

        Итог, ссылающийся на несуществующую позицию, не сойдётся никогда,
        а контроль сходимости сообщит об этом как о дефекте отчётности —
        то есть свалит нашу недоработку на эмитента.
        """
        known = {item.code for item in self.positions}
        for position in self.positions:
            # Запасные составы проверяются наравне с основным: ошибка в них
            # так же превращается в вечно несходящийся итог. Распределение —
            # тоже: тождество с несуществующей позицией не сойдётся никогда.
            listed = [
                *(item for group in position.compositions for item in group),
                *position.split_into,
            ]
            missing = [item.code for item in listed if item.code not in known]
            if missing:
                raise ValueError(
                    f"в составе {position.code} названы неизвестные позиции: "
                    f"{', '.join(sorted(missing))}"
                )
            by_code = {item.code: item for item in self.positions}
            other_form = [
                item.code for item in listed if by_code[item.code].form != position.form
            ]
            if other_form:
                raise ValueError(
                    f"в составе {position.code} названы позиции другого раздела: "
                    f"{', '.join(sorted(other_form))}"
                )

    def _check_aliases_do_not_overlap(self) -> None:
        """Наименование не может принадлежать двум позициям одного раздела.

        Опознание идёт по наименованию, и пересечение синонимов внутри
        раздела означает, что статья ляжет в ту позицию, которая встретилась
        раньше, — то есть произвольно. Это дефект справочника, и находиться
        он должен при загрузке, а не при разборе файла эмитента.

        **Между разделами повтор правомерен и неизбежен.** «Кредиты и займы»
        стоят в балансе дважды — в долгосрочных обязательствах и
        в краткосрочных, — и называются одинаково; в РСБУ их различает код
        строки (1410 и 1510), в МСФО кода нет, и различает раздел. Прежде
        справочник такого не допускал, поэтому обе строки опознавались одной
        позицией, и краткосрочный долг затирал долгосрочный: у ЛСР вместо
        328 256 выходило 35 876, и то же у Норникеля и Сегежи.
        """
        owners: dict[tuple[str, str, str], list[str]] = {}
        for position in self.positions:
            for name in position.match_names:
                # Объявившая вторую форму позиция занимает наименование
                # и в ней: иначе тёзка из второй формы опознавался бы
                # произвольно — именно тем, что встретилось раньше.
                for form in (position.form, *position.also_in_forms):
                    key = (form, position.section, name)
                    owners.setdefault(key, []).append(position.code)
        overlapping = {
            key: codes for key, codes in owners.items() if len(codes) > 1
        }
        if overlapping:
            listed = "; ".join(
                f"«{name}» в разделе {section} — {', '.join(sorted(codes))}"
                for (_, section, name), codes in sorted(overlapping.items())
            )
            raise ValueError(f"наименования принадлежат нескольким позициям: {listed}")

    def get(self, code: str) -> IfrsPosition | None:
        """Позиция по коду; None — кода нет в справочнике."""
        return next((item for item in self.positions if item.code == code), None)

    def ignored_subject(self, name: str) -> IgnoredSubject | None:
        """Предмет, ради которого строка игнорируется; None — не игнорируется.

        Сравнение дословное, приведёнными наименованиями — как у синонимов.
        По вхождению слова игнорирование однажды проглотило бы настоящую
        статью, а потеря величины здесь так же тиха, как везде в ветке.
        """
        normalized = normalize_name(name)
        if not normalized:
            return None
        return next(
            (item for item in self.ignored if normalized in item.match_names), None
        )

    def require(self, code: str) -> IfrsPosition:
        """Позиция по коду; отсутствие — ошибка справочника."""
        found = self.get(code)
        if found is None:
            raise KeyError(f"позиции {code} нет в справочнике МСФО")
        return found

    def match_by_name(
        self, name: str, section: str | None = None, form: str | None = None
    ) -> IfrsPosition | None:
        """Позиция по наименованию из отчётности; None — не опознана.

        Неопознанная статья не теряется: её обязан записать разбор файла,
        и она же требует ручного подтверждения на экране сверки (задача 23).

        `section` нужен наименованиям, которые повторяются в разных разделах
        («Кредиты и займы» — и в долгосрочных обязательствах, и
        в краткосрочных). Без него такое наименование не опознаётся вовсе:
        отдать первую попавшуюся позицию значит отдать произвольную.

        `form` отсекает тёзок из других форм, и это не то же самое, что
        раздел. «Прибыль до налогообложения» стоит и в отчёте о прибыли или
        убытке, и первой строкой косвенного метода в отчёте о движении
        денежных средств — это разные величины с разным смыслом. Без формы
        такое наименование переставало опознаваться вовсе, и в ОПУ терялся
        итог, который прежде опознавался.
        """
        normalized = normalize_name(name)
        found = [item for item in self.positions if normalized in item.match_names]
        if form is not None:
            # Позиция, объявившая вторую форму, опознаётся и в ней: код один,
            # а величина принадлежит форме строки и в итоги другой формы
            # не входит. Правило одно и живёт у самой позиции.
            found = [item for item in found if item.occurs_in(form)]
        if not found:
            return None
        if len(found) == 1:
            return found[0]
        if section is None:
            return None
        in_section = [item for item in found if item.section == section]
        return in_section[0] if len(in_section) == 1 else None

    def ambiguous_name(self, name: str) -> bool:
        """Повторяется ли наименование в нескольких разделах справочника."""
        normalized = normalize_name(name)
        return (
            sum(1 for item in self.positions if normalized in item.match_names) > 1
        )

    def match_form(self, name: str) -> str | None:
        """Код раздела по его заголовку в отчётности; None — не опознан."""
        normalized = normalize_name(name)
        return next(
            (code for code, form in self.forms.items() if normalized in form.match_names),
            None,
        )

    def totals(self, form: str | None = None) -> tuple[IfrsPosition, ...]:
        """Итоговые позиции — те, что проверяются контролями сходимости."""
        return tuple(
            item
            for item in self.positions
            if item.is_total and (form is None or item.form == form)
        )

    def for_form(self, form: str) -> tuple[IfrsPosition, ...]:
        """Позиции одного раздела отчётности."""
        return tuple(item for item in self.positions if item.form == form)


def default_path() -> Path:
    """Путь к справочнику статей МСФО."""
    return settings.methodology_dir / "ifrs_lines.yaml"


@lru_cache(maxsize=8)
def load_ifrs_lines(path: Path | None = None) -> IfrsCatalog:
    """Читает унифицированную модель статей консолидированной отчётности."""
    source = Path(path) if path is not None else default_path()
    catalog = IfrsCatalog.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )
    logger.info(
        "справочник МСФО %s: позиций %d, из них итоговых %d",
        catalog.version,
        len(catalog.positions),
        len(catalog.totals()),
    )
    return catalog
