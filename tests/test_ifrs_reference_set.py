"""Состав эталонных величин: объявлено всё, без чего он бесполезен.

Прогон эталонов появился после регресса 21.09.2026: сноска ЛСР об эскроу
не извлекалась вовсе, а тест был зелёным — он приписывал сноску после
таблицы, тогда как в документе она стоит выше неё. Синтетический тест
отвечает «механизм работает», и это не то же самое, что «документ
разбирается».

Сам прогон идёт по настоящим документам из `data/`, которых в репозитории
нет, — поэтому здесь проверяется то, что от них не зависит: состав объявлен
полно, виды проверок известны прогону, и у каждой величины названо основание.
"""

import importlib.util

import pytest
import yaml

from finlib.config import settings


def module(name: str):
    """Загружает eval/<name>.py: инструмент разработки, не часть пакета."""
    path = settings.base_dir / "eval" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


runner = module("ifrs_reference_run")
REFERENCE = settings.base_dir / "eval" / "ifrs_reference.yaml"
KNOWN_DOCUMENT = {"note_contains", "value", "note_value", "grouping", "reporting_kind"}
KNOWN_DATABASE = {"issuer_type", "metric_refused", "stop_factors"}


@pytest.fixture(scope="module")
def catalog() -> dict:
    """Состав эталонных величин."""
    return yaml.safe_load(REFERENCE.read_text(encoding="utf-8"))


def test_every_reference_value_names_its_basis(catalog) -> None:
    """У каждой величины объявлено, чей документ, где найдена и почему важна.

    Без основания через полгода нельзя отличить существенную величину
    от случайно попавшей, а прогон, остановившийся на непонятной проверке,
    заставляет править эталон вместо разбора.
    """
    assert catalog["checks"], "состав пуст — проверять нечего"
    for check in catalog["checks"]:
        where = f"{check.get('inn')}/{check.get('kind')}"
        assert check.get("inn"), where
        assert check.get("why", "").strip(), where
        assert check.get("found_at"), where
        assert check["source"] in ("document", "database"), where
        if check["source"] == "document":
            assert check.get("document"), where
        else:
            assert check.get("report_date"), where


def test_every_kind_is_known_to_the_run(catalog) -> None:
    """Вид проверки, которого прогон не знает, молча не проходит.

    Прогон на неизвестный вид отвечает расхождением, а не пропуском, —
    но узнать об этом лучше здесь, чем на прогоне после правки разбора.
    """
    for check in catalog["checks"]:
        known = KNOWN_DOCUMENT if check["source"] == "document" else KNOWN_DATABASE
        assert check["kind"] in known, f"{check['inn']}: {check['kind']}"


def test_unknown_kind_is_a_divergence_not_a_pass() -> None:
    """Неизвестный вид проверки даёт расхождение, а не тихий успех."""
    strange = {
        "inn": "0000000000",
        "source": "document",
        "kind": "какой-то новый вид",
        "document": "нет.pdf",
        "expect": "1",
        "why": "проверка правила",
    }
    assert not runner._from_document(strange).ok
    assert not runner._from_database(
        {**strange, "source": "database", "report_date": "2025-12-31"}
    ).ok


def test_unmeasurable_values_are_declared_with_a_reason(catalog) -> None:
    """Непроверяемое объявлено вместе с причиной.

    Молчание о непроверенном читалось бы как «проверено» — то же правило,
    по которому непокрытый признак регрессионного набора объявляет, почему
    он непокрыт.
    """
    for item in catalog.get("not_checked") or ():
        assert item.get("subject"), item
        assert item.get("reason", "").strip(), item
