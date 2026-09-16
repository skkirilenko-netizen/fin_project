"""Тесты регрессионного набора и прогонщика (задача 17).

Прогон набора здесь не выполняется: он ходит в базу за каждой организацией
и — в полном контуре — к модели. Проверяется то, что делает прогонщик вокруг
цикла: состав набора, измерение покрытия по базе, метрики и отчёт.
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
    """Состав набора в исходном виде, до разбора моделью."""
    return yaml.safe_load(RUN.default_set_path().read_text(encoding="utf-8"))


# --- состав набора -----------------------------------------------------------


def test_every_organization_names_its_reason() -> None:
    """Организация без основания включения — это шум, а не набор."""
    assert SET.organizations
    for item in SET.organizations:
        assert item.reason.strip(), item.inn


def test_duplicate_inn_is_refused() -> None:
    """Одна организация дважды — и покрытие, и метрики считаются неверно."""
    payload = raw()
    payload["organizations"].append(copy.deepcopy(payload["organizations"][0]))
    with pytest.raises(ValidationError, match="дважды"):
        RUN.RegressionSet(**payload)


def test_unknown_holding_flag_is_refused() -> None:
    """Набор не вправе измерять покрытие по коду, которого в методике нет.

    Иначе переименованный флаг молча превращает измерение в непокрытое,
    и набор выглядит хуже, чем он есть, — или лучше, если наоборот.
    """
    payload = raw()
    payload["coverage"]["holding_flag"] = "no_such_flag"
    with pytest.raises(ValidationError, match="в методике не объявлен"):
        RUN.RegressionSet(**payload)


def test_size_bounds_ascend_and_end_open() -> None:
    """Границы размерных групп идут по возрастанию и не закрывают верх."""
    payload = raw()
    payload["size_groups"]["bounds"][-1]["max_revenue"] = "3000000"
    with pytest.raises(ValidationError, match="открытой сверху"):
        RUN.RegressionSet(**payload)

    payload = raw()
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


# --- покрытие ----------------------------------------------------------------


def test_coverage_is_measured_over_the_database() -> None:
    """Признаки покрытия берутся из базы, а не из объявлений файла состава."""
    found = {item.name: item for item in RUN.coverage(SET)}
    assert found["Полный набор форм"].count >= 1
    assert found["Упрощённый набор форм"].count >= 1
    # Три пробы экспертной оценки дают эти измерения по построению.
    assert found["Отрицательный собственный капитал"].count >= 1
    assert found["Отбракованные комплекты отчётности"].count >= 1
    assert found["Признаки холдинговой структуры"].count >= 1


def test_uncovered_dimension_says_so() -> None:
    """Непокрытое измерение называется непокрытым, а не пустой строкой."""
    assert RUN._listed(set()) == "не покрыто"
    assert RUN._listed({"7736050003"}) == "7736050003"


# --- метрики и отчёт ---------------------------------------------------------


def runs() -> list:
    """Три вымышленных итога: пройдено с классом, без класса и остановка."""
    return [
        RUN.OrgRun(inn="1" * 10, name="Первая", ok=True, seconds=1.0, class_code="B"),
        RUN.OrgRun(inn="2" * 10, name="Вторая", ok=True, seconds=3.0),
        RUN.OrgRun(
            inn="3" * 10,
            name="Третья",
            ok=False,
            seconds=2.0,
            stage="контроли качества",
            reason="все комплекты в карантине",
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
    report = RUN.Report(
        started=started,
        parameters=RUN.parameters(SET, RUN.Contour.FAST, started),
        organizations=[asdict(item) for item in runs()],
        coverage=[asdict(item) for item in RUN.coverage(SET)],
        metrics=RUN.run_metrics(runs(), SET, RUN.Contour.FAST, started),
    )
    text = RUN.render(report)
    assert "## Параметры прогона" in text
    assert "## Покрытие набора" in text
    assert "версия кода" in text
    # Остановка названа этапом и причиной, а не просто отсутствием класса.
    assert "все комплекты в карантине" in text
    assert "остановлено: контроли качества" in text


def test_unloaded_organization_is_named_as_such() -> None:
    """Незагруженная отчётность отличается от пустой.

    Без этой проверки организация останавливалась бы на расчёте показателей
    с причиной «ни одного не рассчитано», и по отчёту нельзя было бы понять,
    что прогон просто шёл без обращения к источнику. Сеть и цикл здесь
    не трогаются: проверка отвечает раньше.
    """
    entry = RUN.SetEntry(inn="9" * 10, reason="организации в базе нет")
    found = RUN.run_one(entry, RUN.Contour.FAST, fetch=False)
    assert not found.ok
    assert found.stage == "получение отчётности"
    assert "--fetch" in found.reason


def test_empty_metric_is_printed_as_absent() -> None:
    """Пустая величина печатается словом, а не пустым заголовком."""
    assert RUN._metric_lines({"срабатывания": {}}) == ["- срабатывания: нет"]
    assert RUN._metric_lines({"попыток": None}) == ["- попыток: —"]
