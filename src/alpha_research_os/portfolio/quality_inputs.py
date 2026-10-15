"""Build isolated strategy inputs from current local sources, across the full window."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import duckdb

CALCULATION_MODE = "FULL_LOCAL_REBUILD_V3"


def source_signature(root: Path) -> str:
    """Conservative change detector, not a certification of source correctness."""
    files = {}
    for name in ("alpha_research.duckdb", "bank_token.duckdb"):
        for suffix in ("", ".wal"):
            path = root / "data/warehouse" / (name + suffix)
            if path.exists():
                stat = path.stat()
                files[str(path.relative_to(root))] = [stat.st_size, stat.st_mtime_ns]
    names = ["config/bank_factors.json", "config/bank_timing_data.json",
             "scripts/run_quality_strategy.py", "scripts/publish_jqdata_factor.py",
             "scripts/bank_factor_inputs.py", "src/alpha_research_os/factors/bank.py",
             "src/alpha_research_os/portfolio/quality_inputs.py",
             "src/alpha_research_os/portfolio/strategy_backtest.py",
             "src/alpha_research_os/portfolio/shadow_health.py"]
    config = root / "config/bank_factors.json"
    if config.exists():
        settings = json.loads(config.read_bytes())
        names += settings.get("original_fact_files", []) + settings.get("original_growth_files", [])
    for name in sorted(set(names)):
        path = root / name
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return "sha256:" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def build_inputs(root, request, folder, as_of, progress):
    from alpha_research_os.portfolio.quality_strategies import atomic_json, fingerprint, read_json
    ids = list(dict.fromkeys(r.factor_id for r in (*request.score_rules, *request.filter_rules)))
    bank_ids = [fid for fid in ids if fid.startswith("bank-")]
    if bank_ids:
        released = date.fromisoformat(read_json(root / "config/bank_timing_data.json")["released_feature_end"])
        if as_of > released:
            raise ValueError(f"银行财务输入范围仅开放至 {released}；不能生成 {as_of} 信号")
    from alpha_research_os.kernel.holdout import ExposureLedger
    ExposureLedger(root / "data/metadata/tech_research_exposures.ndjson").append(
        actor="quality-strategy-page", vintage=f"full-current-inputs-{request.start}-{as_of}",
        purpose=f"Full local factor rebuild and same-rule historical replay; {folder.name}",
        capability_id="owner-quality-strategy-signal-calculation")
    folder.mkdir(parents=True, exist_ok=True)
    signature = source_signature(root)
    bank_frames, bank_live, fields = None, None, {}
    if bank_ids:
        import pandas as pd
        from scripts.bank_factor_inputs import source_inputs, build_features, daily_pb_inputs
        from alpha_research_os.factors.bank import bank_factor_catalog
        cutoff = pd.Timestamp(as_of, tz="Asia/Shanghai") + pd.Timedelta(hours=23, minutes=59, seconds=59)
        cutoff = min(cutoff, pd.Timestamp.now(tz="Asia/Shanghai"))
        facts, events, market, membership, capitals, coverage = source_inputs(root, request.start, as_of, information_cutoff=cutoff)
        progress({"phase": "全量重建银行历史与收盘后输入", "progress": 2})
        bank_frames = build_features(facts, events, market, capitals, coverage, post_close=True)
        bank_live = build_features(facts, events, market[market.session == as_of], capitals, coverage, information_cutoff=cutoff)
        fields = {item.factor_id: item.field for item in bank_factor_catalog()}
        if any(fields[fid] == "pb_daily" for fid in bank_ids):
            pb = daily_pb_inputs(root, request.start, as_of).drop(columns="close")
            bank_frames = bank_frames.merge(pb, on=["session", "instrument_id"], how="left", validate="one_to_one")
            bank_live = bank_live.merge(pb, on=["session", "instrument_id"], how="left", validate="one_to_one")
        for name, frame in {"bank-facts": facts, "bank-events": events, "bank-membership": membership,
                            "bank-capitals": capitals, "bank-features": bank_frames, "bank-live-features": bank_live}.items():
            frame.to_parquet(folder / f"{name}.parquet", index=False)
    from alpha_research_os.factors.assets import DatasetLineage, FactorAssetRequest
    from alpha_research_os.factors.jqdata import jqdata_catalog
    from alpha_research_os.portfolio.strategy_backtest import _factor_release_candidates
    from scripts.publish_jqdata_factor import _local_materialization_sql
    from scripts.publish_factor_release import _lineage
    catalog = {item.factor_id: item for item in jqdata_catalog()}
    resolved = []
    with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as connection:
        connection.execute("SET threads=4")
        lineage = list(_lineage(connection)) + [DatasetLineage(manifest_table="quality-current-source", checkpoint_hashes=(signature,))]
        for index, fid in enumerate(ids):
            progress({"phase": f"全量重算历史因子 {fid}", "progress": round(2 + 8 * index / len(ids))})
            target = folder / f"factor-{index}.parquet"
            rid = "sha256:" + hashlib.sha256(f"{signature}|{fid}|{request.start}|{as_of}".encode()).hexdigest()
            binding = dict(factor_id=fid, start=request.start, end=as_of, created_at="full-current-local-rebuild", parquet=target,
                           calculation_mode=CALCULATION_MODE, source_signature=signature)
            if fid in bank_ids:
                field = fields[fid]
                for label, frame in (("historical", bank_frames), ("live", bank_live)):
                    if field not in frame:
                        raise ValueError(f"银行因子暂不支持本地全量重算：{fid}")
                    values = frame[["session", "instrument_id", "available_at", field]].rename(columns={field: "value"}).copy()
                    values["factor_id"], values["variant"], values["release_id"] = fid, "RAW", rid
                    path = target if label == "historical" else folder / f"factor-{index}-live.parquet"
                    values.to_parquet(path, index=False)
                    if label == "live":
                        binding.update(live_parquet=path, live_release_id="sha256:" + fingerprint(path),
                                       latest_live_valid_count=int(values.value.notna().sum()), live_information_cutoff=cutoff.isoformat())
            else:
                if fid not in catalog:
                    raise ValueError(f"因子暂不支持本地全量重算：{fid}；不会静默复用旧因子")
                candidates = [r for r in _factor_release_candidates(root, fid) if r["start"] <= request.start]
                if not candidates:
                    raise ValueError(f"缺少因子公式定义版本：{fid}，请先在因子计算页准备")
                published = max(candidates, key=lambda r: (r["end"], r["created_at"], r["release_id"]))
                manifest_path = root / "data/factor_store/releases" / published["release_id"].removeprefix("sha256:") / "manifest.json"
                manifest = read_json(manifest_path)
                spec = FactorAssetRequest.model_validate({**manifest["request"], "engine_version": CALCULATION_MODE,
                    "start": str(request.start), "end": str(as_of), "dataset_lineage": [r.model_dump(mode="json") for r in lineage]})
                sql = _local_materialization_sql(catalog[fid], spec, target, date(request.start.year - 2, 1, 1))
                sql = sql.replace('WHERE coalesce(f_ann_date,ann_date) IS NOT NULL AND end_date IS NOT NULL',
                    f"WHERE coalesce(try_strptime(f_ann_date,'%Y%m%d')::DATE,try_strptime(ann_date,'%Y%m%d')::DATE)<=DATE '{as_of}' AND end_date IS NOT NULL")
                sql = sql.replace('WHERE coalesce(ann_date,end_date) IS NOT NULL', f"WHERE try_strptime(ann_date,'%Y%m%d')::DATE<=DATE '{as_of}'")
                (folder / f"factor-{index}.sql").write_text(sql, encoding="utf-8")
                connection.execute(sql)
                rid = spec.computation_key
                binding.update(formula_definition_manifest_sha256=fingerprint(manifest_path),
                               formula_source_sha256=fingerprint(root / "scripts/publish_jqdata_factor.py"))
            count, unique, early = connection.execute("SELECT count(*),count(DISTINCT(session,instrument_id)),count(*) FILTER(WHERE available_at>(session::TIMESTAMP AT TIME ZONE 'Asia/Shanghai')+INTERVAL 23 HOURS+INTERVAL 59 MINUTES+INTERVAL 59 SECONDS) FROM read_parquet(?)", [str(target)]).fetchone()
            if count != unique or early:
                raise ValueError(f"因子存在重复键或超出历史决策时钟：{fid}")
            last, n = connection.execute("SELECT max(session), count(value) FILTER(WHERE session=?) FROM read_parquet(?)", [as_of, str(target)]).fetchone()
            valid = binding.get("latest_live_valid_count", n)
            if last != as_of or valid < request.target_count:
                raise ValueError(f"{fid} 在 {as_of} 输入不足（有效值 {valid}），不能生成最新信号")
            binding.update(release_id=rid, parquet_sha256=fingerprint(target))
            if fid in bank_ids:
                binding["latest_historical_valid_count"] = n
            manifest_path = folder / f"factor-{index}.manifest.json"
            atomic_json(manifest_path, {k: str(v) for k, v in binding.items() if k != "parquet"})
            binding["source_manifest_sha256"] = fingerprint(manifest_path)
            resolved.append(binding)
    if source_signature(root) != signature:
        raise RuntimeError("源数据或公式在重算期间发生变化，本次输入不发布；请重新计算")
    return resolved
