"""Резервная копия снимков: единственное, что нельзя пересчитать.

    uv run python scripts/snapshots_backup.py --to /Volumes/архив/finsnap
    uv run python scripts/snapshots_backup.py --to ... --check   # только сверка

**Пересчитать снимок нельзя.** Отчётность, показатели, класс, маршрут —
всё это восстанавливается из источников за один прогон. Снимок рейтингов
и снимок котировок не восстанавливается вовсе: метод рейтингов отдаёт только
последнее значение, у торгов история около сорока дней. Потерянный снимок —
потерянный день истории, и другого пути к нему нет.

**Копия проверяемая, а не просто копия.** Рядом с файлами лежит `manifest.json`
с размером и sha256 каждого: «файл скопирован» и «файл скопирован верно» —
разные утверждения, и различает их только сверка. Отличающийся файл
не переписывается молча: копия — доказательная база, и затирать её тем, что
не совпало, значит терять то, ради чего она заведена.

**Куда копировать — решение человека, и оно не зашито.** Каталог называется
доводом: внешний диск, том по локальной сети, смонтированное хранилище.
Ничего не отправляется в сеть само: у проекта внешний контур, но выбор
места хранения — не дело доставки.

**Копируются файлы дня, а не сырые ответы по эмитенту.** В файле дня лежит
полная запись (с 22.09.2026 — и у рейтингов), поэтому сырые ответы
по эмитенту суть кэш доставки: их потеря стоит одного прогона, а не дня
истории.
"""

import hashlib
import json
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Что нельзя пересчитать: снимки дня. Каталоги объявлены поимённо — «всё,
# что лежит в data/raw» включило бы кэш доставки, который пересчитывается.
SOURCES: tuple[Path, ...] = (
    Path("data/raw/cbonds/ratings"),
    Path("data/raw/cbonds/quotes"),
)
MANIFEST = "manifest.json"


def digest(path: Path) -> str:
    """sha256 файла: «скопирован» и «скопирован верно» — разные утверждения."""
    found = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            found.update(block)
    return found.hexdigest()


def main() -> int:
    """Копирует снимки и сверяет копию; 1 — при расхождении либо без каталога."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--to" not in sys.argv:
        print(
            "куда копировать, не сказано: `--to ПУТЬ`. Место хранения — решение "
            "человека, и зашивать его доставка не вправе."
        )
        return 1
    where = Path(sys.argv[sys.argv.index("--to") + 1])
    only_check = "--check" in sys.argv
    if not where.parent.exists():
        print(f"каталога {where.parent} нет: том не смонтирован либо путь чужой")
        return 1

    manifest: dict[str, dict] = {}
    known = where / MANIFEST
    if known.exists():
        manifest = json.loads(known.read_text(encoding="utf-8"))

    copied = matched = differ = missing = 0
    for folder in SOURCES:
        if not folder.exists():
            logger.warning("снимков нет на диске: %s", folder)
            continue
        for item in sorted(folder.glob("*.json")):
            key = str(item.relative_to("data/raw"))
            mine = {"size": item.stat().st_size, "sha256": digest(item)}
            target = where / key
            if key in manifest and manifest[key] != mine:
                # **Расхождение не переписывается.** Снимок дня не меняется
                # по устройству: изменившийся файл означает либо правку,
                # либо порчу, и решать это человеку.
                logger.error(
                    "%s расходится с копией: было %s, стало %s",
                    key,
                    manifest[key]["sha256"][:12],
                    mine["sha256"][:12],
                )
                differ += 1
                continue
            if target.exists() and digest(target) == mine["sha256"]:
                matched += 1
                manifest[key] = mine
                continue
            if only_check:
                missing += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            if digest(target) != mine["sha256"]:
                logger.error("%s скопирован неверно: sha256 не сошёлся", key)
                differ += 1
                continue
            manifest[key] = mine
            copied += 1

    if not only_check:
        known.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(
        f"{where}: скопировано {copied}, сошлось {matched}, "
        f"расхождений {differ}, не скопировано {missing}; в описи {len(manifest)}"
    )
    if differ:
        print(
            "**Расхождение не переписано.** Снимок дня по устройству "
            "не меняется: различие означает правку либо порчу, и решать это "
            "человеку, а не доставке."
        )
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
