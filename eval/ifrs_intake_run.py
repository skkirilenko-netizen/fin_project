"""Прогон приёма документов МСФО: от файла до записи в базу.

Отвечает на ключевой вопрос ветки: **какая доля документов проходит
извлечение без участия человека**. От неё зависит, возможен ли скрининг:
при обязательном подтверждении по каждому эмитенту он невозможен.

Отчёт показывает, сколько документов принято автоматически, сколько
потребовало подтверждения и по какому условию, сколько позиций опознано
справочником и сколько ушло в неопознанные. Счётчик проверенного стоит
рядом со счётчиком сработавшего: доля автопрохождения при нуле выполненных
проверок означала бы не успех, а несделанную работу.

    uv run python eval/ifrs_intake_run.py --path data/raw/ifrs
    uv run python eval/ifrs_intake_run.py --path data/raw/ifrs --write

Без --write прогон ничего не пишет в базу: он отвечает на вопрос о качестве
извлечения, а не загружает отчётность.
"""

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from finlib.db import connection
from finlib.normalize.ifrs_loader import load_extraction
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import Rejection, identify
from finlib.sources.ifrs_review import ReviewOutcome, review

logger = logging.getLogger(__name__)

# Текстовые выгрузки документов: разбор PDF в текст делается снаружи
# (pdftotext), потому что распознавание сканов — отдельная задача, а разбор
# текстового слоя от способа его извлечения не зависит.
SUFFIXES = (".txt", ".md")


@dataclass
class DocumentRun:
    """Итог одного документа."""

    path: Path
    accepted: bool
    outcome: str = ""
    reasons: tuple[str, ...] = ()
    rejection: str | None = None
    check_code: str | None = None
    rows_total: int = 0
    rows_recognised: int = 0
    totals_checked: int = 0
    totals_failed: int = 0
    material_items: tuple[str, ...] = ()
    notes: int = 0
    src_file_id: int | None = None

    @property
    def automatic(self) -> bool:
        """Прошёл ли документ без участия человека."""
        return self.accepted and self.outcome == ReviewOutcome.AUTOMATIC.value


@dataclass
class IntakeReport:
    """Итог прогона по каталогу."""

    runs: list[DocumentRun] = field(default_factory=list)

    @property
    def accepted(self) -> list[DocumentRun]:
        """Документы, признанные отчётностью."""
        return [item for item in self.runs if item.accepted]

    def render(self) -> str:
        """Отчёт для человека."""
        total = len(self.runs)
        accepted = self.accepted
        automatic = [item for item in accepted if item.automatic]
        manual = [item for item in accepted if not item.automatic]
        rejected = [item for item in self.runs if not item.accepted]

        lines = [
            "# Прогон приёма документов МСФО",
            "",
            f"- документов просмотрено: {total}",
            f"- признано отчётностью: {len(accepted)}",
            f"- отклонено на приёме: {len(rejected)}",
        ]
        if accepted:
            share = len(automatic) / len(accepted) * 100
            lines.append(
                f"- **прошло автоматически: {len(automatic)} из {len(accepted)} "
                f"({share:.1f} %)**".replace(".", ",")
            )
            lines.append(f"- потребовало подтверждения: {len(manual)}")

        if rejected:
            lines += ["", "## Отклонены на приёме", ""]
            for item in rejected:
                lines.append(f"- {item.path.name}: {item.check_code} — {item.rejection}")

        if manual:
            lines += ["", "## Потребовали подтверждения", ""]
            grouped: Counter[str] = Counter()
            for item in manual:
                grouped.update(item.reasons)
                listed = ", ".join(item.reasons)
                lines.append(f"- {item.path.name}: {listed}")
            lines += ["", "### По условиям", ""]
            for reason, count in grouped.most_common():
                lines.append(f"- {reason}: {count}")

        if accepted:
            recognised = sum(item.rows_recognised for item in accepted)
            rows = sum(item.rows_total for item in accepted)
            checked = sum(item.totals_checked for item in accepted)
            failed = sum(item.totals_failed for item in accepted)
            lines += [
                "",
                "## Опознание и контроли",
                "",
                f"- строк опознано справочником: {recognised} из {rows}"
                + (f" ({recognised / rows * 100:.1f} %)" if rows else ""),
                f"- строк не опознано: {rows - recognised}",
                f"- итогов сверено: {checked}, из них не сошлось: {failed}",
                f"- сносок под формами извлечено: "
                f"{sum(item.notes for item in accepted)}",
            ]
            material = [name for item in accepted for name in item.material_items]
            if material:
                lines += ["", "### Статьи сверх порога существенности", ""]
                lines += [f"- {name}" for name in material]

        if not total:
            lines += [
                "",
                "Документов не найдено. Прогон отвечает на вопрос о доле "
                "автоматического прохождения, и без файлов отчётности ответа "
                "у него нет: печатать ноль здесь значило бы выдать отсутствие "
                "данных за результат измерения.",
            ]
        return "\n".join(lines)


def run_one(path: Path, write: bool = False, inn: str | None = None) -> DocumentRun:
    """Проводит один документ через приём, извлечение и сверку."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    profile = identify(text)
    if isinstance(profile, Rejection):
        return DocumentRun(
            path, False, rejection=profile.reason, check_code=profile.code.value
        )

    extraction = extract(text, profile.report_dates, profile.grouping)
    decision = review(extraction, profile)

    found = DocumentRun(
        path=path,
        accepted=True,
        outcome=decision.outcome.value,
        reasons=tuple(item.value for item in decision.reasons),
        rows_total=decision.rows_total,
        rows_recognised=decision.rows_recognised,
        totals_checked=decision.totals_checked,
        totals_failed=len(decision.totals_failed),
        material_items=tuple(item.describe() for item in decision.material_items),
        notes=len(extraction.notes),
    )

    if write and inn:
        with connection() as conn:
            loaded = load_extraction(inn, extraction, profile, decision, conn)
            found.src_file_id = loaded.src_file_id
    return found


def run(directory: Path, write: bool = False) -> IntakeReport:
    """Проводит все документы каталога."""
    report = IntakeReport()
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() not in SUFFIXES:
            continue
        # ИНН берётся из имени каталога: файл кладут в data/raw/ifrs/{ИНН}/.
        inn = path.parent.name if path.parent.name.isdigit() else None
        report.runs.append(run_one(path, write=write, inn=inn))
    return report


def main(argv: list[str] | None = None) -> int:
    """Точка входа прогона."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="data/raw/ifrs", help="каталог с документами")
    parser.add_argument(
        "--write", action="store_true", help="записать принятые комплекты в базу"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    directory = Path(args.path)
    if not directory.exists():
        print(
            f"Каталог {directory} не найден. Положите текстовые выгрузки "
            "отчётности в data/raw/ifrs/{ИНН}/ — прогон разбирает текстовый "
            "слой, извлечённый заранее (pdftotext), потому что распознавание "
            "сканов не реализовано."
        )
        return 1
    print(run(directory, write=args.write).render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
