"""Раскрытия уровня 2: абзацы прозы, только названные примечания, приметы — не вывод."""

from finlib.normalize.ifrs_note_lines import load_note_lines
from finlib.sources.ifrs_disclosures import Kind, paragraphs_of, read_disclosures
from finlib.sources.ifrs_notes import ContentsEntry, Note, NoteIndex
from finlib.sources.ifrs_numbers import load_parsing_policy

FOOTER = "ПАО «Тест» Примечания к консолидированной финансовой отчетности"

GENERAL = f"""1 Общие сведения
В отношении Российской Федерации введены санкции, реализованы иные меры ограничительного
характера, которые оказывают влияние на деятельность Группы и ее контрагентов в целом.
{FOOTER}
"""

DEBT = f"""21 Кредиты и займы
Заключенные компаниями Группы кредитные договоры содержат ряд финансовых условий, в случае
нарушения которых кредиторы имеют право потребовать досрочного погашения. По состоянию на
{FOOTER}
37
31 декабря 2025 г. Группа нарушила условия нескольких кредитных договоров, по которым
получила отказ банков от права требования, и соблюдает прочие ограничительные условия.
Прочие движения (1 479) (10)
Активы, переданные в качестве обеспечения
По состоянию на 31 декабря 2025 года у Группы отсутствует имущество, переданное
в залог по кредитным договорам.
Ограничительные условия – в рамках кредитных договоров на Компании Группы распространяются
ограничения на предоставление гарантий и поручительств третьим сторонам и прочие условия.
{FOOTER}
"""

EVENTS = f"""28 События после отчетной даты
В феврале 2026 года Группа привлекла облигационный заем серии БО-П19 на сумму 5 млрд руб.
со ставкой купона 21% с датой погашения в 2029 году.
*****
Приложение: прочая проза, не относящаяся к примечанию о событиях после отчетной даты.
{FOOTER}
"""


def _document() -> tuple[str, NoteIndex]:
    """Три примечания подряд и указатель с оглавлением."""
    text = GENERAL + DEBT + EVENTS
    notes = []
    start = 0
    for number, title, body in (
        (1, "Общие сведения", GENERAL),
        (21, "Кредиты и займы", DEBT),
        (28, "События после отчетной даты", EVENTS),
    ):
        notes.append(Note(number, title, start, start + len(body), number))
        start += len(body)
    contents = tuple(ContentsEntry(note.number, note.title, note.page) for note in notes)
    return text, NoteIndex(tuple(notes), contents)


def _page(offset: int) -> int:
    """Номер страницы по смещению: в тесте одна страница на примечание не нужна."""
    return 1


def test_paragraph_runs_over_a_colontitle_and_skips_tables() -> None:
    """Предложение, прерванное колонтитулом и номером страницы, — один абзац."""
    text, index = _document()
    policy = load_parsing_policy().disclosure_text
    found = paragraphs_of(index.get(21), text, _page, policy, frozenset({FOOTER}))
    first = found[0].text
    assert first.startswith("Заключенные компаниями Группы")
    assert "По состоянию на 31 декабря 2025 г. Группа нарушила условия" in first
    assert FOOTER not in first and "37" not in first.split()
    assert all("(1 479)" not in item.text for item in found)
    assert found[1].heading == "Активы, переданные в качестве обеспечения"


def test_only_named_notes_are_read_and_breach_is_a_marker() -> None:
    """Примета в примечании об общих сведениях не ищется; нарушение — приметой."""
    text, index = _document()
    catalog = load_note_lines()
    policy = load_parsing_policy().disclosure_text
    found = read_disclosures(text, index, _page, index.get(21), catalog.disclosures, policy)
    covenants = found[Kind.COVENANTS]
    assert [number for number, _ in covenants.viewed] == [21]
    assert all("санкции" not in item.text for item in covenants.quotes)
    assert len(covenants.quotes) == 2
    assert set(covenants.breach) == {"нарушила условия", "отказ банков от права требования"}


def test_pledge_heading_does_not_claim_a_covenant_paragraph() -> None:
    """Под заголовком залогов абзац об ограничительных условиях — не залог."""
    text, index = _document()
    found = read_disclosures(
        text, index, _page, index.get(21),
        load_note_lines().disclosures, load_parsing_policy().disclosure_text,
    )  # fmt: skip
    pledges = found[Kind.PLEDGES].quotes
    assert len(pledges) == 1 and "в залог" in pledges[0].text


def test_events_note_is_quoted_whole_up_to_the_end_marker() -> None:
    """Примечание о событиях целиком, но не дальше «*****»."""
    text, index = _document()
    found = read_disclosures(
        text, index, _page, index.get(21),
        load_note_lines().disclosures, load_parsing_policy().disclosure_text,
    )  # fmt: skip
    events = found[Kind.SUBSEQUENT_EVENTS]
    assert len(events.quotes) == 1
    assert events.quotes[0].text.endswith("с датой погашения в 2029 году.")
    assert "Приложение" not in events.quotes[0].text


def test_no_debt_note_means_nothing_viewed_not_nothing_found() -> None:
    """Примечания о долге нет — ковенанты не «не найдены», а не искались."""
    text, index = _document()
    found = read_disclosures(
        text, index, _page, None,
        load_note_lines().disclosures, load_parsing_policy().disclosure_text,
    )  # fmt: skip
    assert found[Kind.COVENANTS].note_missing and found[Kind.PLEDGES].note_missing
    # Поручительства ищутся и в названных примечаниях; в документе их нет.
    assert found[Kind.GUARANTEES].note_missing


def _index(*notes: tuple[int, str, str]) -> tuple[str, NoteIndex]:
    """Документ из примечаний подряд: номер, наименование, текст."""
    text, found, start = "", [], 0
    for number, title, body in notes:
        found.append(Note(number, title, start, start + len(body), number))
        text += body
        start += len(body)
    contents = tuple(ContentsEntry(note.number, note.title, note.page) for note in found)
    return text, NoteIndex(tuple(found), contents)


SEGEZHA_DEBT = """21 КРЕДИТЫ И ЗАЙМЫ
Ограничительные условия – в рамках кредитных договоров на Компании Группы
распространяются определенные ограничительные условия, преимущественно
поведенческого характера: ограничения по привлечению заемных средств, на предоставление
займов, гарантий и поручительств третьим сторонам, на распоряжение активами Группы.
"""

SEGEZHA_RELATED = """27 СДЕЛКИ СО СВЯЗАННЫМИ СТОРОНАМИ
Операции со связанными сторонами совершаются на условиях, согласованных сторонами
сделок, и раскрываются в настоящем примечании в соответствии с требованиями МСФО.
"""


def test_segezha_covenant_restriction_is_not_a_guarantee() -> None:
    """Сегежа: ограничение на выдачу поручительств — ковенант, а не поручительство."""
    text, index = _index(
        (21, "Кредиты и займы", SEGEZHA_DEBT),
        (27, "Сделки со связанными сторонами", SEGEZHA_RELATED),
    )
    found = read_disclosures(
        text, index, _page, index.get(21),
        load_note_lines().disclosures, load_parsing_policy().disclosure_text,
    )  # fmt: skip
    assert len(found[Kind.COVENANTS].quotes) == 1
    guarantees = found[Kind.GUARANTEES]
    assert [number for number, _ in guarantees.viewed] == [21, 27]
    assert guarantees.quotes == ()


OKEY_DEBT = """25 Кредиты и займы
Обеспеченные банковские кредиты и облигационные займы обеспечены основными средствами
АО «ДОРИНДА», ООО «О’КЕЙ» и предоставленным АО «ДОРИНДА» поручительством (Примечание 31).
"""

OKEY_ASSETS = """16 Основные средства и незавершенное строительство
(b) Активы в залоге
По состоянию на 31 декабря 2025 года торговые магазины балансовой стоимостью 16 723 632
тыс. рублей были заложены третьим лицам в качестве залога по банковским кредитам.
"""


def test_okey_pledge_by_assets_and_in_the_fixed_assets_note() -> None:
    """О'КЕЙ: «обеспечены основными средствами» — залог; заложенное — в прим. 16."""
    text, index = _index(
        (16, "Основные средства и незавершенное строительство", OKEY_ASSETS),
        (25, "Кредиты и займы", OKEY_DEBT),
    )
    found = read_disclosures(
        text, index, _page, index.get(25),
        load_note_lines().disclosures, load_parsing_policy().disclosure_text,
    )  # fmt: skip
    pledges = found[Kind.PLEDGES]
    assert [item.note for item in pledges.quotes] == [25, 16]
    assert "16 723 632" in pledges.quotes[1].text
    # Один абзац — залог и поручительство сразу: старше здесь только ковенанты.
    assert [item.note for item in found[Kind.GUARANTEES].quotes] == [25]
