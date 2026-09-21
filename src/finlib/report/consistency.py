"""Контроли согласованности документа.

Документ собирается из нескольких источников: раздел 1 и приложение — из базы,
разделы 2–6 — моделью, оговорки — из методики. Каждый кусок по отдельности
верен, а вместе они могут противоречить друг другу.

Так и вышло: «Ограничения анализа» сообщали, что отчётность за 2025 год
в расчёт не включена, а «Происхождение документа» перечисляло её среди
принятых. Ложным было второе: выборка комплектов не отсекала карантин.

Противоречие в документе хуже отсутствия сведений, поэтому такие контроли
блокирующие: документ не формируется.
"""

import logging
import re
from dataclasses import dataclass

from finlib.metrics.display import round_to
from finlib.report.data import ReportData

logger = logging.getLogger(__name__)

# Год в тексте оговорки о невключённой отчётности.
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


class InconsistentReportError(RuntimeError):
    """Документ противоречит сам себе и не формируется."""

    def __init__(self, problems: list[str]) -> None:
        listed = "; ".join(problems)
        super().__init__(f"документ противоречив и не сформирован: {listed}")
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Inconsistency:
    """Найденное противоречие."""

    code: str
    message: str


def check_document(data: ReportData, limitations: str) -> list[Inconsistency]:
    """Проверяет согласованность разделов документа между собой.

    Единица измерения здесь не сверяется: её проверяет `check_unit`
    по **собранному** документу — до сборки печатать ещё нечего.
    """
    found: list[Inconsistency] = []
    found.extend(_sources_agree(data, limitations))
    found.extend(_verdict_is_single(data))
    found.extend(_deltas_match_levels(data))
    return found


def check_unit(data: ReportData, text: str) -> list[Inconsistency]:
    """Единица в тексте документа — единица комплекта, и никакая другая.

    **Самая тихая из найденных ошибок.** У ФосАгро приём записал ОКЕИ 385
    (миллионы), а документ печатал «663 888 тыс. руб.»: ни один контроль
    сходимости этого не видит — баланс сходится, разделы сходятся,
    коэффициенты верны, и неверны только абсолютные величины, в тысячу раз.
    Поэтому контроль блокирующий и сверяет напечатанное с комплектом, а не
    предположение с предположением.
    """
    from finlib.normalize.lines import load_lines

    if not text.strip():
        return [
            Inconsistency(
                "unit_not_checked",
                "единица измерения не сверена: текста документа нет",
            )
        ]
    own = data.unit_name
    others = sorted(set(load_lines().units.names.values()) - {own})
    wrong = [name for name in others if name in text]
    if not wrong:
        return []
    return [
        Inconsistency(
            "unit_does_not_match_the_set",
            f"в документе напечатана единица «{', '.join(wrong)}», "
            f"а комплект составлен в «{own}»",
        )
    ]


def _sources_agree(data: ReportData, limitations: str) -> list[Inconsistency]:
    """Состав отчётности в «Ограничениях» совпадает с «Происхождением документа».

    Год, названный в оговорке невключённым, не может стоять среди принятых
    комплектов, и наоборот: отбракованный комплект обязан быть назван
    в оговорках, иначе читатель считает его учтённым.
    """
    accepted = {int(item["report_year"]) for item in data.accepted_sources}
    quarantined = {int(item["report_year"]) for item in data.quarantined_sources}

    excluded_in_text: set[int] = set()
    for line in limitations.split("\n"):
        if "не включена" not in line and "не включён" not in line:
            continue
        excluded_in_text.update(int(match.group()) for match in _YEAR.finditer(line))

    found: list[Inconsistency] = []
    contradiction = excluded_in_text & accepted
    if contradiction:
        listed = ", ".join(str(year) for year in sorted(contradiction))
        found.append(
            Inconsistency(
                "source_both_excluded_and_accepted",
                f"отчётность за {listed} год названа невключённой в «Ограничениях» "
                f"и принятой в «Происхождении документа»",
            )
        )
    silent = quarantined - excluded_in_text
    if silent:
        listed = ", ".join(str(year) for year in sorted(silent))
        found.append(
            Inconsistency(
                "quarantine_not_disclosed",
                f"отчётность за {listed} год отбракована контролями, "
                f"но в «Ограничениях» об этом не сказано",
            )
        )
    return found


def _deltas_match_levels(data: ReportData) -> list[Inconsistency]:
    """Изменение равно разности отображаемых уровней.

    Прежде дельты считались по полной точности, а уровни отображались
    округлёнными: «снизилась с 0,41 до 0,31 (изменение 0,11)» при разности
    отображаемых уровней 0,10. Формально ошибки нет, документ арифметически
    несогласован, и читатель правильно ему не доверяет.

    После введения единой точки округления равенство выполняется
    по построению, и контроль сторожит именно это построение.
    """
    from finlib.metrics.derived import DerivedKind, parse

    by_code = {item.code: item for item in data.metrics}
    found: list[Inconsistency] = []
    for row in data.derived:
        parsed = parse(row["metric_code"])
        if parsed is None or parsed.kind is not DerivedKind.CHANGE_ABS:
            continue
        base = by_code.get(parsed.base)
        if base is None or row["value"] is None:
            continue
        periods = sorted(value for value, item in base.values.items() if item is not None)
        current = row["report_date"]
        earlier = [item for item in periods if item < current]
        if current not in base.values or not earlier:
            continue
        scale = data.scale_of(parsed.base)
        difference = round_to(base.values[current], scale) - round_to(
            base.values[earlier[-1]], scale
        )
        declared = round_to(row["value"], scale)
        if declared != difference:
            found.append(
                Inconsistency(
                    "delta_does_not_match_levels",
                    f"«{parsed.base}»: заявленное изменение {declared} не равно "
                    f"разности отображаемых уровней {difference}",
                )
            )
    return found


def _verdict_is_single(data: ReportData) -> list[Inconsistency]:
    """Класс либо присвоен, либо нет — третьего в документе быть не может.

    Стоп-фактор старше правила достаточности: при сработавшем стоп-факторе
    класс присваивается, а узость основания отражается отдельной фразой
    о том, что балльная оценка не формируется.
    """
    if data.assessment is None:
        return []
    has_class = bool(data.class_code)
    has_refusal = bool(data.assessment["no_class_reason"])
    if has_class == has_refusal:
        return [
            Inconsistency(
                "verdict_is_ambiguous",
                "в оценке одновременно есть и класс, и причина его неприсвоения"
                if has_class
                else "в оценке нет ни класса, ни причины его неприсвоения",
            )
        ]
    return []
