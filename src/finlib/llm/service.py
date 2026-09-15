"""Порождение текстовой части заключения: запрос, постпроверка, журнал.

Ответ, не прошедший постпроверку, наверх не возвращается: делается одна
повторная попытка, затем ошибка. Каждое обращение записывается в llm_log
независимо от исхода — журнал ведётся и по отклонённым ответам.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from finlib.config import settings
from finlib.db import PgConnection, execute
from finlib.llm.client import Completion, LLMClient
from finlib.llm.context import ConclusionContext, build_context
from finlib.llm.verify import VerificationResult, strip_reasoning, verify
from finlib.standards import Standard

logger = logging.getLogger(__name__)

PROMPT_NAME = "conclusion"
MAX_ATTEMPTS = 2

_INSERT_LOG = """
INSERT INTO llm_log (
    inn, report_date, model, prompt_name, prompt_text, response_text,
    temperature, verified, foreign_numbers, attempt, duration_ms
) VALUES (
    %(inn)s, %(report_date)s, %(model)s, %(prompt_name)s, %(prompt_text)s, %(response_text)s,
    %(temperature)s, %(verified)s, %(foreign_numbers)s, %(attempt)s, %(duration_ms)s
)
"""


class ConclusionRejectedError(RuntimeError):
    """Ответ модели не прошёл постпроверку и наверх не возвращается."""

    def __init__(self, attempts: int, foreign: list[str]) -> None:
        super().__init__(
            f"ответ модели отклонён постпроверкой после {attempts} попыток; "
            f"посторонние числа: {', '.join(foreign[:10]) or '—'}"
        )
        self.attempts = attempts
        self.foreign = foreign


@dataclass(frozen=True, slots=True)
class Conclusion:
    """Принятая текстовая часть заключения."""

    inn: str
    report_date: date
    text: str
    model: str
    attempt: int
    checked_numbers: int


def load_prompt(path: Path | None = None) -> str:
    """Читает шаблон инструкции вместе с общими правилами."""
    prompts = settings.prompts_dir
    template = (path or prompts / "conclusion.md").read_text(encoding="utf-8")
    rules = (prompts / "rules.md").read_text(encoding="utf-8")
    return template.replace("{rules}", rules)


def build_prompt(context: ConclusionContext, path: Path | None = None) -> str:
    """Подставляет блоки контекста в шаблон."""
    return load_prompt(path).replace("{blocks}", context.blocks())


def _log(
    context: ConclusionContext,
    prompt: str,
    completion: Completion | None,
    result: VerificationResult | None,
    attempt: int,
) -> None:
    """Пишет обращение к модели в журнал независимо от исхода.

    Журнал ведётся на собственном соединении, а не в транзакции вызывающего:
    отклонённый ответ поднимает исключение, транзакция вызывающего
    откатывается, и запись об отклонении пропала бы вместе с ней. А она нужна
    именно тогда, когда что-то пошло не так.
    """
    execute(
        _INSERT_LOG,
        {
            "inn": context.inn,
            "report_date": context.report_date,
            "model": completion.model if completion else settings.llm_model,
            "prompt_name": PROMPT_NAME,
            "prompt_text": prompt,
            "response_text": completion.text if completion else None,
            "temperature": 0,
            "verified": result.verified if result else None,
            "foreign_numbers": json.dumps(
                [
                    {"number": item.text, "context": item.context}
                    for item in (result.foreign if result else [])
                ],
                ensure_ascii=False,
            )
            if result
            else None,
            "attempt": attempt,
            "duration_ms": completion.duration_ms if completion else None,
        },
    )


def generate_conclusion(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
    standard: Standard = Standard.RSBU,
    client: LLMClient | None = None,
    context: ConclusionContext | None = None,
) -> Conclusion:
    """Готовит текстовую часть заключения с постпроверкой и повторной попыткой."""
    context = context or build_context(
        inn, conn, report_date=report_date, standard=standard
    )
    prompt = build_prompt(context)
    blocks = context.blocks()

    own_client = client is None
    client = client or LLMClient()
    last_foreign: list[str] = []
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            completion = client.complete(prompt)
            text = strip_reasoning(completion.text)
            result = verify(completion.text, blocks)
            _log(context, prompt, completion, result, attempt)

            if result.verified:
                logger.info(
                    "заключение принято с попытки %d, сверено чисел %d",
                    attempt,
                    result.checked,
                )
                return Conclusion(
                    inn=context.inn,
                    report_date=context.report_date,
                    text=text,
                    model=completion.model,
                    attempt=attempt,
                    checked_numbers=result.checked,
                )

            last_foreign = result.foreign_values
            logger.warning(
                "попытка %d отклонена: %s", attempt, result.summary()
            )
    finally:
        if own_client:
            client.close()

    raise ConclusionRejectedError(MAX_ATTEMPTS, last_foreign)
