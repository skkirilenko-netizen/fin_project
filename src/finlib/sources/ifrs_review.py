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

**Неполный вид — с двумя исключениями по решению владельца 28.09.2026**
(`ReviewContext`): промежуточная проходит сама при подтверждённом годовом
того же эмитента и всех опознанных позициях, раскрываемая — с понижением
уверенности вместо стопа. Специального назначения решение не касается.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import partial

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.quality.codes import CheckCode
from finlib.quality.totals import (
    Composition,
    TotalCheck,
    TotalVerdict,
    check_total,
)
from finlib.sources.ifrs_confirmed import Confirmed
from finlib.sources.ifrs_extract import (
    Extraction,
    UnrecognisedRow,
    materiality_base,
    materiality_share,
)
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
# Коды свои, а не коды бухгалтерских контролей. Прежде основания ветки писались
# кодами РСБУ, и документ по МСФО называл их наименованиями другого предмета:
# «определение типа отчётности по содержимому файла» вместо «вид отчётности
# неполный». Два основания — неопознанная позиция и статья сверх порога —
# делили при этом один код, и различить их в журнале было нечем.
REASON_CODES: dict[ReviewReason, CheckCode] = {
    ReviewReason.CHECK_FAILED: CheckCode.IFRS_TOTAL_MISMATCH,
    ReviewReason.IMPLAUSIBLE_GROUPING: CheckCode.DIGIT_GROUPING_IMPLAUSIBLE,
    ReviewReason.UNRECOGNISED_POSITION: CheckCode.IFRS_UNRECOGNISED_POSITION,
    ReviewReason.MATERIAL_SPECIFIC_ITEM: CheckCode.IFRS_MATERIAL_ITEM,
    ReviewReason.REPORTING_KIND: CheckCode.IFRS_REPORTING_KIND,
    ReviewReason.LOST_PAGE: CheckCode.IFRS_LOST_PAGE,
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
    # Мера существенности: величина строки к базе **своей формы**. Прежде
    # графа называлась долей валюты баланса и у строки потока её и содержала —
    # отношение оборота за год к запасу на дату, правомерно превышающее сотню.
    materiality_share: Decimal
    # Позиция, которой мерилась существенность: без неё «12,4 %» не проверить.
    base: str

    def describe(self) -> str:
        """Человеческое описание для экрана сверки."""
        return (
            f"«{self.row.source_name}» — {self.row.largest} "
            f"({self.materiality_share:.1%} от {self.base})"
        )


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """Обстановка комплекта, которой нет в самом документе (решение 28.09.2026).

    **Неполный вид отчётности перестаёт быть стопом в двух случаях.**
    Промежуточный комплект проходит сам, если годовой того же эмитента
    подтверждён и все позиции опознаны: состав раскрытий промежуточной
    сокращён по устройству (МСФО (IAS) 34), а строки те же, что человек уже
    видел в годовом. Раскрываемая отчётность проходит с понижением уверенности
    вместо стопа — но только если основание понижения объявлено методикой
    (`ifrs_metrics.yaml`, `confidence`): пропустить без понижения значило бы
    применить половину решения.

    Обстановку собирает вызывающий из базы; `review` без неё держит прежнее
    строгое правило, и умолчанием это не решается — `pipeline` передаёт её
    всегда.
    """

    # Отчётная дата подтверждённого годового того же эмитента; None — нет.
    annual_confirmed: date | None = None
    # Объявлено ли методикой основание понижения уверенности для раскрываемой.
    disclosable_lowers_confidence: bool = False


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
    # Сколько неопознанных строк удалось измерить порогом существенности.
    # Знаменатель обязателен: у строк потока базы нет вовсе, и ноль статей
    # сверх порога без этого числа неотличим от невыполненной проверки.
    rows_measured: int = 0
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
    # Почему неполный вид отчётности не стал основанием (решение 28.09.2026);
    # пусто — вид полный либо основание стоит.
    kind_passed: str = ""

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
            f"{len(self.material_items)} из {self.rows_measured} измеренных; "
            f"величины отброшены у "
            f"{self.rows_with_dropped} строк из {self.rows_with_values} "
            "с величинами"
        )


def review(
    extraction: Extraction,
    profile: DocumentProfile,
    catalog: IfrsCatalog | None = None,
    confirmed: Confirmed | None = None,
    context: ReviewContext | None = None,
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
    material, rows_measured = _material_items(
        unrecognised, extraction, report_date, catalog
    )

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
    # **Форма, обещанная документом и не найденная, — тоже потеря.** Признак
    # потерянных страниц её не ловит: окно считается между первой и последней
    # **найденной** формой, а у СИБУРа потеряна страница самой первой — отчёта
    # о прибылях, 7-й из 60. Форма, потерянная целиком, выглядела как форма,
    # которой в документе нет.
    if profile.missing_forms:
        reasons.append(ReviewReason.LOST_PAGE)
        promised = {
            "audit_report": "аудиторским заключением",
            "contents": "оглавлением",
            "ias1": "обязательным составом МСФО (IAS) 1",
        }.get(profile.expected_from, profile.expected_from)
        problems.append(
            "формы обещаны "
            + promised
            + ", но в тексте не найдены: "
            + ", ".join(
                code.removeprefix("ifrs.") for code in profile.missing_forms
            )
        )
    passed_kind = _kind_passes(profile.reporting_kind, context, bool(unrecognised))
    if profile.reporting_kind is not ReportingKind.FULL and passed_kind is None:
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
        rows_measured=rows_measured,
        plausibility=plausibility,
        problems=tuple(problems),
        rows_with_values=extraction.rows_with_values,
        rows_with_dropped=len(extraction.dropped_values),
        rows_confirmed=tuple(sorted(known.rows)),
        confirmed_from=known.from_reports,
        rows_ignored=len(extraction.ignored),
        kind_passed=passed_kind or "",
    )
    logger.info("экран сверки: %s", result.describe())
    return result


def _kind_passes(
    kind: ReportingKind, context: ReviewContext | None, unrecognised: bool
) -> str | None:
    """Почему неполный вид отчётности не останавливает; None — останавливает.

    Промежуточная — при подтверждённом годовом того же эмитента и всех
    опознанных позициях; раскрываемая — когда методика объявила понижение
    уверенности. Отчётность специального назначения решением не затронута.
    """
    if context is None:
        return None
    if kind is ReportingKind.INTERIM and context.annual_confirmed and not unrecognised:
        return (
            f"промежуточная: годовой на {context.annual_confirmed:%d.%m.%Y} подтверждён, "
            "все позиции опознаны"
        )
    if kind is ReportingKind.DISCLOSABLE and context.disclosable_lowers_confidence:
        return "раскрываемая: принимается с понижением уверенности"
    return None


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
) -> tuple[list[MaterialItem], int]:
    """Неопознанные статьи сверх порога существенности и число измеренных строк.

    Статья сверх порога никогда не сворачивается в «прочее»: у Автодора 85 %
    активов лежат в двух статьях, которых нет ни у кого другого. Порог задан
    методикой, база — своя у каждой формы.

    **У строки, мерить которую нечем, основания не возникает вовсе.** База
    формы объявлена методикой, и у отчёта о движении денежных средств её нет:
    поток за период не доля ни от запаса, ни от оборота. Нулевой порог был бы
    не тем же самым — он срабатывал бы на любой строке потока, и именно так
    основание и держало комплекты: из 106 статей сверх порога 65 были
    строками потока, у О'КЕЙ — с долями 336,9 % и 301,5 % валюты баланса.

    Возвращается и знаменатель — сколько строк удалось измерить: ноль статей
    сверх порога при неизвестном числе измеренных строк не означает ничего.

    Считаются строки, оставшиеся неопознанными: статья, которой человек уже
    присвоил код у этого эмитента, — та самая «вынесенная отдельной позицией»,
    о которой говорит правило существенности, а не свёрнутая в «прочее».
    """
    threshold = catalog.materiality.share_of_total_assets
    value_of = partial(_value_at, extraction, report_date)
    found: list[MaterialItem] = []
    measured = 0
    for row in rows:
        # Мера считается одной функцией на весь проект: прежде то же
        # выражение стояло здесь, в загрузчике и в разметке — и в разметке
        # знаменателем была выручка, а называлось это тоже долей активов.
        share = materiality_share(row, catalog, value_of)
        if share is None:
            continue
        measured += 1
        base = materiality_base(row.form, catalog)
        if share >= threshold and base is not None:
            found.append(MaterialItem(row, share, base))
    return found, measured


def _value_at(
    extraction: Extraction, report_date: date, code: str
) -> Decimal | None:
    """Величина позиции за отчётный период — база для меры существенности."""
    return extraction.value_of(code, report_date)
