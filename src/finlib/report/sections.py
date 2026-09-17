"""Разбор текста модели на разделы 2–6.

Модель возвращает markdown с заголовками вида «### 2. Фактическая база».
Документ собирается по разделам, а не вставкой текста целиком: так видно,
что модель не пропустила раздел и не сочинила своего, и так заголовки
в документе оформляются стилем Word, а не решётками.

Недостающий раздел — ошибка, а не повод молча отдать неполный документ.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Разделы, которые пишет модель. Номера и наименования заданы в шаблонах
# prompts/ и должны совпадать с ними дословно.
#
# Раздела 4 здесь нет: «Риски и надзорные сигналы» собирается расчётом наравне
# с «Ключевым выводом» и «Предложениями по дальнейшим действиям». Замер
# 17.09.2026 показал, что оставленный модели раздел вырождается — по ООО
# «Магнит» он свёлся к одной фразе, — а добавить ей туда нечего: формулировки
# сигналов предписаны, величины и отсечки набраны расчётом.
EXPECTED: tuple[tuple[int, str], ...] = (
    (2, "Фактическая база"),
    (3, "Аналитическая интерпретация"),
    (5, "Ограничения анализа"),
    (6, "Вопросы к организации"),
)

_HEADING = re.compile(r"^#{1,6}\s*(\d)\.\s*(.+?)\s*$", re.MULTILINE)


class MissingSectionError(RuntimeError):
    """Модель не написала обязательный раздел."""

    def __init__(self, missing: list[int]) -> None:
        listed = ", ".join(str(item) for item in missing)
        super().__init__(f"в ответе модели нет обязательных разделов: {listed}")
        self.missing = missing


@dataclass(frozen=True, slots=True)
class Section:
    """Раздел заключения: номер, заголовок и абзацы."""

    number: int
    title: str
    paragraphs: tuple[str, ...]


def split_sections(text: str) -> list[Section]:
    """Разбирает ответ модели на разделы; отсутствие обязательного — ошибка."""
    found: dict[int, Section] = {}
    matches = list(_HEADING.finditer(text))
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        found[number] = Section(number, match.group(2), _paragraphs(body))

    missing = [number for number, _ in EXPECTED if number not in found]
    if missing:
        raise MissingSectionError(missing)

    # Порядок задаётся нами, а не моделью: перестановка разделов в документе
    # недопустима, даже если модель выдала их не по порядку.
    return [
        Section(number, title, found[number].paragraphs) for number, title in EXPECTED
    ]


def _paragraphs(body: str) -> tuple[str, ...]:
    """Абзацы раздела; пустые строки и маркеры списков убираются."""
    found: list[str] = []
    for line in body.split("\n"):
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^[-*•]\s+", "— ", cleaned)
        found.append(cleaned)
    return tuple(found)
