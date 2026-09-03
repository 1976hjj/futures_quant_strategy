"""Translate the safe feature-expression manifest into DuckDB SQL."""

from __future__ import annotations

from typing import Any

from alpha_research_os.kernel.errors import IntegrityViolation


def _identifier(value: str) -> str:
    if not value.isidentifier() or value.startswith("_"):
        raise IntegrityViolation(
            "FACTOR_SQL_IDENTIFIER_REJECTED",
            "factor SQL identifiers must be public Python identifiers",
            rule_id="RULE-001",
        )
    return f'"{value}"'


def expression_manifest_to_duckdb(node: dict[str, Any], *, window: str) -> str:
    """Compile only manifests already accepted by the feature AST compiler."""

    node_type = node.get("type")
    if node_type == "constant":
        value = node["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid canonical numeric constant")
        return repr(value)
    if node_type == "field":
        field = _identifier(str(node["field"]))
        relative = int(node["relative_session"])
        if relative > 0:
            raise IntegrityViolation(
                "FACTOR_SQL_FUTURE_ACCESS",
                "future fields cannot be translated to factor SQL",
                rule_id="RULE-001",
            )
        return field if relative == 0 else f"lag({field}, {-relative}) OVER ({window})"
    if node_type == "unary":
        operand = expression_manifest_to_duckdb(node["operand"], window=window)
        operator = "+" if node["operator"] == "pos" else "-"
        return f"({operator}({operand}))"
    if node_type == "binary":
        left = expression_manifest_to_duckdb(node["left"], window=window)
        right = expression_manifest_to_duckdb(node["right"], window=window)
        operators = {"add": "+", "sub": "-", "mul": "*"}
        if node["operator"] == "div":
            return f"(CASE WHEN ({right}) = 0 THEN NULL ELSE ({left}) / ({right}) END)"
        if node["operator"] not in operators:
            raise ValueError("unknown canonical binary operator")
        return f"(({left}) {operators[node['operator']]} ({right}))"
    if node_type == "point_function":
        operand = expression_manifest_to_duckdb(node["operand"], window=window)
        if node["function"] == "Abs":
            return f"abs({operand})"
        if node["function"] == "Log":
            return f"(CASE WHEN ({operand}) > 0 THEN ln({operand}) END)"
        raise ValueError("unknown canonical point function")
    if node_type == "rolling_function":
        field = _identifier(str(node["field"]))
        size = int(node["window"])
        frame = f"({window} ROWS BETWEEN {size - 1} PRECEDING AND CURRENT ROW)"
        functions = {"Mean": "avg", "Sum": "sum", "Std": "stddev_pop"}
        function = functions.get(str(node["function"]))
        if function is None:
            raise ValueError("unknown canonical rolling function")
        return f"(CASE WHEN count({field}) OVER {frame} = {size} THEN {function}({field}) OVER {frame} END)"
    raise ValueError(f"unknown canonical expression node: {node_type}")
