"""Owner-authorized descriptive bank dividend-yield / price-index chart.

Does not publish factors, alter source archives, or fit timing decisions.
"""
from __future__ import annotations
import hashlib
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from alpha_research_os.factors.bank import cash_per_current_share, dividend_cycle
from alpha_research_os.kernel.holdout import ExposureLedger
from scripts.bank_factor_inputs import reconcile_dividend_events

START = date(2016, 1, 1)
END = date(2026, 10, 11)

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    run = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'-dividend-price'
    folder = ROOT / 'reports/bank_sector_research' / run
    folder.mkdir(parents=True, exist_ok=False)
    authorization = dict(owner_confirmed=True, user_statement='另外开放到2026.10月 2016年开始',
                         purpose='银行股息率与中证银行价格指数描述图', chart_start=str(START),
                         owner_released_through='2026-10-31', query_end=str(END),
                         warmup_start='2015-01-01', domains=['bank_raw_price','implemented_dividends',
                         'bank_vendor_dividend_yield','historical_bank_membership','CSI_399986_price_index'],
                         no_forward_labels_or_parameter_tuning=True)
    auth_file = folder / '查看范围授权.json'
    auth_file.write_text(json.dumps(authorization, ensure_ascii=False, indent=2), encoding='utf-8')
    ledger = ExposureLedger(ROOT / 'data/metadata/bank_chart_view_exposures.ndjson')
    event = ledger.append(actor='project-owner-authorized-assistant', vintage='bank-dividend-price:2016-01-01:2026-10-11',
                          purpose='Owner-approved descriptive bank yield and price-index chart; 2015 dividend warmup; no tuning',
                          capability_id='owner-authorization:sha256:'+digest(auth_file))
    authorization['exposure_event_id'] = event.event_id
    # Keep the authorization bytes matching the capability hash; store exposure separately.
    (folder/'本次查看登记.json').write_text(json.dumps({'authorization_sha256':digest(auth_file),
        'exposure_event_id':event.event_id,'ledger':'data/metadata/bank_chart_view_exposures.ndjson'},ensure_ascii=False,indent=2),encoding='utf-8')

    db = ROOT / 'data/warehouse/bank_token.duckdb'
    with duckdb.connect(str(db), read_only=True) as c:
        market = c.execute('''SELECT ts_code,trade_date,close,dv_ttm,source_file,source_sha256,source_row
            FROM daily_basic_complete WHERE trade_date BETWEEN ? AND ? ORDER BY ts_code,trade_date''', [START, END]).df()
        events = c.execute('''SELECT * FROM bank_dividend_events
            WHERE try_cast(ex_date AS DATE) BETWEEN DATE '2015-01-01' AND ?''',[END]).df()
        dividend_codes = {r[0] for r in c.execute('SELECT DISTINCT ts_code FROM dividend').fetchall()}
        aliases = c.execute("SELECT view_name,sql FROM duckdb_views() WHERE schema_name='main' AND view_name IN ('daily_basic_complete','bank_dividend_events')").fetchall()
    market['session'] = pd.to_datetime(market.trade_date).dt.normalize()
    if market.duplicated(['session','ts_code']).any():
        raise ValueError('Duplicate bank price keys')
    market = market[market.close.gt(0) & np.isfinite(market.close)].copy()
    with duckdb.connect(str(ROOT/'data/warehouse/alpha_research.duckdb'),read_only=True) as c:
        members = c.execute("""SELECT ts_code,in_date,out_date FROM research.sw_industry_membership
            WHERE l1_name='银行' AND in_date <= ? AND (out_date IS NULL OR out_date>=?)""", [END,START]).df()
    members['in_date']=pd.to_datetime(members.in_date)
    members['out_date']=pd.to_datetime(members.out_date)
    keep=np.zeros(len(market),dtype=bool)
    for m in members.itertuples():
        keep |= (market.ts_code.eq(m.ts_code) & market.session.ge(m.in_date)
                 & (market.session.lt(m.out_date) if pd.notna(m.out_date) else True)).to_numpy()
    market=market.loc[keep].copy()
    for col in ['ex_date','imp_ann_date','ann_date','end_date','record_date','pay_date']:
        events[col]=pd.to_datetime(events[col],errors='coerce').dt.date
        events[col]=events[col].astype(object).where(events[col].notna(),None)
    events=reconcile_dividend_events(events)
    computed=[];checks=[]
    for code, bars in market.groupby('ts_code',sort=True):
        ev=events[events.ts_code.eq(code)].to_dict('records')
        for bar in bars.itertuples():
            day=bar.session.date()
            cash=cash_per_current_share(ev,day,code in dividend_codes)
            computed.append(dict(session=bar.session,ts_code=code,close=bar.close,
                cash_per_current_share_365=cash,cash_yield365=cash/bar.close if cash is not None else np.nan,
                vendor_dv_ttm_pct=bar.dv_ttm,source_file=bar.source_file,
                source_sha256=bar.source_sha256,source_row=bar.source_row))
    panel=pd.DataFrame(computed)
    if panel.empty or panel.cash_yield365.dropna().lt(0).any():
        raise ValueError('Empty panel or negative dividend yield')
    # Independent event-by-event check on each bank's latest day plus historical anchor days.
    anchors=set(pd.to_datetime(['2016-01-04','2020-07-01','2025-12-31']))
    latest=panel.session.max()
    anchors.add(latest)
    for row in panel[panel.session.isin(anchors)].itertuples():
        ev=events[events.ts_code.eq(row.ts_code)].to_dict('records')
        known=[e for e in ev if e['ex_date'] is not None and e['ex_date']<=row.session.date()
               and e.get('imp_ann_date') is not None and e['imp_ann_date']<row.session.date()]
        amount=0.0;bad=False;components=[]
        for e in known:
            if not 0 <= (row.session.date()-e['ex_date']).days < 365: continue
            affected=[x for x in known if x['ex_date']>=e['ex_date']]
            if (e['cash_div_tax'] is None or not np.isfinite(e['cash_div_tax']) or e['cash_div_tax']<0
                or any(x['stk_div'] is None or not np.isfinite(x['stk_div']) or x['stk_div']<0 for x in affected)):
                bad=True;break
            scale=float(np.prod([1+x['stk_div'] for x in affected]))
            part=e['cash_div_tax']/scale;amount+=part
            components.append(dict(ex_date=str(e['ex_date']),cash_div_tax=e['cash_div_tax'],share_scale=scale,
                normalized_cash=part,source_file=e.get('source_file'),source_sha256=e.get('source_sha256')))
        expected=amount/row.close if not bad and row.ts_code in dividend_codes else np.nan
        actual=row.cash_yield365
        if not ((pd.isna(expected) and pd.isna(actual)) or np.isclose(expected,actual,rtol=1e-12,atol=1e-12)):
            raise ValueError('Independent cash-yield check failed')
        checks.append(dict(session=str(row.session.date()),code=row.ts_code,close=row.close,
            cash=amount if not bad else None,yield_ratio=actual if pd.notna(actual) else None,components=components))
    daily=panel.groupby('session').agg(mean_yield=('cash_yield365','mean'),median_yield=('cash_yield365','median'),
        valid_banks=('cash_yield365','count'),priced_banks=('ts_code','size'),cash_yield_sum=('cash_yield365','sum'),
        vendor_dv_ttm_mean_pct=('vendor_dv_ttm_pct','mean'))
    if not np.allclose(daily.mean_yield,daily.cash_yield_sum/daily.valid_banks,equal_nan=True,atol=1e-12):
        raise ValueError('Aggregation check failed')
    endpoint='https://push2his.eastmoney.com/api/qt/stock/kline/get'
    params=dict(secid='0.399986',fields1='f1,f2,f3,f4,f5,f6',fields2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
                klt='101',fqt='0',beg='20160101',end=latest.strftime('%Y%m%d'),lmt='5000')
    resp=requests.get(endpoint,params=params,headers={'User-Agent':'Mozilla/5.0'},timeout=30)
    resp.raise_for_status();payload=resp.json();data=payload.get('data') or {}
    if data.get('code')!='399986' or not data.get('klines'): raise ValueError('No correct index response')
    source=dict(endpoint=endpoint,parameters=params,retrieved_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                raw_response_sha256=hashlib.sha256(resp.content).hexdigest(),response=payload)
    (folder/'price_index_source.json').write_text(json.dumps(source,ensure_ascii=False),encoding='utf-8')
    raw=pd.DataFrame([s.split(',') for s in data['klines']])
    index=pd.DataFrame(dict(session=pd.to_datetime(raw[0]),price_index=pd.to_numeric(raw[2]),
                            high=pd.to_numeric(raw[3]),low=pd.to_numeric(raw[4]))).set_index('session')
    if index.index.duplicated().any() or not index.price_index.between(index.low,index.high).all():
        raise ValueError('Index key or OHLC failure')
    if not index.index.equals(daily.index): raise ValueError('Yield/index dates differ; no silent filling')
    combined=daily.join(index[['price_index']],validate='one_to_one')
    panel.to_parquet(folder/'individual_yield_inputs.parquet',index=False)
    events.to_parquet(folder/'implemented_dividend_events.parquet',index=False)
    combined.to_csv(folder/'银行平均股息率与价格指数.csv',encoding='utf-8-sig',index_label='session')
    (folder/'计算抽查.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    draw(combined,folder)
    audit=dict(chart_start=str(combined.index.min().date()),chart_end=str(latest.date()),trading_days=len(combined),
        bank_date_rows=len(panel),latest=combined.iloc[-1].to_dict(),min_valid_banks=int(combined.valid_banks.min()),
        max_valid_banks=int(combined.valid_banks.max()),source_database=str(db),source_provider='t.xiaodefa.top Tushare-compatible',
        price_and_dividends_same_database=True,actual_views=dict(aliases),member_database='alpha_research.duckdb sw_industry_membership',
        dividend_formula='mean_i(implemented_cash_per_current_share_in_last_365_calendar_days_i / same_day_raw_close_i)',
        stock_distribution_normalization='each cash amount divided by current and subsequent implemented 1+stk_div',
        independent_stock_day_checks=len(checks),index_source=endpoint,index_code='399986',index_basis='price index; no cash dividend reinvestment; fqt=0',
        authority_sources=['https://www.sse.com.cn/market/sseindex/calculation/',
        'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/399986factsheet.pdf'],
        coverage_policy='bank historical membership and archived current42-bank price subset; missing input excluded with counts; actual zero yield included',
        limitations=['Dividend and industry archives are current-source historical snapshots; first-disclosure versions not fully certified',
        'Cash scale follows existing bank-factor cash/stock distribution definition; rights/other non-free capital changes not fully modeled',
        'Equal-weight bank yield differs from the official index constituent weighting',
        'Yield includes price in denominator, so inverse relation is partly mechanical; does not prove forward timing ability'],
        authorization=authorization,
        files={p.name:digest(p) for p in folder.iterdir() if p.is_file()})
    (folder/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(folder=str(folder),start=audit['chart_start'],end=audit['chart_end'],
        days=len(combined),latest=audit['latest'],checks=len(checks)),ensure_ascii=False))

def recompute_cycle(source_folder):
    """Reuse archived prices/index; obtain bounded fiscal-period dividend warmup."""
    source_folder=source_folder.resolve()
    parent=json.loads((source_folder/'audit.json').read_bytes())
    for name in ('individual_yield_inputs.parquet','银行平均股息率与价格指数.csv'):
        if digest(source_folder/name)!=parent['files'][name]:
            raise ValueError('Archived price/chart source changed')
    run=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'-dividend-cycle-price'
    folder=ROOT/'reports/bank_sector_research'/run
    folder.mkdir(parents=True,exist_ok=False)
    auth={'owner_confirmed':True,'user_statement':'对 请修正 如果因子算错和结果了 也请修正 重新算',
        'inherited_chart_release':'2016-01-01 through 2026-10 month; description only',
        'purpose':'correct annual dividend-cycle yield, reusing exposed stock prices and index',
        'warmup_start':'2013-01-01','no2026_forward_labels':True}
    write=folder/'查看范围授权.json'
    write.write_text(json.dumps(auth,ensure_ascii=False,indent=2),encoding='utf-8')
    event=ExposureLedger(ROOT/'data/metadata/bank_chart_view_exposures.ndjson').append(
        actor='project-owner-authorized-assistant',vintage='bank-dividend-cycle:2016:2026-10',
        purpose='Owner-requested correction of exposed annual dividend-cycle chart; no2026 forward labels',
        capability_id='owner-authorization:sha256:'+digest(write))
    (folder/'本次查看登记.json').write_text(json.dumps({'exposure_event_id':event.event_id,'authorization_sha256':digest(write)},indent=2),encoding='utf-8')
    old=pd.read_parquet(source_folder/'individual_yield_inputs.parquet')
    old['session']=pd.to_datetime(old.session)
    end=old.session.max().date()
    with duckdb.connect(str(ROOT/'data/warehouse/bank_token.duckdb'),read_only=True) as c:
        ev=c.execute("SELECT * FROM bank_dividend_events WHERE try_cast(ex_date AS DATE) BETWEEN DATE '2013-01-01' AND ?",[end]).df()
        covered={r[0] for r in c.execute('SELECT DISTINCT ts_code FROM dividend').fetchall()}
    for col in ('ex_date','imp_ann_date','end_date','ann_date','record_date','pay_date'):
        ev[col]=pd.to_datetime(ev[col],errors='coerce').dt.date
        ev[col]=ev[col].astype(object).where(ev[col].notna(),None)
    ev=reconcile_dividend_events(ev)
    computed=[];checks=[]
    anchors={pd.Timestamp('2016-01-04'),pd.Timestamp('2025-12-31'),old.session.max()}
    for code,bars in old.groupby('ts_code',sort=True):
        events=ev[ev.ts_code.eq(code)].to_dict('records')
        for bar in bars.itertuples():
            cycle=dividend_cycle(events,bar.session.date(),code in covered)
            amount=cycle.cash
            value=amount/bar.close if amount is not None else np.nan
            computed.append({'session':bar.session,'ts_code':code,'close':bar.close,
                'cash_per_current_share_cycle':amount,'cash_yield_cycle':value,
                'cycle_status':cycle.status,'cycle_period_end':cycle.period_end,'cycle_event_count':cycle.event_count,
                'range_review_flag':bool(pd.notna(value) and (value < .025 or value > .06)),
                'vendor_dv_ttm_pct':bar.vendor_dv_ttm_pct,'source_file':bar.source_file,
                'source_sha256':bar.source_sha256,'source_row':bar.source_row})
            if bar.session in anchors:
                independent=0.
                for period,ex,cash,scale,part in cycle.components:
                    matching=[e for e in events if e['end_date']==period and e['ex_date']==ex]
                    assert len(matching)==1
                    current_scale=np.prod([1+e['stk_div'] for e in events if e['ex_date'] is not None
                        and ex<=e['ex_date']<=bar.session.date() and e['imp_ann_date'] is not None
                        and e['imp_ann_date']<bar.session.date()])
                    independent+=matching[0]['cash_div_tax']/current_scale
                if amount is not None and not np.isclose(amount,independent,atol=1e-12):
                    raise ValueError('Independent cycle amount arithmetic failed')
                checks.append({'session':str(bar.session.date()),'code':code,'status':cycle.status,
                    'cash':amount,'yield':value if pd.notna(value) else None,'components':[[str(p),str(ex),v,s,a] for p,ex,v,s,a in cycle.components]})
    panel=pd.DataFrame(computed)
    d=panel.groupby('session').agg(mean_yield=('cash_yield_cycle','mean'),median_yield=('cash_yield_cycle','median'),
        valid_banks=('cash_yield_cycle','count'),priced_banks=('ts_code','size'),cash_yield_sum=('cash_yield_cycle','sum'),
        vendor_dv_ttm_mean_pct=('vendor_dv_ttm_pct','mean'))
    prior=pd.read_csv(source_folder/'银行平均股息率与价格指数.csv',parse_dates=['session']).set_index('session')
    if not d.index.equals(prior.index):raise ValueError('Corrected chart calendar changed')
    d=d.join(prior[['price_index']],validate='one_to_one')
    if not np.allclose(d.mean_yield,d.cash_yield_sum/d.valid_banks,equal_nan=True):raise ValueError('Cycle mean check failed')
    panel.to_parquet(folder/'individual_yield_inputs.parquet',index=False)
    ev.to_parquet(folder/'implemented_dividend_events.parquet',index=False)
    d.to_csv(folder/'银行平均股息率与价格指数.csv',encoding='utf-8-sig',index_label='session')
    (folder/'计算抽查.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    comparison=d[['mean_yield','valid_banks']].join(prior[['mean_yield','valid_banks']].add_prefix('old365_'))
    comparison['difference_pp']=100*(comparison.mean_yield-comparison.old365_mean_yield)
    comparison.to_csv(folder/'周期口径与旧365日口径对比.csv',encoding='utf-8-sig',index_label='session')
    draw(d,folder,cycle=True)
    latest=panel[panel.session.eq(panel.session.max())]
    latest.to_csv(folder/'最新个股周期与异常检查.csv',encoding='utf-8-sig',index=False)
    audit={'yield_policy':'annual_dividend_cycle_2.0.0','chart_start':str(d.index.min().date()),'chart_end':str(end),
        'latest':d.iloc[-1].to_dict(),'parent_source':str(source_folder),'parent_audit_sha256':digest(source_folder/'audit.json'),
        'source_database':'bank_token.duckdb','index_reused_from_archived_chart':True,
        'index_basis':'399986 price index, no cash reinvestment','independent_stock_day_checks':len(checks),
        'latest_cycle_status_counts':latest.cycle_status.value_counts().to_dict(),
        'latest_missing':latest[latest.cash_yield_cycle.isna()][['ts_code','cycle_status']].to_dict('records'),
        'limits':['current vendor historical snapshot, not fully disclosure-version-certified',
            'equal-bank mean differs from official index weights','non-free capital changes need independent certification',
            'frequency transition explicitly retains last complete annual reference; not forecast'],
        'files':{p.name:digest(p) for p in folder.iterdir() if p.is_file()}}
    (folder/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'folder':str(folder),'latest':audit['latest'],'missing':audit['latest_missing']},ensure_ascii=False))


def draw(d,folder,cycle=False):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.font_manager import FontProperties
    plt.rcParams['font.family']=FontProperties(fname='C:/Windows/Fonts/msyh.ttc').get_name()
    plt.rcParams['axes.unicode_minus']=False
    fig,axes=plt.subplots(2,1,figsize=(15.5,8.4),sharex=True,gridspec_kw={'height_ratios':[4,1]})
    fig.patch.set_facecolor('#f7f8f4')
    fig.suptitle('银行平均股息率与银行价格指数',x=.065,y=.965,ha='left',fontsize=23,fontweight='bold')
    basis='覆盖一年的已实施分红周期股息率' if cycle else '近365天已实施税前现金股息率'
    fig.text(.065,.911,f'{d.index.min():%Y-%m-%d} — {d.index.max():%Y-%m-%d}    {basis}，个股等权平均',fontsize=12,color='#596960')
    ax=axes[0];right=ax.twinx()
    a=ax.plot(d.index,100*d.mean_yield,color='#126955',lw=1.5,label='平均已实施股息率（左轴）')
    b=right.plot(d.index,d.price_index,color='#bb7142',lw=1.2,alpha=.88,label='中证银行399986价格指数（右轴）')
    ax.set_ylabel('股息率（%）',color='#126955');right.set_ylabel('价格指数（点，不含股息）',color='#bb7142')
    ax.set_ylim(0,max(8,100*d.mean_yield.max()*1.15));ax.grid(axis='y',alpha=.15)
    ax.legend(a+b,[l.get_label() for l in a+b],loc='upper left',frameon=False,fontsize=11)
    last=d.iloc[-1]
    ax.text(.985,.94,f'末日平均股息率 {100*last.mean_yield:.2f}%\n银行价格指数 {last.price_index:,.2f} 点',transform=ax.transAxes,ha='right',va='top',fontsize=12,
            bbox={'facecolor':'white','edgecolor':'none','alpha':.8,'pad':8})
    axes[1].plot(d.index,d.valid_banks,color='#607568',lw=1,label='股息率有效银行数')
    axes[1].plot(d.index,d.priced_banks,color='#a0a69c',lw=1,ls='--',label='当日银行价格样本数')
    axes[1].set_ylabel('样本数');axes[1].set_ylim(0,48);axes[1].legend(loc='upper left',frameon=False,fontsize=10,ncol=2)
    axes[1].xaxis.set_major_locator(mdates.YearLocator());axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    axes[1].set_xlim(d.index.min(),d.index.max()+pd.Timedelta(days=60))
    fig.text(.065,.065,'来源：股价/分红来自 bank_token.duckdb；历史行业区间来自 alpha_research；指数行情来自东方财富。',fontsize=10,color='#526259')
    fig.text(.065,.038,'价格指数现金除息会自然回落。等权股息率与指数权重不同；价格下跌也会机械推高股息率，不能据此证明择时有效。',fontsize=10,color='#526259')
    fig.subplots_adjust(left=.065,right=.92,top=.865,bottom=.13,hspace=.09)
    fig.savefig(folder/'银行平均股息率_叠加银行价格指数.png',dpi=150,facecolor=fig.get_facecolor())
    fig.savefig(folder/'银行平均股息率_叠加银行价格指数.svg',facecolor=fig.get_facecolor())
    plt.close(fig)

def year_view(source_folder: Path, year: int):
    """Zoom an existing audited chart without querying additional business data."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.font_manager import FontProperties
    source_folder=source_folder.resolve()
    source=source_folder/'银行平均股息率与价格指数.csv'
    parent=json.loads((source_folder/'audit.json').read_bytes())
    if digest(source)!=parent['files'][source.name]:
        raise ValueError('Archived dividend chart data changed')
    d=pd.read_csv(source,parse_dates=['session']).set_index('session')
    d=d.loc[d.index.year==year].copy()
    if d.empty: raise ValueError('No observations in requested year')
    folder=ROOT/'reports/bank_sector_research'/(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+f'-dividend-price-{year}')
    folder.mkdir(parents=True,exist_ok=False)
    d.to_csv(folder/f'{year}年银行股息率与价格指数.csv',encoding='utf-8-sig',index_label='session')
    plt.rcParams['font.family']=FontProperties(fname='C:/Windows/Fonts/msyh.ttc').get_name()
    plt.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=(16,8.2))
    fig.patch.set_facecolor('#f7f8f4');right=ax.twinx()
    fig.suptitle(f'{year}年以来：银行平均股息率与价格指数',x=.07,y=.965,ha='left',fontsize=22,fontweight='bold')
    fig.text(.07,.91,f'{d.index.min():%Y-%m-%d} — {d.index.max():%Y-%m-%d}   日频数据；日期每半月标注',fontsize=12,color='#596960')
    a=ax.plot(d.index,100*d.mean_yield,color='#126955',lw=2,label='等权平均已实施股息率（左轴）')
    b=right.plot(d.index,d.price_index,color='#bb7142',lw=1.8,label='中证银行399986价格指数（右轴）')
    ax.set_ylabel('股息率（%）',color='#126955',fontsize=12)
    right.set_ylabel('银行价格指数（点，不含股息）',color='#bb7142',fontsize=12)
    ax.tick_params(axis='y',colors='#126955');right.tick_params(axis='y',colors='#bb7142')
    low,high=100*d.mean_yield.min(),100*d.mean_yield.max()
    ax.set_ylim(low-.15*(high-low),high+.3*(high-low))
    span=d.price_index.max()-d.price_index.min()
    right.set_ylim(d.price_index.min()-.15*span,d.price_index.max()+.3*span)
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonthday=[1,15]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    ax.xaxis.set_minor_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.tick_params(axis='x',labelrotation=45,labelsize=11)
    ax.grid(axis='y',alpha=.18);ax.grid(axis='x',which='major',alpha=.12)
    ax.set_xlim(pd.Timestamp(year,1,1),d.index.max()+pd.Timedelta(days=5))
    ax.legend(a+b,[line.get_label() for line in a+b],loc='upper left',frameon=False,fontsize=11)
    mark=pd.Timestamp(year,6,30)
    if mark in d.index:
        row=d.loc[mark];ax.axvline(mark,color='#737b73',lw=1,ls='--')
        ax.scatter([mark],[100*row.mean_yield],color='#126955',s=45,zorder=5)
        right.scatter([mark],[row.price_index],color='#bb7142',s=45,zorder=5)
        ax.text(mark+pd.Timedelta(days=4),ax.get_ylim()[1]-.07*(ax.get_ylim()[1]-ax.get_ylim()[0]),
                f'06-30\n平均股息率 {100*row.mean_yield:.2f}%\n价格指数 {row.price_index:,.2f}',
                fontsize=11,va='top',bbox={'facecolor':'white','edgecolor':'#d8ded4','alpha':.95,'pad':7})
    last=d.iloc[-1]
    ax.text(.985,.96,f'{d.index.max():%m-%d} 最新\n平均股息率 {100*last.mean_yield:.2f}%\n价格指数 {last.price_index:,.2f}',
            transform=ax.transAxes,ha='right',va='top',fontsize=11,
            bbox={'facecolor':'white','edgecolor':'#d8ded4','alpha':.95,'pad':7})
    basis='覆盖一年的已实施分红周期金额' if parent.get('yield_policy')=='annual_dividend_cycle_2.0.0' else '近365天已实施税前现金分红'
    fig.text(.07,.066,f'股息率 = {basis} / 当天股价，按当日有效银行等权平均；不是预测股息率。',fontsize=10,color='#526259')
    fig.text(.07,.039,'沿用上一张图的归档数据。双轴仅用于看走势，交叉点没有数值含义；价格下跌本身也会推高股息率。',fontsize=10,color='#526259')
    fig.subplots_adjust(left=.07,right=.92,top=.85,bottom=.19)
    target=folder/f'{year}年银行股息率_叠加价格指数.png'
    fig.savefig(target,dpi=160,facecolor=fig.get_facecolor())
    fig.savefig(target.with_suffix('.svg'),facecolor=fig.get_facecolor())
    plt.close(fig)
    audit=dict(source_folder=str(source_folder),source_csv_sha256=digest(source),source_audit_sha256=digest(source_folder/'audit.json'),
        first_day=str(d.index.min().date()),last_day=str(d.index.max().date()),sessions=len(d),
        date_tick_policy='01 and 15 of each month; weekly minor ticks',
        no_new_business_data_read=True,latest=last.to_dict(),
        june30=d.loc[mark].to_dict() if mark in d.index else None,
        files={p.name:digest(p) for p in folder.iterdir() if p.is_file()})
    (folder/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'folder':str(folder),'start':audit['first_day'],'end':audit['last_day'],'sessions':len(d)},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-chart',type=Path)
    parser.add_argument('--year',type=int,default=2026)
    parser.add_argument('--cycle-source',type=Path,default=ROOT/'reports/bank_sector_research/20261011-235644-dividend-price')
    parser.add_argument('--legacy365',action='store_true',help='Explicit reproduction of old365-day chart only')
    args=parser.parse_args()
    if args.source_chart:year_view(args.source_chart,args.year)
    elif args.legacy365:main()
    else:recompute_cycle(args.cycle_source)
