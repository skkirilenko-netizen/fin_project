"""Проверка нечисловых утверждений о состоянии показателя.

Постпроверка чисел защищает только числовые утверждения. Фразу «коэффициент
абсолютной ликвидности не рассчитывается» она пропускает — чисел в ней нет, —
хотя показатель рассчитан и равен 0,16. Модель написала так про Газпром,
приняв за факт оговорку методики.

Утверждение о нерасчёте проверяемо: рядом с ним стоит код показателя, а его
фактическое состояние известно из блока ПОКАЗАТЕЛИ. Сверяется утверждение,
а не слова: если код назван рассчитанным в блоках, заявление о нерасчёте ложно.
"""

import re
from dataclasses import dataclass

from finlib.llm.pairs import AnchorIndex

# Заявление о том, что показатель не рассчитан, не раскрыт или отсутствует.
# «не» отделено от корня: между ними встают частицы и наречия («не был
# рассчитан», «не может быть рассчитан»).
_DENIAL = re.compile(
    r"\bне\b[\s\w]{0,30}?"
    r"(?:рассчит\w*|расчит\w*|исчисл\w*|определ[её]н\w*|раскры\w*|доступ\w*|применим\w*)",
    re.IGNORECASE,
)

# Код показателя или строки рядом с заявлением: в скобках либо голым
# четырёхзначным числом («по строке 1120»).
_CODE = re.compile(r"\(([a-z0-9][a-z0-9_]*)\)|\b(\d{4})\b")

# Границы предложения: за ними говорится уже о другой величине.
_SENTENCE_END = re.compile(r"[.!?\n]")

# Указание периода в том же предложении. Показатель, рассчитанный за один год
# и не рассчитанный за другой, попадает в блок со значениями, и утверждение
# «не рассчитан за 2025 год» остаётся верным — придираться не к чему.
_PERIOD = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass(frozen=True, slots=True)
class FalseClaim:
    """Заявление о нерасчёте, опровергнутое входными данными."""

    code: str
    text: str
    context: str

    def describe(self) -> str:
        """Человеческое объяснение, чем утверждение плохо."""
        return (
            f"«{self.code}» назван нерассчитанным, но значение показателя "
            "приведено во входных данных"
        )


def find_false_claims(text: str, index: AnchorIndex, calculated: set[str]) -> list[FalseClaim]:
    """Заявления о нерасчёте при коде, который во входных данных рассчитан.

    Сверка идёт по множеству рассчитанных кодов, а не по словам: утверждение
    ложно тогда и только тогда, когда у названного кода есть значение.

    Код берётся ближайший к заявлению и только из того же предложения. Иначе
    во фразе «валюта баланса (1600) составила X. Данные по строке 1120
    не раскрыты» отрицание приписалось бы коду 1600 из предыдущего утверждения.
    """
    found: list[FalseClaim] = []
    for match in _DENIAL.finditer(text):
        sentence, offset = _sentence_around(text, match.span())
        if _PERIOD.search(sentence):
            continue
        code = _nearest_code(sentence, match.start() - offset, index)
        if code is not None and code in calculated:
            found.append(FalseClaim(code, match.group(), " ".join(sentence.split())))
    return found


def _sentence_around(text: str, span: tuple[int, int]) -> tuple[str, int]:
    """Предложение, внутри которого стоит заявление, и его смещение в тексте."""
    start = 0
    for boundary in _SENTENCE_END.finditer(text, 0, span[0]):
        start = boundary.end()
    match = _SENTENCE_END.search(text, span[1])
    end = match.start() if match else len(text)
    return text[start:end], start


def _nearest_code(sentence: str, position: int, index: AnchorIndex) -> str | None:
    """Ближайший к заявлению код, известный по входным блокам."""
    best: tuple[int, str] | None = None
    for match in _CODE.finditer(sentence):
        code = (match.group(1) or match.group(2)).casefold()
        if index.get(code) is None:
            continue
        distance = min(abs(match.start() - position), abs(match.end() - position))
        if best is None or distance < best[0]:
            best = (distance, code)
    return best[1] if best else None
