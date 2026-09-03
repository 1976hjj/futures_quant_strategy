"""Standard-library-only worker for the restricted Python factor language.

This module is imported by the trusted parent for source validation and executed
as an isolated ``python -I -S`` script for factor evaluation.
"""

from __future__ import annotations

import ast
import json
import math
import os
import sys
from typing import Any

POLICY_VERSION = "restricted-python-factor-v1"
HELPER_NAMES = frozenset(
    {
        "abs",
        "current",
        "exp",
        "history",
        "is_missing",
        "lag",
        "len",
        "log",
        "max",
        "min",
        "rolling_mean",
        "rolling_std",
        "rolling_sum",
        "round",
        "safe_div",
        "sqrt",
        "sum",
    }
)
FIELD_HELPERS = frozenset({"current", "history", "lag", "rolling_mean", "rolling_std", "rolling_sum"})
_ALLOWED_NODE_TYPES = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Assign,
    ast.AugAssign,
    ast.If,
    ast.For,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Expr,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
)


class PluginSourceError(ValueError):
    """The submitted source falls outside the restricted plugin language."""


def _reject(message: str) -> None:
    raise PluginSourceError(message)


def validate_plugin_source(
    source: str,
    *,
    declared_fields: tuple[str, ...],
    max_ast_nodes: int,
    max_lookback: int,
) -> ast.Module:
    """Return a validated AST for one zero-argument ``factor`` function."""

    if "\x00" in source:
        _reject("NUL bytes are forbidden")
    try:
        tree = ast.parse(source, filename="<factor-plugin>", mode="exec")
    except SyntaxError as error:
        _reject(f"syntax error at line {error.lineno}")
    nodes = tuple(ast.walk(tree))
    if len(nodes) > max_ast_nodes:
        _reject(f"AST node budget exceeded: {len(nodes)} > {max_ast_nodes}")
    forbidden = next((node for node in nodes if not isinstance(node, _ALLOWED_NODE_TYPES)), None)
    if forbidden is not None:
        _reject(f"syntax is forbidden: {type(forbidden).__name__}")

    functions = [node for node in nodes if isinstance(node, ast.FunctionDef)]
    other_top_level = [
        node
        for index, node in enumerate(tree.body)
        if not isinstance(node, ast.FunctionDef)
        and not (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    if len(functions) != 1 or functions[0].name != "factor" or other_top_level:
        _reject("source must contain exactly one factor function and no executable top-level statements")
    function = functions[0]
    args = function.args
    if function.decorator_list or function.returns is not None:
        _reject("decorators and return annotations are forbidden")
    if (
        args.args
        or args.posonlyargs
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or args.defaults
        or args.kw_defaults
    ):
        _reject("factor must be a zero-argument function")

    parent_by_node = {child: parent for parent in nodes for child in ast.iter_child_nodes(parent)}
    assigned_names: set[str] = set()
    accessed_fields: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id.startswith("_") or node.id in HELPER_NAMES or node.id == "factor":
                _reject(f"local name is reserved: {node.id}")
            assigned_names.add(node.id)
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                _reject("assignment targets must be simple local names")
        if isinstance(node, ast.AugAssign) and not isinstance(node.target, ast.Name):
            _reject("augmented assignment targets must be simple local names")
        if isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name):
                _reject("for-loop targets must be simple local names")
            if not (
                isinstance(node.iter, ast.Call)
                and isinstance(node.iter.func, ast.Name)
                and node.iter.func.id == "history"
            ):
                _reject("for loops may iterate only over history(...)")
            if node.orelse:
                _reject("for-else is forbidden")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in HELPER_NAMES:
                _reject("only approved direct helper calls are allowed")
            if node.keywords:
                _reject("keyword arguments are forbidden")
            if node.func.id in FIELD_HELPERS:
                if (
                    not node.args
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    _reject(f"{node.func.id} requires a literal field name")
                if node.args[0].value not in declared_fields:
                    _reject(f"undeclared field requested: {node.args[0].value}")
                accessed_fields.add(node.args[0].value)
                if node.func.id != "current":
                    if len(node.args) != 2 or not isinstance(node.args[1], ast.Constant):
                        _reject(f"{node.func.id} requires a literal period")
                    period = node.args[1].value
                    period_limit = max_lookback - 1 if node.func.id == "lag" else max_lookback
                    if isinstance(period, bool) or not isinstance(period, int) or not 1 <= period <= period_limit:
                        _reject(f"{node.func.id} period exceeds the declared lookback")
                elif len(node.args) != 1:
                    _reject("current accepts exactly one argument")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in HELPER_NAMES and node.id not in assigned_names:
                _reject(f"unknown name: {node.id}")
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(float(value)) or abs(float(value)) > 1_000_000:
                    _reject("numeric literal exceeds the safe bound")
            elif isinstance(value, str):
                parent = parent_by_node.get(node)
                is_docstring = isinstance(parent, ast.Expr)
                is_field_argument = (
                    isinstance(parent, ast.Call)
                    and parent.args
                    and parent.args[0] is node
                    and isinstance(parent.func, ast.Name)
                    and parent.func.id in FIELD_HELPERS
                )
                if len(value) > 128 or not (is_docstring or is_field_argument):
                    _reject("string literals are allowed only for documentation and declared field access")
            elif value is not None and not isinstance(value, bool):
                _reject(f"literal type is forbidden: {type(value).__name__}")
    if accessed_fields != set(declared_fields):
        _reject("plugin field access must exactly match FactorSpec.required_fields")
    return tree


class _Context:
    def __init__(self, fields: tuple[str, ...], max_lookback: int) -> None:
        self.fields = fields
        self.max_lookback = max_lookback
        self.series: dict[str, list[float | int | None]] = {field: [] for field in fields}
        self.availability: list[str | None] = []
        self.accessed_times: list[str] = []

    def reset_access(self) -> None:
        self.accessed_times = []

    def _field(self, field: str) -> list[float | int | None]:
        if field not in self.series:
            raise ValueError("undeclared field access")
        return self.series[field]

    def _period(self, period: int) -> int:
        if isinstance(period, bool) or not isinstance(period, int) or not 1 <= period <= self.max_lookback:
            raise ValueError("invalid history period")
        return period

    def _mark(self, index: int) -> None:
        if -len(self.availability) <= index < len(self.availability):
            value = self.availability[index]
            if value is not None:
                self.accessed_times.append(value)

    def current(self, field: str) -> float | int | None:
        values = self._field(field)
        if not values:
            return None
        self._mark(-1)
        return values[-1]

    def lag(self, field: str, period: int) -> float | int | None:
        values = self._field(field)
        period = self._period(period)
        index = len(values) - period - 1
        if index < 0:
            return None
        self._mark(index)
        return values[index]

    def history(self, field: str, period: int) -> tuple[float | int | None, ...]:
        values = self._field(field)
        period = self._period(period)
        start = max(0, len(values) - period)
        for index in range(start, len(values)):
            self._mark(index)
        padding = (None,) * max(0, period - len(values))
        return padding + tuple(values[-period:])

    def rolling_values(self, field: str, period: int) -> tuple[float, ...] | None:
        values = self.history(field, period)
        if any(value is None for value in values):
            return None
        return tuple(float(value) for value in values if value is not None)


def _safe_div(left: float | int | None, right: float | int | None) -> float | None:
    if left is None or right is None or float(right) == 0:
        return None
    value = float(left) / float(right)
    return value if math.isfinite(value) else None


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, (int, float)) and not math.isfinite(float(value)))


def _execute(payload: dict[str, Any]) -> list[dict[str, Any]]:
    source = payload["source"]
    fields = tuple(payload["declared_fields"])
    max_lookback = int(payload["max_lookback"])
    tree = validate_plugin_source(
        source,
        declared_fields=fields,
        max_ast_nodes=int(payload["max_ast_nodes"]),
        max_lookback=max_lookback,
    )
    context = _Context(fields, max_lookback)

    def rolling_mean(field: str, period: int) -> float | None:
        values = context.rolling_values(field, period)
        return None if values is None else sum(values) / len(values)

    def rolling_sum(field: str, period: int) -> float | None:
        values = context.rolling_values(field, period)
        return None if values is None else sum(values)

    def rolling_std(field: str, period: int) -> float | None:
        values = context.rolling_values(field, period)
        if values is None:
            return None
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    def safe_log(value: float | int | None) -> float | None:
        return None if value is None or float(value) <= 0 else math.log(float(value))

    def safe_sqrt(value: float | int | None) -> float | None:
        return None if value is None or float(value) < 0 else math.sqrt(float(value))

    def safe_exp(value: float | int | None) -> float | None:
        if value is None:
            return None
        try:
            result = math.exp(float(value))
        except OverflowError:
            return None
        return result if math.isfinite(result) else None

    namespace: dict[str, Any] = {
        "__builtins__": {},
        "abs": abs,
        "current": context.current,
        "exp": safe_exp,
        "history": context.history,
        "is_missing": _is_missing,
        "lag": context.lag,
        "len": len,
        "log": safe_log,
        "max": max,
        "min": min,
        "rolling_mean": rolling_mean,
        "rolling_std": rolling_std,
        "rolling_sum": rolling_sum,
        "round": round,
        "safe_div": _safe_div,
        "sqrt": safe_sqrt,
        "sum": sum,
    }
    exec(compile(tree, "<factor-plugin>", "exec"), namespace, namespace)  # noqa: S102 - validated restricted AST
    function = namespace["factor"]
    rows = payload["rows"]
    sessions = sorted({row["session"] for row in rows})
    row_by_key = {(row["instrument_id"], row["session"]): row for row in rows}
    instruments = sorted({row["instrument_id"] for row in rows})
    outputs: list[dict[str, Any]] = []
    for instrument_id in instruments:
        context.series = {field: [] for field in fields}
        context.availability = []
        for session in sessions:
            row = row_by_key.get((instrument_id, session))
            values = row["values"] if row is not None else {}
            for field in fields:
                value = values.get(field)
                if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(float(value))):
                    raise ValueError("worker received a non-finite or non-numeric feature")
                context.series[field].append(value)
            context.availability.append(row["available_at"] if row is not None else None)
            if row is None:
                continue
            context.reset_access()
            result = function()
            if result is not None and (isinstance(result, bool) or not isinstance(result, (int, float))):
                raise ValueError("factor must return a number or None")
            value = None if result is None or not math.isfinite(float(result)) else float(result)
            outputs.append(
                {
                    "available_at": max(context.accessed_times, default=row["available_at"]),
                    "instrument_id": instrument_id,
                    "session": session,
                    "value": value,
                }
            )
    return outputs


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read())
        result = {
            "isolation": {
                "environment_keys": sorted(os.environ),
                "isolated_flag": bool(sys.flags.isolated),
                "no_site_flag": bool(sys.flags.no_site),
                "working_directory_empty": not any(os.scandir(".")),
            },
            "ok": True,
            "policy_version": POLICY_VERSION,
            "rows": _execute(payload),
        }
    except Exception as error:
        result = {
            "error": {"message": str(error)[:1000], "type": type(error).__name__},
            "ok": False,
            "policy_version": POLICY_VERSION,
        }
    sys.stdout.buffer.write(json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
