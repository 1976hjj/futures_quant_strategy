from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from alpha_research_os.data.contracts import (
    DomainCapability,
    LicenseSpec,
    PITGrade,
    ProviderSpec,
    RevisionBehavior,
)
from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import DataDomain


def _license() -> LicenseSpec:
    return LicenseSpec(
        license_id="test-license",
        terms_url="https://example.invalid/terms",
        retrieval_allowed=True,
        local_raw_storage_allowed=True,
        derived_storage_allowed=True,
        redistribution_allowed=False,
        commercial_use_allowed=None,
        credential_required=True,
        attribution_required=True,
        notes="Test-only declaration.",
    )


def test_native_pit_capability_must_expose_available_at() -> None:
    with pytest.raises(ValidationError, match="available_at"):
        DomainCapability(
            data_domain=DataDomain.FUNDAMENTAL,
            fields=("net_profit",),
            coverage_start=date(2020, 1, 1),
            coverage_end=None,
            pit_grade=PITGrade.NATIVE_PIT,
            revision_behavior=RevisionBehavior.APPEND_WITH_HISTORY,
            time_fields=("published_at",),
        )


def test_provider_spec_and_license_are_hashable_manifests() -> None:
    capability = DomainCapability(
        data_domain=DataDomain.MARKET,
        fields=("open", "high", "low", "close", "volume", "amount"),
        coverage_start=date(2020, 1, 1),
        coverage_end=date(2024, 12, 31),
        pit_grade=PITGrade.RECONSTRUCTED_PIT,
        revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
        time_fields=("event_time",),
        preserves_delisted_history=False,
        limitations=("no historical revisions",),
    )
    provider = ProviderSpec(
        provider_id="provider-a",
        provider_version="v1",
        adapter_version="1.0.0",
        api_base_url="https://example.invalid/api",
        documentation_url="https://example.invalid/docs",
        assessed_at=datetime(2026, 9, 1, tzinfo=UTC),
        assessor="test",
        timezone="Asia/Shanghai",
        license=_license(),
        capabilities=(capability,),
        response_backfill_policy="overwrites",
        rate_limit_notes="unknown",
    )

    assert content_hash(provider).startswith("sha256:")
    assert provider.capabilities[0].pit_grade is PITGrade.RECONSTRUCTED_PIT
    assert provider.license.redistribution_allowed is False
