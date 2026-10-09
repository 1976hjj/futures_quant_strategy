"""Read completed browser jobs; write supplementary research only, never change releases."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BATCH = '20261009-171149-5e2cf6'
PACK = ROOT / 'data/factor_store/bank_inputs/d5590f6952b8a34075113c38567b3e56ae30ffa8895ae05172a683c8da0511e5'
OUT = ROOT / 'reports/bank_factor_comparison_20261009.html'
LOW = {'bank-npl-ratio-pit', 'bank-roe-volatility-3y'}
DIAGNOSTIC = {'bank-quality-gate-pit'}
FEATURES = ['book_to_price','cash_yield365','cet1_ratio','annual_earnings_yield','nim_change','npl_improvement','npl_ratio','profit_growth','provision_coverage_ratio','gated_cash_yield365','quality_gate','quality_score','roe_median3','roe_weighted','roe_volatility3']


def read(p):
    return json.loads(p.read_text(encoding='utf-8'))


def inference(values, block=63, repeats=10000):
    """Circular MBB + Bartlett HAC, protecting the overlapping 63-session labels."""
    from math import erfc, sqrt
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < block * 3:
        return {'n': n, 'status': '有效样本不足三个持仓周期'}
    mean = float(x.mean()); centered = x-mean
    lag = min(block,n-1)
    variance = float(centered @ centered)/n
    for k in range(1,lag+1):
        variance += 2*(1-k/(lag+1))*float(centered[k:] @ centered[:-k])/n
    se = sqrt(max(0.,variance)/n)
    p = erfc(abs(mean/se)/sqrt(2)) if se else (1. if mean == 0 else 0.)
    b = min(block,n); full, rem = divmod(n,b)
    doubled=np.concatenate([x,x]); c=np.concatenate([[0.],np.cumsum(doubled)])
    rng=np.random.default_rng(20261009)
    starts=rng.integers(0,n,size=(repeats,full))
    totals=(c[starts+b]-c[starts]).sum(axis=1)
    if rem:
        last=rng.integers(0,n,size=repeats); totals += c[last+rem]-c[last]
    means=totals/n
    return {'n':n,'mean':mean,'hac_lag':lag,'hac_p':p,'bootstrap_block':b,'bootstrap_repeats':repeats,'bootstrap_p':float((np.sum(abs(means-mean)>=abs(mean))+1)/(repeats+1)), 'ci_lower':float(np.quantile(means,.025)),'ci_upper':float(np.quantile(means,.975)), 'status':'历史诊断'}


def bh(p):
    a=np.asarray(p,dtype=float); order=np.argsort(a); m=len(a)
    out=np.empty(m); out[order]=np.minimum(1,np.minimum.accumulate((a[order]*m/np.arange(1,m+1))[::-1])[::-1])
    return out.tolist()


def collect():
    state=read(ROOT/f'reports/factor_batches/{BATCH}.state.json')
    rows=[]
    for item in state['items']:
        row={**item,'declared_direction':'LOW' if item['factor_id'] in LOW else 'HIGH'}
        job=item.get('m4_job_id'); p=ROOT/f'reports/m4_runs/{job}.json'
        if job and p.exists():
            run=read(p)
            row['pipeline_status']=run['status']
            basics=run.get('stages',{}).get('basic_evidence',{}).get('result',[])
            if basics:
                eid=basics[0]['evidence_id'].split(':')[-1]
                folder=ROOT/'data/evidence_store/bundles'/eid
                if (folder/'factor_summary.parquet').exists():
                    row['summary']=pd.read_parquet(folder/'factor_summary.parquet').iloc[0].to_dict()
                    daily=pd.read_parquet(folder/'daily_metrics.parquet')
                    daily['year']=daily.session.astype(str).str[:4]
                    sign=-1 if item['factor_id'] in LOW else 1
                    row['year_rank_ic']=(daily.groupby('year').rank_ic.mean()*sign).to_dict()
                    row['conservative']=inference(daily.rank_ic*sign)
            audit=run.get('stages',{}).get('audit_walk_forward',{}).get('result',{})
            row['walk_forward_findings']=audit.get('findings',[])
        rows.append(row)
    features=pd.read_parquet(PACK/'features.parquet')
    labels_id='1f0881b73d7ebd5743270bc65460f66a48a37048169d4966296eef6a3c76be4f'
    labels=pd.read_parquet(ROOT/'data/evidence_store/labels'/labels_id/'forward_return_labels.parquet')
    assert not features.duplicated(['session','instrument_id']).any()
    assert set(map(tuple,features[['session','instrument_id']].itertuples(index=False,name=None)))==set(map(tuple,labels[['signal_session','instrument_id']].itertuples(index=False,name=None)))
    signal_close=pd.to_datetime(features.session.astype(str),utc=True)+pd.Timedelta(hours=7)
    assert (pd.to_datetime(features.available_at,utc=True)<=signal_close).all()
    merged=features.merge(labels[['signal_session','instrument_id','value','is_valid']],left_on=['session','instrument_id'],right_on=['signal_session','instrument_id'],validate='one_to_one')
    for row,field in zip(rows,FEATURES):
        pairs=merged.loc[merged.is_valid & merged[field].notna(),['session',field,'value']].copy()
        by=pairs.groupby('session',sort=True)
        xr=by[field].rank(method='average'); yr=by.value.rank(method='average')
        dx=xr-xr.groupby(pairs.session).transform('mean'); dy=yr-yr.groupby(pairs.session).transform('mean')
        numerator=(dx*dy).groupby(pairs.session).sum()
        denom=np.sqrt((dx*dx).groupby(pairs.session).sum()*(dy*dy).groupby(pairs.session).sum())
        ic=(numerator/denom).where(by.size()>=10)
        direction=-1 if row['factor_id'] in LOW else 1
        if row.get('summary',{}).get('mean_rank_ic') is not None:
            assert abs(float(ic.mean())-row['summary']['mean_rank_ic'])<1e-10, row['factor_id']
        row['supplemental_label_release_id']='sha256:'+labels_id
        row['supplemental_field']=field
        row['year_rank_ic']=(ic.groupby(ic.index.astype(str).str[:4]).mean()*direction).to_dict()
        row['conservative']=inference(ic*direction)
        row.setdefault('summary',{})['mean_coverage']=float((by.size().reindex(features.session.unique(),fill_value=0)/features.groupby('session').size()).mean())
        row['summary']['mean_rank_ic']=float(ic.mean())
    family=[r for r in rows if r['factor_id'] not in DIAGNOSTIC and 'hac_p' in r.get('conservative',{})]
    for metric in ['hac','bootstrap']:
        for r,q in zip(family,bh([r['conservative'][metric+'_p'] for r in family])):
            r['conservative'][metric+'_family_q']=q
    features['year']=features.session.astype(str).str[:4]
    coverage=features.groupby('year')[FEATURES].agg(lambda s: round(s.notna().mean()*100,1))
    jobs=[]
    for p in sorted((ROOT/'reports/strategy_backtests').glob('*.request.json')):
        request=read(p)
        if not request.get('name','').startswith('银行因子研究·'):
            continue
        job=p.name.removesuffix('.request.json')
        result=ROOT/f'reports/strategy_backtests/{job}.result.json'
        js={'job_id':job,'request':request}
        if result.exists():
            d=read(result)
            js.update(summary=d.get('summary'),annual=d.get('annual'),status=d.get('status'),benchmark=d.get('benchmark',{}).get('summary'),trades=len(d.get('trades',[])))
        else:
            s=ROOT/f'reports/strategy_backtests/{job}.state.json'
            js['status']=read(s).get('status') if s.exists() else 'UNKNOWN'
        jobs.append(js)
    return state,rows,coverage,jobs


def percent(x):
    return '—' if x is None or pd.isna(x) else f'{x*100:.2f}%'


def table(headers,body):
    return '<table><thead><tr>'+''.join('<th>'+html.escape(str(v))+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in body)+'</tbody></table>'


def main():
    state,rows,coverage,jobs=collect()
    payload={'batch':state,'factors':rows,'coverage_by_year':coverage.to_dict(),'strategy_jobs':jobs,'inference_note':'Fixed declared directions; 63-session HAC and 63-session moving blocks; all non-gate RAW factors form one BH family. Incomplete family q values are provisional.'}
    OUT.with_suffix('.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    body='<h1>银行行业因子批次研究 · 2026-10-09</h1><p>真实浏览器任务，已有报告和版本保留。阶段状态：'+html.escape(state['status'])+'；已完成 '+str(sum(r['status'] in {'COMPLETED','PASS'} for r in rows))+'/15 项。该状态表示任务执行，不等于因子有效。</p>'
    body+='<p>统一区间 2020-01-02—2025-12-31，42 家当前银行来源名单，按当时上市状态及已知银行归属筛选。严格使用已公开、可核验版本；当前名单不是历史全行业的完整存续名单。最新采集的修订值不倒填历史；不请求新外部数值。</p>'
    body+='<h2>事前比较设置</h2><p>M4：63 日收益，3 组，每日最低 10 个有效样本，RAW 与缩尾标准化；稳健性、滚动时间和共同去重。单因子账户：固定声明方向，前 5 名，63/126 个交易日换仓，不额外择时；预定股息率、盈利收益率及质量门槛后股息率增加前 10 名对照。同银行池等权账户单列为参照。未完成的配置不会写成已验证结果。</p>'
    body+='<p>账户收盘形成信号、下一交易日开盘成交，保留默认费用、滑点、冲击及现金分红入账。沪深300价格指数只作附加参考，不能直接作为银行行业总收益基准。</p>'
    body+='<h2>逐年有效值覆盖率（%）</h2>'+table(['字段']+list(coverage.index),[[c]+[coverage.loc[y,c] for y in coverage.index] for c in FEATURES])
    body+='<h2>收益关系及跨年稳定性</h2><p>方向预先固定：不良率和ROE波动低值优先，其余高值优先；不根据全样本结果反转方向。年度RankIC已经按这个方向统一；正值才支持预设逻辑。质量门槛是二元筛选诊断，不参加连续因子优选。</p>'
    vals=[]
    for r in rows:
        s=r.get('summary',{}); ci=r.get('conservative',{}); yr=r.get('year_rank_ic',{})
        vals.append([html.escape(r['name']),html.escape(r['status']),percent(s.get('mean_coverage')),f"{ci.get('mean',float('nan')):.3f}" if 'mean' in ci else '—',*[f'{yr.get(str(y),float("nan")):.3f}' if str(y) in yr else '—' for y in range(2020,2026)],f"{ci.get('bootstrap_family_q',float('nan')):.3f}" if 'bootstrap_family_q' in ci else '—',f"[{ci['ci_lower']:.3f}, {ci['ci_upper']:.3f}]" if 'ci_lower' in ci else '—'])
    body+=table(['因子','执行状态','配对覆盖','方向RankIC','2020','2021','2022','2023','2024','2025','联合MBB q','MBB 95%区间'],vals)
    body+='<p>上表收益统计由冻结输入的全部15个字段与已生成的同银行、同日期63日收益标签独立重算，标签键完全一致，并交叉核验已完成的M4结果。执行状态一列仅指系统M4，尚未完成的系统检验不会冒充完成。系统原M4统计使用5阶HAC/5日区块；面对重叠63日收益，不能据此夸大显著性。本补充统一改用63阶HAC、63日区块、10,000次重采样，并把全部非门槛原始因子放入同一BH-FDR家族。检验仍属已看过历史的回顾诊断，不是全新盲测；滚动报告明确记录这一点。辅助标签只检查行情和停牌；真实费用、涨跌停和现金分红由账户回测独立核验。</p>'
    body+='<h2>单因子账户报告</h2>'
    body+=table(['名称','状态','调仓/持股数','年化','最大回撤','夏普','报告'],[[html.escape(j['request']['name']),html.escape(str(j.get('status'))),f"{j['request']['rebalance_sessions']}日 / {j['request']['target_count']}",percent((j.get('summary') or {}).get('annualized_return')),percent((j.get('summary') or {}).get('maximum_drawdown')),str((j.get('summary') or {}).get('sharpe','—')),f'<a href="http://127.0.0.1:8773/api/v1/strategy/jobs/{j["job_id"]}/report">查看</a>'] for j in jobs])
    body+='<h2>判读与边界</h2><p>优先比较同方向跨年是否持续、不同持仓周期是否相近、成本后是否仍有优势、银行等权参照下是否抗跌。覆盖不齐的因子必须在共同有效银行样本及共同年份上复核，不能把筛掉样本的收益当成新增信息。单次最高年化不能证明长期有效，缺失字段也不代表基本面健康。</p>'
    body+='<p>本文件是补充研究结果，不覆盖旧结论。批次ID：'+BATCH+'。完整任务、输入来源、原始版本及逐年指标保存在同名JSON中。</p>'
    style='<style>body{font:15px/1.7 "Microsoft YaHei",sans-serif;max-width:1500px;margin:32px auto;padding:0 25px;color:#223b31;background:#fafbf8}table{border-collapse:collapse;width:100%;font-size:13px;margin:18px 0}td,th{border:1px solid #d9e3dc;padding:8px;text-align:right}td:first-child,th:first-child{text-align:left}th{background:#eaf4ee}h1,h2{color:#176b4b}a{color:#176b4b}</style>'
    OUT.write_text('<!doctype html><meta charset="utf-8">'+style+body,encoding='utf-8')
    print(OUT)


if __name__=='__main__':
    main()
