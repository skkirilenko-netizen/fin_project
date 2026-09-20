"""Одна позиция — одна величина: сложение, повтор и спор различаются здесь.

**Затирание величины — самый дорогой из известных ветке дефектов.** У ЛСР
краткосрочный долг однажды затёр долгосрочный, и вся долговая нагрузка
считалась по величине, заниженной в разы, при сходящемся балансе. Тот же
класс нашёлся внутри одного раздела: «Эмиссионный доход» 26 408 и
«Добавочный капитал» 16 849 легли на один код, словарь оставил последнюю
величину, и капитал не сходился ровно на 26 408.

Поэтому притязания строк на позицию сводятся в одном месте и по объявленному
правилу, а не тем, как устроен словарь:

| Случай | Исход |
|---|---|
| одна строка | величина строки |
| та же строка разобрана дважды | величина одна, повтор посчитан |
| несколько строк одного раздела | **сумма** |
| несколько строк у итоговой позиции | **спор**: величины нет |
| строка чужой формы | принято, но величина принадлежит её форме |
| строка чужого раздела | притязание отклонено |

**Один код в двух формах правомерен, а величина у них своя.** Неденежные
корректировки отчёта о движении денежных средств по определению повторяют
статьи баланса и отчёта о прибыли — амортизация, проценты, права
пользования, — и плодить для них двойники справочника хуже, чем позволить
один код двум формам. Но **одна и та же позиция в балансе и в потоке — два
разных факта**: величины разных форм не складываются и не подменяют друг
друга, поэтому притязание чужой формы в величину позиции не входит вовсе
и уходит отдельным исходом (`Fold.OTHER_FORM`). Прежде оно отклонялось,
и строка возвращалась в очередь присест за присестом.

Раздел у чужой формы не сверяется: разделы двух форм несопоставимы, и
«оборотные активы» в отчёте о движении денежных средств не означают ничего.

**Форма и раздел проверяются у притязания, а не только у опознания.**
Правило «статья чужого раздела не опознаётся вовсе» действовало у автомата
и не действовало у человека: размечая строку руками, можно было отдать
коду оборотных активов строку из внеоборотных — у ЛСР так и вышло, и
дебиторская задолженность 1 410 из внеоборотных легла к оборотной. Правило
одно, и проверяется оно в одном месте.

**Отклонённое притязание не пропадает.** Строка возвращается в очередь
разметки: отказ здесь означает «мы не знаем, чем эта строка является»,
а не «этой строки нет». Спорная величина не берётся вовсе — взять любую
из двух значило бы выбрать произвольно.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.ifrs_lines import IfrsPosition

logger = logging.getLogger(__name__)


class Fold(StrEnum):
    """Чем кончилась свёртка притязаний на одну позицию."""

    SINGLE = "single"
    SUMMED = "summed"
    REPEATED = "repeated"
    CONTESTED = "contested"
    NONE = "none"
    # Притязание принято, но строка стоит в другой форме: величина принадлежит
    # её форме, а не позиции. Это не отказ — строка опознана и в очередь
    # не возвращается, — и не величина позиции: у баланса и у потока она своя.
    OTHER_FORM = "other_form"


class Refusal(StrEnum):
    """Почему притязание строки на позицию отклонено."""

    FOREIGN_SECTION = "foreign_section"
    CONTESTED_TOTAL = "contested_total"
    # Позиция объявлена несуммируемой: одна и та же формулировка стоит
    # в форме дважды, у разных итогов, и сумма двух строк — величина,
    # которой нет ни в отчётности, ни в природе.
    NOT_SUMMABLE = "not_summable"


REFUSAL_TEXT: dict[Refusal, str] = {
    Refusal.FOREIGN_SECTION: "строка стоит в другом разделе, чем позиция",
    Refusal.CONTESTED_TOTAL: "итог не может быть раскрыт несколькими строками",
    Refusal.NOT_SUMMABLE: "позиция не складывается из нескольких строк",
}


@dataclass(frozen=True, slots=True)
class Claim:
    """Притязание строки на позицию: где строка стоит и что несёт.

    Раздел необязателен: у строки, под которой нет ни одного итога, раздела
    не определить, и судить о нём нельзя. Отсутствие раздела — не совпадение
    с любым, а отказ от проверки именно этого правила.
    """

    name: str
    values: tuple[Decimal, ...]
    form: str
    section: str | None = None
    key: tuple[str, int] | None = None


@dataclass(frozen=True, slots=True)
class Folded:
    """Величина позиции и судьба каждого притязания."""

    code: str
    kind: Fold
    values: tuple[Decimal, ...] = ()
    accepted: tuple[Claim, ...] = ()
    repeated: tuple[Claim, ...] = ()
    refused: tuple[tuple[Claim, Refusal], ...] = field(default_factory=tuple)
    # Притязания строк чужой формы: код тот же, величина принадлежит форме
    # строки. В `values` они не входят и с величиной позиции не складываются.
    other_form: tuple[Claim, ...] = ()
    reason: str = ""

    @property
    def value(self) -> Decimal | None:
        """Величина за отчётный период; None — величины нет."""
        return self.values[0] if self.values else None

    def describe(self) -> str:
        """Однострочное описание для отчёта и журнала."""
        return f"{self.code}: {self.kind.value}, {self.reason}" if self.reason else (
            f"{self.code}: {self.kind.value}"
        )


def fold(position: IfrsPosition, claims: Sequence[Claim]) -> Folded:
    """Сводит притязания строк на одну позицию в одну величину или в отказ."""
    kept: list[Claim] = []
    refused: list[tuple[Claim, Refusal]] = []
    other_form: list[Claim] = []
    for claim in claims:
        if claim.form != position.form:
            # Величина принадлежит форме строки, а не позиции: раздел при этом
            # не сверяется — разделы двух форм несопоставимы.
            other_form.append(claim)
        elif claim.section is not None and claim.section != position.section:
            refused.append((claim, Refusal.FOREIGN_SECTION))
        else:
            kept.append(claim)

    kept, repeated = _without_repeats(kept)

    if not kept and other_form:
        names = ", ".join(f"«{claim.name}»" for claim in other_form)
        forms = ", ".join(sorted({claim.form for claim in other_form}))
        return Folded(
            position.code,
            Fold.OTHER_FORM,
            repeated=tuple(repeated),
            refused=tuple(refused),
            other_form=tuple(other_form),
            reason=(
                f"строки {names} стоят в форме {forms}: величина принадлежит ей, "
                f"а не позиции формы {position.form}"
            ),
        )
    if not kept:
        return Folded(
            position.code,
            Fold.NONE,
            accepted=(),
            repeated=tuple(repeated),
            refused=tuple(refused),
            other_form=tuple(other_form),
            reason=_refusal_reason(refused),
        )
    if len(kept) == 1:
        return Folded(
            position.code,
            Fold.REPEATED if repeated else Fold.SINGLE,
            values=kept[0].values,
            accepted=(kept[0],),
            repeated=tuple(repeated),
            refused=tuple(refused),
            other_form=tuple(other_form),
            reason=(
                f"строка «{kept[0].name}» разобрана {len(repeated) + 1} раза, "
                "величина взята один раз"
                if repeated
                else _refusal_reason(refused)
            ),
        )
    if position.is_total or not position.summable:
        # Итог, раскрытый двумя строками с разными величинами, — не сумма
        # этих строк: итог один, и какая из них он, отчётность не говорит.
        #
        # То же у позиции, объявленной несуммируемой: распределение прибыли
        # печатается одной строкой на блок, и та же формулировка стоит второй
        # раз под общим совокупным доходом. У Акрона «Собственникам Компании»
        # складывалось в 75 439 — величину, которой нет ни в отчётности,
        # ни в природе.
        refusal = (
            Refusal.CONTESTED_TOTAL if position.is_total else Refusal.NOT_SUMMABLE
        )
        refused.extend((claim, refusal) for claim in kept)
        names = ", ".join(f"«{claim.name}»" for claim in kept)
        return Folded(
            position.code,
            Fold.CONTESTED,
            repeated=tuple(repeated),
            refused=tuple(refused),
            other_form=tuple(other_form),
            reason=(
                f"{REFUSAL_TEXT[refusal]}: {names}; величина не взята"
            ),
        )
    values = _summed(kept)
    names = ", ".join(f"«{claim.name}»" for claim in kept)
    return Folded(
        position.code,
        Fold.SUMMED,
        values=values,
        accepted=tuple(kept),
        repeated=tuple(repeated),
        refused=tuple(refused),
        other_form=tuple(other_form),
        reason=f"величина сложена из {len(kept)} строк: {names}",
    )


def _without_repeats(claims: list[Claim]) -> tuple[list[Claim], list[Claim]]:
    """Отделяет повторы — одну и ту же строку, разобранную дважды.

    Повтором считается полное совпадение наименования и всех величин:
    такая строка не вторая статья, а та же самая, встреченная разбором
    ещё раз. Складывать её значило бы удвоить величину — у Норникеля
    «Прибыль за год» приходит дважды одной и той же суммой.
    """
    kept: list[Claim] = []
    repeated: list[Claim] = []
    for claim in claims:
        twin = next(
            (
                item
                for item in kept
                if item.name == claim.name and item.values == claim.values
            ),
            None,
        )
        if twin is None:
            kept.append(claim)
        else:
            repeated.append(claim)
    return kept, repeated


def _summed(claims: list[Claim]) -> tuple[Decimal, ...]:
    """Сумма по периодам; период берётся, пока величина есть у всех строк.

    Складывать разное число периодов нельзя: строка, раскрытая только
    за отчётный год, в сумме сравнительного периода означала бы ноль,
    а ноль здесь запрещён тем же правилом, что и везде.
    """
    depth = min(len(claim.values) for claim in claims)
    return tuple(
        sum((claim.values[index] for claim in claims), start=Decimal(0))
        for index in range(depth)
    )


def _refusal_reason(refused: list[tuple[Claim, Refusal]]) -> str:
    """Текст об отклонённых притязаниях; пусто — отклонять было нечего."""
    if not refused:
        return ""
    return "; ".join(
        f"«{claim.name}» — {REFUSAL_TEXT[reason]}" for claim, reason in refused
    )
