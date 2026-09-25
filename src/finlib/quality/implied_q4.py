"""Сверка подразумеваемого IV квартала: годовой минус девять месяцев.

**IV квартал в отчётности не печатается, но следует из неё** (фаза 5-бис,
решение владельца 25.09.2026): годовая величина потока минус девять месяцев
того же года. Доля IV квартала за пределами распределения говорит о паре
комплектов, а не об эмитенте: годовой пересмотрен, девять месяцев собраны
иначе либо величина одного из них не та. Сверка — запись журнала уровня
warning; в маршрут она не идёт и карантина не ставит.

**Отсечки из распределения, двусторонние** (`interim.yaml`, `implied_q4`):
IV квартал бывает и больше половины года, и убыточным при прибыльном годе —
судить по знаку или по «разумной доле» значило бы назначить порог.

**Записи — состояние, а не событие**: при каждой загрузке эмитента они
переписываются, и рядом пишется, сколько пар сверено и скольким мерить было
нечем. Ноль аномалий при неизвестном числе сверок не значит ничего.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, execute, fetch_all
from finlib.quality.codes import CheckCode, CheckStatus, Severity
from finlib.quality.journal import CheckRecord, log_records
from finlib.scoring.interim import Band, load_interim
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Величины строк на годовую дату и на 30 сентября, вне карантина; у периода
# одна величина — с предпочтением первоисточника, как во всякой выборке.
_VALUES = """
SELECT DISTINCT ON (f.report_date, f.line_code)
       f.report_date, f.line_code, f.value, f.src_file_id
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s
  AND f.line_code = ANY(%(codes)s) AND f.value IS NOT NULL
  AND s.is_actual AND s.status <> 'quarantine'
  AND to_char(f.report_date, 'MM-DD') IN ('12-31', '09-30')
ORDER BY f.report_date, f.line_code, source_rank(s.source)
"""

_FORGET = """
DELETE FROM dq_log
WHERE inn = %(inn)s AND check_code = %(code)s AND details->>'standard' = %(standard)s
"""


@dataclass(frozen=True, slots=True)
class Quarter:
    """Подразумеваемый IV квартал одной строки одного года."""

    line_code: str
    year_end: date
    annual: Decimal
    nine_months: Decimal
    src_file_id: int

    @property
    def value(self) -> Decimal:
        """IV квартал: годовой минус девять месяцев."""
        return self.annual - self.nine_months

    @property
    def share(self) -> Decimal | None:
        """Доля IV квартала в годовой; None — годовая неположительна, мерить нечем."""
        if self.annual <= 0:
            return None
        return self.value / self.annual


def quarters(
    rows: list[dict], codes: set[str]
) -> list[Quarter]:
    """Пары «год — девять месяцев того же года» по строкам."""
    by_key = {(row["report_date"], row["line_code"]): row for row in rows}
    found: list[Quarter] = []
    for (moment, code), row in by_key.items():
        if code not in codes or (moment.month, moment.day) != (12, 31):
            continue
        nine = by_key.get((date(moment.year, 9, 30), code))
        if nine is None:
            continue
        found.append(
            Quarter(
                code,
                moment,
                Decimal(row["value"]),
                Decimal(nine["value"]),
                int(row["src_file_id"]),
            )
        )
    return found


def outside(item: Quarter, band: Band) -> bool:
    """Лежит ли доля IV квартала за отсечками; без доли — не лежит."""
    share = item.share
    return share is not None and (share < band.low or share > band.high)


def check_implied_q4(inn: str, standard: Standard, conn: PgConnection) -> tuple[int, int]:
    """Сверяет IV квартал эмитента; возвращает число сверенных пар и аномалий."""
    bands = load_interim().implied_q4.lines.get(standard.value, {})
    if not bands:
        return 0, 0
    rows = fetch_all(
        _VALUES,
        {"inn": inn, "standard": standard.value, "codes": sorted(bands)},
        conn=conn,
    )
    found = quarters(rows, set(bands))
    execute(
        _FORGET,
        {"inn": inn, "code": CheckCode.IMPLIED_Q4_ANOMALY.value, "standard": standard.value},
        conn=conn,
    )
    measured = [item for item in found if item.share is not None]
    records: list[CheckRecord] = []
    for item in measured:
        band = bands[item.line_code]
        if not outside(item, band):
            continue
        records.append(
            CheckRecord(
                inn=inn,
                check_code=CheckCode.IMPLIED_Q4_ANOMALY,
                status=CheckStatus.WARNING,
                src_file_id=item.src_file_id,
                report_date=item.year_end,
                line_code=item.line_code,
                message=(
                    f"IV квартал {item.year_end:%Y} года по строке {item.line_code}: "
                    f"{item.value} при годовой {item.annual} — доля {item.share:.3f} "
                    f"вне отсечек {band.low}…{band.high}"
                ),
                details={
                    "standard": standard.value,
                    "annual": str(item.annual),
                    "nine_months": str(item.nine_months),
                    "share": str(item.share),
                    "low": str(band.low),
                    "high": str(band.high),
                },
            )
        )
    records.append(
        CheckRecord(
            inn=inn,
            check_code=CheckCode.IMPLIED_Q4_ANOMALY,
            status=CheckStatus.PASS if not records else CheckStatus.INFO,
            severity=Severity.INFO,
            message=(
                f"IV квартал сверен у пар {len(measured)}, аномалий {len(records)}, "
                f"мерить нечем (годовая неположительна) {len(found) - len(measured)}"
            ),
            details={"standard": standard.value, "summary": True},
        )
    )
    log_records(records, conn=conn)
    return len(measured), len(records) - 1
