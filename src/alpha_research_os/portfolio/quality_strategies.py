"""Versioned, evidence-bound strategy notebook and persistent signal jobs.

The notebook records owner-selected research candidates; it does not promote
them to VALIDATED or DEPLOYABLE and never places brokerage orders.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import calendar
from datetime import date
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CATEGORIES = {"SMALL_CAP": "小市值", "BANK": "银行", "FACTOR_WEIGHTED": "因子加权"}
SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")


def chart_history(result: dict) -> dict:
    """Expose recorded end-of-day accounting; never substitute live positions."""
    fields = ("session", "nav", "cash", "positions", "daily_return",
              "actual_stock_exposure", "target_shadow_exposure", "dividend_cash",
              "cash_interest", "cost", "missing_marks", "holdings")
    daily = [{k: row[k] for k in fields if k in row} for row in result.get("daily", [])]
    # Presentation-only reconstruction from this result's own executed trades.
    # Keep the saved result immutable and never use today's price or positions.
    costs: dict[str, float | None] = {}
    names: dict[str, str] = {}
    last_rebalances: dict[str, str] = {}
    holding_starts: dict[str, str] = {}
    quantities: dict[str, float] = {}
    trades = iter(sorted(result.get("trades", []), key=lambda t: t["session"]))
    trade = next(trades, None)

    def finite_number(value):
        return isinstance(value, (int, float)) and math.isfinite(value)

    def money(value):
        return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    for row in daily:
        while trade is not None and trade["session"] <= row["session"]:
            code = trade["ts_code"]
            if trade.get("side") in ("BUY", "SELL"):
                last_rebalances[code] = trade["session"]
                post_quantity = trade.get("post_quantity")
                quantity = trade.get("quantity")
                if trade["side"] == "BUY" and finite_number(quantity) and quantity > 0 and (
                    quantities.get(code) == 0 or post_quantity == quantity
                ):
                    holding_starts[code] = trade["session"]
                if finite_number(post_quantity):
                    quantities[code] = post_quantity
                    if post_quantity == 0:
                        holding_starts.pop(code, None)
            if trade.get("security_name"):
                names[code] = trade["security_name"]
            balance = costs.get(code, 0.0)
            if trade.get("side") == "BUY":
                amount, fee = trade.get("amount_cny"), trade.get("total_cost_cny")
                costs[code] = (money(balance + amount + fee)
                               if balance is not None and finite_number(amount) and finite_number(fee)
                               else None)
            elif trade.get("side") == "SELL":
                allocated = trade.get("cost_basis_cny")
                costs[code] = (money(balance - allocated)
                               if code in costs and balance is not None and finite_number(allocated)
                               else None)
                if costs[code] is not None and costs[code] < 0:
                    costs[code] = None
                if trade.get("post_quantity") == 0:
                    costs[code] = 0.0
            trade = next(trades, None)
        if "holdings" not in row:
            continue
        holdings = []
        for original in row["holdings"]:
            holding = dict(original)
            code = holding["ts_code"]
            if not holding.get("security_name") and code in names:
                holding["security_name"] = names[code]
            if code in last_rebalances:
                holding["last_rebalance_session"] = last_rebalances[code]
            if code in holding_starts:
                holding["holding_since_session"] = holding_starts[code]
            cost = holding.get("cost_basis_cny", costs.get(code))
            value, quantity = holding.get("market_value_cny"), holding.get("quantity")
            if finite_number(cost) and cost >= 0 and finite_number(quantity) and quantity > 0:
                holding.setdefault("cost_basis_cny", cost)
                holding.setdefault("average_cost_price", cost / quantity)
                if finite_number(value):
                    pnl = money(value - cost)
                    holding.setdefault("unrealized_pnl_cny", pnl)
                    holding.setdefault("unrealized_pnl_pct", pnl / cost if cost > 0 else None)
            holdings.append(holding)
        row["holdings"] = holdings
    return {"summary": result.get("summary", {}), "annual": result.get("annual", []),
            "initial_cash_cny": result.get("config", {}).get("initial_cash_cny"),
            "daily": daily}


def quarterly_signals(trading_days: list[date], calendar_end: date, as_of: date) -> set[date]:
    if not trading_days:
        return set()
    groups: dict[tuple[int, int], date] = {}
    for day in trading_days:
        groups[(day.year, (day.month - 1) // 3)] = day
    output = {trading_days[0]}
    for (year, quarter), day in groups.items():
        month = (quarter + 1) * 3
        period_end = date(year, month, calendar.monthrange(year, month)[1])
        if period_end <= calendar_end and day <= as_of:
            output.add(day)
    return output


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_with_retry(temporary: Path, path: Path) -> None:
    # Windows readers/virus scanners can briefly deny replacement of an open
    # file. Retry the same complete temporary file, never write JSON in place.
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.05 * (attempt + 1))


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    replace_with_retry(temporary, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def inverse_risk_weights(values: dict[str, float], cap: float = .15) -> dict[str, float]:
    """Stable water filling; dict order is the selected/retained purchase order."""
    if not values:
        return {}
    if any(v is None or not math.isfinite(v) or v <= 0 for v in values.values()):
        raise ValueError("risk weighting requires a positive finite risk value for every stock")
    if not 0 < cap <= 1:
        raise ValueError("invalid risk weight cap")
    # A short list cannot meet a 15% cap; keep unused slots in cash rather than
    # silently relaxing the cap. The engine accepts total target weight <= 1.
    budget = min(1.0, len(values) * cap)
    remaining = sorted(values)
    weights: dict[str, float] = {}
    while remaining:
        total = math.fsum(1 / values[c] for c in remaining)
        provisional = {c: budget / values[c] / total for c in remaining}
        saturated = [c for c in remaining if provisional[c] > cap + 1e-12]
        if not saturated:
            weights.update(provisional)
            break
        for c in saturated:
            weights[c] = cap
            budget -= cap
        remaining = [c for c in remaining if c not in saturated]
    return {c: weights[c] for c in values}


class RegisterStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")
    category: Literal["SMALL_CAP", "BANK", "FACTOR_WEIGHTED"]
    name: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=2000)


class HoldingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ts_code: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    quantity: int = Field(ge=0, le=100_000_000)


class CalculateSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    capital_cny: float = Field(default=1_000_000, gt=0, le=1_000_000_000)
    current_holdings: list[HoldingInput] = Field(default_factory=list, max_length=500)


class QualityStrategyBook:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.store = self.root / "data/quality_strategies"
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen] = {}

    def _id(self, value: str) -> str:
        if not SAFE_ID.fullmatch(value):
            raise ValueError("invalid strategy/job identifier")
        return value

    def definitions(self) -> list[dict]:
        path = self.root / "config/quality_strategies.json"
        definitions = read_json(path)["strategies"] if path.exists() else []
        for path in sorted((self.store / "definitions").glob("*.json")):
            definitions.append(read_json(path))
        return definitions

    def definition(self, sid: str) -> dict:
        sid = self._id(sid)
        for item in self.definitions():
            if item["strategy_id"] == sid:
                return item
        raise FileNotFoundError(sid)

    def _source(self, definition: dict) -> dict:
        # Keep selected seed evidence independently of disposable research files.
        cached = self.store / "evidence" / f"{self._id(definition['strategy_id'])}-{definition['source_sha256']}.json"
        if cached.exists():
            if fingerprint(cached) != definition["source_sha256"]:
                raise ValueError("recorded strategy evidence integrity failure")
            return read_json(cached)
        path = (self.root / definition["source_path"]).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("strategy evidence path escapes project")
        if fingerprint(path) != definition["source_sha256"]:
            raise ValueError("strategy source evidence changed; record a new version")
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_name(cached.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(path.read_bytes())
        if fingerprint(temporary) != definition["source_sha256"]:
            temporary.unlink()
            raise ValueError("strategy source changed while archiving")
        replace_with_retry(temporary, cached)
        return read_json(cached)

    def latest_job(self, sid: str) -> dict | None:
        paths = sorted((self.store / "jobs").glob(f"{self._id(sid)}--*.request.json"), reverse=True)
        return self.job(paths[0].name.removesuffix(".request.json")) if paths else None

    def item(self, definition: dict, *, detail: bool = False) -> dict:
        try:
            source = self._source(definition)
            error = None
        except (OSError, ValueError, KeyError) as exc:
            source, error = {}, str(exc)
        parent_reference = definition.get("source_evidence_role") == "PARENT_RULE_REFERENCE"
        summary = {} if parent_reference else source.get("summary") or source.get("result_summary") or {}
        item = {k: definition.get(k) for k in ["strategy_id", "version", "category", "name", "notes", "status", "schedule", "allocation", "fixed_exposure", "evidence_version", "information_clock"]}
        item.update(summary=summary, start=definition["request"]["start"], end=definition["request"]["end"], target_count=definition["request"]["target_count"], rebalance_sessions=definition["request"]["rebalance_sessions"], evidence_error=error, latest_job=self.latest_job(definition["strategy_id"]))
        job = item["latest_job"]
        item["latest_summary"] = None
        item["latest_as_of"] = None
        item["calculation_state"] = "NOT_CALCULATED"
        item["latest_calculation_mode"] = None
        successful = next((self.job(p.name.removesuffix(".state.json")) for p in sorted(
            (self.store / "jobs").glob(f"{definition['strategy_id']}--*.state.json"), reverse=True)
            if read_json(p)["status"] == "PASS"), None)
        if successful:
            calculated = self.result(successful["job_id"])
            item["latest_summary"] = calculated["historical_replay"]["summary"]
            item["latest_as_of"] = calculated["as_of"]
            from alpha_research_os.portfolio.quality_inputs import CALCULATION_MODE, source_signature
            item["latest_calculation_mode"] = calculated.get("calculation_mode")
            if calculated.get("calculation_mode") != CALCULATION_MODE:
                item["calculation_state"] = "LEGACY_INPUTS"
            elif (calculated.get("source_signature") != source_signature(self.root)
                  or calculated.get("definition_sha256") != hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()):
                item["calculation_state"] = "STALE"
            else:
                item["calculation_state"] = "UP_TO_DATE"
        if definition["category"] == "BANK":
            settings = self.root / "config/bank_timing_data.json"
            item["released_input_end"] = read_json(settings)["released_feature_end"] if settings.exists() else None
        if detail:
            # A new variant may inherit a parent's rules as its provenance, but
            # must never label the parent's performance as this variant's own.
            archived = chart_history({} if parent_reference else source)
            item.update(request=definition["request"], annual=archived["annual"], daily=archived["daily"], initial_cash_cny=archived["initial_cash_cny"], related_evidence=[])
            for index, evidence in enumerate(definition.get("related_evidence", [])):
                path = (self.root / evidence["path"]).resolve()
                if not path.is_relative_to(self.root):
                    raise ValueError("related evidence path escapes project")
                if evidence.get("source_sha256") or path.exists():
                    result = self._source({"strategy_id": f"{definition['strategy_id']}-related-{index}",
                                           "source_path": evidence["path"],
                                           "source_sha256": evidence.get("source_sha256") or fingerprint(path)})
                    item["related_evidence"].append({"label": evidence["label"], "start": result["config"]["start"], "end": result["config"]["end"], "summary": result["summary"]})
            job = item["latest_job"]
            item["signal"] = self.result(successful["job_id"]) if successful else None
        return item

    def list(self, category: str | None = None) -> dict:
        if category is not None and category not in CATEGORIES:
            raise ValueError("unknown strategy category")
        rows = [self.item(d) for d in self.definitions() if category is None or d["category"] == category]
        return {"categories": [{"id": k, "name": v} for k, v in CATEGORIES.items()], "items": rows}

    def detail(self, sid: str) -> dict:
        return self.item(self.definition(sid), detail=True)

    def register(self, payload: dict) -> dict:
        request = RegisterStrategy.model_validate(payload)
        if not request.name.strip():
            raise ValueError("strategy name cannot be blank")
        source = self.root / "reports/strategy_backtests" / f"{request.job_id}.result.json"
        result = read_json(source)
        if result.get("status") != "PASS" or not result.get("daily") or not result.get("summary"):
            raise ValueError("only completed backtests with a curve can be recorded")
        from alpha_research_os.portfolio.strategy_backtest import StrategyBacktestRequest
        config = StrategyBacktestRequest.model_validate(result["config"]).model_dump(mode="json")
        sid = "saved-" + uuid.uuid4().hex[:12]
        evidence = self.store / "evidence" / f"{sid}.json"
        with self.lock:
            atomic_json(evidence, result)
            definition = {"strategy_id": sid, "version": 1, "category": request.category, "name": request.name.strip(), "notes": request.notes, "status": "RESEARCH_CANDIDATE", "schedule": "INTERVAL", "allocation": "EQUAL", "fixed_exposure": None, "source_path": evidence.relative_to(self.root).as_posix(), "source_sha256": fingerprint(evidence), "request": config, "evidence_version": "SAVED_BACKTEST", "related_evidence": [], "recorded_at": datetime.now(timezone.utc).isoformat()}
            atomic_json(self.store / "definitions" / f"{sid}.json", definition)
        return self.detail(sid)

    def job(self, jid: str) -> dict:
        jid = self._id(jid)
        path = self.store / "jobs" / f"{jid}.state.json"
        if not path.exists():
            raise FileNotFoundError(jid)
        state = read_json(path)
        process = self.processes.get(jid)
        if state["status"] == "RUNNING" and process is not None and process.poll() is not None:
            result = self.store / "jobs" / f"{jid}.result.json"
            state.update(status="PASS" if process.returncode == 0 and result.exists() else "FAIL", error=None if process.returncode == 0 else "计算进程退出，请查看错误日志")
            atomic_json(path, state)
        elif state["status"] == "STARTING" and not state.get("pid"):
            created = datetime.fromisoformat(state["created_at"])
            if (datetime.now(timezone.utc) - created).total_seconds() > 30:
                state.update(status="FAIL", error="计算进程未能启动，请重新计算")
                atomic_json(path, state)
        elif state["status"] == "RUNNING" and process is None and not process_alive(int(state.get("pid") or 0)):
            result = self.store / "jobs" / f"{jid}.result.json"
            state.update(status="PASS" if result.exists() else "FAIL", error=None if result.exists() else "计算进程已结束且没有完整结果，请重新计算")
            atomic_json(path, state)
        return state

    def result(self, jid: str) -> dict:
        jid = self._id(jid)
        result = read_json(self.store / "jobs" / f"{jid}.result.json")
        # Older signal JSON omitted daily cash/exposure. Read its own immutable
        # backtest, not today's warehouse, and only after validating its hash.
        backtest = self.store / "jobs" / f"{jid}.backtest.json"
        expected = result.get("source_backtest_sha256")
        if expected and backtest.exists() and fingerprint(backtest) == expected:
            result["historical_replay"] = chart_history(read_json(backtest))
        return result

    def running(self) -> bool:
        if any(process.poll() is None for process in self.processes.values()):
            return True
        return any(self.job(p.name.removesuffix(".state.json"))["status"] in ("RUNNING", "STARTING") for p in (self.store / "jobs").glob("*.state.json"))

    def calculate(self, sid: str, payload: dict) -> dict:
        definition = self.definition(sid)
        self._source(definition)
        parameters = CalculateSignal.model_validate(payload)
        if len({h.ts_code for h in parameters.current_holdings}) != len(parameters.current_holdings):
            raise ValueError("持仓代码不能重复")
        with self.lock:
            if self.running():
                raise RuntimeError("优质策略正在计算，请等待本次任务结束")
            jid = sid + "--" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
            folder = self.store / "jobs"
            atomic_json(folder / f"{jid}.request.json", {"job_id": jid, "definition": definition, "parameters": parameters.model_dump(mode="json")})
            state = {"job_id": jid, "strategy_id": sid, "status": "STARTING", "phase": "检查本地输入并计算模型参考持仓", "progress": 0, "created_at": datetime.now(timezone.utc).isoformat(), "error": None}
            atomic_json(folder / f"{jid}.state.json", state)
            interpreter = self.root / ".research_venv/Scripts/python.exe"
            if not interpreter.exists():
                interpreter = Path(sys.executable)
            with (folder / f"{jid}.log").open("wb") as log:
                try:
                    process = subprocess.Popen([str(interpreter), str(self.root / "scripts/run_quality_strategy.py"), "--project-root", str(self.root), "--job-id", jid], cwd=self.root, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                except OSError as exc:
                    state.update(status="FAIL", error=f"计算进程启动失败：{exc}")
                    atomic_json(folder / f"{jid}.state.json", state)
                    raise RuntimeError(state["error"]) from exc
            self.processes[jid] = process
            state = read_json(folder / f"{jid}.state.json")
            state["pid"] = process.pid
            if state["status"] == "STARTING":
                state["status"] = "RUNNING"
            atomic_json(folder / f"{jid}.state.json", state)
            return state
