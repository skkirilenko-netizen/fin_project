"""Порождение текстовой части заключения: запрос, постпроверка, журнал.

Ответ, не прошедший постпроверку, наверх не возвращается: делается одна
повторная попытка, затем ошибка. Каждое обращение записывается в llm_log
независимо от исхода — журнал ведётся и по отклонённым ответам.
"""

import json
import logging
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
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


class PromptScheme(StrEnum):
    """Схема, по которой порождается текстовая часть.

    `free` — свободная генерация: модель сама формулирует утверждения
    о показателях по переданным величинам. `theses` — сборка из предписанных
    формулировок: утверждения выбирает расчёт, модель связывает их в текст
    (задача 18).

    Значение схемы совпадает с именем шаблона в `prompts/` и с именем промпта
    в `llm_log`: замеры двух схем идут в один журнал, и различать их надо
    по записи, а не по времени прогона.
    """

    FREE = "conclusion"
    THESES = "conclusion_theses"


DEFAULT_SCHEME = PromptScheme.FREE
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


def load_prompt(
    path: Path | None = None, scheme: PromptScheme = DEFAULT_SCHEME
) -> str:
    """Читает шаблон инструкции вместе с общими правилами.

    Общие правила одни на обе схемы и подставляются в шаблон, а не
    дублируются в нём: правило, разошедшееся между схемами, сделало бы
    сравнение схем сравнением двух разных требований.
    """
    prompts = settings.prompts_dir
    template = (path or prompts / f"{scheme.value}.md").read_text(encoding="utf-8")
    rules = (prompts / "rules.md").read_text(encoding="utf-8")
    return template.replace("{rules}", rules)


def build_prompt(
    context: ConclusionContext,
    path: Path | None = None,
    scheme: PromptScheme = DEFAULT_SCHEME,
) -> str:
    """Подставляет блоки контекста в шаблон выбранной схемы."""
    return load_prompt(path, scheme).replace("{blocks}", context.blocks())


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


def _thesis_texts(
    context: ConclusionContext, conn: PgConnection | None, standard: Standard
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Предписанные тезисы и наименования групп, по которым они разложены.

    Возвращаются вместе: обе величины берутся из одного перечня тезисов,
    и собрать их порознь значило бы дважды спросить расчёт об одном и том же.
    """
    from finlib.scoring.theses import build_theses

    found = build_theses(
        context.inn, conn, report_date=context.report_date, standard=standard
    )
    groups = tuple(name for name, _ in found.by_group())
    return tuple(item.text for item in found.theses), groups


def _text_context(
    inn: str, conn: PgConnection | None, report_date: date, standard: Standard
) -> TextContext:
    """Контекст контроля утверждений текста по данным расчёта."""
    from finlib.normalize.lines import ReportingType, load_lines
    from finlib.report.data import load_report_data

    data = load_report_data(inn, conn, report_date=report_date, standard=standard)
    reporting_type = ReportingType(data.organization["reporting_type"])
    return data.text_context(load_lines(), reporting_type, load_metrics())


def _log(
    context: ConclusionContext,
    prompt: str,
    completion: Completion | None,
    result: VerificationResult | None,
    attempt: int,
    is_test: bool,
    scheme: PromptScheme = DEFAULT_SCHEME,
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
            "prompt_name": scheme.value,
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
    scheme: PromptScheme = DEFAULT_SCHEME,
) -> Conclusion:
    """Готовит текстовую часть заключения с постпроверкой и повторной попыткой.

    is_test помечает записи журнала как тестовые. Журнал обращений к модели —
    доказательная база системы: он показывает, что именно было предъявлено
    модели и что она ответила. Стирать его прогоном тестов нельзя, поэтому
    тесты помечают свои записи и убирают только помеченные.

    scheme выбирает схему текстовой части. Блок тезисов собирается только
    для неё же: подавать готовые утверждения при свободной генерации значило
    бы мерить не ту схему, ради сравнения с которой замер делается.
    """
    context = context or build_context(
        inn,
        conn,
        report_date=report_date,
        standard=standard,
        with_theses=scheme is PromptScheme.THESES,
    )
    # Контроль утверждений текста без контекста не выполняется вовсе
    # (`verify` пропускает его при text_context=None), а цикл обработки его
    # не передавал: шесть блокирующих правил месяцами не работали в боевом
    # прогоне, и нули в замерах означали не чистый текст, а невыполненную
    # проверку. Контекст строится здесь, если вызывающий его не дал.
    text_context = text_context or _text_context(
        context.inn, conn, context.report_date, standard
    )
    if scheme is PromptScheme.THESES:
        # Состав утверждений проверяется только там, где он предписан:
        # при свободной генерации сверять текст не с чем. Раскладка по группам
        # приходит оттуда же: группы объявлены тем же перечнем тезисов.
        theses, groups = _thesis_texts(context, conn, standard)
        text_context = replace(
            text_context, theses=theses, thesis_groups=groups
        )
    prompt = build_prompt(context, scheme=scheme)
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
            _log(context, asked, completion, result, attempt, is_test, scheme)

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
