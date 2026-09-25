"""Выборка всего, что нужно заключению, одним обращением к базе.

Документ собирается из того же, из чего собирался контекст модели: оценки,
разложения, показателей и журнала качества. Разница в том, что здесь читается
и то, что модели не передавалось, — причины исключения показателей и перечень
выполненных контролей: они идут в приложение, которое модель не пишет.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from finlib.db import PgConnection, fetch_all, fetch_one
from finlib.metrics.definitions import EXCLUSION_ORDER, ExclusionKind
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Показатель исключён из балла из-за нехватки данных, а не решением методики.
# Формулировку ставит scoring/metric_score.py; здесь она опознаётся, чтобы
# отделить неполноту отчётности от исключения по методике (docs/scoring.md).
NOT_CALCULATED_MARK = "не рассчитан"

_ORGANIZATION = """
-- `org_meta` названа иначе, чем `meta` комплекта: два поля одного имени
-- в одной строке сливаются, и адрес организации подменялся бы сведениями
-- комплекта.
SELECT o.inn, o.name, o.short_name, o.ogrn, o.okved, o.region,
       o.meta AS org_meta,
       s.reporting_type, s.standard, s.unit_code, s.unit_source, s.knd,
       s.correction_version, s.source, s.digit_grouping, s.meta
FROM organization o
JOIN src_file s ON s.inn = o.inn AND s.report_year = %(year)s
                AND s.standard = %(standard)s AND s.is_actual
WHERE o.inn = %(inn)s
-- **Комплект документа прежде доставки агрегатора.** За год их два, и взятый
-- произвольно комплект агрегатора лишал бы документ единицы измерения,
-- вида отчётности, сведений аудиторского заключения и типа эмитента:
-- у него этого нет вовсе.
ORDER BY source_rank(s.source)
LIMIT 1
"""

# Прочие организации базы: их наименования нужны правилу «заключение
# об одной организации не называет другую». Стандарт здесь не при чём —
# организация одна на оба.
_OTHER_ORGANIZATIONS = """
SELECT name, short_name FROM organization WHERE inn <> %(inn)s
"""

_ASSESSMENT = """
SELECT * FROM assessment
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = %(d)s
"""

_GROUPS = """
SELECT * FROM assessment_group WHERE assessment_id = %(id)s
ORDER BY nominal_weight DESC, group_code
"""

_METRICS = """
SELECT * FROM assessment_metric WHERE assessment_id = %(id)s
ORDER BY group_code, metric_code
"""

_FLAGS = "SELECT * FROM assessment_flag WHERE assessment_id = %(id)s ORDER BY flag_code"

# Сигналы упорядочены по весу: надзорные первыми. Порядок задан здесь, а не
# в коде сборки документа: он свойство данных, а не оформления.
_SIGNALS = """
SELECT * FROM assessment_signal WHERE assessment_id = %(id)s
ORDER BY CASE level WHEN 'supervisory' THEN 0 ELSE 1 END, signal_code
"""

_PERIODS = """
SELECT DISTINCT report_date FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s ORDER BY report_date DESC LIMIT 3
"""

_METRIC_VALUES = """
SELECT report_date, metric_code, value, status, confidence, reason, reason_code
FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
ORDER BY metric_code, report_date DESC
"""

# Контроли качества по комплектам организации. Считается различённое
# срабатывание, а не строка журнала: приложение показывает, что выполнялось.
#
# **Строка журнала и срабатывание — разные вещи там, где запись историческая.**
# Контроли качества переписываются при каждом прогоне и дают по строке
# на объект, а расхождение периодов, расхождение знака и перезапись остаются
# историей и кладутся заново при каждой загрузке. Считая строки, приложение
# печатало бы число наших прогонов: у ООО «Магнит» 104 записи на 28
# пересмотренных величин.
#
# Период и объект контроля выбираются вместе с исходом: без них по сводке
# нельзя установить, какой период отбракован и какая строка не сошлась,
# а именно это от сводки и требуется. Объект — строки отчётности, которых
# контроль касался; контроль, применённый к комплекту целиком, их не имеет.
#
# **Считаются записи той версии кода, которой комплект загружен.** Журнал —
# доказательная база, и удалять из него нельзя; но запись, порождённая
# разбором, которого больше нет, о комплекте уже не говорит. У ЛСР так
# остались 18 записей «расхождение сравнительных данных» от 18.09.2026:
# 12 из них знаковые, 6 — следы наших же исправлений справочника, а роли
# периодов во всех 18 совпадают, то есть столкновения не было ни одного.
# Сегодняшняя загрузка того же комплекта даёт их иными кодами.
#
# Сверяется версия комплекта, а не текущая версия процесса: иначе любой
# коммит обнулял бы сводку по комплекту, загруженному до него, — молчаливый
# ноль вместо сведений.
_CHECKS = """
SELECT d.check_code, d.severity, d.status, d.report_date, s.report_year,
       count(DISTINCT (d.form_code, d.line_code, d.previous_value,
                       d.new_value, d.message)) AS runs,
       array_remove(array_agg(DISTINCT d.line_code), NULL) AS line_codes
FROM dq_log d
JOIN src_file s ON s.id = d.src_file_id
WHERE d.inn = %(inn)s AND s.standard = %(standard)s AND s.is_actual
  AND d.code_version IS NOT DISTINCT FROM s.code_version
GROUP BY d.check_code, d.severity, d.status, d.report_date, s.report_year
ORDER BY d.check_code, d.severity, d.status, d.report_date DESC NULLS LAST
"""

# Записи прежних разборов: в сводку не идут, но называются числом — молча
# пропасть они не вправе, иначе сводка выдаёт неполноту за чистоту.
_CHECKS_OLD = """
SELECT count(*) AS records, count(DISTINCT d.check_code) AS codes
FROM dq_log d
JOIN src_file s ON s.id = d.src_file_id
WHERE d.inn = %(inn)s AND s.standard = %(standard)s AND s.is_actual
  AND d.code_version IS DISTINCT FROM s.code_version
"""

# Строки, раскрытые за отчётный период: по ним видно, какие из обязательных
# величин раздела «Фактическая база» вообще существуют у этой организации.
# Величины отчётного периода вместе с силой опознания и ссылкой на примечание:
# величина, взятая из примечания, называется в документе вместе с номером
# примечания и наименованием его строки. Без ссылки покрытие процентов
# не совпадает ни с одной строкой отчёта о прибыли или убытке, и читатель
# не понимает почему: у Автодора в форме 414, а начислено 54 382.
_DISCLOSED = """
SELECT DISTINCT line_code, form_code, value, recognition, note_number,
       note_source_name
FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = %(d)s
  AND value IS NOT NULL
"""

# Комплекты отчётности вместе со статусом. Карантин отбирается здесь, а не
# в запросе: приложение обязано назвать и принятые комплекты, и отбракованные,
# иначе «Ограничения» и «Происхождение документа» противоречат друг другу.
_SOURCES = """
SELECT report_year, reporting_type, correction_version, status, knd, loaded_at,
       quarantine_reason
FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND is_actual
ORDER BY report_year DESC
"""


@dataclass(frozen=True, slots=True)
class StopFactorView:
    """Стоп-фактор так, как его называет документ **своего** стандарта.

    Коды у РСБУ и МСФО одни и те же — правило одно, — а формулировки свои:
    у РСБУ они в `scoring.yaml`, у МСФО в `ifrs_issuer_type.yaml`. Взять
    формулировку чужого справочника значило бы напечатать в заключении
    по консолидированной отчётности утверждение, писанное для бухгалтерской:
    приметы у такого текста нет, и правило чистоты стандарта его не поймает.
    """

    code: str
    name: str
    statement: str
    metrics: tuple[str, ...]
    # Класс, которым стоп-фактор ограничивает оценку. Нужен документу затем,
    # чтобы не утверждать ограничения, которого нет: у Сегежи класс E, а
    # формулировка отрицательного оборотного капитала обещает «класс ограничен
    # средним» — ограничение слабее присвоенного класса и его не меняет.
    cap: str | None = None


def cap_is_weaker(cap: str | None, data: "ReportData") -> bool:
    """Слабее ли ограничение стоп-фактора присвоенного класса.

    Порядок классов берётся у справочника **своего** стандарта: шкалы у РСБУ
    и МСФО разные, и сравнивать коды вне своей шкалы нельзя. Пустое
    ограничение и незнакомый код отвечают «нет»: оговорка о недействующем
    ограничении при неизвестном порядке была бы утверждением без основания.
    """
    if not cap or not data.class_code or cap == data.class_code:
        return False
    if data.standard is Standard.IFRS:
        from finlib.normalize.ifrs_metrics import load_ifrs_metrics

        order = [item.code for item in load_ifrs_metrics().classes]
    else:
        from finlib.scoring.definitions import load_scoring

        order = [item.code for item in load_scoring().classes]
    if cap not in order or data.class_code not in order:
        return False
    return order.index(cap) < order.index(data.class_code)


def stop_factor_of(code: str, standard: Standard) -> StopFactorView | None:
    """Стоп-фактор по коду в справочнике своего стандарта; None — такого нет."""
    if standard is Standard.IFRS:
        from finlib.normalize.ifrs_issuer_type import load_issuer_types

        found = next(
            (item for item in load_issuer_types().stop_factors if item.code == code),
            None,
        )
        return (
            StopFactorView(
                code=found.code,
                name=found.name,
                statement=" ".join(found.statement.split()),
                # Показателя у стоп-фактора по разделу заключения нет вовсе:
                # условие его — слова аудитора, и величины, которую следовало
                # бы назвать в фактической базе, за ним не стоит.
                metrics=(found.metric,) if found.metric else (),
                cap=found.cap,
            )
            if found is not None
            else None
        )
    from finlib.scoring.definitions import StopEffect, load_scoring

    scoring = load_scoring()
    factor = next((item for item in scoring.stop_factors if item.code == code), None)
    return (
        StopFactorView(
            code=factor.code,
            name=factor.name,
            statement=" ".join(factor.statement.split()),
            metrics=tuple(factor.metrics),
            # У РСБУ ограничение задано последствием: «до низшего» — это низший
            # класс методики, и назвать его надо тем же способом, каким его
            # применяет расчёт.
            cap=factor.cap
            if factor.effect is StopEffect.CAP_AT_CLASS
            else scoring.lowest_class,
        )
        if factor is not None
        else None
    )


@dataclass(frozen=True, slots=True)
class FlagConflict:
    """Флаг и стоп-фактор, построенные на одних и тех же показателях."""

    flag_code: str
    flag_name: str
    stop_factor_code: str
    stop_factor_name: str
    metrics: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class MetricRow:
    """Показатель в приложении: значения по периодам и роль в оценке."""

    code: str
    name: str
    unit: str
    group_name: str
    values: dict[date, Decimal | None]
    reasons: dict[date, str | None]
    # Машинная причина отказа по периодам: по ней определяется семейство
    # отказа, то есть что с ним делать. Текст причины для этого не годится —
    # он написан читателю, а не разбору.
    reason_codes: dict[date, str | None]
    included: bool
    score: Decimal | None
    level_score: Decimal | None
    dynamics_score: Decimal | None
    # **На скольких точках посчитана динамика.** Сорок процентов балла
    # показателя — динамика, и два наблюдения от пяти в документе выглядели
    # одинаково: число считалось, хранилось и не печаталось нигде.
    periods_used: int
    exclusion_reason: str | None
    exclusion_kind: str | None

    @property
    def missing_data(self) -> bool:
        """Исключён из-за нехватки данных, а не решением методики."""
        if self.exclusion_kind is not None:
            return self.exclusion_kind == ExclusionKind.NO_DATA.value
        return bool(
            self.exclusion_reason and NOT_CALCULATED_MARK in self.exclusion_reason
        )

    def refusal_kind(self, report_date: date):
        """Семейство отказа за период: из него следует, что делать.

        `data_missing` — величины нет в отчётности, и из отказа следует запрос
        к организации; `our_gap` и `not_applicable` — запрашивать нечего.
        Семейство объявлено методикой (`refusals.yaml`), а не выведено здесь:
        документ обязан говорить о показателе одним голосом в «Ограничениях»
        и в «Вопросах».
        """
        from finlib.quality.refusals import Kind, load_refusals
        from finlib.report.refusals import METRIC_REASONS

        code = self.reason_codes.get(report_date)
        if code is None:
            return Kind.DATA_MISSING
        found = load_refusals().reason(METRIC_REASONS.get(code, code))
        return found.kind if found is not None else Kind.DATA_MISSING

    @property
    def exclusion_rank(self) -> int:
        """Место причины в иерархии: стоп-фактор, шкала, дублирование, данные."""
        if self.exclusion_kind is None:
            return len(EXCLUSION_ORDER)
        return ExclusionKind(self.exclusion_kind).rank


@dataclass
class ReportData:
    """Всё, что нужно документу, прочитанное один раз."""

    inn: str
    report_date: date
    standard: Standard
    organization: dict
    unit_name: str
    assessment: dict | None
    groups: list[dict] = field(default_factory=list)
    metrics: list[MetricRow] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    periods: list[date] = field(default_factory=list)
    derived: list[dict] = field(default_factory=list)
    metric_rows: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    # Записи журнала прежних разборов: в сводку не идут, но называются числом.
    # Удалённое от забытого не отличить, и молчание о них читалось бы как
    # «журнал чист».
    checks_superseded: dict = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    # Раскрытые строки отчётного периода вместе с величинами: раздел
    # «Фактическая база» собирается расчётом и печатает не только состав,
    # но и значения.
    line_values: dict[str, Decimal] = field(default_factory=dict)
    # Ссылка на примечание у тех величин, что взяты из примечания: номер
    # и наименования строк. Читатель, сверяющий заключение с отчётностью,
    # обязан видеть источник — иначе величина не совпадает ни с одной строкой
    # формы и выглядит ошибкой расчёта.
    line_notes: dict[str, tuple[int, str]] = field(default_factory=dict)
    # Опознавательные слова наименований **всех прочих** организаций базы.
    # Перечень ведётся не в коде и не в методике: организации приходят
    # загрузкой, и знать их наименования может только база.
    other_issuer_words: frozenset[str] = frozenset()

    @property
    def audit_signals(self) -> tuple:
        """Сигналы аудиторского заключения — предписанными формулировками.

        Сведения лежат в `src_file.meta` комплекта, формулировки берутся
        из методики сейчас. Раздел 4 без них у ФосАгро был пуст, хотя мнение
        аудитора модифицировано, а раздел 7 на этот пустой раздел ссылался.
        """
        from finlib.normalize.ifrs_audit import load_audit_policy
        from finlib.sources.ifrs_audit import audit_from_meta

        audit = audit_from_meta(self.organization.get("meta"))
        if audit is None:
            return ()
        return audit.signal_hits(load_audit_policy())

    @property
    def issuer_type(self) -> str | None:
        """Тип эмитента, определённый по этому комплекту.

        Лежит в `src_file.meta`: расчёт по фактам документа не видит, а от типа
        зависят состав показателей и применимость стоп-факторов. Документ
        обязан его называть — иначе отказ по ликвидности у девелопера читается
        как пробел данных.
        """
        return ((self.organization or {}).get("meta") or {}).get("issuer_type")

    @property
    def disclosed_lines(self) -> frozenset[str]:
        """Коды строк, раскрытых за отчётный период."""
        return frozenset(self.line_values)

    @property
    def class_code(self) -> str | None:
        """Присвоенный класс; None — если основание оказалось недостаточным."""
        return self.assessment["class_code"] if self.assessment else None

    def text_context(self, lines_catalog, reporting_type, catalog):
        """Собирает контекст для контроля утверждений текста.

        Проверка опирается на расчёт, а не на то, как текст выглядит: строки
        набора форм, показатели с отменённым знаменателем, показатели в днях
        и неприменимые шаблонные блоки берутся отсюда.
        """
        from finlib.llm.textcheck import TextContext
        from finlib.metrics.definitions import Unit
        from finlib.report.policy import load_policy

        # Набор известных строк и наименования величин берутся **по стандарту**:
        # у МСФО кодов строк, утверждённых приказом, нет вовсе, статья названа
        # позицией унифицированной модели, а показатели живут в своём
        # справочнике. Перечень РСБУ, применённый к тексту по МСФО, объявил бы
        # каждую статью неизвестной строкой и не нашёл бы ни одного показателя.
        foreign_names: frozenset[str] = frozenset()
        foreign_versions: frozenset[str] = frozenset()
        if self.standard is Standard.IFRS:
            known, names = self._ifrs_text_names()
            foreign_names, foreign_versions = self._foreign_marks(
                catalog, set(names.values())
            )
        else:
            known = frozenset(
                code
                for code in {item.code for item in lines_catalog.lines}
                if lines_catalog.has(code, reporting_type)
            )
            names = {item.code: item.name for item in catalog.metrics}
        refused = {
            row["metric_code"]: catalog.require(row["metric_code"]).name
            for row in self.metric_rows
            if row["reason_code"] in ("negative_denominator", "sign_change")
            and catalog.get(row["metric_code"]) is not None
        }
        days = frozenset(
            item.name for item in catalog.metrics if item.unit is Unit.DAYS
        )
        conflict = self.flag_conflict()
        return TextContext(
            known_lines=known,
            refused_metrics=refused,
            days_metrics=days,
            forbidden_templates=self.forbidden_templates(catalog),
            # Столкновение флага и стоп-фактора фиксируется в «Ключевом
            # выводе» нами, а модели остаётся не противоречить ему.
            flag_conflict=conflict.message if conflict is not None else None,
            fact_base=self.fact_base_codes(),
            # В тексте документа показатель назван наименованием, а не кодом,
            # и правило состава ищет его так же.
            metric_names=names,
            # Приметы чужого стандарта: наименования и версии РСБУ, которых
            # в справочниках МСФО нет. Правило блокирующее, потому что дефект
            # этого класса повторился семь раз.
            foreign_names=foreign_names,
            foreign_versions=foreign_versions,
            # Опознавательные слова других организаций базы: заключение
            # об одной организации не называет другую.
            other_issuers=self._other_issuers(),
            questions=load_policy().questions,
        )

    def _other_issuers(self) -> frozenset[str]:
        """Опознавательные слова других организаций без слов своей.

        Своя организация исключается целиком: «ФосАгро» в заключении
        о ФосАгро — это она сама, а наименования пересекаются словами
        («Группа ЛСР» и «Группа Черкизово» родовым словом уже не считаются).
        """
        from finlib.report.policy import load_policy

        rule = load_policy().other_issuers
        mine = {
            rule.phrase_of(name)
            for name in (
                self.organization.get("name") or "",
                self.organization.get("short_name") or "",
            )
        }
        return frozenset(
            phrase
            for phrase in self.other_issuer_words
            if phrase and not any(other and phrase in other for other in mine)
        )

    def _ifrs_text_names(self) -> tuple[frozenset[str], dict[str, str]]:
        """Известные статьи МСФО и наименования величин для контроля текста.

        Статьи берутся из обоих справочников ветки: позиции форм живут
        в `ifrs_lines.yaml`, величины примечаний — в `ifrs_note_lines.yaml`.
        Наименования показателей — из справочника показателей МСФО: у РСБУ
        своих кодов нет ни одного общего, и перечень одного стандарта,
        применённый к другому, не нашёл бы ни одной величины.
        """
        from finlib.normalize.ifrs_lines import load_ifrs_lines
        from finlib.normalize.ifrs_metrics import load_ifrs_metrics
        from finlib.normalize.ifrs_note_lines import load_note_lines

        positions = load_ifrs_lines().positions
        note_lines = load_note_lines().lines
        known = frozenset(
            {item.code for item in positions} | {item.code for item in note_lines}
        )
        names = {item.code: item.name for item in positions}
        names |= {item.code: item.name for item in note_lines}
        names |= {item.code: item.name for item in load_ifrs_metrics().metrics}
        return known, names

    def _foreign_marks(
        self, catalog, own_names: set[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Наименования и версии РСБУ, которых у МСФО нет.

        Сверяется разность, а не перечень РСБУ целиком: «Выручка» и «Чистый
        долг» есть в обоих справочниках, и запрещать их значило бы запретить
        писать о выручке. Запрещено то, что принадлежит только РСБУ:
        подставленное наименование выглядит верным и означает другое.

        **Берутся наименования показателей, а не строк.** Наименование строки
        РСБУ — обычное словосочетание бухгалтерского языка: «кредиторская
        задолженность» и «оценочные обязательства» стоят в оговорках самой
        методики МСФО, и запрет на них ловил бы русскую речь, а не чужой
        стандарт. У строки есть своя примета — код, и её ловит отдельное
        правило. Наименование показателя устроено иначе: «Коэффициент текущей
        ликвидности» против «Текущая ликвидность» — это два разных справочника,
        и в документе МСФО первое означает, что тезис собран не по той методике.

        Отбрасывается и то, что входит частью в наименование МСФО: такое
        вхождение — совпадение слов, а не чужое наименование.
        """
        from finlib.utils import marked_by

        foreign = {item.name for item in catalog.metrics} - own_names
        names = frozenset(
            item
            for item in foreign
            if len(item.strip()) >= 3
            and not any(
                marked_by(own, (item,), str.casefold) for own in own_names if own.strip()
            )
        )

        from finlib.normalize.ifrs_metrics import load_ifrs_metrics
        from finlib.scoring.definitions import load_scoring

        own_versions = {load_ifrs_metrics().version}
        versions = {catalog.version, load_scoring().version} - own_versions
        return names, frozenset(versions)

    def forbidden_templates(self, catalog) -> dict[str, str]:
        """Шаблонные блоки, условие применения которых не выполнено.

        Оговорка показателя идёт в заключение, только если показатель
        участвовал в расчёте. Прежде шаблонный блок печатался без проверки
        применимости: у организации с положительным капиталом документ
        разъяснял, чем плох отрицательный.
        """
        used = {item.code for item in self.metrics}
        found: dict[str, str] = {}
        for metric in catalog.metrics:
            if metric.code in used or not metric.note:
                continue
            found[" ".join(metric.note.split())] = (
                f"показатель «{metric.name}» в расчёте не участвовал"
            )
        return found

    def scale_of(self, code: str) -> int:
        """Разрядность отображения показателя: одна на весь документ."""
        from finlib.metrics.definitions import load_metrics

        return load_metrics().scale_for(code)

    @property
    def breadth_reason(self) -> str | None:
        """Почему балльная оценка не формируется; None — основание достаточно."""
        return self.assessment.get("breadth_reason") if self.assessment else None

    @property
    def stop_factor_code(self) -> str | None:
        """Код стоп-фактора, назначившего ограничение класса."""
        return self.assessment["stop_factor_code"] if self.assessment else None

    @property
    def stop_factor_codes(self) -> tuple[str, ...]:
        """Коды **всех** сработавших стоп-факторов, назначивший класс первым.

        Документ называл один — тот, чьё ограничение младше, — и у Сегежи
        два обстоятельства из трёх до читателя не доходили вовсе. Порядок
        не произволен: назначивший класс стоит первым, остальные за ним
        в порядке методики.
        """
        if not self.assessment:
            return ()
        listed = tuple(self.assessment.get("stop_factor_codes") or ())
        first = self.stop_factor_code
        if first is None:
            return listed
        return (first, *(code for code in listed if code != first))

    @property
    def score_in_appendix(self) -> bool:
        """Приводится ли балл в приложении.

        Без присвоенного класса балл не приводится **нигде** — ни в разделе 1,
        ни в приложении. При отрицательном собственном капитале он бывает
        высоким: у организации с крошечным балансом коэффициенты вырождаются,
        и «балл 67, класс не присвоен» читается как противоречие, хотя
        арифметика верна. Балл без класса ничего не сообщает и вводит
        в заблуждение.
        """
        if self.assessment is None or self.assessment["total_score"] is None:
            return False
        # Класс, присвоенный стоп-фактором при узком основании, балла
        # не раскрывает: балльной оценки просто нет, и число рядом с классом
        # читалось бы как её итог.
        if self.assessment.get("breadth_reason"):
            return False
        return bool(self.class_code)

    @property
    def score_in_summary(self) -> bool:
        """Приводится ли балл в разделе «Ключевой вывод».

        При сработавшем стоп-факторе — нет: класс определён стоп-фактором,
        а не баллом, и соседство «балл 85, класс E» подрывает доверие
        к оценке. В приложении балл при этом остаётся, с пометкой
        «до применения стоп-фактора».
        """
        return self.score_in_appendix and not self.stop_factor_code

    @property
    def accepted_sources(self) -> list[dict]:
        """Комплекты, принятые в расчёт."""
        return [item for item in self.sources if item["status"] != "quarantine"]

    @property
    def quarantined_sources(self) -> list[dict]:
        """Комплекты, отбракованные контролями качества.

        Прежде приложение перечисляло их среди принятых, и «Происхождение
        документа» противоречило разделу «Ограничения анализа», где тот же
        комплект назван невключённым.
        """
        return [item for item in self.sources if item["status"] == "quarantine"]

    @property
    def blocking_failures(self) -> list[dict]:
        """Провалившиеся блокирующие контроли **комплекта этого документа**.

        Провал блокирующего контроля означает, что комплект в расчёт не пошёл,
        и умолчать об этом в «Ключевом выводе» нельзя: читатель обязан знать,
        что часть отчётности отбракована, а не просто отсутствует.

        **Но контроли чужого комплекта здесь не место.** У ФосАгро «Ключевой
        вывод» по годовой отчётности перечислял четыре отказа промежуточного
        комплекта — опознание позиции, полноту вида отчётности, статью сверх
        порога, сходимость итога, — то есть отказы, к комплекту этого документа
        не относящиеся вовсе. Отбракованный комплект другого периода при этом
        не замалчивается: он назван в «Ограничениях анализа» и в приложении.
        """
        return [
            item
            for item in self.checks
            if item["severity"] == "blocking"
            and item["status"] == "fail"
            and item["report_year"] == self.report_date.year
        ]

    def months_since_report(self, generated_at: datetime) -> int:
        """Разрыв между отчётной датой и днём формирования документа."""
        from finlib.report.policy import months_between

        return months_between(self.report_date, generated_at.date())

    def fact_base_codes(self, policy=None) -> tuple[str, ...]:
        """Величины, обязательные в разделе «Фактическая база».

        Состав задан методикой (`report.yaml`), а не выбором модели: разделы
        интерпретации, рисков и вопросов строятся именно на них.
        """
        from finlib.report.policy import load_policy

        policy = policy if policy is not None else load_policy()
        calculated = {
            row["metric_code"] for row in self.metric_rows if row["status"] == "ok"
        }
        return policy.fact_base_of(self.standard).required(
            self.disclosed_lines, calculated
        )

    def flag_conflict(self, flags_catalog=None, scoring=None) -> "FlagConflict | None":
        """Столкновение флага и стоп-фактора, построенного на его показателях.

        Стоп-фактор при этом не смягчается: флаг, отменяющий стоп-фактор, был бы
        путём обхода оценки. Столкновение фиксируется отдельным абзацем
        и требует ручной проверки.
        """
        from finlib.metrics.definitions import load_metrics
        from finlib.scoring.definitions import load_flags, load_scoring

        code = self.stop_factor_code
        if not code or not self.flags:
            return None
        flags_catalog = flags_catalog if flags_catalog is not None else load_flags()
        scoring = scoring if scoring is not None else load_scoring()
        factor = next(
            (item for item in scoring.stop_factors if item.code == code), None
        )
        if factor is None:
            return None
        catalog = load_metrics()
        for row in self.flags:
            flag = flags_catalog.get(row["flag_code"])
            if flag is None or flag.conflict_statement is None:
                continue
            shared = flag.conflicts_with(factor)
            if not shared:
                continue
            names = ", ".join(
                f"«{catalog.require(item).name}»"
                for item in shared
                if catalog.get(item) is not None
            )
            message = (
                " ".join(flag.conflict_statement.split())
                .replace("{stop_factor}", factor.name)
                .replace("{metrics}", names)
            )
            return FlagConflict(
                flag_code=flag.code,
                flag_name=flag.name,
                stop_factor_code=factor.code,
                stop_factor_name=factor.name,
                metrics=shared,
                message=message,
            )
        return None

    @property
    def missing_metrics(self) -> list[MetricRow]:
        """Показатели, не вошедшие в балл из-за нехватки данных."""
        return [item for item in self.metrics if item.missing_data]

    @property
    def excluded_by_methodology(self) -> list[MetricRow]:
        """Показатели, исключённые решением методики, а не нехваткой данных.

        Порядок — фиксированная иерархия причин: стоп-фактор, отсутствие шкалы
        уровня, дублирование с другим показателем, отсутствие данных.
        """
        found = [
            item
            for item in self.metrics
            if not item.included and not item.missing_data and item.exclusion_reason
        ]
        return sorted(found, key=lambda item: (item.exclusion_rank, item.code))


def load_report_data(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
    standard: Standard = Standard.RSBU,
    catalog=None,
    scoring=None,
) -> ReportData:
    """Читает из базы всё, что понадобится документу."""
    from finlib.metrics.definitions import load_metrics
    from finlib.normalize.lines import load_lines
    from finlib.scoring.definitions import load_scoring

    # Справочник показателей берётся по стандарту: коды `cur_liq`, `equity_ratio`,
    # `debt_total` и `net_debt` есть у обоих, а означают разное — в приложении
    # по МСФО печаталось «Коэффициент текущей ликвидности» вместо «Текущая
    # ликвидность», а показатели, которых у РСБУ нет, из таблицы выпадали.
    if catalog is None:
        if standard is Standard.IFRS:
            from finlib.metrics.ifrs_view import IfrsMetricsView

            catalog = IfrsMetricsView()
        else:
            catalog = load_metrics()
    scoring = scoring if scoring is not None else load_scoring()

    params = {"inn": inn, "standard": standard.value}
    periods = [row["report_date"] for row in fetch_all(_PERIODS, params, conn=conn)]
    if not periods:
        raise ValueError(f"для ИНН {inn} нет рассчитанных показателей")
    target = report_date or periods[0]

    organization = fetch_one(
        _ORGANIZATION, {"inn": inn, "year": target.year, "standard": standard.value}, conn=conn
    )
    if organization is None:
        raise ValueError(f"для ИНН {inn} нет комплекта отчётности за {target.year} год")

    header = fetch_one(
        _ASSESSMENT, {"inn": inn, "standard": standard.value, "d": target}, conn=conn
    )
    groups: list[dict] = []
    flags: list[dict] = []
    signals: list[dict] = []
    scored: dict[str, dict] = {}
    if header is not None:
        by_id = {"id": header["id"]}
        groups = fetch_all(_GROUPS, by_id, conn=conn)
        flags = fetch_all(_FLAGS, by_id, conn=conn)
        signals = fetch_all(_SIGNALS, by_id, conn=conn)
        scored = {row["metric_code"]: row for row in fetch_all(_METRICS, by_id, conn=conn)}

    values = fetch_all(
        _METRIC_VALUES, {**params, "dates": periods}, conn=conn
    )
    by_metric: dict[str, list[dict]] = {}
    for row in values:
        by_metric.setdefault(row["metric_code"], []).append(row)

    # Наименования групп — тоже по стандарту: у МСФО их четыре, и они свои.
    group_names = (
        {code: item.name for code, item in catalog.groups.items()}
        if standard is Standard.IFRS
        else {code: item.name for code, item in scoring.groups.items()}
    )
    metrics = [
        _metric_row(code, by_metric[code], scored.get(code), catalog, group_names)
        for code in sorted(by_metric)
        if catalog.get(code) is not None
    ]

    # **Величина позиции берётся из формы, объявленной у позиции.** Один код
    # правомерно стоит в двух формах МСФО — неденежные корректировки потока
    # повторяют статьи отчёта о прибыли, — и это два разных факта. Отображение
    # по коду без формы оставляло то из двух, что пришло позже: у Сегежи налог
    # на прибыль равен −4 784 в отчёте о прибыли и +4 784 в потоке, и документ
    # печатал бы произвольное из них.
    from finlib.normalize.ifrs_forms import pick_by_form

    disclosed, _ = pick_by_form(
        fetch_all(_DISCLOSED, {**params, "d": target}, conn=conn), standard
    )
    return ReportData(
        inn=inn,
        report_date=target,
        standard=standard,
        organization=dict(organization),
        # Единица **комплекта**, а не единица РСБУ: «тыс. руб.» в заключении
        # по консолидированной отчётности, составленной в миллионах, —
        # ошибка в тысячу раз, и ни один контроль сходимости её не ловит.
        unit_name=load_lines().units.name_of(organization["unit_code"]),
        assessment=dict(header) if header is not None else None,
        groups=groups,
        metrics=metrics,
        flags=flags,
        signals=signals,
        periods=periods,
        metric_rows=values,
        derived=[
            row
            for row in values
            if row["status"] == "ok" and catalog.get(row["metric_code"]) is None
        ],
        checks=fetch_all(_CHECKS, params, conn=conn),
        checks_superseded=fetch_one(_CHECKS_OLD, params, conn=conn) or {},
        sources=fetch_all(_SOURCES, params, conn=conn),
        line_values={row["line_code"]: row["value"] for row in disclosed},
        line_notes={
            row["line_code"]: (row["note_number"], row["note_source_name"] or "")
            for row in disclosed
            if row["recognition"] == "note" and row["note_number"] is not None
        },
        other_issuer_words=_other_issuer_words(inn, conn),
    )


def _other_issuer_words(inn: str, conn: PgConnection | None) -> frozenset[str]:
    """Опознавательные слова наименований прочих организаций базы.

    Нужны блокирующему правилу «заключение об одной организации не называет
    другую»: наши оговорки методики несли наблюдения по набору, и в документ
    по ФосАгро попадало «у ЛСР расхождение между двумя мерами оказалось
    наибольшим».
    """
    from finlib.report.policy import load_policy

    rule = load_policy().other_issuers
    found: set[str] = set()
    for row in fetch_all(_OTHER_ORGANIZATIONS, {"inn": inn}, conn=conn):
        for name in (row["name"], row["short_name"]):
            if name:
                found.add(rule.phrase_of(name))
    return frozenset(item for item in found if item)


def _metric_row(
    code: str, points: list[dict], scored: dict | None, catalog, group_names: dict[str, str]
) -> MetricRow:
    """Собирает строку приложения по одному показателю."""
    metric = catalog.require(code)
    return MetricRow(
        code=code,
        name=metric.name,
        unit=metric.unit.value,
        group_name=group_names.get(metric.group, metric.group),
        values={
            item["report_date"]: item["value"] if item["status"] == "ok" else None
            for item in points
        },
        reasons={
            item["report_date"]: item["reason"] if item["status"] != "ok" else None
            for item in points
        },
        reason_codes={
            item["report_date"]: item["reason_code"] if item["status"] != "ok" else None
            for item in points
        },
        included=bool(scored and scored["included"]),
        score=scored["score"] if scored else None,
        level_score=scored["level_score"] if scored else None,
        dynamics_score=scored["dynamics_score"] if scored else None,
        periods_used=int(scored["periods_used"]) if scored else 0,
        exclusion_reason=scored["exclusion_reason"] if scored else None,
        exclusion_kind=scored["exclusion_kind"] if scored else None,
    )
