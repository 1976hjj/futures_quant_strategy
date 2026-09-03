"""A small, deterministic, feature-only expression compiler.

The compiler deliberately accepts only a numerical subset of Python syntax.  It
never calls ``eval`` and it derives the complete temporal dependency footprint
from the parsed tree so a declaration cannot hide a future or undeclared read.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Any

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.errors import IntegrityViolation

Number = int | float
Scalar = Number | None

_MAX_AST_NODES = 128
_MAX_WINDOW = 7560


def _missing_binary(operator: str, left: Scalar, right: Scalar) -> Scalar:
    if left is None or right is None:
        return None
    if operator == "add":
        result = left + right
    elif operator == "sub":
        result = left - right
    elif operator == "mul":
        result = left * right
    elif operator == "div":
        if right == 0:
            return None
        result = left / right
    else:  # pragma: no cover - constructed nodes are compiler-controlled
        raise AssertionError(f"unknown binary operator: {operator}")
    return float(result) if math.isfinite(float(result)) else None


@dataclass(frozen=True, slots=True, order=True)
class ExpressionDependency:
    field: str
    relative_session: int


class _Node:
    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        raise NotImplementedError

    def dependencies(self) -> frozenset[ExpressionDependency]:
        raise NotImplementedError

    def manifest(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _Constant(_Node):
    value: Number

    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        return self.value

    def dependencies(self) -> frozenset[ExpressionDependency]:
        return frozenset()

    def manifest(self) -> dict[str, Any]:
        return {"type": "constant", "value": self.value}


@dataclass(frozen=True, slots=True)
class _Field(_Node):
    name: str
    relative_session: int = 0

    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        values = history.get(self.name, ())
        index = len(values) - 1 + self.relative_session
        return values[index] if 0 <= index < len(values) else None

    def dependencies(self) -> frozenset[ExpressionDependency]:
        return frozenset({ExpressionDependency(self.name, self.relative_session)})

    def manifest(self) -> dict[str, Any]:
        return {"field": self.name, "relative_session": self.relative_session, "type": "field"}


@dataclass(frozen=True, slots=True)
class _Unary(_Node):
    operator: str
    operand: _Node

    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        value = self.operand.evaluate(history)
        if value is None:
            return None
        result = value if self.operator == "pos" else -value
        return float(result) if math.isfinite(float(result)) else None

    def dependencies(self) -> frozenset[ExpressionDependency]:
        return self.operand.dependencies()

    def manifest(self) -> dict[str, Any]:
        return {"operand": self.operand.manifest(), "operator": self.operator, "type": "unary"}


@dataclass(frozen=True, slots=True)
class _Binary(_Node):
    operator: str
    left: _Node
    right: _Node

    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        return _missing_binary(self.operator, self.left.evaluate(history), self.right.evaluate(history))

    def dependencies(self) -> frozenset[ExpressionDependency]:
        return self.left.dependencies() | self.right.dependencies()

    def manifest(self) -> dict[str, Any]:
        return {
            "left": self.left.manifest(),
            "operator": self.operator,
            "right": self.right.manifest(),
            "type": "binary",
        }


@dataclass(frozen=True, slots=True)
class _PointFunction(_Node):
    function: str
    operand: _Node

    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        value = self.operand.evaluate(history)
        if value is None:
            return None
        if self.function == "Abs":
            return abs(value)
        if value <= 0:
            return None
        return math.log(value)

    def dependencies(self) -> frozenset[ExpressionDependency]:
        return self.operand.dependencies()

    def manifest(self) -> dict[str, Any]:
        return {"function": self.function, "operand": self.operand.manifest(), "type": "point_function"}


@dataclass(frozen=True, slots=True)
class _RollingFunction(_Node):
    function: str
    field: str
    window: int

    def _window_values(self, history: dict[str, tuple[Scalar, ...]]) -> tuple[Scalar, ...] | None:
        values = history.get(self.field, ())
        if len(values) < self.window:
            return None
        return values[-self.window :]

    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        values = self._window_values(history)
        if values is None or any(value is None for value in values):
            return None
        present = tuple(float(value) for value in values if value is not None)
        if self.function == "Mean":
            return sum(present) / self.window
        if self.function == "Sum":
            return sum(present)
        mean = sum(present) / self.window
        return math.sqrt(sum((value - mean) ** 2 for value in present) / self.window)

    def dependencies(self) -> frozenset[ExpressionDependency]:
        return frozenset(ExpressionDependency(self.field, -offset) for offset in range(self.window))

    def manifest(self) -> dict[str, Any]:
        return {"field": self.field, "function": self.function, "type": "rolling_function", "window": self.window}


@dataclass(frozen=True, slots=True)
class CompiledFeatureExpression:
    """Canonical compiled form used by the registry and runtime."""

    formula: str
    _root: _Node
    dependencies: tuple[ExpressionDependency, ...]
    implementation_hash: str

    @property
    def required_history(self) -> int:
        return max((-item.relative_session for item in self.dependencies), default=0)

    @property
    def fields(self) -> frozenset[str]:
        return frozenset(item.field for item in self.dependencies)

    def evaluate(self, history: dict[str, tuple[Scalar, ...]]) -> Scalar:
        return self._root.evaluate(history)

    def manifest(self) -> dict[str, Any]:
        return {"kind": "feature_expression_ast", "root": self._root.manifest(), "schema_version": "1"}


class _Compiler:
    _binary_operators = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div"}
    _unary_operators = {ast.UAdd: "pos", ast.USub: "neg"}

    def compile(self, node: ast.AST) -> _Node:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                self._reject("only finite numeric constants are allowed")
            if not math.isfinite(float(node.value)):
                self._reject("numeric constants must be finite")
            return _Constant(node.value)
        if isinstance(node, ast.Name):
            if not node.id.isidentifier() or node.id.startswith("_"):
                self._reject("field names must be public identifiers")
            return _Field(node.id)
        if isinstance(node, ast.BinOp) and type(node.op) in self._binary_operators:
            return _Binary(self._binary_operators[type(node.op)], self.compile(node.left), self.compile(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._unary_operators:
            return _Unary(self._unary_operators[type(node.op)], self.compile(node.operand))
        if isinstance(node, ast.Call):
            return self._compile_call(node)
        self._reject(f"syntax node {type(node).__name__} is not allowed")

    def _compile_call(self, node: ast.Call) -> _Node:
        if not isinstance(node.func, ast.Name) or node.keywords:
            self._reject("only direct calls without keyword arguments are allowed")
        function = node.func.id
        if function in {"Abs", "Log"} and len(node.args) == 1:
            return _PointFunction(function, self.compile(node.args[0]))
        if function == "Ref" and len(node.args) == 2:
            field = self._field_argument(node.args[0])
            period = self._positive_integer(node.args[1], "Ref period")
            return _Field(field, -period)
        if function in {"Mean", "Sum", "Std"} and len(node.args) == 2:
            field = self._field_argument(node.args[0])
            window = self._positive_integer(node.args[1], f"{function} window")
            return _RollingFunction(function, field, window)
        self._reject(f"function {function!r} or its arguments are not allowed")

    def _field_argument(self, node: ast.AST) -> str:
        if not isinstance(node, ast.Name) or node.id.startswith("_"):
            self._reject("time-series functions require a field name as their first argument")
        return node.id

    def _positive_integer(self, node: ast.AST, label: str) -> int:
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool) or not isinstance(node.value, int):
            self._reject(f"{label} must be a positive integer literal")
        if not 1 <= node.value <= _MAX_WINDOW:
            self._reject(f"{label} must be between 1 and {_MAX_WINDOW}")
        return node.value

    @staticmethod
    def _reject(message: str) -> None:
        raise IntegrityViolation("FACTOR_EXPRESSION_REJECTED", message, rule_id="RULE-001")


def compile_feature_expression(formula: str) -> CompiledFeatureExpression:
    """Parse and canonicalize a safe feature expression."""

    try:
        parsed = ast.parse(formula, mode="eval")
    except (SyntaxError, ValueError) as error:
        raise IntegrityViolation(
            "FACTOR_EXPRESSION_REJECTED",
            f"invalid expression syntax: {error.msg if isinstance(error, SyntaxError) else error}",
            rule_id="RULE-001",
        ) from error
    if sum(1 for _ in ast.walk(parsed)) > _MAX_AST_NODES:
        raise IntegrityViolation(
            "FACTOR_EXPRESSION_REJECTED",
            f"expression exceeds the {_MAX_AST_NODES}-node complexity limit",
            rule_id="RULE-001",
        )
    root = _Compiler().compile(parsed.body)
    manifest = {"kind": "feature_expression_ast", "root": root.manifest(), "schema_version": "1"}
    return CompiledFeatureExpression(
        formula=formula,
        _root=root,
        dependencies=tuple(sorted(root.dependencies())),
        implementation_hash=content_hash(manifest),
    )
