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

from finlib.pipeline import accept_ifrs_document
from finlib.quality.codes import CheckCode
from finlib.sources.ifrs_inbox import text_of
from finlib.sources.ifrs_review import ReviewOutcome

logger = logging.getLogger(__name__)

# Документ приходит либо PDF, либо готовой текстовой выгрузкой. Текстовый
# слой PDF извлекается нами; распознавание сканов не реализовано, и документ
# без слоя отклоняется приёмом с этой причиной.
SUFFIXES = (".txt", ".md", ".pdf")


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
    # Строки, принятые по ранее подтверждённому у этого же эмитента: опознание
    # слабее справочника, и в отчёте оно стоит отдельной графой.
    rows_confirmed: int = 0
    # Строки, которые методика не использует осознанно: решение, а не пробел.
    rows_ignored: int = 0
    totals_checked: int = 0
    totals_failed: int = 0
    material_items: tuple[str, ...] = ()
    notes: int = 0
    src_file_id: int | None = None
    # Комплект, которым документ оказался. Читается из содержимого, а не из
    # имени файла: две доставки одного комплекта различаются именем, но не
    # отчётной датой и не видом отчётности.
    report_date: str = ""
    reporting_kind: str = ""
    # Конвенция записи чисел: свойство вёрстки документа, и признак покрытия
    # набора. Хранится здесь, потому что определяется приёмом и больше
    # нигде не восстанавливается.
    grouping: str = ""
    # Доставка того же комплекта, отложенная в пользу другой: причина.
    set_aside: str | None = None
    # **Прочитанное помимо таблиц — от цикла, а не своим чтением.** Замеру
    # нужны заключение и тип эмитента; прежде тот, кому они нужны, читал
    # документ заново — по своему словарю величин и без подтверждённого
    # человеком опознания, — и ответ выходил другим. Здесь лежит то самое
    # чтение, которое цикл и записал в комплект.
    reading: object | None = None

    @property
    def automatic(self) -> bool:
        """Прошёл ли документ без участия человека."""
        return self.accepted and self.outcome == ReviewOutcome.AUTOMATIC.value

    @property
    def counted(self) -> bool:
        """Идёт ли документ в замер: отложенная доставка — не второй комплект."""
        return self.set_aside is None


@dataclass
class IntakeReport:
    """Итог прогона по каталогу."""

    runs: list[DocumentRun] = field(default_factory=list)

    @property
    def accepted(self) -> list[DocumentRun]:
        """Комплекты, признанные отчётностью; отложенные доставки не в счёт."""
        return [item for item in self.runs if item.accepted and item.counted]

    def render(self) -> str:
        """Отчёт для человека."""
        total = len(self.runs)
        accepted = self.accepted
        automatic = [item for item in accepted if item.automatic]
        manual = [item for item in accepted if not item.automatic]
        rejected = [item for item in self.runs if not item.accepted]
        set_aside = [item for item in self.runs if item.set_aside]

        lines = [
            "# Прогон приёма документов МСФО",
            "",
            f"- документов просмотрено: {total}",
            f"- признано отчётностью: {len(accepted)}",
            f"- отклонено на приёме: {len(rejected)}",
            f"- отложено как повторная доставка: {len(set_aside)}",
        ]
        if set_aside:
            lines += ["", "## Повторные доставки того же комплекта", ""]
            for item in set_aside:
                lines.append(f"- {item.path.name}: {item.set_aside}")
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
            confirmed = sum(item.rows_confirmed for item in accepted)
            ignored = sum(item.rows_ignored for item in accepted)
            rows = sum(item.rows_total for item in accepted)
            checked = sum(item.totals_checked for item in accepted)
            failed = sum(item.totals_failed for item in accepted)
            lines += [
                "",
                "## Опознание и контроли",
                "",
                f"- строк опознано справочником: {recognised} из {rows}"
                + (f" ({recognised / rows * 100:.1f} %)" if rows else ""),
                # Две силы опознания печатаются порознь: справочник утверждает
                # о строке вообще, ранее подтверждённое — о строке этого
                # эмитента, и доверие к ним разное.
                f"- строк принято по ранее подтверждённому: {confirmed}",
                # Игнорируемое наименование — решение методики, и стоит оно
                # рядом с неопознанными, а не среди них.
                f"- строк методика не использует осознанно: {ignored}",
                f"- строк не опознано: {rows - recognised - confirmed - ignored}",
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
    document = text_of(path)
    if not document.readable:
        # Файл не прочитан — это не скан: предлагать распознавание там, где
        # дело в шифровании или повреждении, значит назвать ложную причину.
        return DocumentRun(
            path,
            False,
            rejection=f"файл не прочитан: {document.error}",
            check_code=CheckCode.FILE_NOT_PARSED.value,
        )

    # **Прогон не повторяет шаги цикла, а зовёт цикл.** Прежде здесь стояла
    # своя последовательность — приём, извлечение, подтверждённое, сверка,
    # чтение документа, запись, — и она расходилась с боевой: подтверждённое
    # опознание доходило до решения, но не до чтения документа, и тип эмитента
    # выходил другим. Второй путь к одному ответу неминуемо расходится
    # с первым, и увидеть это можно только сравнив два прогона.
    #
    # Страницы передаются приёму внутри цикла: без них проверка потерянной
    # страницы внутри форм всегда отвечает «потерь нет». У Автодора так
    # не существовала вся сторона пассива, у Самолёта потеряны две страницы
    # внутри форм — а доля автоматического прохождения считалась так, будто
    # документы целы.
    # Эмитент называется всегда: ранее подтверждённое опознание участвует
    # в решении наравне со справочником, и без ИНН доля автопрохождения
    # выходила бы меньше настоящей. Запись при этом включает `write`.
    intake = accept_ifrs_document(
        document.text,
        inn=inn,
        raw_path=str(path),
        document=document,
        write=write,
    )
    if not intake.accepted:
        return DocumentRun(
            path, False, rejection=intake.reason, check_code=intake.check_code
        )
    profile, extraction, decision = intake.profile, intake.extraction, intake.review

    found = DocumentRun(
        path=path,
        accepted=True,
        outcome=decision.outcome.value,
        reasons=tuple(item.value for item in decision.reasons),
        rows_total=decision.rows_total,
        rows_recognised=decision.rows_recognised,
        rows_confirmed=len(decision.rows_confirmed),
        rows_ignored=decision.rows_ignored,
        totals_checked=decision.totals_checked,
        totals_failed=len(decision.totals_failed),
        material_items=tuple(item.describe() for item in decision.material_items),
        notes=len(extraction.notes),
        report_date=profile.report_dates[0].isoformat() if profile.report_dates else "",
        reporting_kind=profile.reporting_kind.value
        if hasattr(profile.reporting_kind, "value")
        else str(profile.reporting_kind),
        grouping=profile.grouping.value,
        reading=intake.reading,
    )

    # Запись делает тот же цикл, и здесь остаётся только назвать комплект:
    # прежде запись собиралась отдельно, и чтение документа при ней отличалось
    # от чтения при сверке.
    if intake.loaded is not None:
        found.src_file_id = intake.loaded.src_file_id
    return found


def run(directory: Path, write: bool = False) -> IntakeReport:
    """Проводит каждый документ эмитента; повторные доставки сводятся.

    **Папка — это организация, а не комплект.** Прежде здесь брался один
    документ на папку, и это было верно, пока в папке лежал один комплект
    в двух доставках. С появлением промежуточной отчётности рядом с годовой
    правило стало терять комплект молча: у ФосАгро разбиралась годовая,
    а полугодовая исчезала без сообщения.

    Комплект различается тем, что прочитано из документа, — отчётной датой
    и видом отчётности. Две доставки одного комплекта сводятся в одну,
    и предпочитается документ со страницами и координатами: у текстовой
    выгрузки нет ни того ни другого, и потеря страницы по ней не видна.
    Отложенная доставка называется вместе с причиной.
    """
    report = IntakeReport()
    for folder in sorted({path.parent for path in directory.rglob("*")}):
        # ИНН берётся из имени каталога: файл кладут в data/raw/ifrs/{ИНН}/.
        inn = folder.name if folder.name.isdigit() else None
        found = [
            path
            for path in sorted(folder.iterdir())
            if path.suffix.lower() in SUFFIXES
        ]
        if not found:
            continue
        runs = [run_one(path, write=False, inn=inn) for path in found]
        for item in _fold_deliveries(runs):
            if write and inn and item.accepted and item.counted:
                # Запись в базу делается только по оставленному комплекту:
                # повторная доставка дала бы второй src_file на ту же дату.
                item.src_file_id = run_one(
                    item.path, write=True, inn=inn
                ).src_file_id
            report.runs.append(item)
    return report


def _fold_deliveries(runs: list[DocumentRun]) -> list[DocumentRun]:
    """Помечает повторные доставки одного комплекта, ни одну не пряча."""
    by_report: dict[tuple[str, str], list[DocumentRun]] = {}
    for item in runs:
        if not item.accepted:
            continue
        by_report.setdefault((item.report_date, item.reporting_kind), []).append(item)

    for group in by_report.values():
        if len(group) < 2:
            continue
        kept = next(
            (item for item in group if item.path.suffix.lower() == ".pdf"), group[0]
        )
        for item in group:
            if item is kept:
                continue
            item.set_aside = (
                f"та же отчётность, что «{kept.path.name}»: отчётная дата "
                f"{item.report_date}, вид {item.reporting_kind}"
            )
            logger.info("%s отложен: %s", item.path.name, item.set_aside)
    return runs


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
