"""Разбор и вычисление формул показателей. Без eval.

Язык формул умышленно беден: четырёхзначное число — код строки отчётности,
avg(код) — полусумма на начало и конец периода, прописное имя — константа
из методики. Числовых литералов нет: любое число в формуле было бы магическим,
а это запрещено.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from enum import StrEnum


class FormulaError(ValueError):
    """Ошибка в тексте формулы: выявляется при загрузке методики."""


class NotCalculableReason(StrEnum):
    """Причина, по которой показатель не рассчитан."""

    MISSING_LINES = "missing_lines"
    NO_PREVIOUS_PERIOD = "no_previous_period"
    ZERO_DENOMINATOR = "zero_denominator"
    NEGATIVE_DENOMINATOR = "negative_denominator"
    NOT_IN_FORM = "not_in_form"
    SIGN_CHANGE = "sign_change"


@dataclass(frozen=True, slots=True)
class LineRef:
    """Ссылка на код строки отчётности."""

    code: str


@dataclass(frozen=True, slots=True)
class AvgRef:
    """Средняя балансовая величина: полусумма на начало и конец периода."""

    code: str


@dataclass(frozen=True, slots=True)
class ConstRef:
    """Именованная константа из методики."""

    name: str


@dataclass(frozen=True, slots=True)
class BinOp:
    """Двухместная операция."""

    op: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True, slots=True)
class Neg:
    """Унарный минус."""

    operand: "Node"


Node = LineRef | AvgRef | ConstRef | BinOp | Neg

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op>[+\-*/()])
  | (?P<comma>,)
    """,
    re.VERBOSE,
)

_LINE_CODE = re.compile(r"^\d{4}$")


@dataclass(frozen=True, slots=True)
class _Token:
    """Лексема формулы."""

    kind: str
    text: str
    position: int


def tokenize(text: str) -> list[_Token]:
    """Разбивает формулу на лексемы."""
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise FormulaError(f"непонятный символ {text[position]!r} в позиции {position}")
        kind = str(match.lastgroup)
        if kind != "space":
            tokens.append(_Token(kind, match.group(), position))
        position = match.end()
    return tokens


class _Parser:
    """Рекурсивный спуск по формуле."""

    def __init__(self, tokens: list[_Token], source: str) -> None:
        self.tokens = tokens
        self.source = source
        self.index = 0

    def parse(self) -> Node:
        """Разбирает формулу целиком."""
        node = self._expression()
        if self.index < len(self.tokens):
            token = self.tokens[self.index]
            raise FormulaError(f"лишнее {token.text!r} в позиции {token.position}: {self.source}")
        return node

    def _peek(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self, text: str) -> None:
        token = self._peek()
        if token is None or token.text != text:
            raise FormulaError(f"ожидалось {text!r} в формуле: {self.source}")
        self.index += 1

    def _expression(self) -> Node:
        node = self._term()
        while (token := self._peek()) is not None and token.text in "+-":
            self.index += 1
            node = BinOp(token.text, node, self._term())
        return node

    def _term(self) -> Node:
        node = self._factor()
        while (token := self._peek()) is not None and token.text in "*/":
            self.index += 1
            node = BinOp(token.text, node, self._factor())
        return node

    def _factor(self) -> Node:
        token = self._peek()
        if token is None:
            raise FormulaError(f"формула обрывается: {self.source}")
        if token.text == "-":
            self.index += 1
            return Neg(self._factor())
        if token.text == "(":
            self.index += 1
            node = self._expression()
            self._take(")")
            return node
        if token.kind == "number":
            self.index += 1
            if not _LINE_CODE.match(token.text):
                raise FormulaError(
                    f"числовой литерал {token.text!r} в формуле запрещён: это магическое "
                    f"число. Допустимы только коды строк и именованные константы ({self.source})"
                )
            return LineRef(token.text)
        if token.kind == "name":
            return self._name(token)
        raise FormulaError(f"неожиданное {token.text!r} в формуле: {self.source}")

    def _name(self, token: _Token) -> Node:
        self.index += 1
        if token.text == "avg":
            self._take("(")
            inner = self._peek()
            if inner is None or inner.kind != "number" or not _LINE_CODE.match(inner.text):
                raise FormulaError(f"avg() принимает только код строки: {self.source}")
            self.index += 1
            self._take(")")
            return AvgRef(inner.text)
        if token.text.isupper():
            return ConstRef(token.text)
        raise FormulaError(
            f"неизвестная функция {token.text!r}: допустима только avg() ({self.source})"
        )


def parse_formula(text: str) -> Node:
    """Разбирает текст формулы в дерево."""
    if not text.strip():
        raise FormulaError("пустая формула")
    return _Parser(tokenize(text), text).parse()


def line_codes(node: Node) -> set[str]:
    """Коды строк, нужные формуле за текущий период."""
    if isinstance(node, LineRef | AvgRef):
        return {node.code}
    if isinstance(node, BinOp):
        return line_codes(node.left) | line_codes(node.right)
    if isinstance(node, Neg):
        return line_codes(node.operand)
    return set()


def average_codes(node: Node) -> set[str]:
    """Коды строк, для которых нужна средняя величина, то есть предыдущий период."""
    if isinstance(node, AvgRef):
        return {node.code}
    if isinstance(node, BinOp):
        return average_codes(node.left) | average_codes(node.right)
    if isinstance(node, Neg):
        return average_codes(node.operand)
    return set()


def constant_names(node: Node) -> set[str]:
    """Имена констант, используемых формулой."""
    if isinstance(node, ConstRef):
        return {node.name}
    if isinstance(node, BinOp):
        return constant_names(node.left) | constant_names(node.right)
    if isinstance(node, Neg):
        return constant_names(node.operand)
    return set()


def denominator_of(node: Node) -> Node | None:
    """Знаменатель формулы: правый операнд деления в корне дерева.

    Все наши формулы-коэффициенты заканчиваются делением, поэтому корень
    и есть деление. Если это не так, признак denominator_must_be_positive
    к показателю неприменим, и методика такого сочетания не примет.
    """
    if isinstance(node, BinOp) and node.op == "/":
        return node.right
    return None


class ZeroDenominatorError(ArithmeticError):
    """Знаменатель обратился в ноль: коэффициент не определён."""

    def __init__(self, expression: str) -> None:
        super().__init__(f"нулевой знаменатель: {expression}")
        self.expression = expression


def describe(node: Node) -> str:
    """Текстовое представление узла — для сообщения о нулевом знаменателе."""
    if isinstance(node, LineRef):
        return f"строка {node.code}"
    if isinstance(node, AvgRef):
        return f"средняя величина строки {node.code}"
    if isinstance(node, ConstRef):
        return node.name
    if isinstance(node, Neg):
        return f"-{describe(node.operand)}"
    if isinstance(node, BinOp):
        return f"({describe(node.left)} {node.op} {describe(node.right)})"
    raise FormulaError(f"неизвестный узел {node!r}")


def evaluate(
    node: Node,
    current: Mapping[str, Decimal | None],
    previous: Mapping[str, Decimal | None] | None,
    constants: Mapping[str, Decimal],
) -> Decimal:
    """Вычисляет дерево в Decimal. Отсутствие данных проверяется до вызова."""
    if isinstance(node, LineRef):
        value = current.get(node.code)
        if value is None:
            raise FormulaError(f"строка {node.code} не раскрыта, проверка пропущена")
        return value
    if isinstance(node, AvgRef):
        if previous is None:
            raise FormulaError(f"нет предыдущего периода для avg({node.code})")
        start, end = previous.get(node.code), current.get(node.code)
        if start is None or end is None:
            raise FormulaError(f"строка {node.code} не раскрыта в одном из периодов")
        return (start + end) / 2
    if isinstance(node, ConstRef):
        return constants[node.name]
    if isinstance(node, Neg):
        return -evaluate(node.operand, current, previous, constants)
    if isinstance(node, BinOp):
        left = evaluate(node.left, current, previous, constants)
        right = evaluate(node.right, current, previous, constants)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        if right == 0:
            raise ZeroDenominatorError(describe(node.right))
        try:
            return left / right
        except (DivisionByZero, InvalidOperation) as exc:  # pragma: no cover
            raise ZeroDenominatorError(describe(node.right)) from exc
    raise FormulaError(f"неизвестный узел {node!r}")
