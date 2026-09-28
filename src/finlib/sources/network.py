"""Сбой сети на нашей стороне: отличить от отказа источника, переждать, назвать.

**Нет сети — не отказ источника.** 28.09.2026 у машины пропала сеть, и снимок
рейтингов принял пять `ConnectError` подряд (имя хоста не разрешилось) за отказ
Cbonds: прогон дня остановился с записью «источник отказал», хотя до источника
не дошёл ни один запрос. Такой сбой ждут и повторяют — в минутах, а не
в секундах: сеть возвращается не сразу, — и называют «нет сети».

Судится по причине в цепочке исключения, а не по тексту: имя хоста
не разрешилось (`socket.gaierror`) или сеть и маршрут до узла недоступны
(`ENETDOWN`, `ENETUNREACH`, `EHOSTUNREACH`). Сброс соединения и отказ
в соединении сюда не входят: там нас услышал узел на той стороне.
"""

import errno
import logging
import socket
import time
from collections.abc import Callable

from finlib.config import settings

logger = logging.getLogger(__name__)

# Ошибки сокета, при которых запрос не покинул машину.
_OFFLINE_ERRNO: frozenset[int] = frozenset(
    {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH}
)


class NetworkDownError(Exception):
    """Сети нет у нас: запрос не ушёл дальше машины, источник не спрашивался."""


def network_down(failure: BaseException) -> bool:
    """Сбой на нашей стороне: имя не разрешилось или сеть недоступна."""
    seen: set[int] = set()
    current: BaseException | None = failure
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        if isinstance(current, OSError) and current.errno in _OFFLINE_ERRNO:
            return True
        current = current.__cause__ or current.__context__
    return False


def send[T](call: Callable[[], T], what: str) -> T:
    """Выполняет обращение; при отсутствии сети ждёт и повторяет.

    Повторов `network_retries`, пауза `network_retry_pause_s`, растущая
    с номером повтора. Повторы не проходят предел частоты и не считаются
    в расходе запросов: до источника они не дошли. Сеть так и не появилась —
    `NetworkDownError`; всякий другой сбой поднимается как был.
    """
    retries = max(settings.network_retries, 0)
    for retry in range(retries + 1):
        try:
            return call()
        except Exception as failure:
            if not network_down(failure):
                raise
            if retry == retries:
                raise NetworkDownError(
                    f"нет сети: {what} — {failure} (повторов {retries})"
                ) from failure
            pause = settings.network_retry_pause_s * (retry + 1)
            logger.warning(
                "нет сети (%s): %s, повтор %d из %d через %.0f с",
                what,
                failure,
                retry + 1,
                retries,
                pause,
            )
            time.sleep(pause)
    raise AssertionError("цикл повторов завершается возвратом или исключением")
