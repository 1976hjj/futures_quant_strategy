"""Inventory and run resumable updates of the local strategy data sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from alpha_research_os.kernel.canonical import canonical_json_bytes
from scripts.build_m2e_core_warehouse import _projection as m2e_projection
from scripts.sync_eastmoney_banks import bank_inventory

GROUPS = {
    "reference": ("证券名册与 PIT 状态", "trade_cal、stock_basic、namechange、stock_st、suspend_d", True),
    "market": ("日行情与估值", "daily、adj_factor、daily_basic；包含银行价格、PB与股息率", True),
    "financial": ("财务披露", "income、balance、cashflow、indicator；包含银行盈利、每股净资产、贷款与存款", True),
    "limits": ("涨跌停价格", "stk_limit", True),
    "corporate": ("分红与送转", "按公告日补充 dividend；包含银行中期、年度分红与实施状态", True),
    "benchmark": ("沪深 300 基准", "基准收盘价", False),
    "bank_industry": ("银行基本面、财务与估值", "42家银行；Token主源，免费补缺；独立增量同步", False),
    "bank_free": ("银行资产质量与资本", "东方财富 · 42家银行历史财报、资产质量、拨备、资本与净息差", False),
}
LEGACY_GROUPS = tuple(group for group in GROUPS if group not in ("bank_free", "bank_industry"))
DEFAULT_FACTORS = (
    "jqdata-natural-log-of-market-cap",
    "jqdata-net-operating-cash-flow-coverage",
)
ARCHIVES = {
    "reference": "tushare_reference_archive",
    "market": "tushare_archive",
    "financial": "tushare_financial_archive",
    "limits": "tushare_m2e_archive",
    "corporate": "tushare_corporate_action_archive",
}
STAGE_ORDER = tuple(GROUPS)
DEPENDENCIES = {
    "limits": ("reference",),
    "corporate": ("reference", "market"),
    "factors": ("reference", "market", "financial", "limits", "corporate"),
}

MAX_TRANSIENT_ATTEMPTS = 3
TRANSIENT_ERRORS = (
    "RemoteDisconnected",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "TimeoutError",
    "timed out",
    "Temporary failure",
    "HTTP Error 429",
    "HTTP Error 500",
    "HTTP Error 502",
    "HTTP Error 503",
    "HTTP Error 504",
    "source unavailable after",
    "code=429",
    "code=502",
    "code=503",
    "code=504",
)


def _transient_failure(log_path: Path, start_offset: int) -> bool:
    with log_path.open("rb") as log:
        log.seek(0, os.SEEK_END)
        log.seek(max(start_offset, log.tell() - 16_000))
        tail = log.read().decode("utf-8", errors="replace")
    return any(marker in tail for marker in TRANSIENT_ERRORS)


def latest_complete_day(now: datetime | None = None) -> date:
    current = now or datetime.now(timezone(timedelta(hours=8)))
    return current.date() if current.hour >= 18 else current.date() - timedelta(days=1)


class DataUpdateRequest(BaseModel):
    source_id: Literal["tushare", "eastmoney", "industry"] | None = None
    end: date = Field(default_factory=latest_complete_day)
    groups: list[str] = Field(default_factory=lambda: list(LEGACY_GROUPS))
    factor_ids: list[str] = Field(default_factory=list)
    workers: int = Field(default=2, ge=1, le=8)
    min_free_gb: float = Field(default=10, ge=1, le=500)
    sleep_ms: int = Field(default=50, ge=0, le=5000)
    refresh_recent_periods: int = Field(default=4, ge=0, le=4)

    @model_validator(mode="after")
    def validate_selection(self) -> DataUpdateRequest:
        self.source_id = self.source_id or ("eastmoney" if self.groups == ["bank_free"] else "tushare")
        if "factors" in self.groups or self.factor_ids:
            raise ValueError("请在因子计算页面计算因子，数据管理不更新因子")
        allowed = (
            {"bank_industry"}
            if self.source_id == "industry"
            else {"bank_free"}
            if self.source_id == "eastmoney"
            else set(LEGACY_GROUPS)
        )
        if set(self.groups) - allowed:
            raise ValueError("select data groups from one source; free and token-based updates run separately")
        if self.end > (date.today() if self.source_id == "eastmoney" else latest_complete_day()):
            raise ValueError(
                "target date is not yet complete; today's market data should be updated after 18:00 China time"
            )
        if not self.groups or len(set(self.groups)) != len(self.groups) or set(self.groups) - GROUPS.keys():
            raise ValueError("select one or more known data groups without duplicates")
        if len(set(self.factor_ids)) != len(self.factor_ids) or len(self.factor_ids) > 20:
            raise ValueError("factor selection is duplicated or too large")
        if "factors" in self.groups and not self.factor_ids:
            raise ValueError("select at least one factor when updating factors")
        return self


def _checkpoint(root: Path, group: str) -> dict[str, Any]:
    folder = ARCHIVES[group]
    path = root / "data" / folder / "checkpoint.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _archive_endpoint(root: Path, group: str) -> str:
    endpoint = _checkpoint(root, group).get("api_base_url")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ValueError(f"{group} archive has no valid HTTPS source endpoint")
    return endpoint


def tushare_token(root: Path) -> str | None:
    value = os.environ.get("TUSHARE_TOKEN", "").strip()
    if value:
        return value
    credential = root / "secrets" / "tushare.env"
    if not credential.exists():
        return None
    match = re.fullmatch(r"TUSHARE_TOKEN=([0-9a-f]{40,128})\s*", credential.read_text(encoding="utf-8"))
    if not match:
        raise ValueError("secrets/tushare.env has an invalid TUSHARE_TOKEN entry")
    return match.group(1)


def _entry_end(state: dict[str, Any], group: str) -> str | None:
    if group == "market":
        completed = state.get("completed", {})
        dates = [max(completed.get(api, {}), default="") for api in ("daily", "adj_factor", "daily_basic")]
        return min(dates) if all(dates) else None
    coverage = state.get("coverage") or {}
    return coverage.get("end")


def _published(root: Path, group: str, state: dict[str, Any]) -> bool:
    names = {
        "reference": ("reference_build_summary.json", "checkpoint_hash"),
        "market": ("build_summary.json", "checkpoint_hash"),
        "financial": ("financial_build_summary.json", "checkpoint_hash"),
        "corporate": ("corporate_action_build_summary.json", "checkpoint_hash"),
        "limits": ("m2e_core_build_summary.json", "core_checkpoint_hash"),
    }
    summary_name, hash_field = names[group]
    summary_path = root / "data" / "warehouse" / summary_name
    if not summary_path.exists() or not state:
        return False
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if group == "limits":
        payload = json.dumps(m2e_projection(state), sort_keys=True, separators=(",", ":")).encode()
    else:
        payload = (root / "data" / ARCHIVES[group] / "checkpoint.json").read_bytes()
    return summary.get(hash_field) == f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _datasets(state: dict[str, Any], group: str) -> list[dict[str, Any]]:
    coverage = state.get("coverage") or {}
    rows = []
    for api, partitions in sorted(state.get("completed", {}).items()):
        keys = list(partitions)
        if group in ("market", "reference") and api in ("daily", "adj_factor", "daily_basic", "stock_st", "suspend_d"):
            dates = [key for key in keys if len(key) == 8 and key.isdigit()]
        elif group == "limits" and api == "stk_limit":
            dates = [key[:8] for key in keys if len(key) >= 8 and key[:8].isdigit()]
        elif group == "financial":
            dates = [key[:8] for key in keys if len(key) >= 8 and key[:8].isdigit()]
        elif group == "reference" and api == "stock_basic":
            dates = [key[:8] for key in keys if len(key) >= 8 and key[:8].isdigit()]
        else:
            dates = []
        if dates:
            first, last = min(dates), max(dates)
            start = f"{first[:4]}-{first[4:6]}-{first[6:]}"
            end = f"{last[:4]}-{last[4:6]}-{last[6:]}"
        else:
            start, end = coverage.get("start"), coverage.get("end")
        rows.append({"id": api, "start": start, "end": end, "partitions": len(keys)})
    return rows


def inventory(root: Path) -> dict[str, Any]:
    items = []
    bank_summary_path = root / "data" / "warehouse" / "bank_token_summary.json"
    bank_summary = json.loads(bank_summary_path.read_text(encoding="utf-8")) if bank_summary_path.exists() else {}
    audit_path = root / "data/warehouse/bank_data_audit.json"
    bank_audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else None
    if bank_audit and bank_audit.get("audited_run_id") != bank_summary.get("run_id"):
        bank_audit = None
    for group, (name, description, required) in GROUPS.items():
        if group == "bank_industry":
            continue
        if group == "bank_free":
            free = bank_inventory(root)
            items.append(
                {
                    "id": group,
                    "name": name,
                    "description": description,
                    "required_for_strategy": required,
                    "source_id": "eastmoney",
                    "requires_token": False,
                    **free,
                }
            )
            continue
        if group in ARCHIVES:
            state = _checkpoint(root, group)
            coverage = state.get("coverage") or {}
            end = _entry_end(state, group)
            if end and len(end) == 8 and end.isdigit():
                end = date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:]}").isoformat()
            start = coverage.get("start")
            if group == "market":
                keys = state.get("completed", {}).get("daily", {})
                if keys:
                    first = min(keys)
                    start = f"{first[:4]}-{first[4:6]}-{first[6:]}"
            counts = {api: len(partitions) for api, partitions in state.get("completed", {}).items()}
            published = _published(root, group, state)
            datasets = _datasets(state, group)
        elif group == "benchmark":
            path = root / "data" / "benchmarks" / "csi300_daily.json"
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            start, end = (value.get("coverage") or {}).get("start"), (value.get("coverage") or {}).get("end")
            counts = {"sessions": len(value.get("daily", []))}
            published = bool(value)
            datasets = [{"id": "csi300_daily", "start": start, "end": end, "partitions": len(value.get("daily", []))}]
        else:
            start = end = None
            counts = {}
            published = True
            datasets = []
        items.append(
            {
                "id": group,
                "name": name,
                "description": description,
                "source_id": "tushare",
                "requires_token": group in ARCHIVES,
                "required_for_strategy": required,
                "start": start,
                "end": end,
                "partitions": counts,
                "datasets": datasets,
                "published": published,
            }
        )
    factors = []
    try:
        configured = bool(tushare_token(root))
        credential_status = "configured" if configured else "missing"
    except ValueError:
        configured, credential_status = False, "invalid"
    return {
        "today": date.today().isoformat(),
        "latest_complete_day": latest_complete_day().isoformat(),
        "groups": items,
        "factors": factors,
        "industries": [
            {
                "id": "bank_industry",
                "name": GROUPS["bank_industry"][0],
                "description": GROUPS["bank_industry"][1],
                "published": bank_summary.get("published", False),
                "end": bank_summary.get("complete_cutoff_date"),
                "start": "2010-01-01",
                "last_collected_at": bank_summary.get("groups", {}).get("market", {}).get("collected_at"),
                "datasets": [
                    dict(id=k, partitions=v.get("rows", 0), **v) for k, v in bank_summary.get("groups", {}).items()
                ],
                "bank_count": bank_summary.get("groups", {}).get("market", {}).get("banks", 0),
                "expected_banks": 42,
                "required_for_strategy": False,
                "partitions": {},
                "source_id": "industry",
                "audit": bank_audit,
                "requires_token": True,
            }
        ],
        "sources": [
            {
                "id": "tushare",
                "name": "Tushare · Token数据",
                "credential_required": True,
                "credential_status": credential_status,
                "credential_configured": configured,
                "description": "优先采用Token接口的行情、财务及股息率；免费源仅补缺。配置凭证不代表权限已核验。",
                "defaults": {"groups": list(LEGACY_GROUPS), "factor_ids": []},
            },
            {
                "id": "eastmoney",
                "name": "东方财富 · 免费公开数据",
                "credential_required": False,
                "credential_status": "not_required",
                "credential_configured": False,
                "description": "42家银行财报；补充Token接口缺失的资产质量、资本等字段，各自保留来源版本。",
                "defaults": {"groups": ["bank_free"], "factor_ids": []},
            },
        ],
        "defaults": {"groups": list(LEGACY_GROUPS), "factor_ids": []},
    }


def plan(root: Path, request: DataUpdateRequest) -> dict[str, Any]:
    items = inventory(root)
    group_by_id = {item["id"]: item for item in (items["groups"] + items.get("industries", []))}
    factor_by_id = {item["id"]: item for item in items["factors"]}
    missing_factors = (set(request.factor_ids) - factor_by_id.keys()) if "factors" in request.groups else set()
    if missing_factors:
        raise ValueError(f"factor releases are not available: {sorted(missing_factors)}")
    end = request.end.isoformat()
    selected = [group for group in STAGE_ORDER if group in request.groups]
    for group in selected:
        for dependency in DEPENDENCIES.get(group, ()):
            source = group_by_id[dependency]
            if dependency not in selected and ((source["end"] or "") < end or not source["published"]):
                raise ValueError(f"{group} requires {dependency} to be selected or already published through {end}")
    stages = []
    for group in selected:
        current = group_by_id[group]
        needs_update = (current["end"] or "") < end or not current["published"]
        if group == "bank_industry":
            collected = current.get("last_collected_at") or ""
            needs_update = needs_update or collected[:10] != date.today().isoformat()
        if group == "bank_free":
            # Report-period end is not acquisition freshness. Refresh the bank
            # snapshots daily, even while the latest disclosed period stays unchanged.
            needs_update = (
                not current["published"]
                or current.get("snapshot_day") != date.today().isoformat()
                or (current.get("snapshot_through") or "") < end
            )
        if group == "factors":
            needs_update = any((factor_by_id[f]["end"] or "") < end for f in request.factor_ids)
        stages.append(
            {
                "id": group,
                "name": current["name"],
                "current_end": current["end"],
                "needs_update": needs_update,
                "source_id": request.source_id,
                "requires_token": group in ARCHIVES or group == "bank_industry",
                "last_collected_at": current.get("last_collected_at"),
            }
        )
    required_missing = [
        group
        for group, (_, _, required) in GROUPS.items()
        if required
        and group not in request.groups
        and ((group_by_id[group]["end"] or "") < end or not group_by_id[group]["published"])
    ]
    token_required = any(stage["needs_update"] and stage["requires_token"] for stage in stages)
    token_available = False
    if request.source_id in ("tushare", "industry"):
        try:
            token_available = bool(tushare_token(root))
        except ValueError:
            pass
    if request.source_id in ("eastmoney", "industry"):
        required_missing = []
    return {
        "target_end": end,
        "source_id": request.source_id,
        "stages": stages,
        "required_unselected": required_missing,
        "token_required": token_required,
        "token_available": token_available,
        "can_start": not token_required or token_available,
        "strategy_ready_if_complete": request.source_id == "tushare" and not required_missing,
    }


def _status(path: Path, **detail: Any) -> None:
    path.write_bytes(canonical_json_bytes({"updated_at": datetime.now().astimezone().isoformat(), **detail}))


def run(root: Path, request: DataUpdateRequest, progress_path: Path, log_path: Path) -> None:
    prepared = plan(root, request)
    selected = [stage for stage in prepared["stages"] if stage["needs_update"]]
    if any(stage.get("requires_token", stage["id"] in ARCHIVES) for stage in selected) and not tushare_token(root):
        raise ValueError("TUSHARE_TOKEN is not configured in the backend environment")
    archive_starts = {group: (_checkpoint(root, group).get("coverage") or {}).get("start") for group in ARCHIVES}
    with log_path.open("a", encoding="utf-8") as log:
        for index, stage in enumerate(selected):
            group = stage["id"]
            commands: list[list[str]] = []
            end = request.end.isoformat()
            if group == "bank_free":
                commands = [
                    [
                        "scripts/sync_eastmoney_banks.py",
                        "--end",
                        end,
                        "--workers",
                        str(request.workers),
                        "--min-free-gb",
                        str(request.min_free_gb),
                        "--progress",
                        str(progress_path),
                    ]
                ]
            elif group == "bank_industry":
                commands = [
                    [
                        "scripts/sync_bank_industry.py",
                        "--end",
                        end,
                        "--workers",
                        str(min(request.workers, 2)),
                        "--min-free-gb",
                        str(request.min_free_gb),
                    ]
                ]
            elif group == "reference":
                commands = [
                    [
                        "scripts/backfill_tushare_reference.py",
                        "--start",
                        archive_starts[group],
                        "--end",
                        end,
                        "--endpoint",
                        _archive_endpoint(root, group),
                        "--min-free-gb",
                        str(request.min_free_gb),
                        "--sleep-ms",
                        str(request.sleep_ms),
                    ],
                    ["scripts/build_reference_warehouse.py"],
                ]
            elif group == "market":
                market = _checkpoint(root, "market").get("completed", {})
                latest = min(max(market.get(api, {})) for api in ("daily", "adj_factor", "daily_basic"))
                first = date.fromisoformat(f"{latest[:4]}-{latest[4:6]}-{latest[6:]}") - timedelta(days=7)
                commands = [
                    [
                        "scripts/backfill_tushare_daily.py",
                        "--start",
                        first.isoformat(),
                        "--end",
                        end,
                        "--endpoint",
                        _archive_endpoint(root, group),
                        "--min-free-gb",
                        str(request.min_free_gb),
                        "--sleep-ms",
                        str(request.sleep_ms),
                        "--workers",
                        str(request.workers),
                        "--require-nonempty",
                    ],
                    ["scripts/build_market_warehouse.py"],
                ]
            elif group == "financial":
                commands = [
                    [
                        "scripts/backfill_tushare_financials.py",
                        "--start",
                        archive_starts[group],
                        "--end",
                        end,
                        "--endpoint",
                        _archive_endpoint(root, group),
                        "--min-free-gb",
                        str(request.min_free_gb),
                        "--sleep-ms",
                        str(request.sleep_ms),
                        "--refresh-recent-periods",
                        str(request.refresh_recent_periods),
                    ],
                    ["scripts/build_financial_warehouse.py"],
                ]
            elif group == "limits":
                commands = [
                    [
                        "scripts/backfill_tushare_m2e.py",
                        "--start",
                        archive_starts[group],
                        "--end",
                        end,
                        "--endpoint",
                        _archive_endpoint(root, group),
                        "--api",
                        "stk_limit",
                        "--min-free-gb",
                        str(request.min_free_gb),
                        "--sleep-ms",
                        str(request.sleep_ms),
                    ],
                    ["scripts/build_m2e_core_warehouse.py"],
                ]
            elif group == "corporate":
                commands = [
                    [
                        "scripts/backfill_tushare_corporate_actions.py",
                        "--start",
                        archive_starts[group],
                        "--end",
                        end,
                        "--endpoint",
                        _archive_endpoint(root, group),
                        "--incremental-daily",
                        "--min-free-gb",
                        str(request.min_free_gb),
                        "--sleep-ms",
                        str(request.sleep_ms),
                    ],
                    ["scripts/build_corporate_action_warehouse.py"],
                ]
            elif group == "benchmark":
                commands = [
                    [
                        "scripts/sync_strategy_benchmark.py",
                        "--end",
                        end,
                        "--incremental",
                        "--tushare-endpoint",
                        _archive_endpoint(root, "market"),
                    ]
                ]
            elif group == "factors":
                coverage = {item["id"]: item for item in inventory(root)["factors"]}
                for factor in request.factor_ids:
                    if (coverage[factor]["end"] or "") >= end:
                        continue
                    commands.append(
                        [
                            "scripts/publish_jqdata_factor.py",
                            "--factor-id",
                            factor,
                            "--start",
                            coverage[factor]["start"],
                            "--end",
                            end,
                            "--skip-verification",
                        ]
                    )
            for part, command in enumerate(commands, start=1):
                progress = round((index + (part - 1) / max(1, len(commands))) / max(1, len(selected)) * 100)
                environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root)))}
                for attempt in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
                    _status(
                        progress_path,
                        status="RUNNING",
                        phase=stage["name"],
                        group=group,
                        step=part,
                        steps=len(commands),
                        progress=progress,
                        retry_attempt=attempt,
                        completed_groups=index,
                        total_groups=len(selected),
                    )
                    log.write(f"RUN attempt {attempt}/{MAX_TRANSIENT_ATTEMPTS} " + " ".join(command) + "\n")
                    log.flush()
                    start_offset = log_path.stat().st_size
                    process = subprocess.run(
                        [sys.executable, *command],
                        cwd=root,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    log.flush()
                    if process.returncode == 0:
                        break
                    if attempt == MAX_TRANSIENT_ATTEMPTS or not _transient_failure(log_path, start_offset):
                        raise RuntimeError(f"{stage['name']} failed during {command[0]} (exit {process.returncode})")
                    delay = 5 * 3 ** (attempt - 1)
                    log.write(f"RETRY transient source failure in {delay}s\n")
                    log.flush()
                    _status(
                        progress_path,
                        status="RUNNING",
                        phase=f"{stage['name']}：网络暂不可用，{delay}秒后重试",
                        group=group,
                        step=part,
                        steps=len(commands),
                        progress=progress,
                        retry_attempt=attempt + 1,
                        retry_in_seconds=delay,
                        completed_groups=index,
                        total_groups=len(selected),
                    )
                    time.sleep(delay)
        _status(
            progress_path,
            status="PASS",
            phase="更新完成",
            progress=100,
            completed_groups=len(selected),
            total_groups=len(selected),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    request = DataUpdateRequest.model_validate_json(args.request.read_bytes())
    try:
        run(args.project_root, request, args.progress, args.log)
    except Exception as error:
        with args.log.open("a", encoding="utf-8") as log:
            log.write(traceback.format_exc())
        current = json.loads(args.progress.read_bytes()) if args.progress.exists() else {}
        current.pop("updated_at", None)
        current.update(status="FAIL", phase="更新失败", error=str(error))
        _status(args.progress, **current)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
