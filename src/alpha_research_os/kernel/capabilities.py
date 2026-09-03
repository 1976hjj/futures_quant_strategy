"""Unforgeable-at-the-application-boundary runtime capabilities."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .errors import CapabilityDeniedError


class CapabilityScope(StrEnum):
    READ_LABEL = "READ_LABEL"
    READ_HOLDOUT = "READ_HOLDOUT"


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    actor: str
    scope: CapabilityScope
    resource: str
    issued_at: datetime
    _token: str
    _authority_id: str

    def __reduce__(self) -> None:
        raise TypeError("capabilities cannot be serialized or transferred to another process")


class CapabilityAuthority:
    """Issue and validate opaque capabilities against an internal registry."""

    def __init__(self) -> None:
        self._authority_id = secrets.token_hex(16)
        self._issued: dict[str, Capability] = {}
        self._revoked: set[str] = set()

    def __reduce__(self) -> None:
        raise TypeError("capability authorities cannot be serialized")

    def issue(
        self,
        *,
        actor: str,
        scope: CapabilityScope,
        resource: str,
        now: datetime | None = None,
    ) -> Capability:
        if not actor.strip() or not resource.strip():
            raise ValueError("actor and resource must not be blank")
        issued_at = now or datetime.now(UTC)
        if issued_at.tzinfo is None or issued_at.utcoffset() is None:
            raise ValueError("capability issue time must include a timezone")
        capability = Capability(
            capability_id=f"CAP-{secrets.token_hex(12)}",
            actor=actor,
            scope=scope,
            resource=resource,
            issued_at=issued_at,
            _token=secrets.token_hex(32),
            _authority_id=self._authority_id,
        )
        self._issued[capability.capability_id] = capability
        return capability

    def revoke(self, capability: Capability) -> None:
        self._revoked.add(capability.capability_id)

    def require(
        self,
        capability: Capability | None,
        *,
        actor: str,
        scope: CapabilityScope,
        resource: str,
    ) -> Capability:
        if capability is None:
            self._deny("no capability was supplied", actor, scope, resource)
        assert capability is not None
        registered = self._issued.get(capability.capability_id)
        valid = (
            registered is capability
            and capability._authority_id == self._authority_id
            and capability.capability_id not in self._revoked
            and capability.actor == actor
            and capability.scope is scope
            and capability.resource == resource
        )
        if not valid:
            self._deny("capability is unknown, revoked, or outside its binding", actor, scope, resource)
        return capability

    @staticmethod
    def _deny(reason: str, actor: str, scope: CapabilityScope, resource: str) -> None:
        raise CapabilityDeniedError(
            "HOLDOUT_ACCESS_DENIED" if scope is CapabilityScope.READ_HOLDOUT else "LABEL_ACCESS_DENIED",
            reason,
            rule_id="RULE-035" if scope is CapabilityScope.READ_HOLDOUT else "RULE-005",
            context={"actor": actor, "scope": scope.value, "resource": resource},
        )
