"""Тесты сопоставления кодов отчётности со справочником строк."""

import pytest

from finlib.normalize.lines import ReportingType, load_lines
from finlib.normalize.mapper import MappingOutcome, map_codes

FULL = ReportingType.FULL
SIMPLIFIED = ReportingType.SIMPLIFIED


@pytest.fixture(scope="module")
def catalog():
    """Справочник строк."""
    return load_lines()


def test_full_codes_map_to_themselves(catalog) -> None:
    """В полных формах код строки и есть ключ справочника."""
    result = map_codes({"1110", "1600", "2110"}, set(), catalog, FULL, "0710001")
    assert result.mapped["1110"].line_code == "1110"
    assert result.mapped["1600"].line_code == "1600"
    # 2110 относится к другой форме и в балансе неизвестен.
    assert [item.source_code for item in result.unknown] == ["2110"]


def test_unknown_code_is_reported_not_dropped(catalog) -> None:
    """Код вне справочника попадает в перечень неизвестных."""
    result = map_codes({"1105", "13101", "1110"}, set(), catalog, FULL, "0710001")
    assert {item.source_code for item in result.unknown} == {"1105", "13101"}
    assert all(item.outcome is MappingOutcome.UNKNOWN for item in result.unknown)
    assert "1110" in result.mapped


def test_simplified_code_maps_to_canonical(catalog) -> None:
    """Укрупнённая строка получает канонический код, исходный сохраняется отдельно."""
    result = map_codes({"1230"}, {"1230"}, catalog, SIMPLIFIED, "0710001")
    mapped = result.mapped["1230"]
    assert mapped.line_code == "1240"
    assert mapped.source_code == "1230"


def test_ambiguous_code_is_not_guessed(catalog) -> None:
    """Код 1190 допускают две укрупнённые строки: молча выбирать нельзя."""
    result = map_codes({"1190"}, {"1190"}, catalog, SIMPLIFIED, "0710001")
    assert not result.mapped
    assert len(result.ambiguous) == 1
    item = result.ambiguous[0]
    assert item.source_code == "1190"
    assert set(item.candidates) == {"1150", "1170"}
    assert item.outcome is MappingOutcome.AMBIGUOUS


def test_ambiguity_resolved_by_occupancy(catalog) -> None:
    """Если 1150 занят собственным кодом, для 1190 остаётся одно место."""
    result = map_codes({"1150", "1190"}, {"1150", "1190"}, catalog, SIMPLIFIED, "0710001")
    assert result.mapped["1150"].line_code == "1150"
    assert result.mapped["1190"].line_code == "1170"
    assert not result.ambiguous


def test_occupancy_needs_disclosed_value(catalog) -> None:
    """Нераскрытый 1150 место не занимает: 1190 остаётся неоднозначным."""
    result = map_codes({"1150", "1190"}, {"1190"}, catalog, SIMPLIFIED, "0710001")
    assert "1190" not in result.mapped
    assert [item.source_code for item in result.ambiguous] == ["1190"]


def test_ambiguous_line_is_not_written(catalog) -> None:
    """Неоднозначная строка не попадает ни в одну из строк-претендентов."""
    result = map_codes({"1190"}, {"1190"}, catalog, SIMPLIFIED, "0710001")
    assert "1150" not in {item.line_code for item in result.mapped.values()}
    assert "1170" not in {item.line_code for item in result.mapped.values()}


def test_simplified_profit_codes(catalog) -> None:
    """Коды упрощённого ОФР сопоставляются со своим набором, а не с полным."""
    result = map_codes({"2110", "2120", "2400"}, set(), catalog, SIMPLIFIED, "0710002")
    assert result.mapped["2120"].line_code == "2120"
    assert result.mapped["2120"].line.name == "Расходы по обычной деятельности"
    assert result.mapped["2120"].line.same_meaning_as_full is False


def test_mapped_line_code_requires_mapping(catalog) -> None:
    """Обращение к коду несопоставленной строки — ошибка, а не тихая подстановка."""
    result = map_codes({"1190"}, {"1190"}, catalog, SIMPLIFIED, "0710001")
    with pytest.raises(ValueError, match="не сопоставлен"):
        _ = result.ambiguous[0].line_code
