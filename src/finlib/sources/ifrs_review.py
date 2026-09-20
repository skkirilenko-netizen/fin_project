"""Экран сверки: принять извлечение автоматически или отдать человеку.

Подтверждённая человеком таблица играет роль справочника кодов: числа
приходят из проверенного источника, и каждое утверждение заключения
ссылается на позицию модели. Так сохраняется инвариант 3 при отсутствии
регламентированных кодов строк.

**Режим автоматического прохождения — не послабление, а условие
осуществимости.** При обязательном ручном подтверждении скрининг ста
эмитентов невозможен, а маршрутизация — цель всей ветки. Поэтому извлечение,
у которого сошлись все контроли, опознаны все позиции, нет специфических
статей сверх порога и вид отчётности полный, принимается само — с пометкой
в журнале.

Условия не смягчаются ни по одному: любое срабатывание контроля,
неопознанная позиция, статья сверх порога существенности или неполный вид
отчётности означают ручное подтверждение. Каждое из четырёх — случай,
когда машина не знает, что перед ней.

**Опознание бывает двух сил, и слабейшей хватает для повторного комплекта
того же эмитента.** Справочник утверждает: строка с таким наименованием
означает это у любого эмитента. Ранее подтверждённое утверждает меньше:
у этого эмитента эта строка означает это. Второе — не послабление условия
«машина знает, что перед ней», а другой источник того же знания: человек
уже смотрел ту же строку в той же форме той же организации. Границы объявлены
в `ifrs_confirmed`: тот же эмитент, то же наименование, та же форма и раздел;
на чужого эмитента не переносится, индекс строки в ключ не входит.

Остальные основания это не затрагивает: неполный вид отчётности и страница
без текстового слоя остаются блокирующими, потому что подтверждением строки
они не снимаются — там не опознание, а состав раскрытий и потеря содержимого.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.quality.codes import CheckCode
from finlib.quality.totals import (
    Composition,
    TotalCheck,
    TotalVerdict,
    check_total,
)
from finlib.sources.ifrs_confirmed import Confirmed
from finlib.sources.ifrs_extract import Extraction, UnrecognisedRow, share_of_assets
from finlib.sources.ifrs_inbox import DocumentProfile, ReportingKind
from finlib.sources.ifrs_numbers import PlausibilityCheck, check_plausibility

logger = logging.getLogger(__name__)


class ReviewOutcome(StrEnum):
    """Итог экрана сверки."""

    AUTOMATIC = "automatic"
    MANUAL_REQUIRED = "manual_required"


class ReviewReason(StrEnum):
    """Почему извлечение требует ручного подтверждения."""

    CHECK_FAILED = "check_failed"
    UNRECOGNISED_POSITION = "unrecognised_position"
    MATERIAL_SPECIFIC_ITEM = "material_specific_item"
    REPORTING_KIND = "reporting_kind"
    IMPLAUSIBLE_GROUPING = "implausible_grouping"
    # Страница внутри форм без текстового слоя: содержимого её мы не видим,
    # и что именно потеряно, машина сказать не может.
    LOST_PAGE = "lost_page"
    # Величины, отброшенные без объяснения: граф в строке больше, чем
    # отчётных дат, а шапка о длительности граф промолчала. Отброшенная
    # графа может быть как раз той, которая нужна.
    DROPPED_COLUMN = "dropped_column"


# Основание ручного подтверждения и код контроля, которым оно уходит
# в журнал качества. Отображение объявлено здесь, а не подразумевается:
# основание без кода контроля в `dq_log` не попадёт, и причина отбраковки
# останется в памяти того, кто смотрел экран.
REASON_CODES: dict[ReviewReason, CheckCode] = {
    ReviewReason.CHECK_FAILED: CheckCode.SECTION_SUM,
    ReviewReason.IMPLAUSIBLE_GROUPING: CheckCode.DIGIT_GROUPING_IMPLAUSIBLE,
    ReviewReason.UNRECOGNISED_POSITION: CheckCode.LINE_NOT_RECOGNIZED,
    ReviewReason.MATERIAL_SPECIFIC_ITEM: CheckCode.LINE_NOT_RECOGNIZED,
    ReviewReason.REPORTING_KIND: CheckCode.FILE_REPORTING_TYPE_UNKNOWN,
    ReviewReason.LOST_PAGE: CheckCode.FILE_TEXT_LAYER_MISSING,
    ReviewReason.DROPPED_COLUMN: CheckCode.EXTRA_COLUMNS_DROPPED,
}


@dataclass(frozen=True, slots=True)
class MaterialItem:
    """Неопознанная статья сверх порога существенности.

    Такая статья не сворачивается в «прочее»: она выносится отдельной
    позицией с кодом, который присваивает человек, а наименование
    сохраняется дословно.
    """

    row: UnrecognisedRow
    share_of_assets: Decimal

    def describe(self) -> str:
        """Человеческое описание для экрана сверки."""
        return (
            f"«{self.row.source_name}» — {self.row.largest} "
            f"({self.share_of_assets:.1%} валюты баланса)"
        )


@dataclass
class ReviewResult:
    """Решение экрана сверки со всеми основаниями.

    Счётчики стоят рядом с нарушениями: сколько итогов сверено, сколько
    строк опознано. Ноль расхождений при неизвестном числе проверок
    не означает ничего.
    """

    outcome: ReviewOutcome
    reasons: tuple[ReviewReason, ...] = ()
    totals_checked: int = 0
    totals_failed: tuple[TotalCheck, ...] = ()
    rows_total: int = 0
    rows_recognised: int = 0
    material_items: tuple[MaterialItem, ...] = ()
    plausibility: PlausibilityCheck | None = None
    problems: tuple[str, ...] = ()
    # Строки, опознанные не справочником, а ранее подтверждённым у этого же
    # эмитента. Считаются отдельно: две силы опознания — разные сведения,
    # и в документе они печатаются порознь.
    rows_confirmed: tuple[tuple[str, int], ...] = ()
    confirmed_from: tuple[str, ...] = ()
    # Строки, которые методика не использует осознанно (прибыль на акцию,
    # число акций). Считаются отдельно: решение и недоработка — разные вещи,
    # и ноль игнорируемых строк не то же, что «их никто не искал».
    rows_ignored: int = 0
    # Строки с отброшенными без объяснения величинами и знаменатель к ним —
    # строки с величинами вообще. Ноль потерь при неизвестном числе строк
    # неотличим от невыполненной проверки.
    rows_with_values: int = 0
    rows_with_dropped: int = 0

    @property
    def automatic(self) -> bool:
        """Прошло ли извлечение без участия человека."""
        return self.outcome is ReviewOutcome.AUTOMATIC

    @property
    def check_codes(self) -> tuple[CheckCode, ...]:
        """Коды контролей, которыми основания уходят в журнал качества."""
        return tuple(dict.fromkeys(REASON_CODES[item] for item in self.reasons))

    def describe(self) -> str:
        """Однострочная сводка для журнала."""
        head = (
            "принято автоматически"
            if self.automatic
            else "требуется подтверждение человеком: "
            + ", ".join(item.value for item in self.reasons)
        )
        return (
            f"{head}; итогов сверено {self.totals_checked}, из них не сошлось "
            f"{len(self.totals_failed)}; строк опознано {self.rows_recognised} "
            f"из {self.rows_total}, осознанно игнорируется {self.rows_ignored}; "
            f"статей сверх порога "
            f"{len(self.material_items)}; величины отброшены у "
            f"{self.rows_with_dropped} строк из {self.rows_with_values} "
            "с величинами"
        )


def review(
    extraction: Extraction,
    profile: DocumentProfile,
    catalog: IfrsCatalog | None = None,
    confirmed: Confirmed | None = None,
) -> ReviewResult:
    """Решает, принять извлечение автоматически или отдать человеку.

    Проверки идут все и всегда: их итог нужен человеку на экране сверки
    даже тогда, когда первое же основание уже потребовало подтверждения.
    Останавливаться на первом значило бы показывать половину картины.

    `confirmed` — ранее подтверждённое опознание у **этого же** эмитента
    (`ifrs_confirmed.load_confirmed`). Строка, о которой человек уже сказал,
    чем она является, опознанной считается, и её величина участвует в итогах
    наравне с опознанными справочником: иначе сошедшиеся у ЛСР четырнадцать
    итогов экран видел бы как провал контроля, а нулевая очередь разметки
    не давала бы автопрохождения никогда.
    """
    catalog = catalog or load_ifrs_lines()
    report_date = profile.report_dates[0]
    known = confirmed or Confirmed()

    # Строки, опознанные ранее подтверждённым, из неопознанных выбывают:
    # о них известно, чем они являются.
    unrecognised = [
        item for item in extraction.unrecognised if item.key not in known.rows
    ]
    totals_checked, failed = _check_totals(extraction, catalog, report_date, known)
    plausibility = check_plausibility(
        _values_with(extraction, report_date, known),
        extraction.value_of("ifrs.revenue", report_date),
    )
    material = _material_items(unrecognised, extraction, report_date, catalog)

    # Считаются строки таблиц, а не величины: у строки столько величин,
    # сколько периодов, и графа обязана считать то, как называется.
    rows_total = extraction.rows_total
    reasons: list[ReviewReason] = []
    problems: list[str] = []

    if failed:
        reasons.append(ReviewReason.CHECK_FAILED)
        problems.extend(
            f"итог {item.code}: {item.total} против суммы состава {item.computed}"
            for item in failed
        )
    if not plausibility.plausible:
        reasons.append(ReviewReason.IMPLAUSIBLE_GROUPING)
        problems.extend(plausibility.problems)
    if unrecognised:
        reasons.append(ReviewReason.UNRECOGNISED_POSITION)
        problems.extend(
            f"строка не опознана справочником: «{item.source_name}»"
            for item in unrecognised
        )
    if material:
        reasons.append(ReviewReason.MATERIAL_SPECIFIC_ITEM)
        problems.extend(item.describe() for item in material)
    if extraction.dropped_values:
        # Граф в строке больше, чем берётся, и какая из них за наш период,
        # шапка не объявила. Это потеря величины, а не мелочь вёрстки:
        # у промежуточного ФосАгро так пропадали шестимесячные графы, а
        # квартальные шли в комплект за полугодие — согласованные сами
        # с собой и потому не отличимые от верных ни одним контролем.
        reasons.append(ReviewReason.DROPPED_COLUMN)
        problems.extend(
            f"строка «{name or '(без наименования)'}» формы "
            f"{form.removeprefix('ifrs.')}: отброшены величины "
            + ", ".join(str(value) for value in values)
            for form, name, values in extraction.dropped_values[:5]
        )
    if profile.pages_without_text:
        reasons.append(ReviewReason.LOST_PAGE)
        problems.append(
            "внутри форм страницы без текстового слоя: "
            + ", ".join(str(number) for number in profile.pages_without_text)
            + " — содержимое не извлечено вовсе"
        )
    if profile.reporting_kind is not ReportingKind.FULL:
        reasons.append(ReviewReason.REPORTING_KIND)
        problems.append(
            f"вид отчётности «{profile.reporting_kind.value}»: состав раскрытий "
            "уже, чем у полной"
        )

    result = ReviewResult(
        outcome=ReviewOutcome.MANUAL_REQUIRED if reasons else ReviewOutcome.AUTOMATIC,
        reasons=tuple(dict.fromkeys(reasons)),
        totals_checked=totals_checked,
        totals_failed=tuple(failed),
        rows_total=rows_total,
        rows_recognised=extraction.rows_recognised,
        material_items=tuple(material),
        plausibility=plausibility,
        problems=tuple(problems),
        rows_with_values=extraction.rows_with_values,
        rows_with_dropped=len(extraction.dropped_values),
        rows_confirmed=tuple(sorted(known.rows)),
        confirmed_from=known.from_reports,
        rows_ignored=len(extraction.ignored),
    )
    logger.info("экран сверки: %s", result.describe())
    return result


def _values_with(
    extraction: Extraction, report_date: date, known: Confirmed
) -> dict[str, Decimal]:
    """Величины отчётного периода вместе с ранее подтверждёнными.

    Подтверждённое опознание — такое же опознание, поэтому величина
    подтверждённой строки участвует в итогах. Своё опознание справочником
    она не перебивает: величина уже взятая остаётся.
    """
    values = dict(known.values)
    values.update(extraction.totals(report_date))
    return values


def _check_totals(
    extraction: Extraction,
    catalog: IfrsCatalog,
    report_date: date,
    known: Confirmed | None = None,
) -> tuple[int, list[TotalCheck]]:
    """Сверяет итоги форм с суммами их состава и тождества распределения.

    Арифметика та же, что у контролей РСБУ: `quality/totals.py` работает
    с любым справочником, потому что проверяет равенство суммы, а не природу
    кодов.

    **Тождество распределения проверяется отдельно от состава.** Прибыль
    за период набирается из прибыли до налогообложения и налога, а делится
    между акционерами материнской компании и неконтролирующими долями
    (МСФО (IAS) 1.81B): это два разных утверждения об одной величине, и оба
    обязаны сойтись. Объявить распределение запасным составом нельзя —
    сошедшееся распределение закрыло бы собой несошедшуюся цепочку прибыли.

    Ранее подтверждённые величины входят в состав наравне с опознанными
    справочником, а подтверждённые специфические статьи — через `extras`,
    как и при разметке: кода справочника у них нет, а в итог раздела они
    входят. Иначе у ЛСР все четырнадцать итогов сходились бы на экране
    разметки и проваливались на экране сверки — по одним и тем же данным.
    """
    known = known or Confirmed()
    values = _values_with(extraction, report_date, known)
    checked = 0
    failed: list[TotalCheck] = []

    def tolerance(amount: Decimal) -> Decimal:
        """Допуск сходимости: доля итога и одна единица на округление."""
        return abs(amount) / Decimal(1000) + Decimal(1)

    def sign_of(code: str) -> int:
        """Нормальный знак позиции; неизвестный код считается положительным."""
        position = catalog.get(code)
        return position.normal_sign if position is not None else 1

    # Итог сверяется по **лучшему из объявленных составов**, и эта арифметика
    # одна на два экрана. Прежде экран разметки знал о запасных составах,
    # а экран сверки нет, и один и тот же комплект получал два разных ответа:
    # у ЛСР разметка показывала четырнадцать сошедшихся итогов из
    # четырнадцати, а сверка — провал контроля. Составы объявлены методикой
    # поимённо, потому что у Сегежи нет валовой прибыли, а у ФосАгро
    # и Норникеля — строки «Итого обязательства».
    from finlib.sources.ifrs_markup import best_composition

    for total in catalog.totals():
        found = best_composition(
            total, values, catalog, known.extras.get(total.code, Decimal(0)), _TOLERANCE
        )
        if found.verdict in (TotalVerdict.MATCHED, TotalVerdict.MISMATCHED):
            checked += 1
        if found.verdict is TotalVerdict.MISMATCHED:
            failed.append(found)

    # Тождества распределения — запасных составов у них нет и быть не может:
    # распределение объявлено одно.
    for item in catalog.positions:
        if not item.split_into:
            continue
        found = check_total(
            Composition(item.code, item.split_into),
            values.get,
            lambda code: None,
            tolerance,
            sign_of,
        )
        if found.verdict in (TotalVerdict.MATCHED, TotalVerdict.MISMATCHED):
            checked += 1
        if found.verdict is TotalVerdict.MISMATCHED:
            failed.append(found)
    return checked, failed


# Допуск сходимости на экране сверки: доля итога. Величины печатаются
# округлёнными, и последняя цифра итога не обязана совпадать с суммой
# слагаемых до единицы.
_TOLERANCE = Decimal("0.001")


def _material_items(
    rows: list[UnrecognisedRow],
    extraction: Extraction,
    report_date: date,
    catalog: IfrsCatalog,
) -> list[MaterialItem]:
    """Неопознанные статьи, превышающие порог существенности.

    Статья сверх порога никогда не сворачивается в «прочее»: у Автодора 85 %
    активов лежат в двух статьях, которых нет ни у кого другого. Порог задан
    методикой, доля считается от валюты баланса.

    Считаются строки, оставшиеся неопознанными: статья, которой человек уже
    присвоил код у этого эмитента, — та самая «вынесенная отдельной позицией»,
    о которой говорит правило существенности, а не свёрнутая в «прочее».
    """
    assets = extraction.value_of("ifrs.total_assets", report_date)
    if assets is None or assets == 0:
        # Валюты баланса нет — долю считать не от чего. Сами неопознанные
        # строки при этом уже потребовали подтверждения, так что молчания
        # здесь не возникает.
        return []
    threshold = catalog.materiality.share_of_total_assets
    found: list[MaterialItem] = []
    for row in rows:
        # Доля считается одной функцией на весь проект: прежде то же
        # выражение стояло здесь, в загрузчике и в разметке — и в разметке
        # знаменателем была выручка, а называлось это тоже долей активов.
        share = share_of_assets(row, assets)
        if share >= threshold:
            found.append(MaterialItem(row, share))
    return found
