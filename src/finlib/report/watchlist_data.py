"""Данные интерфейса из готовых строк маршрута и неизменных отчётов, без БД и сети."""

import csv
import json
import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

from finlib.scoring.routing_catalogue import catalogue_for
from finlib.sources.notification_journal import load, receipt
from finlib.standards import Standard

UNKNOWN = "неизвестно"
EVENTS = {
    "впервые наблюдается переход «Технический дефолт» → «Дефолт»": "status",
    "запись впервые обнаружена": "first",
    "льготный срок закончился": "grace",
}


def link(path: Path | None, output: Path) -> str | None:
    """Ссылка относительно страницы только на существующий локальный файл."""
    return (
        os.path.relpath(path.resolve(), output.parent.resolve())
        if path and path.is_file()
        else None
    )


def report_data(path: Path | None) -> dict:
    """Читает сохранённые строки, даты и доставку, не восстанавливая новые события."""
    result = {
        "events": [],
        "late": [],
        "sections": [],
        "sources": [],
        "warnings": [],
        "dailyChanges": None,
        "period": UNKNOWN,
        "reportDay": UNKNOWN,
        "lateAvailable": False,
        "urgentAvailable": False,
        "reportName": path.name if path else UNKNOWN,
    }
    if path is None or not path.is_file():
        result["warnings"].append(
            "Отчёт изменений не сохранён: уведомления и доставка не установлены."
        )
        return result
    text = path.read_text(encoding="utf-8")
    entries = receipt(text, path.name)
    saved = {entry.line: entry for entry in entries or ()}
    known = load(path.parent)[0] if entries is None else {}
    day = re.search(r"^# Что изменилось: (\d{2}\.\d{2}\.\d{4})", text, re.M)
    if day:
        result["reportDay"] = day[1]
    source = re.search(r"\*Доставки дня: (.*?)\. Прогон —", text)
    if source:
        for name, status in re.findall(r"([^,]+?) — ([^,]+)", source[1]):
            result["sources"].append(
                {
                    "name": name.strip(),
                    "status": status.strip(),
                    "note": (
                        "Статус этапа из сохранённого отчёта; "
                        "done сам по себе не доказывает полноту."
                    ),
                }
            )
    else:
        result["sources"].append(
            {
                "name": "Доставки дня",
                "status": UNKNOWN,
                "note": "Полный перечень завершённых этапов не указан в отчёте.",
            }
        )
    warnings = []
    for line in text.splitlines():
        if line.startswith(">"):
            warnings.append(line.lstrip("> ").replace("**", ""))
        elif warnings:
            result["warnings"].append("\n".join(warnings))
            warnings = []
    if warnings:
        result["warnings"].append("\n".join(warnings))
    current = None
    correction = False
    claimed: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith("<!-- fin-notices-v1:"):
            break
        if line.startswith("## "):
            title = line[3:]
            current = {"title": title, "lines": []}
            result["sections"].append(current)
            correction = False
            if title.startswith("Срочное"):
                result["urgentAvailable"] = True
                period = re.search(r"\((.*?)\): (\d+)", title)
                if period:
                    result["period"], claimed["events"] = period[1], int(period[2])
            if title.startswith("Доставлено с опозданием"):
                result["lateAvailable"] = True
                count = re.search(r": (\d+)", title)
                if count:
                    claimed["late"] = int(count[1])
            changed = re.search(r"За сутки сменили корзину: (\d+) из", title)
            if changed:
                result["dailyChanges"] = int(changed[1])
        elif current is not None:
            current["lines"].append(line)
            correction |= "Уточнения сведений источника" in line
            bucket = (
                "events"
                if current["title"].startswith("Срочное")
                else "late"
                if current["title"].startswith("Доставлено с опозданием")
                else None
            )
            if bucket is None or correction or not line.startswith("- "):
                continue
            kind, event_on = "rating", UNKNOWN
            for phrase, code in EVENTS.items():
                found = re.search(re.escape(phrase) + r" (\d{2}\.\d{2}\.\d{4})", line)
                if found:
                    kind, event_on = code, found[1]
                    break
            if kind == "rating":
                assigned = re.search(r" (\d{2}\.\d{2}\.\d{4})$", line)
                if assigned:
                    event_on = assigned[1]
            owner = re.match(r"- (.*?): (.*)", line)
            name, detail = owner.groups() if owner else ("эмитент не установлен", line[2:])
            inn = re.fullmatch(r"(.*?) \((\d+)\)", name)
            entry = saved.get(line)
            record_id = re.search(r"запись ([^:]+):", line)
            if entry is None and record_id and kind in ("first", "grace", "status"):
                journal_kind = {
                    "first": "first_seen",
                    "grace": "grace_end",
                    "status": "status_default",
                }[kind]
                entry = known.get((record_id[1], journal_kind))
            delivered = re.search(r"снимок доставлен: ([^;]+)", line)
            first = re.search(r"впервые выведено (\d{2}\.\d{2}\.\d{4})", line)
            result[bucket].append(
                {
                    "name": inn[1] if inn else name,
                    "inn": inn[2] if inn else None,
                    "text": detail,
                    "line": line,
                    "kind": kind,
                    "eventOn": event_on,
                    "deliveredAt": delivered[1]
                    if delivered
                    else "точное время доставки неизвестно",
                    "firstPrintedOn": (
                        date.fromisoformat(entry.first_printed_on).strftime("%d.%m.%Y")
                        if entry
                        else first[1]
                        if first
                        else UNKNOWN
                    ),
                }
            )
    for bucket, count in claimed.items():
        if len(result[bucket]) != count:
            raise ValueError(
                f"{path.name}: счётчик {bucket} {count} "
                f"не совпадает со строками {len(result[bucket])}"
            )
    if not result["lateAvailable"]:
        result["warnings"].append(
            "В сохранённом отчёте раздел опоздавших уведомлений отсутствует; "
            "это не ноль пропущенных событий."
        )
    for item in result["sources"]:
        if item["status"] not in ("done", "skip"):
            result["warnings"].append(
                f"{item['name']}: {item['status']}; новая доставка не подтверждена."
            )
        partial = re.search(r"Снимок рейтингов неполный: (\d+) из (\d+)", text)
        if partial and "рейтинг" in item["name"]:
            item["status"] = f"неполно: {partial[1]} / {partial[2]}"
            item["note"] += " Поздний добор не подменяет сведения сохранённого отчёта."
    return result


def payload(
    rows: list[dict],
    summary: dict,
    today: date,
    *,
    output: Path,
    report: Path | None,
    csv_path: Path | None,
    cards: Path,
    limitations: list[str],
    coverage: str,
    late_report: Path | None = None,
) -> dict:
    """Передаёт все строки и готовые величины, проверяя уникальность и знаменатель."""
    if len({row["inn"] for row in rows}) != len(rows):
        raise ValueError("в списке повторяется ИНН")
    if summary.get("эмитентов", len(rows)) != len(rows):
        raise ValueError("число строк не совпадает со сводкой")
    data = report_data(report)
    if late_report:
        extra = report_data(late_report)
        data["late"] = extra["late"]
        data["lateAvailable"] = extra["lateAvailable"]
        data["lateReportLink"] = link(late_report, output)
        data["warnings"].append(
            "Опоздавшие уведомления взяты из отдельной пробной публикации; "
            "её журнал не является боевым."
        )
    presented = []
    for row in rows:
        details = [text for ground in row["grounds"] for text in ground["details"]]
        presented.append(
            {
                **row,
                "basketName": row["basket_name"],
                "group": row["subgroups"][0] if row["subgroups"] else "",
                "bonds": "1" if row["bonds"] is True else "0" if row["bonds"] is False else "?",
                "reason": details[0] if details else row["coverage"],
                "action": row["actions"][0] if row["actions"] else "",
                "cardLink": link(cards / f"{row['inn']}.md", output),
            }
        )
    return {
        **data,
        "rows": presented,
        "stats": [{"label": key, "value": value} for key, value in summary.items()],
        "day": today.strftime("%d.%m.%Y"),
        "reportLink": link(report, output),
        "csvLink": link(csv_path, output),
        "coverage": coverage,
        "limitations": limitations,
        "baskets": list(dict.fromkeys((row["basket"], row["basket_name"]) for row in rows)),
    }


def csv_rows(path: Path, *, bonds: set[str] | None = None) -> tuple[list[dict], dict]:
    """Читает готовую выгрузку для отдельной пробы; оценку и величины не пересчитывает."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        saved = list(csv.DictReader(handle, delimiter=";"))
    rows = []
    for item in saved:
        required = ("инн", "наименование", "код корзины", "корзина", "основания")
        if any(key not in item for key in required):
            raise ValueError("CSV не содержит обязательные графы списка")
        values = [
            [key, value]
            for key, value in item.items()
            if key
            in {
                "debt_total",
                "net_debt",
                "ebitda",
                "net_debt_ebitda",
                "net_debt_op_profit",
                "debt_to_op_profit",
                "equity_ratio",
                "cur_liq",
                "денежные средства",
                "платежи 12 месяцев",
                "оферты 12 месяцев",
            }
            and value
        ]
        standard = item.get("стандарт")
        if standard in {entry.value for entry in Standard}:
            catalogue = catalogue_for(Standard(standard))
            values = [[catalogue.name_of(key), value] for key, value in values]
        rows.append(
            {
                "inn": item["инн"],
                "name": item["наименование"],
                "basket": item["код корзины"],
                "basket_name": item["корзина"],
                "subgroups": item.get("подгруппа", "").split("; "),
                "actions": item.get("действие", "").split("; "),
                "values": values,
                "grounds": [
                    {
                        "name": "Основания сохранённого маршрута",
                        "details": item["основания"].split(" | ") if item["основания"] else [],
                    }
                ],
                "notes": item.get("справочные основания", "").split(" | "),
                "sources": item.get("источники оснований", "").split("; "),
                "origin": " · ".join(
                    item.get(key, "") for key in ("способ получения", "стандарт", "контур")
                ),
                "unit": item.get("единица", ""),
                "assessed": item.get("класс по документу", ""),
                "report_date": item.get("отчётная дата") or "отчётности нет",
                "months": None,
                "stale": False,
                "overdue": False,
                "chart": "",
                "coverage": "Полнота проверок не записана в CSV; сохранены все графы выгрузки.",
                "bonds": item["инн"] in bonds if bonds is not None else None,
                "csvFields": item,
            }
        )
    summary = dict(Counter(row["basket_name"] for row in rows if row["bonds"] is True))
    summary["эмитентов"] = len(rows)
    summary["с выпусками в обращении"] = (
        sum(row["bonds"] is True for row in rows) if bonds is not None else UNKNOWN
    )
    summary["без выпусков в обращении"] = (
        sum(row["bonds"] is False for row in rows) if bonds is not None else UNKNOWN
    )
    return rows, summary


def json_text(data: dict) -> str:
    """Экранирует JSON для инертного script, включая попытку закрыть тег."""
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
