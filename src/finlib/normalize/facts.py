"""Правило приоритета величины при столкновении: одно на всех загрузчиков.

**Два пути к одному ответу расходятся.** Приоритет решает, чья величина
останется в `fact_report`, и написанный дважды он однажды разойдётся: один
загрузчик предпочтёт первоисточник, другой — свежую загрузку, и какая величина
попала в документ, будет зависеть от порядка запуска.

Правило трёхступенчатое, и порядок ступеней — часть правила:

1. **Состояние комплекта.** Факт отбракованного комплекта уступает факту
   принятого: величина карантинного комплекта в расчёт не идёт вовсе,
   и занятое ею место пустует. У О'КЕЙ, ГК «Автодор» и ГК «Самолёт» PDF
   отбракован, величины агрегатора за тот же период не записывались правилом
   источника, и период оставался без величин совсем — хуже, чем с величинами
   агрегатора.
2. **Источник.** Первоисточник старше агрегатора. Сравнительная графа свежего
   отчёта эмитента — его последняя редакция, а запись агрегатора — прочтение
   отчёта того года: у ГК «Автодор» операционная прибыль 2024 года равна 2 272
   по сравнительной графе отчёта за 2025 год и 448 по строке агрегатора,
   и разница проходит по цепочке до прибыли, то есть эмитент её пересмотрел.
3. **Роль периода** — при равном источнике: отчётное значение не затирается
   сравнительным, как было и раньше.

Без второй ступени третья решала бы наоборот: величина агрегатора приходит
отчётной и затёрла бы пересмотренную сравнительную.
"""

from collections.abc import Mapping
from decimal import Decimal

# **Выборка одного комплекта периода обязана называть предпочтение источника.**
# За один год у организации теперь два актуальных комплекта: доставка документом
# и доставка агрегатором. Оба актуальны намеренно — величины, которых нет
# в документе, приходят от агрегатора, — и выборка «один комплект года»
# без порядка возвращала произвольный: сведения аудиторского заключения, тип
# эмитента и единица измерения исчезали, потому что у комплекта агрегатора
# их нет вовсе. Поймал это эталонный прогон в ту же ночь, когда универсум
# был загружен.
#
# Порядок тот же, что у приоритета величин: первоисточник старше агрегатора.
SOURCE_PREFERENCE = "ORDER BY source_rank(source)"


def source_preference(alias: str = "") -> str:
    """Порядок выборки комплекта: первоисточник прежде агрегатора.

    `alias` — псевдоним таблицы `src_file` в запросе, если он есть. Подстановкой
    строки этого не сделать: «source_rank» сам содержит слово «source», и замена
    испортила бы имя функции.
    """
    column = f"{alias}.source" if alias else "source"
    return f"ORDER BY source_rank({column})"


# **Единица комплекта называется одним кодом на весь проект.** Прежде её
# набирали порознь тезисы, сигналы, список наблюдения и выгрузка — и наборы
# разошлись: графы списка печатали «млн руб.», а основания рядом с ними
# «тыс. руб.», потому что единая точка печати без названной единицы
# подставляла умолчанием тысячи. Величина одна, и код, считающий её, один.
#
# Комплект выбирается тем же предпочтением источника, что и величины: за год
# их два, и у доставки агрегатора единица своя.
_UNIT = f"""
SELECT s.unit_code FROM src_file s
JOIN fact_report f ON f.src_file_id = s.id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND f.report_date = %(date)s
  AND s.status <> 'quarantine' AND s.is_actual
{source_preference("s")}
LIMIT 1
"""


def unit_name_of(inn: str, report_date, conn, standard: str = "ifrs") -> str:
    """Наименование денежной единицы комплекта; пусто — комплекта нет.

    Пустая строка означает «единицу назвать нечем», и печатать по ней деньги
    нельзя: единая точка печати такую величину не печатает вовсе. Это и есть
    правильный исход — «663 888 тыс. руб.» там, где в отчётности миллионы,
    не ловит ни один контроль сходимости.
    """
    from finlib.db import fetch_one
    from finlib.normalize.lines import load_lines

    row = fetch_one(
        _UNIT, {"inn": inn, "date": report_date, "standard": standard}, conn=conn
    )
    code = (row or {}).get("unit_code")
    if not code:
        return ""
    try:
        return load_lines().units.name_of(code)
    except (KeyError, ValueError):
        return ""

def debt_undisclosed(
    lines: Mapping[str, tuple[Decimal | None, str]], has_bonds: bool
) -> bool:
    """Нераскрыт ли долг: ноль по всем строкам заёмных средств при выпусках.

    **Четвёртый признак «ноль не означает нуля», и он единственный внешний.**
    Три прежних арифметические — ноль ломает тождество отчётности, ноль
    у итога при ненулевом составе, ноль постоянный у эмитента, — и ни один
    из них на строках заёмных средств не срабатывает: ноль там согласован
    со всем остальным. Этот опирается на перечень выпусков: **выпуск
    в обращении и есть заём**, и нуля по заёмным средствам у такого эмитента
    не бывает.

    `lines` — величина и способ получения по каждой строке долга. Признак
    относится к нулю **агрегатора**: у первоисточника ноль означает ноль,
    и правило чтения нулей объявлено у вида отчёта, а не у нас.

    Строка, которой нет вовсе, признака не даёт сама по себе: отсутствие
    величины уже означает «не раскрыто», и показатель по ней не считается.
    Признак нужен ровно для обратного случая — величина есть, равна нулю
    и выглядит раскрытой.
    """
    from finlib.normalize.cbonds_mapping import load_cbonds_mapping

    if not has_bonds or not lines:
        return False
    rule = any(
        report.zero_reading is not None and report.zero_reading.debt_zero_with_bonds
        for report in load_cbonds_mapping().reports.values()
    )
    if not rule:
        return False
    known = [(value, source) for value, source in lines.values() if value is not None]
    if not known:
        return False
    return all(
        value == 0 and source == AGGREGATOR for value, source in known
    )


# Способ получения, у которого ноль означает и нераскрытие. Правило объявлено
# у вида отчёта (`cbonds_mapping.yaml`, `zero_reading`), а здесь названо имя
# источника: у первоисточника ноль означает ноль.
AGGREGATOR = "cbonds"

# Условие `ON CONFLICT ... DO UPDATE`: приоритет и признак изменения.
# Строкой, а не запросом: его вставляют в свой `INSERT` оба загрузчика,
# и второго определения правила быть не должно.
PRIORITY_WHERE = """
WHERE (
        set_rank(EXCLUDED.src_file_id) < set_rank(fact_report.src_file_id)
        OR (
          set_rank(EXCLUDED.src_file_id) = set_rank(fact_report.src_file_id)
          AND (
            source_rank(EXCLUDED.recognition) < source_rank(fact_report.recognition)
            OR (
              source_rank(EXCLUDED.recognition) = source_rank(fact_report.recognition)
              AND period_rank(EXCLUDED.period_role)
                  <= period_rank(fact_report.period_role)
            )
          )
        )
      )
  AND (fact_report.value IS DISTINCT FROM EXCLUDED.value
       OR fact_report.period_role IS DISTINCT FROM EXCLUDED.period_role
       OR fact_report.src_file_id IS DISTINCT FROM EXCLUDED.src_file_id
       OR fact_report.source_line_code IS DISTINCT FROM EXCLUDED.source_line_code
       OR fact_report.recognition IS DISTINCT FROM EXCLUDED.recognition
       OR fact_report.note_number IS DISTINCT FROM EXCLUDED.note_number)
"""
