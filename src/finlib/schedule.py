"""Прогон по расписанию: одно определение на прогон, сводку и отчёт изменений.

**Отметки «по расписанию» журнал не несёт.** Агент launchd запускает
ежедневный прогон в 10:00 по рабочим дням
(`scripts/ru.finanalysis.daily-run.plist`), и прогоном по расписанию
считается прогон, стартовавший в рабочий день между 10:00:00 и
`SCHEDULED_SLACK` после. Правило объявлено, и его слабость тоже: ручной
прогон, запущенный ровно в 10:00, будет принят за плановый.

Прежде правило жило в сводке (`eval/status_run.py`), а ежедневному прогону
понадобилось то же различение — какой отчёт изменений дня главный. Второе
определение разошлось бы с первым.
"""

from datetime import datetime, time, timedelta

# Час агента и допуск: launchd стартует в пределах секунд, пробуждение
# машины добавляет минуты.
SCHEDULED_AT = time(10, 0)
SCHEDULED_SLACK = timedelta(minutes=5)


def is_scheduled(started: datetime | None) -> bool:
    """Стартовал ли прогон по расписанию: рабочий день, 10:00 и не позже допуска."""
    if started is None:
        return False
    started = started.astimezone()
    if started.weekday() >= 5:
        return False
    edge = datetime.combine(started.date(), SCHEDULED_AT, tzinfo=started.tzinfo)
    return edge <= started <= edge + SCHEDULED_SLACK
