"""Update ledger and reminders from publication/job metadata, without reading research values."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CHINA = timezone(timedelta(hours=8))
UPDATE_PROMPT = """请检查 D:\\futures_quant_strategy 的银行财报原文证据，补充并核验尚缺的报告数据。
先读取 AGENTS.md、研究宪法和数据访问限制，检查任务锁、磁盘、Token权限及各来源版本。
通用和行业接口数据由我在页面更新。本次只读取已有台账和接口归档作比对，不重复运行接口同步或调用收费接口；接口归档过旧时列出需要我在页面更新的项目。
查交易所、巨潮或银行官网的新季报、中报、年报、更正及分红公告，与已归档报告逐项比较，只补尚缺的报告或修订版本。
财报原文需保存公告链接、原始文件及哈希、报告期、真实披露时间、采集时间、页码、单位、合并范围和指标口径。
核验盈利、净资产及其他权益工具、资产质量、拨备、资本、净息差、贷款存款及分红所属期间和实施状态。
未披露、漏采、口径冲突、尚待核验分别记录；季度累计数不能直接当单季，预案不能当已实施分红。
指标缺失或无效时保留空值及原因，不擅自跨时点、跨定义或跨口径替代；来源直接提供的指标与计算得到的派生指标分别标明，新增补算须核验输入、可用时间、公式及口径。
原文补充单独保存证据版本，不覆盖接口原始表。已有同银行、报告期和指标的数据先核对时间、单位及口径，记录差异，不盲目覆盖。
更新后记录成功/失败、来源版本、截止日期、缺项及待重算因子。因子计算由我在页面另行启动；不扩展研究范围，不改写旧证据或旧报告。
现有采集器不支持的新报告类型要明确说明并补齐可核验流程，不能宣称更新已完成。最后给我更新结果和仍需处理的清单。"""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=CHINA) if parsed.tzinfo is None else parsed.astimezone(CHINA)
    except (TypeError, ValueError):
        return None


def update_history(root: Path, page: int = 1, page_size: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("page must be positive and page_size must be between 1 and 100")
    files = sorted((root / "reports/data_updates").glob("*.request.json"), reverse=True)
    items = []
    for path in files[(page - 1) * page_size:page * page_size]:
        job_id = path.name.removesuffix(".request.json")
        request = _read(path)
        progress = _read(path.with_name(job_id + ".progress.json"))
        items.append({"job_id": job_id, "source_id": request.get("source_id", "tushare"),
                      "groups": request.get("groups", []), "target_end": request.get("end"),
                      "status": progress.get("status", "UNKNOWN"), "updated_at": progress.get("updated_at"),
                      "error": progress.get("error"), "completed_groups": progress.get("completed_groups", 0),
                      "total_groups": progress.get("total_groups", 0)})
    return {"items": items, "page": page, "page_size": page_size, "total_items": len(files),
            "total_pages": (len(files) + page_size - 1) // page_size}


def maintenance_inventory(root: Path, inventory: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(CHINA)).astimezone(CHINA)
    success: dict[str, datetime] = {}
    for path in sorted((root / "reports/data_updates").glob("*.request.json"), reverse=True):
        progress = _read(path.with_name(path.name.replace(".request.json", ".progress.json")))
        # A failed/empty job must never reset a source's freshness clock.
        stamp = _stamp(progress.get("updated_at"))
        if progress.get("status") != "PASS" or not stamp or stamp > current:
            continue
        updated_groups = progress.get("updated_groups")
        if updated_groups is None:
            requested = _read(path).get("groups", [])
            # Legacy logs are usable only when every requested group actually ran.
            updated_groups = requested if (requested and progress.get("total_groups") == len(requested)
                                           and progress.get("completed_groups") == len(requested)) else []
        for group in updated_groups:
            if group not in success or stamp > success[group]:
                success[group] = stamp
    rows = []
    for group in [*inventory.get("groups", []), *inventory.get("industries", [])]:
        collected = _stamp(group.get("last_collected_at"))
        candidates = [s for s in (success.get(group["id"]), collected) if s and s <= current]
        last = max(candidates) if candidates and group.get("published") else None
        days = (current.date() - last.date()).days if last else None
        state = "missing" if not last else "long_unused" if days >= 30 else "due" if days >= 7 else "current"
        rows.append({"id": group["id"], "name": group["name"], "source_id": group.get("source_id"),
                     "coverage_end": group.get("end"), "last_success_at": last.isoformat() if last else None,
                     "days_since_success": days, "state": state,
                     "next_check_at": (last + timedelta(days=7)).isoformat() if last else None})
    next_week = current.replace(hour=20, minute=30, second=0, microsecond=0) + timedelta(days=-current.weekday())
    if next_week <= current:
        next_week += timedelta(days=7)
    return {"checked_at": current.isoformat(), "timezone": "Asia/Shanghai", "interval_days": 7,
            "long_unused_days": 30, "weekly_schedule": "每周一 20:30（北京时间）",
            "next_weekly_at": next_week.isoformat(), "ledger": rows,
            "official_reports_status": "待通过对话检查官方公告；来源采集完成不代表原文核验完成",
            "update_prompt_scope": "bank_reports", "update_prompt": UPDATE_PROMPT}
