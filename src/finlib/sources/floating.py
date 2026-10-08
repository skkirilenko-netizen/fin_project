"""Неустановленные купоны: оценка по текущей ставке либо граница снизу. **Только диск.**

Источник отдаёт пустой `cupon_sum` у купона, ставка которого ещё
не определена, и прежде разбор графика читал пустое нулём — платежи года
занижены ровно на эти купоны (`methodology/routing.yaml`,
`refinancing.floating_coupons`, решения владельца 01.10.2026).

Правило, по порядку:

- купон текущего периода зафиксирован в графике — берётся он, оценки нет
  (уточнение а); лаг фиксации отдельно не моделируется;
- ставка купона прописана в тексте условий для его номера («1–12 купоны —
  18 % годовых») — это данные, а не оценка (решение владельца 02.10.2026,
  3.1), печать «по условиям выпуска»;
- флоатер, индекс которого объявлен методикой, и формула условий —
  «индекс + спред» (уточнение б) — оценка: ставка индекса на дату расчёта
  плюс спред;
- пол и потолок — «не менее», «не более», MAX и MIN с числом — вычисляются
  при текущем значении индекса (решение владельца 02.10.2026, 3.2): не
  связывают — индекс + спред, связывают — пол либо потолок; внутри MAX/MIN
  формула линейная по индексу (R/2, 25,90 % − R);
- индекса нет (ИПЦ, иностранный), а пол есть — граница снизу по полу;
- нефлоатер, у которого купон после оферты либо устанавливается
  эмитентом, — оценка по последнему известному купону с пометкой;
- всё прочее — неразобранная формула: купон в сумму не входит, основание
  помечается неполным («не менее»).

Ставки — с диска: ключевая и RUONIA — сохранённые страницы Банка России
(`data/raw/cbr/`), КБД — опубликованные точки кривой биржи; у ежедневных
рядов ставка старше объявленного числа дней ставкой на дату не считается.
Сумма купона на одну бумагу — ставка × непогашенный номинал × дни периода
/ 365 (база начисления выпуска не учитывается — допущение, названо).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path

from finlib.config import settings
from finlib.sources.cbonds_flows import Payment, Schedule, schedule_of

CBR = settings.raw_dir / "cbr"
MOEX = settings.raw_dir / "moex"
CBONDS = settings.raw_dir / "cbonds"

ESTIMATE = "estimate"
TERMS = "terms"
FLOOR = "floor"
LAST_KNOWN = "last_known"
LOWER = "lower_bound"

# Признаки формулы, которая не «индекс + спред» (уточнение б владельца):
# множитель, делитель, потолок, минимум из нескольких величин, второй индекс.
_NOT_ADDITIVE = re.compile(
    r"(×|\*\s*\d|\d\s*\*|умнож|коэффициент|/\s*2\b|делен|\b(?:min|max)\s*\(|"
    r"минимальн\w* из|не более|не выше|максимальный размер|ИПЦ|инфляц)",
    re.IGNORECASE,
)
_CAP = re.compile(r"(?:не более|не выше)\s*(\d+(?:[.,]\d+)?)\s*%", re.IGNORECASE)
# MAX/MIN двух величин: формула по индексу и число (решение владельца 3.2).
_EXTREMUM = re.compile(r"\b(max|min)\s*\(([^;()]*);([^;()]*)\)", re.IGNORECASE)
# Ставка, прописанная для диапазона купонов: «1-12 купоны - 18% годовых»,
# «Купон 1: 25%». После числа — «годовых», знак препинания или конец:
# «… + 1%» формулой, а не ставкой, сюда не попадает.
_FIXED = re.compile(
    r"(?:(?<![\d.,])(\d+)\s*(?:[-–]\s*(\d+))?\s*(?:-?[йо]\w?\s*)?купон\w*"
    r"|купон\w*\s*(\d+)(?:\s*[-–]\s*(\d+))?)"
    r"\s*[-–:]?\s*(\d+(?:[.,]\d+)?)\s*%\s*(?:годовых)?(?=\s*(?:[,.;]|до\b|$))",
    re.IGNORECASE,
)
_FLOOR = re.compile(
    r"(?:не менее|не ниже|минимальн\w+ (?:размер|значение|ставк)\w*[^.;]{0,40}?)\s*"
    r"(\d+(?:[.,]\d+)?)\s*%",
    re.IGNORECASE,
)
_FLOOR_MAX = re.compile(r"max\s*\([^;)]*;\s*(\d+(?:[.,]\d+)?)\s*%\s*\)", re.IGNORECASE)
_AFTER_OFFER = re.compile(
    r"(оферт|определя\w* эмитент|устанавлива\w+ эмитент|эмитент\w* (?:устанавлива|определя)|"
    r"в соответствии с эмиссионными документами|решени\w* эмитента)",
    re.IGNORECASE,
)
_TERM = re.compile(r"(\d+)Y")


@dataclass(frozen=True, slots=True)
class Terms:
    """Как оценивается неустановленный купон выпуска."""

    kind: str
    index: str = ""
    spread: Decimal = Decimal(0)
    floor: Decimal | None = None
    term: Decimal | None = None
    last_coupon: Decimal | None = None
    # Ставка = `factor` × индекс + `spread`, затем пол и потолок. Множитель
    # не единица — только внутри MAX/MIN (R/2, 25,90 % − R).
    factor: Decimal = Decimal(1)
    cap: Decimal | None = None
    # Ставки по условиям выпуска для диапазонов номеров купонов.
    fixed: tuple[tuple[int, int, Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class Estimate:
    """Оценка купона на одну бумагу и её основание; `amount` None — оценки нет."""

    amount: Decimal | None
    kind: str
    basis: str

    @property
    def data(self) -> bool:
        """Ставка взята из условий выпуска: это данные, а не оценка."""
        return self.kind == TERMS


def _decimal(value: object) -> Decimal | None:
    """Число записи источника; пустое и мусор — None."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except ArithmeticError:
        return None


def index_of(name: str, rules: dict) -> str:
    """Код индекса методики по наименованию источника; пусто — индекс не объявлен."""
    for code, rule in rules["indices"].items():
        if name in rule["names"]:
            return code
    return ""


def fixed_rates(text: str) -> tuple[tuple[int, int, Decimal], ...]:
    """Ставки, прописанные в тексте условий для диапазонов купонов: (с, по, ставка %)."""
    found: list[tuple[int, int, Decimal]] = []
    for match in _FIXED.finditer(text):
        first = match.group(1) or match.group(3)
        last = match.group(2) or match.group(4) or first
        rate = _decimal(match.group(5))
        if first is None or rate is None:
            continue
        found.append((int(first), int(last), rate))
    return tuple(found)


def _linear(expression: str) -> tuple[Decimal, Decimal] | None:
    """Линейная формула по одному индексу: (множитель, слагаемое %); None — не разобрана.

    Разбирается то, что стоит внутри MAX/MIN: «Cri/2», «25.90% - R»,
    «R + 1,8%». Число со знаком % — слагаемое в процентах годовых, без
    знака — множитель или делитель; переменная — любое слово, и она одна.
    """
    tokens = re.findall(r"\d+(?:[.,]\d+)?\s*%?|[^\W\d_][\w-]*|[+\-*/]", expression)
    if not tokens or "".join(tokens).replace(" ", "") != re.sub(r"\s+", "", expression):
        return None
    factor = constant = Decimal(0)
    names: set[str] = set()
    sign = Decimal(1)
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token in "+-":
            sign = Decimal(1) if token == "+" else Decimal(-1)
            position += 1
            continue
        # Слагаемое: величина и цепочка «* число» либо «/ число».
        coefficient = Decimal(1)
        variable = False
        if token.rstrip().endswith("%"):
            value = _decimal(token.rstrip(" %"))
        elif re.fullmatch(r"\d+(?:[.,]\d+)?", token):
            value = _decimal(token)
            if value is None:
                return None
            coefficient = value
            value = None
        else:
            names.add(token)
            variable = True
            value = None
        position += 1
        while position + 1 < len(tokens) and tokens[position] in "*/":
            number = _decimal(tokens[position + 1].rstrip(" %"))
            if number is None or number == 0:
                return None
            coefficient = coefficient * number if tokens[position] == "*" else coefficient / number
            position += 2
        if variable:
            factor += sign * coefficient
        elif value is not None:
            constant += sign * value * coefficient
        else:
            return None
        sign = Decimal(1)
    if len(names) != 1 or factor == 0:
        return None
    return factor, constant


def _extremum(text: str) -> tuple[Decimal, Decimal, Decimal | None, Decimal | None] | None:
    """MAX/MIN «формула; число»: множитель, слагаемое, пол, потолок; None — не разобрано."""
    # «MIN(А;Б) — выбор меньшего» — определение, а не формула: без чисел.
    found = {
        (m.group(1).lower(), m.group(2).strip(), m.group(3).strip())
        for m in _EXTREMUM.finditer(text)
        if re.search(r"\d", m.group(2) + m.group(3))
    }
    if len(found) != 1:
        return None
    which, left, right = found.pop()
    bound = None
    for constant, formula in ((right, left), (left, right)):
        number = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*%", constant)
        if number:
            bound, expression = _decimal(number.group(1)), formula
            break
    if bound is None:
        return None
    line = _linear(expression)
    if line is None:
        return None
    floor, cap = (bound, None) if which == "max" else (None, bound)
    return line[0], line[1], floor, cap


def _single(pattern: re.Pattern[str], text: str) -> tuple[bool, Decimal | None]:
    """Одно значение границы в тексте: (разобрано, величина); разные значения — не разобрано."""
    values = {_decimal(item) for item in pattern.findall(text)}
    values.discard(None)
    if len(values) > 1:
        return False, None
    return True, (values.pop() if values else None)


def terms_of(record: dict, rules: dict, last_coupon: Decimal | None) -> Terms:
    """Правило оценки по записи выпуска и последнему известному купону (ставке, %)."""
    text = re.sub(r"<[^>]+>|&\w+;", " ", str(record.get("cupon_rus") or ""))
    fixed = fixed_rates(text)
    floating = str(record.get("floating_rate") or "") == "1"
    if not floating:
        if _AFTER_OFFER.search(text) and last_coupon is not None:
            return Terms(LAST_KNOWN, last_coupon=last_coupon, fixed=fixed)
        return Terms(LOWER, fixed=fixed)
    name = str(record.get("reference_rate_name_rus") or "")
    code = index_of(name, rules)
    floor_ok, floor = _single(_FLOOR, text)
    if floor is None:
        floor_ok, floor = _single(_FLOOR_MAX, text)
    cap_ok, cap = _single(_CAP, text)
    if not code:
        # Индекса на диске нет (ИПЦ, иностранный): пол — граница снизу числом.
        if floor is not None and floor_ok:
            return Terms(FLOOR, index=name, floor=floor, fixed=fixed)
        return Terms(LOWER, index=name, fixed=fixed)
    term = None
    if code == "ofz_curve":
        found = _TERM.search(name)
        term = Decimal(found.group(1)) if found else None
        if term is None:
            return Terms(LOWER, index=name, fixed=fixed)
    extremum = _extremum(text)
    if extremum is not None:
        factor, spread, low, high = extremum
        body = _EXTREMUM.sub(" ", text)
        if _NOT_ADDITIVE.search(_FLOOR.sub(" ", _CAP.sub(" ", body))):
            return _bounded_or_lower(name, floor, floor_ok, fixed)
        return Terms(
            ESTIMATE, index=code, spread=spread, term=term, factor=factor,
            floor=low if low is not None else floor, cap=high if high is not None else cap,
            fixed=fixed,
        )
    if not (floor_ok and cap_ok):
        return _bounded_or_lower(name, None, False, fixed)
    # Компаундированная RUONIA записана формулой с множителями по самому
    # индексу — её оценка по текущей overnight объявлена методикой.
    body = _FLOOR.sub(" ", _CAP.sub(" ", _FLOOR_MAX.sub(" ", text)))
    if code == "ruonia":
        body = re.sub(r"\(.*?\)\s*\*\s*B\s*/\s*T\w*\s*\*\s*\d+\s*%", "", body)
    if _NOT_ADDITIVE.search(body):
        return _bounded_or_lower(name, floor, floor_ok, fixed)
    spread = _decimal(record.get("margin"))
    if spread is None:
        return _bounded_or_lower(name, floor, floor_ok, fixed)
    return Terms(
        ESTIMATE, index=code, spread=spread, term=term, floor=floor, cap=cap, fixed=fixed
    )


def _bounded_or_lower(
    name: str, floor: Decimal | None, floor_ok: bool, fixed: tuple
) -> Terms:
    """Формула не разобрана: пол — граница снизу числом, иначе «не менее»."""
    if floor is not None and floor_ok:
        return Terms(FLOOR, index=name, floor=floor, fixed=fixed)
    return Terms(LOWER, index=name, fixed=fixed)


@cache
def key_rate() -> tuple[tuple[date, Decimal], ...]:
    """Ключевая ставка по дням действия, со всех сохранённых страниц Банка России."""
    found: dict[date, Decimal] = {}
    for path in sorted(CBR.glob("keyrate_*.html")):
        text = path.read_text(encoding="utf-8")
        for day, value in re.findall(r"<td>(\d\d\.\d\d\.\d{4})</td>\s*<td>([\d,]+)</td>", text):
            found[_ru_date(day)] = Decimal(value.replace(",", "."))
    return tuple(sorted(found.items()))


@cache
def ruonia() -> tuple[tuple[date, date, Decimal], ...]:
    """RUONIA: день ставки, день публикации и ставка, со всех сохранённых страниц."""
    found: dict[date, tuple[date, Decimal]] = {}
    pattern = re.compile(
        r"<tr>\s*<td>(\d\d\.\d\d\.\d{4})</td>\s*<td[^>]*>([\d,]+)</td>"
        r"(?:\s*<td[^>]*>[^<]*</td>){8}\s*<td[^>]*>(\d\d\.\d\d\.\d{4})</td>"
    )
    for path in sorted(CBR.glob("ruonia_*.html")):
        for day, value, published in pattern.findall(path.read_text(encoding="utf-8")):
            found[_ru_date(day)] = (_ru_date(published), Decimal(value.replace(",", ".")))
    return tuple(sorted((day, pub, value) for day, (pub, value) in found.items()))


@cache
def curves() -> dict[str, list[tuple[Decimal, Decimal]]]:
    """Опубликованные точки КБД по дням."""
    from finlib.sources.market import curve_of

    raw = json.loads((MOEX / "zcyc_by_day.json").read_text(encoding="utf-8"))
    return {
        day: curve_of(item["yearyields"]) for day, item in raw.items() if item.get("yearyields")
    }


def _percent(value: Decimal) -> str:
    """Ставка для печати: два знака, запятая."""
    return f"{value:.2f}".replace(".", ",")


def _ru_date(text: str) -> date:
    """Дата вида ДД.ММ.ГГГГ."""
    day, month, year = text.split(".")
    return date(int(year), int(month), int(day))


def rate_on(terms: Terms, today: date, stale_days: int) -> tuple[Decimal, date] | None:
    """Ставка индекса, известная на дату, и её день; None — ставки нет или она несвежая."""
    if terms.index in ("key_rate", "refinancing_rate"):
        # Ключевая ставка действует до следующего решения: свежести у неё нет.
        # Страница сохранена в один день и назад ограничена: дата раньше
        # начала ряда — это «ряда нет», а не «ставка действует».
        known = [(day, value) for day, value in key_rate() if day <= today]
        if not known:
            return None
        return known[-1][1], known[-1][0]
    if terms.index == "ruonia":
        known = [(day, value) for day, published, value in ruonia() if published <= today]
        if not known or today - known[-1][0] > timedelta(days=stale_days):
            return None
        return known[-1][1], known[-1][0]
    if terms.index == "ofz_curve" and terms.term is not None:
        from finlib.sources.market import curve_at

        days = sorted(day for day in curves() if date.fromisoformat(day) <= today)
        if not days or today - date.fromisoformat(days[-1]) > timedelta(days=stale_days):
            return None
        value, _ = curve_at(curves()[days[-1]], terms.term)
        return value, date.fromisoformat(days[-1])
    return None


NAMES = {
    "key_rate": "ключевая ставка",
    "refinancing_rate": "ключевая ставка (ставка рефинансирования)",
    "ruonia": "RUONIA",
    "ofz_curve": "КБД ОФЗ",
}


def estimate(
    terms: Terms,
    face: Decimal,
    days: int,
    today: date,
    stale_days: int,
    number: int | None = None,
    rate_at: Callable[[Terms, date, int], tuple[Decimal, date] | None] | None = None,
) -> Estimate:
    """Купон на одну бумагу за период `days` при непогашенном номинале `face`.

    `rate_at` — ставка индекса на дату, по умолчанию `rate_on`; поток к PV
    подаёт её запомненной на день торгов: правило то же, счёт один раз.
    """
    if number is not None:
        for first, last, rate in terms.fixed:
            if first <= number <= last:
                return Estimate(face * rate / 100 * days / 365, TERMS, "по условиям выпуска")
    if terms.kind == LAST_KNOWN and terms.last_coupon is not None:
        return Estimate(
            face * terms.last_coupon / 100 * days / 365, LAST_KNOWN, "последний известный купон"
        )
    if terms.kind == FLOOR and terms.floor is not None:
        return Estimate(face * terms.floor / 100 * days / 365, FLOOR, "пол купона по условиям")
    if terms.kind == ESTIMATE:
        got = (rate_at or rate_on)(terms, today, stale_days)
        if got is None:
            return Estimate(None, LOWER, "ставки индекса на дату нет")
        rate, seen = got
        label = NAMES.get(terms.index, terms.index)
        if terms.term is not None:
            label = f"{label} {terms.term:.0f} лет"
        said = f"{label} {_percent(rate)} % на {seen:%d.%m.%Y}"
        said += " + спред условий" if terms.factor == 1 else " по формуле условий"
        value = terms.factor * rate + terms.spread
        # **Пол и потолок — при текущем значении индекса** (решение владельца
        # 02.10.2026, 3.2): не связывают — индекс + спред, связывают — они.
        if terms.floor is not None and value < terms.floor:
            value = terms.floor
            said += f"; связывает пол условий {_percent(terms.floor)} %"
        if terms.cap is not None and value > terms.cap:
            value = terms.cap
            said += f"; связывает потолок условий {_percent(terms.cap)} %"
        return Estimate(face * value / 100 * days / 365, ESTIMATE, said)
    return Estimate(None, LOWER, "")


@cache
def record_of(emission_id: str) -> dict | None:
    """Запись выпуска с диска по всем сохранённым перечням выпусков."""
    return _records().get(emission_id)


@cache
def _records() -> dict[str, dict]:
    """Выпуск → запись: перечень выпусков в обращении и выпуски эмитентов."""
    found: dict[str, dict] = {}
    for path in sorted(CBONDS.glob("emissions_*.json")):
        try:
            items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            continue
        for item in items:
            emission = str(item.get("id") or "")
            if emission:
                found.setdefault(emission, item)
    return found


def flow_items(emission_id: str) -> list[dict]:
    """Строки графика выпуска как пришли; пусто — графика нет."""
    path = Path(CBONDS) / f"flow_{emission_id}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("items", [])


def last_rate(plan: Schedule) -> Decimal | None:
    """Ставка последнего купона графика, которую источник назвал; None — ни одной."""
    known = [item.rate for item in plan.payments if item.coupon_known and item.rate]
    return known[-1] if known else None


def _period(payment: Payment, plan: Schedule) -> tuple[Decimal, int] | None:
    """Непогашенный номинал на одну бумагу и дни купонного периода; None — не определены."""
    if plan.nominal is None or payment.start is None:
        return None
    face = plan.nominal - sum(
        (item.redemption for item in plan.payments if item.due < payment.due),
        Decimal(0),
    )
    days = (payment.due - payment.start).days
    if face <= 0 or days <= 0:
        return None
    return face, days


def estimator(rules: dict) -> Callable[[str, Payment, Schedule, date], tuple]:
    """Оценщик купона для `cbonds_flows.refinancing` по блоку методики."""

    def said(
        emission: str, payment: Payment, plan: Schedule, today: date
    ) -> tuple[Decimal | None, str, bool]:
        record = record_of(emission)
        period = _period(payment, plan)
        if record is None or period is None:
            return None, "", False
        terms = terms_of(record, rules, last_rate(plan))
        found = estimate(
            terms, *period, today, int(rules["rate_stale_days"]), payment.number
        )
        return found.amount, found.basis, found.data

    return said


# **Что из оценок идёт в поток к PV** (решение владельца 08.10.2026): ставка
# по условиям выпуска, «индекс + спред» с полом и потолком при текущем индексе
# и последний известный купон — как в рефинансировании. **Пол без индекса —
# нет**: это граница снизу, она занижает купон, завышает отношение цены к PV
# и может спрятать признак; такой выпуск остаётся без потока, и берётся
# подстановка от номинала.
PV_KINDS: tuple[str, ...] = (TERMS, ESTIMATE, LAST_KNOWN)


def pv_coupons(rules: dict) -> Callable[[str, date], dict[date, Decimal]]:
    """Оценки неустановленных будущих купонов выпуска на день торгов: срок → сумма.

    Та же оценка, что у рефинансирования (`estimate`, `terms_of`), по графику
    `cbonds_flows.schedule_of` и при ставке индекса на день торгов, а не на
    сегодня: ряд строится за прошлые дни. Купон без оценки в ответе
    отсутствует — поток у такой бумаги не строится. Правило выпуска и ставка
    индекса дня запоминаются на время одной сборки ряда.
    """
    stale = int(rules["rate_stale_days"])
    plans: dict[str, tuple[Schedule, Terms] | None] = {}
    rates: dict[tuple[str, Decimal | None, date], tuple[Decimal, date] | None] = {}

    def rate_at(terms: Terms, day: date, stale_days: int) -> tuple[Decimal, date] | None:
        key = (terms.index, terms.term, day)
        if key not in rates:
            rates[key] = rate_on(terms, day, stale_days)
        return rates[key]

    def coupons(emission: str, day: date) -> dict[date, Decimal]:
        if emission not in plans:
            plan = schedule_of(emission)
            record = record_of(emission)
            plans[emission] = (
                (plan, terms_of(record, rules, last_rate(plan)))
                if plan is not None and record is not None
                else None
            )
        known = plans[emission]
        if known is None:
            return {}
        plan, terms = known
        found: dict[date, Decimal] = {}
        for payment in plan.payments:
            if payment.coupon_known or payment.due <= day:
                continue
            period = _period(payment, plan)
            if period is None:
                continue
            got = estimate(terms, *period, day, stale, payment.number, rate_at)
            if got.amount is not None and got.kind in PV_KINDS:
                found[payment.due] = got.amount
        return found

    return coupons
