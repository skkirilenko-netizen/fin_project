"""Публикация и сбои журнала на синтетических файлах вне рабочего data."""

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path

import pytest

from finlib.config import settings
from finlib.scoring.routing import load_routing
from finlib.sources import cbonds_events, default_deliveries
from finlib.sources import notification_journal as journal
from finlib.sources.default_notifications import Notice, snapshots_at, timeline

sys.path.insert(0, str(settings.base_dir / "eval"))
import change_report_run as report  # noqa: E402

MOMENT = datetime(2090, 1, 3, 12, 15, tzinfo=journal.MOSCOW)
NOTICE = Notice("test-record", "test-issue", "first_seen", date(2090, 1, 2))


def _render(context: journal.Context) -> str:
    """Первый вывод синтетического ключа либо отсутствие повторного вывода."""
    line = "- Тест: запись test-record: запись впервые обнаружена 02.01.2090"
    return "# Тест\n" + (line if context.claim(NOTICE, line) else "Ключ уже выведен")


def test_rebuild_replays_saved_bytes_without_rendering(tmp_path: Path) -> None:
    """Смена источника и названия не меняет дату и содержание опубликованной строки."""
    path = tmp_path / "changes_2090-01-03.md"
    original = journal.publish(path, _render, printed_at=MOMENT)
    def changed(context: journal.Context) -> str:
        """Новая версия источника не должна использоваться для старого отчёта."""
        raise AssertionError("повторная сборка обратилась к новым данным")
    assert journal.publish(path, changed) == original
    entry = journal.load(tmp_path)[0][NOTICE.record_id, NOTICE.kind]
    assert entry.first_printed_on == "2090-01-03"
    assert entry.line in original
    assert path.read_text(encoding="utf-8") == original


def test_failure_before_publication_does_not_consume_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не сохранённый отчёт не фиксирует ключ, следующий отчёт выводит его."""
    original = journal.atomic_write
    def fail(path: Path, text: str, *, exclusive: bool = False) -> None:
        """Сбой записи отчёта до его публикации."""
        if exclusive:
            raise OSError("сбой до публикации")
        original(path, text)
    monkeypatch.setattr(journal, "atomic_write", fail)
    with pytest.raises(OSError, match="до публикации"):
        journal.publish(tmp_path / "changes_2090-01-03.md", _render, printed_at=MOMENT)
    assert not (tmp_path / journal.JOURNAL).exists()
    assert journal.load(tmp_path)[0] == {}
    monkeypatch.setattr(journal, "atomic_write", original)
    text = journal.publish(tmp_path / "changes_2090-01-04.md", _render, printed_at=MOMENT)
    assert "запись test-record" in text


def test_failure_after_report_recovers_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой между сохранениями восстанавливает ключ из отчёта и не повторяет его."""
    original = journal._write_journal
    def fail(root: Path, known: dict) -> None:
        """Сбой записи индекса после публикации отчёта."""
        raise OSError("сбой журнала")
    monkeypatch.setattr(journal, "_write_journal", fail)
    path = tmp_path / "changes_2090-01-03.md"
    with pytest.raises(OSError, match="сбой журнала"):
        journal.publish(path, _render, printed_at=MOMENT)
    assert path.exists() and not (tmp_path / journal.JOURNAL).exists()
    saved = path.read_text(encoding="utf-8")
    monkeypatch.setattr(journal, "_write_journal", original)
    next_report = journal.publish(tmp_path / "changes_2090-01-04.md", _render,
                                  printed_at=MOMENT)
    assert "Ключ уже выведен" in next_report
    assert "запись test-record" not in next_report
    assert journal.publish(path, _render) == saved
    assert len(journal.load(tmp_path)[0]) == 1


def test_concurrent_publications_claim_a_key_once(tmp_path: Path) -> None:
    """Блокировка общего каталога исключает повтор между двумя публикациями."""
    def publish(day: int) -> str:
        """Сохраняет отдельный синтетический отчёт."""
        return journal.publish(tmp_path / f"changes_2090-01-{day:02d}.md", _render,
                               printed_at=MOMENT)
    with ThreadPoolExecutor(max_workers=2) as executor:
        texts = list(executor.map(publish, [3, 4]))
    assert sum("запись test-record" in text for text in texts) == 1
    assert len(journal.load(tmp_path)[0]) == 1


def test_tampered_report_and_orphan_index_stop_publication(tmp_path: Path) -> None:
    """Порча опубликованного текста не позволяет считать журнал достоверным."""
    path = tmp_path / "changes_2090-01-03.md"
    text = journal.publish(path, _render, printed_at=MOMENT)
    path.write_text(text.replace("# Тест", "# Изменено"), encoding="utf-8")
    with pytest.raises(ValueError, match="не совпадает"):
        journal.publish(tmp_path / "changes_2090-01-04.md", _render)
    path.unlink()
    with pytest.raises(ValueError, match="не подтверждена"):
        journal.publish(tmp_path / "changes_2090-01-04.md", _render)


def test_existing_legacy_report_is_never_rewritten(tmp_path: Path) -> None:
    """Сохранённый прежний отчёт остаётся прежним даже без квитанции."""
    path = tmp_path / "changes_2090-01-02.md"
    text = "# Прежний отчёт\n\n## Срочное за сутки (01.01.2090 → 02.01.2090): 0\n"
    path.write_text(text, encoding="utf-8")
    assert journal.publish(path, _render) == text
    assert path.read_text(encoding="utf-8") == text


def test_output_command_replays_report_without_reading_live_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повтор команды сохранения возвращает старый отчёт без новой сборки и БД."""
    path = tmp_path / "changes_2090-01-03.md"
    saved = journal.publish(path, _render, printed_at=MOMENT)
    def fail(*args: object, **kwargs: object) -> int:
        """Чтение новых входов при повторе запрещено самим сценарием."""
        raise AssertionError("повтор вызвал сборку живых входов")
    monkeypatch.setattr(report, "_render_main", fail)
    monkeypatch.setattr(sys, "argv", ["change_report_run.py", "--kind", "run",
                                     "--output", str(path)])
    assert report.main() == 0
    assert path.read_text(encoding="utf-8") == saved


def test_legacy_keys_and_ambiguous_lines_are_distinguished(tmp_path: Path) -> None:
    """Ключи читаются из сохранённой строки, отсутствие id называется пробелом."""
    (tmp_path / "changes_2090-01-02.md").write_text(
        "## Срочное за сутки (01.01.2090 → 02.01.2090): 2\n"
        "- Тест: купон, выпуск test-issue, запись test-record: "
        "запись впервые обнаружена 02.01.2090\n"
        "- Другой: купон: неплатёж, льготный срок до 04.01.2090\n", encoding="utf-8")
    known, gaps = journal.load(tmp_path)
    assert (NOTICE.record_id, NOTICE.kind) in known
    assert gaps == ((date(2090, 1, 1), date(2090, 1, 2), "changes_2090-01-02.md"),)
    context = journal.Context("next.md", MOMENT, known, gaps)
    other = Notice("other", "another", "first_seen", NOTICE.day)
    assert not context.claim(other, "- неизвестно, была ли строка")


def _snapshots(root: Path) -> None:
    """Два полных синтетических снимка: отрицательная база и новое обязательство."""
    for day, rows in [(1, []), (2, [{
        "id": "test-record", "emission_id": "test-issue", "type_name_rus": "Купон",
        "status_name_rus": "Технический дефолт", "default_date": "2090-01-02",
        "actual_date": None,
    }])]:
        (root / f"defaults_ru_2090-01-{day:02d}.json").write_text(
            json.dumps({"items": rows, "total": len(rows), "count": len(rows)}),
            encoding="utf-8")


def test_late_unbound_keys_have_delivery_and_original_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Два вида одного обязательства не скрываются без привязки и не используют поздний статус."""
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    _snapshots(source)
    default_deliveries.record(source / "defaults_ru_2090-01-02.json",
                              datetime(2090, 1, 2, 14, 29, 6, tzinfo=journal.MOSCOW))
    monkeypatch.setattr(cbonds_events, "CACHE", source)
    until = date(2090, 1, 3)
    snapshots = snapshots_at(source, until)
    notices = timeline(snapshots, until)[0]
    def render(context: journal.Context) -> str:
        """Выводит поздние ключи существующим печатником."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report._late_notifications(context, notices, snapshots, {}, date(2090, 1, 2))
        return buffer.getvalue()
    text = journal.publish(output / "changes_2090-01-03.md", render, printed_at=MOMENT)
    assert "Доставлено с опозданием: 2" in text
    assert text.count("эмитент не установлен") == 2
    assert "02.01.2090 14:29:06 МСК" in text and "впервые выведено 03.01.2090" in text
    assert len(journal.load(output)[0]) == 2
    next_text = journal.publish(output / "changes_2090-01-04.md", render, printed_at=MOMENT)
    assert "Доставлено с опозданием: 0" in next_text


def test_delivery_proof_is_bound_to_bytes_and_never_invents_mtime(tmp_path: Path) -> None:
    """mtime не становится временем доставки, а исправленные байты теряют прежнее свидетельство."""
    _snapshots(tmp_path)
    source = tmp_path / "defaults_ru_2090-01-02.json"
    assert default_deliveries.observed_at(source) is None
    default_deliveries.record(source, MOMENT)
    assert default_deliveries.observed_at(source) == MOMENT
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="не подтверждает байты"):
        default_deliveries.observed_at(source)


def test_new_status_is_independent_and_late_rows_keep_original_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Позднее исполнение не меняет прежние строки, а новый переход имеет собственный ключ."""
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    _snapshots(source)
    row = {"id": "test-record", "emission_id": "test-issue", "type_name_rus": "Купон",
           "status_name_rus": "Дефолт", "default_date": "2090-01-02",
           "actual_date": "2090-01-01"}
    (source / "defaults_ru_2090-01-03.json").write_text(
        json.dumps({"items": [row], "total": 1, "count": 1}), encoding="utf-8")
    monkeypatch.setattr(cbonds_events, "CACHE", source)
    monkeypatch.setattr(cbonds_events, "SNAPSHOTS", source / "ratings")
    def render(context: journal.Context) -> str:
        """Собирает срочное и позднее существующим печатником."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report._urgent(load_routing(), {}, date(2090, 1, 2), date(2090, 1, 3), context)
        return buffer.getvalue()
    text = journal.publish(output / "changes_2090-01-03.md", render, printed_at=MOMENT)
    assert "Срочное за сутки (02.01.2090 → 03.01.2090): 1" in text
    late = text.split("## Доставлено с опозданием:", 1)[1]
    assert "статус «Технический дефолт»" in late
    assert "дата исполнения в снимке отсутствует" in late
    assert "точное время доставки неизвестно" in late
    assert set(journal.load(output)[0]) == {
        ("test-record", "first_seen"), ("test-record", "grace_end"),
        ("test-record", "status_default"),
    }


def test_cached_defaults_do_not_get_retroactive_delivery_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повтор из кэша не объявляет сегодняшнее время временем прежней доставки."""
    sys.path.insert(0, str(settings.base_dir / "scripts"))
    import defaults_fetch
    snapshot = tmp_path / f"defaults_ru_{date.today():%Y-%m-%d}.json"
    snapshot.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(defaults_fetch.cbonds, "CACHE", tmp_path)
    monkeypatch.setattr(defaults_fetch.cbonds, "fetch", lambda *args, **kwargs: {
        "items": [], "total": 0})
    assert defaults_fetch.main() == 0
    assert not (tmp_path / "default_deliveries").exists()


def test_delivery_timestamp_is_written_only_for_a_new_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Новый полный синтетический снимок получает отдельную отметку с хешем."""
    sys.path.insert(0, str(settings.base_dir / "scripts"))
    import defaults_fetch
    snapshot = tmp_path / f"defaults_ru_{date.today():%Y-%m-%d}.json"
    monkeypatch.setattr(defaults_fetch.cbonds, "CACHE", tmp_path)
    def fetch(*args: object, **kwargs: object) -> dict:
        """Явно синтетическая доставка вместо HTTP только в этом тесте."""
        raw = {"items": [], "total": 0, "count": 0}
        snapshot.write_text(json.dumps(raw), encoding="utf-8")
        return raw
    monkeypatch.setattr(defaults_fetch.cbonds, "fetch", fetch)
    assert defaults_fetch.main() == 0
    assert default_deliveries.observed_at(snapshot) is not None


def test_failed_render_has_no_receipt_or_journal(tmp_path: Path) -> None:
    """Исключение сборки не превращает подготовленный ключ в выведенный."""
    def fail(context: journal.Context) -> str:
        """Ошибка после подготовки строки, но до сохранения."""
        _render(context)
        raise RuntimeError("сборка оборвалась")
    with pytest.raises(RuntimeError, match="оборвалась"):
        journal.publish(tmp_path / "changes_2090-01-03.md", fail)
    assert journal.load(tmp_path)[0] == {}
