"""Immutable factor identity registry with compiler-backed verification."""

from __future__ import annotations

from dataclasses import dataclass

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import FactorSpec, ImplementationType

from .expression import CompiledFeatureExpression, ExpressionDependency, compile_feature_expression


@dataclass(frozen=True, slots=True)
class RegisteredFactor:
    spec: FactorSpec
    spec_hash: str
    compiled_expression: CompiledFeatureExpression | None


class FactorRegistry:
    """Bind each ``factor_id`` and version to exactly one frozen specification."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], RegisteredFactor] = {}

    def register(self, spec: FactorSpec) -> RegisteredFactor:
        validated = FactorSpec.model_validate(spec)
        compiled: CompiledFeatureExpression | None = None
        if validated.implementation_type is ImplementationType.EXPRESSION:
            assert validated.expression is not None
            compiled = compile_feature_expression(validated.expression.formula)
            declared = tuple(
                sorted(
                    ExpressionDependency(item.field, item.relative_session)
                    for item in validated.expression.dependencies
                )
            )
            if len(declared) != len(set(declared)):
                raise IntegrityViolation(
                    "FACTOR_DEPENDENCY_MISMATCH",
                    "declared expression dependencies contain duplicates",
                    rule_id="RULE-001",
                )
            if declared != compiled.dependencies:
                raise IntegrityViolation(
                    "FACTOR_DEPENDENCY_MISMATCH",
                    "declared temporal dependencies do not match the compiled AST",
                    rule_id="RULE-001",
                    context={
                        "compiled": [(item.field, item.relative_session) for item in compiled.dependencies],
                        "declared": [(item.field, item.relative_session) for item in declared],
                    },
                )
            if validated.lookback_sessions < compiled.required_history + 1:
                raise IntegrityViolation(
                    "FACTOR_LOOKBACK_UNDERDECLARED",
                    "lookback_sessions does not cover the compiled temporal reads",
                    rule_id="RULE-001",
                    context={"required": compiled.required_history + 1, "declared": validated.lookback_sessions},
                )
            if validated.warmup_sessions < compiled.required_history:
                raise IntegrityViolation(
                    "FACTOR_WARMUP_UNDERDECLARED",
                    "warmup_sessions does not cover the compiled temporal reads",
                    rule_id="RULE-001",
                    context={"required": compiled.required_history, "declared": validated.warmup_sessions},
                )
            if validated.implementation_hash != compiled.implementation_hash:
                raise IntegrityViolation(
                    "FACTOR_IMPLEMENTATION_HASH_MISMATCH",
                    "FactorSpec implementation_hash does not match the canonical AST",
                    rule_id="RULE-027",
                    context={
                        "compiled": compiled.implementation_hash,
                        "declared": validated.implementation_hash,
                    },
                )

        registration = RegisteredFactor(
            spec=validated,
            spec_hash=content_hash(validated),
            compiled_expression=compiled,
        )
        key = (validated.factor_id, validated.factor_version)
        existing = self._entries.get(key)
        if existing is not None and existing.spec_hash != registration.spec_hash:
            raise IntegrityViolation(
                "FACTOR_VERSION_CONFLICT",
                "a factor identity and version cannot be rebound to different content",
                rule_id="RULE-027",
                context={"factor_id": key[0], "factor_version": key[1]},
            )
        self._entries[key] = existing or registration
        return self._entries[key]

    def get(self, factor_id: str, factor_version: str) -> RegisteredFactor:
        try:
            return self._entries[(factor_id, factor_version)]
        except KeyError:
            raise KeyError(f"factor is not registered: {factor_id}@{factor_version}") from None

    def list(self) -> tuple[RegisteredFactor, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))
