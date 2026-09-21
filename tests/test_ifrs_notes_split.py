"""Оговорка о содержании и описание методики — разные поля и разные судьбы.

Чтение первого заключения по МСФО глазами: в «Ограничениях анализа» по ФосАгро
стояло «Пик погашения решает маршрутизацию даже при умеренной нагрузке»
и «у ЛСР расхождение между двумя мерами оказалось наибольшим из наблюдавшихся».
Первое — наше рабочее соображение, второе — сведение о другой организации.
Правило то же, что в `metrics.yaml` у РСБУ: `note` идёт в документ,
`methodology_note` не идёт никуда.
"""

from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.report.policy import load_policy


def test_document_notes_do_not_carry_our_working_words() -> None:
    """Оговорка, идущая в документ, не говорит о маршрутизации и о наборе.

    Слова объявлены здесь, а не выведены: правило против конкретного класса
    протечки — рабочая пометка, написанная для нас, попавшая читателю.
    """
    forbidden = ("маршрутизац", "ни у кого", "наблюдавшихся", "тот же, что в РСБУ")
    for metric in load_ifrs_metrics().metrics:
        if not metric.note:
            continue
        lowered = " ".join(metric.note.split()).casefold()
        for word in forbidden:
            assert word.casefold() not in lowered, f"{metric.code}: {word}"


def test_other_issuers_are_not_named_in_document_notes() -> None:
    """Оговорка документа не называет другого эмитента.

    Перечень эмитентов берётся из того же правила, которым проверяется
    собранный документ: два места, одно понятие.
    """
    rule = load_policy().other_issuers
    known = ("ПАО «Группа ЛСР»", "ПАО «Акрон»", "ПАО «Группа Черкизово»")
    phrases = [rule.phrase_of(name) for name in known]
    for metric in load_ifrs_metrics().metrics:
        if not metric.note:
            continue
        lowered = " ".join(metric.note.split()).casefold()
        for phrase in phrases:
            assert phrase and phrase not in lowered, f"{metric.code}: {phrase}"


def test_methodology_note_keeps_what_was_taken_out() -> None:
    """Вынесенное из оговорки не потеряно, а объявлено описанием методики.

    Потеря сведения здесь тише всего: наблюдение по набору исчезло бы вместе
    с протечкой, и причина решения осталась бы неизвестной.
    """
    by_code = {item.code: item for item in load_ifrs_metrics().metrics}
    assert "ЛСР" in (by_code["ffo_to_debt"].methodology_note or "")
    assert "маршрутизаци" in (by_code["debt_maturity_cover"].methodology_note or "")
    assert "РСБУ" in (by_code["net_debt_ebitda"].methodology_note or "")


def test_a_generic_word_of_a_name_does_not_fire() -> None:
    """Обычное слово языка в наименовании признаком не становится.

    У «ДОМ.РФ Ипотечный агент» опознавательная часть — четыре слова, и слово
    «дом» само по себе запретом быть не может: первое измерение правила
    блокировало по нему всякий документ, включая верные.
    """
    from finlib.llm.textcheck import TextContext, check_other_issuers

    rule = load_policy().other_issuers
    phrase = rule.phrase_of('Общество с ограниченной ответственностью "ДОМ.РФ ИПОТЕЧНЫЙ АГЕНТ"')
    context = TextContext(other_issuers=frozenset({phrase}))
    assert not check_other_issuers("Организация владеет домом и складом.", context)
    assert check_other_issuers(f"Сравнение с {phrase}.", context)
