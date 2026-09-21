"""Одна величина позиции из фактов двух форм — правило выбора, одно на проект.

**Один код в двух формах правомерен, а величина принадлежит форме.**
Неденежные корректировки отчёта о движении денежных средств повторяют статьи
отчёта о прибыли — налог, проценты, обесценение, — и в базе это два разных
факта: ключ `fact_report` форму содержит. Читающий по коду без формы получал
то из двух, что пришло позже: у Сегежи налог на прибыль равен −4 784 в отчёте
о прибыли и +4 784 в потоке, и величины различаются знаком. Выбор зависел
от порядка строк в ответе базы, то есть был произволен.

**Порядок выбора такой.** Есть величина в форме, объявленной у позиции, —
берётся она. Нет — берётся единственная оставшаяся, и это **называется**:
величина пришла из чужой формы, и молчать об этом нельзя. Отбросить её было
бы хуже: у эмитента, раскрывшего амортизацию в отчёте о прибыли, а не
корректировкой потока, EBITDA не посчиталась бы вовсе, а присвоение, сделанное
человеком, перестало бы работать молча — ровно тот дефект, из-за которого
подтверждённые статьи не попадали в факты.

У РСБУ проверять нечего: четырёхзначный код принадлежит одной форме
по устройству нумерации, и справочник здесь не спрашивается.
"""

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from finlib.standards import Standard

logger = logging.getLogger(__name__)


def own_form(code: str, form: str | None, standard: Standard) -> bool:
    """Объявлена ли эта форма у позиции с таким кодом.

    Код, которого в справочнике нет, судить нечем — присвоенный человеком
    специфический код это норма, — и такая величина считается своей: отбросить
    её значило бы потерять факт из-за неполноты справочника.
    """
    if standard is not Standard.IFRS:
        return True
    from finlib.normalize.ifrs_lines import load_ifrs_lines

    return load_ifrs_lines().is_own_form(code, form)


def pick_by_form(
    rows: Iterable[dict[str, Any]],
    standard: Standard,
    *,
    code_key: str = "line_code",
    form_key: str = "form_code",
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Оставляет по одной строке на код: своя форма важнее чужой.

    Отдаёт отобранные строки и коды, величина которых взята из чужой формы, —
    счётчик проверенного лежит рядом с самим выбором, иначе ноль таких кодов
    неотличим от невыполненного правила.
    """
    listed: Sequence[dict[str, Any]] = list(rows)
    if standard is not Standard.IFRS:
        return list(listed), ()

    own: dict[str, dict[str, Any]] = {}
    foreign: dict[str, dict[str, Any]] = {}
    for row in listed:
        code = row[code_key]
        if own_form(code, row.get(form_key), standard):
            own.setdefault(code, row)
        else:
            foreign.setdefault(code, row)
    taken_from_foreign = tuple(sorted(set(foreign) - set(own)))
    chosen = [*own.values(), *(foreign[code] for code in taken_from_foreign)]
    if taken_from_foreign:
        logger.info(
            "величины взяты из чужой формы (в своей их нет): %s",
            ", ".join(taken_from_foreign),
        )
    return chosen, taken_from_foreign
