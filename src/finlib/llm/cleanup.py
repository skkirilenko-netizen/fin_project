"""Снятие технических идентификаторов с проверенного текста.

Два требования выглядят несовместимыми. Постпроверка сверяет пару «число —
код», и для этого модель обязана ставить код при каждом числе. Читатель
заключения технических кодов видеть не должен: «(equity_ratio_chg_abs)»
в тексте — мусор, а в разделе «Вопросы к организации» ещё и бессмыслица.

Они совместимы, если развести моменты. Модель пишет размеченный текст,
постпроверка работает по нему, и только затем разметка снимается.

Снимается **только код**, а не скобка целиком: по требуемому формату
величина изменения приводится в тех же скобках — «снизилось с 0,10 до 0,04
(chg_abs −0,06, или chg_pct −59,0 %)», — и выброс скобки унёс бы саму
величину. После снятия остаётся «(−0,06, или −59,0 %)».

Код строки отчётности — «(1600)» — остаётся: для бухгалтерской отчётности
это обычная ссылка, а не технический мусор, и читатель по ней находит строку
в приложении.
"""

import logging
import re
from collections.abc import Iterable
from functools import lru_cache

logger = logging.getLogger(__name__)

# Идентификатор по виду: латиница с подчёркиванием. Голый код без
# подчёркивания («cur_liq» его имеет, «roa» — нет) опознаётся по справочнику.
_SHAPED = re.compile(r"(?<![\w-])[a-z0-9]*[a-z][a-z0-9]*_[a-z0-9_]+(?![\w-])")

# Скобка, в которой после снятия кода не осталось ничего содержательного.
_EMPTY_BRACKETS = re.compile(r"[(\[]\s*[,:;—–-]?\s*[)\]]")

# Хвост разметки, оставшийся от снятого кода: «(: −0,06)», «(, или −59,0 %)»,
# «(— −1,81)». Дефис принимается только отделённым пробелом: слитный
# с цифрой — знак самой величины, и съесть его значило бы поменять число
# на противоположное. Длинное и среднее тире минусом не бывают никогда.
_LEADING_PUNCT = re.compile(
    r"([(\[])\s*(?:[,:;]|[\u2014\u2013]|-(?=\s))\s*"
)

_SPACES = re.compile(r"[ \t]{2,}")
_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?)\]])")
_AFTER_OPEN = re.compile(r"([(\[])\s+")
_DOUBLED_PUNCT = re.compile(r"([.,;:!?])\1+")


@lru_cache(maxsize=1)
def known_codes() -> frozenset[str]:
    """Коды показателей и производных величин из методики.

    Голый код без подчёркивания по виду не отличить от обычного латинского
    слова, и запрещать всю латынь нельзя: наша же оговорка о показателе
    «Чистый долг к прибыли от продаж» содержит слово EBITDA, и модель обязана
    привести её дословно.
    """
    from finlib.metrics.definitions import load_metrics

    catalog = load_metrics()
    found: set[str] = set()
    for metric in catalog.metrics:
        found.add(metric.code)
        found.update(f"{metric.code}_{suffix}" for suffix in ("chg_abs", "chg_pct"))
    for code in catalog.derived.change.lines:
        found.update(f"{code}_{suffix}" for suffix in ("chg_abs", "chg_pct"))
    for code in catalog.derived.share.lines:
        found.add(f"{code}_share")
    return frozenset(found)


def _pattern(codes: Iterable[str]) -> re.Pattern[str]:
    """Регулярное выражение для перечня кодов, длинные первыми."""
    ordered = sorted(codes, key=len, reverse=True)
    if not ordered:
        return re.compile(r"(?!x)x")
    body = "|".join(re.escape(code) for code in ordered)
    return re.compile(rf"(?<![\w-])(?:{body})(?![\w-])")


def strip_identifiers(text: str, codes: Iterable[str] | None = None) -> str:
    """Убирает технические коды, сохраняя величины и оставляя текст читаемым."""
    known = frozenset(codes) if codes is not None else known_codes()
    cleaned = _SHAPED.sub("", text)
    cleaned = _pattern(known).sub("", cleaned)
    return tidy(cleaned)


def tidy(text: str) -> str:
    """Убирает следы снятия: пустые скобки, двойные пробелы и знаки."""
    cleaned = _EMPTY_BRACKETS.sub("", text)
    cleaned = _LEADING_PUNCT.sub(r"\1", cleaned)
    cleaned = _AFTER_OPEN.sub(r"\1", cleaned)
    cleaned = _SPACES.sub(" ", cleaned)
    cleaned = _BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _DOUBLED_PUNCT.sub(r"\1", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.split("\n"))


def has_identifiers(text: str, codes: Iterable[str] | None = None) -> list[str]:
    """Оставшиеся технические идентификаторы; пустой список — текст чист.

    Применяется к **очищенному** тексту: в сыром коды обязаны быть, иначе
    постпроверке не с чем сверять пару «число — код».
    """
    known = frozenset(codes) if codes is not None else known_codes()
    found = [match.group() for match in _SHAPED.finditer(text)]
    found += [match.group() for match in _pattern(known).finditer(text)]
    return sorted(set(found))
