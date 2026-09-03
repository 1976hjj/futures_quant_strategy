"""Content-addressed, append-only artifact storage."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import FrozenManifest, canonical_json_bytes, sha256_bytes
from .errors import ArtifactConflictError, IntegrityViolation
from .specs import ExperimentSpec


def _digest_part(identifier: str) -> str:
    algorithm, separator, digest = identifier.partition(":")
    if algorithm != "sha256" or separator != ":" or len(digest) != 64:
        raise ValueError(f"invalid SHA-256 identifier: {identifier}")
    return digest


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    manifest_id: str
    byte_size: int
    media_type: str


class ArtifactStore:
    """A local store that never replaces an existing content address."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._objects = self._root / "objects" / "sha256"
        self._manifests = self._root / "manifests" / "sha256"
        self._experiment_identities = self._root / "identities" / "experiments"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._manifests.mkdir(parents=True, exist_ok=True)
        self._experiment_identities.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write_once(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ArtifactConflictError(
                    "ARTIFACT_IMMUTABILITY",
                    "an existing content address contains different bytes",
                    rule_id="RULE-027",
                    context={"storage_key": path.name},
                ) from None

    def _object_path(self, artifact_id: str) -> Path:
        digest = _digest_part(artifact_id)
        return self._objects / digest[:2] / digest

    def _manifest_path(self, manifest_id: str) -> Path:
        digest = _digest_part(manifest_id)
        return self._manifests / digest[:2] / f"{digest}.json"

    def put_bytes(
        self,
        payload: bytes,
        *,
        media_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        if not media_type.strip():
            raise ValueError("media_type must not be blank")
        artifact_id = sha256_bytes(payload)
        self._write_once(self._object_path(artifact_id), payload)
        manifest = FrozenManifest.build(
            "artifact",
            {
                "artifact_id": artifact_id,
                "byte_size": len(payload),
                "media_type": media_type,
                "metadata": metadata or {},
            },
        )
        manifest_bytes = manifest.to_bytes()
        manifest_id = sha256_bytes(manifest_bytes)
        self._write_once(self._manifest_path(manifest_id), manifest_bytes)
        return ArtifactRef(
            artifact_id=artifact_id,
            manifest_id=manifest_id,
            byte_size=len(payload),
            media_type=media_type,
        )

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        payload = self._object_path(reference.artifact_id).read_bytes()
        if sha256_bytes(payload) != reference.artifact_id or len(payload) != reference.byte_size:
            raise IntegrityViolation(
                "ARTIFACT_HASH_MISMATCH",
                "stored artifact does not match its immutable reference",
                rule_id="RULE-027",
                context={"artifact_id": reference.artifact_id},
            )
        return payload

    def read_manifest(self, reference: ArtifactRef) -> dict[str, Any]:
        payload = self._manifest_path(reference.manifest_id).read_bytes()
        if sha256_bytes(payload) != reference.manifest_id:
            raise IntegrityViolation(
                "MANIFEST_HASH_MISMATCH",
                "stored manifest does not match its reference",
                rule_id="RULE-027",
                context={"manifest_id": reference.manifest_id},
            )
        manifest = json.loads(payload)
        expected = canonical_json_bytes(manifest)
        if expected != payload:
            raise IntegrityViolation(
                "MANIFEST_NOT_CANONICAL",
                "stored manifest is not canonical JSON",
                rule_id="RULE-027",
            )
        declared = manifest["payload"]
        if (
            declared["artifact_id"] != reference.artifact_id
            or declared["byte_size"] != reference.byte_size
            or declared["media_type"] != reference.media_type
        ):
            raise IntegrityViolation(
                "ARTIFACT_REFERENCE_MISMATCH",
                "artifact reference disagrees with its immutable manifest",
                rule_id="RULE-027",
                context={"manifest_id": reference.manifest_id},
            )
        return manifest

    def put_experiment_spec(self, spec: ExperimentSpec) -> ArtifactRef:
        """Freeze a spec and bind its experiment ID to exactly one manifest."""

        validated = ExperimentSpec.model_validate(spec)
        spec_manifest = FrozenManifest.build("experiment_spec", validated)
        reference = self.put_bytes(
            spec_manifest.to_bytes(),
            media_type="application/vnd.alpha-research-os.experiment-manifest+json",
            metadata={
                "experiment_id": validated.experiment_id,
                "spec_manifest_hash": spec_manifest.manifest_hash,
            },
        )
        identity = canonical_json_bytes(
            {
                "artifact_id": reference.artifact_id,
                "experiment_id": validated.experiment_id,
                "manifest_id": reference.manifest_id,
                "spec_manifest_hash": spec_manifest.manifest_hash,
            }
        )
        self._write_once(self._experiment_identities / f"{validated.experiment_id}.json", identity)
        return reference

    def bind_identity(self, namespace: str, logical_id: str, reference: ArtifactRef) -> None:
        """Bind a safe logical identity to one immutable artifact reference."""

        safe = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
        if not safe.fullmatch(namespace) or not safe.fullmatch(logical_id):
            raise ValueError("identity namespace and logical_id must be safe path components")
        payload = canonical_json_bytes(
            {
                "artifact_id": reference.artifact_id,
                "logical_id": logical_id,
                "manifest_id": reference.manifest_id,
                "namespace": namespace,
            }
        )
        self._write_once(self._root / "identities" / namespace / f"{logical_id}.json", payload)
