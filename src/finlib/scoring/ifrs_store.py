"""Запись показателей и оценки МСФО в базу — теми же таблицами, что у РСБУ.

**Таблицы одни, стандарт входит в ключ.** Ряды по РСБУ и по МСФО несопоставимы,
и держать для них разные таблицы значило бы иметь две модели одного предмета:
ключ `metric_value` и `assessment` уже содержит стандарт, и этого достаточно.

Разложение оценки МСФО беднее разложения РСБУ, и пустые графы здесь означают
именно отсутствие предмета, а не потерю: уровня и динамики у показателя МСФО
нет — балл ставится по значению, потому что второй точки ряда у эмитента
ещё не бывает; флагов и надзорных сигналов ветка МСФО пока не считает.
Заполнять их нулями нельзя: ноль читался бы как «посчитано и получилось ноль».
"""

import logging
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, fetch_all, fetch_one
from finlib.metrics.ifrs import MetricValue
from finlib.metrics.store import save_results
from finlib.normalize.facts import SOURCE_PREFERENCE
from finlib.normalize.ifrs_metrics import IfrsMetricsPolicy, load_ifrs_metrics
from finlib.quality.periods import PeriodConfidence
from finlib.scoring.definitions import Confidence, StopEffect
from finlib.scoring.engine import Assessment as StoredAssessment
from finlib.scoring.engine import GroupScore as StoredGroup
from finlib.scoring.ifrs import Assessment as IfrsAssessment
from finlib.scoring.ifrs import StopFactors, assess, evaluate_stop_factors
from finlib.scoring.ifrs_signals import ifrs_signals
from finlib.scoring.metric_score import MetricScore as StoredMetric
from finlib.scoring.store import save_assessment
from finlib.standards import Standard

logger = logging.getLogger(__name__)


class _Result:
    """Строка расчёта в виде, который принимает запись показателей.

    Отдельный вид, а не подмена: у РСБУ результат несёт период, статус
    и доверие к периоду, а расчёт МСФО отдаёт значение с причиной отказа.
    Сводить их в одном классе значило бы объявить одинаковым то, что считается
    по-разному.
    """

    __slots__ = (
        "report_date",
        "metric_code",
        "value",
        "status",
        "confidence",
        "reason",
        "reason_code",
    )

    def __init__(
        self,
        item: MetricValue,
        report_date: date,
        confidence: PeriodConfidence = PeriodConfidence.VERIFIED,
    ) -> None:
        self.report_date = report_date
        self.metric_code = item.code
        self.value = item.value
        self.status = _Status("ok" if item.calculable else "not_calculable")
        # Доверие к периоду приходит снаружи: у отчётного периода оно полное,
        # у периода, существующего только сравнительной колонкой, пониженное.
        self.confidence = confidence
        # Причина без наименования: наименование подставляет тот, кто печатает
        # отказ, и в разделе «Ограничения анализа» выходило «Показатель
        # „Покрытие погашений“ не рассчитан: Покрытие погашений: не рассчитан…».
        self.reason = None if item.calculable else _reason_text(item, report_date)
        # Машинная причина — само значение перечисления: свободных строк
        # для неё в коде быть не должно.
        self.reason_code = item.reason.value if item.reason else None


class _Status:
    """Статус значения в виде, который ожидает запись: со полем `value`."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


def _reason_text(
    item: MetricValue, report_date: date, policy: IfrsMetricsPolicy | None = None
) -> str:
    """Причина отказа словами: без наименования показателя, но с периодом.

    Недостающие величины называются словами, а не кодами: «нет входных
    величин — interest_accrued» — технический идентификатор в тексте документа,
    а правило его запрещает. Период назван потому, что утверждение «не
    рассчитан» без года ложно, когда за другой период показатель посчитан.

    **У невозможной поправки место называет методика.** Величины, которой
    не хватает, в справочнике позиций нет вовсе — она стоит сноской, — и
    назвать её словами нечем: в текст протекал код `ifrs.escrow_balance`,
    читателю ничего не говорящий. Методика объявляет место сама: «средства
    на счетах эскроу раскрыты сноской под балансом, а не строкой формы».
    """
    from finlib.metrics.ifrs import reason_text

    return f"за {report_date.year} год {reason_text(item, policy)}"


def save_metrics(
    inn: str,
    report_date: date,
    computed: tuple[MetricValue, ...],
    conn: PgConnection,
    policy: IfrsMetricsPolicy | None = None,
    confidence: PeriodConfidence = PeriodConfidence.VERIFIED,
) -> int:
    """Пишет показатели МСФО за период; повторный расчёт не создаёт дублей.

    Доверие к периоду приходит доводом: период, существующий только
    сравнительной колонкой, блокирующими контролями не проверялся, и признак
    обязан стоять рядом с величиной. В балл такой период не идёт по правилу
    методики — балл считается по уровню отчётного периода.
    """
    policy = policy or load_ifrs_metrics()
    saved = save_results(
        inn,
        [_Result(item, report_date, confidence) for item in computed],
        conn,
        Standard.IFRS,
    )
    logger.info(
        "показатели МСФО: записано %d значений по ИНН %s за %s",
        saved,
        inn,
        report_date,
    )
    return saved


def assess_ifrs(
    inn: str,
    conn: PgConnection,
    policy: IfrsMetricsPolicy | None = None,
) -> tuple[IfrsAssessment, int, StopFactors]:
    """Считает и пишет показатели по всем периодам и оценку по свежему.

    **Периодов больше одного намеренно.** Балл считается по уровню отчётного
    периода (правило объявлено в методике), а сравнительные периоды нужны
    читателю: изменения за период печатаются в приложении и в перечне
    наибольших изменений. Доверие к ним пониженное, и это стоит рядом
    с каждой величиной.
    """
    from finlib.metrics.ifrs_store import (
        compute_from_facts,
        confidence_of,
        periods_of,
    )

    policy = policy or load_ifrs_metrics()
    periods = periods_of(inn, conn)
    if not periods:
        raise ValueError(f"по МСФО у {inn} нет комплектов вне карантина")
    confidences = confidence_of(inn, conn)

    saved = 0
    latest: tuple[MetricValue, ...] = ()
    for report_date in periods:
        computed = compute_from_facts(inn, report_date, conn, policy)
        saved += save_metrics(
            inn,
            report_date,
            computed,
            conn,
            policy,
            confidences.get(report_date, PeriodConfidence.VERIFIED),
        )
        if report_date == periods[0]:
            latest = computed

    # **Стоп-факторы проверяются здесь же, по величинам комплекта.** Прежде
    # оценка звалась с пустым перечнем исключённых и писала
    # `stop_factor_code = NULL`: механизм задачи 26 существовал только
    # в замере, и в документе стоп-фактора не было вовсе.
    stops = stop_factors_of(inn, periods[0], latest, conn, policy)
    logger.info("%s за %s: %s", inn, periods[0], stops.describe())
    result = assess(latest, policy, stops)
    save_ifrs_assessment(inn, periods[0], result, latest, conn, policy, stops)
    save_changes(inn, periods, conn, policy)
    # Стоп-факторы возвращаются вызывающему: их исход — главное, что прогон
    # обязан показать. Класс E у Сегежи получается и по баллу, и по стоп-фактору,
    # и без перечня проверенного одно от другого не отличить.
    return result, saved, stops


def stop_factors_of(
    inn: str,
    report_date: date,
    computed: tuple[MetricValue, ...],
    conn: PgConnection,
    policy: IfrsMetricsPolicy | None = None,
) -> StopFactors:
    """Стоп-факторы комплекта: тип эмитента и заключение берутся из базы.

    Обстановка хранится с комплектом (`src_file.meta`), поэтому доводом
    её передавать не нужно: величина, которую можно передать снаружи, однажды
    передаётся неверной и об этом не сообщает.
    """
    from finlib.sources.ifrs_audit import Determination, audit_from_meta

    meta = _meta_of(inn, report_date, conn)
    audit = audit_from_meta(meta)
    return evaluate_stop_factors(
        computed,
        meta.get("issuer_type") or "corporate",
        audit.sections if audit is not None else (),
        audit is not None and audit.determination is Determination.DETERMINED,
        policy,
    )


def save_changes(
    inn: str,
    periods: tuple[date, ...],
    conn: PgConnection,
    policy: IfrsMetricsPolicy | None = None,
) -> int:
    """Пишет изменения показателей и статей за период — справочно, не в балл.

    Правило участия объявлено методикой: балл МСФО равен уровню, а изменения
    нужны читателю. Считаются они от **округлённых** величин — по той же единой
    точке округления, что печатает документ: дельта, посчитанная по полной
    точности, расходится с разностью напечатанных уровней.
    """
    from finlib.metrics.derived import DerivedKind
    from finlib.metrics.display import round_to
    from finlib.metrics.ifrs_view import IfrsMetricsView

    policy = policy or load_ifrs_metrics()
    if len(periods) < 2:
        logger.info("%s: изменений по МСФО нет — период один", inn)
        return 0
    current, previous = periods[0], periods[1]
    view = IfrsMetricsView(policy)

    values = {
        (row["report_date"], row["metric_code"]): row["value"]
        for row in fetch_all(
            _METRIC_VALUES,
            {"inn": inn, "standard": Standard.IFRS.value, "dates": list(periods[:2])},
            conn=conn,
        )
        if row["status"] == "ok" and row["value"] is not None
    }
    facts = {
        (row["report_date"], row["line_code"]): row["value"]
        for row in fetch_all(
            _FACT_VALUES,
            {"inn": inn, "standard": Standard.IFRS.value, "dates": list(periods[:2])},
            conn=conn,
        )
    }

    results: list[_Change] = []
    for base, scale in _bases(values, facts, view, current, previous):
        now = values.get((current, base), facts.get((current, base)))
        before = values.get((previous, base), facts.get((previous, base)))
        if now is None or before is None:
            continue
        now, before = round_to(now, scale), round_to(before, scale)
        results.append(
            _Change(current, f"{base}_{DerivedKind.CHANGE_ABS.value}", now - before)
        )
        # Процент не считается от неположительной базы и при смене знака:
        # «рост на 55 %» при движении с −442 до −200 читается как ложь.
        if before > 0 and now > 0:
            results.append(
                _Change(
                    current,
                    f"{base}_{DerivedKind.CHANGE_PCT.value}",
                    (now - before) / before * Decimal(100),
                )
            )
    if not results:
        return 0
    return save_results(inn, results, conn, Standard.IFRS)


_METRIC_VALUES = """
SELECT report_date, metric_code, value, status FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
"""

_FACT_VALUES = """
SELECT report_date, line_code, value FROM fact_report f
WHERE f.inn = %(inn)s AND f.standard = %(standard)s
  AND f.report_date = ANY(%(dates)s) AND f.value IS NOT NULL
"""


class _Change:
    """Изменение за период в виде, который принимает запись показателей."""

    __slots__ = (
        "report_date",
        "metric_code",
        "value",
        "status",
        "confidence",
        "reason",
        "reason_code",
    )

    def __init__(self, report_date: date, code: str, value: Decimal) -> None:
        self.report_date = report_date
        self.metric_code = code
        self.value = value
        self.status = _Status("ok")
        # Доверие изменения — худшее из двух периодов: сравнительный период
        # блокирующими контролями не проверялся, и изменение опирается на него.
        self.confidence = PeriodConfidence.COMPARATIVE_ONLY
        self.reason = None
        self.reason_code = None


def _bases(
    values: dict, facts: dict, view, current: date, previous: date
) -> list[tuple[str, int]]:
    """Величины, у которых есть обе точки: код и разрядность отображения.

    Берутся и показатели, и статьи отчётности: перечень наибольших изменений
    фактической базы строится по статьям, а тезисы — по показателям.
    """
    from finlib.metrics.definitions import Unit

    found: list[tuple[str, int]] = []
    for code in sorted({code for _date, code in values}):
        if (current, code) in values and (previous, code) in values:
            found.append((code, view.scale_for(code)))
    line_scale = _SCALE_THOUSANDS
    for code in sorted({code for _date, code in facts}):
        if (current, code) in facts and (previous, code) in facts:
            found.append((code, line_scale))
    _ = Unit
    return found


# Статьи отчётности печатаются целыми тысячами, и изменения считаются от них же.
_SCALE_THOUSANDS = 0


# **Комплект года выбирается с предпочтением первоисточника.** За год их
# теперь два — документ и доставка агрегатора, — и без порядка сведения
# аудиторского заключения и тип эмитента исчезали: у комплекта агрегатора
# их нет вовсе. Правило одно на проект (`normalize/facts.py`).
_AUDIT_META = f"""
SELECT meta FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(year)s
  AND is_actual
{SOURCE_PREFERENCE}
LIMIT 1
"""


def _meta_of(inn: str, report_date: date, conn: PgConnection) -> dict:
    """`src_file.meta` актуального комплекта года; пусто — комплекта нет."""
    row = fetch_one(
        _AUDIT_META,
        {"inn": inn, "standard": Standard.IFRS.value, "year": report_date.year},
        conn=conn,
    )
    return (row["meta"] if row else None) or {}


def _audit_confidence(
    inn: str,
    report_date: date,
    conn: PgConnection,
    policy: IfrsMetricsPolicy,
    assessment: IfrsAssessment,
) -> tuple[Confidence, list[str]]:
    """Уверенность в оценке: основания понижения из методики и из обстановки.

    Сведения берутся **из базы**, а не доводом: довод с умолчанием означал бы,
    что понижение можно молча не применить, — а именно так шесть кодов
    заключения не дошли ни до одного комплекта.

    Оснований четыре, и три из них появились по соразмерности: у Сегежи
    уверенность выходила высшей при классе E, объявленной неопределённости
    непрерывности деятельности, отчётности специального назначения, принятой
    решением человека, и одном показателе в каждой группе.
    """
    from finlib.normalize.ifrs_audit import load_audit_policy
    from finlib.sources.ifrs_audit import audit_from_meta

    rule = policy.confidence
    meta = _meta_of(inn, report_date, conn)
    audit = audit_from_meta(meta)
    grounds: list[str] = []
    # **Уверенность понижает оговорка о величинах, а не всякая модификация
    # мнения.** Нераскрытые сегменты ФосАгро на величины, из которых считаются
    # показатели, не влияют; заниженный резерв Автодора влияет. Следствие вида
    # объявлено методикой у самого вида, и до подтверждения человеком оно
    # не применяется.
    if audit is not None and audit.modified:
        audit_policy = load_audit_policy()
        kind = audit_policy.caveat_kind(audit.effective_caveat_kind(audit_policy))
        if kind is not None and kind.lowers_confidence:
            grounds.append(rule.text_of("modified_opinion"))
    if audit is not None and "going_concern_uncertainty" in audit.sections:
        grounds.append(rule.text_of("going_concern_uncertainty"))
    accepted = ((meta or {}).get("accepted") or {}).get("grounds") or {}
    if accepted:
        grounds.append(rule.text_of("accepted_ground"))
    # Группа, оценённая одним показателем, опирается на единственное
    # наблюдение. Основание срабатывает, когда таковы все группы: отдельная
    # группа из одного показателя — норма ветки, где показателей всего шесть.
    counted = [len(item.metrics) for item in assessment.groups]
    if counted and sum(1 for item in counted if item == 1) >= len(counted) * float(
        rule.single_metric_share
    ):
        grounds.append(rule.text_of("single_metric_groups"))
    return Confidence(rule.level_for(len(grounds))), grounds


def save_ifrs_assessment(
    inn: str,
    report_date: date,
    assessment: IfrsAssessment,
    computed: tuple[MetricValue, ...],
    conn: PgConnection,
    policy: IfrsMetricsPolicy | None = None,
    stops: StopFactors | None = None,
) -> int:
    """Пишет оценку МСФО и её разложение по группам и показателям.

    `stops` — стоп-факторы, по которым считалась оценка: из них берутся
    последствие для класса и оговорки неприменимости. Без них запись
    утверждала бы, что стоп-факторов нет, тогда как их не проверяли.
    """
    policy = policy or load_ifrs_metrics()
    stops = stops if stops is not None else StopFactors()
    audit_confidence, audit_reasons = _audit_confidence(
        inn, report_date, conn, policy, assessment
    )
    in_scoring = {item.code for group in assessment.groups for item in group.metrics}
    stored = StoredAssessment(
        inn=inn,
        standard=Standard.IFRS,
        report_date=report_date,
        total_score=assessment.score,
        class_code=assessment.class_code,
        class_name=assessment.class_name or None,
        no_class_reason=assessment.no_class_reason or None,
        # Узость основания у МСФО объявляется причиной отказа целиком:
        # отдельного случая «класс есть, а балла нет» здесь не возникает —
        # стоп-факторы ветки применяются к показателям, а не к классу.
        breadth_reason=assessment.breadth_reason,
        # Класс до применения стоп-фактора и после — разные сведения: класс E
        # у набравшего по баллу B и класс E у набравшего E выглядят одинаково.
        class_before_stop=assessment.class_before_stop or assessment.class_code,
        stop_factor_code=assessment.stop_factor_code,
        # Все сработавшие, а не только назначивший класс: у Сегежи их три,
        # и документ называл один.
        stop_factor_codes=list(stops.triggered),
        stop_factor_effect=_effect_of(assessment, stops, policy),
        # Сверка с заключением хранится вместе со стоп-фактором: подтверждённый
        # аудитором и неподтверждённый равно остаются в силе, но формулировки
        # во втором случае обязаны быть осторожнее, а нечитаемое заключение —
        # третий исход, а не второй.
        stop_factor_audit=stops.audit_note or None,
        # Основания понижения объявлены методикой ветки, и их четыре: два
        # о заключении, одно о принятом человеком основании карантина, одно
        # о ширине наблюдения. Правило по длине ряда к МСФО не применяется —
        # ряда у эмитента ещё нет. Высокая означает «понижать не по чему».
        confidence=audit_confidence,
        confidence_reasons=[*assessment.divergence, *audit_reasons],
        groups=[
            StoredGroup(
                code=group.code,
                name=group.name,
                score=group.score,
                nominal_weight=group.nominal_weight,
                effective_weight=group.effective_weight,
                metrics_used=len(group.metrics),
                metrics_excluded=sum(
                    1
                    for item in computed
                    if item.group == group.code and item.code not in in_scoring
                ),
            )
            for group in assessment.groups
        ],
        metrics=[
            StoredMetric(
                metric_code=item.code,
                group_code=item.group,
                value=item.value,
                score=_score_of(item.code, assessment),
                # Уровня и динамики у показателя МСФО нет: балл ставится
                # по значению, второй точки ряда у эмитента пока не бывает.
                level=None,
                dynamics=None,
                periods_used=1 if item.calculable else 0,
                included=item.code in in_scoring,
                exclusion_reason=_exclusion_of(item, in_scoring, stops, policy),
                # **Вид причины объявлен у показателя**, и без него все
                # исключённые сводились в одну строку «шкалы уровня у них
                # нет» — включая те, что исключены стоп-фактором: графа
                # говорила о них неправду.
                exclusion_kind=_kind_of(item, in_scoring, stops, policy),
                excluded_by_methodology=not item.in_scoring,
            )
            for item in computed
        ],
        # **Надзорные признаки ветки считаются здесь же и по её справочнику.**
        # Прежде раздел 4 документа по МСФО держался на одном заключении
        # аудитора: движение капитала, структурный сдвиг и выплаты акционерам
        # расчёт не выявлял вовсе, потому что признаков у ветки не было —
        # и отличить это от «ничего не нашлось» было нечем.
        # Сработавшие стоп-факторы доходят до признаков: выплата акционерам
        # при состоянии, ограничившем класс, есть обстоятельство независимо
        # от её размера.
        signals=list(
            ifrs_signals(inn, conn, report_date, stop_factors=stops.triggered)
        ),
        metrics_version=policy.version,
        scoring_version=policy.version,
        flags_version="",
    )
    return save_assessment(stored, conn)


def _effect_of(
    assessment: IfrsAssessment, stops: StopFactors, policy: IfrsMetricsPolicy
) -> StopEffect:
    """Что стоп-фактор сделал с классом: опустил до низшего или ограничил.

    Градация читается из объявленной отсечки, а не из полученного класса:
    балл сам по себе бывает низшим, и тогда ограничение средним выглядело бы
    опусканием до низшего — то есть графа сказала бы о стоп-факторе неправду.
    """
    if assessment.stop_factor_code is None or stops.cap is None:
        return StopEffect.NONE
    return (
        StopEffect.LOWEST_CLASS
        if stops.cap == policy.classes[-1].code
        else StopEffect.CAP_AT_CLASS
    )


def _score_of(code: str, assessment: IfrsAssessment) -> Decimal | None:
    """Балл показателя из разложения оценки; None — в балл не входил."""
    for group in assessment.groups:
        for item in group.metrics:
            if item.code == code:
                return item.score
    return None


def _kind_of(
    item: MetricValue,
    in_scoring: set[str],
    stops: StopFactors,
    policy: IfrsMetricsPolicy,
):
    """Вид причины исключения: решение методики, стоп-фактор или нехватка данных.

    Вид машинный и общий с РСБУ (`ExclusionKind`): по нему причины сводятся
    в одну строку раздела, и вид, названный неверно, сводит разное вместе.
    """
    from finlib.metrics.definitions import ExclusionKind

    if item.code in in_scoring:
        return None
    if stops.limitation_of(item.code) is not None:
        return ExclusionKind.STOP_FACTOR
    declared = next(
        (
            metric.exclusion_kind
            for metric in policy.metrics
            if metric.code == item.code and metric.exclusion_kind
        ),
        None,
    )
    if declared:
        return ExclusionKind(declared)
    return None if item.calculable else ExclusionKind.NO_DATA


def _exclusion_of(
    item: MetricValue,
    in_scoring: set[str],
    stops: StopFactors,
    policy: IfrsMetricsPolicy,
) -> str | None:
    """Почему показатель не вошёл в балл; None — вошёл.

    Причина обязана быть безусловной и различать наше решение от неполноты
    данных: «в балл не идёт по методике» и «не рассчитан» — разные сведения.
    Неприменимость стоп-фактора называется оговоркой методики дословно: она
    объясняет не только исключение, но и то, что оценка не понижена.
    """
    if item.code in in_scoring:
        return None
    limitation = stops.limitation_of(item.code)
    if limitation is not None:
        return limitation
    if not item.in_scoring:
        # **Причина объявлена у показателя, а не подставлена общей фразой.**
        # «Показатель описывает деятельность» верно для рентабельности
        # и неверно для собственного капитала: у него исключение — это
        # стоп-фактор, а не измерение, и методика так и говорит.
        declared = next(
            (
                metric.exclusion_reason
                for metric in policy.metrics
                if metric.code == item.code and metric.exclusion_reason
            ),
            None,
        )
        if declared:
            return " ".join(declared.split())
        return "в балл не входит по методике: показатель описывает деятельность"
    if not item.calculable:
        # Наименование показателя даёт формулировка отказа, и повторять его
        # здесь значило бы напечатать «Покрытие погашений… в балл не вошёл:
        # показатель не рассчитан (Покрытие погашений…: не рассчитан, …)».
        from finlib.metrics.ifrs import REASON_TEXT, named

        text = REASON_TEXT.get(item.reason, "причина не названа")
        missing = ", ".join(named(code) for code in item.missing)
        return f"{text} — {missing}" if missing else text
    return "шкала уровня для показателя не объявлена"
