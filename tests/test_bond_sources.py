"""Выпуски в обращении по источникам: файл эмитента, перечень Cbonds, срез биржи, поручительство.

no-bonds-impact 09.10.2026: у ПИК-СЗ файл выпусков устарел, у SPV «нет выпусков»
приходило от поручителя без собственных выпусков. Случаи — синтетика на диске.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from finlib.sources import bond_sources as sources
from finlib.sources import moex
from finlib.sources.cbonds_events import Guarantee, Issue

DAY = date(2026, 10, 9)
STATUSES = ("в обращении", "размещается")
GUARANTEES = ("Поручитель", "Гарант")


def _issue(isin: str, status: str) -> Issue:
    """Выпуск файла эмитента."""
    return Issue(emission_id=isin.lower(), name=isin, isin=isin, status=status,
                 default=False, unsettled=False, maturity=None, offer=None,
                 outstanding=None, updated=None)


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Два среза биржи, перечень Cbonds, файлы эмитентов и поручительство SPV."""
    (tmp_path / "xsec_2026-10-08.json").write_text(
        json.dumps({"history": [{"SECID": "RUB2"}, {"SECID": "RUS1"}]}), encoding="utf-8")
    (tmp_path / "xsec_2026-10-12.json").write_text(
        json.dumps({"history": [{"SECID": "RUX"}]}), encoding="utf-8")
    (tmp_path / "xsec_2026-10-08_p2.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(moex, "CACHE", tmp_path)
    monkeypatch.setattr(sources, "outstanding_universe", lambda: [
        {"emitent_inn": "A", "isin_code": "RUA1"}])
    files = {
        "A": (),  # пустой файл, выпуск — в перечне
        "B": (_issue("RUB2", "погашена"),),  # устаревший файл, бумага — в срезе
        "S": (_issue("RUS1", "в обращении"),),  # SPV с выпуском
        "G": (),  # поручитель SPV без собственных выпусков
        "N": (_issue("RUN1", "погашена"),),  # выпусков нет нигде
    }
    monkeypatch.setattr(sources, "issues_of", lambda inn: (files[inn], True))
    monkeypatch.setattr(sources, "guarantees_of", lambda inn, statuses: (
        (Guarantee("G", "Головная", "Поручитель", "S-01"),
         Guarantee("S", "Сама", "Поручитель", "S-01"))
        if inn == "S" and "Поручитель" in statuses else ()))


@pytest.mark.usefixtures("disk")
def test_each_source_is_counted_apart() -> None:
    """Пустой файл при выпуске в перечне, устаревший файл при бумаге в срезе, поручитель SPV."""
    counts = sources.bond_sources(["A", "B", "S", "G", "N"], DAY, STATUSES, GUARANTEES)
    assert counts["A"] == {sources.OWN_FILE: 0, sources.LISTED: 1, sources.BOARD: 0,
                           sources.BACKING: 0}
    # Срез — последний не позже дня: 12.10 после 09.10 не берётся.
    assert counts["B"][sources.BOARD] == 1 and counts["B"][sources.OWN_FILE] == 0
    assert counts["S"][sources.OWN_FILE] == 1 and counts["S"][sources.BOARD] == 1
    # Самопоручительство не в счёт.
    assert counts["S"][sources.BACKING] == 0
    assert counts["G"][sources.BACKING] == 1
    assert not any(counts["N"].values())


@pytest.mark.usefixtures("disk")
def test_statuses_and_guarantee_kinds_are_the_callers() -> None:
    """Перечень статусов и видов поручительства называет вызывающий, а не модуль."""
    counts = sources.bond_sources(["B", "S", "G"], DAY, ("погашена",), ("Оферент",))
    assert counts["B"][sources.OWN_FILE] == 1
    assert counts["S"][sources.OWN_FILE] == 0
    assert counts["G"][sources.BACKING] == 0


def test_without_board_slices_the_board_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Срезов на диске нет — пусто, а не отказ."""
    monkeypatch.setattr(moex, "CACHE", tmp_path)
    assert sources.board_isins(DAY) == set()
