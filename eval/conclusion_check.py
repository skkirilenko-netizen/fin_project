"""Прогон модели на организации с разбором постпроверки.

Инструмент разработки, а не часть расчёта. Показывает, сколько чисел ответа
сверено и на чём именно проверка споткнулась, — по нему видно, в чём причина
отклонения: в правилах для модели или в самой проверке.

    make check-conclusion INN=7736050003
    uv run python eval/conclusion_check.py 7736050003 --save

Обращается к локальной модели, поэтому в тесты не входит и в CI не гоняется.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from finlib.llm.client import LLMClient
from finlib.llm.context import build_context
from finlib.llm.service import build_prompt
from finlib.llm.textcheck import NOT_APPLICABLE, counted
from finlib.llm.verify import (
    classify_numbers,
    extract_numbers,
    sections_of,
    strip_reasoning,
    verify,
)
from finlib.metrics.definitions import load_metrics


def _text_context(inn: str, report_date: date):
    """Контекст контроля утверждений текста — тот же, что в боевом цикле."""
    from finlib.normalize.lines import ReportingType, load_lines
    from finlib.report.data import load_report_data

    data = load_report_data(inn, report_date=report_date)
    reporting_type = ReportingType(data.organization["reporting_type"])
    return data.text_context(load_lines(), reporting_type, load_metrics())


def main(argv: list[str] | None = None) -> int:
    """Точка входа инструмента."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inn", help="ИНН организации")
    parser.add_argument("--date", help="отчётная дата в формате ГГГГ-ММ-ДД")
    parser.add_argument(
        "--save", action="store_true", help="сохранить ответ и блоки в eval/out/"
    )
    args = parser.parse_args(argv)

    report_date = date.fromisoformat(args.date) if args.date else None
    context = build_context(args.inn, report_date=report_date)
    blocks = context.blocks()

    with LLMClient() as client:
        completion = client.complete(build_prompt(context))
    text = strip_reasoning(completion.text)

    # Контекст обязателен: без него `verify` пропускает контроль утверждений
    # текста целиком, и инструмент показывал бы чистый разбор там, где
    # проверка не выполнялась. Ровно это месяцами происходило в боевом цикле.
    result = verify(
        text,
        blocks,
        thresholds=load_metrics().stop_factor_values(),
        text_context=_text_context(args.inn, context.report_date),
    )

    print(f"организация: {context.inn}, период {context.report_date:%d.%m.%Y}")
    print(f"модель: {completion.model}, ответ за {completion.duration_ms} мс")
    print(f"всего чисел в ответе: {len(extract_numbers(text))}")
    for name, count in classify_numbers(text, blocks).items():
        print(f"  {count:>4}  {name}")
    print()
    passed = result.checked - len(result.foreign)
    print(f"сверено {result.checked}, прошло {passed}, отклонено {len(result.foreign)}")
    # Счётчик проверенного рядом со счётчиком нарушений: ноль замечаний
    # по правилам текста сам по себе не говорит ничего.
    print("\nправила текста:")
    objects_by_rule = counted(
        sections_of(text), _text_context(args.inn, context.report_date)
    )
    for rule, objects in sorted(objects_by_rule.items()):
        firings = sum(1 for item in result.statements if item.rule.value == rule)
        print(f"  {objects:>4} объектов, нарушений {firings}  {rule}")
    for rule, reason in sorted(NOT_APPLICABLE.items()):
        print(f"     — не применяется  {rule.value}: {reason}")
    if result.problems:
        print("\nзамечания:")
        for item in result.problems:
            print(f"  — {item}")
        for item in result.foreign:
            print(f"      {item.context}")

    if args.save:
        out = Path(__file__).parent / "out"
        out.mkdir(exist_ok=True)
        (out / f"{args.inn}_answer.md").write_text(text, encoding="utf-8")
        (out / f"{args.inn}_blocks.txt").write_text(blocks, encoding="utf-8")
        (out / f"{args.inn}_problems.json").write_text(
            json.dumps(result.problems, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nсохранено в {out}")

    return 0 if result.verified else 1


if __name__ == "__main__":
    sys.exit(main())
