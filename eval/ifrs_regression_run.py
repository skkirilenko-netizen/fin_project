"""Регрессионный прогон набора МСФО (задача 29).

Отвечает на ключевой вопрос ветки: **какая доля извлечений проходит без
участия человека**. От неё зависит, возможен ли скрининг ста эмитентов,
ради которого ветка и делается.

    uv run python eval/ifrs_regression_run.py
    uv run python eval/ifrs_regression_run.py --write   # с записью в базу

Устроен как прогон РСБУ и по тем же правилам:

- **признаки меряются по факту, а не объявляются в составе**: заявленная
  категория — гипотеза, и отчёт показывает заявленное покрытие против
  фактического;
- **ожидаемый отказ — тоже успех**: эмитент вне периметра подтверждает
  правило, и в число остановок он не попадает;
- **у каждой доли назван знаменатель**: доля без него неотличима
  от отсутствия измерения;
- **эмитент без документа не даёт нуля, он даёт пропуск**: ноль
  автоматических прохождений у того, чью отчётность не выгружали, означал бы,
  что извлечение не прошло, тогда как его не было.

Контуров два, как в РСБУ. Быстрый — приём, извлечение, сверка: минуты
на набор, поэтому запускается при каждой правке справочника статей. Полный
добавляет расчёт, оценку и текстовую часть; его метрики здесь не печатаются
вовсе, а не печатаются нулями.
"""

import argparse
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ifrs_intake_run import DocumentRun, _fold_deliveries, run_one  # noqa: E402
from ifrs_set import (  # noqa: E402
    DataSource,
    Entry,
    IfrsSet,
    Outcome,
    load_set,
)

from finlib.config import settings  # noqa: E402
from finlib.normalize.ifrs_lines import load_ifrs_lines  # noqa: E402
from finlib.sources import cbonds  # noqa: E402
from finlib.sources.ifrs_audit import (  # noqa: E402
    Determination,
    Engagement,
    read_audit_report,
)
from finlib.sources.ifrs_inbox import form_headings, text_of  # noqa: E402
from finlib.sources.ifrs_issuer_type import (  # noqa: E402
    Determination as TypeDetermination,
)
from finlib.sources.ifrs_issuer_type import determine_type
from finlib.sources.ifrs_numbers import load_parsing_policy  # noqa: E402
from finlib.version import code_version  # noqa: E402

logger = logging.getLogger(__name__)

SUFFIXES = (".pdf", ".txt", ".md")

# Исход эмитента сверх объявленных в наборе: документа нет вовсе. Не отказ
# и не успех — отсутствие измерения, и в знаменатели оно не входит.
NO_DOCUMENT = "no_document"

# Тип эмитента из `sources/ifrs_issuer_type` в признак покрытия. Соответствие
# объявлено, а не выведено совпадением строк: перечисления живут своей жизнью
# и однажды разойдутся.
ISSUER_TYPE_FEATURES: dict[str, str] = {
    "corporate": "corporate_issuer",
    "quasi_sovereign": "quasi_sovereign_issuer",
    "developer": "developer_issuer",
    "financial": "financial_institution",
}

REPORTING_KIND_FEATURES: dict[str, str] = {
    "full": "full_reporting",
    "interim": "interim_reporting",
    "disclosable": "disclosable_reporting",
    "special_purpose": "special_purpose_reporting",
}

# Код отказа приёма в признак покрытия: отказ — тоже наблюдение.
REJECTION_FEATURES: dict[str, str] = {
    "file_currency_not_rouble": "foreign_currency",
    "financial_institution": "financial_institution",
    "file_text_layer_missing": "no_text_layer",
}

# Признаки, которые прогон умеет померить по документу, и признаки, которые
# он берёт у Cbonds. Перечислены явно и проверяются тестом против правил
# набора: **признак, объявленный в правилах и не измеряемый прогоном, ноль
# в отчёте получит навсегда** — и ноль этот будет неотличим от отсутствия
# наблюдений. Это тот же дефект, что контроль, который никто не вызывает.
CBONDS_FEATURES: frozenset[str] = frozenset(
    {"foreign_currency", "negative_equity", "loss"}
)
DOCUMENT_FEATURES: frozenset[str] = frozenset(
    set(ISSUER_TYPE_FEATURES.values())
    | set(REPORTING_KIND_FEATURES.values())
    | set(REJECTION_FEATURES.values())
    | {
        "automatic_intake",
        "material_specific_item",
        "lost_page",
        "english_grouping",
        "unmodified_opinion",
        "modified_opinion",
        "going_concern",
        "review_engagement",
        "audit_not_readable",
    }
)


@dataclass
class IssuerRun:
    """Итог одного эмитента набора."""

    entry: Entry
    documents: tuple[DocumentRun, ...] = ()
    features: set[str] = field(default_factory=set)
    outcome: str = NO_DOCUMENT
    seconds: float = 0.0

    @property
    def has_document(self) -> bool:
        """Был ли у эмитента документ, который можно было разобрать."""
        return bool(self.documents)

    @property
    def accepted(self) -> tuple[DocumentRun, ...]:
        """Комплекты, признанные отчётностью."""
        return tuple(item for item in self.documents if item.accepted and item.counted)

    @property
    def automatic(self) -> tuple[DocumentRun, ...]:
        """Комплекты, прошедшие без участия человека."""
        return tuple(item for item in self.accepted if item.automatic)

    @property
    def matched(self) -> bool:
        """Совпал ли исход с ожидаемым.

        Ожидаемый отказ засчитывается успехом: эмитент вне периметра методики
        подтверждает правило, а не нарушает его. Эмитент без документа
        не совпадает и не расходится — измерения не было.
        """
        if self.outcome == NO_DOCUMENT:
            return False
        if self.entry.expected_outcome is Outcome.REFUSAL:
            return self.outcome == Outcome.REFUSAL.value
        return self.outcome in (Outcome.ANALYSIS.value, Outcome.MANUAL_REVIEW.value)


@dataclass
class Report:
    """Итог прогона по набору."""

    found: IfrsSet
    runs: list[IssuerRun] = field(default_factory=list)
    started: datetime = field(default_factory=datetime.now)

    @property
    def measured(self) -> list[IssuerRun]:
        """Эмитенты, у которых документ был: только они входят в знаменатели."""
        return [item for item in self.runs if item.has_document]

    def render(self) -> str:
        """Отчёт для человека."""
        lines = [
            "# Регрессионный прогон набора МСФО",
            "",
            f"- прогон: {self.started:%d.%m.%Y %H:%M}",
            "- контур: быстрый (приём, извлечение, сверка)",
            f"- версия кода: {code_version()}",
            f"- справочник статей МСФО: {load_ifrs_lines().version}",
            f"- правила разбора: {load_parsing_policy().version}",
            f"- правила набора: {self.found.rules.version}",
            f"- {self.found.describe()}",
            "",
            "## Эмитенты",
            "",
            "| ИНН | Эмитент | Комплектов | Исход | Ожидалось | Совпало | Секунд |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in self.runs:
            mark = "—" if item.outcome == NO_DOCUMENT else ("да" if item.matched else "НЕТ")
            lines.append(
                f"| {item.entry.inn} | {item.entry.name} | {len(item.accepted)} | "
                f"{item.outcome} | {item.entry.expected_outcome.value} | {mark} | "
                f"{item.seconds:.1f} |".replace(".", ",")
            )
        lines += ["", "## Метрики", ""] + self._metrics()
        lines += ["", "## Покрытие признаков", ""] + self._coverage()
        lines += ["", "## Не измерено", ""] + self._unmeasured()
        return "\n".join(lines)

    def _metrics(self) -> list[str]:
        """Метрики со знаменателями; без знаменателя доля не печатается."""
        measured = self.measured
        accepted = [item for run in measured for item in run.accepted]
        automatic = [item for item in accepted if item.automatic]
        rows = sum(item.rows_total for item in accepted)
        recognised = sum(item.rows_recognised for item in accepted)
        checked = sum(item.totals_checked for item in accepted)
        failed = sum(item.totals_failed for item in accepted)
        seconds = [item.seconds for item in measured]
        if not accepted:
            return [
                "- измерять нечего: ни один документ не принят приёмом. "
                "Это не нулевая доля, а отсутствие измерения"
            ]
        return [
            f"- **прошло автоматически: {len(automatic)} из {len(accepted)} "
            f"комплектов ({_share(len(automatic), len(accepted))})**",
            f"- строк опознано справочником: {recognised} из {rows} "
            f"({_share(recognised, rows)})",
            f"- итогов сошлось: {checked - failed} из {checked} сверенных "
            f"({_share(checked - failed, checked)})",
            f"- эмитентов измерено: {len(measured)} из {len(self.runs)} в прогоне",
            f"- итог совпал с ожиданием: {sum(1 for item in measured if item.matched)} "
            f"из {len(measured)} измеренных",
            f"- секунд на эмитента: в среднем {sum(seconds) / len(seconds):.1f}, "
            f"наибольшее {max(seconds):.1f}".replace(".", ","),
        ]

    @property
    def without_document(self) -> tuple[str, ...]:
        """Признаки, которые прогон меряет без выгрузки документа."""
        return self.found.features_by_source(DataSource.CBONDS)

    def _coverage(self) -> list[str]:
        """Заявленное покрытие против фактического."""
        seen: Counter[str] = Counter()
        for item in self.runs:
            seen.update(item.features)
        lines = ["| Признак | Откуда | Эмитентов |", "|---|---|---|"]
        for code, feature in sorted(self.found.rules.features.items()):
            lines.append(f"| {feature.name} | {feature.source.value} | {seen[code]} |")
        empty = [
            self.found.rules.features[code].name
            for code in self.found.rules.features
            if not seen[code]
        ]
        if empty:
            lines += [
                "",
                "Признаки без единого наблюдения — это не покрытие нулём, "
                "а отсутствие наблюдений: " + ", ".join(empty),
            ]
        return lines

    def _unmeasured(self) -> list[str]:
        """Что не мерилось и почему: молчание читалось бы как измерение."""
        missing = [item for item in self.runs if not item.has_document]
        lines = []
        if missing:
            lines.append(
                f"- эмитентов без документа: {len(missing)} — в знаменатели "
                "не входят, извлечение по ним не выполнялось:"
            )
            lines += [f"  - {item.entry.inn} {item.entry.name}" for item in missing]
        lines.append(
            "- признаков, измеримых без выгрузки документа: "
            f"{len(self.without_document)} из {len(self.found.rules.features)} — "
            "остальные видны только в документе"
        )
        lines.append(
            "- метрики полного контура (класс, распределение классов, текстовая "
            "часть) в быстром не печатаются вовсе: напечатать их нулями значило "
            "бы сказать, что оценка не удалась, тогда как её не спрашивали"
        )
        for item in self.found.rules.uncovered:
            lines.append(f"- {item.name}: {' '.join(item.reason.split())}")
        return lines


def _share(part: int, whole: int) -> str:
    """Доля с процентом; ноль знаменателя — не ноль процентов."""
    if not whole:
        return "знаменатель пуст"
    return f"{part / whole * 100:.1f} %".replace(".", ",")


def _cbonds_features(inn: str, universe: dict[str, list[dict]]) -> set[str]:
    """Признаки, которые видны без выгрузки документа.

    Их три, и перечень объявлен в правилах набора: валюта, отрицательный
    капитал и убыток. Всё прочее — вид мнения, примечания, конвенция
    разрядов — у нормализованных данных отсутствует по устройству.
    """
    rows = universe.get(inn)
    if not rows:
        return set()
    last = sorted(rows, key=lambda item: item["date"])[-1]
    found: set[str] = set()
    if str(last.get("ln104") or "") not in ("RUB", ""):
        found.add("foreign_currency")
    equity = last.get("ln20")
    profit = last.get("ln26")
    if equity not in (None, "") and Decimal(str(equity)) < 0:
        found.add("negative_equity")
    if profit not in (None, "") and Decimal(str(profit)) < 0:
        found.add("loss")
    return found


def _document_features(path: Path, run: DocumentRun) -> set[str]:
    """Признаки, которые видны только из документа."""
    found: set[str] = set()
    if not run.accepted:
        code = run.check_code or ""
        if code in REJECTION_FEATURES:
            found.add(REJECTION_FEATURES[code])
        return found

    found.add(REPORTING_KIND_FEATURES.get(run.reporting_kind, run.reporting_kind))
    if run.automatic:
        found.add("automatic_intake")
    if run.grouping == "english":
        found.add("english_grouping")
    if run.material_items:
        found.add("material_specific_item")
    if "lost_page" in run.reasons:
        found.add("lost_page")

    document = text_of(path)
    if not document.readable:
        return found
    catalog = load_ifrs_lines()
    headings = form_headings(document.text, catalog, load_parsing_policy())
    audit = read_audit_report(
        document.text, document, before=min(headings.values(), default=0)
    )
    if audit.determination is Determination.NOT_READABLE:
        found.add("audit_not_readable")
    elif audit.determination is Determination.DETERMINED:
        if audit.engagement is Engagement.REVIEW:
            found.add("review_engagement")
        # Модифицированность объявлена самим заключением: у мнения есть код,
        # и признак «модифицировано» стоит рядом с ним, а не выводится
        # сравнением кода со списком.
        if audit.modified is True:
            found.add("modified_opinion")
        elif audit.modified is False:
            found.add("unmodified_opinion")
        # Непрерывность деятельности объявляется **разделом** заключения,
        # а не видом мнения и не сигналом: у Сегежи мнение немодифицированное,
        # а раздел стоит. Искать её среди сигналов значило бы не найти никогда.
        if "going_concern_uncertainty" in audit.sections:
            found.add("going_concern")
    return found


def _issuer_type_feature(path: Path) -> str | None:
    """Тип эмитента по статьям отчётности; None — структурного признака нет.

    **Умолчание типом не считается.** `determine_type` при отсутствии
    признаков возвращает обычного корпоративного эмитента — это не вывод,
    а отсутствие вывода, и записывать его в покрытие значило бы объявить
    измеренным то, чего не измеряли. Тип девелопера опознаётся статьями,
    которые справочник без разметки человеком не знает, и до разметки
    тип остаётся неопределённым.
    """
    document = text_of(path)
    if not document.readable:
        return None
    from finlib.sources.ifrs_extract import extract
    from finlib.sources.ifrs_inbox import Rejection, identify

    profile = identify(document.text, document=document, any_currency=True)
    if isinstance(profile, Rejection):
        return None
    found = extract(
        document.text,
        profile.dates_by_form,
        profile.grouping,
        layouts=profile.columns_by_form,
    )
    values = {
        item.code: item.value
        for item in found.values
        if item.report_date == profile.report_dates[0]
    }
    verdict = determine_type(values, document.text)
    if verdict.determination is TypeDetermination.DEFAULT:
        return None
    return ISSUER_TYPE_FEATURES.get(verdict.code)


def run_issuer(
    entry: Entry, root: Path, universe: dict[str, list[dict]], write: bool = False
) -> IssuerRun:
    """Проводит одного эмитента: документы, признаки, исход."""
    started = time.monotonic()
    found = IssuerRun(entry=entry, features=_cbonds_features(entry.inn, universe))
    documents = sorted(
        item for item in (root / entry.inn).glob("*") if item.suffix.lower() in SUFFIXES
    )
    if not documents:
        logger.info("%s: документа нет, извлечение не выполнялось", entry.inn)
        found.seconds = time.monotonic() - started
        return found

    runs = [run_one(path, write=False, inn=entry.inn) for path in documents]
    found.documents = tuple(_fold_deliveries(runs))
    for item in found.documents:
        if not item.counted:
            continue
        found.features |= _document_features(item.path, item)
        if item.accepted:
            kind = _issuer_type_feature(item.path)
            if kind is not None:
                found.features.add(kind)

    if not found.accepted:
        found.outcome = Outcome.REFUSAL.value
    elif found.automatic:
        found.outcome = Outcome.ANALYSIS.value
    else:
        found.outcome = Outcome.MANUAL_REVIEW.value

    if write:
        for item in found.accepted:
            run_one(item.path, write=True, inn=entry.inn)

    found.seconds = time.monotonic() - started
    return found


def run(root: Path | None = None, write: bool = False) -> Report:
    """Проводит набор целиком."""
    found = load_set()
    root = root or settings.base_dir / "data" / "raw" / "ifrs"
    universe: dict[str, list[dict]] = {}
    for row in cbonds.msfo_universe():
        inn = row.get("emitent_inn")
        if inn:
            universe.setdefault(inn, []).append(row)
    if not universe:
        logger.warning(
            "перечень Cbonds пуст: признаки без выгрузки не измерялись — "
            "это не нулевое покрытие, а отсутствие измерения"
        )

    report = Report(found=found)
    for entry in found.in_run:
        report.runs.append(run_issuer(entry, root, universe, write))
    return report


def main(argv: list[str] | None = None) -> int:
    """Точка входа прогона."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=None, help="каталог с документами")
    parser.add_argument(
        "--write", action="store_true", help="записать принятые комплекты в базу"
    )
    parser.add_argument(
        "--out", default=None, help="куда положить отчёт; по умолчанию печатается"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    report = run(Path(args.path) if args.path else None, write=args.write)
    text = report.render()
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"Отчёт: {path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
