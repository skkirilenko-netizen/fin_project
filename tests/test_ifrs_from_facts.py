"""Расчёт МСФО по фактам базы: тот же ответ, что по разобранному документу.

**Два пути к одному числу расходятся, и расхождения не видно, пока их
не сравнить.** Замер задачи 27 считал по документу, заключение обязано
считаться по фактам базы, и первая же сверка нашла расхождение: у ФосАгро
по документу класс B и балл 66,0, а по фактам класс не присваивался вовсе —
величины примечаний в базу не писались, покрытие процентов не считалось,
группа «Обслуживание долга» выпадала, и «Долговая нагрузка» получала
фактический вес 57 % вместо 40 %.

Поэтому здесь проверяется не арифметика — она общая, — а **вход**: что
величина примечания попадает в факты своей формы с пометкой источника
и что расчёт по фактам находит её там.
"""

from datetime import date
from decimal import Decimal

from finlib.db import fetch_all, fetch_one
from finlib.metrics.ifrs_store import (
    compute_from_facts,
    confidence_of,
    inputs_of,
    periods_of,
)
from finlib.normalize.ifrs_loader import load_extraction
from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.quality.periods import PeriodConfidence
from finlib.scoring.ifrs import assess
from finlib.scoring.ifrs_store import (
    save_ifrs_assessment,
    save_metrics,
    stop_factors_of,
)
from finlib.sources.ifrs_document import DocumentReading
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import identify
from finlib.sources.ifrs_notes import NoteValue
from finlib.sources.ifrs_numbers import Grouping
from finlib.sources.ifrs_review import review

INN = "7736050003"
DATES = (date(2025, 12, 31), date(2024, 12, 31))

CONTENTS = """
Содержание
Консолидированный отчёт о финансовом положении 3
Консолидированный отчёт о прибыли или убытке 4
Примечания к консолидированной финансовой отчётности 5
"""

# Комплект с полным составом величин: долг, денежные средства, операционная
# прибыль и амортизация — чтобы считались и долговая нагрузка, и автономия.
BALANCE = """
Консолидированный отчёт о финансовом положении
(в миллионах российских рублей)
Пояснения      31 декабря 2025 года      31 декабря 2024 года
Основные средства                       700 000        650 000
Итого внеоборотные активы               700 000        650 000
Запасы                                  300 000        280 000
Денежные средства и их эквиваленты      500 000        430 000
Итого оборотные активы                  800 000        710 000
Итого активы                          1 500 000      1 360 000
Акционерный капитал                     400 000        400 000
Нераспределённая прибыль                200 000        160 000
Итого капитал                           600 000        560 000
Долгосрочные кредиты и займы            500 000        500 000
Итого долгосрочные обязательства        500 000        500 000
Краткосрочные кредиты и займы           400 000        300 000
Итого краткосрочные обязательства       400 000        300 000
Итого обязательства                     900 000        800 000
Итого капитал и обязательства         1 500 000      1 360 000

Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка                               1 200 000      1 100 000
Себестоимость продаж                    (800 000)      (750 000)
Валовая прибыль                         400 000        350 000
Операционная прибыль                    300 000        260 000
Амортизация основных средств             40 000         38 000
Финансовые расходы 9                    (50 000)       (48 000)
Прибыль до налогообложения              260 000        220 000
Расход по налогу на прибыль              (52 000)       (44 000)
Прибыль за период                       208 000        176 000
"""

HEADER = (
    "\n(в миллионах российских рублей)\n"
    "по состоянию на 31 декабря 2025 года и 31 декабря 2024 года\n"
    + "\nПримечания к консолидированной финансовой отчётности.\n" * 40
)


def loaded(db_conn, notes: tuple[NoteValue, ...] = (), audit=None) -> None:
    """Проводит комплект через приём, разбор, сверку и запись.

    Единственная неопознанная строка подтверждается человеком: иначе комплект
    уходит в карантин и в расчёт не идёт вовсе — инвариант 6 действует и здесь,
    и проверять на карантинном комплекте было бы нечего.

    Основания экрана сверки принимаются **поимённо и с причиной**: карантин
    снимается только по названным, а не подтверждением вообще.
    """
    text = CONTENTS + BALANCE
    profile = identify(text + HEADER)
    assert profile.accepted, getattr(profile, "reason", "")
    extraction = extract(text, DATES, Grouping.RUSSIAN)
    decision = review(extraction, profile)
    load_extraction(
        INN,
        extraction,
        profile,
        decision,
        db_conn,
        DocumentReading(audit=audit, notes=notes, issuer_type="corporate"),
        confirmed_by="аналитик",
        confirmations={"Амортизация основных средств": "ifrs.depreciation"},
        accepted={
            item.value: "проверено человеком на синтетическом комплекте"
            for item in decision.reasons
        },
    )


def test_note_value_becomes_a_fact_of_its_form(db_conn) -> None:
    """Величина примечания — факт формы, строка которой на примечание ссылается.

    Код у неё свой: `ifrs.interest_expense_accrued` и `ifrs.finance_costs` —
    два разных факта одной формы. У Автодора в строке формы 414, а начислено
    по примечанию 54 382, и подменять одно другим нельзя.
    """
    note = NoteValue(
        code="ifrs.interest_expense_accrued",
        value=Decimal(70_000),
        note=9,
        rows=("Процентный расход по кредитам и облигациям",),
        note_title="Финансовые доходы и расходы",
        from_line="ifrs.finance_costs",
    )
    loaded(db_conn, notes=(note,))

    rows = fetch_all(
        "SELECT line_code, value, recognition, note_number, note_source_name, form_code "
        "FROM fact_report WHERE inn = %(i)s AND standard = 'ifrs' "
        "AND report_date = %(d)s AND line_code IN "
        "('ifrs.finance_costs', 'ifrs.interest_expense_accrued') ORDER BY line_code",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    by_code = {row["line_code"]: row for row in rows}
    # Оба факта лежат рядом, и величины разные.
    assert by_code["ifrs.finance_costs"]["value"] == Decimal(-50_000)
    assert by_code["ifrs.interest_expense_accrued"]["value"] == Decimal(70_000)
    # Форма у величины примечания — форма строки, которая на него ссылается.
    assert (
        by_code["ifrs.interest_expense_accrued"]["form_code"]
        == by_code["ifrs.finance_costs"]["form_code"]
    )
    # Источник назван: сила опознания, номер примечания и наименование строки.
    accrued = by_code["ifrs.interest_expense_accrued"]
    assert accrued["recognition"] == "note"
    assert accrued["note_number"] == 9
    assert "Процентный расход" in accrued["note_source_name"]
    # А у величины формы пометки примечания нет вовсе.
    assert by_code["ifrs.finance_costs"]["recognition"] == "catalog"
    assert by_code["ifrs.finance_costs"]["note_number"] is None


def test_refused_note_value_is_written_as_a_refusal(db_conn) -> None:
    """Отказ извлечения из примечания — запись в журнале, а не отсутствие факта.

    У Норникеля капитализированные проценты раскрыты прозой примечания.
    Показатель, которому величины не хватило, обязан назвать причину, иначе
    она останется в памяти того, кто смотрел документ.
    """
    from finlib.sources.ifrs_notes import Refusal

    refused = NoteValue(
        code="ifrs.interest_capitalised",
        refusal=Refusal.LINE_NOT_FOUND,
        note=9,
        from_line="ifrs.finance_costs",
    )
    loaded(db_conn, notes=(refused,))

    rows = fetch_all(
        "SELECT check_code, status, message, line_code FROM dq_log "
        "WHERE inn = %(i)s AND check_code IN "
        "('note_value_not_extracted', 'note_values')",
        {"i": INN},
        conn=db_conn,
    )
    codes = {row["check_code"] for row in rows}
    assert codes == {"note_value_not_extracted", "note_values"}
    refusal = next(row for row in rows if row["check_code"] == "note_value_not_extracted")
    assert refusal["line_code"] == "ifrs.interest_capitalised"
    assert "не извлечена" in refusal["message"]
    # Счётчик проверенного стоит рядом: сколько взято из скольких объявленных.
    summary = next(row for row in rows if row["check_code"] == "note_values")
    assert "взято 0 из 1" in summary["message"]

    # Факта при этом нет — и это верно: величина не получена.
    assert (
        fetch_one(
            "SELECT count(*) AS n FROM fact_report WHERE inn = %(i)s "
            "AND line_code = 'ifrs.interest_capitalised'",
            {"i": INN},
            conn=db_conn,
        )["n"]
        == 0
    )


def test_metrics_and_assessment_come_from_the_facts(db_conn) -> None:
    """Показатели и оценка считаются по фактам базы и в базу же пишутся."""
    note = NoteValue(
        code="ifrs.interest_expense_accrued",
        value=Decimal(70_000),
        note=9,
        rows=("Процентный расход",),
        from_line="ifrs.finance_costs",
    )
    loaded(db_conn, notes=(note,))

    policy = load_ifrs_metrics()
    # Периоды — оба: сравнительная колонка загружена фактами, и показатели
    # по ней считаются. Доверие к ней ниже, и это объявлено признаком.
    assert periods_of(INN, db_conn) == DATES
    confidence = confidence_of(INN, db_conn)
    assert confidence[DATES[0]] is PeriodConfidence.VERIFIED
    assert confidence[DATES[1]] is PeriodConfidence.COMPARATIVE_ONLY
    inputs = inputs_of(INN, DATES[0], db_conn, policy)
    # Величина примечания пришла в свой словарь, а не в величины форм: в состав
    # итогов формы она не входит.
    assert inputs.notes["ifrs.interest_expense_accrued"] == Decimal(70_000)
    assert "ifrs.interest_expense_accrued" not in inputs.values
    assert inputs.issuer_type == "corporate"
    assert inputs.months == 12

    computed = compute_from_facts(INN, DATES[0], db_conn, policy)
    by_code = {item.code: item for item in computed}
    # Покрытие процентов считается именно по величине примечания: 300 000 / 70 000.
    assert by_code["interest_cover_accrued"].value is not None
    assert by_code["interest_cover_accrued"].value.quantize(
        Decimal("0.001")
    ) == Decimal("4.286")
    assert by_code["equity_ratio"].value == Decimal(600_000) / Decimal(1_500_000)

    assert save_metrics(INN, DATES[0], computed, db_conn, policy) == len(computed)
    result = assess(computed, policy, stop_factors_of(INN, DATES[0], computed, db_conn))
    assert save_ifrs_assessment(INN, DATES[0], result, computed, db_conn, policy)

    stored = fetch_one(
        "SELECT class_code, total_score FROM assessment WHERE inn = %(i)s "
        "AND standard = 'ifrs' AND report_date = %(d)s",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    assert stored["class_code"] == result.class_code
    values = fetch_all(
        "SELECT metric_code, status FROM metric_value WHERE inn = %(i)s "
        "AND standard = 'ifrs'",
        {"i": INN},
        conn=db_conn,
    )
    assert {row["metric_code"] for row in values} == set(by_code)
    # Отказ хранится отказом, а не отсутствующей строкой: причина нужна
    # «Ограничениям анализа».
    assert any(row["status"] == "not_calculable" for row in values)


def test_document_is_built_from_ifrs_facts(db_conn, tmp_path) -> None:
    """Заключение по МСФО собирается и говорит о своём стандарте.

    Проверяется не вёрстка, а то, чем документ наполнен: состав фактической
    базы взят у своего стандарта, статья названа наименованием позиции,
    величина из примечания — вместе с номером примечания, а оговорки пришли
    из справочников ветки МСФО. Прежде состав брался у РСБУ: перечень
    не находил ни одной величины и проходил как выполненный, а в оговорки
    попадало «в расчёт входят только строки 1410 и 1510» — утверждение
    о другой отчётности.
    """
    from finlib.report.document import build_report
    from finlib.standards import Standard

    note = NoteValue(
        code="ifrs.interest_expense_accrued",
        value=Decimal(70_000),
        note=9,
        rows=("Процентный расход",),
        from_line="ifrs.finance_costs",
    )
    loaded(db_conn, notes=(note,))
    policy = load_ifrs_metrics()
    computed = compute_from_facts(INN, DATES[0], db_conn, policy)
    save_metrics(INN, DATES[0], computed, db_conn, policy)
    result = assess(computed, policy, stop_factors_of(INN, DATES[0], computed, db_conn))
    save_ifrs_assessment(INN, DATES[0], result, computed, db_conn, policy)

    made = build_report(
        INN,
        db_conn,
        standard=Standard.IFRS,
        with_text=False,
        directory=tmp_path,
        is_test=True,
    )
    from docx import Document

    text = "\n".join(item.text for item in Document(made.path).paragraphs)

    # Неразрывные пробелы разрядов приводятся к обычным: проверяется
    # содержание, а не вёрстка числа.
    plain = text.replace("\u00a0", " ")

    # Единица — комплекта, а не РСБУ: фикстура объявляет миллионы, и документ
    # обязан печатать их. «тыс. руб.» здесь были бы ошибкой в тысячу раз.
    assert "Итого активы — 1 500 000 млн руб." in plain
    assert "Процентные расходы, начисленные по заёмным средствам" in plain
    # Ссылка на примечание стоит рядом с величиной: без неё покрытие процентов
    # не совпадает ни с одной строкой отчёта о прибыли или убытке.
    assert "примечание 9" in plain
    # Оговорка о строках РСБУ в заключении по МСФО — утверждение о другой
    # отчётности, и её здесь быть не должно.
    assert "1410" not in plain and "1510" not in plain
    # Технических идентификаторов в тексте нет: показатель назван наименованием.
    assert "net_debt" not in plain and "debt_maturity_cover" not in plain
    # Отчётность называется по стандарту: «бухгалтерская отчётность»
    # в заключении по консолидированной — утверждение о другом предмете.
    assert "консолидированной финансовой отчётности по МСФО" in plain
    assert "бухгалтерской отчётности" not in plain
    # Оговорки о стоп-факторах ветки здесь нет намеренно: механизм задачи 26
    # существует, но в расчёт по фактам не подключён, и утверждать
    # «не применяются» значило бы выдать наш пробел за решение методики.
    assert "Стоп-факторы, ограничивающие класс" not in plain


def test_qualified_opinion_reaches_the_document(db_conn, tmp_path) -> None:
    """Оговорка аудитора доходит до документа: сигнал, цитата, уверенность.

    У годового комплекта ФосАгро мнение с оговоркой — не раскрыта информация
    о сегментах, требуемая МСФО (IFRS) 8, — и в первом заключении по МСФО
    об этом не было ни слова: раздел 4 был пуст, раздел 7 на него ссылался,
    а уверенность в оценке стояла высшей.

    **Следствий вида оговорки при этом нет, пока вид не установлен человеком.**
    Оговорка о раскрытии величин не затрагивает, и понижать за неё уверенность
    в показателях не за что; приметы основания дают предложение машины,
    а решение остаётся за человеком.
    """
    from docx import Document

    from finlib.report.document import build_report
    from finlib.sources.ifrs_audit import (
        AuditReport,
        Determination,
        Engagement,
        SectionText,
    )
    from finlib.standards import Standard

    basis = (
        "Руководство Группы не раскрыло информацию о сегментах, требуемую "
        "стандартом МСФО (IFRS) 8 «Операционные сегменты»."
    )
    audit = AuditReport(
        Determination.DETERMINED,
        engagement=Engagement.AUDIT,
        opinion="qualified",
        opinion_name="Мнение с оговоркой",
        modified=True,
        sections=("basis_for_opinion",),
        texts=(
            SectionText(
                code="basis_for_opinion",
                name="Основание для выражения мнения",
                text=basis,
            ),
        ),
        auditor="АО «Аудитор»",
        signed_on="10 марта 2026 года",
    )
    loaded(db_conn, audit=audit)
    policy = load_ifrs_metrics()
    computed = compute_from_facts(INN, DATES[0], db_conn, policy)
    save_metrics(INN, DATES[0], computed, db_conn, policy)
    result = assess(computed, policy, stop_factors_of(INN, DATES[0], computed, db_conn))
    save_ifrs_assessment(INN, DATES[0], result, computed, db_conn, policy)

    # Уверенность понижена ступенью, и причина названа словами.
    stored = fetch_one(
        "SELECT confidence, confidence_reasons FROM assessment WHERE inn = %(i)s "
        "AND standard = 'ifrs' AND report_date = %(d)s",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    # Уверенность понижена, но **не заключением**: основания — принятое
    # человеком основание карантина и группы из одного показателя. Вид оговорки
    # человеком не установлен, следствия вида не применялись, и модифицированное
    # мнение уверенность не понижает: у ФосАгро оговорка о раскрытии,
    # а не о величинах.
    assert stored["confidence"] == "low"
    assert any("по одному" in item for item in stored["confidence_reasons"])
    assert any("решением человека" in item for item in stored["confidence_reasons"])
    assert not any("аудитор" in item.lower() for item in stored["confidence_reasons"])

    made = build_report(
        INN,
        db_conn,
        standard=Standard.IFRS,
        with_text=False,
        directory=tmp_path,
        is_test=True,
    )
    text = "\n".join(item.text for item in Document(made.path).paragraphs)

    # Сигнал в разделе 4 — с предписанной формулировкой и основанием. Уровень
    # «требует внимания», а не надзорный: вид оговорки человеком не установлен,
    # и приписывать ей следствия оговорки о величинах нельзя.
    assert "Вид оговорки аудитора не установлен (требует внимания)." in text
    assert "Вид мнения: Мнение с оговоркой" in text
    # Предложение машины печатается человеку, который вид и подтверждает.
    assert "«Оговорка о полноте раскрытий», решение человека не принято" in text
    # Слова аудитора приведены дословно и с указанием источника.
    assert basis in text
    assert "Из аудиторского заключения, раздел «Основание для выражения мнения»" in text
    # Порядок определения уверенности — свой у ветки: флаги и длина ряда,
    # которых ветка не считает, в основаниях стоять не вправе.
    assert "Основания заданы методикой" in text
    assert "Сработал флаг" not in text
    # Эскалация от неустановленного вида не возникает: она следует
    # из надзорного уровня, а его здесь нет.
    assert "Эскалация." not in text
    # Сигнал даёт вопрос: иначе «Запрос пояснений» отсылает к разделу,
    # в котором «расчётом не выявлено».
    assert "«Вид оговорки аудитора не установлен»" in text


def test_caveat_about_values_confirmed_by_a_human_lowers_confidence(
    db_conn, tmp_path
) -> None:
    """Оговорка о величинах, подтверждённая человеком: надзорный уровень и эскалация.

    Соразмерность различает два обстоятельства: у ФосАгро не раскрыты сегменты —
    величины подтверждены, у Автодора занижен резерв — под вопросом сами числа,
    из которых считаются показатели. Прежде вид оговорки не различался вовсе,
    и оба понижали уверенность одинаково.
    """
    from docx import Document

    from finlib.report.document import build_report
    from finlib.sources.ifrs_audit import (
        AuditReport,
        Determination,
        Engagement,
        SectionText,
    )
    from finlib.standards import Standard

    basis = (
        "По нашему мнению, резерв под ожидаемые кредитные убытки занижен: "
        "оценочное обязательство признано не в полной сумме."
    )
    audit = AuditReport(
        Determination.DETERMINED,
        engagement=Engagement.AUDIT,
        opinion="qualified",
        opinion_name="Мнение с оговоркой",
        modified=True,
        sections=("basis_for_opinion",),
        texts=(
            SectionText(
                code="basis_for_opinion",
                name="Основание для выражения мнения",
                text=basis,
            ),
        ),
        auditor="АО «Аудитор»",
        signed_on="10 марта 2026 года",
        caveat_kind="about_values",
        caveat_confirmed_by="аналитик",
    )
    loaded(db_conn, audit=audit)
    policy = load_ifrs_metrics()
    computed = compute_from_facts(INN, DATES[0], db_conn, policy)
    save_metrics(INN, DATES[0], computed, db_conn, policy)
    result = assess(computed, policy, stop_factors_of(INN, DATES[0], computed, db_conn))
    save_ifrs_assessment(INN, DATES[0], result, computed, db_conn, policy)

    stored = fetch_one(
        "SELECT confidence_reasons FROM assessment WHERE inn = %(i)s "
        "AND standard = 'ifrs' AND report_date = %(d)s",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    assert any(
        "оговоркой о величинах" in item for item in stored["confidence_reasons"]
    )

    made = build_report(
        INN,
        db_conn,
        standard=Standard.IFRS,
        with_text=False,
        directory=tmp_path,
        is_test=True,
    )
    text = "\n".join(item.text for item in Document(made.path).paragraphs)
    assert (
        "Мнение аудитора модифицировано оговоркой о величинах (надзорный сигнал)."
        in text
    )
    assert "вид оговорки подтвердил аналитик" in text
    # Надзорный уровень включает эскалацию — в отличие от оговорки о раскрытии.
    assert "Эскалация." in text


def test_going_concern_section_reaches_the_document(db_conn, tmp_path) -> None:
    """Неопределённость непрерывности: стоп-фактор, сигнал, вопрос о планах.

    У Сегежи мнение немодифицированное, а раздел о непрерывности объявлен —
    и соразмерность выходила перевёрнутой: уверенность высшая, эскалации нет.
    Заодно проверяется, что при немодифицированном мнении «Основание
    для выражения мнения» не цитируется вовсе: там предписанное МСА описание
    процедур аудита, а не оговорка об отчётности.
    """
    from docx import Document

    from finlib.report.document import build_report
    from finlib.sources.ifrs_audit import (
        AuditReport,
        Determination,
        Engagement,
        SectionText,
    )
    from finlib.standards import Standard

    procedures = (
        "Мы провели аудит в соответствии с Международными стандартами аудита."
    )
    going = (
        "В Примечании 2 указано на превышение краткосрочных обязательств над "
        "краткосрочными активами Группы. Планы руководства Группы в отношении "
        "этих обстоятельств представлены в Примечании 2."
    )
    audit = AuditReport(
        Determination.DETERMINED,
        engagement=Engagement.AUDIT,
        opinion="unmodified",
        opinion_name="Немодифицированное мнение",
        modified=False,
        sections=("basis_for_opinion", "going_concern_uncertainty"),
        texts=(
            SectionText(
                code="basis_for_opinion",
                name="Основание для выражения мнения",
                text=procedures,
            ),
            SectionText(
                code="going_concern_uncertainty",
                name="Существенная неопределённость в отношении непрерывности "
                "деятельности",
                text=going,
            ),
        ),
        auditor="АО «Аудитор»",
        signed_on="10 марта 2026 года",
    )
    loaded(db_conn, audit=audit)
    policy = load_ifrs_metrics()
    computed = compute_from_facts(INN, DATES[0], db_conn, policy)
    save_metrics(INN, DATES[0], computed, db_conn, policy)
    stops = stop_factors_of(INN, DATES[0], computed, db_conn)
    # Условие стоп-фактора — сам раздел заключения, и показателя у него нет.
    assert "going_concern_uncertainty" in stops.triggered
    result = assess(computed, policy, stops)
    save_ifrs_assessment(INN, DATES[0], result, computed, db_conn, policy, stops)

    stored = fetch_one(
        "SELECT stop_factor_code, stop_factor_codes, confidence FROM assessment "
        "WHERE inn = %(i)s AND standard = 'ifrs' AND report_date = %(d)s",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    assert stored["stop_factor_code"] == "going_concern_uncertainty"
    assert "going_concern_uncertainty" in stored["stop_factor_codes"]
    assert stored["confidence"] != "high"

    made = build_report(
        INN,
        db_conn,
        standard=Standard.IFRS,
        with_text=False,
        directory=tmp_path,
        is_test=True,
    )
    text = "\n".join(item.text for item in Document(made.path).paragraphs)

    # Надзорный сигнал и стоп-фактор — оба, и оба из заключения.
    assert "Существенная неопределённость в отношении непрерывности деятельности" in text
    assert going in text
    # Раздел о процедурах аудита не цитируется: мнение немодифицировано.
    assert procedures not in text
    # Вопрос о планах руководства называет примечание, указанное аудитором.
    assert "Планы руководства раскрыты в примечании 2" in text
    # Надзорный сигнал даёт эскалацию в предложениях по действиям.
    assert "Эскалация." in text


def test_foreign_standard_marks_are_blocking(db_conn, tmp_path) -> None:
    """Документ по МСФО не говорит словами РСБУ — правило структурное.

    Дефект этого класса повторился семь раз: оговорка показателя, оговорка
    строки, наименование показателя, наименование контроля, версия методики,
    состав фактической базы, тезисы. Искать их по одному значило бы находить
    их по одному и впредь, поэтому правило ищет приметы, а не перечень
    известных случаев.
    """
    from finlib.llm.textcheck import (
        SEVERITY,
        Severity,
        TextContext,
        TextRule,
        check_foreign_standard,
    )
    from finlib.standards import Standard

    assert SEVERITY[TextRule.FOREIGN_STANDARD_MARK] is Severity.BLOCKING

    context = TextContext(
        foreign_names=frozenset({"Коэффициент текущей ликвидности"}),
        foreign_versions=frozenset({"1.3.0"}),
    )
    # Код строки РСБУ, ссылка на строку, чужое наименование и чужая версия —
    # четыре приметы, и каждая ловится отдельно.
    found = check_foreign_standard(
        "Валюта баланса (1600) — 1 500 000. По строке 1410 раскрыт долг. "
        "Коэффициент текущей ликвидности 0,81. Версия справочника: 1.3.0.",
        Standard.IFRS,
        context,
    )
    assert len(found) == 4
    assert all(item.rule is TextRule.FOREIGN_STANDARD_MARK for item in found)

    # Год четырёхзначным кодом не считается: он стоит в датах и в периодах.
    clean = check_foreign_standard(
        "Отчётная дата 31.12.2025, сравнительный период — 2024 год.",
        Standard.IFRS,
        context,
    )
    assert clean == []

    # К документу по РСБУ правило не применяется вовсе: там эти приметы свои.
    assert check_foreign_standard("Валюта баланса (1600)", Standard.RSBU, context) == []


def test_ifrs_theses_cover_the_scored_groups(db_conn) -> None:
    """Тезисы ветки покрывают группы, по которым считается балл.

    Долговая нагрузка и обслуживание долга несут 70 % веса: раздел без них
    описывал бы не то, по чему присвоен класс. Основание тезиса — часть
    калибровочной шкалы: бесспорных ориентиров у ветки нет, и абсолютных
    нормативов методика не содержит принципиально.
    """
    from finlib.scoring.theses import build_ifrs_theses

    note = NoteValue(
        code="ifrs.interest_expense_accrued",
        value=Decimal(70_000),
        note=9,
        rows=("Процентный расход",),
        from_line="ifrs.finance_costs",
    )
    loaded(db_conn, notes=(note,))
    policy = load_ifrs_metrics()
    computed = compute_from_facts(INN, DATES[0], db_conn, policy)
    save_metrics(INN, DATES[0], computed, db_conn, policy)

    found = build_ifrs_theses(INN, db_conn, report_date=DATES[0])
    groups = {name for name, _ in found.by_group()}
    assert {"Долговая нагрузка", "Обслуживание долга"} <= groups
    # Наименования показателей — свои: «Текущая ликвидность», а не
    # «Коэффициент текущей ликвидности».
    text = "\n".join(item.text for item in found.theses)
    assert "Коэффициент текущей ликвидности" not in text
    assert "калибровочной шкалы методики" in text


def test_score_is_level_only_by_declared_rule() -> None:
    """Балл МСФО равен уровню, динамика справочно — и это объявлено методикой."""
    policy = load_ifrs_metrics()
    assert policy.scoring.score_from == "level_only"
    assert policy.scoring.dynamics_use == "reference_only"
    assert policy.scoring.dynamics_in_score is False
    assert policy.scoring.origin.strip() and policy.scoring.dynamics_origin.strip()
