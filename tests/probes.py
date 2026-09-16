"""Пробы ответов ГИР БО, сохранённые в репозитории, чтобы тесты не ходили в сеть."""

from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "probe"

FULL_BFO = PROBE_DIR / "girbo_bfo_full_7736050003.json"
SIMPLIFIED_BFO = PROBE_DIR / "girbo_bfo_simplified_2100010824.json"
# Комплекты с реальной корректировкой: у 2024, 2022 и 2021 годов
# actualCorrectionNumber = 1.
CORRECTED_BFO = PROBE_DIR / "girbo_bfo_corrected_2522002003.json"
ORG_CARD = PROBE_DIR / "girbo_org_7736050003.json"
SEARCH_FOUND = PROBE_DIR / "girbo_search_7736050003.json"
SEARCH_CORRECTED = PROBE_DIR / "girbo_search_2522002003.json"
SEARCH_SIMPLIFIED = PROBE_DIR / "girbo_search_2100010824.json"
SEARCH_EMPTY = PROBE_DIR / "girbo_search_empty.json"


def read_probe(path: Path) -> bytes:
    """Читает пробу как сырые байты, ровно в том виде, в каком её отдал источник."""
    return path.read_bytes()
