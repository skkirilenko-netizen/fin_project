"""Замер по аудиторским заключениям: тип задания, вид мнения, разделы.

Отвечает на три вопроса задачи 25: у скольких комплектов заключение
прочитано, у скольких вид мнения определён и сколько разделов-признаков
нашлось. Три состояния определённости считаются порознь — «оговорок нет»,
«заключение нечитаемо» и «заключения нет» суть разные сведения, и сводить
их в одно число значило бы потерять ровно то, ради чего задача делается.

    uv run python eval/ifrs_audit_run.py

Ничего не пишет ни в базу, ни на диск: это замер, а не загрузка.
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from finlib.normalize.ifrs_audit import load_audit_policy
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_audit import AuditReport, Determination, read_audit_report
from finlib.sources.ifrs_inbox import form_headings, text_of
from finlib.sources.ifrs_numbers import load_parsing_policy

logger = logging.getLogger(__name__)


def measure(path: Path) -> AuditReport | str:
    """Читает заключение одного документа; строка — замер не состоялся."""
    document = text_of(path)
    if not document.readable:
        return f"файл не прочитан: {document.error}"
    # Заключение стоит до первой формы — это его место в отчётности,
    # и оно же граница поиска.
    headings = form_headings(document.text, load_ifrs_lines(), load_parsing_policy())
    return read_audit_report(
        document.text, document, before=min(headings.values(), default=0)
    )


def main(argv: list[str] | None = None) -> int:
    """Печатает замер по заключениям; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    policy = load_audit_policy()
    print(
        f"справочник заключения {policy.version}: видов мнения "
        f"{len(policy.opinions)}, разделов-признаков {len(policy.sections)}, "
        f"сигналов {len(policy.signals)}"
    )

    found: list[tuple[str, AuditReport]] = []
    for folder in sorted(item for item in args.path.iterdir() if item.is_dir()):
        documents = [
            item for item in sorted(folder.iterdir()) if item.suffix.lower() == ".pdf"
        ]
        if not documents:
            continue
        outcome = measure(documents[0])
        if isinstance(outcome, str):
            print(f"\n{folder.name}: замер не состоялся — {outcome}")
            continue
        found.append((folder.name, outcome))

    if not found:
        print(
            "\nдокументов не найдено: доли не измерены. Печатать ноль здесь "
            "значило бы выдать отсутствие данных за результат."
        )
        return 1

    print("\nЗАКЛЮЧЕНИЕ ПО КАЖДОМУ КОМПЛЕКТУ")
    for inn, report in found:
        pages = (
            f", страницы {report.pages[0]}–{report.pages[1]}" if report.pages else ""
        )
        print(f"  {inn}{pages}: {report.describe()}")
        for line in report.limitations(policy):
            print(f"      оговорка: {line.split('.')[0]}.")
        for code in report.signals:
            signal = next(item for item in policy.signals if item.code == code)
            print(f"      сигнал [{signal.level}] {signal.name}")

    states = Counter(item.determination for _, item in found)
    print("\nТРИ СОСТОЯНИЯ ОПРЕДЕЛЁННОСТИ")
    for state in Determination:
        print(f"  {state.value}: {states.get(state, 0)} из {len(found)}")

    determined = [item for _, item in found if item.determination is Determination.DETERMINED]
    kinds = Counter(item.opinion for item in determined)
    print("\nВИД МНЕНИЯ У ОПРЕДЕЛЁННЫХ")
    for kind in policy.opinions:
        print(f"  {kind.name}: {kinds.get(kind.code, 0)} из {len(determined)}")
    engagements = Counter(
        item.engagement.value for item in determined if item.engagement is not None
    )
    print(
        "  тип задания: "
        + ", ".join(f"{name} {count}" for name, count in engagements.items())
    )

    print("\nРАЗДЕЛЫ-ПРИЗНАКИ")
    sections = Counter(code for _, item in found for code in item.sections)
    for section in policy.sections:
        print(f"  {section.name}: {sections.get(section.code, 0)} из {len(found)}")
    signals = Counter(code for _, item in found for code in item.signals)
    print(
        "  сигналов сработало: "
        + (
            ", ".join(f"{code} {count}" for code, count in signals.items())
            if signals
            else "нет"
        )
        + f" (проверено комплектов {len(found)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
