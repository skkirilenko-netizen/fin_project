"""Величины промежуточного комплекта: поток к году, баланс на дату.

**Поток промежуточной отчётности накопительный с начала года**, и умножать
его на два нельзя: у бизнеса с зимним пиком полугодие занижает год, у летнего
завышает, и ошибка эта не ловится ни одним контролем сходимости — величина
согласована сама с собой, неверна только мера. Поэтому поток приводится
к скользящим двенадцати месяцам тождеством, объявленным методикой
(`standards.yaml`, `period_preference.rolling_formula`):

    последний годовой + текущее с начала года − прошлогоднее с начала года

**Отрезок обязан совпадать**: полугодие вычитается из полугодия, девять
месяцев из девяти. Иначе из года вычитается не то, что прибавлено, и выходит
величина настоящего вида, которой не соответствует ни один период.

**Нехватка любой из трёх величин — отказ, а не приближение.** Это то же
правило, что у показателей: один недостающий компонент отменяет величину
целиком, и причина называется машинным кодом.

**Баланс берётся на дату как есть**: величина на момент, приводить её
к году не к чему.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finlib.standards import load_standards

logger = logging.getLogger(__name__)


def in_unit(value: Decimal | None, own: object, target: object) -> Decimal | None:
    """Величина в единице `target` (коды ОКЕИ); единица не известна — величины нет.

    **Слагаемые тождества LTM приходят из разных комплектов, а единица
    у эмитента меняется**: у Брусники комплекты агрегатора до 30.06.2024
    в тысячах, дальше в миллионах; у 3900019850 — год 2025 в миллионах,
    первый квартал 2026 в миллиардах. Сложенные как есть, они дают величину
    настоящего вида, которой не соответствует ни один период. Одна точка
    пересчёта на проект: тренд, база маршрута и операционный результат LTM.
    """
    from finlib.sources.cbonds_events import OKEI_MULTIPLIER

    if value is None:
        return None
    if str(own) == str(target):
        # Одна и та же единица, в том числе неназванная у обоих: пересчитывать
        # нечего, и отказ здесь отнял бы величину без причины.
        return value
    mine = OKEI_MULTIPLIER.get(str(own))
    theirs = OKEI_MULTIPLIER.get(str(target))
    if mine is None or theirs is None:
        return None
    return value if mine == theirs else value * mine / theirs


@dataclass(frozen=True, slots=True)
class Rolling:
    """Скользящие двенадцать месяцев: величина и из чего она сложена.

    **Состав хранится рядом с величиной.** Тождество из трёх слагаемых
    проверяется только по ним, а «EBITDA 4 280» без состава защитить нечем.
    """

    value: Decimal | None
    annual: date | None = None
    current: date | None = None
    previous: date | None = None
    reason: str = ""

    @property
    def known(self) -> bool:
        """Сложилась ли величина."""
        return self.value is not None

    def describe(self) -> str:
        """Состав словами — для карточки и замера."""
        if not self.known:
            return f"не считается: {self.reason}"
        return (
            f"год {self.annual:%d.%m.%Y} + с начала года {self.current:%d.%m.%Y} "
            f"− {self.previous:%d.%m.%Y}"
        )


def same_ytd_year_before(moment: date) -> date:
    """Та же отчётная дата прошлого года: полугодие сравнивается с полугодием."""
    # 29 февраля в невисокосном году не существует, и подставлять 28-е значило
    # бы сравнивать разные отрезки. Отчётные даты отчётности такого случая
    # не дают — они кварталные, — но правило объявляется, а не подразумевается.
    return date(moment.year - 1, moment.month, moment.day)


def last_annual_before(moment: date, known: set[date]) -> date | None:
    """Ближайший годовой период, кончившийся до этой даты."""
    annual = sorted(
        item for item in known if (item.month, item.day) == (12, 31) and item < moment
    )
    return annual[-1] if annual else None


def rolling_flow(values: dict[date, Decimal | None], moment: date) -> Rolling:
    """Поток за скользящие двенадцать месяцев на промежуточную дату.

    `values` — величина строки по отчётным датам, как она пришла: у потока
    промежуточного периода это накопленное с начала года.

    Годовая дата возвращает величину как есть: приводить годовой поток
    к двенадцати месяцам не к чему.
    """
    if (moment.month, moment.day) == (12, 31):
        value = values.get(moment)
        return Rolling(value, annual=moment, reason="" if value is not None else
                       "величина годового периода не раскрыта")
    rule = load_standards().period_preference
    if rule.interim_use != "rolling_twelve_months":
        # Правило объявлено в методике, и код его только применяет: другое
        # значение означает, что методика изменилась, а расчёт — нет.
        return Rolling(None, reason=f"правило приведения не поддержано: {rule.interim_use}")
    # **Годовой — ровно прошлого года, а не ближайший из известных.** Тождество
    # верно только тогда, когда прошлогоднее с начала года лежит внутри того
    # же годового периода, что прибавлен; «ближайший годовой» при пропущенном
    # годе брал позапрошлый, и выходила величина настоящего вида, которой
    # не соответствует ни один период.
    wanted = date(moment.year - 1, 12, 31)
    annual = wanted if wanted in values else None
    before = same_ytd_year_before(moment)
    current_value = values.get(moment)
    parts = {
        "годового периода": (annual, values.get(annual) if annual else None),
        "текущего с начала года": (moment, current_value),
        "прошлогоднего с начала года": (before, values.get(before)),
    }
    missing = [name for name, (when, value) in parts.items() if when is None or value is None]
    if missing:
        return Rolling(None, reason="нет величины " + ", ".join(missing))
    assert annual is not None
    value = (
        (values[annual] or Decimal(0))
        + (current_value or Decimal(0))
        - (values[before] or Decimal(0))
    )
    return Rolling(value, annual=annual, current=moment, previous=before)


def ltm_values(
    by_date: dict[date, dict[str, Decimal | None]],
    moment: date,
    flows: set[str],
) -> tuple[dict[str, Decimal | None], dict[str, Rolling]]:
    """Величины базы на дату: потоки за скользящие двенадцать месяцев, баланс как есть.

    **База маршрута — LTM** (`standards.yaml`, `period_preference.basis`).
    Поток, у которого тождество не сложилось, остаётся без величины — отказ,
    а не приближение, — и его состав возвращается рядом, чтобы причина
    называлась. Балансовая величина берётся на дату: приводить её не к чему.
    """
    current = dict(by_date.get(moment, {}))
    rolled: dict[str, Rolling] = {}
    for code in flows:
        found = rolling_flow(
            {day: values.get(code) for day, values in by_date.items()}, moment
        )
        rolled[code] = found
        current[code] = found.value
    return current, rolled
