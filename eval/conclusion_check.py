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
from finlib.llm.verify import (
    classify_numbers,
    extract_numbers,
    strip_reasoning,
    verify,
)
from finlib.metrics.definitions import load_metrics


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

    result = verify(text, blocks, thresholds=load_metrics().stop_factor_values())

    print(f"организация: {context.inn}, период {context.report_date:%d.%m.%Y}")
    print(f"модель: {completion.model}, ответ за {completion.duration_ms} мс")
    print(f"всего чисел в ответе: {len(extract_numbers(text))}")
    for name, count in classify_numbers(text, blocks).items():
        print(f"  {count:>4}  {name}")
    print()
    passed = result.checked - len(result.foreign)
    print(f"сверено {result.checked}, прошло {passed}, отклонено {len(result.foreign)}")
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
