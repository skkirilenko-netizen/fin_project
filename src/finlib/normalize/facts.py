"""Правило приоритета величины при столкновении: одно на всех загрузчиков.

**Два пути к одному ответу расходятся.** Приоритет решает, чья величина
останется в `fact_report`, и написанный дважды он однажды разойдётся: один
загрузчик предпочтёт первоисточник, другой — свежую загрузку, и какая величина
попала в документ, будет зависеть от порядка запуска.

Правило двухступенчатое, и порядок ступеней — часть правила:

1. **Источник.** Первоисточник старше агрегатора. Сравнительная графа свежего
   отчёта эмитента — его последняя редакция, а запись агрегатора — прочтение
   отчёта того года: у ГК «Автодор» операционная прибыль 2024 года равна 2 272
   по сравнительной графе отчёта за 2025 год и 448 по строке агрегатора,
   и разница проходит по цепочке до прибыли, то есть эмитент её пересмотрел.
2. **Роль периода** — при равном источнике: отчётное значение не затирается
   сравнительным, как было и раньше.

Без первой ступени вторая решала бы наоборот: величина агрегатора приходит
отчётной и затёрла бы пересмотренную сравнительную.
"""

# Условие `ON CONFLICT ... DO UPDATE`: приоритет и признак изменения.
# Строкой, а не запросом: его вставляют в свой `INSERT` оба загрузчика,
# и второго определения правила быть не должно.
PRIORITY_WHERE = """
WHERE (
        source_rank(EXCLUDED.recognition) < source_rank(fact_report.recognition)
        OR (
            source_rank(EXCLUDED.recognition) = source_rank(fact_report.recognition)
            AND period_rank(EXCLUDED.period_role) <= period_rank(fact_report.period_role)
        )
      )
  AND (fact_report.value IS DISTINCT FROM EXCLUDED.value
       OR fact_report.period_role IS DISTINCT FROM EXCLUDED.period_role
       OR fact_report.src_file_id IS DISTINCT FROM EXCLUDED.src_file_id
       OR fact_report.source_line_code IS DISTINCT FROM EXCLUDED.source_line_code
       OR fact_report.recognition IS DISTINCT FROM EXCLUDED.recognition
       OR fact_report.note_number IS DISTINCT FROM EXCLUDED.note_number)
"""
