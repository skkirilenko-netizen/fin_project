"""Тесты реестра контролей: написанное обязано вызываться.

Третий случай конструкции «написано, но не вызывается» — после `check_text`
и `check_calculated`. Оба раза код был верен, тесты зелены, а в боевом пути
его никто не звал, и нули в замерах читались как чистый результат. Память
об этом не работает, поэтому здесь механическая проверка.
"""

from finlib.quality.codes import CheckCode
from finlib.quality.wiring import (
    REGISTRY,
    calls_in_sources,
    live_calls,
    reachable_modules,
    summary,
    unwired,
)


def test_every_check_code_is_registered() -> None:
    """Новый код контроля нельзя завести молча.

    Код без записи в реестре — это контроль, о котором неизвестно, работает
    ли он: ровно то состояние, из которого выросли все три случая.
    """
    missing = set(CheckCode) - set(REGISTRY)
    assert not missing, sorted(item.value for item in missing)


def test_wired_checks_are_actually_called() -> None:
    """Контроль, числящийся действующим, обязан иметь вызов, достижимый из цикла.

    Прямая страховка от первого случая: `check_text` числился рабочим
    и не выполнялся, потому что цикл не передавал ему контекст. Упоминания
    кода мало — модуль, в котором он назван, обязан быть достижим
    от `finlib.pipeline`, иначе контроль в боевом пути не выполняется вовсе.
    """
    idle = {
        code.value: item.called_from
        for code, item in REGISTRY.items()
        if item.wired and not live_calls(code)
    }
    assert not idle, idle


def test_reachability_is_computed_from_the_pipeline() -> None:
    """Достижимость считается от цикла, а не от факта существования файла.

    Без этого реестр считал бы подключённым всякий контроль, чей код
    где-нибудь упомянут, — то есть повторил бы ошибку, от которой заведён.
    Проверяется обе стороны: модуль цикла достижим, а модуль, который цикл
    не импортирует, — нет, хотя файл существует и работает.
    """
    live = reachable_modules()
    assert "finlib.quality.checks" in live
    assert "finlib.pipeline" in live
    # Терминальный слой цикл не импортирует: он сам вызывает цикл.
    assert "finlib.cli" not in live
    # Сам реестр тоже недостижим — и это верно: он описывает проверки,
    # а не выполняет их.
    assert "finlib.quality.wiring" not in live


def test_wired_checks_are_called_where_declared() -> None:
    """Объявленное место вызова совпадает с действительным.

    Реестр не только утверждает, что контроль работает, но и говорит где:
    перечень мест — то, по чему при разборе отказа ищут причину.
    """
    wrong: dict[str, dict[str, tuple[str, ...]]] = {}
    for code, item in REGISTRY.items():
        if not item.wired:
            continue
        actual = live_calls(code)
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
        code.value: live_calls(code)
        for code, item in REGISTRY.items()
        if not item.wired and live_calls(code)
    }
    assert not stale, (
        f"контроль вызывается, но числится неподключённым: {stale}. "
        "Переведите запись в реестре в wired и назовите места вызова"
    )


def test_code_written_but_unreachable_is_not_called_wired(monkeypatch) -> None:
    """Код, написанный и не достижимый из цикла, подключённым не считается.

    Разница между `calls_in_sources` и `live_calls` — это и есть третий
    случай: код написан, покрыт тестами, упомянут в своём модуле, а цикл
    его не зовёт. Здесь она проверяется прямо: при опустевшем графе
    достижимости ни один контроль не считается работающим, хотя все
    упоминания на месте.
    """
    import finlib.quality.wiring as module

    code = CheckCode.BALANCE_EQUALITY
    assert calls_in_sources(code), "контроль должен быть упомянут в исходниках"
    assert live_calls(code), "и достижим от цикла"

    monkeypatch.setattr(module, "reachable_modules", frozenset)
    assert calls_in_sources(code), "упоминания никуда не делись"
    assert not module.live_calls(code), "но вызовом они быть перестали"


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


def test_ifrs_plausibility_is_wired_after_task_23() -> None:
    """Проверка правдоподобия конвенции подключена к экрану сверки.

    Нулевой пункт задачи 23. До него она была написана, покрыта тестами
    и не вызывалась ниоткуда: сверять сумму разделов с итогом было не с чем.
    Реестр это фиксировал, и перевод в wired потребовался ровно тогда,
    когда вызов появился.
    """
    entry = REGISTRY[CheckCode.DIGIT_GROUPING_IMPLAUSIBLE]
    assert entry.wired
    assert "sources/ifrs_review.py" in entry.called_from
    assert CheckCode.DIGIT_GROUPING_IMPLAUSIBLE not in unwired()


def test_ifrs_intake_checks_are_all_wired() -> None:
    """Все отказы приёма документа МСФО достижимы от цикла.

    Восемь контролей ветки перешли в wired вместе с подключением
    `pipeline.accept_ifrs_document`.
    """
    intake = (
        CheckCode.FILE_TEXT_LAYER_MISSING,
        CheckCode.FILE_NOT_STATEMENTS,
        CheckCode.FINANCIAL_INSTITUTION,
        CheckCode.FILE_CURRENCY_NOT_DETERMINED,
        CheckCode.FILE_CURRENCY_NOT_ROUBLE,
        CheckCode.FILE_PERIODS_NOT_DETERMINED,
        CheckCode.DIGIT_GROUPING_NOT_DETERMINED,
        CheckCode.DIGIT_GROUPING_IMPLAUSIBLE,
    )
    for code in intake:
        assert REGISTRY[code].wired, code.value
        assert live_calls(code), code.value
