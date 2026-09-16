"""Тесты регрессионного набора и прогонщика (задача 17).

Прогон набора здесь не выполняется: он ходит в базу за каждой организацией
и — в полном контуре — к модели. Проверяется то, что делает прогонщик вокруг
цикла: состав, сверка заявленной категории с фактом, покрытие, метрики
и отчёт.
"""

import copy
import importlib.util
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from finlib.config import settings


def module():
    """Загружает eval/regression_run.py: инструмент разработки, не часть пакета."""
    path = settings.base_dir / "eval" / "regression_run.py"
    spec = importlib.util.spec_from_file_location("regression_run", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


RUN = module()
SET = RUN.load_set()


def raw() -> dict:
    """Настройки набора в исходном виде, до разбора моделью."""
    return yaml.safe_load(RUN.default_set_path().read_text(encoding="utf-8"))


def entry(**changes) -> "RUN.SetEntry":
    """Запись состава с подменёнными полями."""
    base = {
        "category_id": 1,
        "category": "Упрощённая отчётность",
        "name": "ООО «Проверка»",
        "inn": "2100010824",
        "reason": "основание включения",
        "category_status": "гипотеза",
    }
    return RUN.SetEntry(**{**base, **changes})


# --- состав ------------------------------------------------------------------


def test_sample_is_read_with_reasons_and_statuses() -> None:
    """У каждой организации есть основание включения и статус гипотезы."""
    assert len(SET.organizations) >= 30
    for item in SET.organizations:
        assert item.reason.strip(), item.inn
        assert item.category_status in RUN.CATEGORY_STATUS


def test_reserve_stays_in_the_set_but_out_of_the_run() -> None:
    """Резерв остаётся в составе и в прогон не идёт.

    Шесть однотипных вырожденных балансов прогон удлиняют, а нового
    не показывают; выбрасывать их из файла при этом незачем.
    """
    reserve = [item for item in SET.organizations if not item.in_run]
    assert reserve
    assert set(SET.inns).isdisjoint({item.inn for item in reserve})


def test_wrong_inn_checksum_is_refused() -> None:
    """Опечатка в ИНН даёт не ошибку прогона, а тихий пропуск организации."""
    with pytest.raises(ValidationError, match="контрольным разрядам"):
        entry(inn="2100010825")


def test_unknown_category_status_is_refused() -> None:
    """Статус гипотезы берётся из списка, а не пишется свободно."""
    with pytest.raises(ValidationError, match="неизвестен"):
        entry(category_status="вроде бы да")


def test_category_name_must_match_the_settings() -> None:
    """Состав и настройки набора не вправе расходиться в наименовании.

    Иначе переименованная категория молча распадается на две: одну из файла
    состава, другую из настроек, — и покрытие считается по обеим.
    """
    payload = raw()
    base = RUN.RegressionSet(**payload)
    with pytest.raises(ValueError, match="в настройках"):
        base.with_organizations((entry(category="Упрощёнка"),))


def test_duplicate_inn_is_refused() -> None:
    """Одна организация дважды — и покрытие, и метрики считаются неверно."""
    base = RUN.RegressionSet(**raw())
    with pytest.raises(ValueError, match="дважды"):
        base.with_organizations((entry(), entry()))


def test_unknown_holding_flag_is_refused() -> None:
    """Набор не вправе измерять признак по коду, которого в методике нет."""
    payload = raw()
    payload["coverage"]["holding_flag"] = "no_such_flag"
    with pytest.raises(ValidationError, match="в методике не объявлен"):
        RUN.RegressionSet(**payload)


def test_category_declares_a_feature_or_says_why_it_cannot() -> None:
    """Категория без признака и без причины — недосмотр, а не решение."""
    payload = copy.deepcopy(raw())
    payload["categories"][0].pop("feature")
    with pytest.raises(ValidationError, match="ровно одно"):
        RUN.RegressionSet(**payload)

    payload = copy.deepcopy(raw())
    payload["categories"][0]["manual"] = "и признак, и причина"
    with pytest.raises(ValidationError, match="ровно одно"):
        RUN.RegressionSet(**payload)


def test_liquidation_is_checked_by_hand_and_says_so() -> None:
    """Признака ликвидации в отчётности нет, и он не выдуман ради таблицы."""
    liquidation = next(item for item in SET.categories if item.id == 10)
    assert liquidation.feature is None
    assert liquidation.manual.strip()


# --- размерные группы --------------------------------------------------------


def test_size_bounds_ascend_and_end_open() -> None:
    """Границы размерных групп идут по возрастанию и не закрывают верх."""
    payload = copy.deepcopy(raw())
    payload["size_groups"]["bounds"][-1]["max_revenue"] = "3000000"
    with pytest.raises(ValidationError, match="открытой сверху"):
        RUN.RegressionSet(**payload)

    payload = copy.deepcopy(raw())
    payload["size_groups"]["bounds"][0]["max_revenue"] = "900000"
    with pytest.raises(ValidationError, match="возрастанию"):
        RUN.RegressionSet(**payload)


def test_size_group_is_chosen_by_revenue() -> None:
    """Группа определяется выручкой; граница относится к младшей группе."""
    groups = SET.size_groups
    assert groups.group_of(Decimal(50_000)).code == "micro"
    assert groups.group_of(Decimal(120_000)).code == "micro"
    assert groups.group_of(Decimal(120_001)).code == "small"
    assert groups.group_of(Decimal(5_000_000)).code == "large"


def test_size_group_is_not_guessed_without_revenue() -> None:
    """Без раскрытой выручки размер не угадывается.

    У транзитной структуры активы не описывают ни оборот, ни размер
    деятельности: 44 771 тыс. руб. выручки при валюте баланса 418.
    """
    assert SET.size_groups.group_of(None) is None


# --- признаки и покрытие -----------------------------------------------------


def test_features_are_measured_over_the_database() -> None:
    """Признаки берутся из базы, а не из объявлений файла состава."""
    measured = RUN.features_of(SET)
    assert measured
    # Три пробы экспертной оценки дают эти признаки по построению.
    assert RUN.Feature.SIMPLIFIED_FORMS in measured["2100010824"]
    assert RUN.Feature.NEGATIVE_EQUITY in measured["2100010824"]
    assert RUN.Feature.FULL_FORMS in measured["7736050003"]
    assert RUN.Feature.QUARANTINED in measured["2522002003"]


def test_uncovered_dimension_says_so() -> None:
    """Непокрытое измерение называется непокрытым, а не пустой строкой."""
    assert RUN._listed(set()) == "не покрыто"
    assert RUN._listed({"7736050003"}) == "7736050003"


def test_declared_category_is_checked_against_the_fact() -> None:
    """Заявленная категория — гипотеза, и отчёт сверяет её с признаком."""
    measured = {
        "2100010824": {RUN.Feature.SIMPLIFIED_FORMS},
        "7736050003": {RUN.Feature.FULL_FORMS},
    }
    subset = RUN.RegressionSet(**raw()).with_organizations(
        (
            entry(inn="2100010824"),
            entry(
                inn="7736050003",
                category_id=4,
                category="Отрицательный собственный капитал",
            ),
        )
    )
    checks = {item.id: item for item in RUN.categories_check(subset, measured, [])}
    assert checks[1].declared == 1
    assert checks[1].confirmed == 1
    # Заявлен отрицательный капитал, а по базе его нет: категория не сошлась.
    assert checks[4].declared == 1
    assert checks[4].confirmed == 0
    assert "7736050003" in checks[4].unconfirmed


# --- метрики и отчёт ---------------------------------------------------------


def runs() -> list:
    """Четыре вымышленных итога: с классом, без класса, остановка и отказ."""
    return [
        RUN.OrgRun(
            inn="1" * 10, name="Первая", category_id=1, category="Первая",
            expected="analysis", ok=True, seconds=1.0, class_code="B",
            as_expected=True,
        ),
        RUN.OrgRun(
            inn="2" * 10, name="Вторая", category_id=1, category="Первая",
            expected="analysis", ok=True, seconds=3.0, as_expected=True,
        ),
        RUN.OrgRun(
            inn="3" * 10, name="Третья", category_id=1, category="Первая",
            expected="analysis", ok=False, seconds=2.0,
            stage="контроли качества", reason="все комплекты в карантине",
        ),
        RUN.OrgRun(
            inn="4" * 10, name="Банк", category_id=12, category="Вне периметра",
            expected="refusal", ok=False, seconds=0.5,
            stage="получение отчётности", reason="организация не найдена",
            as_expected=True,
        ),
    ]


def test_metrics_separate_stops_from_absent_class() -> None:
    """Остановка на этапе и отсутствие класса — разные исходы.

    Организация, остановленная на контролях, о присвоении класса не говорит
    ничего, и в долю без класса она не входит.
    """
    found = RUN.run_metrics(runs(), SET, RUN.Contour.FAST, datetime.now())
    assert found["прошли цикл"] == 2
    assert found["остановились"] == 1
    assert found["остановки по этапам"] == {"контроли качества": 1}
    assert found["классы"] == {"B": 1}
    assert found["без класса"] == 1
    assert found["доля без класса"] == "50,0 %"


def test_expected_refusal_counts_as_success() -> None:
    """Организация вне периметра отказом подтверждает правило, а не нарушает.

    Кредитная организация в ГИР БО отсутствует, и отказ по ней — правильный
    исход. В числе остановок она не значится.
    """
    found = RUN.run_metrics(runs(), SET, RUN.Contour.FAST, datetime.now())
    assert found["ожидался отказ"] == 1
    assert found["итог совпал с ожиданием"] == "3 из 4"
    assert found["остановились"] == 1


def _quarantine_entry(**changes):
    """Запись состава организации, от которой ожидается отбраковка."""
    base = {
        "category_id": 13,
        "category": "Организации без выручки по устройству",
        "inn": "9707042940",
        "expected_outcome": "quarantine_expected",
        "reason": "СФО: выручки нет по устройству",
    }
    return entry(**{**base, **changes})


def _quarantined_run(**changes):
    """Итог организации, отчётность которой отбракована целиком."""
    base = {
        "inn": "9707042940",
        "name": "СФО",
        "category_id": 13,
        "category": "Организации без выручки по устройству",
        "expected": "quarantine_expected",
        "ok": False,
        "seconds": 0.1,
        "stage": "расчёт показателей",
        "reason": "по загруженным данным не рассчитан ни один показатель",
        "sets": 2,
        "quarantined": 2,
    }
    return RUN.OrgRun(**{**base, **changes})


def test_expected_quarantine_counts_as_success() -> None:
    """Отбраковка организации без выручки по устройству — ожидаемый исход.

    Блокирующий контроль обязательных строк для неё и должен срабатывать:
    выручки у специализированного финансового общества нет по устройству.
    Ослаблять контроль ради таких организаций нельзя — нераскрытая выручка
    у работающей организации остаётся серьёзным сигналом.
    """
    subset = RUN.RegressionSet(**raw()).with_organizations((_quarantine_entry(),))
    found = [_quarantined_run()]
    RUN._finalize(found, {"9707042940": {RUN.Feature.REVENUE_NOT_DISCLOSED}}, subset)
    assert found[0].as_expected
    assert found[0].category_confirmed


def test_quarantine_expected_requires_full_quarantine() -> None:
    """Ожидается не любая остановка, а именно отбраковка всех комплектов."""
    subset = RUN.RegressionSet(**raw()).with_organizations((_quarantine_entry(),))
    partial = [_quarantined_run(sets=2, quarantined=1)]
    RUN._finalize(partial, {"9707042940": set()}, subset)
    assert not partial[0].as_expected


def test_organization_that_suddenly_passed_is_not_as_expected() -> None:
    """Организация, прошедшая цикл вопреки ожиданию, успехом не считается.

    Это сведение о том, что гипотеза устарела: выручку раскрыли. Молча
    засчитать такое за совпадение значило бы потерять сигнал.
    """
    subset = RUN.RegressionSet(**raw()).with_organizations((_quarantine_entry(),))
    passed = [_quarantined_run(ok=True, stage=None, reason=None, quarantined=0)]
    RUN._finalize(passed, {"9707042940": set()}, subset)
    assert not passed[0].as_expected


def test_undisclosed_revenue_is_not_near_zero_turnover() -> None:
    """Нераскрытая выручка и оборот около нуля — разные признаки.

    Организация с оборотом около нуля его всё-таки показала; у организации
    без выручки показывать нечего, и её отчётность отбраковывается целиком.
    Смешение этих признаков делало категорию «нулевые обороты» непроверяемой.
    """
    assert RUN.Feature.REVENUE_NOT_DISCLOSED in RUN.FEATURE_NAMES
    assert RUN.FEATURE_NAMES[RUN.Feature.REVENUE_NOT_DISCLOSED] != RUN.FEATURE_NAMES[
        RUN.Feature.NEAR_ZERO_REVENUE
    ]
    category = next(item for item in SET.categories if item.id == 13)
    assert category.feature is RUN.Feature.REVENUE_NOT_DISCLOSED


def test_refusal_of_the_runner_itself_is_not_a_success() -> None:
    """Сбой прогонщика отказом методики не считается."""
    subset = RUN.RegressionSet(**raw()).with_organizations(
        (entry(inn="7707083893", category_id=12, category="Вне периметра, ожидается отказ"),)
    )
    broken = [
        RUN.OrgRun(
            inn="7707083893", name="Банк", category_id=12,
            category="Вне периметра, ожидается отказ", expected="refusal",
            ok=False, seconds=0.1, stage="сбой прогонщика", reason="TypeError",
        )
    ]
    RUN._finalize(broken, {"7707083893": set()}, subset)
    assert not broken[0].as_expected


def test_unloaded_organization_is_not_an_expected_refusal() -> None:
    """Организация, до которой прогон не дошёл, отказом методики не считается.

    Иначе прогон без загруженных данных отчитывался бы ожидаемыми отказами
    по всем, кому положено отказать, — и метрика лгала бы тем громче, чем
    хуже прошёл прогон.
    """
    subset = RUN.RegressionSet(**raw()).with_organizations(
        (
            entry(
                inn="7707083893",
                category_id=12,
                category="Вне периметра, ожидается отказ",
                expected_outcome="refusal",
            ),
        )
    )
    absent = [
        RUN.OrgRun(
            inn="7707083893", name="Банк", category_id=12,
            category="Вне периметра, ожидается отказ", expected="refusal",
            ok=False, seconds=0.0, attempted=False,
            stage="получение отчётности", reason="отчётность не загружена",
        )
    ]
    RUN._finalize(absent, {"7707083893": set()}, subset)
    assert not absent[0].as_expected


def test_fast_contour_has_no_text_metrics() -> None:
    """Метрик текстового слоя у прогона без модели нет вовсе.

    Печатать их нулями значило бы сказать, что модель не прошла контроли,
    тогда как её не спрашивали.
    """
    found = RUN.run_metrics(runs(), SET, RUN.Contour.FAST, datetime.now())
    assert "доля прошедших контроли текста" not in found


def test_model_is_named_only_where_it_worked() -> None:
    """Модель входит в контролируемые параметры полного контура."""
    started = datetime.now()
    fast = RUN.parameters(SET, RUN.Contour.FAST, started)
    full = RUN.parameters(SET, RUN.Contour.FULL, started)
    assert fast["модель"] == "не привлекалась"
    assert full["модель"] == settings.llm_model
    # Версии справочников — тоже контролируемые параметры: расхождение прогонов
    # без них с одинаковой вероятностью означает и правку методики, и смену
    # модели.
    assert set(fast["справочники"]) >= {"metrics", "scoring", "flags", "signals"}


def test_report_renders_parameters_and_tables() -> None:
    """Отчёт — таблица с шапкой, пригодная для представления руководству."""
    started = datetime.now()
    measured = RUN.features_of(SET)
    report = RUN.Report(
        started=started,
        parameters=RUN.parameters(SET, RUN.Contour.FAST, started),
        organizations=[asdict(item) for item in runs()],
        coverage=[asdict(item) for item in RUN.coverage(SET, measured)],
        categories=[asdict(item) for item in RUN.categories_check(SET, measured, [])],
        metrics=RUN.run_metrics(runs(), SET, RUN.Contour.FAST, started),
    )
    text = RUN.render(report)
    assert "## Параметры прогона" in text
    assert "## Заявленное покрытие против фактического" in text
    assert "## Покрытие набора" in text
    assert "версия кода" in text
    # Остановка названа этапом и причиной, а ожидаемый отказ — ожидаемым.
    assert "все комплекты в карантине" in text
    assert "остановлено: контроли качества" in text
    assert "отказ, как и ожидался" in text


def test_unloaded_organization_is_named_as_such() -> None:
    """Незагруженная отчётность отличается от пустой.

    Без этой проверки организация останавливалась бы на расчёте показателей
    с причиной «ни одного не рассчитано», и по отчёту нельзя было бы понять,
    что прогон просто шёл без обращения к источнику. Сеть и цикл здесь
    не трогаются: проверка отвечает раньше.
    """
    found = RUN.run_one(entry(inn="7727728250"), RUN.Contour.FAST, fetch=False)
    if found.ok:  # pragma: no cover — организация уже загружена прежним прогоном
        pytest.skip("организация загружена, случай не воспроизводится")
    assert found.stage == "получение отчётности"
    assert "--fetch" in found.reason


def test_empty_metric_is_printed_as_absent() -> None:
    """Пустая величина печатается словом, а не пустым заголовком."""
    assert RUN._metric_lines({"срабатывания": {}}) == ["- срабатывания: нет"]
    assert RUN._metric_lines({"попыток": None}) == ["- попыток: —"]
