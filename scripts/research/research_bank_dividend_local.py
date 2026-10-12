"""Frozen follow-up: local 120/252-session high yields and suspicious-result audit."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts.research.research_bank_dividend_decade import evidence, HORIZONS
from scripts.research.research_bank_dividend_rebound import past_rank, regression, summary, sha
from scripts.research.research_bank_sector_timing import holm_adjust, write_json


def prepare(base):
    spec,f,_=evidence(base)
    folder=ROOT/'reports/bank_sector_research'/(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'-dividend-local')
    folder.mkdir(parents=True,exist_ok=False)
    config=dict(version='local-dividend-1.0.0',base=str(base),base_feature_sha256=sha(base/'features.parquet'),
        base_spec_sha256=sha(base/'experiment_spec.json'),base_label_sha256=sha(base/'labels.parquet'),
        prior_results=spec['prior_results']+[dict(path=str(base/'results.json'),sha256=sha(base/'results.json'),count=64)],
        scope=spec['scope'],owner_release_sha256=sha(base/'owner_release.json'),
        rules={'local120':'此前半年股息率最高20%','local252':'此前一年股息率最高20%'},
        windows=[120,252],percentile=.8,current_excluded=True,minimum='entire window valid',
        horizons=list(HORIZONS),models=['raw','controlled'],family_size=16,joint_family_size=spec['joint_family_size']+16,
        purpose='Explore the already viewed2026 local-high example; these are additional searches, not independent verification',
        no_rule_retuning_after_labels=True,script_sha256=sha(__file__))
    write_json(folder/'experiment_spec.json',config)
    known=f.mean_yield.where(f.coverage_ready)
    for window in config['windows']:
        rank=past_rank(known,window=window,minimum=window)
        f[f'local{window}_rank']=rank
        f[f'local{window}']=rank.ge(.8).astype(float).where(rank.notna())
    f.to_parquet(folder/'features.parquet')
    write_json(folder/'feature_manifest.json',dict(spec_sha256=sha(folder/'experiment_spec.json'),feature_sha256=sha(folder/'features.parquet'),labels_read=False))
    print(json.dumps(dict(folder=str(folder),phase='PREPARED',family=16)))


def analyse(folder):
    spec=json.loads((folder/'experiment_spec.json').read_bytes());fm=json.loads((folder/'feature_manifest.json').read_bytes());base=Path(spec['base'])
    for p,digest in [(Path(__file__),spec['script_sha256']),(folder/'experiment_spec.json',fm['spec_sha256']),
        (folder/'features.parquet',fm['feature_sha256']),(base/'labels.parquet',spec['base_label_sha256'])]:
        assert sha(p)==digest,str(p)
    if (folder/'results.json').exists():raise ValueError('Immutable result exists')
    f=pd.read_parquet(folder/'features.parquet');lab=pd.read_parquet(base/'labels.parquet')
    tests=[];groups=[];annual=[]
    for rule in spec['rules']:
        for h in HORIZONS:
            y=lab[lab.horizon.eq(h)].set_index('session').reindex(f.index)
            for name,flag in [('high',f[rule].eq(1)),('other',f[rule].eq(0))]:
                groups.append(dict(rule=rule,horizon=h,group=name,**summary(y,flag,h)))
            for controlled in (False,True):
                tests.append(dict(rule=rule,horizon=h,model='controlled' if controlled else 'raw',**regression(f[rule],y['return'],f,h+20,controlled)))
            for year in range(2016,2027):
                mask=f.index.year==year
                annual.append(dict(rule=rule,horizon=h,year=year,high=summary(y,f[rule].eq(1)&mask,h),other=summary(y,f[rule].eq(0)&mask,h)))
    prior=[]
    for item in spec['prior_results']:
        assert sha(item['path'])==item['sha256']
        r=json.loads(Path(item['path']).read_bytes());rows=r.get('tests',r.get('single_tests',[])+r.get('combinations',[]))
        assert len(rows)==item['count'];prior+=rows
    joint=holm_adjust([t['p_value'] for t in prior+tests])
    for t,q in zip(tests,joint[len(prior):]):t['holm_joint']=q
    base_results=json.loads((base/'results.json').read_bytes())
    updated_prior=base_results['tests']
    for t,q in zip(updated_prior,joint[len(prior)-64:len(prior)]):t['holm_joint']=q
    write_json(folder/'results.json',dict(scope=spec['scope'],family_size=16,joint_family_size=spec['joint_family_size'],
        groups=groups,tests=tests,annual=annual,base_tests_with_expanded_correction=updated_prior,
        latest_local_ranks={r:float(f[r+'_rank'].iloc[-1]) for r in spec['rules']}))
    # Independently rebuild score-based HAC, including year/month fixed effects.
    model_checks=0;group_checks=0
    for t in updated_prior+tests:
        if t['coefficient'] is None:continue
        y=lab[lab.horizon.eq(t['horizon'])].set_index('session')['return'].reindex(f.index).to_numpy()
        signal=f[t['rule']].to_numpy();cols=[np.ones(len(f)),signal]
        if t['model']=='controlled':
            eligible=np.isfinite(signal)&np.isfinite(y)&f.past_return60.notna().to_numpy()&f.past_vol60.notna().to_numpy()
            years=sorted(set(f.index.year[eligible]))
            cols += [(f.index.year==year).astype(float) for year in years[1:]]
            cols += [(f.index.month==month).astype(float) for month in range(2,13)]
            cols += [f.past_return60.to_numpy(),f.past_vol60.to_numpy()]
        X=np.column_stack(cols);valid=np.isfinite(y)&np.isfinite(X).all(axis=1)
        active=np.r_[True,True,np.abs(X[valid,2:]).sum(axis=0)>0];X=X[:,active]
        beta=np.linalg.lstsq(X[valid],y[valid],rcond=None)[0]
        bread=np.linalg.inv(X[valid].T@X[valid]);influence=np.zeros(len(f))
        influence[valid]=(X[valid]@bread[1])*(y[valid]-X[valid]@beta)
        variance=np.dot(influence,influence);lag=t['horizon']+20
        for k in range(1,lag+1):variance+=2*(1-k/(lag+1))*np.dot(influence[k:],influence[:-k])
        se=math.sqrt(max(0,variance*valid.sum()/(valid.sum()-X.shape[1])))
        np.testing.assert_allclose([beta[1],se],[t['coefficient'],t['se']],atol=1e-10)
        model_checks+=1
    for g in groups:
        y=lab[lab.horizon.eq(g['horizon'])].set_index('session')['return'].reindex(f.index)
        mask=f[g['rule']].eq(1 if g['group']=='high' else 0)&y.notna()
        assert mask.sum()==g['days']
        if mask.any():np.testing.assert_allclose(y[mask].mean(),g['mean_return'],atol=1e-13)
        group_checks+=1
    for rule,window in [('local120',120),('local252',252)]:
        for i in [window, min(window+400,len(f)-1),len(f)-1]:
            history=f.mean_yield.where(f.coverage_ready).iloc[i-window:i].dropna()
            if len(history)==window:
                expected=((history<f.mean_yield.iloc[i]).sum()+.5*(history==f.mean_yield.iloc[i]).sum())/window
                np.testing.assert_allclose(f[rule+'_rank'].iloc[i],expected,atol=1e-13)
    write_json(folder/'independent_audit.json',dict(status='PASS',independent_hac_models=model_checks,group_checks=group_checks,
        local_rank_past_only=True,expanded_family=spec['joint_family_size'],
        suspicious_result_note='Some adjusted HAC associations pass0.05; effective episode count remains small; year controls do not establish causality or OOS validity',
        negative_cash_high80_years=f.loc[f.high80.eq(1)&f.stable.eq(0)].groupby(f.loc[f.high80.eq(1)&f.stable.eq(0)].index.year).size().to_dict(),
        files={p.name:sha(p) for p in folder.iterdir() if p.is_file()}))
    print(json.dumps(dict(folder=str(folder),phase='COMPLETE',family=16,joint=spec['joint_family_size'],hac_checks=model_checks)))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['prepare','analyse']);p.add_argument('folder',type=Path)
    a=p.parse_args();{'prepare':prepare,'analyse':analyse}[a.phase](a.folder.resolve())
