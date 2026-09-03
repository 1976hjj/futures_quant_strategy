"""Audit-gated publication of immutable PIT Parquet datasets."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq

from alpha_research_os.kernel.artifacts import ArtifactRef, ArtifactStore
from alpha_research_os.kernel.audit import FindingSeverity
from alpha_research_os.kernel.canonical import FrozenManifest, canonical_json_bytes
from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import AuditStatus, DatasetSpec, PartitionSpec

from .audit import audit_normalized_records, audit_survivorship
from .contracts import DatasetRelease, NormalizedRecord, PITGrade, ProviderSpec, RawSnapshotRef


class PITDatasetPublisher:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    @staticmethod
    def _parquet_bytes(records: tuple[NormalizedRecord, ...]) -> bytes:
        rows = []
        for record in records:
            rows.append(
                {
                    "logical_key": record.logical_key,
                    "record_type": record.record_type.value,
                    "instrument_id": record.instrument_id,
                    "event_time": record.event_time,
                    "published_at": record.published_at,
                    "available_at": record.available_at,
                    "ingested_at": record.ingested_at,
                    "source": record.source,
                    "source_record_id": record.source_record_id,
                    "revision_id": record.revision_id,
                    "raw_snapshot_id": record.raw_snapshot_id,
                    "record_hash": record.record_hash,
                    "values_json": canonical_json_bytes(record.value_map()).decode("utf-8"),
                }
            )
        table = pa.Table.from_pylist(rows)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression="zstd", version="2.6")
        return sink.getvalue().to_pybytes()

    def publish(
        self,
        records: Iterable[NormalizedRecord],
        *,
        raw_snapshots: tuple[RawSnapshotRef, ...],
        dataset_id: str,
        dataset_version: str,
        schema_version: str,
        provider_spec: ProviderSpec,
        transformation_commit: str,
        pit_rule_version: str,
        adjustment_rule_version: str,
        publisher: str,
        published_at: datetime,
        expected_historical_members: frozenset[str] | None = None,
    ) -> tuple[DatasetSpec, DatasetRelease, tuple[ArtifactRef, ...]]:
        provider_spec = ProviderSpec.model_validate(provider_spec)
        records_tuple = tuple(NormalizedRecord.model_validate(record) for record in records)
        if not records_tuple:
            raise ValueError("cannot publish an empty PIT dataset")
        if not provider_spec.license.derived_storage_allowed:
            raise IntegrityViolation(
                "PROVIDER_LICENSE_BLOCKED",
                "provider license forbids derived dataset storage",
                rule_id="RULE-032",
                context={"license_id": provider_spec.license.license_id},
            )
        capability_by_domain = {item.data_domain: item for item in provider_spec.capabilities}
        for record in records_tuple:
            capability = capability_by_domain.get(record.record_type)
            if capability is None or capability.pit_grade in {PITGrade.CURRENT_ONLY, PITGrade.UNVERIFIED}:
                raise IntegrityViolation(
                    "PROVIDER_PIT_CAPABILITY_BLOCKED",
                    "formal PIT publication requires native or reconstructed PIT capability",
                    rule_id="RULE-002",
                    context={"data_domain": record.record_type.value},
                )
            if record.source != provider_spec.provider_id:
                raise IntegrityViolation(
                    "PROVIDER_LINEAGE_MISMATCH",
                    "record source differs from the frozen ProviderSpec",
                    rule_id="RULE-009",
                    context={"source": record.source, "provider": provider_spec.provider_id},
                )
        if any(snapshot.provider_id != provider_spec.provider_id for snapshot in raw_snapshots):
            raise IntegrityViolation(
                "PROVIDER_LINEAGE_MISMATCH",
                "raw snapshot belongs to a different ProviderSpec",
                rule_id="RULE-009",
            )
        expected_provider_artifact = FrozenManifest.build("provider_spec", provider_spec).manifest_hash
        expected_license_artifact = FrozenManifest.build("provider_license", provider_spec.license).manifest_hash
        if any(
            snapshot.provider_spec_artifact_id != expected_provider_artifact
            or snapshot.license_spec_artifact_id != expected_license_artifact
            for snapshot in raw_snapshots
        ):
            raise IntegrityViolation(
                "PROVIDER_MANIFEST_LINEAGE_MISMATCH",
                "raw snapshot is not pinned to the supplied ProviderSpec and license",
                rule_id="RULE-009",
            )
        known_snapshots = {snapshot.snapshot_id for snapshot in raw_snapshots}
        findings = list(audit_normalized_records(records_tuple, known_raw_snapshots=known_snapshots))
        has_universe = any(record.record_type.value == "universe" for record in records_tuple)
        if has_universe and expected_historical_members is None:
            raise IntegrityViolation(
                "SURVIVORSHIP_AUDIT_NOT_CONFIGURED",
                "universe publication requires an independently derived historical member set",
                rule_id="RULE-007",
            )
        if expected_historical_members is not None:
            findings.extend(audit_survivorship(records_tuple, set(expected_historical_members)))
        blockers = [finding for finding in findings if finding.severity is FindingSeverity.BLOCKER]
        if blockers:
            first = blockers[0]
            raise IntegrityViolation(
                first.code,
                f"dataset release blocked at {first.location}: {first.message}",
                rule_id=first.rule_id,
                context={"finding_count": len(blockers), **first.evidence},
            )
        grouped: defaultdict[str, list[NormalizedRecord]] = defaultdict(list)
        for record in records_tuple:
            grouped[record.record_type.value].append(record)
        partition_refs: list[ArtifactRef] = []
        partition_specs: list[PartitionSpec] = []
        for domain in sorted(grouped):
            domain_records = tuple(sorted(grouped[domain], key=lambda item: (item.logical_key, item.revision_id)))
            payload = self._parquet_bytes(domain_records)
            reference = self._artifacts.put_bytes(
                payload,
                media_type="application/vnd.apache.parquet",
                metadata={
                    "dataset_version": dataset_version,
                    "partition": domain,
                    "row_count": len(domain_records),
                },
            )
            partition_refs.append(reference)
            partition_specs.append(
                PartitionSpec(name=domain, row_count=len(domain_records), content_hash=reference.artifact_id)
            )
        spec = DatasetSpec(
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            schema_version=schema_version,
            provider=provider_spec.provider_id,
            coverage_start=min(record.event_time.date() for record in records_tuple),
            coverage_end=max(record.event_time.date() for record in records_tuple),
            raw_snapshot_hashes=tuple(sorted(known_snapshots)),
            transformation_commit=transformation_commit,
            pit_rule_version=pit_rule_version,
            adjustment_rule_version=adjustment_rule_version,
            partitions=tuple(partition_specs),
            audit_status=AuditStatus.PASSED,
            published_at=published_at,
            publisher=publisher,
        )
        spec_manifest = FrozenManifest.build(
            "dataset_spec",
            {
                "audit_findings": [
                    {
                        "audit_id": finding.audit_id,
                        "code": finding.code,
                        "evidence": finding.evidence,
                        "location": finding.location,
                        "message": finding.message,
                        "rule_id": finding.rule_id,
                        "severity": finding.severity,
                    }
                    for finding in findings
                ],
                "dataset_spec": spec,
                "license_manifest_hash": expected_license_artifact,
                "provider_manifest_hash": expected_provider_artifact,
            },
        )
        spec_ref = self._artifacts.put_bytes(
            spec_manifest.to_bytes(),
            media_type="application/vnd.alpha-research-os.dataset-manifest+json",
            metadata={"dataset_id": dataset_id, "dataset_version": dataset_version},
        )
        self._artifacts.bind_identity("datasets", dataset_version, spec_ref)
        release = DatasetRelease(
            dataset_manifest_hash=spec_manifest.manifest_hash,
            dataset_spec_artifact_id=spec_ref.artifact_id,
            partition_artifact_ids=tuple(reference.artifact_id for reference in partition_refs),
            record_count=len(records_tuple),
        )
        return spec, release, tuple(partition_refs)

    def verify_partition(self, reference: ArtifactRef) -> int:
        payload = self._artifacts.read_bytes(reference)
        table = pq.read_table(pa.BufferReader(payload))
        return table.num_rows
