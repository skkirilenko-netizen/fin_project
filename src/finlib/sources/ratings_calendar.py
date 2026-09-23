"""История рейтинговых действий за два года — отдельный слой проверки.

**Корзину этот слой не называет и не двигает.** Подписка отдаёт только
последнее значение рейтинга, и история выгружена руками; правило маршрута
остаётся прежним — оно строится на ежедневном снимке. Календарь отвечает
на то, на что снимок ответить не может: упреждение правила отзыва, ложные
тревоги, понижения перед дефолтами.

**Из него берётся ровно одно — дата перехода в нынешнюю категорию**
(`transitions`). Снимок говорит, когда рейтинг подтверждён в последний раз,
а не когда эмитент в эту категорию попал: у Кириллицы категория C стоит
с 14.05.2026, а снимок датирован последним подтверждением. Дата — содержание
основания, а не его условие: корзину по-прежнему называет категория снимка,
и `test_the_calendar_does_not_move_a_basket` проверяет, что вердикт без
календаря и с календарём один и тот же.

Замер упреждения объясняет, почему больше отсюда не берётся ничего: перевод
в категорию дефолта опережает само событие на 42 дня по медиане — это
признание случившегося, а не предупреждение о нём.

**Значение рейтинга и прогноз — разные сведения, и события у них разные.**
Источник печатает их одной строкой: «AA(RU) (Стабильный)». Изменение уровня
и изменение одного прогноза при том же уровне — не одно и то же, и сложенные
вместе они дали бы 1 682 «изменения», среди которых настоящих понижений
меньше.

**Направление берётся у справочника точек шкалы, а не у написания.** «AA»
длиннее «C» и любым сравнением строк оказывается «больше»; место точки
объявлено источником (`rating_scale_point_ordnum`, 1 — высшая), и сравнение
идёт внутри одной шкалы: места точек разных шкал несопоставимы.

**Привязка к ИНН — только дословная.** Наименования в выгрузке есть, ИНН нет;
«Газпром», «Газпром нефть» и «Газпром капитал» — разные эмитенты, и поиск
по вхождению у нас уже однажды солгал на поручителе. Несопоставившееся
не пропадает: оно называется перечнем.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from finlib.utils import marked_by

logger = logging.getLogger(__name__)

CALENDAR = Path("data/raw/cbonds/calendar")

# **Отзыв — значение шкалы, а не признак.** Написание дословное: источник
# печатает его латиницей в любой шкале.
WITHDRAWN = "withdrawn"

# Шкалы, которые в измерение кредитных действий не идут. Перечень поимённый
# и по той же причине, что у ESG: страховая надёжность и надёжность долговых
# инструментов структурного финансирования — не кредитоспособность эмитента,
# а «данные недоступны в Excel» не шкала вовсе. Шкалы структурного
# финансирования при этом считаются отдельно: для СФО рейтинг транша и есть
# оценка качества.
TRANCHE_SCALES = (
    "Рейтинги надежности долг. инстр. структурного финансирования",
    "Национальная рейтинговая шкала сектора структ. фин. для РФ",
    "Рейтинг долговых инструментов, являющихся структурными облигациями",
)
NOT_CREDIT = (
    "Рейтинг фин. надежности страховым компаниям по межд. шкале",
    "Международная шкала для международных организаций",
    "данные недоступны в Excel",
    *TRANCHE_SCALES,
)

# Прогноз стоит внутри значения, в скобках: «AA(RU) (Стабильный)». Уровень
# при этом сам содержит скобки — «AA(RU)», — поэтому отделяется последняя
# группа, а не первая.
_FORECAST = re.compile(r"^(?P<level>.+?)\s*\((?P<forecast>[^()]+)\)\s*$")

# Прогнозы источника: пять значений справочника. Перечень нужен, чтобы
# не принять за прогноз хвост самого уровня — «AA(RU)» тоже кончается скобкой.
FORECASTS = (
    "Стабильный",
    "Позитивный",
    "Негативный",
    "Развивающийся",
)


@dataclass(frozen=True, slots=True)
class Action:
    """Одно рейтинговое действие календаря."""

    when: date
    agency: str
    scale: str
    # Наименование так, как его печатает выгрузка: ИНН в ней нет вовсе.
    name: str
    isin: str
    reg_number: str
    level: str
    forecast: str
    was_level: str
    was_forecast: str
    # Эмитент либо эмиссия: у первого рейтинг о самом эмитенте, у второй —
    # о выпуске, и смешивать их нельзя.
    about: str

    @property
    def withdrawn(self) -> bool:
        """Отзыв: значение шкалы, а не признак."""
        return self.level.strip().lower() == WITHDRAWN

    @property
    def level_changed(self) -> bool:
        """Изменился ли сам уровень; прогноз здесь ни при чём."""
        return bool(self.was_level) and self.level != self.was_level

    @property
    def forecast_only(self) -> bool:
        """Изменился только прогноз при том же уровне — другое событие."""
        return (
            bool(self.was_level)
            and self.level == self.was_level
            and self.forecast != self.was_forecast
        )

    @property
    def affirmed(self) -> bool:
        """Подтверждение: и уровень, и прогноз те же."""
        return (
            bool(self.was_level)
            and self.level == self.was_level
            and self.forecast == self.was_forecast
        )


def split_rating(text: str) -> tuple[str, str]:
    """Уровень и прогноз из строки источника: «AA(RU) (Стабильный)».

    Прогнозом считается только объявленное значение справочника: уровень сам
    кончается скобкой, и «AA(RU)» без этого разбирался бы как уровень «AA»
    с прогнозом «RU».
    """
    said = str(text or "").strip()
    if not said:
        return "", ""
    found = _FORECAST.match(said)
    if found is None:
        return said, ""
    tail = found.group("forecast").strip()
    if tail not in FORECASTS:
        return said, ""
    return found.group("level").strip(), tail


def _as_date(value: object) -> date | None:
    """Дата ячейки; пустое и мусор остаются `None`."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _agency(kind: str) -> str:
    """Агентство из вида события: «Рейтинг эмитента: АКРА»."""
    return str(kind or "").split(":")[-1].strip()


def _about(kind: str) -> str:
    """О чём действие: об эмитенте либо об эмиссии.

    Вхождение проверяется единой точкой: пустой маркер входит в любой текст,
    и проверка, всегда отвечающая «да», неотличима от невыполненной.
    """
    return "emission" if marked_by(str(kind or ""), ("эмиссии",), str.lower) else "issuer"


def read_actions(folder: Path | None = None) -> tuple[Action, ...]:
    """Читает все файлы выгрузки календаря; шапки у них две, набора два."""
    import openpyxl

    folder = folder or CALENDAR
    found: list[Action] = []
    for path in sorted(folder.glob("*.xlsx")):
        book = openpyxl.load_workbook(path, read_only=True)
        sheet = book.active
        rows = sheet.iter_rows(values_only=True)
        head = [str(item or "") for item in next(rows)]
        at = {name: number for number, name in enumerate(head)}
        for row in rows:
            when = _as_date(row[at["Дата"]]) if "Дата" in at else None
            if when is None:
                continue
            kind = str(row[at["Тип события"]] or "")
            level, forecast = split_rating(row[at["Рейтинг"]])
            was_level, was_forecast = split_rating(row[at["Предыдущий рейтинг"]])
            # У выгрузки по эмиссиям прогноз стоит отдельными колонками
            # и пуст во всех строках — это сведение об источнике, а не о нас.
            if "Прогноз" in at and not forecast:
                forecast = str(row[at["Прогноз"]] or "").strip()
            found.append(
                Action(
                    when=when,
                    agency=_agency(kind),
                    scale=str(row[at["Шкала"]] or "").strip(),
                    name=str(row[at["Бумага"]] or "").strip(),
                    isin=str(row[at.get("ISIN", 0)] or "").strip()
                    if "ISIN" in at
                    else "",
                    reg_number=str(row[at.get("Рег. номер", 0)] or "").strip()
                    if "Рег. номер" in at
                    else "",
                    level=level,
                    forecast=forecast,
                    was_level=was_level,
                    was_forecast=was_forecast,
                    about=_about(kind),
                )
            )
        book.close()
    logger.info("календарь рейтинговых действий: записей %d", len(found))
    return tuple(found)


def scale_ids() -> dict[str, str]:
    """Наименование шкалы → её идентификатор у источника.

    Выгрузка называет шкалу словами, а справочник точек — идентификатором,
    и направление изменения считается по справочнику. Наименование
    сопоставляется дословно: шкалы «Оценка собственной кредитоспособности»
    у разных агентств зовутся одинаково, и сопоставление по вхождению
    смешало бы их.
    """
    from finlib.sources.cbonds_events import scale_points

    found: dict[str, str] = {}
    for item in scale_points().values():
        name = str(item.get("scale_name_rus") or "").strip()
        if name:
            found.setdefault(name, str(item.get("scale_id")))
    return found


def direction(action: Action, order: dict[tuple[str, str], int], scale: str) -> int:
    """Куда двинулся уровень: −1 понижение, +1 повышение, 0 — не сравнить.

    **Место точки берётся у справочника, а не у написания.** Сравнение идёт
    внутри одной шкалы: места точек разных шкал несопоставимы, и точка,
    которой в справочнике нет, в сравнение не идёт вовсе — молча считать
    её высшей значило бы спрятать неполноту справочника.
    """
    now = order.get((scale, action.level))
    was = order.get((scale, action.was_level))
    if now is None or was is None:
        return 0
    return -1 if now > was else (1 if now < was else 0)


# --- привязка наименования к ИНН ---------------------------------------------

# **Приведение наименования объявлено здесь и только для этого слоя.**
# Живого опознания наименований у нас нет — в маршруте эмитент опознаётся
# ИНН, — и звать разбор отчётности сюда было бы вторым путём к чужому ответу.
_SPACES = re.compile(r"\s+")
_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "'": '"'})


def prepared(name: str) -> str:
    """Наименование, приведённое к виду сравнения: регистр, кавычки, пробелы."""
    said = str(name or "").translate(_QUOTES).replace('"', " ")
    return _SPACES.sub(" ", said).strip().lower()


def by_name() -> tuple[dict[str, str], dict[str, int]]:
    """Словарь «наименование → ИНН» из карточек эмитентов и счётчики.

    Берутся оба наименования карточки — краткое и фирменное: выгрузка
    календаря печатает то одно, то другое. Наименование, притязающее
    на два ИНН, в словарь не идёт вовсе: сопоставление по нему было бы
    произвольным.
    """
    from finlib.scoring.routing_store import cards

    claims: dict[str, set[str]] = {}
    for inn, card in cards().items():
        for key in ("name_rus", "full_name_rus"):
            said = prepared(card.get(key) or "")
            if said:
                claims.setdefault(said, set()).add(inn)
    found = {name: next(iter(inns)) for name, inns in claims.items() if len(inns) == 1}
    counts = {
        "наименований": len(claims),
        "однозначных": len(found),
        "спорных": sum(1 for inns in claims.values() if len(inns) > 1),
    }
    return found, counts


def by_isin(actions: tuple[Action, ...]) -> tuple[dict[str, str], dict[str, int]]:
    """Второй словарь: наименование из выгрузки по эмиссиям → ИНН через ISIN.

    У выгрузки по эмиссиям есть и наименование, и ISIN, а ISIN у нас связан
    с эмитентом перечнем выпусков. Словарь нужен, чтобы **сверить** первый:
    два пути к одному ответу расходятся, и расхождения не видно, пока
    их не сравнить.
    """
    from finlib.sources.cbonds import bond_issuers
    from finlib.sources.cbonds_events import issues_of

    holder: dict[str, str] = {}
    for inn in bond_issuers():
        issues, known = issues_of(inn)
        if not known:
            continue
        for issue in issues:
            if issue.isin:
                holder[issue.isin] = inn
    claims: dict[str, set[str]] = {}
    for item in actions:
        if item.about != "emission" or not item.isin:
            continue
        inn = holder.get(item.isin)
        if inn is None:
            continue
        # У выпуска наименование составное: «АФК Система, 001P-01» — эмитент
        # стоит до запятой, и это устройство наименования выгрузки, а не
        # догадка: запятая отделяет бумагу от эмитента у всех строк.
        said = prepared(item.name.split(",")[0])
        if said:
            claims.setdefault(said, set()).add(inn)
    found = {name: next(iter(inns)) for name, inns in claims.items() if len(inns) == 1}
    counts = {
        "наименований": len(claims),
        "однозначных": len(found),
        "спорных": sum(1 for inns in claims.values() if len(inns) > 1),
        "выпусков с ISIN": len(holder),
    }
    return found, counts


def bound(
    actions: tuple[Action, ...],
) -> tuple[dict[str, str], Counter, list[str]]:
    """Наименование → ИНН по обоим словарям, счётчики и несопоставившееся.

    Словари сверяются между собой: совпали — привязка подтверждена дважды,
    разошлись — это называется и в словарь не идёт. Несопоставившееся
    возвращается перечнем: молчание о нём читалось бы как полнота.
    """
    cards_map, _ = by_name()
    isin_map, _ = by_isin(actions)
    counts: Counter = Counter()
    disagree: list[str] = []
    found: dict[str, str] = {}
    names = {prepared(item.name.split(",")[0]) for item in actions}
    for said in sorted(names):
        if not said:
            continue
        first, second = cards_map.get(said), isin_map.get(said)
        if first and second and first != second:
            counts["словари разошлись"] += 1
            disagree.append(said)
            continue
        inn = first or second
        if inn is None:
            counts["не привязано"] += 1
            continue
        found[said] = inn
        counts["привязано"] += 1
        if first and second:
            counts["подтверждено обоими"] += 1
        elif first:
            counts["только по карточкам"] += 1
        else:
            counts["только по ISIN"] += 1
    counts["наименований всего"] = len([item for item in names if item])
    return found, counts, disagree


# --- дата перехода в нынешнюю категорию --------------------------------------


@dataclass(frozen=True, slots=True)
class Transition:
    """С какого дня эмитент в нынешней категории и с какого уровня переведён."""

    since: date
    agency: str
    was_level: str
    # Понижение, повышение или движение внутри категории: «понижение с B-»
    # и «повышение с C» — разные сведения, и вывести одно из другого
    # по написанию уровней нельзя.
    direction: int


@lru_cache(maxsize=1)
def transitions() -> dict[tuple[str, str, str, str], Transition]:
    """Дата входа в категорию по ключу «ИНН, агентство, шкала, категория».

    **Шкала входит в ключ, потому что у одного агентства их две.** У Сегежи
    Эксперт РА держит ruB по национальной шкале и ruCCC по оценке собственной
    кредитоспособности; свёрнутые в одну череду, они дали бы дату действия
    по одной шкале при категории, взятой у другой. Наименование шкалы
    сравнивается дословно — тем же правилом, что и в `scale_ids`.

    **Снимок датирует подтверждение, а не переход.** У Кириллицы рейтинг
    ruC подтверждён позже, чем присвоен, и формулировка «Рейтинг ruC
    (Эксперт РА, 28.08.2026)» читается как событие того дня. Переход ищется
    назад по календарю: от свежайшего действия той же категории того же
    агентства, пока категория не сменится, — и первое из этой череды и есть
    вход.

    Ключ по категории, а не по уровню: категория — то, чем оперирует
    маршрут, а внутри неё уровень меняется, ничего не меняя в основании.

    Возвращается пусто, если календаря на диске нет: выгрузка ручная,
    и «файлов нет» — не «переходов не было».
    """
    from finlib.sources.cbonds_events import category_of

    if not CALENDAR.exists():
        logger.info("календаря рейтинговых действий на диске нет")
        return {}
    actions = read_actions()
    names, _, _ = bound(actions)
    order, scales = _order(), scale_ids()
    by_key: dict[tuple[str, str], list[Action]] = {}
    for item in actions:
        # Действия об эмиссии сюда не идут: они о выпуске, а основание
        # маршрута — о рейтинге эмитента, и смешивать объекты нельзя.
        if item.about != "issuer" or item.scale in NOT_CREDIT:
            continue
        inn = names.get(prepared(item.name.split(",")[0]))
        if inn is None:
            continue
        by_key.setdefault((inn, item.agency, item.scale), []).append(item)
    found: dict[tuple[str, str, str, str], Transition] = {}
    for (inn, agency, scale), items in by_key.items():
        items.sort(key=lambda entry: entry.when)
        latest = items[-1]
        # **Отзыв кончает череду, а не пропускается.** Пропущенный отзыв
        # оставил бы у эмитента категорию, которой у него больше нет:
        # у Кириллицы рейтинги отозваны 02.06.2026, а последним
        # не-отозванным действием стоит BBB прошлого года.
        if latest.withdrawn:
            continue
        category = category_of(latest.level)
        entry = latest
        for older in reversed(items[:-1]):
            if older.withdrawn or category_of(older.level) != category:
                break
            entry = older
        found[(inn, agency, scale, category)] = Transition(
            since=entry.when,
            agency=agency,
            was_level=entry.was_level,
            direction=direction(entry, order, scales.get(entry.scale, "")),
        )
    logger.info("переходов в нынешнюю категорию: %d", len(found))
    return found


@lru_cache(maxsize=1)
def _order() -> dict[tuple[str, str], int]:
    """Места точек шкал: справочник читается один раз на процесс."""
    from finlib.sources.cbonds_events import point_order

    return point_order()
