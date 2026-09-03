from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from alpha_research_os.data.contracts import FetchRequest, PITGrade
from alpha_research_os.data.publisher import PITDatasetPublisher
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.data.synthetic import normalize_synthetic_response
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.errors import ArtifactConflictError, IntegrityViolation
from alpha_research_os.kernel.specs import AuditStatus


def test_synthetic_provider_to_immutable_parquet_release(m2_provider, tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    raw_store = RawSnapshotStore(artifacts)
    snapshots = []
    records = []
    for capability in m2_provider.spec.capabilities:
        request = FetchRequest(
            request_id=f"FETCH-{capability.data_domain.value}",
            data_domain=capability.data_domain,
            start="2010-01-01",
            end="2026-01-01",
            fields=capability.fields,
        )
        response = m2_provider.fetch(request)
        captured = raw_store.capture(m2_provider.spec, response)
        assert raw_store.read_payload(captured) == response.payload
        snapshots.append(captured.reference)
        records.extend(normalize_synthetic_response(response, captured.reference))

    publisher = PITDatasetPublisher(artifacts)
    spec, release, partitions = publisher.publish(
        records,
        raw_snapshots=tuple(snapshots),
        dataset_id="a-share-synthetic-pit",
        dataset_version="DS-M2-SYNTHETIC-001",
        schema_version="1.0.0",
        provider_spec=m2_provider.spec,
        transformation_commit="1" * 40,
        pit_rule_version="1.0.0",
        adjustment_rule_version="1.0.0",
        publisher="m2-test",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        expected_historical_members=frozenset({"CN-EQ-000001", "CN-EQ-DELISTED-001"}),
    )

    assert spec.audit_status is AuditStatus.PASSED
    assert release.record_count == 11
    assert len(partitions) == len(m2_provider.spec.capabilities) == 7
    assert sum(publisher.verify_partition(reference) for reference in partitions) == 11
    assert release.dataset_spec_artifact_id == release.dataset_manifest_hash

    repeated_spec, repeated_release, repeated_partitions = publisher.publish(
        records,
        raw_snapshots=tuple(snapshots),
        dataset_id="a-share-synthetic-pit",
        dataset_version="DS-M2-SYNTHETIC-001",
        schema_version="1.0.0",
        provider_spec=m2_provider.spec,
        transformation_commit="1" * 40,
        pit_rule_version="1.0.0",
        adjustment_rule_version="1.0.0",
        publisher="m2-test",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        expected_historical_members=frozenset({"CN-EQ-000001", "CN-EQ-DELISTED-001"}),
    )
    assert repeated_spec == spec
    assert repeated_release == release
    assert repeated_partitions == partitions

    with pytest.raises(ArtifactConflictError, match="ARTIFACT_IMMUTABILITY"):
        publisher.publish(
            records,
            raw_snapshots=tuple(snapshots),
            dataset_id="a-share-synthetic-pit",
            dataset_version="DS-M2-SYNTHETIC-001",
            schema_version="1.0.0",
            provider_spec=m2_provider.spec,
            transformation_commit="1" * 40,
            pit_rule_version="1.0.0",
            adjustment_rule_version="1.0.0",
            publisher="changed-after-release",
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
            expected_historical_members=frozenset({"CN-EQ-000001", "CN-EQ-DELISTED-001"}),
        )

    with pytest.raises(IntegrityViolation, match="SURVIVORSHIP_AUDIT_NOT_CONFIGURED"):
        publisher.publish(
            records,
            raw_snapshots=tuple(snapshots),
            dataset_id="missing-survivorship-audit",
            dataset_version="DS-M2-MISSING-AUDIT-001",
            schema_version="1.0.0",
            provider_spec=m2_provider.spec,
            transformation_commit="1" * 40,
            pit_rule_version="1.0.0",
            adjustment_rule_version="1.0.0",
            publisher="m2-test",
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
        )

    market_record = next(record for record in records if record.record_type.value == "market")
    blocked_capabilities = tuple(
        capability.model_copy(update={"pit_grade": PITGrade.CURRENT_ONLY})
        if capability.data_domain.value == "market"
        else capability
        for capability in m2_provider.spec.capabilities
    )
    current_only_provider = m2_provider.spec.model_copy(update={"capabilities": blocked_capabilities})
    with pytest.raises(IntegrityViolation, match="PROVIDER_PIT_CAPABILITY_BLOCKED"):
        publisher.publish(
            (market_record,),
            raw_snapshots=tuple(snapshots),
            dataset_id="blocked-current-only",
            dataset_version="DS-M2-CURRENT-ONLY-001",
            schema_version="1.0.0",
            provider_spec=current_only_provider,
            transformation_commit="1" * 40,
            pit_rule_version="1.0.0",
            adjustment_rule_version="1.0.0",
            publisher="m2-test",
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
        )

    modified = records[0].model_copy(update={"record_hash": "sha256:" + "f" * 64})
    with pytest.raises(IntegrityViolation, match="RECORD_HASH_MISMATCH"):
        publisher.publish(
            (modified,),
            raw_snapshots=tuple(snapshots),
            dataset_id="blocked",
            dataset_version="DS-M2-BLOCKED-001",
            schema_version="1.0.0",
            provider_spec=m2_provider.spec,
            transformation_commit="1" * 40,
            pit_rule_version="1.0.0",
            adjustment_rule_version="1.0.0",
            publisher="m2-test",
            published_at=datetime(2026, 9, 1, tzinfo=UTC),
            expected_historical_members=frozenset(),
        )
