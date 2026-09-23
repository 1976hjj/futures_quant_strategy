"""Create one immutable RAW release from individually published factor releases."""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path

import duckdb

from alpha_research_os.factors.assets import (
    DatasetLineage, FactorAssetRequest, FactorReleaseManifest,
)
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash
from scripts.publish_factor_release import _quality, _register, _sha256_file, _sql_path


class _EmptyCatalog:
    def list(self) -> tuple[()]:
        return ()


def _save(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def publish_cohort(project_root: Path, release_ids: list[str], start: str, end: str) -> str:
    if len(release_ids) < 2 or len(release_ids) != len(set(release_ids)):
        raise ValueError("M4.5 cohort requires at least two distinct releases")
    store = project_root / "data/factor_store"
    parents: list[tuple[FactorReleaseManifest, Path]] = []
    for release_id in release_ids:
        digest = release_id.removeprefix("sha256:")
        path = store / "releases" / digest / "manifest.json"
        manifest = FactorReleaseManifest.model_validate_json(path.read_bytes())
        if manifest.release_id != release_id or manifest.factor_count != 1:
            raise ValueError("cohort inputs must be individual RAW factor releases")
        if manifest.request.start > date.fromisoformat(start) or manifest.request.end < date.fromisoformat(end):
            raise ValueError(f"factor coverage does not contain the shared window: {release_id}")
        parquet = store / manifest.parquet_relative_path
        if _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError(f"factor asset hash mismatch: {release_id}")
        parents.append((manifest, parquet))
    source = parents[0][0].request
    for manifest, _ in parents[1:]:
        item = manifest.request
        if (item.universe_id, item.universe_version, item.signal_clock_version) != (
            source.universe_id, source.universe_version, source.signal_clock_version
        ):
            raise ValueError("selected factors do not share the same universe and signal clock")
    references = tuple(sorted((manifest.request.factors[0] for manifest, _ in parents),
                              key=lambda item: (item.factor_id, item.factor_version)))
    if len({(ref.factor_id, ref.factor_version) for ref in references}) != len(references):
        raise ValueError("cohort contains the same factor twice")
    lineage: dict[str, set[str]] = {}
    for manifest, _ in parents:
        for item in manifest.request.dataset_lineage:
            lineage.setdefault(item.manifest_table, set()).update(item.checkpoint_hashes)
    lineage["metadata.factor_release_manifest"] = {manifest.parquet_hash for manifest, _ in parents}
    request = FactorAssetRequest(
        engine_version="1.0.0", factors=references,
        dataset_lineage=tuple(DatasetLineage(manifest_table=table, checkpoint_hashes=tuple(sorted(hashes)))
                              for table, hashes in sorted(lineage.items())),
        universe_id=source.universe_id, universe_version=source.universe_version,
        start=date.fromisoformat(start), end=date.fromisoformat(end),
        signal_clock_version=source.signal_clock_version,
    )
    release_id = request.computation_key
    directory = store / "releases" / release_id.removeprefix("sha256:")
    directory.mkdir(parents=True, exist_ok=True)
    parquet = directory / "raw_factor_values.parquet"
    quality_path = directory / "quality_summary.json"
    manifest_path = directory / "manifest.json"
    if manifest_path.exists() and parquet.exists() and quality_path.exists():
        manifest = FactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.request != request or _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError("cached cohort release failed immutable verification")
        quality = json.loads(quality_path.read_bytes())
        _register(project_root / "data/warehouse/alpha_research.duckdb", store, manifest,
                  content_hash(manifest), _EmptyCatalog(), quality["factors"], "initial")
        return release_id
    temporary = directory / f".cohort.{uuid.uuid4().hex}.parquet"
    selections = [
        "SELECT '" + release_id + "' AS release_id, session, instrument_id, factor_id, "
        "factor_version, variant, value, available_at, implementation_hash "
        f"FROM read_parquet('{_sql_path(path)}') "
        f"WHERE session BETWEEN DATE '{start}' AND DATE '{end}'"
        for _, path in parents
    ]
    with duckdb.connect() as connection:
        connection.execute(
            f"COPY (SELECT * FROM ({' UNION ALL '.join(selections)}) "
            "ORDER BY session, instrument_id, factor_id, factor_version) "
            f"TO '{_sql_path(temporary)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    quality, details = _quality(temporary, len(parents))
    os.replace(temporary, parquet)
    _save(quality_path, canonical_json_bytes(quality) + b"\n")
    manifest = FactorReleaseManifest(
        release_id=release_id, request=request, created_at=datetime.now().astimezone(),
        parquet_relative_path=parquet.relative_to(store).as_posix(),
        parquet_hash=_sha256_file(parquet), row_count=quality["row_count"],
        session_count=quality["session_count"], instrument_count=quality["instrument_count"],
        factor_count=quality["factor_count"], quality_status="PASS",
        quality_summary_hash=_sha256_file(quality_path),
    )
    _save(manifest_path, canonical_json_bytes(manifest) + b"\n")
    _save(directory / "cohort_sources.json", canonical_json_bytes({"source_release_ids": release_ids}) + b"\n")
    _register(project_root / "data/warehouse/alpha_research.duckdb", store, manifest,
              content_hash(manifest), _EmptyCatalog(), details, "initial")
    return release_id
