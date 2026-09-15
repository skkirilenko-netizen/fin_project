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
    """Проверяет согласованность разделов документа между собой."""
    found: list[Inconsistency] = []
    found.extend(_sources_agree(data, limitations))
    found.extend(_verdict_is_single(data))
    return found


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
