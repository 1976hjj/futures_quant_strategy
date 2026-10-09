"""Bounded, date-purged nested tuning; research artifacts only, no production edits."""
from __future__ import annotations
import sys, json, hashlib, math, html
from pathlib import Path
from datetime import date, datetime, timezone
from itertools import product
import numpy as np
import pandas as pd
import lightgbm as lgb

ROOT=Path('D:/futures_quant_strategy')
sys.path.insert(0,str(ROOT/'src'))
from alpha_research_os.portfolio.strategy_backtest import StrategyBacktestRequest,run_backtest
SOURCE=ROOT/'reports/bank_return_ml_20261009'
OUT=ROOT/'reports/bank_nested_tuning_20261009'
MAIN=Path('C:/Users/Adminis/Desktop/量化研究/研究成果/20261008_银行优质低估_v1/银行优质低估研究报告.html')
CORE=['cash_yield365','annual_earnings_yield','book_to_price','roe_weighted','roe_volatility3',
      'npl_ratio','provision_coverage_ratio','cet1_ratio','profit_growth','npl_improvement']
FULL=CORE+['roe_median3','net_interest_margin','nim_change','quality_score','roe_change252',
           'capital_change252','provision_change252','yield_change252','bp_history_percentile252',
           'relative_momentum63','return126','volatility63','drawdown126']
SETS={'core10':CORE,'full23':FULL}
CAPS={'small':dict(num_leaves=3,max_depth=2,n_estimators=80,min_child_samples=80,reg_lambda=20.),
      'medium':dict(num_leaves=7,max_depth=3,n_estimators=120,min_child_samples=60,reg_lambda=10.),
      'flexible':dict(num_leaves=15,max_depth=4,n_estimators=180,min_child_samples=40,reg_lambda=5.)}
FAMILIES=['regression','ranking','residual']
CONFIGS=[dict(id=f'{a}_{b}_{c}_{d}',family=a,feature_set=b,capacity=c,window=d)
         for a,b,c,d in product(FAMILIES,SETS,CAPS,['expanding','recent3years'])]
ALPHAS=[.25,.5,1.]
PORTS={'tuned_regression':'内部调优·收益回归＋股息率',
       'tuned_ranking':'内部调优·排名学习＋股息率',
       'tuned_residual':'内部调优·股息率残差修正',
       'adaptive':'内部调优·三类自适应选择',
       'pure_regression':'内部调优·纯收益回归',
       'pure_ranking':'内部调优·纯排名学习'}

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def dump(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf-8')
def boundary(day,calendar):return calendar[max(0,calendar.index(day)-5)]

def load():
    x=pd.read_parquet(SOURCE/'features_only.parquet')
    y=pd.read_parquet(SOURCE/'labels_only.parquet')
    data=x.merge(y,on=['session','instrument_id'],how='left',validate='one_to_one')
    s=json.loads((SOURCE/'schedule_dates.json').read_text(encoding='utf-8'))
    cuts=[date.fromisoformat(d) for d in s['rebalances']]
    train_dates={date.fromisoformat(d) for d in s['training_dates']}
    pred_dates={date.fromisoformat(d) for d in s['eval_dates']}|set(cuts)|{x.session.max()}
    return x,data,cuts,train_dates,pred_dates,sorted(x.session.unique())

def register():
    OUT.mkdir(exist_ok=True)
    spec=dict(created_at=datetime.now(timezone.utc).isoformat(),status='RESEARCH_ONLY',
      source_files={n:sha(SOURCE/n) for n in ['features_only.parquet','labels_only.parquet','schedule_dates.json']},
      outer='2022-2025 same 16 signal dates as previous experiment; no 2026 reads',
      training='Only signal-time quality_gate==1 banks; all mature monthly samples before outer cutoff minus 5 sessions',
      validation='Last 9 available mature training signal dates, 3 chronological blocks of 3; inner train labels end before block-first signal minus 5 sessions; min 8 train dates',
      configurations=CONFIGS,capacities=CAPS,features=SETS,blend_alphas=ALPHAS,
      total_candidate_variants=109,training_feature_missing_threshold=.8,
      regression='Squared-error relative return clipped to +/-0.5, grouped date-equal sample weights',
      ranking='LambdaRank, relevance 0..4 from within-date training forward return quintiles; label_gain=[0,1,2,3,4], top10',
      residual='Train nonnegative yield-percentile slope using train labels, fit LightGBM to residual; score=yield_rank + alpha*.20*tanh(centered_residual_prediction/max(train_residual_MAD,.02))',
      reg_rank_blend='(1-alpha)*yield_percentile + alpha*within-signal prediction_percentile',
      hyperparam_selection='Average validation top10 return difference vs same-pool yield baseline minus .25*std(date differences); top10 labels gross, actual execution costs evaluated separately',
      fallback='Accept learned correction only if mean validation gain >=0.002 and positive in at least 2 of 3 inner blocks; otherwise pure yield rule',
      pure_controls='Pick alpha=1 best internally in regression/ranking without fallback, always apply; all failures preserved',
      portfolio=PORTS,execution='Existing engine same 1000000CNY / 2% cash / top10 / 63 sessions / costs, raw prices and share accounting',
      exposure='Historical 2020-2025 already exposed; nested selection audit prevents date leakage but not global researcher exposure. Not untouched OOS.',
      fixed_trial_budget='36 model configurations x3 blend/correction strengths + rule; no additions after outer performance is read',
      formal_status='Exploratory historical research; no production promotion; previous files/results preserved')
    path=OUT/'experiment_spec_before_tuning.json'
    if path.exists():
        old=json.loads(path.read_text(encoding='utf-8'));assert old['configurations']==CONFIGS and old['blend_alphas']==ALPHAS
    else:dump(path,spec)

def fit(cfg,train,cut):
    if cfg['window']=='recent3years':
        earliest=(pd.Timestamp(cut)-pd.DateOffset(years=3)).date()
        train=train[train.session>=earliest].copy()
    train=train.sort_values(['session','instrument_id'])
    assert train.session.nunique()>=8
    cols=[c for c in SETS[cfg['feature_set']] if train[c].notna().mean()>=.2 and train[c].nunique()>1]
    weights=(1/train.groupby('session').session.transform('size')).to_numpy(copy=True);weights/=weights.mean()
    y=train.relative_return63.clip(-.5,.5).to_numpy()
    base_rank=(train.groupby('session').cash_yield365.rank(pct=True)-.5).to_numpy()
    xm=float(np.average(base_rank,weights=weights));ym=float(np.average(y,weights=weights))
    beta=max(0.,float(np.average((base_rank-xm)*(y-ym),weights=weights)/(np.average((base_rank-xm)**2,weights=weights)+.001)))
    intercept=ym-beta*xm
    residual=y-(intercept+beta*base_rank)
    scale=max(.02,float(np.median(np.abs(residual-np.median(residual)))))
    params=dict(learning_rate=.03,verbosity=-1,n_jobs=4,random_state=20261009,
                deterministic=True,force_col_wise=True,**CAPS[cfg['capacity']])
    if cfg['family']=='ranking':
        relevance=np.minimum(4,np.floor(train.groupby('session').relative_return63.rank(pct=True).to_numpy()*5)).astype(int)
        model=lgb.LGBMRanker(objective='lambdarank',label_gain=[0,1,2,3,4],lambdarank_truncation_level=13,**params)
        model.fit(train[cols],relevance,group=train.groupby('session',sort=True).size().tolist(),
                  sample_weight=weights,eval_at=[10])
    else:
        model=lgb.LGBMRegressor(objective='regression',**params)
        model.fit(train[cols],residual if cfg['family']=='residual' else y,sample_weight=weights)
    return dict(model=model,columns=cols,scale=scale,beta=beta,intercept=intercept,
                train_rows=len(train),train_dates=train.session.nunique(),train_banks=train.instrument_id.nunique(),
                train_max_label_end=str(train.label_end.max()),train_first=str(train.session.min()),train_last=str(train.session.max()))

def predict(bundle,frame):
    z=frame[['session','instrument_id','cash_yield365']].copy()
    z['raw_prediction']=bundle['model'].predict(frame[bundle['columns']])
    z['yield_rank']=z.groupby('session').cash_yield365.rank(pct=True)
    z['prediction_rank']=z.groupby('session').raw_prediction.rank(pct=True)
    z['centered_raw']=z.raw_prediction-z.groupby('session').raw_prediction.transform('mean')
    z['correction']=.20*np.tanh(z.centered_raw/bundle['scale'])
    return z

def score(pred,cfg,alpha):
    return pred.yield_rank+alpha*pred.correction if cfg['family']=='residual' else (1-alpha)*pred.yield_rank+alpha*pred.prediction_rank

def validation_rows(pred,val,cfg,alpha,fold):
    z=pred.merge(val[['session','instrument_id','relative_return63']],on=['session','instrument_id'],validate='one_to_one')
    z['score']=score(z,cfg,alpha)
    rows=[]
    for d,g in z.groupby('session'):
        assert len(g)>=10
        selected=g.sort_values(['score','instrument_id'],ascending=[False,True]).head(10)
        base=g.sort_values(['yield_rank','instrument_id'],ascending=[False,True]).head(10)
        a=float(selected.relative_return63.mean());b=float(base.relative_return63.mean())
        rows.append(dict(session=str(d),fold=fold,candidate_return=a,baseline_return=b,delta=a-b))
    return rows

def choose(ledger,family=None,pure=False):
    options=[r for r in ledger if family is None or r['family']==family]
    if pure:
        options=[r for r in options if r['alpha']==1.]
    else:
        options=[r for r in options if r['mean_delta']>=.002 and r['positive_folds']>=2]
    if not options:return dict(candidate='rule',family='rule',alpha=0.,selection_score=0.,mean_delta=0.)
    # Deterministic tie-breaking favors less correction, lower capacity, fewer features.
    ordered=sorted(options,key=lambda r:(-r['selection_score'],r['alpha'],list(CAPS).index(r['capacity']),len(SETS[r['feature_set']]),r['candidate']))
    return ordered[0]

def tune():
    register()
    x,data,cuts,train_dates,pred_dates,calendar=load()
    ledger_all=[];audit=[];choice_rows=[];predictions=[]
    for i,cut in enumerate(cuts):
        outer_boundary=boundary(cut,calendar)
        mature=data[data.session.isin(train_dates)&data.valid_label.fillna(False)&
                     (data.label_end<outer_boundary)&(data.quality_gate==1)].copy()
        dates=sorted(mature.session.unique());assert len(dates)>=21
        val_dates=dates[-9:];fold_records=[];ledger=[];store={cfg['id']:{a:[] for a in ALPHAS} for cfg in CONFIGS}
        for fold in range(3):
            vv=val_dates[fold*3:(fold+1)*3];inner_cut=vv[0];inner_boundary=boundary(inner_cut,calendar)
            tr=mature[(mature.session<inner_cut)&(mature.label_end<inner_boundary)].copy()
            val=mature[mature.session.isin(vv)].copy()
            assert tr.label_end.max()<inner_boundary<inner_cut and val.label_end.max()<outer_boundary
            assert tr.session.nunique()>=8
            fold_records.append(dict(outer_cutoff=str(cut),fold=fold,inner_cutoff=str(inner_cut),
              embargo_boundary=str(inner_boundary),train_rows=len(tr),train_dates=tr.session.nunique(),
              train_max_label_end=str(tr.label_end.max()),validation_dates=[str(d) for d in vv],
              validation_max_label_end=str(val.label_end.max()),outer_embargo_boundary=str(outer_boundary)))
            for cfg in CONFIGS:
                b=fit(cfg,tr,inner_cut);pred=predict(b,val)
                for a in ALPHAS:store[cfg['id']][a].extend(validation_rows(pred,val,cfg,a,fold))
        for cfg in CONFIGS:
            for a in ALPHAS:
                values=store[cfg['id']][a];dd=np.array([r['delta'] for r in values]);fm=[np.mean([r['delta'] for r in values if r['fold']==f]) for f in range(3)]
                ledger.append(dict(candidate=cfg['id']+f'_a{a}',config_id=cfg['id'],alpha=a,**{k:v for k,v in cfg.items() if k!='id'},
                    outer_cutoff=str(cut),validation_periods=len(values),mean_delta=float(dd.mean()),
                    std_delta=float(dd.std(ddof=1)),selection_score=float(dd.mean()-.25*dd.std(ddof=1)),
                    positive_folds=sum(v>0 for v in fm),fold_mean_deltas=fm,validation_details=values))
        selected={
            'tuned_regression':choose(ledger,'regression'),
            'tuned_ranking':choose(ledger,'ranking'),
            'tuned_residual':choose(ledger,'residual'),
            'adaptive':choose(ledger),
            'pure_regression':choose(ledger,'regression',True),
            'pure_ranking':choose(ledger,'ranking',True)}
        nextcut=cuts[i+1] if i+1<len(cuts) else date(2026,1,1)
        xx=x[x.session.isin(pred_dates)&(x.session>=cut)&(x.session<nextcut)&(x.quality_gate==1)].copy()
        assert xx.groupby('session').size().min()>=10
        cache={};model_audit=[]
        for portfolio,r in selected.items():
            if r['candidate']=='rule':
                z=xx[['session','instrument_id','cash_yield365']].copy()
                z['score']=z.groupby('session').cash_yield365.rank(pct=True)
                z['raw_prediction']=np.nan
            else:
                cid=r['config_id'];cfg=next(c for c in CONFIGS if c['id']==cid)
                if cid not in cache:
                    b=fit(cfg,mature,cut);cache[cid]=b
                    modelpath=OUT/'models';modelpath.mkdir(exist_ok=True)
                    b['model'].booster_.save_model(str(modelpath/f'{cut}_{cid}.txt'))
                    model_audit.append(dict(config_id=cid,**{k:v for k,v in b.items() if k!='model'}))
                b=cache[cid];z=predict(b,xx);z['score']=score(z,cfg,r['alpha'])
            z['portfolio']=portfolio;z['training_cutoff']=cut;z['candidate']=r['candidate']
            z['return_opportunity_score']=z.groupby('session').score.rank(pct=True)*100
            predictions.append(z)
            choice_rows.append(dict(portfolio=portfolio,cutoff=str(cut),**r))
        audit.append(dict(cutoff=str(cut),outer_embargo_boundary=str(outer_boundary),
            mature_train_rows=len(mature),mature_train_dates=len(dates),mature_train_banks=mature.instrument_id.nunique(),
            max_label_end=str(mature.label_end.max()),inner_splits=fold_records,final_model_audit=model_audit))
        ledger_all.extend(ledger)
        print('TUNED',cut,'candidates',len(ledger),'chosen',{k:r['candidate'] for k,r in selected.items()},flush=True)
        # Checkpoint complete rounds without overwriting frozen previous experiments.
        dump(OUT/f'inner_validation_{cut}.json',ledger)
        dump(OUT/f'choices_{cut}.json',selected)
    pd.concat(predictions,ignore_index=True).to_parquet(OUT/'outer_predictions.parquet',index=False)
    dump(OUT/'all_trial_ledger.json',ledger_all);dump(OUT/'nested_timing_audit.json',audit)
    dump(OUT/'selected_candidates.json',choice_rows)
    print('TUNING_COMPLETE',len(ledger_all),'trial records',flush=True)

def backtest():
    x,data,cuts,_,_,_=load()
    p=pd.read_parquet(OUT/'outer_predictions.parquet')
    q=json.loads((ROOT/'reports/strategy_backtests/20261009-192158-d5f6de.request.json').read_text(encoding='utf-8'))
    for key,name in PORTS.items():
        dest=OUT/f'portfolio_{key}.json'
        if dest.exists():print('EXISTING',key,flush=True);continue
        schedule={};chosen=[]
        for d in cuts:
            g=p[(p.session==d)&(p.portfolio==key)].sort_values(['score','instrument_id'],ascending=[False,True]).head(10)
            assert len(g)==10
            schedule[d]={code:.1 for code in g.instrument_id}
            chosen.extend(dict(portfolio=key,signal=d,code=r.instrument_id,score=float(r.score),
                              opportunity_score=float(r.return_opportunity_score),candidate=r.candidate) for r in g.itertuples())
        dump(OUT/f'targets_{key}.json',{str(d):v for d,v in schedule.items()})
        request=StrategyBacktestRequest.model_validate(dict(q,name='银行内部调优研究·'+name,start=str(cuts[0])))
        dump(OUT/f'request_{key}.json',request.model_dump(mode='json'))
        print('BACKTEST_START',key,flush=True)
        last=[-1]
        def progress(v):
            a=int(v.get('progress',0))//25
            if a>last[0]:last[0]=a;print('BACKTEST_PROGRESS',key,v.get('progress'),v.get('current_session'),flush=True)
        r=run_backtest(ROOT,request,target_schedule=schedule,include_selection_history=True,progress_callback=progress)
        r['research_signal_source']='Outer forward predictions; hyperparameters selected by date-purged inner validation only. Request score_rules is only a preflight anchor.'
        r['research_targets_sha256']=sha(OUT/f'targets_{key}.json')
        assert r['status']=='PASS';dump(dest,r)
        pd.DataFrame(chosen).to_parquet(OUT/f'selections_{key}.parquet',index=False)
        print('BACKTEST_DONE',key,'annual',r['summary']['annualized_return'],'DD',r['summary']['maximum_drawdown'],flush=True)

def verify():
    x,_,cuts,_,_,calendar=load();p=pd.read_parquet(OUT/'outer_predictions.parquet')
    audit=json.loads((OUT/'nested_timing_audit.json').read_text(encoding='utf-8'))
    assert len(audit)==16 and not p.duplicated(['portfolio','session','instrument_id']).any()
    assert (p.training_cutoff<=p.session).all() and p.score.notna().all()
    assert len(json.loads((OUT/'all_trial_ledger.json').read_text(encoding='utf-8')))==16*108
    for a in audit:
        assert a['max_label_end']<a['outer_embargo_boundary']<a['cutoff']
        for b in a['inner_splits']:
            assert b['train_max_label_end']<b['embargo_boundary']<b['inner_cutoff']
            assert b['validation_max_label_end']<b['outer_embargo_boundary']
    checks={}
    for k in PORTS:
        r=json.loads((OUT/f'portfolio_{k}.json').read_text(encoding='utf-8'));d=pd.DataFrame(r['daily']);t=pd.DataFrame(r['trades']);nav=d.nav.to_numpy()
        assert len(d)==969 and r['summary']['rebalance_count']==16
        assert math.isclose((nav[-1]/nav[0])**(252/(len(nav)-1))-1,r['summary']['annualized_return'],abs_tol=1e-9)
        assert math.isclose(float(min(nav/np.maximum.accumulate(nav)-1)),r['summary']['maximum_drawdown'],abs_tol=1e-9)
        assert math.isclose(float(t.total_cost_cny.sum()),r['summary']['total_cost'],abs_tol=.03)
        assert math.isclose(float(d.dividend_cash.sum()),r['summary']['total_dividend_cash'],abs_tol=.03)
        selected=pd.read_parquet(OUT/f'selections_{k}.parquet').merge(x[['session','instrument_id','quality_gate']],
           left_on=['signal','code'],right_on=['session','instrument_id'],how='left',validate='many_to_one')
        assert selected.session.notna().all() and (selected.quality_gate==1).all()
        assert (selected.groupby('signal').size()==10).all()
        checks[k]=dict(status='PASS',signal_count=16,trades=len(t),sessions=len(d),gate='PASS')
    dump(OUT/'final_validation.json',dict(status='PASS',nested_timing='PASS',trial_records=16*108,portfolio_checks=checks,
                                       production_edited=False,source_data_edited=False))
    print('VERIFICATION_PASS',flush=True)

def report():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei'];plt.rcParams['axes.unicode_minus']=False
    results={}
    for k in ['bank_equal','gate_cash10','lgb_gate10']:
        r=json.loads((SOURCE/f'portfolio_{k}.json').read_text(encoding='utf-8'))
        results[k]=r
    for k in PORTS:results[k]=json.loads((OUT/f'portfolio_{k}.json').read_text(encoding='utf-8'))
    names={'bank_equal':'银行等权','gate_cash10':'原规则：质量门槛＋股息率','lgb_gate10':'上一版固定LightGBM',**PORTS}
    rows=[dict(strategy=k,name=names[k],**r['summary'],annual={str(a['year']):a['return'] for a in r['annual']}) for k,r in results.items()]
    choices=json.loads((OUT/'selected_candidates.json').read_text(encoding='utf-8'))
    cc=pd.DataFrame(choices)
    counts=cc.groupby(['portfolio','family','alpha']).size().rename('rounds').reset_index()
    best=max([r for r in rows if r['strategy'] in PORTS],key=lambda r:r['annualized_return'])
    base=next(r for r in rows if r['strategy']=='gate_cash10')
    decision='内部调优后仍未超过原规则，保留规则主方案。' if best['annualized_return']<=base['annualized_return'] else '内部调优有方案超过原规则；仍需结合回撤与逐年结果，暂不进行生产迁移。'
    pred=pd.read_parquet(OUT/'outer_predictions.parquet');lab=pd.read_parquet(SOURCE/'labels_only.parquet')
    evaldates={date.fromisoformat(d) for d in json.loads((SOURCE/'schedule_dates.json').read_text(encoding='utf-8'))['eval_dates']}
    z=pred[pred.session.isin(evaldates)].merge(lab,on=['session','instrument_id'],validate='many_to_one');z=z[z.valid_label]
    metrics=[]
    for (k,d),g in z.groupby(['portfolio','session']):
        h=g.sort_values(['score','instrument_id'],ascending=[False,True]).head(10)
        metrics.append(dict(portfolio=k,session=d,rank_ic=float(g.score.corr(g.relative_return63,method='spearman')),
           top10_relative=float(h.relative_return63.mean()),top10_absolute=float(h.total_return63.mean()),bank_return=float(h.bank_return63.iloc[0])))
    md=pd.DataFrame(metrics);md.to_parquet(OUT/'prediction_diagnostics.parquet',index=False)
    dg=md.groupby('portfolio').agg(periods=('session','nunique'),rank_ic=('rank_ic','mean'),
                                  top10_relative=('top10_relative','mean')).reset_index()
    fig,ax=plt.subplots(figsize=(11,4.7))
    for k in ['bank_equal','gate_cash10','lgb_gate10','adaptive','tuned_residual','pure_ranking']:
        d=pd.DataFrame(results[k]['daily']);ax.plot(pd.to_datetime(d.session),d.nav/d.nav.iloc[0],label=names[k],lw=1.3)
    ax.set_title('银行内部调优：同区间、同成本净值对照');ax.grid(alpha=.2);ax.legend(fontsize=9);fig.tight_layout()
    fig.savefig(OUT/'curves.svg');fig.savefig(OUT/'curves.png',dpi=140);plt.close(fig)
    pct=lambda v:f'{v*100:.2f}%'
    table='<table><tr><th>方案</th><th>年化收益</th><th>累计收益</th><th>最大回撤</th><th>夏普</th></tr>'
    for r in rows:table+=f'<tr><td>{r["name"]}</td><td>{pct(r["annualized_return"])}</td><td>{pct(r["total_return"])}</td><td>{pct(r["maximum_drawdown"])}</td><td>{r["sharpe"]:.3f}</td></tr>'
    table+='</table>'
    annual='<table><tr><th>方案</th>'+''.join(f'<th>{y}</th>' for y in range(2022,2026))+'</tr>'
    for r in rows:annual+='<tr><td>'+r['name']+'</td>'+''.join('<td>'+pct(r['annual'][str(y)])+'</td>' for y in range(2022,2026))+'</tr>'
    annual+='</table>'
    body=f'<section id="bank-nested-tuning-20261009"><h2>2026-10-09 银行模型内部调优结果</h2><p><strong>{decision}</strong></p>'
    body+=f'<p>历史结果最优的调优方案为“{best["name"]}”，年化{pct(best["annualized_return"])}、最大回撤{pct(best["maximum_drawdown"])}；原规则年化{pct(base["annualized_return"])}、回撤{pct(base["maximum_drawdown"])}。这里的“最优”是展示后的历史比较，不是拿外层结果选参数后又重跑。</p>'
    body+=table+annual+(OUT/'curves.svg').read_text(encoding='utf-8')
    body+='<h3>这轮怎样调参</h3><p>固定36种模型配置：收益回归、排名学习、股息率残差修正三类；10个核心变量或23个全变量；小/中/较灵活三种容量；扩展历史或最近3年窗口。每种尝试25%、50%、100%的模型混合或修正强度，共108个候选，再加原规则退回选项。全部候选和失败都写入试验台账，共16轮×108=1728条内部验证记录。</p>'
    body+='<p>每次外层预测前，只取已经走完未来63日收益、且在当时通过质量门槛的历史样本。最近9个成熟采样日分为3块内部验证；每块训练标签结束日严格早于验证起点前5个交易日，内部验证标签也必须在外层预测前成熟。只用训练期决定缺失列筛选。没有随机拆分银行日期，没有把预测日之后的收益用于当日调参。</p>'
    body+='<p>内部选择目标：前10平均相对收益比原股息率排序提高多少，减去差值波动的25%。只有平均改善至少0.2个百分点且3个内部验证块中至少2块改善，才允许修正；否则退回原规则。纯回归/纯排名两个对照始终使用内部选出的算法，以便识别“算法改善”与“退回规则”之间的区别。内层目标仍是税前标签收益，外层另用现有真实账户引擎核算成本、股数与权益。</p>'
    body+='<h3>究竟多少轮使用了算法</h3>'+counts.to_html(index=False)
    body+='<p>family=rule表示本轮未通过内部改善条件，完全沿用股息率；alpha表示算法混合/修正强度。结果若接近原规则，可能是大量退回规则，而不能宣称纯机器学习学出了同样稳健的策略。</p>'
    body+='<h3>预测排序诊断</h3>'+dg.to_html(index=False,float_format=lambda v:f'{v:.4f}')
    body+='<p>45个成熟采样期的平均指标，收益区间重叠，不是独立试验，也不是策略年化。模型只输出相对收益机会排序；不会把高分直接解释为基本面健康或准确合理价格。</p>'
    body+='<h3>修正公式</h3><p>收益/排名混合：分数=(1−α)×当日股息率百分位＋α×当日模型预测百分位。残差修正：先用历史训练数据估计股息率与相对收益的非负斜率，树模型学习尚未解释的收益；最终分数=股息率百分位＋α×0.20×tanh(当日去均值的残差预测/训练期残差尺度)。残差尺度不小于0.02；修正幅度有限，不允许完全抛弃原股息率基础。</p>'
    body+='<h3>最后一轮内部选择（并非外层结果挑选）</h3>'
    last=cc.cutoff.max();last_table=cc[cc.cutoff==last][['portfolio','cutoff','candidate','family','alpha','mean_delta','selection_score']]
    body+=last_table.to_html(index=False,float_format=lambda v:f'{v:.4f}')
    body+='<p>同区间2022-01-04至2025-12-31，16次信号、969个交易日，100万元、前10等权、每63日换仓、2%现金预留；佣金、历史税费、滑点、参与率与之前保持一致，T收盘信号最早T+1开盘成交。原规则和旧模型结果直接复用原始文件。</p>'
    body+='<p>限制：2020–2025此前已被研究者查看，本轮内部时间隔离不能恢复未看过的历史样本。当前42银行来源集合及财务历史版本、部分资本事件的认证限制仍在；缺失未当作零或健康。所有输出保持RESEARCH_ONLY，后续新增日期验证才能提高结论可信度。没有请求新财务/价格数据，没有改前后端或原始库。</p>'
    body+='<p><a href="https://lightgbm.readthedocs.io/en/stable/Parameters.html">LightGBM官方参数及排名目标文档</a>。完整参数、每轮选择、所有内部候选、模型文件、预测、账户回测和核验记录均保留。</p></section>'
    style='<style>body{max-width:1250px;margin:32px auto;padding:0 24px;font:16px/1.7 "Microsoft YaHei",sans-serif;color:#26372f}table{border-collapse:collapse;width:100%;margin:16px 0}td,th{padding:8px;border:1px solid #dce5df;text-align:right}td:first-child,th:first-child{text-align:left}svg{width:100%;height:auto}h2,h3{color:#176749}</style>'
    (OUT/'report.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><title>银行内部调优研究</title>'+style+'<body>'+body+'</body></html>',encoding='utf-8')
    dump(OUT/'summary.json',dict(decision=decision,portfolios=rows,best_tuned_strategy=best['strategy'],
      choices=counts.to_dict('records'),diagnostics=dg.to_dict('records'),status='RESEARCH_ONLY',
      total_validation_trials=1728,spec_sha256=sha(OUT/'experiment_spec_before_tuning.json')))
    old=MAIN.read_bytes();at=old.rfind(b'</body>');assert at>=0
    if b'id="bank-nested-tuning-20261009"' not in old:
        archive=MAIN.parent/'prior_report_archive';archive.mkdir(exist_ok=True);h=hashlib.sha256(old).hexdigest()
        backup=archive/f'before_nested_tuning_20261009_{h[:12]}.html';backup.write_bytes(old)
        new=old[:at]+body.encode('utf-8')+old[at:];MAIN.write_bytes(new)
        assert new.startswith(old[:at]) and new.endswith(old[at:])
        dump(OUT/'integration_receipt.json',dict(report=str(MAIN),archive=str(backup),old_sha256=h,
             new_sha256=sha(MAIN),previous_bytes_preserved=True))
    print('REPORT_DONE',json.dumps([dict(name=r['name'],annual=r['annualized_return'],dd=r['maximum_drawdown']) for r in rows],ensure_ascii=False),flush=True)

if __name__=='__main__':
    stage=sys.argv[1] if len(sys.argv)>1 else 'all'
    if stage in ('tune','all'):tune()
    if stage in ('backtest','all'):backtest()
    if stage in ('verify','all'):verify()
    if stage in ('report','all'):report()
