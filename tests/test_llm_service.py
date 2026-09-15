"""Тесты оркестровки: повторная попытка, отказ, журнал. Сеть не используется."""

from datetime import date

import httpx
import pytest

from finlib.db import execute, fetch_all, fetch_one
from finlib.llm.client import LLMClient
from finlib.llm.context import ConclusionContext
from finlib.llm.service import (
    MAX_ATTEMPTS,
    ConclusionRejectedError,
    build_prompt,
    generate_conclusion,
    load_prompt,
)

INN = "7736050003"
PERIOD = date(2025, 12, 31)

CONTEXT = ConclusionContext(
    inn=INN,
    report_date=PERIOD,
    organization="=== ОРГАНИЗАЦИЯ ===\nИНН: 7736050003",
    data="=== ДАННЫЕ ===\n1600  БАЛАНС  |  25 736 328 136",
    metrics="=== ПОКАЗАТЕЛИ ===\ncur_liq  «Коэффициент текущей ликвидности»  0,82",
    flags="=== ФЛАГИ ===\nФлагов не сработало.",
    assessment="=== ОЦЕНКА ===\nКласс: C",
    limitations="=== ОГРАНИЧЕНИЯ АНАЛИЗА ===\n- оговорка",
)

GOOD = (
    "### 2. Фактическая база\nВалюта баланса (1600) — 25 736 328 136 тыс. руб., "
    "ликвидность (cur_liq) — 0,82."
)
BAD = "### 2. Фактическая база\nРентабельность (roa) достигла 37,4 процента."

_CLEAN = "DELETE FROM llm_log WHERE inn = %(i)s AND is_test"


@pytest.fixture(autouse=True)
def clean_journal(db_conn):
    """Убирает только тестовые записи журнала.

    Боевые записи не трогаются ни при каких условиях: журнал обращений
    к модели — доказательная база системы, и прогон pytest однажды её уже
    уничтожил. Условие is_test стоит в обоих DELETE намеренно.
    """
    execute(_CLEAN, {"i": INN}, conn=db_conn)
    db_conn.commit()
    yield db_conn
    execute(_CLEAN, {"i": INN}, conn=db_conn)
    db_conn.commit()


def client_returning(*answers: str) -> LLMClient:
    """Клиент, отдающий заданные ответы по очереди."""
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        text = queue.pop(0) if queue else answers[-1]
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    return LLMClient(
        base_url="http://model.invalid/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )


def journal(inn: str = INN) -> list[dict]:
    """Тестовые записи журнала обращений к модели."""
    return fetch_all(
        "SELECT * FROM llm_log WHERE inn = %(i)s AND is_test ORDER BY id", {"i": inn}
    )


# --- приём и отказ ----------------------------------------------------------


def test_verified_answer_is_returned() -> None:
    """Прошедший проверку ответ возвращается наверх."""
    with client_returning(GOOD) as client:
        result = generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)
    assert result.text.startswith("### 2.")
    assert result.attempt == 1
    assert result.checked_numbers > 0


def test_rejected_answer_is_retried_then_fails() -> None:
    """Непрошедший ответ повторяется, затем ошибка."""
    with client_returning(BAD, BAD) as client, pytest.raises(ConclusionRejectedError) as info:
        generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)
    assert info.value.attempts == MAX_ATTEMPTS
    assert any("37,4" in item for item in info.value.foreign)


def test_second_attempt_can_succeed() -> None:
    """Если повтор прошёл проверку, он и возвращается."""
    with client_returning(BAD, GOOD) as client:
        result = generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)
    assert result.attempt == 2


def test_rejected_text_is_not_returned() -> None:
    """Отклонённый текст наверх не попадает: наружу идёт только ошибка."""
    produced = None
    with client_returning(BAD, BAD) as client:
        try:
            produced = generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)
        except ConclusionRejectedError as exc:
            # В сообщении об ошибке — перечень посторонних чисел, но не сам текст.
            assert "Рентабельность достигла" not in str(exc)
            assert "Фактическая база" not in str(exc)
    assert produced is None, "отклонённое заключение вернулось наверх"


def test_reasoning_is_stripped_from_result() -> None:
    """Черновик рассуждения в заключение не попадает."""
    answer = f"<think>прикину 12345</think>{GOOD}"
    with client_returning(answer) as client:
        result = generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)
    assert "<think>" not in result.text
    assert "12345" not in result.text


# --- журнал -----------------------------------------------------------------


def test_every_call_is_logged() -> None:
    """Каждое обращение к модели записывается, включая отклонённые."""
    with client_returning(BAD, GOOD) as client:
        generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)

    rows = journal()
    assert len(rows) == 2
    assert [row["attempt"] for row in rows] == [1, 2]
    assert [row["verified"] for row in rows] == [False, True]
    assert all(row["model"] == "test-model" for row in rows)
    assert all(row["prompt_name"] == "conclusion" for row in rows)
    assert all(row["temperature"] == 0 for row in rows)


def test_log_survives_rejection() -> None:
    """Запись об отклонении переживает исключение и откат транзакции вызывающего."""
    with client_returning(BAD, BAD) as client, pytest.raises(ConclusionRejectedError):
        generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)

    rows = journal()
    assert len(rows) == MAX_ATTEMPTS, "журнал по отклонённым ответам потерян"
    assert all(row["verified"] is False for row in rows)


def test_foreign_numbers_are_logged_with_context() -> None:
    """В журнал попадают посторонние числа вместе с их окружением."""
    with client_returning(BAD, BAD) as client, pytest.raises(ConclusionRejectedError):
        generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)

    row = journal()[0]
    numbers = row["foreign_numbers"]["numbers"]
    assert numbers
    assert numbers[0]["number"] == "37,4"
    assert numbers[0]["violation"] == "not_in_blocks"
    assert "Рентабельность" in numbers[0]["context"]


def test_forbidden_wording_is_logged_separately() -> None:
    """Отсылка к нормативу пишется в журнал своим разделом, а не среди чисел."""
    answer = "### 2. Фактическая база\nЛиквидность (cur_liq) — 0,82 при норме не менее."
    with client_returning(answer, answer) as client, pytest.raises(ConclusionRejectedError):
        generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)

    row = journal()[0]
    assert row["foreign_numbers"]["numbers"] == []
    assert row["foreign_numbers"]["wordings"]
    assert row["foreign_numbers"]["wordings"][0]["label"] == "норма"


def test_code_version_is_logged() -> None:
    """Обращение помечается версией кода, которой сделан прогон.

    Записи разных версий несопоставимы: правка инструкции или постпроверки
    меняет поведение текстового слоя целиком, и разбор журнала считает
    только текущую версию.
    """
    from finlib.version import code_version

    with client_returning(GOOD) as client:
        generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)

    assert journal()[0]["code_version"] == code_version()


def test_prompt_and_response_are_logged() -> None:
    """Текст инструкции и ответа сохраняются целиком."""
    with client_returning(GOOD) as client:
        generate_conclusion(INN, context=CONTEXT, client=client, is_test=True)

    row = journal()[0]
    assert "ОГРАНИЧЕНИЯ АНАЛИЗА" in row["prompt_text"]
    assert row["response_text"] == GOOD
    assert row["duration_ms"] is not None


# --- шаблон -----------------------------------------------------------------


def test_prompt_includes_rules() -> None:
    """Общие правила подставляются в шаблон, а не дублируются в нём."""
    prompt = load_prompt()
    assert "{rules}" not in prompt
    assert "ниже норматива" in prompt, "запрет формулировок о нормативах на месте"
    assert "не вычисляет" in prompt


def test_prompt_includes_all_blocks() -> None:
    """В инструкцию попадают все шесть блоков контекста."""
    prompt = build_prompt(CONTEXT)
    for block in ("ОРГАНИЗАЦИЯ", "ДАННЫЕ", "ПОКАЗАТЕЛИ", "ФЛАГИ", "ОЦЕНКА", "ОГРАНИЧЕНИЯ"):
        assert f"=== {block}" in prompt
    assert "{blocks}" not in prompt


def test_prompt_forbids_writing_first_section() -> None:
    """Раздел «Ключевой вывод» и приложение модель не пишет."""
    prompt = load_prompt()
    assert "Ключевой вывод" in prompt
    assert "формируются без тебя" in prompt


def test_model_never_sees_raw_file() -> None:
    """В инструкции нет ни пути к сырому файлу, ни его содержимого."""
    prompt = build_prompt(CONTEXT)
    assert "data/raw" not in prompt
    assert "girbo" not in prompt.lower()


def test_log_is_written_for_other_organisation_independently() -> None:
    """Журнал ведётся по каждой организации отдельно."""
    other = "2100010824"
    execute(_CLEAN, {"i": other})
    context = ConclusionContext(
        inn=other,
        report_date=date(2024, 12, 31),
        organization="=== ОРГАНИЗАЦИЯ ===\nИНН: 2100010824",
        data="=== ДАННЫЕ ===",
        metrics="=== ПОКАЗАТЕЛИ ===",
        flags="=== ФЛАГИ ===",
        assessment="=== ОЦЕНКА ===",
        limitations="=== ОГРАНИЧЕНИЯ АНАЛИЗА ===",
    )
    with client_returning("Текст без чисел.") as client:
        generate_conclusion(other, context=context, client=client, is_test=True)

    assert len(journal(other)) == 1
    assert fetch_one(
        "SELECT count(*) AS n FROM llm_log WHERE inn = %(i)s AND is_test", {"i": INN}
    )["n"] == 0
    execute(_CLEAN, {"i": other})
