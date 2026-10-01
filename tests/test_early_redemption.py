"""Поле early_redemption_date: читается, но в платежи и основания не входит.

Решение владельца 01.10.2026: смысл поля не подтверждён — объявленное
исполнение call либо ближайшая возможная дата, — и до сверки оно печатается
в карточке справочно.
"""

import json
from datetime import date

from finlib.sources import cbonds_events


def test_field_is_read_and_absent_stays_none(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Дата поля читается датой; пустое поле остаётся None, а не нулём."""
    monkeypatch.setattr(cbonds_events, "CACHE", tmp_path)
    items = [
        {"id": "1", "document_rus": "ПКО ПКБ, 001Р-07", "status_name_rus": "В обращении",
         "early_redemption_date": "2026-10-19"},
        {"id": "2", "document_rus": "Прочий, 01", "status_name_rus": "В обращении",
         "early_redemption_date": None},
    ]
    (tmp_path / "emissions_7700000000.json").write_text(
        json.dumps({"items": items}), encoding="utf-8"
    )
    issues, known = cbonds_events.issues_of("7700000000")
    assert known
    assert [item.early_redemption for item in issues] == [date(2026, 10, 19), None]
