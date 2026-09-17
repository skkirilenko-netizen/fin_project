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

    @property
    def laid_out(self) -> str:
        """Текст страницы, собранный из кусков с сохранением промежутков.

        Плоский текст извлекателя склеивает колонки одиночным пробелом,
        и разделитель разрядов становится неотличим от разделителя колонок:
        у Автодора «Амортизация 737 562» — это 737 и 562 за два года,
        а читалось как семьсот тридцать семь тысяч, то есть амортизация
        в шестьдесят процентов активов.

        Координаты эту разницу показывают прямо. Между «73» и «7»
        промежуток девять пунктов на два знака, между «7» и «562» —
        семьдесят три пункта на один знак. Ширина знака берётся из самого
        документа как медиана по всем соседним парам, а лишний промежуток
        переводится в пробелы. Правило «внутри числа пробел один, между
        колонками два и более» у разбора уже есть, и оно начинает работать.

        Пусто, когда координат нет: у готовой текстовой выгрузки их
        не бывает, и подменять её нечем.
        """
        if not self.pieces:
            return ""
        rows = _rows_of(self.pieces)
        unit = _character_width(rows)
        return "\n".join(_row_text(row, unit) for row in rows)

    def columns(self, packed: str) -> tuple[tuple[str, float], ...]:
        """Ячейки визуальной строки, знаки которой без пробелов равны данным."""
        if not self.pieces:
            return ()
        rows = _rows_of(self.pieces)
        unit = _character_width(rows)
        for row in rows:
            if "".join("".join(item.text.split()) for item in row) != packed:
                continue
            return _row_columns(row, unit)
        return ()


# Насколько близко по вертикали куски считаются одной строкой. Строки форм
# отстоят друг от друга на тринадцать пунктов и более; половина этого
# с запасом отделяет соседние строки от дрожания базовой линии внутри одной.
_SAME_ROW = 3.0

# Со скольких пробелов промежуток считается границей колонки. Один пробел —
# разделитель разрядов внутри числа, два и более — граница колонки; это
# правило разбора, и здесь оно только воспроизводится.
_COLUMN_GAP = 2


def _rows_of(pieces: tuple[TextPiece, ...]) -> list[list[TextPiece]]:
    """Куски страницы, сгруппированные в строки сверху вниз."""
    rows: list[list[TextPiece]] = []
    for piece in sorted(pieces, key=lambda item: (-item.top, item.left)):
        if rows and abs(rows[-1][0].top - piece.top) <= _SAME_ROW:
            rows[-1].append(piece)
            continue
        rows.append([piece])
    return [sorted(row, key=lambda item: item.left) for row in rows]


def _character_width(rows: list[list[TextPiece]]) -> float:
    """Ширина знака в пунктах — медиана по соседним кускам.

    Берётся из самого документа, а не задаётся числом: у разных шрифтов
    и кеглей она разная, а нужна она только для перевода промежутка
    в пробелы.

    Берётся нижняя четверть, а не середина: половина соседств на странице
    таблицы — это промежутки между колонками, и медиана меряла бы их,
    а не знак. У соседних кусков одного слова шаг равен ширине знака,
    и они лежат в нижней части распределения.
    """
    advances = [
        (second.left - first.left) / len(first.text)
        for row in rows
        for first, second in zip(row, row[1:], strict=False)
        if first.text and second.left > first.left
    ]
    if not advances:
        return 1.0
    advances.sort()
    return max(advances[len(advances) // 4], 0.1)


def _row_text(row: list[TextPiece], unit: float) -> str:
    """Строка из кусков: промежуток между ними переведён в пробелы."""
    parts = [row[0].text]
    for first, second in zip(row, row[1:], strict=False):
        parts.append(" " * _spaces_between(first, second, unit) + second.text)
    return "".join(parts).rstrip()


def _spaces_between(first: TextPiece, second: TextPiece, unit: float) -> int:
    """Сколько пробелов умещается в промежутке между двумя кусками."""
    extra = (second.left - first.left) - len(first.text) * unit
    return max(0, round(extra / unit))


def _row_columns(row: list[TextPiece], unit: float) -> tuple[tuple[str, float], ...]:
    """Ячейки строки с правым краем каждой: куски, разделённые промежутком.

    Отдаётся правый край, а не левый: величины в таблице выровнены по правому
    краю, и левый у них разный — «60 021» начинается на 422 пункте, а «21»
    той же колонки на 440. По левому краю колонка не опознаётся, по правому
    опознаётся.
    """
    columns: list[list] = [[row[0].text, row[0].left]]
    for first, second in zip(row, row[1:], strict=False):
        spaces = _spaces_between(first, second, unit)
        if spaces >= _COLUMN_GAP:
            columns.append([second.text, second.left])
        else:
            # Внутри колонки промежуток сохраняется как есть: у «73» и «7»
            # его нет вовсе — это одно число, — а у «млн руб.» он один.
            columns[-1][0] += " " * spaces + second.text
    return tuple(
        (str(text).strip(), float(left) + len(str(text)) * unit)
        for text, left in columns
    )


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
        """Плоский текст документа, как его отдал извлекатель.

        Перестроенный по координатам текст (`PdfPage.laid_out`) разбору
        не подставляется: он сдвигает все смещения в документе, а на них
        опираются поиск заголовков форм, окно шапки, поиск валюты и дат.
        Замена основы разбора — отдельная работа; координаты применяются
        точечно, через `columns_of`.
        """
        return "\n".join(page.text for page in self.pages)

    def page_at(self, offset: int) -> int:
        """Номер страницы, на которой стоит этот знак плоского текста."""
        position = 0
        for page in self.pages:
            position += len(page.text) + 1
            if offset < position:
                return page.number
        return self.pages[-1].number if self.pages else 0

    @property
    def pages_without_text(self) -> tuple[int, ...]:
        """Номера страниц, у которых текстового слоя нет вовсе.

        Страница без слоя — не пустая страница, а страница, содержимое
        которой мы не видим. У Автодора так потерялась вся сторона пассива:
        баланс занимает страницы 8 и 9, слой есть только у восьмой, и разбор
        кончался на «Всего активов». Ни один контроль этого не заметил —
        актив сошёлся сам с собой, а пассива просто не существовало.
        """
        return tuple(page.number for page in self.pages if not page.text.strip())

    def columns_of(self, line: str) -> tuple[tuple[str, float], ...]:
        """Ячейки строки таблицы с их положением; пусто — строка не опознана.

        Отвечает на единственный вопрос, на который плоский текст ответить
        не может: где в строке кончается одна колонка и начинается другая.
        Строка ищется по совпадению знаков без пробелов — это равенство,
        а не догадка, и найдётся она ровно тогда, когда координаты есть.

        Положение отдаётся вместе с текстом: по нему ячейка кладётся
        в свою колонку периода, а колонка примечаний отбрасывается — она
        стоит левее всех колонок с величинами у всех строк сразу.
        """
        wanted = "".join(line.split())
        if not wanted:
            return ()
        for page in self.pages:
            found = page.columns(wanted)
            if found:
                return found
        return ()

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
    положение куска на странице.

    **Режим раскладки pypdf не годится, и это проверено.** Он печатает
    границы колонок промежутками, что разбору как раз и нужно, но платит
    за это разрывом слов («консолид ированного», «нео тъемлем ой»)
    и потерей повёрнутого текста — у ЛСР и Сегежи опознание после него
    падает вдвое. Границы колонок берутся из координат, а текст — из
    обычного режима.

    Дополнение `crypto` обязательно: часть отчётности приходит
    с шифрованием AES, и без него файл не читается вовсе.
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
