"""Collect official annual-report indexes and PDFs; no Token calls or factor runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"}


def request(method, url, **kwargs):
    for attempt in range(3):
        try:
            response = requests.request(method, url, headers=HEADERS, timeout=(15, 60), **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def collect(root: Path, year: int, released_end: date, codes=()):
    released = date.fromisoformat(json.loads((root / "config/bank_timing_data.json").read_bytes())["released_feature_end"])
    if released_end > released or date(year, 12, 31) > released_end:
        raise ValueError("Official report scope exceeds released bank inputs")
    if shutil.disk_usage(root).free < 10 * 1024**3:
        raise ValueError("Insufficient disk space")
    banks = json.loads((root / "config/eastmoney_banks.json").read_bytes())["banks"]
    banks = [b for b in banks if not codes or b["code"] in codes]
    folder = root / "data/bank_original_reports" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    documents, gaps, revision_notices = [], [], []

    def acquire(bank):
        code = bank["code"].split(".")[0]
        target = folder / code
        target.mkdir()
        top = request("POST", "https://www.cninfo.com.cn/new/information/topSearch/query", data={"keyWord": code, "maxNum": 10})
        (target / "stock_lookup.json").write_bytes(top.content)
        hits = top.json()
        hit = next(h for h in hits if h["code"] == code)
        params = {"pageNum": 1, "pageSize": 30, "column": "szse" if bank["code"].endswith(".SZ") else "sse",
                  "tabName": "fulltext", "stock": code + "," + hit["orgId"], "searchkey": "年度报告",
                  "seDate": f"{year + 1}-01-01~{released_end}", "isHLtitle": "false"}
        rows, responses = [], []
        for page in range(1, 21):
            params["pageNum"] = page
            response = request("POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query", data=params)
            (target / f"announcement_index_{page}.json").write_bytes(response.content)
            value = response.json()
            responses.append(value)
            rows.extend(value.get("announcements") or [])
            if not value.get("hasMore"):
                break
        else:
            raise ValueError("Official index pagination exceeded limit")
        if len(rows) != value["totalAnnouncement"] or len({r["announcementId"] for r in rows}) != len(rows):
            raise ValueError("Official index count mismatch")
        candidates, revisions = [], []
        for row in rows:
            title = re.sub(r"<[^>]+>", "", row["announcementTitle"]).replace(" ", "")
            if row.get("secCode") != code or not re.search(str(year) + r"年?年度报告", title):
                continue
            if any(s in title for s in ("更正", "修订", "补充", "取消")):
                revisions.append({"code": bank["code"], "year": year, "announcement": row})
            if not any(s in title for s in ("摘要", "H股", "英文", "英语", "社会责任", "董事会", "监事会", "补充", "说明", "更正", "修订", "取消")):
                candidates.append(row)
        if not candidates:
            raise ValueError("Original full annual report not found in official index")
        selected = min(candidates, key=lambda r: (r["announcementTime"], r["announcementId"]))
        published = datetime.fromtimestamp(selected["announcementTime"] / 1000, UTC).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date()
        if published > released_end:
            raise ValueError("Disclosure after released cutoff")
        url = "https://static.cninfo.com.cn/" + selected["adjunctUrl"]
        pdf = request("GET", url)
        if not pdf.content.startswith(b"%PDF"):
            raise ValueError("Official attachment is not a PDF")
        path = target / f"{code}_{year}_{selected['announcementId']}.pdf"
        path.write_bytes(pdf.content)
        document = {"code": bank["code"], "name": bank["name"], "year": year,
                    "title": selected["announcementTitle"], "publication_date": str(published),
                    "url": url, "source_file": str(path), "sha256": hashlib.sha256(pdf.content).hexdigest(),
                    "announcement_id": selected["announcementId"], "bytes": len(pdf.content),
                    "retrieved_at": datetime.now(UTC).isoformat(), "indexed_alternatives": len(candidates),
                    "publication_index_file": str(target / "announcement_index_1.json"),
                    "revision_notice_count": len(revisions), "historical_grade": "original_attachment_indexed; exhaustive_revisions_uncertified"}
        (target / "document_manifest.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf8")
        return document, revisions

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(acquire, b): b for b in banks}
        for future in as_completed(pending):
            bank = pending[future]
            try:
                document, revisions = future.result()
                documents.append(document); revision_notices.extend(revisions)
                print(f"REPORT {bank['code']} {year} {document['publication_date']} {len(documents)}/{len(banks)}", flush=True)
            except Exception as exc:
                gaps.append({"code": bank["code"], "name": bank["name"], "year": year, "reason": str(exc)})
                print(f"REPORT_FAIL {bank['code']} {type(exc).__name__}", flush=True)
    path = folder / "original_report_collection.json"
    path.write_text(json.dumps({"created_at": datetime.now(UTC).isoformat(), "year": year, "released_end": str(released_end),
                               "documents": sorted(documents, key=lambda d: d["code"]), "gaps": gaps, "revision_notices": revision_notices,
                               "expected_banks": len(banks), "new_requests": "official indexes and PDFs only; no Token calls"}, ensure_ascii=False, indent=2), encoding="utf8")
    return path


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--released-end", type=date.fromisoformat, required=True)
    parser.add_argument("--code", action="append", default=[])
    args = parser.parse_args()
    print("collection=" + str(collect(ROOT, args.year, args.released_end, args.code)), flush=True)
