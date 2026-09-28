"""Данные Cbonds: клиент источника и чтение сохранённых ответов.

Cbonds отдаёт **нормализованную** отчётность по ИНН: пять строк отчёта
о прибылях, три сальдо потоков, полтора десятка строк баланса. Величины
основных форм у него и у нас сходятся — на пяти разобранных эмитентах
из 97 сверенных величин разошлись пять, и все пять различаются
определением показателя, а не ошибкой разбора.

**Ценность здесь в обратном: в том, чего Cbonds не различает.** Всё, что
не легло в его полтора десятка кодов, сворачивается в «прочие», и доля
свёрнутого у разных эмитентов отличается на два порядка — от 0,3 % валюты
баланса у ЛСР до 86 % у Автодора. Это и есть машинный признак того, где
разметка нужна, а где справочник Cbonds закрывает форму целиком.

**Клиент появился 18.09.2026** — прежде его не было намеренно, и ответы
выгружались руками. Решение принято не потому, что источник стал основным:
основными формами остаются PDF эмитента и ГИР БО. Он нужен, чтобы отбирать
эмитентов по признакам до выгрузки документа, а этого руками не сделать.

**Имя файла кэша называет запрос.** Хэша от параметров в имени нет: по
`msfo_real_7717151380.json` видно, что спрашивали, а по имени из хэша —
только то, что спрашивали что-то. Повторный запуск в сеть не идёт.

**Без логина и пароля клиент отказывается, а не возвращает пустоту.**
Пустой ответ означает «источник не знает эмитента», и подменять им
отсутствие доступа нельзя: отказ в доступе выглядел бы как отсутствие
данных у всех эмитентов сразу.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from finlib.config import settings
from finlib.sources import network

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")

# Ограничение частоты источник объявляет сам, в `meta` каждого ответа.
# До первого ответа мы его не знаем, поэтому держим заведомо щадящее
# значение: превысить объявленный предел хуже, чем сходить лишний раз реже.
DEFAULT_PER_MINUTE = 30


class CbondsUnavailableError(Exception):
    """Доступа к источнику нет: логин и пароль не заданы."""


class CbondsError(Exception):
    """Источник ответил не набором записей."""


class FilterIgnoredError(CbondsError):
    """Источник принял запрос, но отбора не сделал — вернул всё подряд."""


@dataclass(slots=True)
class Pace:
    """Счётчики обращений и объявленный источником предел частоты."""

    requested: int = 0
    from_cache: int = 0
    per_minute_max: int = DEFAULT_PER_MINUTE
    stamps: list[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Список отметок времени создаётся отдельно для каждого счётчика."""
        if self.stamps is None:
            self.stamps = []

    def wait(self) -> None:
        """Выжидает, пока обращение не уложится в объявленный предел."""
        now = time.monotonic()
        self.stamps = [item for item in self.stamps if now - item < 60.0]
        if len(self.stamps) >= self.per_minute_max:
            pause = 60.0 - (now - self.stamps[0])
            logger.info("предел Cbonds %d в минуту: пауза %.1f с", self.per_minute_max, pause)
            time.sleep(max(pause, 0.0))
        elif self.stamps and now - self.stamps[-1] < settings.http_min_interval_s:
            time.sleep(settings.http_min_interval_s - (now - self.stamps[-1]))
        self.stamps.append(time.monotonic())


pace = Pace()


def _cache_path(name: str) -> Path:
    """Путь сохранённого ответа по имени запроса."""
    return CACHE / f"{name}.json"


def report_method(report: str) -> str:
    """Метод источника по имени отчёта из справочника номенклатуры.

    Имя метода выводится из данных, а не подбирается: в номенклатуре поле
    `report` равно `report_msfo_real`, и метод называется
    `get_report_msfo_real`. Проверено перебором — `get_msfo_real`
    источник не знает вовсе.
    """
    return f"get_{report}"


def _verify_applied(filters: tuple[dict[str, Any], ...], items: list[dict]) -> None:
    """Проверяет, что отбор действительно выполнен источником.

    Неподдерживаемое поле Cbonds **молча пропускает** и отдаёт весь
    справочник: запрос по наименованию «Самолет» возвращает 323 925
    эмитентов, начиная с Ленинградской области. Ответ при этом выглядит
    исправным, и первая же запись сойдёт за найденного эмитента. Поэтому
    равенство сверяется по самим записям, а не по числу найденного.
    """
    for item in filters:
        if item.get("operator") != "eq":
            continue
        field, wanted = item["field"], str(item["value"])
        wrong = [row for row in items if str(row.get(field)) != wanted]
        if wrong:
            raise FilterIgnoredError(
                f"Cbonds вернул записи, не отвечающие отбору {field} = {wanted}: "
                f"поле источником не поддерживается и отбор пропущен молча. "
                f"Первая запись: {wrong[0].get(field)!r}"
            )


def _post(method: str, body: dict[str, Any]) -> httpx.Response:
    """Запрос к источнику с повтором при таймауте и ответе 5xx.

    **Сбой одного запроса — не отказ источника.** 25.09.2026 один таймаут
    на 661-м запросе снимка рейтингов оборвал весь прогон дня. Повторяется
    только то, что бывает временным: таймаут и ответ 5xx. Ответ 4xx —
    суждение источника о запросе, и повтор его не изменит. Каждая попытка
    проходит предел частоты и считается в расходе запросов: суточная норма
    её тоже считает. Попытки исчерпаны — таймаут поднимается как был,
    ответ 5xx возвращается вызывающему и становится `CbondsError`.
    """
    attempts = max(settings.cbonds_attempts, 1)
    for attempt in range(1, attempts + 1):
        pace.wait()
        pace.requested += 1
        try:
            # Нет сети у нас — не попытка источника: её пережидают минутами
            # и называют «нет сети» (`network.send`), а не тратят на неё
            # попытки, отведённые сбою на той стороне.
            response = network.send(
                lambda: httpx.post(
                    f"{settings.cbonds_base_url}/{method}/",
                    json=body,
                    timeout=settings.http_timeout_s,
                ),
                f"Cbonds {method}",
            )
        except httpx.TimeoutException as failure:
            if attempt == attempts:
                raise
            reason = f"таймаут {settings.http_timeout_s} с ({failure})"
        else:
            if response.status_code < 500 or attempt == attempts:
                return response
            reason = f"ответ {response.status_code}"
        pause = settings.cbonds_retry_pause_s * attempt
        logger.warning(
            "Cbonds %s: %s, попытка %d из %d, повтор через %.0f с",
            method,
            reason,
            attempt,
            attempts,
            pause,
        )
        time.sleep(pause)
    raise AssertionError("цикл попыток завершается возвратом или исключением")


def fetch(
    method: str,
    name: str,
    filters: tuple[dict[str, Any], ...] = (),
    limit: int = 100,
    refresh: bool = False,
) -> dict:
    """Ответ Cbonds по методу; сохранённый берётся с диска, сеть не дёргается.

    `name` — имя файла кэша, оно же называет запрос человеку. Отказ доступа
    и отсутствие эмитента различаются: первое поднимает исключение, второе
    даёт ответ с пустым `items`.
    """
    path = _cache_path(name)
    if path.exists() and not refresh:
        pace.from_cache += 1
        logger.info("Cbonds %s: ответ взят с диска (%s)", method, path.name)
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not settings.cbonds_ready:
        raise CbondsUnavailableError(
            f"ответа {name} на диске нет, а обратиться к Cbonds нечем: "
            "CBONDS_LOGIN и CBONDS_PASSWORD не заданы в .env"
        )
    items: list[dict] = []
    raw = ""
    offset = 0
    while True:
        body: dict[str, Any] = {
            "auth": {
                "login": settings.cbonds_login,
                "password": settings.cbonds_password,
            },
            "filters": list(filters),
            "quantity": {"limit": limit, "offset": offset},
        }
        logger.info("Cbonds %s: запрос (%s), смещение %d", method, name, offset)
        response = _post(method, body)
        raw = response.text
        if response.status_code != 200:
            raise CbondsError(f"Cbonds {method}: {response.status_code} — {raw[:200]}")
        found = json.loads(raw, parse_float=Decimal)
        if "items" not in found:
            raise CbondsError(f"Cbonds {method}: в ответе нет `items` — {raw[:200]}")
        declared = found.get("meta", {}).get("throttling_per_minute_max")
        if declared:
            pace.per_minute_max = int(declared)
        page = found.get("items", [])
        _verify_applied(filters, page)
        items.extend(page)
        total = int(found.get("total", len(items)))
        if len(items) >= total or not page:
            break
        offset += len(page)
    CACHE.mkdir(parents=True, exist_ok=True)
    if len(items) == len(found.get("items", [])):
        # Ответ уместился в одну страницу — сохраняется как пришёл.
        path.write_text(raw, encoding="utf-8")
    else:
        # Страниц было несколько, и единственного «сырого вида» у ответа нет.
        # Сохраняется собранный: иначе перечитанный с диска окажется короче
        # полученного из сети, и повторный запуск тихо потеряет записи.
        found["items"] = items
        found["count"] = len(items)
        path.write_text(
            json.dumps(found, ensure_ascii=False, default=str), encoding="utf-8"
        )
    logger.info(
        "Cbonds %s: записей %d из %s, сохранено в %s",
        method,
        len(items),
        found.get("total"),
        path.name,
    )
    return found


def emitent_by_inn(inn: str, refresh: bool = False) -> dict | None:
    """Карточка эмитента по ИНН; None — источник его не знает.

    Отбор только по ИНН: по наименованию Cbonds не отбирает вовсе —
    поле принимается и молча игнорируется, см. `_verify_applied`.
    """
    found = fetch(
        "get_emitents",
        f"emitent_{inn}",
        filters=({"field": "emitent_inn", "operator": "eq", "value": inn},),
        refresh=refresh,
    )
    items = found.get("items", [])
    return items[0] if items else None


def msfo_real(inn: str, refresh: bool = False) -> list[dict]:
    """Нормализованная отчётность по МСФО по ИНН."""
    found = fetch(
        report_method("report_msfo_real"),
        f"msfo_real_{inn}",
        filters=({"field": "emitent_inn", "operator": "eq", "value": inn},),
        refresh=refresh,
    )
    return found.get("items", [])


def deliveries_of(report: object, inn: str, refresh: bool = False) -> list[dict]:
    """Строки вида отчёта по ИНН, сведённые по отчётной дате.

    **Комплект РСБУ приходит тремя доставками.** Баланс, отчёт о финансовых
    результатах и отчёт о движении денежных средств — разные методы источника,
    и одна строка ответа несёт одну форму. Комплект же — это три формы одного
    периода, поэтому доставки сводятся по дате.

    **Отчёт о движении денежных средств отбирается по идентификатору
    эмитента**: поля ИНН у него нет вовсе, а неподдерживаемое поле отбора
    Cbonds пропускает молча и отдаёт весь справочник. Идентификатор берётся
    из ответа предыдущей доставки; не нашёлся — доставка называется
    пропущенной, а не молчит.

    **Столкновение полей при сведении не проглатывается.** Одно поле
    с разными величинами в двух ответах — событие: оно попадает в комплект
    вместе с доставками, из которых он собран.
    """
    merged: dict[str, dict] = {}
    emitent_id: str | None = None
    skipped: list[str] = []
    for delivery in report.deliveries:  # type: ignore[attr-defined]
        value = inn if delivery.filter_field == "emitent_inn" else emitent_id
        if not value:
            skipped.append(delivery.method)
            logger.warning(
                "Cbonds %s: отбор по %s нечем — идентификатор эмитента неизвестен",
                delivery.method,
                delivery.filter_field,
            )
            continue
        found = fetch(
            delivery.method,
            f"{delivery.cache}_{value}",
            filters=({"field": delivery.filter_field, "operator": "eq", "value": value},),
            refresh=refresh,
        )
        for row in found.get("items", []):
            emitent_id = emitent_id or str(row.get("emitent_id") or "") or None
            moment = str(row.get("date") or "")
            if not moment:
                continue
            into = merged.setdefault(
                moment,
                {
                    "date": moment,
                    "emitent_inn": inn,
                    "emitent_name_rus": row.get("emitent_name_rus"),
                    "_deliveries": [],
                    "_collisions": [],
                },
            )
            into["_deliveries"].append(delivery.method)
            # **Комплект появился у агрегатора, когда пришла последняя
            # из его форм**: дата появления — поздняя из трёх, а не первая
            # попавшаяся. Она служит днём видимости промежуточного комплекта
            # в пересчёте истории (`standards.yaml`, `interim_visible_from`).
            created = str(row.get("created_at") or "")
            if created and created > str(into.get("_created_at") or ""):
                into["_created_at"] = created
            for key, item in row.items():
                if item in (None, ""):
                    continue
                if key in into and key.startswith("ln") and into[key] != item:
                    into["_collisions"].append(
                        f"{key}: {into[key]} против {item} ({delivery.method})"
                    )
                    continue
                if key.startswith("ln") or key not in into:
                    into[key] = item
    if skipped:
        for row in merged.values():
            row["_deliveries"].append(f"пропущено: {', '.join(skipped)}")
    return [merged[moment] for moment in sorted(merged)]


def msfo_universe(refresh: bool = False) -> list[dict]:
    """Все записи нормализованной отчётности по МСФО, какие есть у источника.

    Отбирать эмитентов по наименованию источник не умеет, поэтому перечень
    забирается целиком и разбирается у нас. Он невелик — около шести тысяч
    записей, две страницы, — и это же перечень кандидатов набора задачи 29.
    """
    found = fetch(
        report_method("report_msfo_real"),
        "msfo_real_universe",
        limit=5000,
        refresh=refresh,
    )
    return found.get("items", [])


# Отбор универсума облигаций: выпуски в обращении у эмитентов, зарегистрированных
# в России. Оба условия — отбор источника, а не наш: страна названа полем
# карточки выпуска, статус — справочным значением 192 («В обращении»).
OUTSTANDING = (
    {"field": "emitent_country", "operator": "eq", "value": "1"},
    {"field": "status_id", "operator": "eq", "value": "192"},
)


def outstanding_universe(refresh: bool = False) -> list[dict]:
    """Выпуски в обращении российских эмитентов — универсум части первой.

    **Универсум маршрута задаётся долгом, а не отчётностью.** Прежде перечень
    эмитентов собирался из доставок МСФО (`msfo_universe`), и слепое пятно было
    шире списка: эмитента, раскрывающего только РСБУ, в нём не было вовсе,
    и отсутствие его не было видно даже как отсутствие. Маршрут спрашивает,
    нужен ли человек, а нужен он там, где есть долг в обращении.

    Перечень забирается целиком — 5 354 выпуска, две страницы, — и разбирается
    у нас: отбирать эмитентов по признакам источник не умеет.
    """
    found = fetch(
        "get_emissions",
        "emissions_ru_outstanding",
        filters=OUTSTANDING,
        limit=5000,
        refresh=refresh,
    )
    return found.get("items", [])


def bond_issuers(refresh: bool = False) -> dict[str, str]:
    """ИНН и наименование эмитентов с выпусками в обращении.

    Ключ — ИНН, потому что ИНН и есть ключ организации у нас. Выпуск без ИНН
    эмитента в перечень не входит: привязать его не к чему, и молча приписать
    другому эмитенту было бы хуже потери.
    """
    found: dict[str, str] = {}
    for row in outstanding_universe(refresh):
        inn = str(row.get("emitent_inn") or "").strip()
        if not inn:
            continue
        found.setdefault(inn, str(row.get("emitent_name_rus") or inn).strip())
    return found


# Коды «прочего» по разделам баланса. Именно они и означают «Cbonds этой
# статьи не различает»: всё, что не легло в его именованные коды, здесь.
OTHER_CODES: dict[str, str] = {
    "ln5": "current_assets",
    "ln9": "non_current_assets",
    "ln15": "current_liabilities",
    "ln18": "non_current_liabilities",
}

# Код валюты баланса: доля «прочего» считается от него.
TOTAL_ASSETS = "ln11"


@dataclass(frozen=True, slots=True)
class OtherShare:
    """Сколько величины раздела Cbonds не различает."""

    section: str
    amount: Decimal
    share_of_assets: Decimal

    def describe(self) -> str:
        """Строка для отчёта."""
        return f"{self.section}: {self.amount} ({self.share_of_assets:.1%} активов)"


def cached(inn: str) -> list[dict]:
    """Сохранённые записи эмитента; пусто — ответа на диске нет.

    Отсутствие файла и пустой ответ — разные вещи, и путать их нельзя:
    первое означает, что мы не спрашивали, второе — что эмитента нет
    у источника. Пустой список возвращается в обоих случаях, но в журнал
    идут разные сообщения.
    """
    path = CACHE / f"msfo_real_{inn}.json"
    if not path.exists():
        logger.info("ответа Cbonds по ИНН %s на диске нет: источник не спрашивали", inn)
        return []
    found = json.loads(path.read_text(), parse_float=Decimal)
    items = found.get("items", [])
    if not items:
        logger.info("Cbonds не знает ИНН %s: ответ пуст", inn)
    return items


def _value(item: dict, code: str) -> Decimal | None:
    """Величина по коду; None — поле пусто либо нечисловое."""
    raw = item.get(code)
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None


def other_shares(inn: str, report_date: date) -> dict[str, OtherShare]:
    """Доля «прочего» Cbonds по разделам баланса на эту дату.

    Пусто — данных нет: ни записи за период, ни валюты баланса. Ноль
    и отсутствие здесь снова разные вещи, и ноль не подставляется.
    """
    wanted = report_date.isoformat()
    item = next((row for row in cached(inn) if row.get("date") == wanted), None)
    if item is None:
        return {}
    assets = _value(item, TOTAL_ASSETS)
    if assets is None or assets == 0:
        return {}
    found: dict[str, OtherShare] = {}
    for code, section in OTHER_CODES.items():
        amount = _value(item, code)
        if amount is None:
            continue
        found[section] = OtherShare(section, amount, abs(amount) / abs(assets))
    return found
