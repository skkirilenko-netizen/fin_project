"""Тесты состава регрессионного набора МСФО (задача 29).

Прогон здесь не выполняется: он требует документов, которые выгружаются
руками. Проверяется то, что делает набор до прогона, — состав, правила
измерения и объём ручной работы.

**Цена участия эмитента в наборе МСФО — вечер работы человека**, и это
отличает набор от РСБУ, где отчётность берётся из ГИР БО по ИНН. Поэтому
проверяется и то, что объём выгрузки виден из файла: признак, объявленный
проверяемым без документа, обязан быть таким, а число документов считается
по каталогу, а не по составу.
"""

import copy
import importlib.util

import pytest
import yaml
from pydantic import ValidationError

from finlib.config import settings


def module():
    """Загружает eval/ifrs_set.py: инструмент разработки, не часть пакета."""
    path = settings.base_dir / "eval" / "ifrs_set.py"
    spec = importlib.util.spec_from_file_location("ifrs_set", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


ifrs_set = module()


def rules_data() -> dict:
    """Правила измерения как словарь — для порчи в тестах."""
    return yaml.safe_load(ifrs_set.RULES_PATH.read_text(encoding="utf-8"))


# --- состав -------------------------------------------------------------------


def test_set_loads_and_every_inn_checks_out() -> None:
    """Состав читается, и каждый ИНН сходится по контрольным разрядам.

    Опечатка в ИНН даёт не ошибку прогона, а тихий пропуск: документ
    выгрузят не тому эмитенту, и набор окажется меньше, чем считает.
    """
    found = ifrs_set.load_set()
    assert len(found.entries) >= 15
    assert found.in_run
    assert "эмитентов в составе" in found.describe()


def test_mistyped_inn_is_refused() -> None:
    """ИНН с опечаткой состав не принимает."""
    with pytest.raises(ValidationError):
        ifrs_set.Entry(
            cat_id=1,
            category="Обычный корпоративный эмитент",
            name="ООО «Опечатка»",
            inn="5321029507",
            reason="проверка контрольных разрядов",
            category_status="гипотеза",
        )


def test_inn_is_not_repeated() -> None:
    """Один эмитент — одна строка состава: повтор удвоил бы его вес."""
    entries = list(ifrs_set.load_entries())
    with pytest.raises(ValidationError, match="повторяются"):
        ifrs_set.IfrsSet(
            entries=(*entries, entries[0]), rules=ifrs_set.load_rules()
        )


def test_category_unknown_to_the_rules_is_refused() -> None:
    """Категория состава объявлена в правилах измерения, а не только в файле."""
    entry = ifrs_set.Entry(
        cat_id=99,
        category="Категория из ниоткуда",
        name="ООО «Пример»",
        inn="5321029508",
        reason="проверка",
        category_status="гипотеза",
    )
    with pytest.raises(ValidationError, match="категории 99"):
        ifrs_set.IfrsSet(entries=(entry,), rules=ifrs_set.load_rules())


def test_renamed_category_breaks_the_set() -> None:
    """Переименованная категория обязана сломать набор, а не разойтись молча."""
    entry = ifrs_set.Entry(
        cat_id=1,
        category="Обычный корпоративный эмитeнт",
        name="ООО «Пример»",
        inn="5321029508",
        reason="проверка",
        category_status="гипотеза",
    )
    with pytest.raises(ValidationError, match="называется"):
        ifrs_set.IfrsSet(entries=(entry,), rules=ifrs_set.load_rules())


# --- вне периметра ------------------------------------------------------------


def test_out_of_scope_issuers_expect_a_refusal() -> None:
    """Ожидаемый исход у эмитента вне периметра — отказ, и это успех.

    Правило то же, что с кредитными организациями в наборе РСБУ: методика
    их не покрывает, отказ подтверждает правило, а не нарушает его.
    """
    found = ifrs_set.load_set()
    out_of_scope = [
        item for item in found.entries if item.category.startswith("Вне периметра")
    ]
    assert out_of_scope, "в наборе нет ни одного эмитента вне периметра"
    assert all(
        item.expected_outcome is ifrs_set.Outcome.REFUSAL for item in out_of_scope
    )


def test_outcomes_in_the_rules_match_the_code() -> None:
    """Перечень исходов в правилах и в коде один: разойдясь, они смолчат."""
    data = rules_data()
    data["outcomes"].pop("refusal")
    with pytest.raises(ValidationError, match="исходов"):
        ifrs_set.Rules.model_validate(data)


# --- объём ручной работы ------------------------------------------------------


def test_manual_work_is_visible_from_the_set() -> None:
    """Сколько документов выгружать, видно из набора, а не выясняется в прогоне."""
    found = ifrs_set.load_set()
    needed = found.documents_needed
    on_hand = found.documents_on_hand()
    to_fetch = found.documents_to_fetch()
    assert len(needed) == len(on_hand) + len(to_fetch)
    assert {item.inn for item in on_hand} & {item.inn for item in to_fetch} == set()


def test_documents_on_hand_are_counted_by_the_folder(tmp_path) -> None:
    """Наличие документа считается по каталогу, а не объявляется в составе.

    Объявленное наличие устаревает в тот день, когда файл переложили,
    и набор врал бы о себе сам.
    """
    found = ifrs_set.load_set()
    assert found.documents_on_hand(tmp_path) == ()
    first = found.documents_needed[0]
    (tmp_path / first.inn).mkdir()
    (tmp_path / first.inn / "отчётность.pdf").write_bytes(b"%PDF-1.4")
    assert [item.inn for item in found.documents_on_hand(tmp_path)] == [first.inn]


def test_feature_without_a_document_is_declared_as_such() -> None:
    """Признаки, проверяемые без выгрузки, объявлены и их меньшинство.

    Перечень не декоративный: он отвечает на вопрос, что достаётся даром.
    По API отбираются валюта, капитал и результат — аудиторское мнение,
    примечания и конвенция разрядов только из документа.
    """
    found = ifrs_set.load_set()
    without = found.features_by_source(ifrs_set.DataSource.CBONDS)
    assert "negative_equity" in without
    assert "modified_opinion" not in without
    assert len(without) < len(found.rules.features)


def test_feature_source_both_is_refused() -> None:
    """У признака источник один: «оба» означало бы, что он не назван."""
    data = rules_data()
    data["features"]["negative_equity"]["source"] = "both"
    with pytest.raises(ValidationError, match="both"):
        ifrs_set.Rules.model_validate(data)


# --- правила измерения --------------------------------------------------------


def test_category_feature_must_exist() -> None:
    """Категория опирается на признак, объявленный в справочнике признаков.

    Переименованный признак обязан сломать набор, а не молча превратить
    измерение в непокрытое.
    """
    data = rules_data()
    data["categories"][0]["feature"] = "признак_которого_нет"
    with pytest.raises(ValidationError, match="признак"):
        ifrs_set.Rules.model_validate(data)


def test_category_without_a_feature_declares_why() -> None:
    """Категория без машинного признака объявляет причину, а не молчит."""
    data = rules_data()
    broken = copy.deepcopy(data)
    broken["categories"][0].pop("feature")
    with pytest.raises(ValidationError, match="feature"):
        ifrs_set.Rules.model_validate(broken)

    both = copy.deepcopy(data)
    both["categories"][0]["manual"] = "и признак, и причина сразу"
    with pytest.raises(ValidationError, match="feature"):
        ifrs_set.Rules.model_validate(both)


def test_every_metric_carries_a_denominator() -> None:
    """У метрики объявлен знаменатель: доля без него — не измерение."""
    found = ifrs_set.load_rules()
    assert found.metrics
    assert all(item.denominator for item in found.metrics.values())


def test_uncovered_features_name_the_reason() -> None:
    """Непокрытый признак назван вместе с причиной, а не просто отсутствует.

    Отрицательное мнение аудитора добором документов не достаётся: эмитент
    с таким заключением отчётность не публикует. Молчание об этом читалось
    бы как «признак покрыт».
    """
    found = ifrs_set.load_rules()
    assert found.uncovered
    assert all(item.reason.strip() for item in found.uncovered)
