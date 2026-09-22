"""Событийный слой: выпуски и рейтинги эмитента. **Читает диск, не сеть.**

Годовая отчётность не видит того, что случилось **между** отчётными датами:
у ЕвроТранса числа за 2025 год спокойны, а по двенадцати выпускам стоит
неурегулированный дефолт и рейтинги всех четырёх агентств отозваны. Слой
событий отвечает ровно на это.

**Данные собираются отдельными прогонами и лежат на диске**: выпуски —
`scripts/emissions_fetch.py`, рейтинги — ежедневным снимком
`scripts/ratings_snapshot.py`. Здесь только чтение: маршрут не ходит в сеть.

**Категория рейтинга берётся из справочника шкал источника**
(`get_rating_scale_points`, 745 точек), а не из написанной руками таблицы:
у каждой точки объявлены наименование и место в шкале, и «D|ru|», «ruC»,
«C(RU)» различаются буквой категории, а не нашим представлением о ней.
Отзыв при этом — тоже точка шкалы (`Withdrawn`), а не отдельный признак,
и прежнего значения метод `…_maxdate` не хранит: история берётся
из последовательности снимков.

**Дата события источником не приводится.** У дефолта по погашению опорой
служит дата погашения — день, когда платёж был должен состояться; у дефолта
по выпуску в обращении остаётся дата обновления записи. Обе названы тем,
чем они являются: выдумывать дату события мы не будем.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")
SNAPSHOTS = CACHE / "ratings"
SCALE_POINTS = CACHE / "rating_scale_points.json"
SCALES = CACHE / "rating_scales.json"

# Статусы выпуска, которыми источник объявляет дефолт. Сравнение приведённой
# строкой целиком: «дефолт» вхождением поймал бы и «технический дефолт
# устранён», если такой статус появится.
DEFAULT_STATUSES: frozenset[str] = frozenset(
    {"дефолт по погашению", "дефолт по купону", "технический дефолт", "дефолт"}
)

# **Объём выпуска источник отдаёт в рублях, а отчётность бывает в миллионах.**
# Множители кодов ОКЕИ — классификатор, а не суждение: 383 рубли, 384 тысячи,
# 385 миллионы, 386 миллиарды. Без приведения к единице комплекта отношение
# «погашения к денежным средствам» ошибалось бы в тысячу раз — тот же класс
# дефекта, что единица измерения комплекта.
OKEI_MULTIPLIER: dict[str, Decimal] = {
    "383": Decimal(1),
    "384": Decimal(1000),
    "385": Decimal(1000000),
    "386": Decimal(1000000000),
}


def in_unit(value: Decimal | None, unit_code: str | None) -> Decimal | None:
    """Рублёвая величина источника в единице комплекта; None — единица неизвестна."""
    if value is None or unit_code is None:
        return None
    multiplier = OKEI_MULTIPLIER.get(str(unit_code))
    return None if multiplier is None else value / multiplier


# Приведение написания точки шкалы к категории: национальные шкалы пишут
# «ruAA-», «AA-(RU)», «D|ru|», «C(ru.sf)», собственная кредитоспособность —
# строчными. Категория — ведущая буквенная группа без префикса и суффикса.
_PREFIX = re.compile(r"^ru", re.IGNORECASE)
_SUFFIX = re.compile(r"[|(].*$")
_LETTERS = re.compile(r"^([A-Da-d]+)")


@dataclass(frozen=True, slots=True)
class Issue:
    """Выпуск эмитента: то, что нужно маршруту и рефинансированию."""

    name: str
    status: str
    default: bool
    unsettled: bool
    maturity: date | None
    offer: date | None
    outstanding: Decimal | None
    updated: date | None

    @property
    def defaulted(self) -> bool:
        """Дефолт **текущий**: не улажен либо объявлен статусом выпуска.

        **Текущий дефолт и исторический — разные обстоятельства.** У ДВМП
        признак дефолта стоит по еврооблигациям, погашенным около десяти лет
        назад: событие настоящее, но давно урегулированное, и отправлять
        по нему в разбор значило бы судить о сегодняшнем эмитенте по его
        прошлому. Признак неурегулированности и статус выпуска говорят
        о настоящем, признак `has_default` сам по себе — о прошлом.
        """
        return self.unsettled or self.status in DEFAULT_STATUSES

    @property
    def settled_default(self) -> bool:
        """Дефолт в прошлом: признак есть, неурегулированности нет."""
        return self.default and not self.defaulted

    def due_within(self, months: int, today: date) -> Decimal | None:
        """Объём к погашению или оферте в ближайшие месяцы; None — нечего."""
        if self.outstanding is None:
            return None
        edge = date(
            today.year + (today.month - 1 + months) // 12,
            (today.month - 1 + months) % 12 + 1,
            1,
        )
        soon = [item for item in (self.maturity, self.offer) if item is not None]
        if any(today <= item < edge for item in soon):
            return self.outstanding
        return None


@dataclass(frozen=True, slots=True)
class Rating:
    """Рейтинг эмитента: агентство, точка шкалы, категория, прогноз."""

    agency: str
    scale: str
    point: str
    category: str
    outlook: str
    assigned: date | None
    # **Кредитный ли это рейтинг.** ESG-рейтинг о кредитоспособности не говорит,
    # и его точка «ESG-A-» в градацию кредитного риска попадать не должна.
    # Вид шкалы берётся у справочника источника, а не по вхождению «ESG»
    # в наименование: написание — примета, вид — признак.
    credit: bool = True

    @property
    def withdrawn(self) -> bool:
        """Отозван ли рейтинг: отзыв — точка шкалы, а не отдельный признак."""
        return self.point.strip().lower() == "withdrawn"


@dataclass(frozen=True, slots=True)
class IssuerEvents:
    """События эмитента: выпуски и рейтинги вместе с признаком «данных нет»."""

    inn: str
    issues: tuple[Issue, ...] = ()
    ratings: tuple[Rating, ...] = ()
    # **«Данных нет» и «событий нет» — разные вещи.** Пустой перечень выпусков
    # у эмитента без облигаций и отсутствие ответа источника выглядят
    # одинаково, а значат противоположное.
    issues_known: bool = False
    ratings_known: bool = False

    @property
    def defaulted(self) -> tuple[Issue, ...]:
        """Выпуски с текущим дефолтом: неурегулированным либо по статусу."""
        return tuple(item for item in self.issues if item.defaulted)

    @property
    def settled(self) -> tuple[Issue, ...]:
        """Выпуски с дефолтом в прошлом: сведение, а не обстоятельство."""
        return tuple(item for item in self.issues if item.settled_default)

    @property
    def outstanding(self) -> tuple[Issue, ...]:
        """Выпуски в обращении."""
        return tuple(item for item in self.issues if item.status == "в обращении")

    @property
    def live(self) -> tuple[Rating, ...]:
        """Действующие кредитные рейтинги: отозванные значением не считаются."""
        return tuple(
            item for item in self.ratings if item.credit and not item.withdrawn
        )

    def due(self, months: int, today: date) -> Decimal:
        """Объём к погашению и оферте в ближайшие месяцы."""
        return sum(
            (item.due_within(months, today) or Decimal(0) for item in self.issues),
            start=Decimal(0),
        )


def _as_date(value: object) -> date | None:
    """Дата источника; пустое и мусор остаются None."""
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_number(value: object) -> Decimal | None:
    """Величина источника в Decimal; пустое остаётся None."""
    if value in (None, "", "0"):
        return None if value != "0" else Decimal(0)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — мусор источника величиной не становится
        return None


def category_of(point: str) -> str:
    """Категория рейтинга по написанию точки шкалы.

    Правило одно на все шкалы: ведущая буквенная группа без национального
    префикса и суффикса. Справочник шкал даёт написания и порядок точек,
    а категория — их буква: у «D|ru|», «ruC» и «C(RU)» она различима, а таблица,
    написанная руками, разошлась бы с ним на первой же новой шкале.
    """
    text = _SUFFIX.sub("", _PREFIX.sub("", point.strip()))
    found = _LETTERS.match(text)
    return found.group(1).upper() if found else point.strip()


def scale_points() -> dict[str, dict]:
    """Справочник точек шкал по идентификатору; пусто — справочника нет."""
    if not SCALE_POINTS.exists():
        logger.warning("справочника точек шкал на диске нет: %s", SCALE_POINTS)
        return {}
    items = json.loads(SCALE_POINTS.read_text(encoding="utf-8")).get("items", [])
    return {str(item["id"]): item for item in items}


def credit_scales() -> frozenset[str]:
    """Идентификаторы кредитных шкал: перечень объявлен методикой.

    **Вид шкалы у источника для этого не годится.** Тип «национальная» стоит
    только у АКРА, а у национальных шкал Эксперт РА, НКР и НРА он тот же, что
    у ESG-шкал, — ноль. Поэтому кредитные шкалы названы в методике
    по идентификаторам, а шкала, которой в перечне нет, кредитной не считается.

    **Неизвестная шкала печатается в журнале вместе с наименованием.** Новая
    шкала обязана быть замечена: без этого ESG-точка «ESG-A-» однажды попала бы
    в градацию кредитного риска молча — ровно это и случилось при первом
    прогоне.
    """
    from finlib.scoring.routing import load_routing

    declared = load_routing().events.credit_scales
    if not SCALES.exists():
        logger.warning("справочника шкал на диске нет: %s", SCALES)
        return frozenset(declared)
    items = json.loads(SCALES.read_text(encoding="utf-8")).get("items", [])
    unknown = {
        str(item["id"]): str(item.get("name_rus"))
        for item in items
        if str(item["id"]) not in declared
    }
    logger.debug("шкал вне перечня кредитных: %d", len(unknown))
    return frozenset(declared)


def unknown_scales(snapshot: dict[str, list[dict]]) -> dict[str, int]:
    """Шкалы снимка, не объявленные кредитными: наименование и число записей.

    Считаются не для отбора, а чтобы новая шкала не осталась незамеченной:
    «в градацию попало ноль ESG-точек» без знаменателя ничего не значит.
    """
    from finlib.scoring.routing import load_routing

    declared = load_routing().events.credit_scales
    names: dict[str, str] = {}
    if SCALES.exists():
        names = {
            str(item["id"]): str(item.get("name_rus"))
            for item in json.loads(SCALES.read_text(encoding="utf-8")).get("items", [])
        }
    found: dict[str, int] = {}
    for records in snapshot.values():
        for item in records:
            scale = str(item.get("scale_id"))
            if scale in declared:
                continue
            key = f"{scale} {names.get(scale, item.get('scale_name_rus') or '')}"
            found[key] = found.get(key, 0) + 1
    return found


def latest_snapshot() -> tuple[date | None, dict[str, list[dict]]]:
    """Свежий снимок рейтингов: его дата и записи по ИНН."""
    if not SNAPSHOTS.exists():
        return None, {}
    files = sorted(SNAPSHOTS.glob("*.json"))
    if not files:
        return None, {}
    found = json.loads(files[-1].read_text(encoding="utf-8"))
    return _as_date(found.get("date")), found.get("issuers") or {}


def issues_of(inn: str) -> tuple[tuple[Issue, ...], bool]:
    """Выпуски эмитента с диска и признак того, что ответ источника есть."""
    path = CACHE / f"emissions_{inn}.json"
    if not path.exists():
        return (), False
    items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    return (
        tuple(
            Issue(
                name=str(item.get("document_rus") or item.get("isin_code") or "—"),
                status=str(item.get("status_name_rus") or "").strip().lower(),
                default=str(item.get("has_default")) == "1",
                unsettled=str(item.get("has_unsettled_default")) == "1",
                maturity=_as_date(item.get("maturity_date")),
                offer=_as_date(
                    item.get("offert_date_put") or item.get("offert_date")
                ),
                outstanding=_as_number(item.get("outstanding_volume")),
                updated=_as_date(item.get("updating_date")),
            )
            for item in items
        ),
        True,
    )


def events_of(
    inn: str,
    snapshot: dict[str, list[dict]] | None = None,
    credit: frozenset[str] | None = None,
) -> IssuerEvents:
    """События эмитента: выпуски с диска и рейтинги из свежего снимка."""
    if snapshot is None:
        _, snapshot = latest_snapshot()
    if credit is None:
        credit = credit_scales()
    issues, known = issues_of(inn)
    records = snapshot.get(inn)
    ratings = tuple(
        Rating(
            agency=str(item.get("agency_name_rus") or ""),
            scale=str(item.get("scale_name_rus") or ""),
            point=str(item.get("scale_point_name") or ""),
            category=category_of(str(item.get("scale_point_name") or "")),
            outlook=str(item.get("forecast_name_rus") or ""),
            assigned=_as_date(item.get("rating_date")),
            credit=str(item.get("scale_id")) in credit,
        )
        for item in (records or ())
    )
    return IssuerEvents(
        inn=inn,
        issues=issues,
        ratings=ratings,
        issues_known=known,
        ratings_known=records is not None,
    )
