"""Справочник показателей МСФО в том виде, в каком его спрашивает документ.

Документ спрашивает у справочника показателей немногое: наименование, единицу,
группу и разрядность. У РСБУ и у МСФО это разные справочники, и подставлять
один вместо другого нельзя: коды `cur_liq`, `equity_ratio`, `debt_total`
и `net_debt` есть у обоих, а означают разное. В приложении по МСФО так
печаталось «Коэффициент текущей ликвидности» вместо «Текущая ликвидность»,
а показатели, которых у РСБУ нет вовсе, из таблицы **выпадали**: фильтр
отбрасывал всё, чего нет в справочнике РСБУ, — четыре показателя из десяти,
включая оба, по которым считается долговая нагрузка.

Вид, а не наследник: сводить два справочника в одну иерархию значило бы
объявить их одним предметом. Здесь ровно то, что нужно документу.
"""

from dataclasses import dataclass
from decimal import Decimal

from finlib.metrics.definitions import Unit
from finlib.normalize.ifrs_metrics import IfrsMetricsPolicy, load_ifrs_metrics

# Разрядность отображения **не своя**: она объявлена по единицам измерения
# в `metrics.yaml`, блок `display`, и это единая точка округления на весь
# проект. Своя разрядность у ветки МСФО означала бы второе правило округления:
# документ печатал бы уровни с двумя знаками, а изменения считались бы
# от трёх — и контроль согласованности документа поймал бы ровно это
# («заявленное изменение 0,04 не равно разности уровней 0,03»).


@dataclass(frozen=True, slots=True)
class IfrsMetricView:
    """Показатель МСФО так, как его называет документ."""

    code: str
    name: str
    group: str
    unit: Unit
    note: str | None = None
    benchmark: Decimal | None = None


@dataclass(frozen=True, slots=True)
class IfrsGroupView:
    """Группа показателей МСФО: наименование для абзаца и таблицы."""

    name: str


class IfrsMetricsView:
    """Справочник показателей МСФО с интерфейсом, который спрашивает документ."""

    def __init__(self, policy: IfrsMetricsPolicy | None = None) -> None:
        self._policy = policy or load_ifrs_metrics()
        self._by_code = {
            item.code: IfrsMetricView(
                code=item.code,
                name=item.name,
                group=item.group,
                unit=Unit.THOUSAND_RUB if item.unit == "currency" else Unit.RATIO,
                note=item.note,
            )
            for item in self._policy.metrics
        }

    @property
    def version(self) -> str:
        """Версия справочника показателей МСФО."""
        return self._policy.version

    @property
    def metrics(self) -> tuple[IfrsMetricView, ...]:
        """Все показатели справочника в порядке методики."""
        return tuple(self._by_code.values())

    @property
    def groups(self) -> dict[str, IfrsGroupView]:
        """Группы показателей: и балльные, и приложения."""
        found = {
            code: IfrsGroupView(item.name) for code, item in self._policy.groups.items()
        }
        found |= {
            code: IfrsGroupView(item.name)
            for code, item in self._policy.appendix_groups.items()
        }
        return found

    def get(self, code: str) -> IfrsMetricView | None:
        """Показатель по коду; None — кода в справочнике МСФО нет."""
        return self._by_code.get(code)

    def require(self, code: str) -> IfrsMetricView:
        """Показатель по коду; неизвестный код — ошибка, а не молчание."""
        found = self._by_code.get(code)
        if found is None:
            raise KeyError(f"показателя {code} нет в справочнике показателей МСФО")
        return found

    def scale_for(self, code: str) -> int:
        """Разрядность отображения показателя — из единой точки округления."""
        from finlib.metrics.definitions import load_metrics

        metric = self._by_code.get(code)
        unit = metric.unit if metric is not None else Unit.RATIO
        return load_metrics().display.scale_for(unit)
