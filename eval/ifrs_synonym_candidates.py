"""Наименования на подъём в справочник МСФО: работа, лежащая без применения.

Человек присвоил строке код на экране разметки; у другого комплекта — своего
или чужого эмитента — та же строка разбором не опознаётся. Переносит
наименование **только справочник**: подтверждение действует у того же
эмитента (`sources/ifrs_confirmed.py`), а у чужого та же формулировка может
означать другое.

Поднимаются только присвоения вида `exact`: детализация входит в позицию,
но позицией не является, а специфическая статья позиции в ядре не имеет —
её место определяется представлением `ifrs_core_candidate`, а не этим
перечнем.

**Неоднозначное наименование отмечается, а не поднимается.** Один и тот же
текст, подтверждённый двумя разными кодами, — это либо двойник, различаемый
разделом («Кредиты и займы» в долгосрочных и в краткосрочных), либо спор
двух эмитентов о смысле. Синонимом такое объявить нельзя: статья ляжет
в позицию, которая встретилась раньше, то есть произвольно.

**Обрывок наименования синонимом не становится.** Вёрстка переносит длинные
наименования, и человек размечал вторую половину: «права пользования»,
«приобретение основных средств». Как подтверждение у своего эмитента это
работает, как синоним — нет: обрывок подцепит у другого эмитента чужую
строку, и сделает это тихо, потому что опознание по наименованию
об обрывках не знает. Признак обрывка объявлен: наименование начинается
со строчной буквы либо короче трёх слов, **и** у того же эмитента есть
полное написание, в которое оно входит. Второе условие обязательно —
без него отклонялись бы короткие настоящие наименования вроде «Резервы».

Два признака к тому же роду отклоняются сами по себе, без второго условия,
потому что тут доказывать нечего. **Незакрытая скобка** — это обрыв текста,
а не наименование: «Платежи по обязательствам аренды (» у Автодора. И
**родовое короткое слово, которому у нас уже присвоены разные коды**, —
«Прочее» у ФосАгро значит долю в результатах объектов долевого участия
в отчёте о прибыли и прочую инвестиционную деятельность в отчёте о движении
денежных средств. Синонимом такое слово подцепит у другого эмитента
что угодно, и сделает это тихо.

    uv run python eval/ifrs_synonym_candidates.py
    uv run python eval/ifrs_synonym_candidates.py --grouping 7736216869=english

Ничего не пишет ни в базу, ни на диск: это перечень для человека.
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

from finlib.cli import _load_issuers
from finlib.db import fetch_all
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_confirmed import match_key

logger = logging.getLogger(__name__)

_EXACT = """
SELECT inn, source_name, form_code, code
FROM ifrs_line_confirmation
WHERE relation = 'exact' AND source_name <> ''
"""

# Все присвоения любого вида: по ним видно родовое слово — то, которому
# у нас уже присвоены разные коды.
_ASSIGNED = """
SELECT source_name, code FROM ifrs_line_confirmation
WHERE relation <> 'not_a_line' AND source_name <> ''
"""


def main(argv: list[str] | None = None) -> int:
    """Печатает перечень наименований; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument(
        "--grouping",
        action="append",
        default=[],
        metavar="ИНН=конвенция",
        help="Разделитель разрядов вручную: ИНН=russian|english|plain",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    manual: dict[str, str] = {}
    for item in args.grouping:
        inn, _, convention = item.partition("=")
        manual[inn.strip()] = convention.strip()

    issuers, _ = _load_issuers(args.path, manual)
    if not issuers:
        # Пустой каталог не даёт нулевого перечня: он говорит, что перечень
        # не составлялся. Печатать ноль значило бы выдать отсутствие данных
        # за результат.
        print(f"в каталоге {args.path} нет документов, прошедших приём")
        return 1

    catalog = load_ifrs_lines()
    confirmed: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for row in fetch_all(_EXACT, {}):
        # Ключ строки — боевой, тот же, которым разметка ищет прежние
        # подтверждения: свой ключ в замере расходится с ним молча.
        key = (match_key(row["source_name"]), row["form_code"])
        confirmed[key].add((row["code"], row["inn"]))

    # Наименование → где оно осталось неопознанным и каким кодом подтверждено.
    gap: dict[tuple[str, str], dict] = {}
    for issuer in issuers:
        for row in issuer.extraction.unrecognised:
            key = (match_key(row.source_name), row.form)
            if key not in confirmed:
                continue
            item = gap.setdefault(
                key,
                {
                    "name": row.source_name,
                    "codes": set(),
                    "own": set(),
                    "other": set(),
                    # Кто присвоил код: по нему ищется полное написание —
                    # обрывок ловится тем, что у **этого** эмитента есть
                    # наименование длиннее.
                    "by": set(),
                },
            )
            for code, inn in confirmed[key]:
                item["codes"].add(code)
                item["by"].add(inn)
                where = item["own"] if inn == issuer.inn else item["other"]
                where.add(issuer.inn)

    # Все коды, которые человек присваивал этому наименованию, — любого вида
    # и в любой форме. Родовое слово видно именно так: «Прочее» получило
    # у нас два разных кода в двух формах одного эмитента.
    all_codes: dict[str, set[str]] = defaultdict(set)
    for row in fetch_all(_ASSIGNED, {}):
        all_codes[match_key(row["source_name"])].add(row["code"])

    spellings = _spellings(issuers, confirmed)
    ready: dict[tuple[str, str], dict] = {}
    ambiguous: dict[tuple[str, str], dict] = {}
    fragments: dict[tuple[str, str], dict] = {}
    known: dict[tuple[str, str], dict] = {}
    for key, item in gap.items():
        if len(item["codes"]) > 1:
            ambiguous[key] = item
            continue
        position = catalog.get(next(iter(item["codes"])))
        if position is not None and key[0] in position.match_names:
            # Наименование справочник уже знает, и в очереди строка стоит
            # не из-за него: не определился раздел. Поднимать нечего —
            # лечится это разделом, а не синонимом.
            known[key] = item
            continue
        whole = _whole_spelling(item, key[1], spellings, all_codes)
        if whole is not None:
            item["whole"] = whole
            fragments[key] = item
            continue
        ready[key] = item
    cross = {key: item for key, item in ready.items() if item["other"]}

    print(
        f"наименований на подъём: {len(ready)}; из них не опознаны у чужого "
        f"эмитента {len(cross)}; неоднозначных, поднимать нельзя: "
        f"{len(ambiguous)}; обрывков наименования: {len(fragments)}; "
        f"уже в справочнике (дело в разделе): {len(known)}"
    )

    print("\nПОДНИМАТЬ")
    by_code: dict[str, list[dict]] = defaultdict(list)
    for item in ready.values():
        by_code[next(iter(item["codes"]))].append(item)
    for code, items in sorted(by_code.items()):
        position = catalog.get(code)
        print(f"\n  {code} — {position.name if position else 'кода нет в ядре'}")
        for item in sorted(items, key=lambda value: value["name"]):
            mark = " (у чужого эмитента)" if item["other"] else ""
            print(f"      «{item['name']}»{mark}")

    if ambiguous:
        print("\nНЕ ПОДНИМАТЬ: наименование подтверждено разными кодами")
        for item in sorted(ambiguous.values(), key=lambda value: value["name"]):
            print(f"  «{item['name']}» → {', '.join(sorted(item['codes']))}")

    if fragments:
        print("\nНЕ ПОДНИМАТЬ: обрывок наименования, перенесённого вёрсткой")
        for item in sorted(fragments.values(), key=lambda value: value["name"]):
            print(f"  «{item['name']}» — {item['whole']}")

    if known:
        print("\nНЕ ПОДНИМАТЬ: наименование уже в справочнике, не определился раздел")
        for item in sorted(known.values(), key=lambda value: value["name"]):
            print(f"  «{item['name']}» → {', '.join(sorted(item['codes']))}")
    _core_candidates()
    return 0


_CANDIDATES = """
SELECT code, count(DISTINCT inn) AS issuers, count(*) AS confirmations,
       array_agg(DISTINCT source_name ORDER BY source_name) AS names
FROM ifrs_line_confirmation
WHERE relation <> 'not_a_line' AND source_name <> ''
GROUP BY code ORDER BY count(DISTINCT inn) DESC, code
"""


def _core_candidates() -> None:
    """Специфические статьи, подтверждённые у нескольких эмитентов независимо.

    **Счёт переехал сюда из представления базы 24.09.2026.** Представление
    `ifrs_core_candidate` считало то же самое, и ни одного запроса к нему
    в проекте не было: признак, которого никто не читает, не показывает
    ничего, а порог кандидата в методике оставался правилом без места
    применения. Здесь его читают глазами — там же, где решают о подъёме.

    Поднятие остаётся решением человека и правкой `ifrs_lines.yaml` руками:
    подтверждение говорит «у **этого** эмитента строка означает это»,
    а справочник — «у любого».
    """
    from finlib.db import connection, fetch_all
    from finlib.normalize.ifrs_lines import load_ifrs_lines

    least = load_ifrs_lines().core_candidate.distinct_issuers
    with connection() as conn:
        rows = fetch_all(_CANDIDATES, {}, conn=conn)
    ripe = [row for row in rows if row["issuers"] >= least]
    print(
        f"\nКАНДИДАТЫ В ЯДРО (порог методики — {least} эмитента и более): "
        f"{len(ripe)} из {len(rows)} кодов"
    )
    if not ripe:
        # Знаменатель печатается всегда: ноль кандидатов при неизвестном
        # числе кодов неотличим от пустого журнала подтверждений.
        print("  ни один код порога не достиг")
        return
    for row in ripe:
        names = ", ".join(f"«{name}»" for name in row["names"][:3])
        tail = " и др." if len(row["names"]) > 3 else ""
        print(
            f"  {row['code']}: эмитентов {row['issuers']}, "
            f"подтверждений {row['confirmations']} — {names}{tail}"
        )


# Сколько слов должно быть в наименовании, чтобы оно не выглядело обрывком.
_SHORT_NAME_WORDS = 3


def _spellings(issuers: list, confirmed: dict) -> dict[tuple[str, str], set[str]]:
    """Наименования, встреченные у каждого эмитента: строки форм и подтверждения.

    Полным написанием считается то, что видел сам эмитент: строка его формы
    либо его же подтверждение. Чужие написания здесь ни при чём — обрывок
    ловится тем, что у **этого** эмитента есть строка длиннее.
    """
    found: dict[tuple[str, str], set[str]] = defaultdict(set)
    for issuer in issuers:
        for row in issuer.extraction.unrecognised:
            found[(issuer.inn, row.form)].add(match_key(row.source_name))
        for value in issuer.extraction.values:
            form = next(
                (
                    code
                    for code, item in issuer.extraction.forms.items()
                    if value in item.values
                ),
                None,
            )
            if form is not None:
                found[(issuer.inn, form)].add(match_key(value.source_name))
    for (name, form), owners in confirmed.items():
        for _code, inn in owners:
            found[(inn, form)].add(name)
    return found


def _whole_spelling(
    item: dict, form: str, spellings: dict, all_codes: dict[str, set[str]]
) -> str | None:
    """Чем кандидат оказался обрывком или родовым словом; None — ни тем ни этим.

    Обрывок опознаётся двумя признаками сразу: он выглядит незаконченным
    (начинается со строчной буквы либо короче трёх слов) **и** у того же
    эмитента есть наименование длиннее, в которое он входит. Одного первого
    признака мало: «Резервы» — короткое, но настоящее наименование.

    Два случая доказывать нечем, и они отклоняются сами: незакрытая скобка —
    обрыв текста, а короткое слово, которому у нас уже присвоены разные коды, —
    родовое.
    """
    name = item["name"]
    if name.rstrip().endswith("("):
        return "текст обрывается незакрытой скобкой"
    short = name[:1].islower() or len(name.split()) < _SHORT_NAME_WORDS
    if not short:
        return None
    codes = all_codes.get(match_key(name), set())
    if len(codes) > 1:
        return "родовое слово: у нас ему присвоены " + ", ".join(sorted(codes))
    packed = match_key(name)
    for inn in item["by"] | item["own"]:
        for other in spellings.get((inn, form), ()):
            if other != packed and packed in other:
                return f"полное написание «{other}»"
    return None


if __name__ == "__main__":
    sys.exit(main())
