"""Frozen-input bank return research; never edits production code or source warehouses."""
from __future__ import annotations
import sys, json, hashlib, html, math
from pathlib import Path
from datetime import date, datetime, timezone
import numpy as np
import pandas as pd
import duckdb
import lightgbm as lgb
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline

ROOT = Path('D:/futures_quant_strategy')
sys.path.insert(0, str(ROOT/'src'))
from alpha_research_os.portfolio.strategy_backtest import StrategyBacktestRequest, run_backtest
INPUT = ROOT/'data/factor_store/bank_inputs/d5590f6952b8a34075113c38567b3e56ae30ffa8895ae05172a683c8da0511e5'
OUT = ROOT/'reports/bank_return_ml_20261009'
MAIN = Path('C:/Users/Adminis/Desktop/量化研究/研究成果/20261008_银行优质低估_v1/银行优质低估研究报告.html')
BASE = ['cash_yield365','annual_earnings_yield','book_to_price','roe_weighted','roe_median3',
        'roe_volatility3','npl_ratio','provision_coverage_ratio','cet1_ratio','net_interest_margin',
        'profit_growth','npl_improvement','nim_change','quality_score']
EXTRA = ['roe_change252','capital_change252','provision_change252','yield_change252',
         'bp_history_percentile252','relative_momentum63','return126','volatility63','drawdown126']
FEATURES = BASE + EXTRA
MODELS = ['ridge','lgb_full','lgb_no_dividend']
NAMES = {'bank_equal':'银行等权','gate_equal':'质量门槛后等权','cash10':'股息率前10',
         'gate_cash10':'质量门槛＋股息率前10','ridge_gate10':'线性模型＋质量门槛',
         'lgb_gate10':'LightGBM＋质量门槛','lgb_no_dividend_gate10':'LightGBM无股息率＋质量门槛',
         'lgb_all10':'LightGBM全银行前10'}
PARAMS = dict(objective='regression', n_estimators=120, learning_rate=.03,
              max_depth=3, num_leaves=7, min_child_samples=60, reg_lambda=10.,
              verbosity=-1, n_jobs=4, random_state=20261009, deterministic=True,
              force_col_wise=True)

def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding='utf-8')

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    spec = dict(created_at=datetime.now(timezone.utc).isoformat(), status='RESEARCH_ONLY',
        input_pack=str(INPUT), input_sha256=sha(INPUT/'features.parquet'),
        date_range=['2020-01-02','2025-12-31'], evaluation=['2022-01-04','2025-12-31'],
        training_sampling='Every 20 market sessions, all contemporaneously eligible banks; not five banks',
        refit='Every 63 evaluation sessions, expanding window; all labels end before cutoff minus 5 market sessions',
        target='63-market-session raw T+1 open to terminal open economic total return minus same-signal valid-label bank equal mean',
        label_basis='Approved ex-date cash and gift shares, cash not reinvested, gross tax; exclude unresolved price-reference adjustments. Same economic recognition as existing backtest engine, not actual payment date.',
        features=FEATURES, missing='LightGBM native NaN; Ridge train-only median plus missing flags, train-only standardization; no future filling',
        models=MODELS, lightgbm_parameters=PARAMS, ridge_alpha=10.,
        minimum_train_dates=18, feature_max_missing_fraction_train=.8,
        portfolios=NAMES, target_count=10, rebalance_sessions=63,
        known_exposure='2020-2025 already examined, chronological walk-forward diagnostic, not untouched OOS',
        gate='Existing fixed gate only; no learned fundamental-health target, model predicts returns only',
        constraints='No source warehouse, production code or factor releases altered. No 2026 holdout read.',
        trial_budget='One fixed capacity per ML variant; no tuning on evaluation returns')
    f=OUT/'experiment_spec_before_labels.json'
    if f.exists():
        old=json.loads(f.read_text(encoding='utf-8')); assert old['features']==FEATURES and old['lightgbm_parameters']==PARAMS
    else: dump(f,spec)
    df=pd.read_parquet(INPUT/'features.parquet').sort_values(['instrument_id','session']).reset_index(drop=True)
    dates=sorted(df.session.unique()); codes=sorted(df.instrument_id.unique())
    assert df.available_at.notna().all()
    cutoff=pd.to_datetime(df.session.astype(str),utc=True)+pd.Timedelta(hours=7)
    assert (df.available_at<=cutoff).all()
    for old,new in [('roe_weighted','roe_change252'),('cet1_ratio','capital_change252'),
                    ('provision_coverage_ratio','provision_change252'),('cash_yield365','yield_change252')]:
        df[new]=df[old]-df.groupby('instrument_id')[old].shift(252)
    # Historical valuation position uses only observations at/before the signal.
    df['bp_history_percentile252']=df.groupby('instrument_id').book_to_price.transform(
        lambda s:s.rolling(252,min_periods=63).rank(pct=True))
    c=duckdb.connect(str(ROOT/'data/warehouse/alpha_research.duckdb'),read_only=True)
    c.execute('SET threads=4')
    c.register('research_bank_codes',pd.DataFrame({'ts_code':codes}))
    market=c.execute("""select m.ts_code,m.trade_date,m.open,m.close,m.pre_close,m.is_tradeable_bar
       from research.market_daily m join research_bank_codes b using(ts_code)
       where m.trade_date between '2019-01-01' and '2025-12-31' order by m.ts_code,m.trade_date""").df()
    actions=c.execute("""select a.* from research.corporate_action_reconciliation_approved a
       join research_bank_codes b using(ts_code) where a.effective_date between '2019-01-01' and '2025-12-31'""").df()
    state=c.execute("""select u.trade_date as session,u.ts_code as instrument_id,u.listed_session_number
       from research.universe_daily u join research_bank_codes b on b.ts_code=u.ts_code
       where u.trade_date between '2020-01-01' and '2025-12-31' and u.eligible_for_signal""").df()
    c.close()
    market.trade_date=pd.to_datetime(market.trade_date).dt.date
    actions.effective_date=pd.to_datetime(actions.effective_date).dt.date
    state.session=pd.to_datetime(state.session).dt.date
    df=df.merge(state, on=['session','instrument_id'],validate='one_to_one')
    df=df[df.listed_session_number>=60].copy()
    market.to_parquet(OUT/'label_market_inputs.parquet',index=False)
    actions.to_parquet(OUT/'label_approved_actions.parquet',index=False)
    idx_dates=sorted(market.trade_date.unique()); idx={d:i for i,d in enumerate(idx_dates)}
    lookup={}; price_records=[]; bad_by_code={}; ca_by_code={}
    for code,g in market.groupby('ts_code'):
        g=g.sort_values('trade_date'); ca=actions[actions.ts_code==code]
        ca_map={r.effective_date:r for r in ca.itertuples()}
        ca_by_code[code]=ca_map; bad=[]; previous=None; log_index=0.; path=[]
        for r in g.itertuples():
            lookup[(code,r.trade_date)]=r
            a=ca_map.get(r.trade_date)
            cash=float(a.cash_dividend_per_share) if a is not None else 0.
            stock=float(a.stock_dividend_ratio) if a is not None else 0.
            if previous is not None:
                expected=(previous-cash)/(1+stock)
                good=math.isfinite(r.pre_close) and abs(expected-r.pre_close)<=.011
                if not good: bad.append(r.trade_date)
                # Crossing unsupported actions makes trailing technical features unknown.
                ret=(r.close*(1+stock)+cash)/previous-1 if good and previous>0 and r.close>0 else np.nan
            else: ret=np.nan
            path.append({'session':r.trade_date,'instrument_id':code,'economic_daily_return':ret})
            previous=r.close if r.close>0 else previous
        pg=pd.DataFrame(path).set_index('session')
        rr=pg.economic_daily_return
        pg['return63']=rr.rolling(63,min_periods=63).apply(lambda x:np.prod(1+x)-1,raw=True)
        pg['return126']=rr.rolling(126,min_periods=126).apply(lambda x:np.prod(1+x)-1,raw=True)
        pg['volatility63']=rr.rolling(63,min_periods=63).std()*np.sqrt(252)
        pg['drawdown126']=rr.rolling(126,min_periods=126).apply(
            lambda x:np.min(np.r_[1.,np.cumprod(1+x)]/np.maximum.accumulate(np.r_[1.,np.cumprod(1+x)])-1),raw=True)
        price_records.append(pg.reset_index());bad_by_code[code]=set(bad)
    pp=pd.concat(price_records,ignore_index=True)
    df=df.merge(pp.drop(columns='economic_daily_return'),on=['session','instrument_id'],validate='one_to_one')
    df['relative_momentum63']=df.return63-df.groupby('session').return63.transform('mean')
    assert not df.duplicated(['session','instrument_id']).any()
    df.to_parquet(OUT/'features_only.parquet',index=False)
    evaldates=[d for d in dates if d>=date(2022,1,4)]
    rebalances=evaldates[::63]
    training_dates=set(dates[::20]); evalsample=set(d for d in dates[::20] if d>=date(2022,1,4))
    all_samples=training_dates | set(rebalances) | {dates[-1]}
    labels=[]
    for r in df[df.session.isin(all_samples)].itertuples():
        i=idx[r.session];entry=idx_dates[i+1] if i+1<len(idx_dates) else None
        end=idx_dates[i+64] if i+64<len(idx_dates) else None
        reason='';value=None
        if end is None: reason='right_censored_63'
        else:
            a=lookup.get((r.instrument_id,entry));b=lookup.get((r.instrument_id,end))
            if a is None or b is None or not a.is_tradeable_bar or not b.is_tradeable_bar or a.open<=0 or b.open<=0:
                reason='no_tradeable_endpoint'
            elif any(entry<d<=end for d in bad_by_code[r.instrument_id]): reason='unsupported_reference_adjustment'
            else:
                shares=1.;cash=0.
                for d,event in sorted(ca_by_code[r.instrument_id].items()):
                    if entry<d<=end:
                        cash+=shares*float(event.cash_dividend_per_share)
                        shares*=1+float(event.stock_dividend_ratio)
                value=(shares*b.open+cash)/a.open-1
        labels.append(dict(session=r.session,instrument_id=r.instrument_id,entry_date=entry,label_end=end,
                           total_return63=value,valid_label=not reason,invalid_reason=reason))
    y=pd.DataFrame(labels)
    y['bank_return63']=y.groupby('session').total_return63.transform('mean')
    y['relative_return63']=y.total_return63-y.bank_return63
    y.to_parquet(OUT/'labels_only.parquet',index=False)
    dump(OUT/'input_audit.json',dict(feature_rows=len(df),banks=df.instrument_id.nunique(),
        first=str(df.session.min()),last=str(df.session.max()),feature_available_check='PASS',
        label_rows=len(y),valid_labels=int(y.valid_label.sum()),invalid_reasons=y.invalid_reason.value_counts().to_dict(),
        feature_nonmissing={k:int(df[k].notna().sum()) for k in FEATURES},
        spec_sha256=sha(f),features_sha256=sha(OUT/'features_only.parquet'),labels_sha256=sha(OUT/'labels_only.parquet'),
        unsupported_observations={k:len(v) for k,v in bad_by_code.items()},new_remote_data_requested=False))
    dump(OUT/'schedule_dates.json',dict(rebalances=rebalances,training_dates=sorted(training_dates),eval_dates=sorted(evalsample)))
    print('PREPARED',len(df),df.instrument_id.nunique(),len(y),int(y.valid_label.sum()),flush=True)

def train():
    x=pd.read_parquet(OUT/'features_only.parquet');y=pd.read_parquet(OUT/'labels_only.parquet')
    # Labels are joined only here, after immutable feature export and spec registration.
    data=x.merge(y,on=['session','instrument_id'],how='left',validate='one_to_one')
    s=json.loads((OUT/'schedule_dates.json').read_text(encoding='utf-8'))
    rebalances=[date.fromisoformat(d) for d in s['rebalances']]
    train_dates={date.fromisoformat(d) for d in s['training_dates']}
    pred_dates={date.fromisoformat(d) for d in s['eval_dates']}|set(rebalances)|{x.session.max()}
    all_dates=sorted(x.session.unique()); dateidx={d:i for i,d in enumerate(all_dates)}
    preds=[];audits=[];gains=[]
    for j,cut in enumerate(rebalances):
        boundary=all_dates[dateidx[cut]-5]
        tr=data[data.session.isin(train_dates)&data.valid_label.fillna(False)&(data.label_end<boundary)].copy()
        nextcut=rebalances[j+1] if j+1<len(rebalances) else date(2026,1,1)
        te=x[x.session.isin(pred_dates)&(x.session>=cut)&(x.session<nextcut)].copy()
        assert tr.session.nunique()>=18 and tr.label_end.max()<boundary<cut
        active=[k for k in FEATURES if tr[k].notna().mean()>=.2 and tr[k].nunique()>1]
        target=tr.relative_return63.clip(-.5,.5).to_numpy()
        weights=tr.session.map(1/tr.groupby('session').size()).to_numpy();weights=weights/weights.mean()
        audit=dict(cutoff=str(cut),embargo_boundary=str(boundary),train_rows=len(tr),train_dates=tr.session.nunique(),
                   train_banks=tr.instrument_id.nunique(),train_max_label_end=str(tr.label_end.max()),
                   train_first_signal=str(tr.session.min()),train_last_signal=str(tr.session.max()),
                   prediction_rows=len(te),features_used=active,excluded_features=[k for k in FEATURES if k not in active])
        for modelname in MODELS:
            cols=[k for k in active if modelname!='lgb_no_dividend' or k not in ['cash_yield365','yield_change252']]
            if modelname=='ridge':
                model=make_pipeline(SimpleImputer(strategy='median',add_indicator=True),StandardScaler(),Ridge(alpha=10.))
                model.fit(tr[cols],target,ridge__sample_weight=weights)
                dump(OUT/f'ridge_coefficients_{cut}.json',dict(features=model[0].get_feature_names_out().tolist(),
                    coefficients=model[-1].coef_.tolist(),intercept=float(model[-1].intercept_),
                    scaler_mean=model[1].mean_.tolist(),scaler_scale=model[1].scale_.tolist(),
                    imputer_statistics=model[0].statistics_.tolist()))
            else:
                model=lgb.LGBMRegressor(**PARAMS);model.fit(tr[cols],target,sample_weight=weights)
                model.booster_.save_model(str(OUT/f'{modelname}_{cut}.txt'))
                for f,g in zip(cols,model.booster_.feature_importance('gain')):
                    gains.append(dict(model=modelname,cutoff=str(cut),feature=f,gain=float(g)))
            p=te[['session','instrument_id','quality_gate','cash_yield365']].copy()
            p['model']=modelname;p['predicted_relative_return63']=model.predict(te[cols])
            # Cross-sectional centering is based on current predictions only, never observed outcomes.
            p.predicted_relative_return63-=p.groupby('session').predicted_relative_return63.transform('mean')
            p['return_opportunity_score']=p.groupby('session').predicted_relative_return63.rank(pct=True)*100
            p['training_cutoff']=cut;preds.append(p)
        audits.append(audit);print('TRAINED',cut,'rows',len(tr),'banks',tr.instrument_id.nunique(),'dates',tr.session.nunique(),flush=True)
    pd.concat(preds,ignore_index=True).to_parquet(OUT/'walkforward_predictions.parquet',index=False)
    dump(OUT/'rolling_training_audit.json',audits)
    pd.DataFrame(gains).to_parquet(OUT/'training_gain_importance.parquet',index=False)
    evaluate()

def evaluate():
    p=pd.read_parquet(OUT/'walkforward_predictions.parquet');y=pd.read_parquet(OUT/'labels_only.parquet')
    samples=json.loads((OUT/'schedule_dates.json').read_text(encoding='utf-8'))['eval_dates']
    samples={date.fromisoformat(d) for d in samples}
    baseline=pd.read_parquet(OUT/'features_only.parquet')
    baseline=baseline[baseline.session.isin(samples)][['session','instrument_id','quality_gate','cash_yield365']].copy()
    baseline['model']='rule_cash'
    baseline['predicted_relative_return63']=baseline.cash_yield365
    baseline['return_opportunity_score']=baseline.groupby('session').cash_yield365.rank(pct=True)*100
    p=pd.concat([p,baseline],ignore_index=True)
    z=p[p.session.isin(samples)].merge(y,on=['session','instrument_id'],validate='many_to_one')
    z=z[z.valid_label].copy();rows=[]
    for (model,session),g in z.groupby(['model','session']):
        for pool,h in [('all',g),('gate',g[g.quality_gate==1])]:
            h=h.sort_values(['predicted_relative_return63','instrument_id'],ascending=[False,True])
            if len(h)<10:continue
            ic=h.predicted_relative_return63.corr(h.relative_return63,method='spearman')
            rows.append(dict(model=model,pool=pool,session=session,n=len(h),rank_ic=float(ic) if pd.notna(ic) else None,
                             top10_relative=float(h.head(10).relative_return63.mean()),
                             top10_absolute=float(h.head(10).total_return63.mean()),
                             bank_return=float(h.bank_return63.iloc[0]),
                             top_bottom=float(h.head(10).relative_return63.mean()-h.tail(10).relative_return63.mean())))
    pd.DataFrame(rows).to_parquet(OUT/'prediction_monthly_diagnostics.parquet',index=False)
    # Post-run descriptive diagnostics only; no models/thresholds are selected or changed.
    buckets=[]
    for (model,session),g in z[z.quality_gate==1].groupby(['model','session']):
        g=g.copy()
        g['bucket']=np.ceil(g.predicted_relative_return63.rank(method='first',pct=True)*5).astype(int)
        for bucket,h in g.groupby('bucket'):
            buckets.append(dict(model=model,session=session,bucket=int(bucket),members=len(h),
                absolute_return=float(h.total_return63.mean()),relative_return=float(h.relative_return63.mean()),
                bank_return=float(h.bank_return63.iloc[0])))
    pd.DataFrame(buckets).to_parquet(OUT/'score_bucket_diagnostics.parquet',index=False)

def backtests():
    x=pd.read_parquet(OUT/'features_only.parquet');p=pd.read_parquet(OUT/'walkforward_predictions.parquet')
    s=json.loads((OUT/'schedule_dates.json').read_text(encoding='utf-8'))
    dates=[date.fromisoformat(d) for d in s['rebalances']]
    # No valid-label flags are read in portfolio construction: avoid future outcome filtering.
    req=json.loads((ROOT/'reports/strategy_backtests/20261009-192158-d5f6de.request.json').read_text(encoding='utf-8'))
    for key,name in NAMES.items():
        dest=OUT/f'portfolio_{key}.json'
        if dest.exists(): print('EXISTING',key,flush=True);continue
        schedule={};chosen=[]
        for d in dates:
            g=x[x.session==d].copy()
            if key.startswith('gate') or key.endswith('gate10'):g=g[g.quality_gate==1]
            if key.startswith('ridge') or key.startswith('lgb'):
                model='ridge' if key.startswith('ridge') else 'lgb_no_dividend' if 'no_dividend' in key else 'lgb_full'
                g=g.merge(p[(p.session==d)&(p.model==model)][['instrument_id','predicted_relative_return63']],on='instrument_id',validate='one_to_one')
                g=g.sort_values(['predicted_relative_return63','instrument_id'],ascending=[False,True]).head(10)
            elif key.endswith('cash10') or key=='cash10':
                g=g.sort_values(['cash_yield365','instrument_id'],ascending=[False,True]).head(10)
            codes=g.instrument_id.tolist();assert len(codes)>=10
            schedule[d]={code:1/len(codes) for code in codes}
            chosen.extend(dict(strategy=key,signal=d,code=code,weight=1/len(codes)) for code in codes)
        dump(OUT/f'targets_{key}.json',{str(d):v for d,v in schedule.items()})
        q=dict(req,name='银行收益学习研究·'+name,start=str(dates[0]),end='2025-12-31')
        # score_rules remains a provenance/preflight anchor; actual selections exclusively target_schedule.
        request=StrategyBacktestRequest.model_validate(q)
        dump(OUT/f'portfolio_request_{key}.json',q)
        print('BACKTEST_START',key,flush=True)
        last=[-1]
        def progress(v):
            n=int(v.get('progress',0))//20
            if n>last[0]:last[0]=n;print('BACKTEST_PROGRESS',key,v.get('progress'),v.get('current_session'),flush=True)
        r=run_backtest(ROOT,request,target_schedule=schedule,progress_callback=progress,include_selection_history=True)
        r['research_actual_signal_source']='External frozen PIT features / walk-forward predictions, not the request score_rules anchor'
        r['research_targets_sha256']=sha(OUT/f'targets_{key}.json')
        daily=pd.DataFrame(r['daily']);nav=daily.nav.to_numpy()
        assert r['status']=='PASS'
        assert math.isclose((nav[-1]/nav[0])**(252/(len(nav)-1))-1,r['summary']['annualized_return'],abs_tol=1e-9)
        assert math.isclose(float(min(nav/np.maximum.accumulate(nav)-1)),r['summary']['maximum_drawdown'],abs_tol=1e-9)
        assert math.isclose(float(daily.dividend_cash.sum()),r['summary']['total_dividend_cash'],abs_tol=.02)
        dump(dest,r);pd.DataFrame(chosen).to_parquet(OUT/f'selections_{key}.parquet',index=False)
        print('BACKTEST_DONE',key,json.dumps(r['summary']),flush=True)

def report():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei'];plt.rcParams['axes.unicode_minus']=False
    rows=[];results={}
    for key,name in NAMES.items():
        r=json.loads((OUT/f'portfolio_{key}.json').read_text(encoding='utf-8'));results[key]=r
        rows.append(dict(strategy=key,name=name,**r['summary'],annual={str(a['year']):a['return'] for a in r['annual']}))
    diagnostics=pd.read_parquet(OUT/'prediction_monthly_diagnostics.parquet')
    dg=diagnostics.groupby(['model','pool']).agg(months=('session','nunique'),rank_ic=('rank_ic','mean'),
        top10_relative=('top10_relative','mean'),top_bottom=('top_bottom','mean')).reset_index()
    down=diagnostics[diagnostics.bank_return<0].groupby(['model','pool']).agg(periods=('session','size'),
        bank_return=('bank_return','mean'),top10_absolute=('top10_absolute','mean'),top10_relative=('top10_relative','mean')).reset_index()
    buckets=pd.read_parquet(OUT/'score_bucket_diagnostics.parquet').groupby(['model','bucket']).agg(
        months=('session','nunique'),relative_return=('relative_return','mean')).reset_index()
    gain=pd.read_parquet(OUT/'training_gain_importance.parquet')
    gain['fraction']=gain.gain/gain.groupby(['model','cutoff']).gain.transform('sum')
    importance= gain[gain.model=='lgb_full'].groupby('feature').fraction.mean().sort_values(ascending=False)
    audits=json.loads((OUT/'rolling_training_audit.json').read_text(encoding='utf-8'))
    coverage=json.loads((OUT/'input_audit.json').read_text(encoding='utf-8'))
    for group,file in [(['bank_equal','gate_cash10','ridge_gate10','lgb_gate10'],'primary_curve'),
                       (['gate_cash10','lgb_gate10','lgb_no_dividend_gate10','lgb_all10'],'ablation_curve')]:
        fig,ax=plt.subplots(figsize=(11,4.6))
        for k in group:
            d=pd.DataFrame(results[k]['daily']);ax.plot(pd.to_datetime(d.session),d.nav/d.nav.iloc[0],label=NAMES[k],lw=1.5)
        ax.set_title('银行收益预测研究：同区间、同执行成本（2022–2025）');ax.set_ylabel('净值');ax.grid(alpha=.2);ax.legend()
        fig.tight_layout();fig.savefig(OUT/(file+'.svg'));plt.close(fig)
    pct=lambda v:f'{v*100:.2f}%'
    table='<table><tr><th>方案</th><th>年化</th><th>累计收益</th><th>最大回撤</th><th>夏普</th></tr>'
    for r in rows:table+=f'<tr><td>{r["name"]}</td><td>{pct(r["annualized_return"])}</td><td>{pct(r["total_return"])}</td><td>{pct(r["maximum_drawdown"])}</td><td>{r["sharpe"]:.3f}</td></tr>'
    table+='</table>'
    annual='<table><tr><th>方案</th>'+''.join(f'<th>{y}</th>' for y in range(2022,2026))+'</tr>'
    for r in rows:annual+='<tr><td>'+r['name']+'</td>'+''.join('<td>'+pct(r['annual'][str(y)])+'</td>' for y in range(2022,2026))+'</tr>'
    annual+='</table>'
    rule=next(r for r in rows if r['strategy']=='gate_cash10');ml=next(r for r in rows if r['strategy']=='lgb_gate10')
    conclusion=('本轮LightGBM优于原规则的年化收益，但还需结合逐年稳定性与回撤判断，不能直接替代规则。' if ml['annualized_return']>rule['annualized_return']
                else '本轮LightGBM没有超过原有“质量门槛＋股息率”规则，暂保留规则作为主方案；机器学习保留为研究对照。')
    body=f'<section id="bank-return-ml-20261009"><h2>2026-10-09 银行收益预测模型：第二步研究</h2><p><strong>{conclusion}</strong></p>'
    body+='<p>模型目标只有未来63个交易日相对银行板块的收益；基本面改善不直接等于收益提高。输出是相对收益估计与0–100机会分，不是质量认证、绝对涨幅承诺或合理价格区间。</p>'
    body+=table+annual
    body+='<h3>净值对照</h3>'+(OUT/'primary_curve.svg').read_text(encoding='utf-8')+(OUT/'ablation_curve.svg').read_text(encoding='utf-8')
    body+='<h3>样本与防未来函数</h3><p>'+f'冻结特征共有{coverage["feature_rows"]:,}行、{coverage["banks"]}家银行。首轮训练{audits[0]["train_rows"]}行、{audits[0]["train_dates"]}个采样日、{audits[0]["train_banks"]}家；最后一轮{audits[-1]["train_rows"]}行、{audits[-1]["train_dates"]}个采样日、{audits[-1]["train_banks"]}家。不是只训练5家。'+ '</p>'
    body+='<p>2020–2021为起始训练历史，2022–2025按时间滚动检验；训练每20个交易日采样，每63个交易日重训、换仓。训练收益结束日严格早于重训日之前第5个交易日。缺失筛选只看训练期；缺失超过80%的列当轮跳过。树模型保留NaN，线性模型只用训练期中位数与缺失标记，不以后面的数据填前面的值。所有银行共享模型，未输入股票代码。</p>'
    body+='<p>标签用T+1原始开盘价买入，63个交易日后原始开盘价估值，包含已核验现金和送股权益；现金不复投、不计成本。未解释的价格参考调整和不可交易端点只从训练及标签诊断中剔除，不从当时组合选择中偷偷剔除。组合回测另走现有账户引擎：100万元、2%现金预留、前10等权、63日换仓、真实股数与原始行情，含佣金、历史印花税、过户费、滑点和参与率限制。现金/送股沿用引擎除权日经济确认口径，并非实际到账日现金流。</p>'
    body+='<p>股息率采用过去365天实际已实施现金分红/当时价格；PE信息用年度普通股盈利收益率，PB信息用普通股账面价值/价格，不能把年度盈利收益率称为TTM。其他变量包含ROE水平和稳定性、不良率及改善、拨备、核心一级资本、净息差及变化、利润增长，及历史可见的变化、估值位置与价格波动。按当前可比且核验可得的数据纳入，未把未核验历史修订数据倒灌进旧日期。</p>'
    body+='<h3>预测区分能力（与组合收益分开）</h3>'+dg.to_html(index=False,float_format=lambda v:f'{v:.4f}')
    body+='<p>上表为每20交易日采样、成熟有效标签的均值；预测区间相互重叠，所以不能把样本行数当独立试验次数，也不是可直接复利的策略年化。</p>'
    body+='<h3>板块下跌时是否更抗跌</h3>'+down.to_html(index=False,float_format=lambda v:f'{v:.4f}')
    body+='<p>上述期数是有成熟标签且银行均值为负的采样期，不是独立行情轮次；相对收益为正仍可能亏损。模型预测的是相对机会，并不保证下跌时保本。</p>'
    body+='<h3>机会分的历史分层</h3>'+buckets.to_html(index=False,float_format=lambda v:f'{v:.4f}')
    body+='<p>bucket=1为通过质量门槛后预测最低20%，5为最高20%；每个采样日先分层，再等权平均各层后续63日相对收益。分层如不单调，说明分数仍需改进；本轮未据此改模型。它不是价格便宜/合理/高估分层。</p>'
    body+='<h3>训练中的变量使用情况</h3>'+importance.rename('平均训练增益占比').reset_index().to_html(index=False,float_format=lambda v:f'{v:.4f}')
    body+='<p>训练增益只反映模型怎样使用变量，不等于变量在未来有效或因果作用。去掉股息率版本同时去掉股息率变化，但PB/盈利收益率仍可携带相关估值信息。</p>'
    body+='<h3>打分如何输出</h3><p>预测相对收益=f(当时可见的23项银行财务、股息、估值与价格变量)－当日银行预测均值；机会分=当日预测相对收益的百分位×100。固定质量门槛决定能否入选，机会分决定通过门槛后的排序。门槛与质量规则沿用既有版本，本轮没有用未来收益重新定义“基本面健康”。</p>'
    pred=pd.read_parquet(OUT/'walkforward_predictions.parquet')
    last=pred.session.max()
    snapshot=pred[(pred.session==last)&(pred.model=='lgb_full')&(pred.quality_gate==1)].sort_values('predicted_relative_return63',ascending=False).head(10)
    snapshot=snapshot[['instrument_id','predicted_relative_return63','return_opportunity_score','cash_yield365','training_cutoff']]
    snapshot.to_parquet(OUT/'last_research_snapshot_top10.parquet',index=False)
    body+=f'<h3>研究末日打分样例：{last}</h3>'+snapshot.to_html(index=False,float_format=lambda v:f'{v:.4f}')
    body+='<p>这是2025-12-31研究快照，不是2026年当前荐股清单；未来三个月标签尚不成熟，未参与效果诊断。预测相对收益单位为小数，如0.02表示模型估计约+2个百分点板块相对机会，尚未证明此数值校准准确。252滞后财务变化采用同一银行252条可用信号记录差值，估值位置采用当时已有的252条记录，不保证恰等于一个自然年。</p>'
    body+='<p>本轮实际表现：规则2022–2025逐年均强于银行等权；LightGBM在2025年较规则更强，2022–2024较弱，不能仅凭最后一年替代规则。所有方案都受2024年银行行情明显影响。这里的20.49%等年化对应2022–2025新起点和新换仓日期，不能直接与此前2020–2025年化比较。</p>'
    body+='<p>重要边界：2020–2025已经被之前研究多次查看，本轮只是遵守时间顺序的历史检验，不是全新未看过的独立验证；当前42家银行来源名单仍存在历史完整银行集合的认证限制，历史版本、资本事件认证也未全部完成。因此所有模型均标记RESEARCH_ONLY。此次没有请求新财务或价格数据，没有修改前后端、原始库或发布因子。研究脚本和冻结模型保留以便复核。</p>'
    body+='<p>方法参考：<a href="https://lightgbm.readthedocs.io/en/stable/Parameters.html">LightGBM官方参数文档</a>；<a href="https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html">Ridge官方文档</a>。</p></section>'
    style='<style>body{max-width:1200px;margin:32px auto;padding:0 24px;font:16px/1.7 "Microsoft YaHei",sans-serif;color:#26372f}table{border-collapse:collapse;width:100%;margin:16px 0}td,th{padding:8px;border:1px solid #dce5df;text-align:right}td:first-child,th:first-child{text-align:left}svg{width:100%;height:auto}h2,h3{color:#176749}</style>'
    (OUT/'report.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><title>银行收益预测研究</title>'+style+'<body>'+body+'</body></html>',encoding='utf-8')
    dump(OUT/'summary.json',dict(conclusion=conclusion,portfolios=rows,diagnostics=dg.to_dict('records'),
         downmarket_diagnostics=down.to_dict('records'),score_buckets=buckets.to_dict('records'),
         importance=importance.to_dict(),input_audit=coverage,train_audit=audits,status='RESEARCH_ONLY'))
    # Append only: archive and preserve every existing byte around the closing body.
    old=MAIN.read_bytes();mark=b'</body>';at=old.rfind(mark);assert at>=0
    if b'id="bank-return-ml-20261009"' not in old:
        archive=MAIN.parent/'prior_report_archive';archive.mkdir(exist_ok=True)
        oldsha=hashlib.sha256(old).hexdigest();backup=archive/f'before_return_ml_20261009_{oldsha[:12]}.html'
        if not backup.exists():backup.write_bytes(old)
        new=old[:at]+body.encode('utf-8')+old[at:]
        MAIN.write_bytes(new);assert new.startswith(old[:at]) and new.endswith(old[at:])
        dump(OUT/'integration_receipt.json',dict(old_sha256=oldsha,new_sha256=sha(MAIN),archive=str(backup),
             report=str(MAIN),previous_bytes_preserved=True))
    print('REPORT_DONE',json.dumps(rows,ensure_ascii=False),flush=True)

if __name__=='__main__':
    stage=sys.argv[1] if len(sys.argv)>1 else 'all'
    if stage in ('prepare','all'):prepare()
    if stage in ('train','all'):train()
    if stage in ('backtest','all'):backtests()
    if stage in ('report','all'):report()
