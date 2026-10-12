"""Owner-released 2016-2026 dividend research: freeze, outcomes, audit, cleanup.

Only the already archived dividend chart domain is released by this run.
No broad release of 2026 financial statements, baskets, or model holdouts.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import zipfile
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from alpha_research_os.kernel.holdout import ExposureLedger
from scripts.research.research_bank_dividend_rebound import (
    cash_features, labels, past_rank, regression, sha, summary,
)
from scripts.research.research_bank_dividend_entry import stability, capital_path
from scripts.research.research_bank_sector_timing import write_json, holm_adjust, nonoverlap_positions

SOURCE = ROOT / 'reports/bank_sector_research/20261012-074550-dividend-cycle-price'
PRIOR = ROOT / 'reports/bank_sector_research/20261012-090733-dividend-entry'
HORIZONS = (60, 120, 126, 252)
RULES = {
    'yield5': '平均股息率至少5%',
    'high80': '股息率处于此前三年最高20%',
    'high90': '股息率处于此前三年最高10%',
    'high80_stable': '历史高息且分红广度稳定',
    'high80_trend': '历史高息且价格趋势改善',
    'yield5_stable': '至少5%且分红广度稳定',
    'within_high80_stable': '高息区内部：分红稳定与不稳定比较',
    'within_high80_trend': '高息区内部：趋势改善与未改善比较',
}
PROFILES = {'immediate': '一次投入', 'split3': '分三次投入', 'confirm20': '等待趋势确认'}
DEPENDENCIES = ('scripts/research/research_bank_dividend_rebound.py',
    'scripts/research/research_bank_dividend_entry.py',
    'scripts/research/research_bank_sector_timing.py')


def runs(signal):
    """Contiguous true runs; unknown splits a run and never implies a crossing."""
    values = signal.eq(1).to_numpy()
    positions = np.flatnonzero(values)
    if not len(positions):
        return []
    groups = np.split(positions, np.flatnonzero(np.diff(positions) != 1) + 1)
    return [(int(g[0]), int(g[-1])) for g in groups]


def binary(value, known):
    return value.astype(float).where(known)


def prepare():
    folder = ROOT / 'reports/bank_sector_research' / (
        datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S') + '-dividend-decade')
    folder.mkdir(parents=True, exist_ok=False)
    auth = dict(owner_confirmed=True, user_statement='研究下近10年呢 到2026.10月 重新输出报告 旧的删除 多一些图表 特别是哪里高息区间 文字通俗点 结论鲜明点',
        allowed_start='2016-01-01', allowed_end='2026-10-12',
        domain='existing archived individual implemented-dividend yields, raw closes and399986 price index only',
        purpose='descriptive charts and observed forward PRICE returns; no future observations',
        excluded='2026 financial statements, total-return baskets, broad model holdouts',
        exposure_irreversible=True)
    write_json(folder / 'owner_release.json', auth)
    event = ExposureLedger(ROOT / 'data/metadata/bank_chart_view_exposures.ndjson').append(
        actor='project-owner-authorized-assistant', vintage='bank-dividend-price:2016:2026-10',
        purpose=auth['purpose'], capability_id='owner-authorization:sha256:' + sha(folder / 'owner_release.json'))
    write_json(folder / 'exposure.json', dict(event_id=event.event_id, owner_release_sha256=sha(folder / 'owner_release.json')))
    parent = json.loads((SOURCE / 'audit.json').read_bytes())
    names = ('银行平均股息率与价格指数.csv', 'individual_yield_inputs.parquet', 'implemented_dividend_events.parquet')
    for name in names:
        if sha(SOURCE / name) != parent['files'][name]:
            raise ValueError(f'Archived source changed: {name}')
    if parent['chart_end'] > auth['allowed_end'] or parent['yield_policy'] != 'annual_dividend_cycle_2.0.0':
        raise ValueError('Invalid source domain or dividend cycle')
    previous = json.loads((PRIOR / 'experiment_spec.json').read_bytes())['prior_results']
    previous += [dict(path=str(PRIOR / 'results.json'), sha256=sha(PRIOR / 'results.json'), count=40)]
    spec = dict(version='bank-dividend-decade-1.0.0',
        created_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        start=parent['chart_start'], end=parent['chart_end'], exact_ten_year_start='2016-10-10',
        scope='EXPOSED_EXPLORATORY_NOT_OOS_NOT_TRADABLE',
        source=str(SOURCE), sources={n: sha(SOURCE/n) for n in names},
        rules=RULES, horizons=list(HORIZONS), targets=['399986_price_only'],
        models=['raw', 'year_month_price_controls'], family_size=len(RULES)*len(HORIZONS)*2,
        joint_family_size=sum(x['count'] for x in previous)+len(RULES)*len(HORIZONS)*2,
        prior_results=previous,
        yield_policy='implemented annual dividend-cycle cash/current raw close; one annual payment counts once; interim+final count one year',
        coverage='minimum10 known banks and70% of priced historical bank cohort; unknown cash is missing, not0; no yield range clipping',
        percentile='prior756 sessions only; minimum504 valid; midrank ties; current observation excluded',
        absolute_rule='yield>=5% needs coverage only, not percentile warmup; chosen in previous research, not retuned',
        stability='matched-bank prior252-session cash on same share basis; <=25% decline; archived historical reference, not forecast',
        trend='prior20 return>0 and current price>=60-session average; next close entry',
        label='T+1 close to T+1+h close; price only excludes all dividends; tail stays missing',
        events='known0->known1 only; greedy minimum61-session spacing before labels; annual event outcomes may overlap',
        allocation='immediate100%; splitT+1/T+21/T+41 thirds; confirmation first inT..T+20 then next close; cash0return, no confirmation included as0',
        diagnostics='fixed buckets[0,4),[4,5),[5,6),[6,infinity)%; years; equal252-complete sample; exact10year slice; leave-one-year-out; no extra pvalues',
        statistics='Bartlett HAC horizon+20; two-sided; Holm all64new plus420prior; unavailable p conservatively1 for adjustment only',
        source_limits=parent['limits'] + ['current42 archive subset can miss historical departed banks; not certified survivor-free',
            'equal-bank mean differs from market-cap index weighting; price index not directly investable',
            'no expenses, dividend taxes, slippage, liquidity or fill model; not total shareholder return',
            '2026 late signals lack long horizon labels; missing outcomes do not mean failure',
            '2016 full-year overview spans10years9months; exact10year slice is separately shown',
            'manual chart exploration cannot be fully priced into a statistical family'],
        script_sha256=sha(__file__), dependencies={p: sha(ROOT/p) for p in DEPENDENCIES},
        constitution_sha256=sha(ROOT / 'RESEARCH_CONSTITUTION.md'))
    write_json(folder / 'experiment_spec.json', spec)
    d = pd.read_csv(SOURCE/names[0], parse_dates=['session']).set_index('session')
    if d.index.max() > pd.Timestamp(spec['end']) or not d.index.is_unique or not d.index.is_monotonic_increasing:
        raise ValueError('Source calendar invalid')
    panel = pd.read_parquet(SOURCE/names[1]); events = pd.read_parquet(SOURCE/names[2])
    if pd.to_datetime(panel.session).max() > pd.Timestamp(spec['end']):
        raise ValueError('Panel outside release')
    actual = panel.groupby('session').cash_yield_cycle.mean().reindex(d.index)
    np.testing.assert_allclose(actual, d.mean_yield, atol=1e-12, equal_nan=True)
    known = d.valid_banks.ge(10) & (d.valid_banks/d.priced_banks).ge(.7)
    f = d.drop(columns='price_index').copy()
    f['coverage_ready'] = known
    f['yield_rank'] = past_rank(d.mean_yield.where(known))
    f['past_return60'] = d.price_index/d.price_index.shift(60)-1
    f['past_vol60'] = d.price_index.pct_change(fill_method=None).rolling(60, min_periods=60).std()
    f['past_return20'] = d.price_index/d.price_index.shift(20)-1
    ma = d.price_index.rolling(60, min_periods=60).mean()
    f['trend'] = binary(f.past_return20.gt(0)&d.price_index.ge(ma), ma.notna()&f.past_return20.notna())
    f = f.join(stability(panel, events, d.index))
    f['stable'] = binary(f.declining_cash_fraction.le(.25), f.declining_cash_fraction.notna())
    f = f.join(cash_features(panel.rename(columns={'cash_per_current_share_cycle':'cash_per_current_share_365'}), events, d.index))
    f['yield5'] = binary(d.mean_yield.ge(.05), known)
    f['high80'] = binary(f.yield_rank.ge(.8), f.yield_rank.notna())
    f['high90'] = binary(f.yield_rank.ge(.9), f.yield_rank.notna())
    for base, extra, name in [('high80','stable','high80_stable'), ('high80','trend','high80_trend'), ('yield5','stable','yield5_stable')]:
        f[name] = binary(f[base].eq(1)&f[extra].eq(1), f[base].notna()&f[extra].notna())
    for extra in ('stable','trend'):
        f['within_high80_'+extra] = f[extra].where(f.high80.eq(1)&f[extra].notna())
    f.to_parquet(folder/'features.parquet')
    write_json(folder/'feature_manifest.json', dict(spec_sha256=sha(folder/'experiment_spec.json'),
        feature_sha256=sha(folder/'features.parquet'), labels_read=False,
        first_relative_ready=str(f[f.yield_rank.notna()].index.min().date()),
        feature_end=str(f.index.max().date()), counts={r:dict(high=int(f[r].eq(1).sum()),
        other=int(f[r].eq(0).sum()), missing=int(f[r].isna().sum())) for r in RULES}))
    print(json.dumps(dict(folder=str(folder), phase='PREPARED', family=spec['family_size'])))


def evidence(folder):
    spec=json.loads((folder/'experiment_spec.json').read_bytes())
    fm=json.loads((folder/'feature_manifest.json').read_bytes())
    check=[(folder/'experiment_spec.json',fm['spec_sha256']), (folder/'features.parquet',fm['feature_sha256']),
           (Path(__file__),spec['script_sha256']), *[(ROOT/p,d) for p,d in spec['dependencies'].items()],
           *[(Path(spec['source'])/p,d) for p,d in spec['sources'].items()]]
    for p,digest in check:
        if sha(p)!=digest: raise ValueError(f'Frozen evidence changed: {p}')
    f=pd.read_parquet(folder/'features.parquet')
    d=pd.read_csv(Path(spec['source'])/'银行平均股息率与价格指数.csv',parse_dates=['session']).set_index('session')
    if not f.index.equals(d.index): raise ValueError('Calendar differs')
    return spec,f,d


def analyse(folder):
    spec,f,d=evidence(folder)
    if (folder/'labels.parquet').exists() or (folder/'results.json').exists():
        raise ValueError('Result already exists; prepare a new run')
    lab=labels(d.price_index,f.index,horizons=HORIZONS)
    lab.to_parquet(folder/'labels.parquet',index=False)
    by_h={h:lab[lab.horizon.eq(h)].set_index('session').reindex(f.index) for h in HORIZONS}
    common=by_h[252]['return'].notna()
    tests=[];groups=[];buckets=[];chronology=[];leaveouts=[];entries=[];zones=[];allocations=[]
    for rule in RULES:
        for a,b in runs(f[rule]) if not rule.startswith('within_') else []:
            zones.append(dict(rule=rule,start=f.index[a],end=f.index[b],sessions=b-a+1,
                yield_min=f.mean_yield.iloc[a:b+1].min(),yield_max=f.mean_yield.iloc[a:b+1].max(),
                yield_mean=f.mean_yield.iloc[a:b+1].mean(),price_change=d.price_index.iloc[b]/d.price_index.iloc[a]-1,
                ongoing=bool(b==len(f)-1),known_crossing=bool(a>0 and f[rule].iloc[a-1]==0)))
        high=f[rule].eq(1);other=f[rule].eq(0)
        anchors=nonoverlap_positions((high&f[rule].shift(1).eq(0)).to_numpy(),60)
        for h,y in by_h.items():
            for sample,mask in [('all',pd.Series(True,index=f.index)),('year_complete',common),
                ('exact_ten_year',pd.Series(f.index>=pd.Timestamp(spec['exact_ten_year_start']),index=f.index))]:
                for name,flag in [('high',high),('other',other)]:
                    groups.append(dict(rule=rule,horizon=h,sample=sample,group=name,**summary(y,flag&mask,h)))
            for controlled in (False,True):
                tests.append(dict(rule=rule,horizon=h,model='controlled' if controlled else 'raw',
                    **regression(f[rule],y['return'],f,h+20,controlled)))
            for year in range(2016,2027):
                mask=f.index.year==year
                chronology.append(dict(rule=rule,horizon=h,year=year,
                    high=summary(y,high&mask,h),other=summary(y,other&mask,h)))
                if rule in ('yield5','high80'):
                    keep=~mask;ah=y.loc[high&keep,'return'].dropna();al=y.loc[other&keep,'return'].dropna()
                    leaveouts.append(dict(rule=rule,horizon=h,excluded_year=year,high_n=len(ah),other_n=len(al),difference=ah.mean()-al.mean()))
            if rule in ('yield5','high80','high90'):
                for i in anchors:
                    entries.append(dict(rule=rule,signal=f.index[i],**y.iloc[i].to_dict()))
                    if rule in ('yield5','high80') and y.iloc[i].status=='READY':
                        for profile in PROFILES:
                            path=capital_path(d.price_index.to_numpy(),i,h,profile,f.trend.to_numpy())
                            allocations.append(dict(rule=rule,horizon=h,profile=profile,signal=f.index[i],exit=y.iloc[i].exit,**path))
    for h,y in by_h.items():
        for a,b in [(0,.04),(.04,.05),(.05,.06),(.06,np.inf)]:
            flag=f.coverage_ready & f.mean_yield.ge(a)&f.mean_yield.lt(b)
            buckets.append(dict(horizon=h,lower=a,upper=b if np.isfinite(b) else None,**summary(y,flag,h)))
    prior=[]
    for item in spec['prior_results']:
        if sha(item['path'])!=item['sha256']: raise ValueError('Prior result changed')
        old=json.loads(Path(item['path']).read_bytes());rows=old.get('tests',old.get('single_tests',[])+old.get('combinations',[]))
        if len(rows)!=item['count']:raise ValueError('Prior family differs')
        prior.extend(rows)
    if len(tests)!=spec['family_size']: raise ValueError('Incomplete family')
    local=holm_adjust([x['p_value'] for x in tests])
    joint=holm_adjust([x['p_value'] for x in prior+tests])[len(prior):]
    for t,p,q in zip(tests,local,joint): t.update(holm_current=p,holm_joint=q)
    alloc=pd.DataFrame(allocations);allocation_summary=[]
    for (rule,h,profile),g in alloc.groupby(['rule','horizon','profile']):
        allocation_summary.append(dict(rule=rule,horizon=int(h),profile=profile,n=len(g),filled=int(g.filled.sum()),
            mean_return=g.capital_return.mean(),mean_drawdown=g.drawdown.mean(),mean_exposure=g.average_invested_fraction.mean()))
    ef=pd.DataFrame(entries);entry_summary=[]
    for (rule,h),g in ef.groupby(['rule','horizon']):
        good=g[g.status.eq('READY')]
        entry_summary.append(dict(rule=rule,horizon=int(h),signals=len(g),complete=len(good),
            mean_return=good['return'].mean(),win_rate=good['return'].gt(0).mean(),
            mean_drawdown=good.drawdown.mean(),worst_return=good['return'].min()))
    annual=[]
    for year,g in f.groupby(f.index.year):
        annual.append(dict(year=int(year),days=len(g),mean=g.mean_yield.mean(),minimum=g.mean_yield.min(),maximum=g.mean_yield.max(),
            yield5_days=int(g.yield5.eq(1).sum()),relative_days=int(g.high80.eq(1).sum()),relative_known=int(g.high80.notna().sum()),
            minimum_banks=int(g.valid_banks.min()),maximum_banks=int(g.valid_banks.max())))
    result=dict(scope=spec['scope'],start=spec['start'],end=spec['end'],groups=groups,tests=tests,buckets=buckets,
        chronology=chronology,leave_one_year_out=leaveouts,entry_summary=entry_summary,
        allocation_summary=allocation_summary,annual=annual,family_size=len(tests),joint_family_size=spec['joint_family_size'],
        latest=dict(session=str(f.index[-1].date()),yield_mean=f.mean_yield.iloc[-1],yield_median=f.median_yield.iloc[-1],
            rank=f.yield_rank.iloc[-1],high80=f.high80.iloc[-1],yield5=f.yield5.iloc[-1],valid_banks=int(f.valid_banks.iloc[-1]),
            priced_banks=int(f.priced_banks.iloc[-1]),price_index=d.price_index.iloc[-1],
            declining_fraction=f.declining_cash_fraction.iloc[-1]))
    write_json(folder/'results.json',result)
    for name,rows in [('zones',zones),('entry_episodes',entries),('allocation_paths',allocations),
        ('group_statistics',groups),('hypothesis_statistics',tests),('yield_buckets',buckets),('annual_statistics',annual),('leave_one_year_out',leaveouts)]:
        pd.DataFrame(rows).to_csv(folder/(name+'.csv'),index=False,encoding='utf-8-sig')
    write_json(folder/'audit.json',dict(files={p.name:sha(p) for p in folder.iterdir() if p.is_file()},
        max_label_exit=str(lab.exit.max().date()),owner_release_registered=True,no_other2026_domain_read=True))
    print(json.dumps(dict(folder=str(folder),phase='COMPLETE',family=len(tests),joint=spec['joint_family_size'])))


def verify(folder):
    spec,f,d=evidence(folder);audit=json.loads((folder/'audit.json').read_bytes())
    for name,digest in audit['files'].items():
        if sha(folder/name)!=digest:raise ValueError('Evidence artifact changed: '+name)
    lab=pd.read_parquet(folder/'labels.parquet');levels=d.price_index.to_numpy();positions={v:i for i,v in enumerate(f.index)}
    n=0
    for row in lab.itertuples():
        a=positions[row.session]+1;b=a+row.horizon
        if b>=len(d):assert row.status=='TAIL' and pd.isna(lab.loc[row.Index,'return'])
        else:
            assert row.entry==f.index[a] and row.exit==f.index[b]
            path=levels[a:b+1]
            np.testing.assert_allclose(lab.loc[row.Index,'return'],path[-1]/path[0]-1,atol=1e-13)
            running=np.maximum.accumulate(path)
            np.testing.assert_allclose(row.drawdown,np.min(path/running-1),atol=1e-13)
        n+=1
    r=json.loads((folder/'results.json').read_bytes());checked=0
    for g in r['groups']:
        y=lab[lab.horizon.eq(g['horizon'])].set_index('session')['return'].reindex(f.index)
        mask=f[g['rule']].eq(1 if g['group']=='high' else 0)&y.notna()
        if g['sample']=='year_complete': mask &= lab[lab.horizon.eq(252)].set_index('session')['return'].reindex(f.index).notna()
        if g['sample']=='exact_ten_year':mask &= f.index>=pd.Timestamp(spec['exact_ten_year_start'])
        assert mask.sum()==g['days']
        if mask.any():np.testing.assert_allclose(y[mask].mean(),g['mean_return'],atol=1e-13)
        checked+=1
    panel=pd.read_parquet(Path(spec['source'])/'individual_yield_inputs.parquet')
    independent=panel.assign(rebuilt=panel.cash_per_current_share_cycle/panel.close)
    np.testing.assert_allclose(independent.rebuilt,independent.cash_yield_cycle,equal_nan=True,atol=1e-13)
    aggregate=independent.groupby('session').rebuilt.mean().reindex(f.index)
    np.testing.assert_allclose(aggregate,f.mean_yield,equal_nan=True,atol=1e-13)
    for i in list(range(504,len(f),127))+[len(f)-1]:
        history=f.mean_yield.where(f.coverage_ready).iloc[max(0,i-756):i].dropna()
        if len(history)>=504:
            expected=((history<f.mean_yield.iloc[i]).sum()+.5*(history==f.mean_yield.iloc[i]).sum())/len(history)
            np.testing.assert_allclose(f.yield_rank.iloc[i],expected,atol=1e-13)
    entries=pd.read_csv(folder/'entry_episodes.csv',parse_dates=['signal'])
    for rule,g in entries.groupby('rule'):
        days=sorted(g.signal.unique());idx=[positions[pd.Timestamp(day)] for day in days]
        assert all(b-a>=61 for a,b in zip(idx,idx[1:]))
        for i in idx:assert f[rule].iloc[i]==1 and f[rule].iloc[i-1]==0
    allocation_checks=0
    for row in pd.read_csv(folder/'allocation_paths.csv',parse_dates=['signal','exit']).itertuples():
        i=positions[row.signal];a=i+1;b=a+row.horizon
        if row.profile=='immediate':jobs={a:1.}
        elif row.profile=='split3':jobs={a:1/3,a+20:1/3,a+40:1/3}
        else:
            j=next((j for j in range(i,i+21) if f.trend.iloc[j]==1),None)
            jobs={} if j is None else {j+1:1.}
        nav=np.ones(b-a+1)
        for j,w in jobs.items():nav[j-a:]+=w*(levels[j:b+1]/levels[j]-1)
        np.testing.assert_allclose([row.capital_return,row.drawdown],
            [nav[-1]-1,np.min(nav/np.maximum.accumulate(nav)-1)],atol=1e-13)
        allocation_checks+=1
    checks=dict(status='PASS',label_checks=n,group_checks=checked,individual_yield_checks=len(panel),
        aggregate_dates=len(f),rank_past_only_checked=True,entry_crossings_checked=True,
        allocation_checks=allocation_checks,source_hashes_verified=True,end=spec['end'])
    write_json(folder/'independent_audit.json',checks)
    print(json.dumps(checks))


def cleanup(folder):
    """Keep immutable evidence and archived report bytes; remove old readable copies."""
    manifest=json.loads((folder/'pdf_manifest.json').read_bytes());final=Path(manifest['path']).resolve()
    if sha(final)!=manifest['sha256']:raise ValueError('Replacement PDF changed')
    root=(ROOT/'output/pdf').resolve()
    candidates=sorted(p for p in root.glob('银行板块高股息率与入场研究_20261012_090733*.pdf') if p.resolve()!=final)
    history=ROOT/'reports/bank_sector_research/history';history.mkdir(exist_ok=True)
    archive=history/(folder.name+'-superseded-pdf.zip')
    if archive.exists():raise ValueError('Archive exists')
    records=[]
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for p in candidates:
            records.append(dict(path=str(p),sha256=sha(p),member=p.name,size=p.stat().st_size))
            z.write(p,p.name)
    with zipfile.ZipFile(archive) as z:
        import hashlib
        for item in records:
            assert hashlib.sha256(z.read(item['member'])).hexdigest()==item['sha256']
    write_json(history/(folder.name+'-superseded-pdf.json'),dict(archive=str(archive),archive_sha256=sha(archive),
        replacement=manifest,old_reports=records,authorization='user:旧的删除; original bytes archived for RULE-026/027'))
    for p in candidates:
        resolved=p.resolve()
        if resolved.parent!=root or sha(resolved)!=next(x['sha256'] for x in records if x['path']==str(p)):
            raise ValueError('Unsafe or changed deletion target')
        resolved.unlink()
    write_json(folder/'cleanup_complete.json',dict(removed=len(records),archive=str(archive),final_retained=final.exists()))
    print(json.dumps(dict(removed=len(records),final=str(final))))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['prepare','analyse','verify','cleanup'])
    parser.add_argument('folder',type=Path,nargs='?')
    args=parser.parse_args()
    if args.phase=='prepare':prepare()
    else:
        if args.folder is None:parser.error('folder required')
        {'analyse':analyse,'verify':verify,'cleanup':cleanup}[args.phase](args.folder.resolve())
