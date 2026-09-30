"""Тесты чтения аудиторского заключения (задача 25).

Случаи с живых комплектов: у Автодора заключение занимает страницы 3–7
и все пять — изображение без текстового слоя; у ЛСР «Важные обстоятельства»
объявляют пересмотр ранее выпущенной отчётности; у Сегежи мнение
немодифицированное, а существенная неопределённость объявлена отдельным
разделом.
"""

from finlib.normalize.ifrs_audit import load_audit_policy
from finlib.sources.ifrs_audit import Determination, Engagement, read_audit_report

QUALIFIED = """
Аудиторское заключение независимого аудитора
Акционерам и Совету директоров
Мнение с оговоркой
Мы провели аудит консолидированной финансовой отчетности.
Основание для выражения мнения с оговоркой
Мы не смогли получить достаточные надлежащие аудиторские доказательства.
Ключевые вопросы аудита
"""

UNMODIFIED = """
Аудиторское заключение независимых аудиторов
Мнение
Отчетность отражает достоверно во всех существенных отношениях.
Важные обстоятельства – пересмотр раскрываемой консолидированной отчетности
Мы обращаем внимание на пояснение 2 (а).
"""

GOING_CONCERN = """
АУДИТОРСКОЕ ЗАКЛЮЧЕНИЕ НЕЗАВИСИМОГО АУДИТОРА
Мнение
Отчетность отражает достоверно во всех существенных отношениях.
Существенная неопределенность в отношении непрерывности деятельности
Превышение краткосрочных обязательств над краткосрочными активами.
"""

REVIEW = """
Заключение по результатам обзорной проверки
Вывод с оговоркой
Объем обзорной проверки существенно меньше объема аудита, и мы не выражаем
аудиторского мнения.
"""

CONTENTS_ONLY = """
Содержание
Аудиторское заключение независимого аудитора 3
Консолидированный отчет о финансовом положении 8
"""


def test_qualified_opinion_is_not_read_as_unmodified() -> None:
    """«Мнение с оговоркой» не становится немодифицированным.

    Заголовок модифицированного мнения начинается со слова «Мнение»,
    и обратный порядок перебора делал бы оговорку невидимой.
    """
    found = read_audit_report(QUALIFIED)
    assert found.determination is Determination.DETERMINED
    assert found.opinion == "qualified"
    assert found.modified is True
    assert "basis_for_opinion" in found.sections
    assert "key_audit_matters" in found.sections


def test_unmodified_opinion_and_restatement_signal() -> None:
    """Пересмотр ранее выпущенной отчётности — сигнал, а не пометка."""
    found = read_audit_report(UNMODIFIED)
    assert found.opinion == "unmodified"
    assert found.modified is False
    assert found.signals == ("statements_restated",)


def test_going_concern_is_independent_of_the_opinion() -> None:
    """Неопределённость объявляется отдельно и мнение не модифицирует."""
    found = read_audit_report(GOING_CONCERN)
    assert found.opinion == "unmodified"
    assert "going_concern_uncertainty" in found.sections
    assert found.signals == ()


def test_review_is_a_separate_engagement() -> None:
    """Обзорная проверка — тип задания, а не разновидность мнения."""
    found = read_audit_report(REVIEW)
    assert found.engagement is Engagement.REVIEW
    assert found.opinion == "qualified"
    policy = load_audit_policy()
    limitations = found.limitations(policy)
    assert policy.limitations["review"] in limitations
    # Оговорка о модифицированном мнении своя у каждого вида оговорки, и пока
    # вид человеком не установлен, печатается оговорка вида «не установлен»:
    # одна общая формулировка говорила бы о нераскрытых сегментах то же,
    # что о заниженном резерве.
    until = policy.caveat_kind_until_confirmed
    assert " ".join(until.limitation.split()) in limitations


# ФосАгро 6м2026, заключение по обзорной проверке (стр. 3–4 файла): основание
# стоит перед выводом и называется по выводу, а не по мнению.
REVIEW_BASIS = """
Заключение по результатам обзорной проверки промежуточной финансовой информации
Основание для вывода с оговоркой
В примечаниях к консолидированной промежуточной сокращенной финансовой отчетности руководство
Группы не раскрыло информацию о сегментах за шесть месяцев, закончившихся 30 июня 2026 года, и за
шесть месяцев, закончившихся 30 июня 2025 года, требуемую в соответствии с пунктом 16А (g) стандарта
МСФО (IAS) 34.
Вывод с оговоркой
На основе проведенной нами обзорной проверки, за исключением влияния вопроса, изложенного в разделе
«Основание для вывода c оговоркой», не выявлены факты, которые могут служить основанием для того,
чтобы мы сочли, что прилагаемая отчетность не подготовлена во всех существенных отношениях.
20 августа 2026 года
"""


def test_review_basis_section_is_read_and_quoted() -> None:
    """ФосАгро 6м2026: «Основание для вывода с оговоркой» читается и цитируется.

    Без заголовков обзорной проверки раздел не читался: цитаты не было,
    а предложение вида печаталось «приметы вида не называют» — приметы
    не искались. Вывод, идущий следом, в цитату основания не попадает.
    """
    policy = load_audit_policy()
    found = read_audit_report(REVIEW_BASIS, policy=policy)
    assert found.engagement is Engagement.REVIEW
    assert found.opinion == "qualified"
    assert "basis_for_opinion" in found.sections
    quotes = found.quotes(policy)
    assert len(quotes) == 1
    assert "не раскрыло информацию о сегментах" in quotes[0]
    assert "не выявлены факты" not in quotes[0]
    assert found.proposed_caveat_kind(policy) == "about_disclosure"


def test_contents_entry_is_not_the_report() -> None:
    """Оглавление называет заключение, но заключением не является.

    У Автодора заключение объявлено оглавлением, а текста его в документе
    нет: страницы 3–7 — изображение. Исход здесь «не прочитано»,
    а не «мнение немодифицированное» и не «заключения нет».
    """
    found = read_audit_report(CONTENTS_ONLY)
    assert found.determination is Determination.NOT_READABLE
    assert found.opinion is None


def test_absent_report_is_not_the_same_as_unreadable() -> None:
    """Заключения нет вовсе — третье состояние, со своей оговоркой."""
    found = read_audit_report("Консолидированный отчет о финансовом положении\n")
    assert found.determination is Determination.ABSENT
    policy = load_audit_policy()
    assert policy.limitations["absent"] in found.limitations(policy)
    assert policy.limitations["not_readable"] not in found.limitations(policy)


def test_signal_does_not_fire_without_the_marker() -> None:
    """Раздел «Важные обстоятельства» сам по себе сигналом не является."""
    text = UNMODIFIED.replace(
        "Важные обстоятельства – пересмотр раскрываемой консолидированной отчетности",
        "Важные обстоятельства – основы подготовки отчетности",
    )
    found = read_audit_report(text)
    assert "emphasis_of_matter" in found.sections
    assert found.signals == ()


SIGNED = """
Аудиторское заключение независимых аудиторов
Независимый аудитор: АО «Кэпт»
Мнение
Отчетность за год, закончившийся 31 декабря 2025 года, отражает достоверно.
Важные обстоятельства – пересмотр раскрываемой отчетности
Мы обращаем внимание на пояснение 2 (а).
8 мая 2026 года
Заявление об ответственности руководства
Руководство отвечает за подготовку отчетности.
"""


def test_section_text_is_taken_whole_and_quoted_with_the_source() -> None:
    """Текст раздела берётся целиком, а цитата называет раздел и подпись."""
    policy = load_audit_policy()
    found = read_audit_report(SIGNED, policy=policy)
    text = found.text_of("emphasis_of_matter")
    assert text is not None and text.found
    assert text.text == "Мы обращаем внимание на пояснение 2 (а)."
    quote = found.quote("emphasis_of_matter", policy)
    assert "Важные обстоятельства" in quote
    assert "АО «Кэпт»" in quote
    assert "8 мая 2026 года" in quote
    assert "«Мы обращаем внимание на пояснение 2 (а).»" in quote


def test_report_ends_before_the_responsibility_section() -> None:
    """Заключение кончается заголовком следующего раздела, а не формой."""
    found = read_audit_report(SIGNED)
    text = found.text_of("emphasis_of_matter")
    assert "Руководство отвечает" not in text.text


BASIS_WITH_SUBSECTIONS = """
Аудиторское заключение независимого аудитора
Мнение с оговоркой
Отчетность отражает достоверно, за исключением указанного ниже.
Основание для выражения мнения с оговоркой
Руководство не раскрыло информацию о сегментах.
Наши обязанности далее описаны в разделе «Ответственность аудитора за аудит
консолидированной финансовой отчетности» нашего заключения.
Независимость
Мы независимы по отношению к Группе в соответствии с Кодексом СМСЭБ.
www.example.ru 2
Ключевые вопросы аудита
Вопросы, наиболее значимые для нашего аудита.
"""


def test_quote_ends_at_the_next_subsection() -> None:
    """Цитата раздела кончается заголовком подраздела, а не идёт дальше него.

    У ФосАгро в «Основание для выражения мнения» попали «Независимость»
    и колонтитул страницы: подразделы, предписанные МСА, в перечень
    разделов-признаков не входят и границы не образовывали.
    """
    policy = load_audit_policy()
    found = read_audit_report(BASIS_WITH_SUBSECTIONS, policy=policy)
    text = found.text_of("basis_for_opinion")
    assert text is not None and text.found
    assert "Руководство не раскрыло информацию о сегментах." in text.text
    assert "Мы независимы" not in text.text
    assert "example.ru" not in text.text


def test_reference_to_a_section_inside_a_sentence_does_not_cut_the_quote() -> None:
    """Ссылка на раздел внутри предложения цитату не обрывает.

    Граница держится началом строки, а не вхождением слов: заключение
    ссылается на «Ответственность аудитора…» внутри фразы, и по вхождению
    цитата обрывалась на середине — «далее описаны в разделе «»».
    """
    found = read_audit_report(BASIS_WITH_SUBSECTIONS, policy=load_audit_policy())
    text = found.text_of("basis_for_opinion")
    assert "Ответственность аудитора за аудит" in text.text


def test_caveat_kind_is_proposed_but_not_applied() -> None:
    """Вид оговорки машина предлагает, а следствия применяет человек.

    Оговорка о раскрытии величин не затрагивает, оговорка о величинах ставит
    под вопрос сами числа. Различает их человек: приметы в прозе — не
    доказательство, и до решения действует вид «не установлен».
    """
    from dataclasses import replace

    policy = load_audit_policy()
    disclosure = read_audit_report(
        QUALIFIED.replace(
            "Мы не смогли получить достаточные надлежащие аудиторские доказательства.",
            "Руководство не раскрыло информацию о сегментах, требуемую МСФО (IFRS) 8.",
        ),
        policy=policy,
    )
    assert disclosure.proposed_caveat_kind(policy) == "about_disclosure"
    # Предложение следствий не имеет: действует вид по умолчанию.
    assert disclosure.effective_caveat_kind(policy) == "not_determined"
    assert not policy.caveat_kind("not_determined").lowers_confidence

    values = read_audit_report(
        QUALIFIED.replace(
            "Мы не смогли получить достаточные надлежащие аудиторские доказательства.",
            "Резерв под ожидаемые кредитные убытки занижен на 1 200 млн руб.",
        ),
        policy=policy,
    )
    assert values.proposed_caveat_kind(policy) == "about_values"
    assert values.effective_caveat_kind(policy) == "not_determined"

    # Подтверждённый человеком вид действует, и следствия у него свои.
    confirmed = replace(values, caveat_kind="about_values", caveat_confirmed_by="кто-то")
    assert confirmed.effective_caveat_kind(policy) == "about_values"
    assert policy.caveat_kind("about_values").lowers_confidence

    # Приметы двух видов сразу предложения не дают: выбрать по написанию нельзя.
    both = read_audit_report(
        QUALIFIED.replace(
            "Мы не смогли получить достаточные надлежащие аудиторские доказательства.",
            "Резерв занижен, а информация о сегментах не раскрыта.",
        ),
        policy=policy,
    )
    assert both.proposed_caveat_kind(policy) is None
    assert both.effective_caveat_kind(policy) == "not_determined"

    # У немодифицированного мнения вида оговорки нет вовсе.
    assert read_audit_report(UNMODIFIED).effective_caveat_kind(policy) is None


def test_every_caveat_kind_has_a_signal_of_its_own() -> None:
    """У каждого вида оговорки свой сигнал со своим уровнем.

    Вид без сигнала молчит: оговорка разобрана по виду, а документ о ней
    не сказал. Уровень при этом решает, возникнет ли эскалация, и держать
    его в коде значило бы решать методический вопрос кодом.
    """
    policy = load_audit_policy()
    by_kind = {
        item.caveat_kind: item for item in policy.signals if item.caveat_kind
    }
    assert set(by_kind) == {item.code for item in policy.caveat_kinds}
    assert by_kind["about_values"].level == "supervisory"
    assert by_kind["about_disclosure"].level == "attention"
    assert by_kind["not_determined"].level == "attention"


def test_signing_date_is_not_taken_from_the_opinion_text() -> None:
    """Отчётная дата из текста мнения датой подписания не становится.

    У ФосАгро «31 декабря 2025 года» стоит в самом мнении, и без привязки
    к блоку подписи она становилась датой заключения. Ложная дата в цитате
    хуже отсутствующей: проверить её читатель не может, а поверит.
    """
    without = SIGNED.replace("8 мая 2026 года\n", "")
    found = read_audit_report(without)
    assert found.signed_on == ""
    assert "31 декабря 2025" not in found.quote("emphasis_of_matter", load_audit_policy())
