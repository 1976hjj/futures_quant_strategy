"""Yield compensation, broad dividend stability and analytical entry experiments.

Fetch -> prepare (no labels) -> analyse -> PDF. Never reads outcomes after2025.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd
import requests

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts.research.research_bank_dividend_rebound import labels,read_daily,regression,sha,summary
from scripts.research.research_bank_sector_timing import write_json,holm_adjust,nonoverlap_positions

BASE=ROOT/'reports/bank_sector_research/20261012-082921-dividend-horizons'
RATES=ROOT/'data/research_external/chinabond/20261012'
HORIZONS=(126,252)
RULES={'yield5':'银行均值股息率至少5%', 'spread2':'股息率减10年国债至少2个百分点',
       'spread3':'股息率减10年国债至少3个百分点',
       'yield5_stable':'至少5%且分红广度稳定', 'spread2_stable':'补偿至少2个百分点且分红广度稳定'}
PROFILES={'immediate':'一次投入','split3':'三次等额投入','confirm20':'最多等20日趋势确认'}


def fetch_rates():
    RATES.mkdir(parents=True,exist_ok=True)
    if (RATES/'manifest.json').exists():
        print(json.dumps({'rate_archive':str(RATES),'cached':True}));return
    def one(year):
        url='https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery'
        params=dict(startDate=f'{year}-01-01',endDate=f'{year}-12-31',gjqx='0',qxId='ycqx',locale='cn_ZH')
        dest=RATES/f'{year}.html'
        if dest.exists():raw=dest.read_bytes()
        else:
            response=requests.get(url,params=params,timeout=30);response.raise_for_status()
            raw=response.content;dest.write_bytes(raw)
        table=pd.read_html(StringIO(raw.decode('utf-8').replace('&nbsp','')))[1]
        # This official endpoint returns three curves and a header row. Never
        # accidentally pool corporate AAA and negotiable-CD curves with CGBs.
        government=table[table.iloc[:,0].astype(str).str.contains('国债',regex=False)]
        result=pd.DataFrame({'observation_date':pd.to_datetime(government.iloc[:,1]),
                             'cgb10y':pd.to_numeric(government.iloc[:,8])/100})
        if not len(result) or result.observation_date.dt.year.ne(year).any() or result.cgb10y.isna().any():
            raise ValueError(f'Invalid official curve response: {year}')
        return result,dict(year=year,url=requests.Request('GET',url,params=params).prepare().url,
                           file=dest.name,sha256=sha(dest),rows=len(result))
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(one,range(2016,2026)))
    frame=pd.concat([r[0] for r in results]).sort_values('observation_date').reset_index(drop=True)
    if frame.observation_date.duplicated().any() or frame.observation_date.max()>pd.Timestamp('2025-12-31'):
        raise ValueError('Duplicate or unreleased rate observations')
    frame.to_parquet(RATES/'cgb10y.parquet',index=False)
    write_json(RATES/'manifest.json',dict(source='ChinaBond government bond yield curve, official PBC history endpoint',
        retrieved_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),rows=len(frame),
        start=str(frame.observation_date.min().date()),end=str(frame.observation_date.max().date()),
        historical_snapshot_not_first_publication_certified=True,
        parquet_sha256=sha(RATES/'cgb10y.parquet'),requests=[r[1] for r in results]))
    print(json.dumps({'rate_archive':str(RATES),'rows':len(frame)}))


def lagged_rates(frame,calendar):
    joined=pd.merge_asof(pd.DataFrame({'session':calendar}),frame.sort_values('observation_date'),
                         left_on='session',right_on='observation_date',direction='backward',allow_exact_matches=False)
    age=(joined.session-joined.observation_date).dt.days
    joined.loc[age.gt(10),'cgb10y']=np.nan
    return joined.set_index('session')


def stability(panel,events,calendar):
    cash=panel.pivot(index='session',columns='ts_code',values='cash_per_current_share_cycle').reindex(calendar)
    scales=pd.DataFrame(1.,index=calendar,columns=cash.columns)
    for e in events.itertuples():
        if e.ts_code in scales and pd.notna(e.stk_div) and e.stk_div>0:
            available=max(pd.Timestamp(e.ex_date),pd.Timestamp(e.imp_ann_date)+pd.Timedelta(days=1))
            scales.loc[scales.index>=available,e.ts_code]*=1+e.stk_div
    records=[]
    for i,day in enumerate(calendar):
        row=dict(session=day,matched_cash_banks=0,declining_cash_fraction=np.nan,median_cash_growth=np.nan)
        if i>=252:
            now=cash.iloc[i].to_numpy();old=cash.iloc[i-252].to_numpy()*scales.iloc[i-252].to_numpy()/scales.iloc[i].to_numpy()
            valid=np.isfinite(now)&np.isfinite(old)&(now>=0)&(old>=0)
            row['matched_cash_banks']=int(valid.sum())
            if valid.sum()>=max(10,int(np.ceil(.7*cash.iloc[i].notna().sum()))):
                row['declining_cash_fraction']=float((now[valid]<old[valid]-1e-12).mean())
                ratio=valid&(old>0)
                if ratio.sum()>=10:row['median_cash_growth']=float(np.median(now[ratio]/old[ratio]-1))
        records.append(row)
    return pd.DataFrame(records).set_index('session')


def prepare():
    bspec=json.loads((BASE/'experiment_spec.json').read_bytes())
    end=json.loads((ROOT/'config/bank_timing_data.json').read_bytes())['released_feature_end']
    if end!='2025-12-31' or bspec['end']!=end:raise ValueError('This study is explicitly2025-bounded')
    manifest=json.loads((RATES/'manifest.json').read_bytes())
    if sha(RATES/'cgb10y.parquet')!=manifest['parquet_sha256']:raise ValueError('Rate archive changed')
    audit=json.loads((BASE/'audit.json').read_bytes())
    if sha(BASE/'features.parquet')!=audit['files']['features.parquet']:raise ValueError('Prior features changed')
    folder=ROOT/'reports/bank_sector_research'/(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'-dividend-entry')
    folder.mkdir(parents=True,exist_ok=False)
    previous=bspec['prior_results']+[dict(path=str(BASE/'results.json'),sha256=sha(BASE/'results.json'),count=128)]
    family=len(RULES)*len(HORIZONS)*2*2
    spec=dict(version='bank-dividend-entry-1.0.0',created_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),end=end,
        scope='EXPOSED_HISTORY_EXPLORATORY_NOT_OOS_NOT_TRADABLE',rules=RULES,horizons=list(HORIZONS),targets=['price','basket_total'],
        family_size=family,joint_family_size=family+sum(x['count'] for x in previous),prior_results=previous,
        rate_policy='CGB10y official observation strictly before T; max10 calendar-day age, no interpolation; current snapshot not certified PIT',
        cash_policy='matched-bank252-session cash amount on current share basis; declining fraction<=25%; no share-price-unit sum weighting',
        controls='year and month, prior60 price return and volatility; HAC horizon+20; joint Holm all current and prior searches',
        entry_policy='known0->1 crossings; minimum127-session separation fixed before labels; profiles share signal date and exit T+1+h',
        profiles=PROFILES,profile_definitions=['immediate=T+1 close100%',
            'split3=T+1,T+21,T+41 closes each1/3; uninvested cash earns zero',
            'confirm20=first signal day in[T,T+20] with prior20 return>0 and price>=MA60, fill NEXT close; no confirmation stays cash, included as0'],
        entry_inference='descriptive paired episodes only, no new p-values or best-profile selection; full paths required for all profiles',
        chronology='leave-one-signal-year-out raw mean contrasts as descriptive audit, not additional significance tests',
        source_chart=bspec['source_chart'],source_csv_sha256=bspec['source_csv_sha256'],
        feature_source_path=str(BASE/'features.parquet'),feature_source_sha256=sha(BASE/'features.parquet'),
        basket_path=bspec['basket_path'],basket_sha256=bspec['basket_sha256'],
        rate_path=str(RATES/'cgb10y.parquet'),rate_sha256=sha(RATES/'cgb10y.parquet'),
        script_sha256=sha(__file__),dependencies={p:sha(ROOT/p) for p in ('scripts/research/research_bank_dividend_rebound.py','scripts/research/research_bank_sector_timing.py')},
        constitution_sha256=sha(ROOT/'RESEARCH_CONSTITUTION.md'),
        limitations=bspec['limits']+['yield minus government YTM is an income comparison, NOT an equity risk premium or arbitrage spread',
            'cash stability is realized reference cash, not forecast; bank aggregate may hide individual cuts',
            'entry capital-budget paths exclude cash interest,tax,cost,slippage and fills; price index cannot be directly traded',
            'waiting/splitting can reduce invested capital and risk; this is not necessarily timing alpha'])
    write_json(folder/'experiment_spec.json',spec)
    f=pd.read_parquet(BASE/'features.parquet')
    rates=pd.read_parquet(spec['rate_path'])
    f=f.join(lagged_rates(rates,f.index))
    src=Path(spec['source_chart'])
    with duckdb.connect() as c:
        panel=c.execute('SELECT * FROM read_parquet(?) WHERE session<=CAST(? AS DATE)',[str(src/'individual_yield_inputs.parquet'),end]).df()
        events=c.execute('SELECT * FROM read_parquet(?) WHERE ex_date<=CAST(? AS DATE) AND imp_ann_date<CAST(? AS DATE)',[str(src/'implemented_dividend_events.parquet'),end,end]).df()
    f=f.join(stability(panel,events,f.index))
    f['dividend_compensation']=f.mean_yield-f.cgb10y
    f['broad_stable']=f.declining_cash_fraction.le(.25).astype(float).where(f.declining_cash_fraction.notna())
    known=f.yield_rank.notna()
    f['yield5']=f.mean_yield.ge(.05).astype(float).where(known)
    for threshold,name in ((.02,'spread2'),(.03,'spread3')):
        f[name]=f.dividend_compensation.ge(threshold).astype(float).where(known&f.cgb10y.notna())
    for base in ('yield5','spread2'):
        f[base+'_stable']=(f[base].eq(1)&f.broad_stable.eq(1)).astype(float).where(f[base].notna()&f.broad_stable.notna())
    if f.index.max()>pd.Timestamp(end):raise ValueError('Unreleased features')
    f.to_parquet(folder/'features.parquet')
    write_json(folder/'feature_manifest.json',dict(spec_sha256=sha(folder/'experiment_spec.json'),feature_sha256=sha(folder/'features.parquet'),
        labels_computed=False,no2026=True,rate_known_days=int(f.cgb10y.notna().sum()),
        counts={rule:dict(high=int(f[rule].eq(1).sum()),other=int(f[rule].eq(0).sum())) for rule in RULES}))
    print(json.dumps({'folder':str(folder),'family':family,'phase':'PREPARED'}))


def capital_path(levels,anchor,horizon,profile,trend):
    """Analytical budget paths with next-close fills and a common exit."""
    a,b=anchor+1,anchor+1+horizon
    if b>=len(levels):return None
    path=np.asarray(levels[a:b+1],float)
    if not np.isfinite(path).all() or (path<=0).any():return None
    if profile=='immediate':jobs={a:1.}
    elif profile=='split3':jobs={a:1/3,a+20:1/3,a+40:1/3}
    elif profile=='confirm20':
        signal=next((j for j in range(anchor,min(anchor+21,len(trend))) if trend[j]==1),None)
        jobs={} if signal is None else {signal+1:1.}
    else:raise ValueError('Unknown entry profile')
    cash,units=1.,0.;nav=[];exposure=[]
    for i in range(a,b+1):
        weight=jobs.get(i,0.);units+=weight/levels[i];cash-=weight
        nav.append(cash+units*levels[i]);exposure.append(1-cash)
    nav=np.asarray(nav)
    return dict(capital_return=float(nav[-1]-1),drawdown=float(np.min(nav/np.maximum.accumulate(nav)-1)),
        minimum_return=float(nav.min()-1),filled=bool(jobs),first_fill_offset=min(jobs)-anchor if jobs else None,
        average_invested_fraction=float(np.mean(exposure)))


def analyse(folder):
    spec=json.loads((folder/'experiment_spec.json').read_bytes());fm=json.loads((folder/'feature_manifest.json').read_bytes())
    for p,d in [(folder/'experiment_spec.json',fm['spec_sha256']),(folder/'features.parquet',fm['feature_sha256']),
                 (Path(__file__),spec['script_sha256']),(Path(spec['basket_path']),spec['basket_sha256']),
                 *[(ROOT/p,d) for p,d in spec['dependencies'].items()]]:
        if sha(p)!=d:raise ValueError('Frozen dependency changed')
    if (folder/'results.json').exists():raise ValueError('Immutable result exists')
    f=pd.read_parquet(folder/'features.parquet');src=Path(spec['source_chart'])
    if sha(src/'银行平均股息率与价格指数.csv')!=spec['source_csv_sha256']:raise ValueError('Source changed')
    daily=read_daily(src,spec['end'],['session','price_index']);daily.index=pd.DatetimeIndex(daily.index)
    if not daily.index.equals(f.index):raise ValueError('Calendar mismatch')
    basket=pd.read_parquet(spec['basket_path']);basket=basket.set_index(pd.to_datetime(basket.session)).reindex(f.index)
    levels={'price':daily.price_index.to_numpy(),'basket_total':basket.bank_equal_total_return_index.to_numpy()}
    labs={'price':labels(levels['price'],f.index,horizons=HORIZONS),
          'basket_total':labels(levels['basket_total'],f.index,horizons=HORIZONS,segments=basket.basket_segment,ready=basket.status.eq('READY'))}
    tests=[];groups=[];leaveouts=[];entries=[]
    for target,lab in labs.items():
        lab.to_parquet(folder/f'labels_{target}.parquet',index=False)
        for h in HORIZONS:
            y=lab[lab.horizon.eq(h)].set_index('session').reindex(f.index)
            for rule in RULES:
                high=f[rule].eq(1);low=f[rule].eq(0)
                for name,mask in (('high',high),('other',low)):
                    groups.append(dict(target=target,horizon=h,rule=rule,group=name,**summary(y,mask,h)))
                for controlled in (False,True):
                    tests.append(dict(target=target,horizon=h,rule=rule,model='controlled' if controlled else 'raw',
                                      **regression(f[rule],y['return'],f,h+20,controlled)))
                for year in range(2018,2026):
                    mask=f.index.year!=year
                    ah=y.loc[high&mask,'return'].dropna();al=y.loc[low&mask,'return'].dropna()
                    leaveouts.append(dict(target=target,horizon=h,rule=rule,excluded_signal_year=year,
                                          high_n=len(ah),other_n=len(al),difference=ah.mean()-al.mean()))
                crossings=f[rule].eq(1)&f[rule].shift(1).eq(0)
                for i in nonoverlap_positions(crossings.to_numpy(),126):
                    if y.iloc[i].status!='READY':continue
                    for profile in PROFILES:
                        path=capital_path(levels[target],i,h,profile,f.trend_recovery.to_numpy())
                        if path is None:raise ValueError('Label/path disagreement')
                        entries.append(dict(target=target,horizon=h,rule=rule,profile=profile,signal=f.index[i],exit=y.iloc[i].exit,**path))
    prior=[]
    for item in spec['prior_results']:
        if sha(item['path'])!=item['sha256']:raise ValueError('Related results changed')
        old=json.loads(Path(item['path']).read_bytes())
        records=old.get('tests',old.get('single_tests',[])+old.get('combinations',[]))
        if len(records)!=item['count']:raise ValueError('Prior family changed')
        prior+=records
    if len(tests)!=spec['family_size']:raise ValueError('Family incomplete')
    local=holm_adjust([t['p_value'] for t in tests]);joint=holm_adjust([t['p_value'] for t in prior+tests])[len(prior):]
    for t,p,q in zip(tests,local,joint):t.update(holm_current=p,holm_joint=q)
    entries=pd.DataFrame(entries);entry_summary=[]
    for (target,h,rule,profile),g in entries.groupby(['target','horizon','rule','profile']):
        entry_summary.append(dict(target=target,horizon=int(h),rule=rule,profile=profile,n=len(g),filled=int(g.filled.sum()),
            mean_return=g.capital_return.mean(),win_rate=g.capital_return.gt(0).mean(),mean_drawdown=g.drawdown.mean(),
            mean_minimum_return=g.minimum_return.mean(),mean_invested_fraction=g.average_invested_fraction.mean()))
    result=dict(scope=spec['scope'],family_size=len(tests),joint_family_size=spec['joint_family_size'],groups=groups,tests=tests,
                leave_one_year_out=leaveouts,entry_summary=entry_summary,no2026_outcomes=True)
    write_json(folder/'results.json',result)
    entries.to_csv(folder/'entry_paths.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(tests).to_csv(folder/'hypothesis_statistics.csv',index=False,encoding='utf-8-sig')
    write_json(folder/'audit.json',dict(files={p.name:sha(p) for p in folder.iterdir() if p.is_file()},no2026=True,
                immutable_sources=True,max_label_exit=str(max(pd.to_datetime(lab.exit).max() for lab in labs.values()).date())))
    print(json.dumps({'folder':str(folder),'phase':'COMPLETE','family':len(tests),'joint_family':result['joint_family_size']}))


def pdf(folder):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak,Image
    from xml.sax.saxutils import escape
    from pypdf import PdfReader
    r=json.loads((folder/'results.json').read_bytes());spec=json.loads((folder/'experiment_spec.json').read_bytes())
    prev=json.loads((BASE/'results.json').read_bytes());f=pd.read_parquet(folder/'features.parquet')
    audit=json.loads((folder/'audit.json').read_bytes())
    for name,d in audit['files'].items():
        if sha(folder/name)!=d:raise ValueError('Evidence changed before PDF')
    pdfmetrics.registerFont(TTFont('ResearchCN','C:/Windows/Fonts/msyh.ttc',subfontIndex=0))
    regular=ParagraphStyle('body',fontName='ResearchCN',fontSize=9.6,leading=16,textColor=colors.HexColor('#223c32'),spaceAfter=8,wordWrap='CJK')
    heading=ParagraphStyle('head',parent=regular,fontSize=19,leading=27,spaceAfter=16)
    subtitle=ParagraphStyle('sub',parent=regular,fontSize=12,leading=19,spaceBefore=8,spaceAfter=10)
    small=ParagraphStyle('small',parent=regular,fontSize=8,leading=12)
    out=ROOT/'output/pdf'/('银行板块高股息率与入场研究_'+folder.name.split('-')[0]+'_'+folder.name.split('-')[1]+'.pdf')
    out.parent.mkdir(parents=True,exist_ok=True)
    if out.exists():raise ValueError('PDF already exists; do not overwrite a delivered artifact')
    story=[];width=174*mm
    def para(text,style=regular):story.append(Paragraph(escape(text),style))
    def table(headers,rows,widths=None):
        data=[[Paragraph(escape(str(x)),small) for x in headers]]+[[Paragraph(escape(str(x)),small) for x in row] for row in rows]
        t=Table(data,colWidths=widths or [width/len(headers)]*len(headers),repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#deebe4')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f4f7f3')]),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,0),(-1,0),.7,colors.HexColor('#779887'))]))
        story.extend([t,Spacer(1,10)])
    def find(rows,**keys):return next(x for x in rows if all(x[k]==v for k,v in keys.items()))
    def pct(x):return '不足' if x is None or not np.isfinite(x) else f'{x*100:.2f}%'
    def page(title):story.append(PageBreak());para(title,heading)
    para('银行板块高股息率与入场研究',heading)
    para('期限、利率补偿、分红稳定与分批进入',subtitle)
    para('研究日期：2026年10月12日。收益检验截至2025年12月31日；2026年只用于此前已经授权的描述图。')
    para('这份报告继续探索高股息率的投资含义，不把某个60日检验不显著等同于整个方向无效。研究分成三个问题：高息区间是否提供收益补偿，分红稳定与利率环境能否帮助解释，以及怎样进入才能控制等待和回撤。')
    para('目前值得保留的线索',subtitle)
    para('一、此前5%-6%的绝对股息率区间，半年和一年价格表现较好，但样本集中于2022-2024。二、高位内分红参考稳定的表现优于下降组，仍须检查年份混杂。三、本轮补入官方10年国债历史曲线，比较相对补偿，并用固定事件日期比较一次投入、分三次投入和等待趋势确认。')
    para('需要区分“持有结果”和“入场优势”。长期正收益可能来自市场年份或分红补偿；等待、分批的较小回撤也可能只是资金暴露较少。本报告不把这些直接叫作策略收益或择时能力。')
    table(['研究范围','本轮内容'],[['特征历史','2016起；足够覆盖与预热后才定义条件'],['期限','60/120/126/252日回顾；本轮126/252日'],['新检验家族',f"本轮{r['family_size']}项；累计{r['joint_family_size']}项联合校正"],['新增来源','中债官方10年国债收益率曲线；2016-2025'],['执行性质','脚本研究；不修改前后端或因子定义']],[40*mm,134*mm])
    para('阅读顺序：先看已有线索，再看利率补偿和分红广度，然后比较半年与一年的入场方式；最后检查风险、来源和审计。',small)
    page('01 / 已有线索：期限不能一概而论')
    rows=[]
    for h in (60,120,126,252):
        lo=find(prev['buckets'],target='price',horizon=h,lower=.04);hi=find(prev['buckets'],target='price',horizon=h,lower=.05)
        rows.append([f'{h}日',pct(lo['mean_return']),pct(hi['mean_return']),str(hi['days']),str(hi['nonoverlap_n'])])
    table(['期限','均值4%-5%','均值5%-6%','高息日期数','高息非重叠窗口'],rows)
    para('表中是区间内日期之后的平均价格收益，不含现金分红，不是180次独立买点。5%-6%条件来自2022年30日、2023年100日、2024年50日；一年期只有2个按规则选出的非重叠观察。6%以上没有有效样本，不能推断越高越好。')
    rows=[]
    for h in (60,120,126,252):
        a=find(prev['groups'],target='price',horizon=h,rule='within_high80_cash',group='high',sample='all_observed')
        b=find(prev['groups'],target='price',horizon=h,rule='within_high80_cash',group='other',sample='all_observed')
        rows.append([f'{h}日',pct(a['mean_return']),pct(b['mean_return']),f"{a['days']}/{b['days']}"])
    table(['高位内部','分红参考未下降','分红参考下降','两组日期数'],rows)
    para('分红下降的153个高位日集中在2021和2022。此前“一年含分红收益差异很大”的结果，下降组全部在2022、未下降组全部在2023和2024，年份控制后无法识别条件，不能把该结果当成分红预测能力。')
    para('提示：滚动最高20%、最高10%与绝对5%并不等价。更极端的相对高位是线索，但不能看完结果后只保留最好的门槛。')
    page('02 / 新增比较：股息率与国债收益率')
    para('定义：银行平均股息率 - 10年国债到期收益率。它只是收益补偿参照，不是已实现超额收益、严格的股票风险溢价或无风险套利。股票的分红可能下降，国债有期限和利率风险。')
    para('取官方曲线严格早于T日的最近观察，最多容忍10个自然日；没有插值。它仍是当前历史快照，不是已经认证的每一天首次发布版本。')
    rows=[]
    for rule in ('yield5','spread2','spread3'):
        for h in HORIZONS:
            a=find(r['groups'],target='price',horizon=h,rule=rule,group='high');b=find(r['groups'],target='price',horizon=h,rule=rule,group='other')
            t=find(r['tests'],target='price',horizon=h,rule=rule,model='raw')
            rows.append([RULES[rule],str(h),pct(a.get('mean_return')),pct(b.get('mean_return')),f"{a['days']}/{b['days']}",f"{t['holm_joint']:.3f}"])
    table(['条件','日数','条件成立收益','其他日期收益','两组日期数','联合校正p'],rows,[52*mm,13*mm,28*mm,28*mm,27*mm,26*mm])
    para('阈值2、3个百分点在这一轮看收益标签之前固定，不按结果挑最好的。请先看均值差、样本年份及完整事件，再看p值。模型控制年份、月份、前60日收益与波动，控制模型也不能当成因果证据。')
    para('无条件均值差的年份敏感性',subtitle)
    rows=[]
    for rule in ('yield5','spread2','spread3'):
        for h in HORIZONS:
            values=[x['difference'] for x in r['leave_one_year_out'] if x['target']=='price' and x['rule']==rule and x['horizon']==h and x['difference'] is not None]
            rows.append([RULES[rule],str(h),pct(min(values)),pct(max(values))])
    table(['去掉任一信号年份','期限','差异最小值','差异最大值'],rows,[75*mm,20*mm,39*mm,40*mm])
    page('03 / 分红稳定：避免少数高金额银行主导')
    para('此前的同批银行分红金额加总，较高的每股金额银行可能贡献较大。本轮增加按银行家数计算的广度：同一批银行中，分红参考同比下降者占比不超过25%，即至少75%银行未下降。送转股统一股数单位；没有分红记录不当成零。')
    para('这也是“已实施年度参考”的稳定性，不是对下一年度的承诺。广度不能替代对大权重银行、资产质量和真实利润的核查。')
    rows=[]
    for rule in ('yield5_stable','spread2_stable'):
        for h in HORIZONS:
            a=find(r['groups'],target='price',horizon=h,rule=rule,group='high');b=find(r['groups'],target='price',horizon=h,rule=rule,group='other')
            t=find(r['tests'],target='price',horizon=h,rule=rule,model='controlled')
            rows.append([RULES[rule],str(h),pct(a.get('mean_return')),pct(b.get('mean_return')),f"{a['days']}/{b['days']}",f"{t['holm_joint']:.3f}"])
    table(['组合条件','期限','成立价格收益','其他日期收益','两组日期数','控制模型校正p'],rows,[52*mm,13*mm,28*mm,28*mm,27*mm,26*mm])
    para('组合条件与全部其他日期的比较，不等于分红稳定对同一高息样本的净增量；变化后的银行样本、年份覆盖都可能影响结果。需要结合前一轮高位内部的对照一起判断。')
    para('来源追踪已发现的金额下降',subtitle)
    para('在2021-08-04和2022-05-05两个复核点，同批银行加总下降较大的归档记录包括民生银行0.370至0.213元、浦发银行0.600至0.480元、浙商银行0.240至0.161元。金额已按当前每股单位核对；这是归档算术复核，不替代公告原文认证。')
    table(['信号年份','高息>=5%日期','其中广度稳定日期'],[[str(y),int(g.yield5.eq(1).sum()),int(g.yield5_stable.eq(1).sum())] for y,g in f.groupby(f.index.year) if g.yield5.eq(1).any()])
    para('若一个筛选条件几乎没有删掉任何样本，它也几乎没有提供新增信息；不能仅凭组合收益较高就宣称改进。')
    page('04 / 同一批事件：一次、分批、等待确认')
    para('只在前一天明确不满足、当天首次满足条件时选事件，事件至少间隔127个交易日。三种方式共享信号日期和最终退出日期；年度窗口仍可能重叠。先固定事件，再看未来路径。')
    table(['方式','固定定义'],[['一次投入','T+1收盘投入全部预算'],['三次等额','T+1、T+21、T+41收盘各投入1/3；未投入资金收益为零'],['趋势确认','最多等20日：20日涨幅为正且站上60日均线；确认后下一交易日收盘投入；未确认则持有现金，0收益也保留']],[37*mm,137*mm])
    for h in HORIZONS:
        if h==252:
            page('04 / 入场方式：一年期与资金暴露')
            para('一年期仍使用完全相同的信号选择规则；最终退出为T+1+252日。不延长等待确认者的退出日期，也不删除始终没有确认而保持现金的事件。')
        rows=[]
        for rule in ('yield5','spread2_stable'):
            for profile in PROFILES:
                try:e=find(r['entry_summary'],target='price',horizon=h,rule=rule,profile=profile)
                except StopIteration:continue
                rows.append([RULES[rule],str(h),PROFILES[profile],str(e['n']),pct(e['mean_return']),pct(e['mean_drawdown']),f"{e['filled']}/{e['n']}"])
        table(['事件条件','期限','投入方式','次数','预算收益','最大回撤均值','执行/事件'],rows,[46*mm,12*mm,33*mm,11*mm,24*mm,27*mm,21*mm])
        para('这里是指数上的预算路径，不是可交易回测。分批和等待降低平均投入比例，可能自然降低回撤；不因此自动获得更强择时。真实策略还需要股息到账、现金利息、手续费、税、滑点与成交模型。')
        if h==252:
            rows=[]
            for rule in ('yield5','spread2_stable'):
                for profile in PROFILES:
                    try:e=find(r['entry_summary'],target='price',horizon=h,rule=rule,profile=profile)
                    except StopIteration:continue
                    rows.append([RULES[rule],PROFILES[profile],pct(e['mean_invested_fraction']),pct(e['mean_minimum_return'])])
            table(['条件','方式','平均资金暴露','入场后最低预算收益均值'],rows,[52*mm,35*mm,35*mm,52*mm])
    page('05 / 风险、适用边界与下一轮')
    para('含分红篮子对照',subtitle)
    rows=[]
    for rule in ('yield5','spread2_stable'):
        for h in HORIZONS:
            a=find(r['groups'],target='basket_total',horizon=h,rule=rule,group='high');b=find(r['groups'],target='basket_total',horizon=h,rule=rule,group='other')
            rows.append([RULES[rule],str(h),pct(a.get('mean_return')),pct(b.get('mean_return')),str(a['days']),str(a['nonoverlap_n'])])
    table(['条件','期限','成立含分红收益','其他日期收益','成立日期数','非重叠窗口'],rows,[52*mm,13*mm,28*mm,28*mm,27*mm,26*mm])
    para('这条篮子是另外的等权含分红序列，仅2020-2025，跨断点窗口留空；不是399986官方全收益指数。样本日期和权重不同，不能把它与价格指数的均值直接相减，宣称差额全部是股息。')
    para('继续推进的优先级',subtitle)
    para('1. 保留绝对高息和利率补偿两个视角，做年份敏感性、同一批日期和完整事件对照，避免只追求看起来最好的阈值。')
    para('2. 分红稳定继续追到具体银行及公告期间；广度稳定与少数大权重银行的下降应分别记录。对真实削减和频率过渡保持区分。')
    para('3. 仓位方向先研究预算和回撤：分批是否改善最坏等待、现金是否拖累上涨。当前事件少，不能据此选出最优进入方式。')
    para('4. 后续市场风格识别可加入温和活跃度回暖与过热区分、信用风险和利率方向。但需要另行冻结定义，保留所有搜索，不能从已看过的最佳案例倒推规则。')
    para('结论的尺度',subtitle)
    para('本轮提供探索证据和更具体的研究方向，不给出“这个方向不行”或“已经能赚钱”的总判决。正均值、统计显著、稳定跨年份、能够成交和成本后盈利，是不同层次的要求。')
    page('06 / 来源、审计与文件整理')
    para('分红与价格：既有bank_token归档及历史银行成员资料，使用修正后的年度分红周期口径；频率变化、重复期间、异常来源和过期参考明确标记。')
    para('价格指数：中证银行399986价格序列的既有东方财富归档，不含现金股息再投资；银行股息率为有效银行等权均值。两者权重不同。')
    para('国债：中债官方“国债及其他债券收益率曲线”的国债曲线，10年期限；接口还返回企业债和中短期票据，解析时明确排除这些曲线。2016-2025各年原始HTML、请求URL、检索日期和哈希独立归档。')
    para('官方来源：https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery',small)
    para('曲线说明：https://yield.chinabond.com.cn/cbweb-cbrc-web/cbrc/showCbrc',small)
    para('本轮特征、规则、代码哈希在收益计算之前冻结。收益窗口从T+1收盘开始，未来标签截至2025年末。原始资料、旧因子和结构化实验结果没有覆盖。')
    para(f"本轮{r['family_size']}项检验，加上前五轮{r['joint_family_size']-r['family_size']}项，共{r['joint_family_size']}项联合Holm校正；不可计算的p按1处理。重叠收益通过HAC日历序列处理，仍受有效事件少与控制模型设定影响。")
    para('边界：历史分红、行业与利率快照未完成全部首次披露和修订认证；当前42家银行归档子集也不是完整市场历史。未使用未开放的2026收益进行检验。没有独立样本外验证，没有交易成本或真实资金回测。')
    para('用户要求的旧Markdown报告会从报告目录移除，原文及哈希压缩归档用于历史证据追溯；项目规则和技术说明不属于研究报告，不删除。所有核心计算以JSON、Parquet和CSV为证据，不以报告文字反写数据库。')
    para('可追溯的本轮目录：'+folder.name,small)
    para('计算脚本：scripts/research/research_bank_dividend_entry.py；输入、结果与校验：experiment_spec.json、feature_manifest.json、results.json、audit.json。',small)
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('ResearchCN',8);canvas.setFillColor(colors.HexColor('#61786d'))
        canvas.drawString(18*mm,12*mm,'银行板块研究 | 已查看历史的探索，不构成已验证策略')
        canvas.drawRightString(192*mm,12*mm,str(doc.page));canvas.restoreState()
    SimpleDocTemplate(str(out),pagesize=A4,leftMargin=18*mm,rightMargin=18*mm,topMargin=18*mm,bottomMargin=22*mm,
        title='银行板块高股息率与入场研究',author='本项目研究').build(story,onFirstPage=footer,onLaterPages=footer)
    reader=PdfReader(out);text=''.join(page.extract_text() or '' for page in reader.pages)
    if '国债' not in text or len(reader.pages)<7:raise ValueError('PDF text/page verification failed')
    write_json(folder/'pdf_manifest.json',dict(path=str(out),sha256=sha(out),pages=len(reader.pages),source_result_sha256=sha(folder/'results.json')))
    print(json.dumps({'pdf':str(out),'pages':len(reader.pages)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['fetch-rates','prepare','analyse','pdf']);p.add_argument('folder',type=Path,nargs='?')
    a=p.parse_args()
    if a.command=='fetch-rates':fetch_rates()
    elif a.command=='prepare':prepare()
    elif a.command=='analyse':analyse(a.folder.resolve())
    else:pdf(a.folder.resolve())
