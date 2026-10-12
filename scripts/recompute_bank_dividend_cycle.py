"""Publish corrected bank yields and one coherent bank-sector input version."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts.publish_bank_factor import publish
from scripts.bank_factor_workflow import dependency_task, prepare_sector_inputs
from scripts.bank_processing import ProcessingRequest, execute, resolve_asset
from alpha_research_os.factors.bank_timing import bank_timing_catalog


def main():
    settings=json.loads((ROOT/'config/bank_timing_data.json').read_bytes())
    start,end=date.fromisoformat(settings['start']),date.fromisoformat(settings['released_feature_end'])
    output=ROOT/'reports/bank_sector_research'/(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'-cycle-factor-recalculation')
    output.mkdir(parents=True,exist_ok=False)
    results={'start':str(start),'end':str(end),'stocks':[],'sectors':[],
        'policy':'annual_dividend_cycle_2.0.0; old365 versions preserved, not reused',
        'all_sector_dependencies_same_source':True}
    for factor in ('bank-cash-dividend-yield-365','bank-quality-dividend-yield-365'):
        result=publish(ROOT,start,end,factor)
        results['stocks'].append({'factor_id':factor,**result})
        print(json.dumps({'stock':factor,'release':result['release_id']},ensure_ascii=False),flush=True)
    with dependency_task(ROOT):
        source=prepare_sector_inputs(ROOT,start,end)
        request=ProcessingRequest(stage='INDICATORS',start=start,end=end,source_id=source,
            factor_ids=[i.factor_id for i in bank_timing_catalog()])
        result=execute(ROOT,request,lambda phase,pct:print(f'phase={phase} progress={pct}',flush=True))
        _,manifest=resolve_asset(ROOT,'INDICATORS',result['asset_id'])
        results['sectors']=manifest['summaries']
        results['sector_release_id']=result['asset_id']
        results['source_id']=source
    with (output/'recalculation.json').open('x',encoding='utf-8') as f:
        json.dump(results,f,ensure_ascii=False,indent=2)
    print(json.dumps({'folder':str(output),'sector_release_id':results['sector_release_id'],
        'source_id':source,'sector_count':len(results['sectors'])},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
