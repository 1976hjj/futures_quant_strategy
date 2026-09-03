# ruff: noqa: E501
"""Local read-only dashboard for a checkpointed Tushare archive job."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A 股历史数据回填监控</title><style>
:root{color-scheme:dark;--bg:#101827;--panel:#182235;--line:#2a3851;--text:#e8edf7;--sub:#9cacbf;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 18px}h1{font-size:24px;margin:0 0 6px}.sub{color:var(--sub);margin:0 0 22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}.label{color:var(--sub);font-size:13px}.value{font-size:27px;font-weight:650;margin:5px 0}.small{font-size:13px;color:var(--sub)}
.status{display:inline-block;border-radius:99px;padding:5px 10px;font-size:13px;font-weight:700}.RUNNING,.COMPLETED{background:#064e3b;color:var(--ok)}.WAITING,.RETRYING{background:#713f12;color:var(--warn)}.STALLED,.STOPPED,.FAILED{background:#4c1d2b;color:var(--bad)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:12px}.bar{height:12px;background:#27364d;border-radius:99px;overflow:hidden}.bar>i{display:block;height:100%;background:linear-gradient(90deg,#38bdf8,#34d399);width:0;transition:width .3s}.rows{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}.row{padding:12px;border-radius:9px;background:#131d2e}.error{color:#fecdd3;white-space:pre-wrap;word-break:break-word}.buttons{display:flex;gap:10px;margin-top:14px}button{background:#243450;color:var(--text);border:1px solid #3b4e70;border-radius:8px;padding:8px 12px;cursor:pointer}
@media(max-width:620px){.rows{grid-template-columns:1fr}.value{font-size:23px}}</style></head><body><main>
<h1>A 股历史数据回填监控</h1><p class="sub">只读本机页面 · 自动刷新：<span id="refresh">5</span> 秒</p>
<div class="grid"><section class="card"><div class="label">任务状态</div><div class="value"><span id="health" class="status">读取中</span></div><div class="small" id="process"></div></section>
<section class="card"><div class="label">总进度</div><div class="value" id="progress">—</div><div class="small" id="partition"></div></section>
<section class="card"><div class="label">速率 / 预计剩余</div><div class="value" id="rate">—</div><div class="small" id="eta"></div></section>
<section class="card"><div class="label">D盘可用空间</div><div class="value" id="disk">—</div><div class="small" id="disknote"></div></section></div>
<section class="panel"><div class="label">整体归档进度</div><div class="bar"><i id="bar"></i></div><div class="rows" id="tables"></div></section>
<section class="panel"><div class="label">最后 checkpoint</div><div id="checkpoint" class="small">—</div><div class="buttons"><button onclick="copyDiagnostic()">复制诊断信息</button><button onclick="loadStatus()">立即刷新</button></div></section>
<section class="panel"><div class="label">最近错误</div><div id="error" class="small">无</div></section>
</main><script>
let latest={};function text(id,v){document.getElementById(id).textContent=v}function fmt(n){return new Intl.NumberFormat().format(n||0)}
function duration(s){if(s==null)return '计算中';if(s<60)return Math.round(s)+'秒';if(s<3600)return Math.round(s/60)+'分钟';return (s/3600).toFixed(1)+'小时'}
function render(d){latest=d;const h=document.getElementById('health');h.textContent=d.health;h.className='status '+d.health;text('process',d.process_running?'回填进程：运行中':'回填进程：未发现');text('progress',d.progress_percent.toFixed(2)+'%');text('partition',fmt(d.completed_partitions)+' / '+fmt(d.expected_partitions)+' 分区');text('rate',d.rate_per_hour==null?'采样中':d.rate_per_hour.toFixed(1)+' 分区/小时');text('eta',d.eta_seconds==null?'预计剩余：计算中':'预计剩余：'+duration(d.eta_seconds));text('disk',d.free_gb.toFixed(1)+' GiB');text('disknote','安全水位 '+d.min_free_gb.toFixed(1)+' GiB');document.getElementById('bar').style.width=d.progress_percent+'%';text('checkpoint',d.checkpoint_updated_at+' · 已停滞 '+duration(d.stale_seconds));const tables=document.getElementById('tables');tables.innerHTML=Object.entries(d.tables).map(([name,count])=>`<div class="row"><div class="label">${name}</div><div class="value">${fmt(count)}</div><div class="small">日期分区</div></div>`).join('');const e=document.getElementById('error');e.textContent=d.recent_error||'无';e.className=d.recent_error?'error':'small'}
async function loadStatus(){try{const r=await fetch('/api/status',{cache:'no-store'});render(await r.json())}catch(e){text('error','监控页无法读取状态：'+e)}}
async function copyDiagnostic(){await navigator.clipboard.writeText(JSON.stringify(latest,null,2));text('refresh','已复制')}
loadStatus();setInterval(loadStatus,5000);</script></body></html>"""


class StatusTracker:
    def __init__(self, archive: Path, expected: int, min_free_gb: float, process_script: str) -> None:
        self.archive = archive
        self.expected = expected
        self.min_free_gb = min_free_gb
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", process_script):
            raise ValueError("process script must be a plain file name")
        self.process_script = process_script
        self._samples: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        checkpoint = self.archive / "checkpoint.json"
        state = self._read_json(checkpoint)
        completed = state.get("completed", {})
        tables = {name: len(partitions) for name, partitions in completed.items()}
        total = sum(tables.values())
        expected = self._expected_partitions(state)
        now = time.time()
        with self._lock:
            self._samples.append((now, total))
            self._samples = [sample for sample in self._samples if now - sample[0] <= 300]
            rate = self._rate()
        mtime = checkpoint.stat().st_mtime if checkpoint.exists() else 0.0
        stale = max(0.0, now - mtime) if mtime else None
        running = self._backfill_process_running()
        run_status = self._read_json(self.archive / "run_status.json")
        declared_status = run_status.get("status")
        if running and declared_status == "RETRYING":
            health = "RETRYING"
        elif running and stale is not None and stale < 120:
            health = "RUNNING"
        elif running and stale is not None and stale < 600:
            health = "WAITING"
        elif running:
            health = "STALLED"
        elif declared_status == "COMPLETED":
            health = "COMPLETED"
        else:
            health = "STOPPED"
        disk_path = self.archive if self.archive.exists() else self.archive.parent
        free_gb = shutil.disk_usage(disk_path).free / (1024**3)
        error = self._last_error(run_status)
        return {
            "checkpoint_updated_at": datetime.fromtimestamp(mtime, UTC).astimezone().isoformat() if mtime else "不存在",
            "completed_partitions": total,
            "eta_seconds": (expected - total) / rate * 3600 if rate and total < expected else 0,
            "expected_partitions": expected,
            "free_gb": free_gb,
            "health": health,
            "min_free_gb": self.min_free_gb,
            "process_running": running,
            "progress_percent": min(100.0, total / expected * 100) if expected else 0.0,
            "rate_per_hour": rate,
            "recent_error": error,
            "retry_attempt": run_status.get("retry_attempt"),
            "retry_in_seconds": run_status.get("next_retry_seconds"),
            "stale_seconds": stale,
            "tables": tables,
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"completed": {}}
        for _ in range(3):
            try:
                return json.loads(path.read_bytes())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
        return {"completed": {}}

    def _rate(self) -> float | None:
        if len(self._samples) < 2:
            return None
        first_time, first_total = self._samples[0]
        last_time, last_total = self._samples[-1]
        if last_time <= first_time or last_total <= first_total:
            return None
        return (last_total - first_total) / (last_time - first_time) * 3600

    def _expected_partitions(self, state: dict[str, Any]) -> int:
        if state.get("schema") == "tushare-financial-backfill-v1":
            entries = [
                entry
                for partitions in state.get("completed", {}).values()
                for entry in partitions.values()
            ]
            base = len(state.get("apis", [])) * len(state.get("periods", []))
            page_size = int(state.get("page_size", 5000))
            full_pages = sum(int(entry.get("rows", 0)) == page_size for entry in entries)
            return max(1, base + full_pages)
        if state.get("schema") != "tushare-reference-backfill-v1":
            return self.expected
        sessions = [str(value) for value in state.get("open_sessions", [])]
        if not sessions:
            return self.expected
        coverage = state.get("coverage", {})
        start_year = int(str(coverage.get("start", "1990"))[:4])
        end_year = int(str(coverage.get("end", "1990"))[:4])
        namechange_partitions = 1 + max(0, end_year - start_year + 1)
        static_partitions = 1 + 5 + namechange_partitions
        return static_partitions + len(sessions) + sum(session >= "20000101" for session in sessions)

    def _backfill_process_running(self) -> bool:
        command = (
            "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{self.process_script}*' "
            "-and $_.CommandLine -notlike '*serve_backfill_dashboard.py*' } | "
            "Select-Object -First 1 -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return bool(result.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _last_error(self, run_status: dict[str, Any]) -> str | None:
        if run_status.get("status") == "RETRYING":
            return (
                f"RETRYING attempt {run_status.get('retry_attempt')}; "
                f"next wait {run_status.get('next_retry_seconds')}s; "
                f"{run_status.get('error_type')}: {run_status.get('error_message')}"
            )
        if run_status.get("status") == "FAILED":
            return f"{run_status.get('error_type')}: {run_status.get('error_message')}"
        path = self.archive / "backfill.stderr.log"
        if not path.exists() or not path.stat().st_size:
            return None
        return path.read_text(encoding="utf-8", errors="replace")[-1500:].strip() or None


def make_handler(tracker: StatusTracker, title: str):
    page = HTML.replace("A 股历史数据回填监控", html.escape(title))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._write(HTTPStatus.OK, "text/html; charset=utf-8", page.encode())
            elif self.path == "/api/status":
                self._write(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    json.dumps(tracker.status(), ensure_ascii=False, allow_nan=False).encode(),
                )
            else:
                self._write(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=Path("data/tushare_archive"))
    parser.add_argument("--expected-partitions", type=int, default=26148)
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--process-script", default="backfill_tushare_daily.py")
    parser.add_argument("--title", default="A 股历史数据回填监控")
    args = parser.parse_args()
    tracker = StatusTracker(args.archive.resolve(), args.expected_partitions, args.min_free_gb, args.process_script)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(tracker, args.title))
    print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
