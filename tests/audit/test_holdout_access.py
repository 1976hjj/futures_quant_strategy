from __future__ import annotations

import pickle
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alpha_research_os.kernel.capabilities import CapabilityAuthority, CapabilityScope
from alpha_research_os.kernel.errors import CapabilityDeniedError, LedgerIntegrityError
from alpha_research_os.kernel.holdout import ExposureLedger, HoldoutVault


def test_unauthorized_actor_cannot_read_holdout_and_success_is_logged(tmp_path: Path) -> None:
    authority = CapabilityAuthority()
    ledger = ExposureLedger(tmp_path / "exposure.ndjson")
    vault = HoldoutVault(authority, ledger)
    vault.seal("HOLDOUT-2024Q3", b"secret observations")

    with pytest.raises(CapabilityDeniedError, match="HOLDOUT_ACCESS_DENIED"):
        vault.read(
            "HOLDOUT-2024Q3",
            actor="research-agent",
            purpose="factor search",
            capability=None,
        )
    assert ledger.verify() == ()

    capability = authority.issue(
        actor="authorized-auditor",
        scope=CapabilityScope.READ_HOLDOUT,
        resource="HOLDOUT-2024Q3",
        now=datetime(2024, 9, 1, tzinfo=UTC),
    )
    payload = vault.read(
        "HOLDOUT-2024Q3",
        actor="authorized-auditor",
        purpose="scheduled vintage evaluation",
        capability=capability,
        accessed_at=datetime(2024, 9, 1, 1, tzinfo=UTC),
    )

    assert payload == b"secret observations"
    events = ledger.verify()
    assert len(events) == 1
    assert events[0].actor == "authorized-auditor"
    assert events[0].vintage == "HOLDOUT-2024Q3"


def test_capability_and_vault_cannot_cross_process_by_serialization(tmp_path: Path) -> None:
    authority = CapabilityAuthority()
    capability = authority.issue(
        actor="auditor",
        scope=CapabilityScope.READ_HOLDOUT,
        resource="HOLDOUT-1",
    )
    vault = HoldoutVault(authority, ExposureLedger(tmp_path / "ledger.ndjson"))

    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(capability)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(vault)


def test_revoked_or_wrongly_bound_capability_is_denied(tmp_path: Path) -> None:
    authority = CapabilityAuthority()
    vault = HoldoutVault(authority, ExposureLedger(tmp_path / "ledger.ndjson"))
    vault.seal("HOLDOUT-1", b"secret")
    capability = authority.issue(
        actor="auditor",
        scope=CapabilityScope.READ_HOLDOUT,
        resource="HOLDOUT-1",
    )

    with pytest.raises(CapabilityDeniedError):
        vault.read("HOLDOUT-1", actor="agent", purpose="wrong actor", capability=capability)

    forged = replace(capability, actor="agent")
    with pytest.raises(CapabilityDeniedError):
        vault.read("HOLDOUT-1", actor="agent", purpose="forged", capability=forged)

    authority.revoke(capability)
    with pytest.raises(CapabilityDeniedError):
        vault.read("HOLDOUT-1", actor="auditor", purpose="revoked", capability=capability)


def test_exposure_ledger_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "ledger.ndjson"
    ledger = ExposureLedger(path)
    ledger.append(
        actor="auditor",
        vintage="HOLDOUT-1",
        purpose="scheduled check",
        capability_id="CAP-1",
        accessed_at=datetime(2024, 9, 1, tzinfo=UTC),
    )
    path.write_text(path.read_text(encoding="utf-8").replace("auditor", "attacker"), encoding="utf-8")

    with pytest.raises(LedgerIntegrityError, match="CHAIN_BROKEN"):
        ledger.verify()
