"""Масштабируется ли разметка: сколько работы одного эмитента достаётся другим.

Вопрос ветки, а не удобства: если каждый эмитент требует своей разметки,
объём работы растёт вместе с набором и скрининг ста эмитентов невозможен —
ровно та цель, ради которой ветка делается. Если же наименования повторяются,
работа конечна, и видно, где кончается.

    uv run python eval/ifrs_markup_scale.py

Считаются три вещи, и они отвечают на разные вопросы.

**Сжатие очереди.** Сколько в очереди строк и сколько среди них различных
наименований. Строка — это работа глазами, наименование — работа решением:
одно решение закрывает столько строк, у скольких эмитентов наименование
встретилось.

**Насыщение.** Эмитенты проходятся по очереди, и у каждого следующего
считается, какая доля его очереди уже встречалась у предыдущих. Кривая,
идущая вверх, означает конечность работы; плоская — что каждый эмитент
приносит свой словарь и конструкция не масштабируется.

**Неиспользованная работа.** Наименования, присвоенные человеком у одного
эмитента и оставшиеся неопознанными у другого. Разметка принадлежит
комплекту, на котором сделана, и переносить её по индексу строки нельзя —
но наименование переносится справочником, и эта величина показывает,
сколько уже сделанной работы лежит неподнятым в ядро.

Ничего не пишет ни в базу, ни на диск: это замер.
"""

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ifrs_regression_run import _share  # noqa: E402

from finlib.cli import _load_issuers  # noqa: E402
from finlib.db import fetch_all  # noqa: E402
from finlib.sources.ifrs_confirmed import match_key  # noqa: E402

logger = logging.getLogger(__name__)

# Наименования, которые статьёй не являются: их разметка никому не помогает,
# и в счёт общности они не идут. Перечень тот же по смыслу, что у метрики
# общности: колонтитулы и пустые подписи.
_NOT_AN_ITEM = ("", "-", "—")

_CONFIRMED = """
SELECT DISTINCT inn, source_name FROM ifrs_line_confirmation
WHERE relation <> 'not_a_line'
"""


@dataclass
class Queue:
    """Очередь разметки одного комплекта: строки и их наименования."""

    inn: str
    report_date: str
    rows: int = 0
    names: set[str] = field(default_factory=set)


@dataclass
class Scale:
    """Итог замера."""

    queues: list[Queue] = field(default_factory=list)
    confirmed: dict[str, set[str]] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        """Строк в очереди всего — работа глазами."""
        return sum(item.rows for item in self.queues)

    @property
    def names(self) -> Counter[str]:
        """Наименование → у скольких комплектов оно в очереди."""
        found: Counter[str] = Counter()
        for item in self.queues:
            found.update(item.names)
        return found

    def render(self) -> str:
        """Отчёт для человека."""
        names = self.names
        repeated = {name: count for name, count in names.items() if count > 1}
        rows_repeated = sum(count for count in repeated.values())
        lines = [
            "# Масштаб разметки МСФО",
            "",
            f"- комплектов в замере: {len(self.queues)}",
            f"- строк в очереди: {self.rows}",
            f"- различных наименований: {len(names)}",
            f"- наименований у двух и более комплектов: {len(repeated)}; "
            f"строк за ними {rows_repeated}",
            f"- сжатие очереди: {_share(self.rows - len(names), self.rows)} строк "
            "закрывается решениями по уже встреченным наименованиям",
            "",
            "## Насыщение",
            "",
            "Комплекты проходятся по очереди; у каждого следующего считается, "
            "какая доля его очереди уже встречалась у предыдущих.",
            "",
            "| № | Комплект | Строк | Уже встречалось | Доля |",
            "|---|---|---|---|---|",
        ]
        seen: set[str] = set()
        for number, item in enumerate(self.queues, start=1):
            known = len(item.names & seen)
            lines.append(
                f"| {number} | {item.inn} {item.report_date} | {len(item.names)} | "
                f"{known} | {_share(known, len(item.names))} |"
            )
            seen |= item.names

        lines += ["", "## Откуда приходит повтор", ""]
        lines += [
            "Порядок обхода влияет на кривую насыщения, поэтому здесь его нет: "
            "у каждого комплекта считается, какая доля его очереди встречается "
            "у **остальных** — отдельно у другого комплекта того же эмитента "
            "и отдельно у чужих. Это и есть ответ на вопрос, конечна ли работа.",
            "",
            "| Комплект | Наименований | Свой другой комплект | Чужие эмитенты |",
            "|---|---|---|---|",
        ]
        own_total = other_total = names_total = 0
        for item in self.queues:
            own: set[str] = set()
            other: set[str] = set()
            for neighbour in self.queues:
                if neighbour is item:
                    continue
                target = own if neighbour.inn == item.inn else other
                target |= item.names & neighbour.names
            own -= other
            own_total += len(own)
            other_total += len(other)
            names_total += len(item.names)
            lines.append(
                f"| {item.inn} {item.report_date} | {len(item.names)} | "
                f"{len(own)} ({_share(len(own), len(item.names))}) | "
                f"{len(other)} ({_share(len(other), len(item.names))}) |"
            )
        lines.append(
            f"| **всего** | {names_total} | {own_total} "
            f"({_share(own_total, names_total)}) | {other_total} "
            f"({_share(other_total, names_total)}) |"
        )

        lines += ["", "## Неиспользованная работа", ""]
        # Считается присвоение **у другого эмитента**: собственное присвоение
        # к строке того же комплекта и так вернётся восстановлением разметки,
        # и складывать их вместе значило бы выдать сделанную работу за
        # неиспользованную.
        waiting: Counter[str] = Counter()
        for item in self.queues:
            for name in item.names:
                elsewhere = {
                    inn
                    for inn, confirmed in self.confirmed.items()
                    if inn != item.inn and name in confirmed
                }
                if elsewhere:
                    waiting[name] += 1
        lines.append(
            f"- наименований, присвоенных человеком у другого эмитента и здесь "
            f"неопознанных: {len(waiting)}; строк за ними {sum(waiting.values())}"
        )
        lines.append(
            "- это работа, которая сделана и не переиспользована: разметка "
            "принадлежит комплекту, на котором сделана, а наименование "
            "переносится справочником — и только им"
        )
        return "\n".join(lines)


def measure(path: Path | None = None) -> Scale:
    """Собирает очереди всех комплектов и присвоения прежних сессий."""
    issuers, _ = _load_issuers(path or Path("data/raw/ifrs"))
    found = Scale()
    for issuer in issuers:
        queue = Queue(inn=issuer.inn, report_date=str(issuer.report_date))
        for row in issuer.extraction.unrecognised:
            # Ключ строки — боевой, тот же, которым разметка ищет прежние
            # подтверждения: свой ключ в замере расходится с ним молча,
            # и перенос разметки выходил бы измеренным иначе, чем работает.
            name = match_key(row.source_name)
            if name in _NOT_AN_ITEM:
                continue
            queue.rows += 1
            queue.names.add(name)
        found.queues.append(queue)

    try:
        for row in fetch_all(_CONFIRMED):
            found.confirmed.setdefault(row["inn"], set()).add(
                match_key(row["source_name"])
            )
    except Exception as failure:  # noqa: BLE001 — замер не должен падать из-за базы
        logger.warning("присвоения прежних сессий не прочитаны: %s", failure)
    return found


def main(argv: list[str] | None = None) -> int:
    """Точка входа замера."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    found = measure(args.path)
    if not found.queues:
        print(
            "комплектов не найдено: замер отвечает на вопрос о повторяемости "
            "наименований, и без документов ответа у него нет"
        )
        return 1
    print(found.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
