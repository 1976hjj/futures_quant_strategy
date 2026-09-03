"""Immutable capture and verification of Provider responses."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Literal

from alpha_research_os.kernel.artifacts import ArtifactRef, ArtifactStore
from alpha_research_os.kernel.canonical import FrozenManifest, sha256_bytes
from alpha_research_os.kernel.errors import IntegrityViolation

from .contracts import ProviderResponse, ProviderSpec, RawSnapshotRef


@dataclass(frozen=True, slots=True)
class CapturedSnapshot:
    reference: RawSnapshotRef
    payload_reference: ArtifactRef
    manifest_reference: ArtifactRef


class RawSnapshotStore:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    def capture(
        self,
        provider: ProviderSpec,
        response: ProviderResponse,
        *,
        storage_encoding: Literal["identity", "gzip"] = "identity",
    ) -> CapturedSnapshot:
        provider = ProviderSpec.model_validate(provider)
        response = ProviderResponse.model_validate(response)
        capabilities = {item.data_domain: item for item in provider.capabilities}
        if response.request.data_domain not in capabilities:
            raise IntegrityViolation(
                "PROVIDER_CAPABILITY_MISMATCH",
                "response domain is absent from the frozen ProviderSpec",
                rule_id="RULE-032",
                context={"domain": response.request.data_domain.value},
            )
        capability = capabilities[response.request.data_domain]
        undeclared_fields = set(response.request.fields) - set(capability.fields)
        if undeclared_fields:
            raise IntegrityViolation(
                "PROVIDER_CAPABILITY_MISMATCH",
                "request uses fields absent from the frozen ProviderSpec",
                rule_id="RULE-032",
                context={"fields": sorted(undeclared_fields)},
            )
        if not provider.license.retrieval_allowed or not provider.license.local_raw_storage_allowed:
            raise IntegrityViolation(
                "PROVIDER_LICENSE_BLOCKED",
                "provider license does not permit retrieval and immutable raw storage",
                rule_id="RULE-032",
                context={"license_id": provider.license.license_id},
            )
        provider_manifest = FrozenManifest.build("provider_spec", provider)
        provider_reference = self._artifacts.put_bytes(
            provider_manifest.to_bytes(),
            media_type="application/vnd.alpha-research-os.provider-manifest+json",
            metadata={"provider_id": provider.provider_id},
        )
        license_manifest = FrozenManifest.build("provider_license", provider.license)
        license_reference = self._artifacts.put_bytes(
            license_manifest.to_bytes(),
            media_type="application/vnd.alpha-research-os.license-manifest+json",
            metadata={"license_id": provider.license.license_id},
        )
        stored_payload = (
            response.payload if storage_encoding == "identity" else gzip.compress(response.payload, mtime=0)
        )
        payload_reference = self._artifacts.put_bytes(
            stored_payload,
            media_type=response.media_type if storage_encoding == "identity" else "application/gzip",
            metadata={
                "content_encoding": storage_encoding,
                "original_byte_size": len(response.payload),
                "original_media_type": response.media_type,
                "original_payload_hash": sha256_bytes(response.payload),
                "provider_id": provider.provider_id,
                "request_id": response.request.request_id,
                "raw": True,
            },
        )
        snapshot_manifest = FrozenManifest.build(
            "raw_snapshot",
            {
                "adapter_version": provider.adapter_version,
                "capability_manifest_hash": provider_manifest.manifest_hash,
                "provider_spec_artifact_id": provider_reference.artifact_id,
                "data_domain": response.request.data_domain,
                "license_manifest_hash": license_manifest.manifest_hash,
                "license_spec_artifact_id": license_reference.artifact_id,
                "payload_artifact_id": payload_reference.artifact_id,
                "payload_encoding": storage_encoding,
                "payload_manifest_id": payload_reference.manifest_id,
                "uncompressed_byte_size": len(response.payload),
                "uncompressed_payload_hash": sha256_bytes(response.payload),
                "provider_id": provider.provider_id,
                "provider_request_id": response.provider_request_id,
                "provider_version": provider.provider_version,
                "request": response.request,
                "retrieved_at": response.retrieved_at,
            },
        )
        manifest_reference = self._artifacts.put_bytes(
            snapshot_manifest.to_bytes(),
            media_type="application/vnd.alpha-research-os.raw-snapshot-manifest+json",
            metadata={"provider_id": provider.provider_id, "request_id": response.request.request_id},
        )
        reference = RawSnapshotRef(
            snapshot_id=snapshot_manifest.manifest_hash,
            payload_artifact_id=payload_reference.artifact_id,
            payload_manifest_id=payload_reference.manifest_id,
            payload_encoding=storage_encoding,
            uncompressed_payload_hash=sha256_bytes(response.payload),
            uncompressed_byte_size=len(response.payload),
            snapshot_manifest_artifact_id=manifest_reference.artifact_id,
            provider_spec_artifact_id=provider_reference.artifact_id,
            license_spec_artifact_id=license_reference.artifact_id,
            provider_id=provider.provider_id,
            request_id=response.request.request_id,
            retrieved_at=response.retrieved_at,
        )
        return CapturedSnapshot(reference, payload_reference, manifest_reference)

    def read_payload(self, snapshot: CapturedSnapshot) -> bytes:
        manifest_bytes = self._artifacts.read_bytes(snapshot.manifest_reference)
        manifest = json.loads(manifest_bytes)
        if snapshot.manifest_reference.artifact_id != snapshot.reference.snapshot_id:
            raise IntegrityViolation(
                "RAW_SNAPSHOT_HASH_MISMATCH",
                "raw snapshot manifest no longer matches its identity",
                rule_id="RULE-027",
            )
        payload = manifest["payload"]
        if payload["payload_artifact_id"] != snapshot.payload_reference.artifact_id:
            raise IntegrityViolation(
                "RAW_SNAPSHOT_LINEAGE_MISMATCH",
                "snapshot manifest points to a different raw payload",
                rule_id="RULE-009",
            )
        stored_payload = self._artifacts.read_bytes(snapshot.payload_reference)
        if snapshot.reference.payload_encoding == "identity":
            payload = stored_payload
        elif snapshot.reference.payload_encoding == "gzip":
            payload = gzip.decompress(stored_payload)
        else:
            raise IntegrityViolation(
                "RAW_SNAPSHOT_ENCODING_UNSUPPORTED",
                "raw snapshot uses an unsupported content encoding",
                rule_id="RULE-027",
                context={"payload_encoding": snapshot.reference.payload_encoding},
            )
        if (
            len(payload) != snapshot.reference.uncompressed_byte_size
            or sha256_bytes(payload) != snapshot.reference.uncompressed_payload_hash
        ):
            raise IntegrityViolation(
                "RAW_SNAPSHOT_DECOMPRESSION_MISMATCH",
                "decoded raw payload does not match its original size and hash",
                rule_id="RULE-027",
            )
        return payload
