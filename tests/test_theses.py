"""Тесты сборки предписанных тезисов и переключателя схемы (задача 18).

Тезис — то же, что и ответ модели, с точки зрения постпроверки: он приводится
дословно, и если он сам нарушает правила, отклонён будет верный ответ.
Поэтому справочник проверяется теми же средствами, что и текст модели.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.llm.context import ConclusionContext, build_context
from finlib.llm.pairs import build_index, find_anchor
from finlib.llm.service import PromptScheme, build_prompt, load_prompt
from finlib.llm.verify import verify
from finlib.llm.wording import find_forbidden
from finlib.metrics.definitions import load_metrics
from finlib.scoring.theses import (
    Bands,
    ThesisKind,
    build_theses,
    load_theses,
)

# Упрощённая отчётность, отрицательный капитал, четыре надзорных сигнала:
# организация, на которой видно и связь тезисов с сигналами, и отказы расчёта.
SIMPLE = "2100010824"
# Полные формы, все группы показателей, один сигнал по журналу качества.
FULL = "7736050003"


# --- справочник --------------------------------------------------------------


def test_catalog_loads() -> None:
    """Справочник тезисов читается и не пуст."""
    catalog = load_theses()
    assert catalog.version
    assert catalog.metrics
    assert catalog.common.status


def test_every_gender_matches_printed_name() -> None:
    """Род объявлен у каждого показателя, который печатается в тезисах.

    Род берётся по печатаемому наименованию — короткому, если оно задано.
    Показатель без объявленного рода общих тезисов динамики не получает,
    и это молчание надо видеть здесь, а не в готовом заключении.
    """
    catalog = load_theses()
    for metric in load_metrics().metrics:
        assert metric.code in catalog.genders, metric.code
        assert catalog.genders[metric.code] in {"m", "f", "n"}


def test_verb_forms_cover_all_genders() -> None:
    """У каждой формы глагола есть все три рода."""
    catalog = load_theses()
    for name, forms in catalog.verbs.items():
        assert set(forms) == {"m", "f", "n"}, name


def test_bands_order_is_checked() -> None:
    """Границы частей шкалы в обратном порядке справочник не примет."""
    with pytest.raises(ValueError, match="обратном порядке"):
        Bands(lower_below=Decimal(70), upper_from=Decimal(30), origin="проверка")


def test_band_boundaries_split_the_scale() -> None:
    """Балл уровня раскладывается на три части без разрывов."""
    bands = load_theses().bands
    assert bands.of(Decimal(0)) == "lower"
    assert bands.of(bands.lower_below) == "middle"
    assert bands.of(bands.upper_from) == "upper"
    assert bands.of(Decimal(100)) == "upper"


def test_thesis_texts_pass_the_wording_check() -> None:
    """Тексты тезисов не отсылают к нормативу.

    Модель обязана привести тезис дословно. Если тезис сам нарушает словарь
    запрещённых формулировок, ответ будет отклонён за наше нарушение,
    а не за её.
    """
    catalog = load_theses()
    for rule in catalog._all_rules():  # noqa: SLF001 — проверяется весь справочник
        assert not find_forbidden(rule.text), rule.code


def test_thesis_texts_have_no_bare_comparisons() -> None:
    """В тезисах нет сравнений с числом: отношение величин выражено словами."""
    catalog = load_theses()
    forbidden = ("ниже 1", "меньше 100", "выше 1,0", "не менее", "при норме")
    for rule in catalog._all_rules():  # noqa: SLF001
        lowered = rule.text.lower()
        for item in forbidden:
            assert item not in lowered, f"{rule.code}: {item}"


# --- сборка ------------------------------------------------------------------


def test_theses_are_built_for_simplified_reporting() -> None:
    """По упрощённой отчётности тезисы собираются и называют отказы расчёта."""
    found = build_theses(SIMPLE)
    assert found.theses
    kinds = {item.kind for item in found.theses}
    assert ThesisKind.STATUS in kinds
    assert ThesisKind.POSITION in kinds


def test_one_thesis_per_family_and_metric() -> None:
    """Из одного семейства показатель получает не больше одного тезиса.

    Иначе показатель описывался бы дважды разными словами, и читатель искал бы
    разницу там, где её нет.
    """
    for inn in (SIMPLE, FULL):
        seen: set[tuple[str, str]] = set()
        for item in build_theses(inn).theses:
            key = (item.subject, item.kind.value)
            assert key not in seen, key
            seen.add(key)


def test_negative_equity_cancels_roe_interpretation() -> None:
    """При отрицательном капитале рентабельность капитала не истолковывается.

    Прибыль, делённая на среднюю величину, сменившую знак внутри периода,
    даёт большое положительное число, и «капитал принёс прибыль» рядом
    с отрицательным капиталом — ложное утверждение.
    """
    texts = {item.code: item.text for item in build_theses(SIMPLE).theses}
    assert "roe_negative_equity" in texts
    assert "roe_above" not in texts
    assert "не истолковывается" in texts["roe_negative_equity"]


def test_days_metrics_speak_of_acceleration() -> None:
    """У показателей в днях сокращение периода оборота — ускорение."""
    texts = {item.code: item.text for item in build_theses(FULL).theses}
    assert "days_shorter" in texts or "days_longer" in texts
    if "days_shorter" in texts:
        assert "ускорился" in texts["days_shorter"]


def test_thesis_keeps_the_non_breaking_space() -> None:
    """Свёртка формулировки не съедает неразрывный пробел в разрядах числа.

    На вид он от обычного не отличается, а величина в тезисе обязана совпадать
    с величиной в приложении посимвольно: расхождение читатель видит как
    расхождение расчёта.
    """
    from finlib.metrics.display import DIGIT_SPACE

    texts = [item.text for item in build_theses(FULL).theses]
    assert any(DIGIT_SPACE in item for item in texts)


def test_no_latin_beyond_codes() -> None:
    """Латиница в тезисе допустима только как код показателя в скобках."""
    import re

    for inn in (SIMPLE, FULL):
        for item in build_theses(inn).theses:
            stripped = re.sub(r"\([a-z0-9_]+\)", "", item.text)
            assert not re.search(r"[A-Za-z]", stripped), item.code


# --- связь с сигналами -------------------------------------------------------


def test_signals_come_with_value_and_threshold() -> None:
    """У сигнала в перечне стоят и величина, и отсечка.

    Тезис без них проверить нечем, а прежде модель получала одно наименование
    и истолковать сигнал не могла.
    """
    found = build_theses(SIMPLE)
    assert found.signals
    for signal in found.signals:
        assert "величина" in signal.headline
        assert "отсечка" in signal.headline


def test_signal_is_linked_to_metric_theses_by_calculation() -> None:
    """Связь сигнала с тезисами считается, а не оставляется модели.

    Сигнал об изъятии капитала посчитан по строкам 1300 и 2400; показатели,
    построенные на тех же строках и рассчитанные у этой организации, названы
    при нём прямо.
    """
    found = build_theses(SIMPLE)
    withdrawal = next(
        item for item in found.signals if item.code == "equity_withdrawal"
    )
    assert "1300" in withdrawal.lines
    assert "equity" in withdrawal.related
    subjects = {item.subject for item in found.theses}
    assert set(withdrawal.related) <= subjects


def test_link_names_only_calculated_metrics() -> None:
    """В связь идут только рассчитанные показатели.

    Строка 1300 входит в формулы половины справочника, и без этого отбора
    сигнал указывал бы на показатели, которых у организации нет: истолковать
    сигнал через нерассчитанный показатель нельзя.
    """
    found = build_theses(SIMPLE)
    not_calculable = {
        item.subject for item in found.theses if item.kind is ThesisKind.STATUS
    }
    for signal in found.signals:
        assert not set(signal.related) & not_calculable


def test_signal_without_lines_says_so() -> None:
    """Сигнал, посчитанный по журналу качества, связи с показателями не имеет."""
    found = build_theses(FULL)
    revision = next(
        (item for item in found.signals if item.code == "revision_intensity"), None
    )
    assert revision is not None
    assert revision.lines == ()
    assert revision.related == ()
    assert "тезисов о показателях" in found.block()


# --- блок и постпроверка -----------------------------------------------------


def test_block_numbers_are_anchored() -> None:
    """Каждое число тезиса привязано к коду — тому, которому принадлежит.

    Это и есть суть разворота: пару «число — код» составляет расчёт, а не
    модель. Если тезис не проходит собственную постпроверку, приведённый
    дословно, он отклонит верный ответ.
    """
    for inn in (SIMPLE, FULL):
        context = build_context(inn, with_theses=True)
        blocks = context.blocks()
        answer = "### 3. Аналитическая интерпретация\n" + "\n\n".join(
            item.text for item in build_theses(inn).theses
        )
        result = verify(answer, blocks, thresholds=load_metrics().stop_factor_values())
        assert result.foreign == [], [item.describe() for item in result.foreign]


def test_signal_value_is_anchored_to_its_own_code() -> None:
    """Величина сигнала привязывается к коду сигнала, а не к строке баланса.

    У структурного сдвига код кончается кодом строки — «structure_shift_1300», —
    и без правила о подчёркивании якорем становился бы кусок «1300», значения
    у которого совсем другие.
    """
    context = build_context(SIMPLE, with_theses=True)
    index = build_index(context.blocks())
    text = "Структурный сдвиг (structure_shift_1300) 181,2 п. п."
    span = (text.index("181,2"), text.index("181,2") + len("181,2"))
    anchor = find_anchor(text, span, index)
    assert anchor is not None
    assert anchor.key == "structure_shift_1300"


def test_theses_block_only_in_theses_scheme() -> None:
    """Блок тезисов собирается только для своей схемы.

    Подать готовые утверждения при свободной генерации значило бы мерить
    не ту схему, ради сравнения с которой замер делается.
    """
    assert "=== ТЕЗИСЫ ===" not in build_context(SIMPLE).blocks()
    assert "=== ТЕЗИСЫ ===" in build_context(SIMPLE, with_theses=True).blocks()


# --- переключатель схемы -----------------------------------------------------


def test_scheme_picks_its_template() -> None:
    """Каждая схема читает свой шаблон, и общие правила подставляются в оба."""
    free = load_prompt(scheme=PromptScheme.FREE)
    theses = load_prompt(scheme=PromptScheme.THESES)
    assert free != theses
    assert "Модель не вычисляет" in free
    assert "Модель не вычисляет" in theses
    assert "{rules}" not in theses
    assert "ТЕЗИСЫ" in theses


def test_scheme_value_is_the_journal_name() -> None:
    """Имя схемы совпадает с именем промпта в журнале.

    Замеры двух схем идут в один журнал, и различать их надо по записи,
    а не по времени прогона.
    """
    assert PromptScheme.FREE.value == "conclusion"
    assert PromptScheme.THESES.value == "conclusion_theses"


def test_prompt_carries_the_theses_block() -> None:
    """Блоки контекста попадают в промпт схемы тезисов."""
    context = ConclusionContext(
        inn=SIMPLE,
        report_date=date(2024, 12, 31),
        organization="=== ОРГАНИЗАЦИЯ ===\nИНН: 2100010824",
        data="=== ДАННЫЕ ===\n1600  БАЛАНС  |  418",
        metrics="=== ПОКАЗАТЕЛИ ===\nequity  «Собственный капитал»  -442",
        flags="=== ФЛАГИ ===\nФлагов не сработало.",
        assessment="=== ОЦЕНКА ===\nКласс: E",
        limitations="=== ОГРАНИЧЕНИЯ АНАЛИЗА ===\n- оговорка",
        theses="=== ТЕЗИСЫ ===\n- Собственный капитал (equity) отрицателен: -442.",
    )
    prompt = build_prompt(context, scheme=PromptScheme.THESES)
    assert "=== ТЕЗИСЫ ===" in prompt
    assert "Собственный капитал (equity) отрицателен" in prompt
