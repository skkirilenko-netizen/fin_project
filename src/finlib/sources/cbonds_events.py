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

**Дата события приходит отдельным методом, и искать её пришлось дважды.**
`get_emission_default` отдаёт по каждому дефолту плановый срок исполнения
(`estimated_date`), дату дефолта (`default_date`), дату объявления, **дату
фактического исполнения** (`actual_date`) и неисполненную сумму. Перечень
берётся целиком по стране — 3 529 записей в четырёх запросах, — и лежит
в `defaults_ru.json`.

Прежде дата выводилась из даты погашения выпуска, и это было неверно:
у Мечела неисполнение оферты датировано 17.09.2015, а дата погашения его
выпусков — 25.02.2020, то есть ошибка в пять лет. Ошибка была нашей дважды:
метод объявлен в `docs/cbonds/openapi.yaml`, лежащем в самом репозитории,
а первая проба его имени оборвалась на транспорте и была записана как
«метода нет». **Отсутствие ответа ответом не является**, и справочник методов
следует читать прежде, чем угадывать имена.

**Факт платежа в графике платежей не лежит.** `get_flow_new` отдаёт поле
`actual_payment_date`, которое выглядит датой фактического платежа, а является
**сроком, сдвинутым на рабочий день**: у Кириллицы купон со сроком
07.10.2023 (суббота) стоит с «фактом» 09.10.2023, а у ЕвроТранса заполнены
и платежи 2027 года, которых ещё не было. По 93 выпускам с признаком дефолта
неуплаченным не оказался ни один — включая выпуск Кириллицы в статусе «Дефолт
по погашению», у которого неисполненная сумма 300 000 000 стоит в записи
дефолта.
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
# Перечень дефолтов по стране целиком: события, даты, суммы и дата
# фактического исполнения. Собирает `scripts/events_fetch.py`.
DEFAULTS = CACHE / "defaults_ru.json"
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

    # Идентификатор выпуска у источника: по нему лежит график платежей,
    # и без него выпуск с графиком не связать.
    emission_id: str
    name: str
    # ISIN: ключ к бирже. У торгуемой облигации SECID Московской биржи равен
    # ISIN, и переходника между источниками не требуется.
    isin: str
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


@dataclass(frozen=True, slots=True)
class Guarantee:
    """Поручительство по выпуску: кто отвечает по долгу и в каком виде.

    **Вид обязательства объявлен статусом записи.** Поручитель и гарант
    отвечают по долгу, оферент обязуется выкупить бумагу по требованию —
    это обязательство о ликвидности, а не о кредитном качестве, и брать
    по нему чужую корзину нельзя.
    """

    inn: str
    name: str
    status: str
    issue: str


def guarantees_of(inn: str, statuses: frozenset[str]) -> tuple[Guarantee, ...]:
    """Поручительства эмитента с диска; перечень видов объявлен методикой.

    Одно и то же поручительство стоит у нескольких выпусков, и перечень
    сводится по паре «ИНН, вид»: эмитенту важно, кто отвечает, а не по скольким
    выпускам.
    """
    path = CACHE / f"guarantors_{inn}.json"
    if not path.exists():
        return ()
    found: dict[tuple[str, str], Guarantee] = {}
    for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
        status = str(item.get("status_name_rus") or "").strip()
        if status not in statuses:
            continue
        who = str(item.get("guarantor_inn") or "")
        key = (who, status)
        found.setdefault(
            key,
            Guarantee(
                inn=who,
                name=str(item.get("guarantor_name_rus") or "").strip(),
                status=status,
                issue=str(item.get("emission_document_rus") or "").strip(),
            ),
        )
    return tuple(found.values())


@dataclass(frozen=True, slots=True)
class DefaultRecord:
    """Одно событие дефолта: что не исполнено, когда и на какую сумму.

    **Исполнено ли обязательство, говорит дата фактического исполнения**
    (`actual_date`): у ДВМП купон, просроченный 05.05.2017, уплачен 23.05.2017,
    а погашение 2018 года не исполнено до сих пор. Признак карточки эмитента
    говорит о том же, но одним словом на всего эмитента.
    """

    emission_id: str
    kind: str
    status: str
    due: date | None
    when: date | None
    announced: date | None
    met: date | None
    amount: Decimal | None

    @property
    def settled(self) -> bool:
        """Исполнено ли обязательство в конце концов."""
        return self.met is not None

    @property
    def moment(self) -> date | None:
        """Дата события: дата дефолта, иначе плановый срок, иначе объявление."""
        return self.when or self.due or self.announced


@dataclass(frozen=True, slots=True)
class DefaultEvent:
    """Дата дефолта, чем она является и по какому выпуску получена.

    **Дата может быть неизвестна, и это не то же самое, что её отсутствие
    в природе.** Перечня дефолтов может не быть на диске вовсе, и тогда
    давность не считается: понизить корзину по неизвестной давности значило бы
    решить по отсутствию данных.
    """

    when: date | None
    origin: str
    issue: str

    @property
    def known(self) -> bool:
        """Есть ли дата, по которой считается давность."""
        return self.when is not None


def default_records() -> dict[str, tuple[DefaultRecord, ...]]:
    """События дефолтов по выпускам с диска; пусто — перечня нет.

    Перечень забирается целиком по стране (`emission_emitent_country_id = 1`):
    3 529 записей в четырёх запросах против одного запроса на выпуск.
    """
    if not DEFAULTS.exists():
        logger.warning("перечня дефолтов на диске нет: %s", DEFAULTS)
        return {}
    found: dict[str, list[DefaultRecord]] = {}
    for item in json.loads(DEFAULTS.read_text(encoding="utf-8")).get("items", []):
        emission = str(item.get("emission_id") or "")
        found.setdefault(emission, []).append(
            DefaultRecord(
                emission_id=emission,
                kind=str(item.get("type_name_rus") or ""),
                status=str(item.get("status_name_rus") or ""),
                due=_as_date(item.get("estimated_date")),
                when=_as_date(item.get("default_date")),
                announced=_as_date(item.get("announcement_date")),
                met=_as_date(item.get("actual_date")),
                amount=_as_number(item.get("unsettled_amount")),
            )
        )
    return {key: tuple(rows) for key, rows in found.items()}


def default_event(records: tuple[DefaultRecord, ...], unsettled: bool) -> DefaultEvent:
    """Дата дефолта по событиям: свежайшая, и у неурегулированного своя.

    **Неисполненное обязательство старше исполненного.** У ТГК-2 семь записей,
    из них две без даты исполнения: давность считается по ним, а не по той,
    что позже и улажена. Если неисполненных записей нет, а признак эмитента
    говорит о неурегулированности, берётся свежайшая из имеющихся — источники
    расходятся, и вопрос о статусе урегулирования как раз и задаётся.
    """
    dated = [item for item in records if item.moment is not None]
    if not dated:
        return DefaultEvent(None, "", "")
    open_ones = [item for item in dated if not item.settled]
    pool = open_ones if (unsettled and open_ones) else dated
    latest = max(pool, key=lambda item: item.moment)
    origin = f"{latest.kind.lower()}, {latest.status.lower()}"
    if latest.due is not None:
        origin += f", срок {latest.due:%d.%m.%Y}"
    return DefaultEvent(latest.moment, origin, latest.emission_id)


@dataclass(frozen=True, slots=True)
class Rating:
    """Рейтинг эмитента: агентство, точка шкалы, категория, прогноз."""

    agency: str
    scale: str
    point: str
    category: str
    outlook: str
    assigned: date | None
    # **Место точки в своей шкале, а не наша догадка о старшинстве.** Какая
    # из двух категорий хуже, по написанию не видно: «AA» длиннее «C»
    # и по любому правилу сравнения строк оказывается «больше». Справочник
    # шкал объявляет место сам (`rating_scale_point_ordnum`, 1 — высшая),
    # и сравнение идёт им. `None` означает, что точки в справочнике нет:
    # сравнивать нечем, и молча считать её высшей нельзя.
    order: int | None = None
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
    # События дефолтов по выпускам эмитента: даты, суммы, факт исполнения.
    # Пусто при непустом `defaulted` означает, что перечня нет на диске.
    records: tuple[DefaultRecord, ...] = ()
    records_known: bool = False

    @property
    def open_records(self) -> tuple[DefaultRecord, ...]:
        """События, обязательство по которым не исполнено до сих пор."""
        return tuple(item for item in self.records if not item.settled)

    @property
    def unsettled_default(self) -> bool:
        """Есть ли неурегулированный дефолт: по событиям, а не по признаку.

        **События первичны, признак карточки — только при их отсутствии**
        (решение от 22.09.2026). Событие подробнее и датировано: у него есть
        вид обязательства, срок, сумма и дата фактического исполнения, —
        а признак карточки отстаёт, и это видно на ДВМП: неисполненные
        обязательства 2018 года, а признак стоит бессрочно.

        Прежде брался тяжелейший из двух источников, и у Росгеологии
        с Концессиями теплоснабжения карточка держала обстоятельство при всех
        исполненных событиях. Расхождение при этом не исчезает: оно считается
        и идёт в перечень к агрегатору — это противоречие **внутри источника**,
        а не вопрос к эмитенту.
        """
        if self.records:
            return bool(self.open_records)
        return bool(self.defaulted)

    @property
    def settled_only(self) -> bool:
        """Дефолт был и улажен целиком: кредитная история, а не состояние."""
        had = bool(self.settled) or bool(self.records)
        return had and not self.unsettled_default

    @property
    def undated_records(self) -> tuple[DefaultRecord, ...]:
        """Неисполненные события, у которых даты нет ни в одном поле.

        **Датированное обстоятельство недатированного не закрывает.** Событие
        без даты давности не имеет, и считать её по другому событию значило бы
        объявить старым то, о чём даты нет.
        """
        return tuple(item for item in self.open_records if item.moment is None)

    @property
    def sources_disagree(self) -> bool:
        """Расходятся ли карточка и события в том, улажен ли дефолт."""
        return bool(self.defaulted) and bool(self.records) and not self.open_records

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
    def worst(self) -> Rating | None:
        """Действующий рейтинг с наименьшим местом в своей шкале.

        **Сравнение идёт местом точки, а не написанием.** «AA» длиннее «C»
        и любым сравнением строк оказывается «больше» — графа «худшая
        категория» считала бы не то, как называется. Рейтинг, места которого
        в справочнике нет, в сравнение не идёт вовсе: молча считать его
        высшим значило бы спрятать неполноту справочника.
        """
        rated = [item for item in self.live if item.order is not None]
        return max(rated, key=lambda item: item.order) if rated else None

    @property
    def live(self) -> tuple[Rating, ...]:
        """Действующие кредитные рейтинги: отозванные значением не считаются."""
        return tuple(
            item for item in self.ratings if item.credit and not item.withdrawn
        )

    def event(self) -> DefaultEvent:
        """Дата дефолта: свежайшая, у неурегулированного — по неисполненному.

        Одна дата на оба исхода: дефолт у эмитента либо улажен, либо нет,
        и двух событий разной давности одновременно у него не бывает —
        давность считается от свежайшего.
        """
        return default_event(self.records, self.unsettled_default)

    # **Рефинансирование здесь не считается, и это правило, а не пробел.**
    # Прежде объём к погашению брался как остаток выпуска целиком, если
    # погашение либо оферта попадали в окно; график платежей отвечает точнее —
    # купоны и амортизация порознь от оферты, потому что предъявление бумаги
    # право владельца, а не обязанность. Мера оставлена одна:
    # `sources/cbonds_flows.py::refinancing`.


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


def point_order() -> dict[tuple[str, str], int]:
    """Место точки в шкале по паре «шкала, написание»; 1 — высшая.

    Ключ — пара, а не написание: «C» стоит в нескольких шкалах, и место у него
    своё в каждой. Сравнивать места точек разных шкал можно лишь приблизительно,
    поэтому сравнение и делается внутри шкалы, а не между ними.
    """
    found: dict[tuple[str, str], int] = {}
    for item in scale_points().values():
        number = item.get("rating_scale_point_ordnum")
        if number is None:
            continue
        found[(str(item.get("scale_id")), str(item.get("name")))] = int(number)
    return found


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
                emission_id=str(item.get("id") or ""),
                name=str(item.get("document_rus") or item.get("isin_code") or "—"),
                isin=str(item.get("isin_code") or "").strip(),
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
    order: dict[tuple[str, str], int] | None = None,
    defaults: dict[str, tuple[DefaultRecord, ...]] | None = None,
) -> IssuerEvents:
    """События эмитента: выпуски с диска, рейтинги снимка, события дефолтов."""
    if snapshot is None:
        _, snapshot = latest_snapshot()
    if credit is None:
        credit = credit_scales()
    if order is None:
        order = point_order()
    if defaults is None:
        defaults = default_records()
    issues, known = issues_of(inn)
    rated = snapshot.get(inn)
    ratings = tuple(
        Rating(
            agency=str(item.get("agency_name_rus") or ""),
            scale=str(item.get("scale_name_rus") or ""),
            point=str(item.get("scale_point_name") or ""),
            category=category_of(str(item.get("scale_point_name") or "")),
            outlook=str(item.get("forecast_name_rus") or ""),
            assigned=_as_date(item.get("rating_date")),
            order=order.get(
                (str(item.get("scale_id")), str(item.get("scale_point_name") or ""))
            ),
            credit=str(item.get("scale_id")) in credit,
        )
        for item in (rated or ())
    )
    return IssuerEvents(
        inn=inn,
        issues=issues,
        ratings=ratings,
        issues_known=known,
        ratings_known=rated is not None,
        records=tuple(
            entry for item in issues for entry in defaults.get(item.emission_id, ())
        ),
        records_known=bool(defaults),
    )
