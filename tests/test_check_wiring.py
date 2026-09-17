"""Тесты реестра контролей: написанное обязано вызываться.

Третий случай конструкции «написано, но не вызывается» — после `check_text`
и `check_calculated`. Оба раза код был верен, тесты зелены, а в боевом пути
его никто не звал, и нули в замерах читались как чистый результат. Память
об этом не работает, поэтому здесь механическая проверка.
"""

from finlib.quality.codes import CheckCode
from finlib.quality.wiring import REGISTRY, calls_in_sources, summary, unwired


def test_every_check_code_is_registered() -> None:
    """Новый код контроля нельзя завести молча.

    Код без записи в реестре — это контроль, о котором неизвестно, работает
    ли он: ровно то состояние, из которого выросли все три случая.
    """
    missing = set(CheckCode) - set(REGISTRY)
    assert not missing, sorted(item.value for item in missing)


def test_wired_checks_are_actually_called() -> None:
    """Контроль, числящийся действующим, обязан иметь боевой вызов.

    Это прямая страховка от первого случая: `check_text` числился рабочим
    и не выполнялся, потому что цикл не передавал ему контекст.
    """
    idle = {
        code.value: item.called_from
        for code, item in REGISTRY.items()
        if item.wired and not calls_in_sources(code)
    }
    assert not idle, idle


def test_wired_checks_are_called_where_declared() -> None:
    """Объявленное место вызова совпадает с действительным.

    Реестр не только утверждает, что контроль работает, но и говорит где:
    перечень мест — то, по чему при разборе отказа ищут причину.
    """
    wrong: dict[str, dict[str, tuple[str, ...]]] = {}
    for code, item in REGISTRY.items():
        if not item.wired:
            continue
        actual = calls_in_sources(code)
        declared = set(item.called_from)
        if not declared <= set(actual):
            wrong[code.value] = {"объявлено": item.called_from, "найдено": actual}
    assert not wrong, wrong


def test_unwired_checks_really_have_no_call() -> None:
    """Числящийся неподключённым обязан не иметь вызова.

    Вторая сторона проверки, и она не менее важна: устаревший `not_wired` —
    это работающий контроль, которому не верят. Когда задача 23 подключит
    проверку правдоподобия, этот тест потребует сменить статус.
    """
    stale = {
        code.value: calls_in_sources(code)
        for code, item in REGISTRY.items()
        if not item.wired and calls_in_sources(code)
    }
    assert not stale, (
        f"контроль вызывается, но числится неподключённым: {stale}. "
        "Переведите запись в реестре в wired и назовите места вызова"
    )


def test_unwired_checks_name_the_reason_and_the_task() -> None:
    """У неподключённого объявлены причина и задача, в которой он включается.

    Без этого запись `not_wired` через месяц неотличима от забытой:
    непонятно, ждёт ли контроль своей очереди или его просто не дописали.
    """
    for code, item in unwired().items():
        assert item.reason and item.reason.strip(), code.value
        assert item.planned_in and item.planned_in.strip(), code.value
        assert item.since, code.value


def test_registry_reports_its_own_state() -> None:
    """Реестр сообщает и общее число контролей, и число неподключённых.

    Счётчик проверенного рядом со счётчиком сработавшего — то же правило,
    что и для самих контролей.
    """
    text = summary()
    assert str(len(REGISTRY)) in text
    assert "не подключено" in text


def test_ifrs_plausibility_is_declared_not_wired_for_now() -> None:
    """Проверка правдоподобия конвенции пока не подключена — и это записано.

    Она готова и покрыта тестами, но разбора форм МСФО, из которого она
    вызывается, ещё нет. Запись говорит об этом прямо, чтобы отсутствие
    её срабатываний не читалось как «нарушений не найдено».
    """
    entry = REGISTRY[CheckCode.DIGIT_GROUPING_IMPLAUSIBLE]
    assert not entry.wired
    assert "задача 23" in entry.planned_in
    assert CheckCode.DIGIT_GROUPING_IMPLAUSIBLE in unwired()
