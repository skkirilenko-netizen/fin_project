"""Проверки резервирования на явно синтетических временных файлах."""

import sys
from datetime import date
from pathlib import Path

import pytest

from finlib.config import settings
from finlib.sources import notification_journal as journal
from finlib.sources.default_notifications import Notice

sys.path.insert(0, str(settings.base_dir / "scripts"))
import snapshots_backup as backup  # noqa: E402


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Два синтетических снимка и кэш, который копировать не нужно."""
    data = tmp_path / "data"
    ratings = data / "raw/cbonds/ratings"
    ratings.mkdir(parents=True)
    (ratings / "2090-01-01.json").write_text('{"issuers": {}}', encoding="utf-8")
    (ratings.parent / "defaults_ru_2090-01-01.json").write_text('{"items": []}', encoding="utf-8")
    (ratings.parent / "defaults_ru.json").write_text("legacy", encoding="utf-8")
    (ratings.parent / "ratings_2090-01-01_issuer.json").write_text("cache", encoding="utf-8")
    monkeypatch.setattr(backup, "DATA", data)
    return data


def test_copy_and_both_checks_include_dated_defaults(source: Path, tmp_path: Path) -> None:
    """Опись содержит оба снимка, но не ручной перечень и кэш запросов."""
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    assert set(backup.read_manifest(archive)) == {
        "cbonds/ratings/2090-01-01.json", "cbonds/defaults_ru_2090-01-01.json"}
    assert backup.main(["--to", str(archive), "--check"]) == 0
    assert backup.main(["--to", str(archive), "--verify"]) == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt", "missing_source"])
def test_check_rejects_incomplete_archive(source: Path, tmp_path: Path, damage: str) -> None:
    """Отсутствующий файл и повреждённый файл не дают ложного успеха."""
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    target = archive / "cbonds/defaults_ru_2090-01-01.json"
    if damage == "missing":
        target.unlink()
    elif damage == "corrupt":
        target.write_text("broken", encoding="utf-8")
    else:
        (source / "raw/cbonds/defaults_ru_2090-01-01.json").unlink()
        target.unlink()
    previous = (archive / backup.MANIFEST).read_bytes()
    assert backup.main(["--to", str(archive), "--check"]) == 1
    assert (archive / backup.MANIFEST).read_bytes() == previous


def test_verify_works_after_loss_of_sources(source: Path, tmp_path: Path) -> None:
    """Утрата исходников не мешает проверить и восстановить архивные байты."""
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    source.rename(tmp_path / "lost_source")
    assert backup.main(["--to", str(archive), "--verify"]) == 0
    restore = tmp_path / "restore"
    restore.mkdir()
    for key, expected in backup.read_manifest(archive).items():
        target = restore / key
        backup._copy(archive / key, target)
        assert backup.metadata(target) == expected


def test_new_snapshot_is_not_silently_missing(source: Path, tmp_path: Path) -> None:
    """Новый исходный снимок без записи описи делает сверку неполной."""
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    (source / "raw/cbonds/defaults_ru_2090-01-02.json").write_text("{}", encoding="utf-8")
    assert backup.main(["--to", str(archive), "--check"]) == 1


def test_changed_source_requires_explicit_acceptance(source: Path, tmp_path: Path) -> None:
    """Законный добор рейтингов не затирает сохранённую версию молча."""
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    key = "cbonds/ratings/2090-01-01.json"
    old = (archive / key).read_bytes()
    (source / "raw" / key).write_text('{"issuers": {"test": []}}', encoding="utf-8")
    assert backup.main(["--to", str(archive)]) == 1
    assert (archive / key).read_bytes() == old
    assert backup.main(["--to", str(archive), "--accept-changed"]) == 0


@pytest.mark.parametrize("bad", ["{}", "{", '[]', '{"../escape": {"size": 0, "sha256": "bad"}}'])
def test_invalid_manifest_is_not_overwritten(source: Path, tmp_path: Path, bad: str) -> None:
    """Повреждённая или небезопасная опись не заменяется новой молча."""
    archive = tmp_path / "archive"
    archive.mkdir()
    path = archive / backup.MANIFEST
    path.write_text(bad, encoding="utf-8")
    assert backup.main(["--to", str(archive)]) == 1
    assert path.read_text(encoding="utf-8") == bad


def test_empty_sources_and_absent_archive_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ноль проверенных файлов и отсутствие копии не означают успех."""
    monkeypatch.setattr(backup, "DATA", tmp_path / "empty")
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive), "--check"]) == 1
    assert backup.main(["--to", str(archive), "--verify"]) == 1
    assert backup.main(["--to", str(archive)]) == 1
    assert not archive.exists()


def test_publication_artifacts_are_kept(source: Path, tmp_path: Path) -> None:
    """Журнал и опубликованные отчёты сохраняют единственность уведомлений."""
    output = source / "output"
    output.mkdir()
    (output / "changes_2090-01-01.md").write_text("synthetic report", encoding="utf-8")
    (output / "default_notification_journal.json").write_text(
        '{"version": 1, "entries": []}', encoding="utf-8"
    )
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    assert "output/changes_2090-01-01.md" in backup.read_manifest(archive)


def _published_notice(output: Path) -> tuple[Path, str]:
    """Создаёт синтетический отчёт с одним ключом и подтверждённым журналом."""
    notice = Notice("test-record", "test-issue", "first_seen", date(2090, 1, 1))
    path = output / "changes_2090-01-01.md"

    def render(context: journal.Context) -> str:
        """Печатает первый вывод синтетического события."""
        line = "- Тест: запись test-record впервые обнаружена 01.01.2090"
        return "# Тест\n" + (line if context.claim(notice, line) else "Повтора нет")

    return path, journal.publish(path, render)


def test_backup_rejects_journal_without_its_report(source: Path, tmp_path: Path) -> None:
    """Совпадение хешей не подтверждает восстановление осиротевшего журнала."""
    report, _ = _published_notice(source / "output")
    report.unlink()
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 1
    before = (archive / backup.MANIFEST).read_bytes()
    assert backup.main(["--to", str(archive), "--verify"]) == 1
    assert (archive / backup.MANIFEST).read_bytes() == before


@pytest.mark.parametrize("missing_index", [False, True])
def test_archive_recovers_exact_notice_without_sources(
    source: Path, tmp_path: Path, missing_index: bool,
) -> None:
    """Квитанция восстанавливает ключ и строку, в том числе после сбоя индекса."""
    report, text = _published_notice(source / "output")
    expected = journal.load(source / "output")[0]
    if missing_index:
        (source / "output" / journal.JOURNAL).unlink()
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    source.rename(tmp_path / "lost_source")
    before = {path: path.read_bytes() for path in archive.rglob("*") if path.is_file()}
    assert backup.main(["--to", str(archive), "--verify"]) == 0
    assert journal.load(archive / "output")[0] == expected
    assert (archive / "output" / report.name).read_text(encoding="utf-8") == text
    assert {path: path.read_bytes() for path in before} == before


def test_unlisted_report_cannot_confirm_archived_journal(source: Path, tmp_path: Path) -> None:
    """Отчёт вне описи не маскирует отсутствие обязательного артефакта."""
    report, _ = _published_notice(source / "output")
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    manifest = backup.read_manifest(archive)
    del manifest[f"output/{report.name}"]
    backup._write_manifest(archive, manifest)
    assert backup.main(["--to", str(archive), "--verify"]) == 1


def test_verify_rejects_escape_through_symlink(source: Path, tmp_path: Path) -> None:
    """Опись не позволяет читать файл вне копии через символьную ссылку."""
    archive = tmp_path / "archive"
    assert backup.main(["--to", str(archive)]) == 0
    target = archive / "cbonds/defaults_ru_2090-01-01.json"
    target.unlink()
    target.symlink_to(source / "raw/cbonds/defaults_ru_2090-01-01.json")
    assert backup.main(["--to", str(archive), "--verify"]) == 1
