"""Typed failures raised by the research integrity kernel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class IntegrityViolation(Exception):
    """A constitution-backed integrity rule was violated."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        rule_id: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.rule_id = rule_id
        self.context = dict(context or {})
        prefix = f"{code}"
        if rule_id:
            prefix += f" ({rule_id})"
        super().__init__(f"{prefix}: {message}")


class CanonicalizationError(IntegrityViolation):
    """A value cannot be represented by the canonical manifest format."""


class ArtifactConflictError(IntegrityViolation):
    """An immutable content address already contains different bytes."""


class CapabilityDeniedError(IntegrityViolation):
    """A caller does not possess the required unrevoked capability."""


class LedgerIntegrityError(IntegrityViolation):
    """An append-only hash chain no longer verifies."""
