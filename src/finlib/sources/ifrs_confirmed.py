"""Ранее подтверждённое опознание: что человек уже сказал об этом эмитенте.

**Справочник и подтверждение — утверждения разной силы, и слабейшего хватает
для повторного комплекта того же эмитента.** Справочник утверждает: строка
с таким наименованием означает это у любого эмитента. Подтверждение
утверждает меньше: у **этого** эмитента эта строка означает это. Для решения
о повторном комплекте второго достаточно — человек уже смотрел ту же строку
в той же форме той же организации, и ничего нового машина не угадывает.

Границы объявлены и не раздвигаются:

- **только тот же эмитент.** Подтверждение у чужого эмитента знанием
  не считается: там та же формулировка может означать другое, и именно
  поэтому подтверждённый код ядром не становится;
- **только то же наименование.** Сравнение идёт приведёнными
  наименованиями — тем же `normalize_name`, которым опознаётся любой синоним
  справочника. Иначе «Выручка7» со сноской и «Выручка» были бы разными
  строками, хотя это одна;
- **только та же форма и раздел.** Проверка та же, что у автомата и у
  человека на экране разметки: притязание строки чужой формы или чужого
  раздела отклоняется (`ifrs_claims.fold`), и подтверждение здесь не сильнее
  справочника.

Индекс строки в ключ не входит намеренно: у следующего года он означает
другую строку. Переносит наименование — не место в таблице.

Величины собираются тем же кодом, что при разметке (`IssuerMarkup.values`
и `extras`): второй способ превратить решения человека в величины неминуемо
разошёлся бы с первым.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.normalize.lines import normalize_name
from finlib.sources.ifrs_extract import Extraction
from finlib.sources.ifrs_inbox import DocumentProfile

logger = logging.getLogger(__name__)

_CONFIRMED = """
SELECT DISTINCT ON (form_code, source_name)
       form_code, source_name, match_key, code, relation, related_codes, report_date
FROM ifrs_line_confirmation
WHERE inn = %(inn)s
ORDER BY form_code, source_name, confirmed_at DESC, id DESC
"""

_REFRESH_MATCH_KEYS = """
UPDATE ifrs_line_confirmation SET match_key = %(key)s WHERE id = %(id)s
"""

_MATCH_KEYS = """
SELECT id, source_name, match_key FROM ifrs_line_confirmation WHERE inn = %(inn)s
"""


def match_key(source_name: str) -> str:
    """Ключ сопоставления подтверждения со строкой отчётности.

    **Запись о том, что было, и ключ поиска — разные вещи.** `source_name`
    хранится дословно и не правится никогда: у части подтверждений в нём стоит
    мусор разбора — «Поступление от выпуска акций 19 51 012 -», — и это запись
    того, что разбор тогда прочитал. Ключ же вычисляется **текущим разбором**
    и потому пересчитывается: когда разбор научится отрезать номер примечания
    и величины, прежние подтверждения начнут находиться, а доказательная база
    останется нетронутой.

    Пока правило приведения одно — то же `normalize_name`, которым опознаётся
    любой синоним справочника. Функция названа отдельно, чтобы правка правила
    была в одном месте, а не в трёх.
    """
    return normalize_name(source_name)


def refresh_match_keys(inn: str, conn=None) -> int:
    """Пересчитывает ключи сопоставления этого эмитента; возвращает число правок.

    Вызывается перед присестом разметки: ключ — величина производная, и она
    обязана соответствовать нынешнему разбору, а не тому, который действовал
    в день подтверждения.
    """
    from finlib.db import execute, fetch_all

    try:
        rows = fetch_all(_MATCH_KEYS, {"inn": inn}, conn=conn)
    except Exception as failure:  # noqa: BLE001 — разметка работает и без базы
        logger.warning("ключи сопоставления не прочитаны: %s", failure)
        return 0
    changed = 0
    for row in rows:
        wanted = match_key(row["source_name"])
        if row["match_key"] == wanted:
            continue
        execute(_REFRESH_MATCH_KEYS, {"key": wanted, "id": row["id"]}, conn=conn)
        changed += 1
    if changed:
        logger.info("%s: ключей сопоставления пересчитано %d", inn, changed)
    return changed


@dataclass(frozen=True, slots=True)
class ConfirmedFact:
    """Величина подтверждённой строки, готовая лечь в факты комплекта.

    **Форма берётся у строки, а не у позиции.** Один код в двух формах
    правомерен, и одна и та же позиция в балансе и в потоке — два разных
    факта: форма входит в ключ `fact_report`, и подменять её формой позиции
    значило бы записать величину потока под балансовой датой.
    """

    form: str
    code: str
    values: tuple[Decimal, ...]
    source_name: str
    index: int


@dataclass(frozen=True, slots=True)
class Confirmed:
    """Строки, опознанные по ранее подтверждённому у этого же эмитента."""

    # Ключ строки нынешнего комплекта → код, присвоенный человеком прежде.
    codes: dict[tuple[str, int], str] = field(default_factory=dict)
    # Величины по кодам справочника и сверх него — то же, что даёт разметка.
    values: dict[str, Decimal] = field(default_factory=dict)
    extras: dict[str, Decimal] = field(default_factory=dict)
    # Величины подтверждённых строк для записи фактами. Точное присвоение
    # и специфическая статья — величина самой строки, и она идёт в факты
    # с пометкой источника опознания. Детализация и агрегат не идут:
    # величина детализации уже входит в свою позицию, а агрегат покрывает
    # несколько позиций, и разложить его нечем — фактом под одним кодом
    # он был бы величиной не той статьи.
    facts: tuple[ConfirmedFact, ...] = ()
    # Отчётные даты комплектов, на которых эти решения были приняты: без них
    # «принято по ранее подтверждённому» не проверить глазами.
    from_reports: tuple[str, ...] = ()

    @property
    def rows(self) -> frozenset[tuple[str, int]]:
        """Ключи строк, о которых человек уже сказал, чем они являются."""
        return frozenset(self.codes)

    def describe(self) -> str:
        """Однострочная сводка для журнала."""
        if not self.codes:
            return "ранее подтверждённых строк нет"
        where = ", ".join(self.from_reports) or "—"
        return (
            f"принято по ранее подтверждённому строк {len(self.codes)}; "
            f"подтверждения комплектов: {where}"
        )


def load_confirmed(
    inn: str | None,
    extraction: Extraction,
    profile: DocumentProfile,
    catalog: IfrsCatalog | None = None,
    conn=None,
) -> Confirmed:
    """Собирает ранее подтверждённое опознание для строк этого комплекта.

    Без организации подтверждений нет: привязать их не к чему. Недоступная
    база — тоже не ошибка приёма, но и не молчание: причина пишется
    в журнал, а решение остаётся прежним, то есть строже.
    """
    if inn is None:
        return Confirmed()
    catalog = catalog or load_ifrs_lines()
    from finlib.db import fetch_all

    try:
        rows = fetch_all(_CONFIRMED, {"inn": inn}, conn=conn)
    except Exception as failure:  # noqa: BLE001 — приём работает и без базы
        logger.warning("ранее подтверждённое опознание не прочитано: %s", failure)
        return Confirmed()

    # Сопоставление идёт по ключу, вычисленному разбором, а наименование
    # остаётся записью о том, что было. Ключа ещё нет — считается на месте
    # тем же правилом: графа заведена позже подтверждений.
    by_name = {
        (item["form_code"], item["match_key"] or match_key(item["source_name"])): item
        for item in rows
    }
    if not by_name:
        return Confirmed()

    from finlib.sources.ifrs_markup import Decision, IssuerMarkup, Relation

    markup = IssuerMarkup(inn, Path(""), profile, extraction)
    codes: dict[tuple[str, int], str] = {}
    reports: set[str] = set()
    facts: dict[tuple[str, int], ConfirmedFact] = {}
    for row in extraction.unrecognised:
        found = by_name.get((row.form, match_key(row.source_name)))
        if found is None:
            continue
        relation = found["relation"] or Relation.EXACT.value
        code = found["code"]
        if relation == Relation.NOT_A_LINE.value:
            markup.dismissed[row.key] = Decision.NOT_A_LINE
        elif relation == Relation.PART_OF.value:
            markup.parts[row.key] = code
        elif relation == Relation.AGGREGATE_OF.value:
            markup.aggregates[row.key] = tuple(found["related_codes"] or (code,))
        elif relation == Relation.SPECIFIC.value:
            markup.dismissed[row.key] = Decision.SPECIFIC
            markup.specific[row.key] = code
        else:
            markup.assignments[row.key] = code
        if relation in (Relation.EXACT.value, Relation.SPECIFIC.value) and row.values:
            facts[row.key] = ConfirmedFact(
                row.form, code, row.values, row.source_name, row.index
            )
        codes[row.key] = code
        reports.add(f"{found['report_date']:%d.%m.%Y}")

    # Притязание, отклонённое правилом формы и раздела, знанием не является:
    # строка возвращается в неопознанные, как если бы подтверждения не было.
    rejected = markup.rejects(catalog)
    for key in rejected:
        codes.pop(key, None)
        facts.pop(key, None)
        markup.assignments.pop(key, None)
        markup.parts.pop(key, None)
        markup.aggregates.pop(key, None)

    found = Confirmed(
        codes=codes,
        values=markup.values(catalog),
        extras=markup.extras(catalog),
        from_reports=tuple(sorted(reports)),
        facts=tuple(facts.values()),
    )
    logger.info("%s: %s", inn, found.describe())
    return found
