"""Fill absent annual bank facts from hashed, publication-indexed original reports.

Provider values are cross-check candidates only. Existing facts are never replaced,
and a later report's comparative column is never backdated to the prior year.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from alpha_research_os.kernel.canonical import content_hash  # noqa: E402
from scripts.bank_factor_inputs import METRICS, source_inputs  # noqa: E402

PATTERNS = {
    "ordinary_eps_basic": r"(?:基本(?:及|和|[/／])?稀释每股收益|基本每股收益|每股基本收益)",
    "roe_weighted": r"加权平均(?:净资产收益率|净资产收益|权益回报率|股东权益回报率)",
    "net_interest_margin": r"(?:净息差|净利息收益率)",
    "npl_ratio": r"不良贷款(?:比率|率)",
    "provision_coverage_ratio": r"(?:(?:不良贷款)?拨备覆盖率|贷款减值准备对不良贷款比率)",
    "cet1_ratio": r"核心一级资本充足率",
    "common_bvps": r"每股净资产",
    "parent_profit": r"归属于[^\n]{0,35}?(?:净利润|净利潤)",
}
CANDIDATE_FIELDS = {
    "ordinary_eps_basic": "tushare_eps", "roe_weighted": "tushare_roe_waa_pct",
    "common_bvps": "valuation_common_bvps", "parent_profit": "tushare_parent_profit",
    **{m: m for m in ("net_interest_margin", "npl_ratio", "provision_coverage_ratio", "cet1_ratio")},
}


def numbers(text):
    text = re.sub(r"(?<=\d)[,，](?=\d)", "", text)
    return [float(v) for v in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text)]


def current_header(context, year):
    lines = context.splitlines()
    for stop in range(len(lines), 0, -1):
        for width in (1, 2, 3):
            line = " ".join(lines[max(0, stop - width):stop])
            if len(line) > 180 or re.search(r"同比|发行|准则|公告|董事会|报告期内", line):
                continue
            years = list(dict.fromkeys(re.findall(r"20\d{2}", line)))
            if str(year) in years and str(year - 1) in years:
                return years[0] == str(year) and years[1] == str(year - 1)
    return False


def logical_lines(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])', '', text)
    lines = [v.strip() for v in text.splitlines() if v.strip()]
    result = []
    for line in lines:
        # Join broken Chinese labels, but never join a numeric cell to another row.
        if (result and not numbers(result[-1])
                and len(result[-1]) < 30 and len(line) < 80
                and re.match(r"^[\u4e00-\u9fff]", line)):
            result[-1] += line
        else:
            result.append(line)
    return result


def extract_cell(pages, year, metric, expected, reviewed_pages=()):
    """Return only a first current-year table cell matching an independent candidate."""
    if expected is None or not np.isfinite(expected):
        return None
    matches = []
    for no, text in enumerate(pages, 1):
        lines = logical_lines(text)
        nearby = "\n".join(pages[max(0, no - 2):no + 1])
        for j, line in enumerate(lines):
            match = re.search(PATTERNS[metric], line)
            if not match or re.search(r"扣除非经常|扣非", line):
                continue
            preceding = logical_lines(pages[no - 2])[-60:] if no > 1 else []
            before = "\n".join(preceding + lines[max(0, j - 60):j])
            single_margin = (metric == 'net_interest_margin' and len(line) < 45
                             and '生息资产' in before and re.search(r'利息收入|利息净收入|利息收支', before)
                             and any(re.fullmatch(str(year) + r'\s*年(?:[（(].*[）)])?', v.strip())
                                     for v in before.splitlines()[-65:]))
            relative_capital = (
                metric == 'cet1_ratio'
                and re.search(rf'{year}\s*年\s*年度报告', text)
                and re.search(r'报告期末\s*上年末', before + line[:match.start()])
                and re.search(r'项目\s*本集团\s*本行\s*本集团\s*本行', before + line[:match.start()])
            )
            reviewed_header = (no in reviewed_pages and
                               re.search(rf'(?:{year}\s*(?:年年度报告|年度报告|年年报|年报)'
                                         rf'|公司\s*{year}\s*年度分别按照)',
                                         text + '\n' + '\n'.join(pages[:3])))
            if not current_header(before, year) and not single_margin and not relative_capital and not reviewed_header:
                continue
            tail = line[match.end():]
            # Footnote markers are typography, not numeric financial cells.
            tail = re.sub(r"^\s*[（(]\d+(?:[,，]\d+)*[）)]", "", tail)
            if re.match(r"^\d{1,2}(?![\d.])", tail):
                tail = re.sub(r"^\d{1,2}(?![\d.])", "", tail, count=1)
            marker_only = re.fullmatch(r'\s*(\d{1,2})\s*', tail)
            if marker_only and re.search(
                r'(?:^|\n)\s*'+marker_only[1]+r'\s*[、.．]\s*\S', nearby
            ):
                tail = ''
            for following in lines[j + 1:j + 4]:
                if numbers(tail):
                    break
                if re.fullmatch(r"[（(]\d+[）)]", following):
                    continue
                tail += " " + following
            tail = re.sub(r"^[\s（(]*[%％]?[）)]?\s*[≤≥<>]\s*\d+(?:\.\d+)?", "", tail)
            tail = re.sub(r"^[\s（(]*[%％][）)]?", "", tail)
            values = numbers(tail)
            if len(values) < (1 if single_margin else 2):
                continue
            marker = re.match(r"^\s*(\d{1,2})(?![\d.])\s+", tail)
            if (marker and values[0] == int(marker[1]) and len(values) > 2
                    and re.search(r"(?:^|\n)\s*" + marker[1] + r"\s*[、.．]\s*\S", nearby)):
                values = values[1:]
            prefix = re.split(r"\d", tail, maxsplit=1)[0]
            if re.search(r"同比|下降|增长|提高|个百分点|保持", prefix):
                continue
            context = before + "\n" + "\n".join(lines[j:j + 4])
            if metric == "common_bvps" and not re.search(
                r"普通股[^\n]{0,60}每股净资产|每股净资产[^\n]{0,130}普通股", nearby
            ):
                continue
            factors = [1.0]
            if metric == "parent_profit":
                factors = [scale for scale, unit in ((1e6, "百万元"), (1e3, "千元"),
                           (1e4, "万元"), (1e8, "亿元"), (1.0, "单位：元"))
                           if unit in text[:2500] or unit in context]
            scales = [scale for scale in factors
                      if abs(values[0] * scale - expected) <=
                      (max(scale * .51, abs(expected) * .00001)
                       if metric == "parent_profit" else .0051)]
            if len(set(scales)) != 1:
                continue
            scale = scales[0]
            matches.append(dict(pdf_page=no, snippet="\n".join(lines[j:j + 4]), context=context,
                                value=values[0] * scale,
                                previous_value=values[1] * scale if len(values) > 1 else None,
                                original_display_value=values[0], original_display_unit_factor=scale))
    if not matches:
        return None
    if max(m["value"] for m in matches) - min(m["value"] for m in matches) > .0051:
        return None
    return matches[0]


def ocr_text(root, document, page, no, engine):
    """Cache immutable OCR evidence with cell coordinates; never use OCR without reconciliation."""
    key = content_hash({'pdf': document['sha256'], 'page': no, 'engine': 'rapidocr-onnxruntime-1.2.3',
                        'scale': 2, 'grouping': 1}).removeprefix('sha256:')
    folder = root / 'data/features/bank_original_evidence/ocr'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + '.json')
    if path.exists():
        return json.loads(path.read_bytes())['text']
    bitmap = page.render(scale=2)
    try:
        cells, _ = engine(np.asarray(bitmap.to_pil().convert('RGB')))
    finally:
        bitmap.close()
    groups = []
    for box, text, score in sorted(cells or [], key=lambda cell: (np.mean(np.asarray(cell[0])[:, 1]), cell[0][0][0])):
        if float(score) < .60:
            continue
        y = float(np.mean(np.asarray(box)[:, 1]))
        group = next((g for g in reversed(groups) if abs(g['y'] - y) <= 12), None)
        if group is None:
            group = dict(y=y, cells=[])
            groups.append(group)
        group['cells'].append((float(box[0][0]), text))
    text = '\n'.join(' '.join(t for _, t in sorted(g['cells'])) for g in groups)
    path.write_text(json.dumps(dict(pdf_sha256=document['sha256'], pdf_page=no, text=text, cells=cells,
                                    availability='original publication; OCR is only a transcription'),
                               ensure_ascii=False, indent=2), encoding='utf-8')
    return text


def supplement(root, collection, end, ocr_requests=(), codes=(), reviewed_cells=()):
    import pypdfium2 as pdfium

    settings = json.loads((root / "config/bank_timing_data.json").read_bytes())
    if end > date.fromisoformat(settings["released_feature_end"]):
        raise ValueError("禁止读取未释放研究区间")
    facts, *_ = source_inputs(root, date.fromisoformat(settings["feature_warmup_start"]), end)
    existing = {(r.code, r.report_date, r.metric) for r in facts.itertuples()}
    with duckdb.connect(str(root / "data/warehouse/bank_token.duckdb"), read_only=True) as c:
        panel = c.execute("SELECT * FROM bank_primary_research_panel WHERE try_cast(report_date AS DATE)<=?",
                          [end]).df()
    with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as c:
        eps = c.execute("""SELECT ts_code,end_date,try_cast(basic_eps AS DOUBLE) eps
            FROM raw.income_statement_versions WHERE end_date<=? AND coalesce(f_ann_date,ann_date)<=?
            AND report_type='1' AND basic_eps IS NOT NULL AND ts_code IN
            (SELECT DISTINCT ts_code FROM research.sw_industry_membership WHERE l1_name='银行')""",
                        [end.strftime('%Y%m%d'), end.strftime('%Y%m%d')]).df()
    documents = json.loads(collection.read_bytes())["documents"]
    records, gaps, used = [], [], []
    ocr_engine = None
    ocr_selection = {}
    for request in ocr_requests:
        code, year, bounds = request.split(':')
        low, high = map(int, bounds.split('-'))
        ocr_selection[(code, int(year))] = set(range(low, high + 1))
    for document in documents:
        year, code = int(document['year']), document['code']
        if codes and code not in codes:
            continue
        report_date = date(year, 12, 31)
        published = date.fromisoformat(document['publication_date'])
        if report_date > end or published > end:
            continue
        missing = [metric for metric in PATTERNS if (code, report_date, metric) not in existing]
        growth_missing = (code, report_date, "profit_growth_direct") not in existing
        if growth_missing and 'parent_profit' not in missing:
            missing.append('parent_profit')
        if not missing:
            continue
        path = Path(document['source_file'])
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != document['sha256']:
            raise ValueError("原始年报文件缺失或哈希不一致")
        rows = panel[(panel.code == code) & (panel.report_date.astype(str) == str(report_date))]
        candidates = rows.iloc[0].to_dict() if len(rows) == 1 else {}
        pdf = pdfium.PdfDocument(path)
        pages = []
        try:
            for n in range(min(130, len(pdf))):
                page = pdf[n]
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                    if n + 1 in ocr_selection.get((code, year), set()):
                        if ocr_engine is None:
                            from rapidocr_onnxruntime import RapidOCR

                            ocr_engine = RapidOCR()
                        print(json.dumps(dict(code=code,year=year,ocr_page=n+1)),flush=True)
                        text = ocr_text(root, document, page, n + 1, ocr_engine)
                    pages.append(text)
                finally:
                    textpage.close()
                    page.close()
        finally:
            pdf.close()
        added = 0
        for metric in missing:
            expected = candidates.get(CANDIDATE_FIELDS[metric])
            if metric == 'ordinary_eps_basic' and not pd.notna(expected):
                values = eps[(eps.ts_code == code) & (eps.end_date == f'{year}1231')].eps.dropna().unique()
                expected = float(values[0]) if len(values) == 1 else None
            review = next((item for item in reviewed_cells if item['code'] == code
                           and item['year'] == year and item['metric'] == metric), None)
            if review and review['source_sha256'] != document['sha256']:
                raise ValueError('人工复核记录与原始年报哈希不一致')
            found = extract_cell(pages, year, metric, review['value'] if review else expected,
                                 (review['pdf_page'],) if review else ())
            if review and found is not None and found['pdf_page'] != review['pdf_page']:
                found = None
            if found is None:
                gaps.append(dict(code=code, report_date=str(report_date), metric=metric,
                                 reason='原始当年表格列、单位或候选值尚未核对通过'))
                continue
            base = dict(code=code, name=document['name'], report_date=str(report_date),
                        publication_date=str(published), available_at=str(published)+'T23:59:59+08:00',
                        publication_evidence_verified=True, original_current_year_column_reconciled=True,
                        first_disclosure_exhaustive_certified=False, source='issuer_original_report',
                        source_file=str(path), source_url=document['url'], source_sha256=document['sha256'],
                        evidence_grade='ORIGINAL_CURRENT_ANNUAL_COLUMN_GAP_SUPPLEMENT',
                        scope_status='group_reported_definition', uses_next_year_comparative=False,
                        review_method='current-year-first header, original numeric cell and independent candidate')
            if review:
                base['review_method'] = 'visually reconciled original current-year column; provider is diagnostic only'
                base['review_note'] = review['reason']
            if (code, report_date, metric) not in existing:
                unit = 'CNY' if metric == 'parent_profit' else 'CNY/share' if metric in (
                    'ordinary_eps_basic', 'common_bvps') else 'percent'
                records.append(dict(base, **found, metric=metric, unit=unit,
                                    definition_id=metric+'_annual_original'))
                added += 1
            if metric == 'parent_profit' and growth_missing and found['previous_value'] > 0:
                growth = dict(found, value=found['value']/found['previous_value']-1)
                records.append(dict(base, **growth, metric='profit_growth_direct', unit='ratio',
                                    definition_id='same_report_parent_profit_yoy'))
                added += 1
        if added:
            used.append({k: document[k] for k in ('code','year','publication_date','sha256','source_file','url')})
        print(json.dumps(dict(code=code, year=year, added=added), ensure_ascii=True), flush=True)
    frame = pd.DataFrame(records)
    if frame.empty:
        raise ValueError('未发现通过核对的补充事实')
    if not set(frame.metric).issubset(METRICS) or frame.duplicated(['code','report_date','metric']).any():
        raise ValueError('补充事实键或指标无效')
    identity = content_hash({'engine': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                             'collection': hashlib.sha256(collection.read_bytes()).hexdigest(),
                             'facts': frame.to_json(orient='records'), 'end': str(end),
                             'ocr_requests': ocr_requests, 'codes': codes, 'reviewed_cells': reviewed_cells})
    folder = root / 'data/features/bank_original_evidence' / identity.removeprefix('sha256:')
    if not folder.exists():
        folder.mkdir(parents=True)
        frame.to_parquet(folder/'supplemental_financial_facts.parquet',index=False)
        evidence = dict(asset_id=identity,created_at=datetime.now(UTC).isoformat(),end=str(end),
                        source_collection=str(collection),sources=used,remaining=gaps,
                        counts=frame.metric.value_counts().to_dict(),existing_values_replaced=0,
                        reviewed_cells=reviewed_cells,
                        historical_grade=('RESEARCH_ONLY; original publication reconciled; '
                                          'exhaustive revisions uncertified'))
        (folder/'source_manifest.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    return folder


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection',type=Path,required=True)
    parser.add_argument('--end',type=date.fromisoformat,default=date(2025,12,31))
    parser.add_argument('--ocr-pages', action='append', default=[], metavar='CODE:YEAR:FIRST-LAST',
                        help='Explicit original PDF pages requiring image transcription and visual review')
    parser.add_argument('--code', action='append', default=[], help='Limit a follow-up to an incomplete bank')
    parser.add_argument('--reviewed-cells', type=Path,
                        help='Hash-bound, visually reviewed current-year cells when API candidates disagree')
    args=parser.parse_args()
    reviews = json.loads(args.reviewed_cells.read_bytes()) if args.reviewed_cells else []
    print('supplement_folder='+str(supplement(ROOT,args.collection,args.end,args.ocr_pages,args.code,reviews)),flush=True)
