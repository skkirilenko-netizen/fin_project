"""Извлечение текста из PDF: интерфейс и реализация на pypdf.

Извлекатель отделён от разбора форм намеренно. Библиотеки для PDF
различаются тем, насколько точно держат раскладку по колонкам, и замена
одной на другую — обычное дело: pypdf отдаёт текст быстро и без бинарных
зависимостей, pdfminer.six точнее восстанавливает координаты, poppler
работает извне процесса. Разбор форм от этого выбора зависеть не должен.

Поэтому наружу отдаётся не строка, а **строки с координатами**. Координата
нужна разбору таблиц: колонки различаются положением по горизонтали, тогда
как в плоском тексте они разделены пробелами — и ширина промежутка остаётся
единственным признаком, который у разных извлекателей разный. Пока разбор
работает по плоскому тексту, но данные для перехода уже есть.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TextPiece:
    """Кусок текста с координатами на странице.

    Координаты в точках PDF, начало — левый нижний угол страницы.
    Извлекатель, не умеющий координат, ставит нули и объявляет это
    через `has_coordinates`.
    """

    text: str
    left: float
    top: float


@dataclass
class PdfPage:
    """Страница документа: плоский текст и куски с координатами."""

    number: int
    text: str
    pieces: tuple[TextPiece, ...] = field(default_factory=tuple)


@dataclass
class PdfDocument:
    """Разобранный документ либо причина, по которой он не прочитан.

    Причины две, и путать их нельзя. **Слой пуст** — документ отсканирован,
    нужен OCR. **Файл не прочитан** — повреждён, зашифрован незнакомым
    способом или не является PDF; распознавание тут ни при чём, и предлагать
    его значило бы назвать ложную причину.
    """

    pages: tuple[PdfPage, ...] = ()
    error: str | None = None
    extractor: str = ""

    @property
    def readable(self) -> bool:
        """Прочитан ли файл; пустой слой при этом возможен."""
        return self.error is None

    @property
    def text(self) -> str:
        """Плоский текст документа."""
        return "\n".join(page.text for page in self.pages)

    @property
    def has_coordinates(self) -> bool:
        """Отдаёт ли извлекатель координаты кусков текста."""
        return any(page.pieces for page in self.pages)

    def describe(self) -> str:
        """Сводка о качестве извлечения — её печатает прогон приёма."""
        if not self.readable:
            return f"{self.extractor}: файл не прочитан ({self.error})"
        return (
            f"{self.extractor}: страниц {len(self.pages)}, знаков "
            f"{len(self.text)}, координаты "
            f"{'есть' if self.has_coordinates else 'не отдаются'}"
        )


class PdfExtractor(Protocol):
    """Интерфейс извлекателя: PDF на входе, страницы с координатами на выходе.

    Замена извлекателя не должна трогать разбор форм, поэтому интерфейс
    объявлен отдельно от реализации.
    """

    name: str

    def read(self, path: Path) -> PdfDocument: ...


class PypdfExtractor:
    """Извлекатель на pypdf: чистый Python, без бинарных зависимостей.

    Координаты берутся из матрицы преобразования текста: `tm[4]` и `tm[5]` —
    положение куска на странице. Дополнение `crypto` обязательно: часть
    отчётности приходит с шифрованием AES, и без него файл не читается вовсе.
    """

    name = "pypdf"

    def read(self, path: Path) -> PdfDocument:
        """Читает документ; ошибка чтения возвращается, а не поднимается."""
        from pypdf import PdfReader

        try:
            reader = PdfReader(str(path))
            pages: list[PdfPage] = []
            for number, page in enumerate(reader.pages, start=1):
                pieces: list[TextPiece] = []

                def collect(text, cm, tm, font_dict, font_size, pieces=pieces) -> None:
                    """Складывает куски текста вместе с их положением."""
                    if text and text.strip():
                        pieces.append(TextPiece(text, float(tm[4]), float(tm[5])))

                text = page.extract_text(visitor_text=collect) or ""
                pages.append(PdfPage(number, text, tuple(pieces)))
            return PdfDocument(tuple(pages), extractor=self.name)
        except Exception as exc:  # noqa: BLE001 — причина уйдёт в отказ приёма
            logger.warning("файл %s не прочитан: %s", path.name, exc)
            return PdfDocument(error=str(exc), extractor=self.name)


class PlainTextExtractor:
    """Готовая текстовая выгрузка: координат у неё нет и быть не может."""

    name = "plain"

    def read(self, path: Path) -> PdfDocument:
        """Читает текстовый файл как одну страницу без координат."""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return PdfDocument(error=str(exc), extractor=self.name)
        return PdfDocument((PdfPage(1, text),), extractor=self.name)


# Извлекатель по умолчанию. Смена сводится к замене этой строки: разбор форм
# работает с PdfDocument и о библиотеке не знает.
DEFAULT_EXTRACTOR: PdfExtractor = PypdfExtractor()


def read_document(path: Path, extractor: PdfExtractor | None = None) -> PdfDocument:
    """Читает документ подходящим извлекателем."""
    if path.suffix.lower() != ".pdf":
        return PlainTextExtractor().read(path)
    return (extractor or DEFAULT_EXTRACTOR).read(path)
