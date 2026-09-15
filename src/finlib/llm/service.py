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
from finlib.llm.cleanup import strip_identifiers
from finlib.llm.client import Completion, LLMClient
from finlib.llm.context import ConclusionContext, build_context
from finlib.llm.textcheck import TextContext
from finlib.llm.verify import VerificationResult, strip_reasoning, verify
from finlib.metrics.definitions import load_metrics
from finlib.standards import Standard
from finlib.version import code_version

logger = logging.getLogger(__name__)

PROMPT_NAME = "conclusion"
# Попытки имеют смысл, пока модели сообщают, что было не так (with_corrections):
# при нулевой температуре повтор с тем же промптом даёт тот же ответ.
# Три попытки — по наблюдениям на пробах: формат «число со своим кодом»
# модель выдерживает не с первого раза, но замечания отрабатывает.
MAX_ATTEMPTS = 3

_INSERT_LOG = """
INSERT INTO llm_log (
    inn, report_date, model, prompt_name, prompt_text, response_text,
    temperature, verified, foreign_numbers, attempt, duration_ms, code_version,
    is_test
) VALUES (
    %(inn)s, %(report_date)s, %(model)s, %(prompt_name)s, %(prompt_text)s, %(response_text)s,
    %(temperature)s, %(verified)s, %(foreign_numbers)s, %(attempt)s, %(duration_ms)s,
    %(code_version)s, %(is_test)s
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
    # Текст в том виде, в каком его подтвердила постпроверка, до снятия
    # разметки. Нужен сквозной сверке: всё, что происходит после проверки,
    # иначе остаётся вне контроля.
    verified_text: str = ""


def load_prompt(path: Path | None = None) -> str:
    """Читает шаблон инструкции вместе с общими правилами."""
    prompts = settings.prompts_dir
    template = (path or prompts / "conclusion.md").read_text(encoding="utf-8")
    rules = (prompts / "rules.md").read_text(encoding="utf-8")
    return template.replace("{rules}", rules)


def build_prompt(context: ConclusionContext, path: Path | None = None) -> str:
    """Подставляет блоки контекста в шаблон."""
    return load_prompt(path).replace("{blocks}", context.blocks())


def with_corrections(prompt: str, problems: list[str]) -> str:
    """Дописывает к инструкции перечень замечаний к прошлому ответу.

    Без этого повторная попытка бессмысленна: температура нулевая, промпт
    тот же — модель выдаёт тот же текст и отклоняется по той же причине.
    Замечания формулируются как факты о прошлом ответе, а не как подсказка,
    что написать: подсказывать содержание нельзя, это работа расчёта.
    """
    if not problems:
        return prompt
    listed = "\n".join(f"- {item}" for item in problems)
    return (
        f"{prompt}\n\n"
        "## Предыдущий ответ отклонён\n\n"
        "Автоматическая проверка нашла в нём следующее:\n\n"
        f"{listed}\n\n"
        "Перепиши заключение целиком, устранив перечисленное. Числа бери "
        "из блоков без изменений, новых не вводи."
    )


def _log(
    context: ConclusionContext,
    prompt: str,
    completion: Completion | None,
    result: VerificationResult | None,
    attempt: int,
    is_test: bool,
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
            # В журнал уходят все виды замечаний, а не только числа: отсылка
            # к нормативу и ложное утверждение о нерасчёте отклоняют ответ
            # наравне с посторонним числом, и разбирать отказ надо по ним же.
            "foreign_numbers": json.dumps(
                {
                    "numbers": [
                        {
                            "number": item.text,
                            "violation": item.violation.value,
                            "anchor": item.anchor,
                            "context": item.context,
                        }
                        for item in result.foreign
                    ],
                    "wordings": [
                        {"text": item.text, "label": item.label, "context": item.context}
                        for item in result.wordings
                    ],
                    "claims": [
                        {"code": item.code, "text": item.text, "context": item.context}
                        for item in result.claims
                    ],
                    "verdicts": [
                        {
                            "text": item.text,
                            "violation": item.violation.value,
                            "context": item.context,
                        }
                        for item in result.verdicts
                    ],
                    "statements": [
                        {
                            "rule": item.rule.value,
                            "severity": item.severity.value,
                            "message": item.message,
                            "context": item.context,
                        }
                        for item in result.statements
                    ],
                },
                ensure_ascii=False,
            )
            if result
            else None,
            "attempt": attempt,
            "duration_ms": completion.duration_ms if completion else None,
            # Версия кода прогона: записи разных версий несопоставимы, и разбор
            # журнала считает только текущую.
            "code_version": code_version(),
            "is_test": is_test,
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
    text_context: TextContext | None = None,
    is_test: bool = False,
) -> Conclusion:
    """Готовит текстовую часть заключения с постпроверкой и повторной попыткой.

    is_test помечает записи журнала как тестовые. Журнал обращений к модели —
    доказательная база системы: он показывает, что именно было предъявлено
    модели и что она ответила. Стирать его прогоном тестов нельзя, поэтому
    тесты помечают свои записи и убирают только помеченные.
    """
    context = context or build_context(
        inn, conn, report_date=report_date, standard=standard
    )
    prompt = build_prompt(context)
    blocks = context.blocks()
    # Пороги стоп-факторов — единственные числа-ориентиры, которые методика
    # объявляет прямо; называть их модели разрешено. Всякий другой порог
    # («ниже 1,0» для текущей ликвидности) она выдумала.
    thresholds = load_metrics().stop_factor_values()

    own_client = client is None
    client = client or LLMClient()
    last_foreign: list[str] = []
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            asked = with_corrections(prompt, last_foreign)
            completion = client.complete(asked)
            text = strip_reasoning(completion.text)
            result = verify(
                completion.text,
                blocks,
                thresholds=thresholds,
                text_context=text_context,
            )
            _log(context, asked, completion, result, attempt, is_test)

            if result.verified:
                logger.info(
                    "заключение принято с попытки %d, сверено чисел %d",
                    attempt,
                    result.checked,
                )
                return Conclusion(
                    inn=context.inn,
                    report_date=context.report_date,
                    # Коды снимаются после проверки: они механизм сверки,
                    # а не часть заключения. Читатель их видеть не должен.
                    text=strip_identifiers(text),
                    verified_text=text,
                    model=completion.model,
                    attempt=attempt,
                    checked_numbers=result.checked,
                )

            last_foreign = result.problems
            logger.warning(
                "попытка %d отклонена: %s", attempt, result.summary()
            )
    finally:
        if own_client:
            client.close()

    raise ConclusionRejectedError(MAX_ATTEMPTS, last_foreign)
