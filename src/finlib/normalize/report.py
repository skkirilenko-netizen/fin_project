"""Отчёт о загрузке комплекта отчётности."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class LoadReport:
    """Что произошло при загрузке одного комплекта."""

    inn: str
    report_year: int
    correction_version: int
    src_file_id: int | None = None
    periods: tuple[date, ...] = ()
    facts_total: int = 0
    facts_written: int = 0
    facts_unchanged: int = 0
    facts_kept_by_priority: int = 0
    overwritten: int = 0
    period_mismatches: int = 0
    unknown_codes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Игнорируемые коды — принятое решение методики, предупреждением не считаются.
    ignored_codes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Коды полного набора, отсутствующие в упрощённом: неприменимость, не пробел.
    not_applicable_codes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    ambiguous_codes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    line_conflicts: int = 0
    superseded_versions: int = 0

    @property
    def has_warnings(self) -> bool:
        """Есть ли в загрузке то, что требует внимания человека."""
        return bool(
            self.unknown_codes
            or self.ambiguous_codes
            or self.line_conflicts
            or self.period_mismatches
        )

    def summary(self) -> str:
        """Однострочная сводка для вывода в CLI."""
        parts = [
            f"ИНН {self.inn}, {self.report_year} год, корректировка {self.correction_version}",
            f"строк записано {self.facts_written} из {self.facts_total}",
        ]
        if self.facts_unchanged:
            parts.append(f"без изменений {self.facts_unchanged}")
        if self.facts_kept_by_priority:
            parts.append(f"сохранено отчётных значений {self.facts_kept_by_priority}")
        if self.overwritten:
            parts.append(f"перезаписано {self.overwritten}")
        if self.period_mismatches:
            parts.append(f"расхождений периодов {self.period_mismatches}")
        if self.unknown_codes:
            total = sum(len(codes) for codes in self.unknown_codes.values())
            parts.append(f"неизвестных кодов {total}")
        if self.ambiguous_codes:
            total = sum(len(codes) for codes in self.ambiguous_codes.values())
            parts.append(f"неоднозначных кодов {total}")
        if self.line_conflicts:
            parts.append(f"спорных строк {self.line_conflicts}")
        return "; ".join(parts)
