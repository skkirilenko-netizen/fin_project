"""Тесты прогонщика регрессионного набора МСФО (задача 29).

Документы здесь не разбираются: разбор проверен своими тестами. Проверяется
то, что делает прогонщик вокруг разбора, — знаменатели, ожидаемые исходы
и отличие «измерено нулём» от «не измерялось».
"""

import importlib.util

from finlib.config import settings


def module(name: str):
    """Загружает модуль из eval: инструмент разработки, не часть пакета."""
    path = settings.base_dir / "eval" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


# Модуль состава берётся у самого прогонщика, а не загружается вторым
# экземпляром: второй даёт свои перечисления, и сравнение исходов по
# тождеству молча перестаёт работать.
runner = module("ifrs_regression_run")


def entry(inn: str = "5321029508", outcome=None):
    """Строка состава для проверки."""
    return runner.Entry(
        cat_id=1,
        category="Обычный корпоративный эмитент",
        name="ООО «Пример»",
        inn=inn,
        reason="проверка",
        category_status="гипотеза",
        expected_outcome=outcome or runner.Outcome.ANALYSIS,
    )


def test_issuer_without_a_document_is_not_a_zero(tmp_path) -> None:
    """Эмитент без документа даёт пропуск, а не нулевое прохождение.

    Ноль автоматических прохождений у того, чью отчётность не выгружали,
    означал бы, что извлечение не прошло, — тогда как его не было.
    """
    found = runner.run_issuer(entry(), tmp_path, {})
    assert found.outcome == runner.NO_DOCUMENT
    assert not found.has_document
    assert not found.matched


def test_denominators_count_only_measured_issuers(tmp_path) -> None:
    """В знаменатели идут только эмитенты, у которых документ был."""
    report = runner.Report(found=runner.load_set())
    report.runs = [runner.IssuerRun(entry=entry())]
    text = report.render()
    assert "эмитентов без документа: 1" in text
    assert "отсутствие измерения" in text


def test_expected_refusal_counts_as_a_match() -> None:
    """Ожидаемый отказ — успех прогона: эмитент вне периметра подтверждает правило."""
    found = runner.IssuerRun(
        entry=entry(outcome=runner.Outcome.REFUSAL),
        documents=(object(),),
        outcome=runner.Outcome.REFUSAL.value,
    )
    assert found.matched

    unexpected = runner.IssuerRun(
        entry=entry(),
        documents=(object(),),
        outcome=runner.Outcome.REFUSAL.value,
    )
    assert not unexpected.matched


def test_features_without_a_document_come_from_cbonds() -> None:
    """Признаки, объявленные проверяемыми без выгрузки, так и проверяются.

    Объявление в правилах набора обязано быть правдой: признак, который
    меряется только по документу, не может числиться доступным по API.
    """
    universe = {
        "5321029508": [
            {"date": "2025-12-31", "ln104": "RUB", "ln20": "-100", "ln26": "-5"}
        ]
    }
    found = runner._cbonds_features("5321029508", universe)
    assert found == {"negative_equity", "loss"}
    assert runner._cbonds_features("0000000000", universe) == set()


def test_share_says_when_the_denominator_is_empty() -> None:
    """Доля от пустого знаменателя не печатается нулём процентов."""
    assert "знаменатель пуст" in runner._share(0, 0)
    assert "%" in runner._share(1, 2)


def test_every_declared_feature_can_be_measured() -> None:
    """Признак, объявленный в правилах, прогон обязан уметь померить.

    Иначе он получит ноль навсегда, и ноль этот будет неотличим
    от отсутствия наблюдений — тот же дефект, что контроль, которого никто
    не вызывает.
    """
    declared = set(runner.load_set().rules.features)
    measurable = runner.DOCUMENT_FEATURES | runner.CBONDS_FEATURES
    assert declared - measurable == set(), "объявлены, но не меряются"
    assert measurable - declared == set(), "меряются, но не объявлены"


def test_cbonds_features_match_the_declaration() -> None:
    """Перечень признаков без выгрузки в коде и в правилах один."""
    rules = runner.load_set()
    declared = set(rules.features_by_source(runner.DataSource.CBONDS))
    assert declared == set(runner.CBONDS_FEATURES)
