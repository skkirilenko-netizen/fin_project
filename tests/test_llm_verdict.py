"""Тесты проверки утверждений о классе и балле.

Класс — фиксированная арифметика методики (инвариант 2). Числовой сверкой
это не проверить: буква классом не является, а балл при сработавшем
стоп-факторе в блоки не подаётся вовсе.
"""

import pytest

from finlib.llm.verdict import VerdictViolation, parse_verdict
from finlib.llm.verify import verify

WITH_CLASS = """
=== ПОКАЗАТЕЛИ ===
cur_liq  «Коэффициент текущей ликвидности»  31.12.2025: 0,82

=== ОЦЕНКА ===
Класс: C — Состояние с признаками напряжения
Общий балл: 60,93 из 100
Уверенность в оценке: medium

Баллы по группам:
  Ликвидность: 45,00 из 100, вес в оценке 30,00 %, показателей в расчёте 3
"""

STOPPED = """
=== ПОКАЗАТЕЛИ ===
cur_liq  «Коэффициент текущей ликвидности»  31.12.2025: 0,82

=== ОЦЕНКА ===
Класс: E — Критическое финансовое состояние
Сработал стоп-фактор «Отрицательный собственный капитал»: обязательства превышают активы.
"""

NO_CLASS = """
=== ПОКАЗАТЕЛИ ===
cur_liq  «Коэффициент текущей ликвидности»  31.12.2025: 0,82

=== ОЦЕНКА ===
Класс не присвоен. Причина: оценка определяется одной группой показателей
Уверенность в оценке: low
"""


# --- разбор вердикта --------------------------------------------------------


def test_verdict_is_read_from_the_block() -> None:
    """Класс и раскрытие балла берутся из блока ОЦЕНКА."""
    verdict = parse_verdict(WITH_CLASS)
    assert verdict.class_code == "C"
    assert verdict.score_disclosed


def test_withheld_score_is_recognized() -> None:
    """При стоп-факторе балл в блоке не приводится."""
    verdict = parse_verdict(STOPPED)
    assert verdict.class_code == "E"
    assert not verdict.score_disclosed


def test_absent_class_is_recognized() -> None:
    """Отсутствие класса читается как отсутствие, а не как ошибка разбора."""
    verdict = parse_verdict(NO_CLASS)
    assert verdict.class_code is None
    assert not verdict.score_disclosed


# --- класс ------------------------------------------------------------------


def test_correct_class_passes() -> None:
    """Названный верно класс проверку проходит."""
    assert verify("Организации присвоен класс C.", WITH_CLASS).verified


def test_wrong_class_is_rejected() -> None:
    """Класс, отличный от присвоенного, — пересмотр оценки."""
    result = verify("Организации присвоен класс B.", WITH_CLASS)
    assert not result.verified
    assert result.verdicts[0].violation is VerdictViolation.WRONG_CLASS
    assert result.verdicts[0].expected == "C"
    assert "присвоен класс C" in result.verdicts[0].describe()


@pytest.mark.parametrize("phrase", ["класс B", "класса B", "классу B", "класс «B»"])
def test_class_mention_forms_are_caught(phrase: str) -> None:
    """Склонение и кавычки опознанию не мешают."""
    assert not verify(f"Речь идёт про {phrase}.", WITH_CLASS).verified


def test_any_class_is_rejected_when_none_assigned() -> None:
    """Если класс не присвоен, назвать любой — выдумать оценку."""
    result = verify("По совокупности признаков это класс D.", NO_CLASS)
    assert not result.verified
    assert result.verdicts[0].violation is VerdictViolation.CLASS_NOT_ASSIGNED
    assert "класс не присвоен" in result.verdicts[0].describe()


def test_saying_class_is_not_assigned_passes() -> None:
    """Сказать, что класс не присвоен, можно: буквы там нет."""
    answer = "Класс финансового состояния не присвоен: основание слишком узкое."
    assert verify(answer, NO_CLASS).verified


# --- балл -------------------------------------------------------------------


def test_total_score_is_rejected_when_withheld() -> None:
    """При стоп-факторе итоговый балл в текст не выносится."""
    result = verify("Общий балл организации высок.", STOPPED)
    assert not result.verified
    assert result.verdicts[0].violation is VerdictViolation.SCORE_WITHHELD


@pytest.mark.parametrize(
    "phrase",
    ["общий балл", "итоговый балл", "интегральный балл", "суммарный балл"],
)
def test_total_score_synonyms_are_caught(phrase: str) -> None:
    """Итоговый балл узнаётся не только по слову «общий»."""
    assert not verify(f"Отмечается высокий {phrase}.", STOPPED).verified


def test_group_score_is_allowed_when_total_is_withheld() -> None:
    """Балл группы под запрет не подпадает: он в блоках есть."""
    answer = "Балл по группе «Ликвидность» — 45,00 из 100."
    result = verify(answer, WITH_CLASS)
    assert result.verified, result.problems


def test_total_score_is_allowed_when_disclosed() -> None:
    """Когда балл раскрыт, называть его можно."""
    assert verify("Общий балл — 60,93 из 100.", WITH_CLASS).verified


def test_score_and_class_violations_are_reported_together() -> None:
    """Оба нарушения попадают в сводку, а не заслоняют друг друга."""
    result = verify("Класс A, общий балл высокий.", NO_CLASS)
    assert not result.verified
    assert len(result.verdicts) == 2
    assert "расхождений с оценкой" in result.summary()
