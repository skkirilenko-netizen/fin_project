"""Тесты флагов: срабатывание, текст оговорки, отсутствие влияния на класс."""

from decimal import Decimal

from finlib.quality.thresholds import load_thresholds
from finlib.scoring.definitions import load_flags
from finlib.scoring.flags import evaluate_flag, evaluate_flags

CONSTANTS = load_thresholds().constants
CATALOG = load_flags()
HOLDING = CATALOG.get("holding_structure")

# Отчётность ПАО «Газпром» за 2025 год, тысячи рублей.
GAZPROM = {
    "1170": Decimal("5453427716"),
    "1600": Decimal("25736328136"),
    "2200": Decimal("127437867"),
    "2310": Decimal("691333849"),
}

# Отчётность ПК «Стройсервис» за 2024 год: финансовых вложений нет.
SMALL = {
    "1170": Decimal("0"),
    "1600": Decimal("418"),
    "2200": Decimal("0"),
    "2310": Decimal("0"),
}


def test_holding_flag_fires_on_gazprom() -> None:
    """Признаки холдинговой структуры распознаются на реальных данных."""
    assert HOLDING is not None
    hit = evaluate_flag(HOLDING, GAZPROM, CONSTANTS)
    assert hit is not None
    assert hit.code == "holding_structure"
    assert hit.lowers_confidence


def test_holding_flag_silent_on_small_company() -> None:
    """У малого предприятия без финансовых вложений флаг не срабатывает."""
    assert HOLDING is not None
    assert evaluate_flag(HOLDING, SMALL, CONSTANTS) is None


def test_holding_text_has_numbers_substituted() -> None:
    """Числа подставляются из расчёта, а не берутся из воздуха."""
    assert HOLDING is not None
    hit = evaluate_flag(HOLDING, GAZPROM, CONSTANTS)
    assert hit is not None
    assert "21,2 %" in hit.message
    assert "в 5,4 раза" in hit.message
    assert "{" not in hit.message, "плейсхолдеры не заменены"


def test_holding_text_matches_agreed_wording() -> None:
    """Согласованные формулировки сохранены дословно."""
    assert HOLDING is not None
    hit = evaluate_flag(HOLDING, GAZPROM, CONSTANTS)
    assert hit is not None
    # «может» распространяется на оба придаточных: «может быть сосредоточена…
    # а отчётность… — отражать преимущественно».
    assert "может быть сосредоточена" in hit.message
    assert "отражать преимущественно" in hit.message
    assert "отражает главным образом" not in hit.message
    assert "может не составляться вовсе" in hit.message
    assert "непубличная" in hit.message


def test_participation_phrase_handles_loss() -> None:
    """При отрицательной прибыли от продаж отношение не считается."""
    assert HOLDING is not None
    values = dict(GAZPROM)
    values["2200"] = Decimal("-500000")
    hit = evaluate_flag(HOLDING, values, CONSTANTS)
    assert hit is not None
    assert "при отрицательной прибыли от продаж" in hit.message
    assert "раза" not in hit.message


def test_flag_not_checked_without_data() -> None:
    """Нераскрытые строки — флаг не проверяется, а не считается несработавшим."""
    assert HOLDING is not None
    values = dict(GAZPROM)
    values["1170"] = None
    assert evaluate_flag(HOLDING, values, CONSTANTS) is None


def test_both_conditions_required() -> None:
    """Условия объединены по «и»: одной доли вложений недостаточно."""
    assert HOLDING is not None
    assert HOLDING.combine == "all"
    values = dict(GAZPROM)
    values["2310"] = Decimal("1000")  # доходы от участия ниже прибыли от продаж
    assert evaluate_flag(HOLDING, values, CONSTANTS) is None


def test_flag_details_carry_calibration() -> None:
    """Происхождение порогов едет вместе с флагом."""
    assert HOLDING is not None
    hit = evaluate_flag(HOLDING, GAZPROM, CONSTANTS)
    assert hit is not None
    assert "одном наблюдении" in hit.details["calibration"]
    assert "задачи 11" in hit.details["calibration"]
    assert len(hit.details["conditions"]) == 2


def test_no_flag_affects_class() -> None:
    """Ни один флаг не меняет класс: инвариант 2."""
    for flag in CATALOG.flags:
        assert not flag.affects_class


def test_evaluate_all_flags() -> None:
    """Общий проход по справочнику возвращает только сработавшие."""
    assert [item.code for item in evaluate_flags(GAZPROM, CONSTANTS)] == ["holding_structure"]
    assert evaluate_flags(SMALL, CONSTANTS) == []
