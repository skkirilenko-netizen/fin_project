"""Проверка утверждений о классе и балле.

Класс — фиксированная арифметика методики (инвариант 2), и модель не вправе
его пересматривать. Проверить это числовой сверкой нельзя: буква класса
числом не является, а балл при сработавшем стоп-факторе в блоки не подаётся
вовсе, так что «балл 85» модель может назвать только выдумав.

Сверка идёт с блоком ОЦЕНКА — тем же, что получила модель. Три нарушения:
назван класс, отличный от присвоенного; назван класс, когда класс
не присвоен; назван итоговый балл, когда он не раскрыт.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

# Строки блока ОЦЕНКА, по которым узнаётся вынесенный вердикт.
_CLASS = re.compile(r"^Класс:\s*([A-E])\b", re.MULTILINE)
_NO_CLASS = re.compile(r"^Класс не присвоен", re.MULTILINE)
_SCORE = re.compile(r"^Общий балл:", re.MULTILINE)

# Упоминание класса в ответе: слово «класс» в любой форме и буква рядом.
# Буква может стоять в кавычках или скобках — «класс «C»», «класс (C)».
_CLASS_MENTION = re.compile(r"\bкласс\w*\s*[«\"'(]?\s*([A-E])\b", re.IGNORECASE)

# Итоговый балл. Балл группы под запрет не подпадает: он в блоках есть
# и назван там своим именем — «балл по группе».
_TOTAL_SCORE = re.compile(
    r"\b(?:общий|итогов\w+|интегральн\w+|суммарн\w+|совокупн\w+)\s+балл\w*"
    r"|\bбалл\w*\s+организации",
    re.IGNORECASE,
)


class VerdictViolation(StrEnum):
    """Чем именно плохо утверждение о вердикте."""

    WRONG_CLASS = "wrong_class"
    CLASS_NOT_ASSIGNED = "class_not_assigned"
    SCORE_WITHHELD = "score_withheld"


@dataclass(frozen=True, slots=True)
class Verdict:
    """Вердикт, каким он передан модели в блоке ОЦЕНКА."""

    class_code: str | None
    score_disclosed: bool


@dataclass(frozen=True, slots=True)
class VerdictClaim:
    """Утверждение о классе или балле, расходящееся с оценкой."""

    text: str
    violation: VerdictViolation
    context: str
    expected: str | None = None

    def describe(self) -> str:
        """Человеческое объяснение, чем утверждение плохо."""
        if self.violation is VerdictViolation.WRONG_CLASS:
            return (
                f"назван класс {self.text}, тогда как присвоен класс {self.expected}"
            )
        if self.violation is VerdictViolation.CLASS_NOT_ASSIGNED:
            return f"назван класс {self.text}, тогда как класс не присвоен"
        return (
            f"«{self.text}» — итоговый балл в текст не выносится: "
            "он не раскрыт во входных данных"
        )


def parse_verdict(blocks: str) -> Verdict:
    """Читает вердикт из блока ОЦЕНКА."""
    match = _CLASS.search(blocks)
    return Verdict(
        class_code=match.group(1) if match else None,
        score_disclosed=_SCORE.search(blocks) is not None,
    )


def find_verdict_claims(text: str, verdict: Verdict) -> list[VerdictClaim]:
    """Утверждения о классе и балле, расходящиеся с оценкой."""
    found: list[VerdictClaim] = []
    for match in _CLASS_MENTION.finditer(text):
        named = match.group(1).upper()
        if verdict.class_code is None:
            found.append(
                VerdictClaim(
                    named,
                    VerdictViolation.CLASS_NOT_ASSIGNED,
                    _context_of(text, match.span()),
                )
            )
        elif named != verdict.class_code:
            found.append(
                VerdictClaim(
                    named,
                    VerdictViolation.WRONG_CLASS,
                    _context_of(text, match.span()),
                    verdict.class_code,
                )
            )
    if not verdict.score_disclosed:
        for match in _TOTAL_SCORE.finditer(text):
            found.append(
                VerdictClaim(
                    match.group(),
                    VerdictViolation.SCORE_WITHHELD,
                    _context_of(text, match.span()),
                )
            )
    return found


def _context_of(text: str, span: tuple[int, int], width: int = 60) -> str:
    """Окружение утверждения — чтобы в журнале было видно, где оно появилось."""
    start = max(0, span[0] - width)
    end = min(len(text), span[1] + width)
    return " ".join(text[start:end].split())
