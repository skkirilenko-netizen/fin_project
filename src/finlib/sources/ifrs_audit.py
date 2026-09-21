"""Чтение аудиторского заключения: тип задания, вид мнения, разделы.

**Вид мнения объявлен заголовком раздела**, и читается он оттуда. Это третий
случай подряд, когда строение документа надёжнее поиска по словам: раздел
строки задаёт ближайший итог ниже неё, примечание находится по ссылке
из формы, вид мнения — по заголовку. Поиск по словам дал бы здесь чужой
ответ: слово «оговорка» стоит и в шаблонном абзаце об ответственности
аудитора у всех без исключения.

**Три состояния определённости, и путать их нельзя.**

| Состояние | Что значит |
|---|---|
| `determined` | заключение прочитано, вид мнения назван |
| `not_readable` | заключение в документе есть, но страницы без текстового слоя |
| `absent` | заключения в документе нет вовсе |

У Автодора заключение занимает страницы 3–7, и все пять — изображение без
текста: сказать по нему «мнение немодифицированное» значило бы выдать
незнание за результат. У промежуточной отчётности заключения может не быть
вовсе, и это другое сведение, а не то же самое.

**Обзорная проверка — отдельный тип задания.** Объём процедур там меньше
аудита, и аудитор прямо пишет, что мнения не выражает; свести её к виду
мнения значило бы выдать меньшую уверенность за большую.
"""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from finlib.normalize.ifrs_audit import AuditPolicy, load_audit_policy
from finlib.normalize.lines import normalize_name
from finlib.sources.pdf_text import PdfDocument

logger = logging.getLogger(__name__)

# Запись оглавления кончается номером страницы: заголовком заключения
# она не является, как и заголовком примечания.
_CONTENTS_TAIL = re.compile(r"\d{1,3}(?:\s*[-–—]\s*\d{1,3})?\s*$")


class Determination(StrEnum):
    """Определён ли вид мнения и почему нет."""

    DETERMINED = "determined"
    NOT_READABLE = "not_readable"
    ABSENT = "absent"


class Engagement(StrEnum):
    """Тип задания: аудит или обзорная проверка."""

    AUDIT = "audit"
    REVIEW = "review"


class TextRefusal(StrEnum):
    """Почему дословный текст раздела не извлечён.

    **Текст берётся целиком или не берётся вовсе.** Обрезанная аудиторская
    оговорка хуже отсутствующей: читатель решит, что прочёл её полностью,
    и решит это молча.
    """

    BOUNDARY_NOT_DETERMINED = "boundary_not_determined"
    PAGES_NOT_READABLE = "pages_not_readable"
    EMPTY = "empty"


TEXT_REFUSAL_TEXT: dict[TextRefusal, str] = {
    TextRefusal.BOUNDARY_NOT_DETERMINED: (
        "конец раздела не определён: следующего заголовка за ним нет"
    ),
    TextRefusal.PAGES_NOT_READABLE: (
        "внутри раздела есть страницы без текстового слоя"
    ),
    TextRefusal.EMPTY: "под заголовком раздела нет текста",
}


@dataclass(frozen=True, slots=True)
class SectionText:
    """Дословный текст раздела заключения либо отказ с названной причиной."""

    code: str
    name: str
    text: str = ""
    refusal: TextRefusal | None = None

    @property
    def found(self) -> bool:
        """Извлечён ли текст целиком."""
        return bool(self.text)

    def describe(self) -> str:
        """Однострочное описание для отчёта."""
        if self.found:
            return f"{self.name}: {len(self.text)} знаков"
        reason = TEXT_REFUSAL_TEXT.get(self.refusal, "причина не названа")
        return f"{self.name}: текст не извлечён — {reason}"


@dataclass(frozen=True, slots=True)
class AuditSignalHit:
    """Сработавший сигнал заключения: формулировка методики и основание.

    Основание здесь структурное, а не числовое: у сигналов по показателям
    им служит величина с отсечкой, а вид мнения объявлен заголовком раздела,
    и проверить тезис читатель может только по самому заключению. Поэтому
    в основании стоит вид мнения либо раздел, а рядом — подпись и дата.
    """

    code: str
    name: str
    level: str
    message: str
    basis: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Итог чтения заключения."""

    determination: Determination
    engagement: Engagement | None = None
    opinion: str | None = None
    opinion_name: str = ""
    modified: bool | None = None
    sections: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    pages: tuple[int, int] | None = None
    unreadable_pages: tuple[int, ...] = ()
    texts: tuple[SectionText, ...] = ()
    auditor: str = ""
    signed_on: str = ""

    def text_of(self, code: str) -> SectionText | None:
        """Дословный текст раздела по коду."""
        return next((item for item in self.texts if item.code == code), None)

    def quote(self, code: str, policy: AuditPolicy) -> str:
        """Цитата раздела для документа — готовой строкой, а не заново.

        Цитата обязана назвать источник: раздел, аудитора и дату заключения.
        Читатель обязан видеть, где кончается аудитор и начинаемся мы,
        а без подписи и даты цитата этого не показывает.
        """
        found = self.text_of(code)
        if found is None or not found.found:
            return ""
        signed = ""
        if self.auditor:
            signed = f", {self.auditor}"
            if self.signed_on:
                signed += f", {self.signed_on}"
        return policy.attribution.quote_template.format(
            section=found.name, signed=signed, text=found.text
        )

    def describe(self) -> str:
        """Однострочная сводка для отчёта и журнала."""
        if self.determination is Determination.ABSENT:
            return "заключения в документе нет"
        if self.determination is Determination.NOT_READABLE:
            pages = ", ".join(str(item) for item in self.unreadable_pages)
            return f"заключение не прочитано: страницы без текстового слоя {pages}"
        kind = "обзорная проверка" if self.engagement is Engagement.REVIEW else "аудит"
        sections = ", ".join(self.sections) if self.sections else "нет"
        return f"{kind}, {self.opinion_name}; разделы-признаки: {sections}"

    def as_meta(self) -> dict:
        """Сведения заключения для `src_file.meta` — сырыми, без формулировок.

        **Хранятся факты, а не текст методики.** Оговорки и формулировка
        сигнала берутся из справочника в момент сборки документа: методика
        правится, и результат обязан меняться вместе с ней. Дословный текст
        разделов — единственное, что хранится как есть: это слова аудитора,
        и взять их заново неоткуда, документа при сборке заключения уже нет.
        """
        return {
            "determination": self.determination.value,
            "engagement": self.engagement.value if self.engagement else None,
            "opinion": self.opinion,
            "opinion_name": self.opinion_name,
            "modified": self.modified,
            "sections": list(self.sections),
            "signals": list(self.signals),
            "unreadable_pages": list(self.unreadable_pages),
            "auditor": self.auditor,
            "signed_on": self.signed_on,
            "texts": [
                {
                    "code": item.code,
                    "name": item.name,
                    "text": item.text,
                    "refusal": item.refusal.value if item.refusal else None,
                }
                for item in self.texts
            ],
        }

    def limitations(self, policy: AuditPolicy) -> tuple[str, ...]:
        """Оговорки для раздела «Ограничения анализа» — дословно из методики."""
        found: list[str] = []
        if self.determination is Determination.ABSENT:
            found.append(policy.limitations["absent"])
        if self.determination is Determination.NOT_READABLE:
            found.append(policy.limitations["not_readable"])
        if self.engagement is Engagement.REVIEW:
            found.append(policy.limitations["review"])
        if self.modified:
            found.append(policy.limitations["modified"])
        return tuple(found)

    def quotes(self, policy: AuditPolicy) -> tuple[str, ...]:
        """Дословные цитаты разделов, объявленных цитируемыми, — и отказы.

        **Отказ называется наравне с цитатой.** Раздел, объявленный
        цитируемым и не извлечённый, иначе неотличим от раздела, которого
        в заключении нет: читатель решит, что оговорки не было.

        Условие печати объявлено у каждого раздела: «Основание для выражения
        мнения» при немодифицированном мнении содержит предписанное МСА
        описание процедур, а не оговорку, и в «Ограничениях анализа» ему
        не место.
        """
        found: list[str] = []
        for quoted in policy.quoted_sections:
            if not quoted.holds(self.modified):
                continue
            code = quoted.code
            section = self.text_of(code)
            if section is None:
                continue
            quote = self.quote(code, policy)
            if quote:
                found.append(" ".join(quote.split()))
                continue
            found.append(
                f"Раздел заключения «{section.name}»: "
                f"{TEXT_REFUSAL_TEXT.get(section.refusal, 'причина не названа')}. "
                "Текст раздела приводится по самому заключению."
            )
        return tuple(found)

    def plans_note(self, policy: AuditPolicy) -> int | None:
        """Номер примечания, в котором аудитор указал планы руководства.

        Ссылка берётся из того предложения раздела, где аудитор о планах
        говорит, а не из первого номера в разделе: в том же разделе стоят
        ссылки на примечание об обязательствах и на само допущение
        непрерывности. `None` — ссылки нет, и выдумывать номер нельзя.
        """
        import re

        rule = policy.plans_reference
        section = self.text_of(rule.section)
        if section is None or not section.text:
            return None
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(section.text.split())):
            if not any(marker in sentence for marker in rule.markers):
                continue
            found = re.search(rule.note_pattern, sentence)
            if found is not None:
                return int(found.group(1))
        return None

    def signal_hits(self, policy: AuditPolicy) -> tuple["AuditSignalHit", ...]:
        """Сработавшие сигналы заключения с предписанной формулировкой.

        Условие каждого объявлено в справочнике, а не здесь: прочитав
        справочник, надо видеть, когда печатается формулировка.
        """
        found: list[AuditSignalHit] = []
        for signal in policy.signals:
            if signal.condition == "opinion_modified":
                if not self.modified:
                    continue
                basis = f"Вид мнения: {self.opinion_name}"
            elif signal.condition == "section_present":
                # Наличие раздела и есть утверждение аудитора: искать в нём
                # слова незачем, наименование предписано МСА.
                if signal.section not in self.sections:
                    continue
                section = policy.section(signal.section)
                basis = (
                    f"Основание: раздел заключения «{section.name}»"
                    if section is not None
                    else "Основание: раздел заключения"
                )
            else:
                # Сигнал по разделу опознан при чтении: его код лежит
                # в `signals`, а не выводится здесь заново.
                if signal.code not in self.signals:
                    continue
                section = policy.section(signal.section) if signal.section else None
                basis = (
                    f"Основание: раздел заключения «{section.name}»"
                    if section is not None
                    else "Основание: раздел заключения"
                )
            if self.auditor:
                basis += f"; заключение подписано {self.auditor}"
                if self.signed_on:
                    basis += f", {self.signed_on}"
            found.append(
                AuditSignalHit(
                    code=signal.code,
                    name=signal.name,
                    level=signal.level,
                    message=" ".join(signal.formulation.split()),
                    basis=basis,
                )
            )
        return tuple(found)


def audit_from_meta(meta: dict | None) -> AuditReport | None:
    """Восстанавливает сведения заключения из `src_file.meta`.

    Документ собирается из базы, самого файла отчётности при этом нет,
    и оговорки с цитатой берутся отсюда. Восстанавливается тот же объект,
    которым пользуется загрузка: формулировки считает одна реализация,
    а не две — расхождение двух путей к одному ответу не видно, пока
    их не сравнить.
    """
    if not meta or not meta.get("audit"):
        return None
    found = meta["audit"]
    engagement = found.get("engagement")
    return AuditReport(
        determination=Determination(found["determination"]),
        engagement=Engagement(engagement) if engagement else None,
        opinion=found.get("opinion"),
        opinion_name=found.get("opinion_name", ""),
        modified=found.get("modified"),
        sections=tuple(found.get("sections", ())),
        signals=tuple(found.get("signals", ())),
        unreadable_pages=tuple(found.get("unreadable_pages", ())),
        auditor=found.get("auditor", ""),
        signed_on=found.get("signed_on", ""),
        texts=tuple(
            SectionText(
                code=item["code"],
                name=item["name"],
                text=item.get("text", ""),
                refusal=TextRefusal(item["refusal"]) if item.get("refusal") else None,
            )
            for item in found.get("texts", ())
        ),
    )


def read_audit_report(
    text: str,
    document: PdfDocument | None = None,
    before: int = 0,
    policy: AuditPolicy | None = None,
) -> AuditReport:
    """Читает заключение: тип задания, вид мнения и разделы-признаки.

    `before` — смещение первой формы: заключение стоит до неё, и дальше
    искать незачем. Это то же опирание на строение, что и везде здесь.
    """
    policy = policy or load_audit_policy()
    lines, offsets = _lines_with_offsets(text)
    limit = before or len(text)

    heading = _report_heading(lines, offsets, limit, policy)
    if heading is None:
        # Заголовка в тексте нет. Но заключение могло быть объявлено
        # оглавлением либо лежать на страницах без текстового слоя — тогда
        # это «не прочитано», а не «нет».
        lost = _pages_before_forms(document, limit)
        if lost or _declared_in_contents(lines, policy):
            return AuditReport(Determination.NOT_READABLE, unreadable_pages=lost)
        return AuditReport(Determination.ABSENT)

    index, engagement = heading
    start = offsets[index]
    # Конец заключения — заголовок следующего раздела документа, а не начало
    # первой формы: между подписью аудитора и формой стоит заявление
    # об ответственности руководства, и у Норникеля из-за этого терялась
    # страница. Первая форма остаётся внешним рубежом.
    end = _report_end(lines, offsets, index, limit, policy)
    block = [
        lines[position].strip()
        for position in range(index, len(lines))
        if offsets[position] < end
    ]

    pages = None
    if document is not None:
        pages = (document.page_at(start), document.page_at(max(start, end - 1)))
    lost = _unreadable_within(document, pages)

    opinion = _opinion_of(block, policy)
    if opinion is None:
        # Заголовок заключения есть, а раздела мнения нет: так выглядит
        # заключение, у которого текстом взят только титул.
        return AuditReport(
            Determination.NOT_READABLE,
            engagement=engagement,
            pages=pages,
            unreadable_pages=lost,
        )

    sections = _sections_of(block, policy)
    return AuditReport(
        Determination.DETERMINED,
        engagement=engagement,
        opinion=opinion.code,
        opinion_name=opinion.name,
        modified=opinion.modified,
        sections=sections,
        signals=_signals_of(block, sections, policy),
        pages=pages,
        unreadable_pages=lost,
        texts=_texts_of(lines, offsets, index, end, sections, document, policy),
        auditor=(auditor := _auditor_of(block, policy))[0],
        signed_on=_signed_on(block, policy, auditor[1]),
    )


def _report_end(
    lines: list[str],
    offsets: list[int],
    index: int,
    limit: int,
    policy: AuditPolicy,
) -> int:
    """Смещение конца заключения: заголовок следующего раздела документа."""
    wanted = [normalize_name(item) for item in policy.ends_before]
    for position in range(index + 1, len(lines)):
        if offsets[position] >= limit:
            break
        stripped = lines[position].strip()
        if not stripped or _CONTENTS_TAIL.search(stripped):
            continue
        normalized = normalize_name(stripped)
        if any(normalized.startswith(item) for item in wanted):
            return offsets[position]
    return limit


def _texts_of(
    lines: list[str],
    offsets: list[int],
    index: int,
    end: int,
    sections: tuple[str, ...],
    document: PdfDocument | None,
    policy: AuditPolicy,
) -> tuple[SectionText, ...]:
    """Дословный текст каждого найденного раздела — целиком либо никак.

    Границы раздела — его заголовок и заголовок следующего раздела. Если
    следующего нет, концом служит конец заключения; не определился и он —
    отказ. Обрезанная оговорка хуже отсутствующей.
    """
    # Заголовок раздела повторяется на следующей странице — у Европлана
    # «Ключевые вопросы аудита» стоят дважды. Повтор не начинает нового
    # раздела: это тот же случай, что «(продолжение)» у примечаний.
    starts: list[tuple[int, str]] = []
    seen: set[str] = set()
    for position in range(index, len(lines)):
        if offsets[position] >= end:
            break
        normalized = normalize_name(lines[position].strip())
        if not normalized:
            continue
        for section in policy.sections:
            if any(
                normalized.startswith(normalize_name(item))
                for item in section.headings
            ):
                if section.code not in seen:
                    seen.add(section.code)
                    starts.append((position, section.code))
                break

    found: list[SectionText] = []
    for order, (position, code) in enumerate(starts):
        section = policy.section(code)
        if code not in sections:
            continue
        stop = (
            starts[order + 1][0]
            if order + 1 < len(starts)
            else _line_at(offsets, end)
        )
        if stop <= position + 1:
            found.append(
                SectionText(code, section.name, refusal=TextRefusal.EMPTY)
            )
            continue
        lost = _unreadable_within(
            document,
            (
                document.page_at(offsets[position]),
                document.page_at(offsets[min(stop, len(lines) - 1)]),
            )
            if document is not None
            else None,
        )
        if lost:
            found.append(
                SectionText(code, section.name, refusal=TextRefusal.PAGES_NOT_READABLE)
            )
            continue
        body = " ".join(
            _without_signature(
                _until_subsection(lines[position + 1 : stop], policy), policy
            )
        )
        found.append(
            SectionText(code, section.name, text=body)
            if body
            else SectionText(code, section.name, refusal=TextRefusal.EMPTY)
        )
    return tuple(found)


def _until_subsection(lines: list[str], policy: AuditPolicy) -> list[str]:
    """Строки раздела до заголовка следующего подраздела.

    **Границу держит начало строки, а не вхождение слов.** Заголовки-признаки
    границу образуют не все: у ФосАгро в «Основание для выражения мнения»
    попали «Независимость» и колонтитул страницы, потому что подразделы,
    предписанные МСА, в перечень признаков не входят. Но и по вхождению слов
    резать нельзя: заключение ссылается на свой раздел внутри предложения
    («далее описаны в разделе «Ответственность аудитора…»), и цитата
    обрывалась на середине фразы. Поэтому сравнивается начало строки —
    та же опора на строение, что и везде здесь.
    """
    wanted = [normalize_name(item) for item in policy.quote_ends_before]
    body: list[str] = []
    for line in lines:
        normalized = normalize_name(line.strip())
        if normalized and any(normalized.startswith(item) for item in wanted):
            break
        body.append(line)
    return body


def _without_signature(lines: list[str], policy: AuditPolicy) -> list[str]:
    """Строки раздела без блока подписи в конце.

    Подпись стоит за последним разделом и ни к одному из них не относится:
    строка, состоящая только из даты или только из наименования аудитора,
    в цитату идти не должна — иначе читатель прочтёт её как часть оговорки.
    """
    pattern = re.compile(
        "".join(policy.attribution.date_pattern.split()), re.IGNORECASE
    )
    found = [line.strip() for line in lines if line.strip()]
    while found:
        last = found[-1]
        only_date = pattern.fullmatch(last) is not None
        only_auditor = any(
            last.startswith(marker) for marker in policy.attribution.auditor_markers
        ) or any(last.startswith(form) for form in policy.attribution.auditor_forms)
        if not (only_date or only_auditor):
            break
        found.pop()
    return found


def _line_at(offsets: list[int], offset: int) -> int:
    """Номер строки, начинающейся не раньше этого смещения."""
    return sum(1 for item in offsets if item < offset)


def _auditor_of(block: list[str], policy: AuditPolicy) -> tuple[str, int]:
    """Наименование аудитора и место строки; пусто — реквизиты не найдены."""
    for index, line in enumerate(block):
        for marker in policy.attribution.auditor_markers:
            if line.startswith(marker):
                return line[len(marker) :].strip(), index
    for index, line in enumerate(block):
        if any(line.startswith(form) for form in policy.attribution.auditor_forms):
            return line.strip(), index
    return "", -1


def _signed_on(block: list[str], policy: AuditPolicy, after: int) -> str:
    """Дата подписания заключения; пусто — не найдена.

    **Дата берётся только из блока подписи** — то есть после строки
    с реквизитами аудитора и отдельной короткой строкой. Без этой привязки
    датой подписания становилась отчётная дата из текста мнения: у ФосАгро
    «31 декабря 2025 года». Ложная дата в цитате хуже отсутствующей —
    читатель проверить её не может, а поверит.
    """
    if after < 0:
        return ""
    pattern = re.compile(
        "".join(policy.attribution.date_pattern.split()), re.IGNORECASE
    )
    found = [
        match.group()
        for line in block[after + 1 :]
        if len(line) <= policy.attribution.date_line_max_length
        and (match := pattern.search(line))
    ]
    return found[-1] if found else ""


def _lines_with_offsets(text: str) -> tuple[list[str], list[int]]:
    """Строки документа вместе со смещением начала каждой."""
    lines = text.split("\n")
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    return lines, offsets


def _lines_until(offsets: list[int], end: int) -> int:
    """Сколько строк умещается до смещения."""
    return sum(1 for item in offsets if item < end)


def _report_heading(
    lines: list[str], offsets: list[int], limit: int, policy: AuditPolicy
) -> tuple[int, Engagement] | None:
    """Строка заголовка заключения и тип задания; None — заголовка нет.

    Запись оглавления заголовком не считается: она кончается номером
    страницы. Иначе заключением становилось бы содержание — у Автодора
    оно называет заключение, которого в тексте нет.
    """
    for index, line in enumerate(lines):
        if offsets[index] >= limit:
            break
        stripped = line.strip()
        if not stripped or _CONTENTS_TAIL.search(stripped):
            continue
        # Запись оглавления переносится, и номер страницы остаётся на второй
        # строке: «Аудиторское заключение независимых аудиторов о раскрываемой»
        # / «консолидированной финансовой отчетности 3-4». Это по-прежнему
        # оглавление, а не заголовок — склейка та же, что у примечаний.
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if following[:1].islower() and _CONTENTS_TAIL.search(following):
            continue
        normalized = normalize_name(stripped)
        for kind in (Engagement.REVIEW, Engagement.AUDIT):
            for heading in policy.report_headings[kind.value]:
                if normalized.startswith(normalize_name(heading)):
                    return index, kind
    return None


def _declared_in_contents(lines: list[str], policy: AuditPolicy) -> bool:
    """Объявлено ли заключение оглавлением: независимое свидетельство."""
    wanted = [
        normalize_name(heading)
        for headings in policy.report_headings.values()
        for heading in headings
    ]
    for line in lines:
        stripped = line.strip()
        if not _CONTENTS_TAIL.search(stripped):
            continue
        normalized = normalize_name(stripped)
        if any(normalized.startswith(item) for item in wanted):
            return True
    return False


def _pages_before_forms(
    document: PdfDocument | None, limit: int
) -> tuple[int, ...]:
    """Страницы без текстового слоя, лежащие до первой формы."""
    if document is None:
        return ()
    last = document.page_at(max(0, limit - 1))
    return tuple(number for number in document.pages_without_text if number <= last)


def _unreadable_within(
    document: PdfDocument | None, pages: tuple[int, int] | None
) -> tuple[int, ...]:
    """Страницы без текстового слоя внутри найденных границ заключения."""
    if document is None or pages is None:
        return ()
    first, last = pages
    return tuple(
        number for number in document.pages_without_text if first <= number <= last
    )


def _opinion_of(block: list[str], policy: AuditPolicy):
    """Вид мнения по заголовку раздела; None — раздела нет.

    Перебор идёт в порядке справочника: сначала модифицированные виды,
    и лишь потом немодифицированный. «Мнение с оговоркой» начинается
    со слова «Мнение», и обратный порядок делал бы оговорку невидимой.
    """
    for kind in policy.opinions:
        for heading in kind.headings:
            wanted = normalize_name(heading)
            for line in block:
                normalized = normalize_name(line)
                if normalized == wanted or normalized.startswith(f"{wanted} "):
                    return kind
    return None


def _sections_of(block: list[str], policy: AuditPolicy) -> tuple[str, ...]:
    """Разделы-признаки, найденные в заключении."""
    found: list[str] = []
    for section in policy.sections:
        for heading in section.headings:
            wanted = normalize_name(heading)
            if any(normalize_name(line).startswith(wanted) for line in block):
                found.append(section.code)
                break
    return tuple(found)


def _signals_of(
    block: list[str], sections: tuple[str, ...], policy: AuditPolicy
) -> tuple[str, ...]:
    """Сигналы заключения: условие объявлено справочником, не кодом."""
    found: list[str] = []
    for signal in policy.signals:
        if signal.condition != "section_found" or signal.section not in sections:
            continue
        section = policy.section(signal.section)
        heading = next(
            (
                line
                for line in block
                if any(
                    normalize_name(line).startswith(normalize_name(item))
                    for item in section.headings
                )
            ),
            "",
        )
        lowered = heading.lower()
        if any(marker.lower() in lowered for marker in signal.markers):
            found.append(signal.code)
    return tuple(found)
