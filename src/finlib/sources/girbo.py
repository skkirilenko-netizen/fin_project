"""Клиент ГИР БО: поиск организации по ИНН и получение опубликованной отчётности.

Структура ответа выверена на реальных пробах, сохранённых в data/raw/probe:
формы приходят отдельными блоками, значения — в атрибутах вида current1600,
previous1600, beforePrevious1600. Наименований строк в ответе нет.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finlib.normalize.lines import ReportingType
from finlib.sources.cache import CachedResponse, RawCache
from finlib.sources.errors import (
    CreditOrganizationError,
    OrganizationNotFoundError,
    ReportsNotPublishedError,
    SourceError,
)
from finlib.sources.http import PoliteClient
from finlib.utils import json_loads_decimal, to_decimal

logger = logging.getLogger(__name__)

SOURCE_NAME = "girbo"

SEARCH_PATH = "/advanced-search/organizations/search"
BFO_PATH = "/nbo/organizations/{org_id}/bfo/"

# Блоки форм, которые нас интересуют. Отчёт об изменениях капитала (0710004)
# устроен матрицей (authorized3100, additional3100, ...), в справочник строк
# не заложен и не разбирается.
FORM_BLOCKS: tuple[str, ...] = ("balance", "financialResult", "fundsMovement")

# Сдвиг периода в годах назад от отчётного года.
PERIOD_OFFSETS: dict[str, int] = {"current": 0, "previous": 1, "beforePrevious": 2}

# Глубина истории у форм разная: третий период есть только в балансе.
# Метрики задачи 6 обязаны считаться с этим, а не ожидать одинаковой глубины.
FORM_PERIOD_DEPTH: dict[str, int] = {"0710001": 3, "0710002": 2, "0710005": 2}

# Код налогового документа определяет набор строк отчётности.
KND_TO_REPORTING_TYPE: dict[str, ReportingType] = {
    "0710099": ReportingType.FULL,
    "0710096": ReportingType.SIMPLIFIED,
}

# В ответе ГИР БО поля единицы измерения нет; значения приходят в тысячах рублей.
ASSUMED_UNIT_CODE = "384"
ASSUMED_UNIT_MULTIPLIER = Decimal(1)

_VALUE_KEY = re.compile(r"^(current|previous|beforePrevious)(\d{4,6})$")
_HIGHLIGHT = re.compile(r"</?strong>")


def strip_highlight(text: str | None) -> str | None:
    """Снимает подсветку совпадений, которой источник оборачивает найденное."""
    return None if text is None else _HIGHLIGHT.sub("", text)


@dataclass(frozen=True, slots=True)
class Organization:
    """Реквизиты организации из карточки источника."""

    inn: str
    girbo_id: int
    short_name: str | None = None
    full_name: str | None = None
    ogrn: str | None = None
    kpp: str | None = None
    okpo: str | None = None
    okved: str | None = None
    okopf: str | None = None
    region: str | None = None


@dataclass(frozen=True, slots=True)
class FormData:
    """Значения одной формы по периодам: дата отчёта -> код строки -> значение."""

    form_code: str
    values: dict[date, dict[str, Decimal | None]]

    @property
    def report_dates(self) -> tuple[date, ...]:
        """Периоды, за которые форма содержит данные, от свежего к старому."""
        return tuple(sorted(self.values, reverse=True))

    @property
    def depth(self) -> int:
        """Сколько периодов пришло по этой форме."""
        return len(self.values)


@dataclass(frozen=True, slots=True)
class ReportSet:
    """Один опубликованный комплект отчётности: одна организация, один год, одна корректировка."""

    inn: str
    girbo_bfo_id: int
    report_year: int
    report_date: date
    knd: str
    reporting_type: ReportingType
    correction_version: int
    is_actual: bool
    forms: dict[str, FormData] = field(default_factory=dict)

    @property
    def form_codes(self) -> tuple[str, ...]:
        """Коды форм, пришедших в комплекте."""
        return tuple(sorted(self.forms))

    def report_dates(self, form_code: str) -> tuple[date, ...]:
        """Периоды, доступные по конкретной форме; у баланса их больше."""
        form = self.forms.get(form_code)
        return form.report_dates if form is not None else ()


def _period_date(report_year: int, prefix: str) -> date:
    """Дата отчёта для периода: 31 декабря соответствующего года."""
    return date(report_year - PERIOD_OFFSETS[prefix], 12, 31)


def parse_form(block: dict[str, Any], report_year: int) -> FormData | None:
    """Разбирает блок формы в значения по периодам; None, если блока нет."""
    if not block:
        return None
    form_code = block.get("okud")
    if not form_code:
        raise SourceError("в блоке формы отсутствует код ОКУД, разбор вслепую запрещён")
    values: dict[date, dict[str, Decimal | None]] = {}
    for key, raw in block.items():
        match = _VALUE_KEY.match(key)
        if match is None:
            continue  # id, okud и ссылки на пояснения expl* значениями не являются
        prefix, line_code = match.group(1), match.group(2)
        period = _period_date(report_year, prefix)
        values.setdefault(period, {})[line_code] = to_decimal(raw)
    return FormData(form_code=str(form_code), values=values)


def parse_organization(payload: dict[str, Any]) -> Organization:
    """Собирает реквизиты организации из строки поиска или карточки."""
    okved = payload.get("okved2")
    okopf = payload.get("okopf")
    return Organization(
        inn=str(strip_highlight(str(payload["inn"]))),
        girbo_id=int(payload["id"]),
        short_name=strip_highlight(payload.get("shortName")),
        full_name=strip_highlight(payload.get("fullName")),
        ogrn=strip_highlight(payload.get("ogrn")),
        kpp=payload.get("kpp"),
        okpo=payload.get("okpo"),
        okved=okved.get("id") if isinstance(okved, dict) else okved,
        okopf=okopf.get("name") if isinstance(okopf, dict) else okopf,
        region=strip_highlight(payload.get("region")),
    )


def parse_report_sets(payload: list[dict[str, Any]], inn: str) -> list[ReportSet]:
    """Разбирает список комплектов отчётности организации."""
    result: list[ReportSet] = []
    for entry in payload:
        report_year = int(entry["period"])
        actual_correction = int(entry.get("actualCorrectionNumber") or 0)
        for type_correction in entry.get("typeCorrections") or []:
            correction = type_correction.get("correction") or {}
            knd = str(correction.get("knd") or entry.get("knd") or "")
            reporting_type = KND_TO_REPORTING_TYPE.get(knd)
            if reporting_type is None:
                raise SourceError(
                    f"неизвестный КНД {knd!r} у ИНН {inn} за {report_year} год: "
                    "набор строк отчётности определить нельзя"
                )
            version = int(correction.get("correctionVersion") or 0)
            forms: dict[str, FormData] = {}
            for block_name in FORM_BLOCKS:
                form = parse_form(correction.get(block_name) or {}, report_year)
                if form is not None:
                    forms[form.form_code] = form
            result.append(
                ReportSet(
                    inn=inn,
                    girbo_bfo_id=int(entry["id"]),
                    report_year=report_year,
                    report_date=date(report_year, 12, 31),
                    knd=knd,
                    reporting_type=reporting_type,
                    correction_version=version,
                    is_actual=version == actual_correction,
                    forms=forms,
                )
            )
    return sorted(result, key=lambda r: (r.report_year, r.correction_version), reverse=True)


def is_credit_organization(payload: list[dict[str, Any]]) -> bool:
    """Признак кредитной организации: её отчётность сдаётся в Банк России."""
    return any(bool(entry.get("isCb")) for entry in payload)


class GirboSource:
    """Источник ГИР БО: поиск по ИНН, получение комплектов, кэш на диске."""

    def __init__(
        self,
        client: PoliteClient | None = None,
        cache: RawCache | None = None,
        *,
        journal: bool = True,
    ) -> None:
        self._client = client if client is not None else PoliteClient()
        self._cache = cache if cache is not None else RawCache(SOURCE_NAME)
        self._journal = journal

    def close(self) -> None:
        """Закрывает соединение с источником."""
        self._client.close()

    def __enter__(self) -> "GirboSource":
        """Вход в контекст."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Закрывает соединение."""
        self.close()

    def _get(
        self, key: str, path: str, params: dict[str, Any] | None, *, force_refresh: bool
    ) -> CachedResponse:
        """Берёт ответ из кэша, а при промахе или force_refresh — из сети."""
        if not force_refresh:
            cached = self._cache.read(key)
            if cached is not None:
                logger.info("ответ взят из кэша: %s", cached.path)
                return cached
        content = self._client.get_bytes(path, params)
        return self._cache.write(key, content, url=f"{self._client.base_url}{path}")

    def find_organization(self, inn: str, *, force_refresh: bool = False) -> Organization:
        """Находит организацию по ИНН; точное совпадение обязательно."""
        response = self._get(
            f"search_{inn}", SEARCH_PATH, {"query": inn, "page": 0}, force_refresh=force_refresh
        )
        payload = json_loads_decimal(response.content)
        for row in payload.get("content") or []:
            if strip_highlight(str(row.get("inn"))) == inn:
                return parse_organization(row)
        raise OrganizationNotFoundError(inn)

    def fetch_report_sets(
        self, inn: str, *, force_refresh: bool = False
    ) -> tuple[Organization, list[ReportSet]]:
        """Отдаёт реквизиты и все опубликованные комплекты отчётности организации."""
        organization = self.find_organization(inn, force_refresh=force_refresh)
        response = self._get(
            f"bfo_{inn}",
            BFO_PATH.format(org_id=organization.girbo_id),
            None,
            force_refresh=force_refresh,
        )
        payload = json_loads_decimal(response.content)
        if not payload:
            raise ReportsNotPublishedError(inn)
        if is_credit_organization(payload):
            self._reject_credit_organization(inn)
        return organization, parse_report_sets(payload, inn)

    def _reject_credit_organization(self, inn: str) -> None:
        """Фиксирует отбраковку кредитной организации в журнале качества и прерывает работу."""
        if self._journal:
            from finlib.quality.codes import CheckCode, CheckStatus
            from finlib.quality.journal import log_check

            log_check(
                inn=inn,
                check_code=CheckCode.CREDIT_ORGANIZATION,
                status=CheckStatus.FAIL,
                message="Кредитная организация: отчётность сдаётся в Банк России по формам 0409",
                details={"source": SOURCE_NAME, "flag": "isCb"},
            )
        raise CreditOrganizationError(inn)
