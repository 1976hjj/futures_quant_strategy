"""Chart-rich Chinese PDF generated exclusively from frozen research evidence."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from xml.sax.saxutils import escape
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts.research.research_bank_dividend_decade import evidence, runs, HORIZONS, PROFILES
from scripts.research.research_bank_dividend_rebound import sha
from scripts.research.research_bank_sector_timing import write_json,nonoverlap_positions

GREEN='#176c56';GOLD='#d79b32';BLUE='#447ba0';RED='#b94c44';GREY='#7a877e';PAPER='#f7f8f3'
H_LABELS=['60日','120日','半年\n126日','一年\n252日']


def pct(x,digits=2):
    return '尚无' if x is None or not np.isfinite(x) else f'{x*100:.{digits}f}%'


def find(rows,**keys):
    return next(r for r in rows if all(r[k]==v for k,v in keys.items()))


def build(base,local):
    spec,f,d=evidence(base)
    r=json.loads((base/'results.json').read_bytes());lr=json.loads((local/'results.json').read_bytes())
    ls=json.loads((local/'experiment_spec.json').read_bytes())
    if Path(ls['base'])!=base or sha(base/'features.parquet')!=ls['base_feature_sha256']:
        raise ValueError('Local/base evidence mismatch')
    lf=pd.read_parquet(local/'features.parquet')
    audits=[json.loads((p/'independent_audit.json').read_bytes()) for p in (base,local)]
    assert all(a['status']=='PASS' for a in audits)
    for name,digest in json.loads((base/'audit.json').read_bytes())['files'].items():
        assert sha(base/name)==digest,name
    for name,digest in audits[1]['files'].items():assert sha(local/name)==digest,name
    lab=pd.read_parquet(base/'labels.parquet')
    by_h={h:lab[lab.horizon.eq(h)].set_index('session').reindex(f.index) for h in HORIZONS}
    chart_dir=base/'charts';chart_dir.mkdir(exist_ok=True)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.font_manager import FontProperties
    from matplotlib.patches import Patch
    plt.rcParams.update({'font.family':FontProperties(fname='C:/Windows/Fonts/msyh.ttc').get_name(),
        'axes.unicode_minus':False,'font.size':11,'axes.spines.top':False,'axes.spines.right':False,
        'axes.labelcolor':'#364e43','text.color':'#243e32','axes.titlesize':14})
    chart_names=[]
    def save(fig,name):
        fig.patch.set_facecolor(PAPER)
        fig.savefig(chart_dir/(name+'.png'),dpi=185,facecolor=PAPER,bbox_inches='tight')
        plt.close(fig);chart_names.append(name)
    def format_dates(ax,short=False):
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonthday=1) if short else mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d' if short else '%Y'))
        ax.grid(axis='y',alpha=.18)
    def shade(ax,signal,color=GOLD,alpha=.23):
        for a,b in runs(signal):
            ax.axvspan(signal.index[a]-pd.Timedelta(hours=12),signal.index[b]+pd.Timedelta(hours=12),color=color,alpha=alpha,lw=0)
    def group(rule,h,name='high',sample='all'):
        return find(r['groups'],rule=rule,horizon=h,group=name,sample=sample)
    def local_group(rule,h,name='high'):
        return find(lr['groups'],rule=rule,horizon=h,group=name)

    # 1: full history with actual high-zone shading, never bridging false days.
    fig,axes=plt.subplots(2,1,figsize=(11.5,7),sharex=True,gridspec_kw={'height_ratios':[3,1]})
    ax=axes[0];right=ax.twinx()
    shade(ax,f.yield5)
    a=ax.plot(f.index,f.mean_yield*100,color=GREEN,lw=1.8,label='银行平均股息率')
    b=right.plot(d.index,d.price_index,color=BLUE,lw=1.3,alpha=.8,label='银行价格指数（右轴）')
    ax.axhline(5,color=GOLD,ls='--',lw=1.1);ax.set_ylabel('股息率（%）');right.set_ylabel('399986指数点位')
    ax.set_title('金色底纹 = 当天平均股息率至少5%');ax.set_ylim(1.8,6.0)
    ax.legend(a+b+[Patch(color=GOLD,alpha=.25)],[x.get_label() for x in a+b]+['至少5%的高息区'],loc='upper left',ncol=3,fontsize=10,frameon=False)
    axes[1].plot(f.index,f.yield_rank*100,color=GREEN,lw=1)
    shade(axes[1],f.high80,color=GREEN,alpha=.17);axes[1].axhline(80,color=GREY,ls='--',lw=.8)
    axes[1].set_ylabel('三年分位（%）');axes[1].set_ylim(0,105)
    axes[1].set_title('绿色底纹 = 高于此前三年80%的日期；早期历史不足留空',fontsize=11)
    format_dates(axes[1]);ax.grid(axis='y',alpha=.18);fig.tight_layout();save(fig,'01_full_history')

    # 2: monthly heat map; unfinished future months remain visibly empty.
    monthly=f.mean_yield.groupby([f.index.year,f.index.month]).mean().unstack().reindex(index=range(2016,2027),columns=range(1,13))*100
    fig,ax=plt.subplots(figsize=(11.5,5));cmap=plt.get_cmap('YlOrBr').copy();cmap.set_bad('#e7ebe4')
    im=ax.imshow(monthly.to_numpy(),vmin=2,vmax=5.5,cmap=cmap,aspect='auto')
    ax.set_xticks(range(12),[f'{m}月' for m in range(1,13)]);ax.set_yticks(range(11),[str(y) for y in range(2016,2027)])
    for i in range(11):
        for j in range(12):
            value=monthly.iloc[i,j]
            ax.text(j,i,'-' if pd.isna(value) else f'{value:.2f}',ha='center',va='center',fontsize=9,color='white' if pd.notna(value) and value>4.8 else '#3d4538')
    ax.set_title('每月平均股息率（%）：颜色越深，股息率越高')
    fig.colorbar(im,ax=ax,fraction=.025,pad=.02,label='股息率（%）');fig.tight_layout();save(fig,'02_monthly_heatmap')

    # 3: historical high zones enlarged for 2022-2024.
    fig,axes=plt.subplots(3,1,figsize=(11.5,7))
    for ax,year in zip(axes,(2022,2023,2024)):
        g=f.loc[str(year)];index=d.loc[g.index,'price_index'];right=ax.twinx();shade(ax,g.yield5)
        ax.plot(g.index,g.mean_yield*100,color=GREEN,lw=1.8);right.plot(g.index,index,color=BLUE,lw=1.2,alpha=.85)
        ax.axhline(5,color=GOLD,ls='--',lw=.9);ax.set_ylabel(f'{year}\n股息率（%）');right.set_ylabel('价格指数')
        ax.set_ylim(3.7,5.65);format_dates(ax,True)
    axes[0].set_title('高息主要发生在这三年：金色标出至少5%的真实日期')
    fig.tight_layout();save(fig,'03_high_zones_zoom')

    # 4: state holding comparisons, common horizon displays.
    fig,axes=plt.subplots(1,2,figsize=(11.5,4.5),sharey=True)
    for ax,rule,title in zip(axes,['yield5','high80'],['绝对高息：至少5%','长期高息：此前三年最高20%']):
        x=np.arange(4);high=[group(rule,h)['mean_return']*100 for h in HORIZONS];other=[group(rule,h,'other')['mean_return']*100 for h in HORIZONS]
        ax.bar(x-.17,high,width=.34,color=GREEN,label='高息日期');ax.bar(x+.17,other,width=.34,color='#9eb0a2',label='其他日期')
        for i,v in enumerate(high):ax.text(i-.17,v+.5,f'{v:.1f}',ha='center',fontsize=9)
        for i,v in enumerate(other):ax.text(i+.17,v+.5,f'{v:.1f}',ha='center',fontsize=9)
        ax.set_xticks(x,H_LABELS);ax.set_title(title);ax.axhline(0,color=GREY,lw=.7);ax.grid(axis='y',alpha=.15);ax.legend(frameon=False,fontsize=10)
    axes[0].set_ylabel('后续平均价格收益（%）');fig.tight_layout();save(fig,'04_holding_returns')

    # 5: local highs over a half year and a year.
    fig,axes=plt.subplots(1,2,figsize=(11.5,4.8),sharey=True)
    for ax,rule,title in zip(axes,['local120','local252'],['此前半年内最高20%','此前一年内最高20%']):
        high=[local_group(rule,h)['mean_return']*100 for h in HORIZONS];other=[local_group(rule,h,'other')['mean_return']*100 for h in HORIZONS]
        ax.plot(range(4),high,'o-',color=GREEN,lw=2,label='局部高息');ax.plot(range(4),other,'o--',color=GREY,label='其他日期')
        for i,v in enumerate(high):ax.annotate(f'{v:.2f}%',(i,v),xytext=(0,9),textcoords='offset points',ha='center',fontsize=10)
        ax.set_xticks(range(4),H_LABELS);ax.axhline(0,color=GREY,lw=.7);ax.set_title(title);ax.grid(axis='y',alpha=.16);ax.legend(frameon=False)
    axes[0].set_ylabel('后续平均价格收益（%）');fig.tight_layout();save(fig,'05_local_high_returns')

    # 6: yield buckets with observations shown in every cell.
    fig,ax=plt.subplots(figsize=(11.5,4.2));arr=np.full((4,4),np.nan)
    for i,a in enumerate([0,.04,.05,.06]):
        for j,h in enumerate(HORIZONS):
            v=find(r['buckets'],lower=a,horizon=h);arr[i,j]=v.get('mean_return',np.nan)*100 if v['days'] else np.nan
    cmap=plt.get_cmap('YlGn').copy();cmap.set_bad('#e7ebe4');im=ax.imshow(arr,cmap=cmap,vmin=0,vmax=20,aspect='auto')
    for i,a in enumerate([0,.04,.05,.06]):
        for j,h in enumerate(HORIZONS):
            v=find(r['buckets'],lower=a,horizon=h)
            text='无样本' if not v['days'] else f"{pct(v['mean_return'])}\n{v['days']}个日期"
            ax.text(j,i,text,ha='center',va='center',fontsize=11,color='white' if np.isfinite(arr[i,j]) and arr[i,j]>13 else '#243e32')
    ax.set_xticks(range(4),H_LABELS);ax.set_yticks(range(4),['低于4%','4%至不足5%','5%至不足6%','至少6%'])
    ax.set_title('股息率分档：5%-6%区间的价格表现更突出')
    fig.colorbar(im,ax=ax,fraction=.03,pad=.025,label='后续平均价格收益（%）');fig.tight_layout();save(fig,'06_yield_buckets')

    # 7: first entry differs from daily state holding returns.
    fig,axes=plt.subplots(1,2,figsize=(11.5,4.2),sharey=True)
    for ax,rule,title in zip(axes,['yield5','high80'],['至少5%：入区事件与持有日期','三年最高20%：入区事件与持有日期']):
        state=[group(rule,h)['mean_return']*100 for h in HORIZONS]
        entry=[find(r['entry_summary'],rule=rule,horizon=h)['mean_return']*100 for h in HORIZONS]
        ax.plot(range(4),state,'o-',color=GREEN,label='所有高息日期');ax.plot(range(4),entry,'s-',color=RED,label='刚进入高息区')
        ax.axhline(0,color=GREY,lw=.7);ax.set_xticks(range(4),H_LABELS);ax.grid(axis='y',alpha=.16);ax.set_title(title,fontsize=12);ax.legend(frameon=False,fontsize=10)
    axes[0].set_ylabel('平均价格收益（%）');fig.tight_layout();save(fig,'07_state_vs_entry')

    # 8: local entry forward paths. Every fixed-rule complete event is shown.
    local_events=[]
    fig,axes=plt.subplots(1,2,figsize=(11.5,4.7),sharey=True)
    for ax,rule,title in zip(axes,['local120','local252'],['半年局部高息：进入后价格路径','一年局部高息：进入后价格路径']):
        anchors=nonoverlap_positions((lf[rule].eq(1)&lf[rule].shift(1).eq(0)).to_numpy(),60)
        paths=[]
        for i in anchors:
            for h in HORIZONS:
                local_events.append(dict(rule=rule,signal=f.index[i],yield_value=f.mean_yield.iloc[i],**by_h[h].iloc[i].to_dict()))
            if i+253<len(d):
                path=(d.price_index.iloc[i+1:i+254].to_numpy()/d.price_index.iloc[i+1]-1)*100
                paths.append(path);ax.plot(range(253),path,color=GREY,lw=.8,alpha=.3)
        ax.plot(range(253),np.mean(paths,axis=0),color=GREEN,lw=2.5,label=f'平均路径（{len(paths)}次）')
        ax.axhline(0,color=GREY,lw=.7);ax.set_title(title,fontsize=12);ax.set_xlabel('入区后交易日');ax.grid(alpha=.15);ax.legend(frameon=False,fontsize=10)
    axes[0].set_ylabel('相对T+1价格变化（%）');fig.tight_layout();save(fig,'08_local_event_paths')
    le=pd.DataFrame(local_events);le.to_csv(base/'local_entry_diagnostics.csv',index=False,encoding='utf-8-sig')

    # 9: all allocation profiles, common signal and common terminal date.
    fig,axes=plt.subplots(1,2,figsize=(11.5,4.5))
    for ax,metric,title in zip(axes,['mean_return','mean_drawdown'],['一年后资金收益','期间平均最大回撤']):
        for j,(profile,color) in enumerate(zip(PROFILES,[GREEN,GOLD,BLUE])):
            values=[find(r['allocation_summary'],rule=rule,horizon=252,profile=profile)[metric]*100 for rule in ['yield5','high80']]
            ax.bar(np.arange(2)+(j-1)*.24,values,width=.23,color=color,label=PROFILES[profile])
        ax.set_xticks([0,1],['至少5%：4次','三年高息：10次']);ax.axhline(0,color=GREY,lw=.7);ax.set_title(title);ax.set_ylabel('%');ax.grid(axis='y',alpha=.15)
        ax.legend(frameon=False,fontsize=9)
    fig.tight_layout();save(fig,'09_allocation')

    # 10: broad dividend stability and true incremental trend comparison.
    fig,axes=plt.subplots(1,2,figsize=(11.5,4.5),sharey=True)
    for ax,rule,title,labels_ in zip(axes,['within_high80_stable','within_high80_trend'],
        ['同在高息区：分红广度稳定更好','同在高息区：趋势改善未显示增益'],[('稳定','不稳定'),('趋势改善','未改善')]):
        hi=[group(rule,h)['mean_return']*100 for h in HORIZONS];lo=[group(rule,h,'other')['mean_return']*100 for h in HORIZONS]
        ax.plot(range(4),hi,'o-',color=GREEN,label=labels_[0]);ax.plot(range(4),lo,'o-',color=RED,label=labels_[1]);ax.axhline(0,color=GREY,lw=.7)
        ax.set_xticks(range(4),H_LABELS);ax.set_title(title,fontsize=12);ax.grid(axis='y',alpha=.15);ax.legend(frameon=False)
    axes[0].set_ylabel('后续平均价格收益（%）');fig.tight_layout();save(fig,'10_extra_conditions')

    # 11: cash-vs-price identity, not an assertion that high yields forecast.
    fig,axes=plt.subplots(2,1,figsize=(11.5,6.2),sharex=True)
    axes[0].plot(f.index,f.price_contribution20*100,color=BLUE,label='过去20日：股价变化贡献')
    axes[0].plot(f.index,f.cash_contribution20*100,color=GOLD,label='过去20日：分红参考金额贡献')
    axes[0].axhline(0,color=GREY,lw=.7);axes[0].set_ylabel('股息率变化（百分点）');axes[0].legend(frameon=False,ncol=2,fontsize=10)
    axes[0].set_title('股息率变高：可能是价格下跌，也可能是分红参考金额上升')
    axes[1].plot(f.index,f.declining_cash_fraction*100,color=RED,lw=1.3)
    axes[1].axhline(25,color=GREY,ls='--',lw=.9);axes[1].set_ylabel('分红参考下降的银行（%）')
    axes[1].set_title('同比对比同一批银行；虚线25%为本轮既定稳定界线',fontsize=11)
    format_dates(axes[1]);axes[0].grid(axis='y',alpha=.15);fig.tight_layout();save(fig,'11_price_cash_decomposition')

    # 12:2026, short dates, explicit local high shading.
    g=lf.loc['2026'];fig,axes=plt.subplots(2,1,figsize=(11.5,6.8),sharex=True,gridspec_kw={'height_ratios':[3,1]})
    ax=axes[0];right=ax.twinx();shade(ax,g.local120,GOLD,.22)
    aa=ax.plot(g.index,g.mean_yield*100,color=GREEN,lw=2,label='平均股息率');bb=right.plot(g.index,d.loc[g.index,'price_index'],color=BLUE,lw=1.6,label='价格指数（右轴）')
    ax.set_ylabel('股息率（%）');right.set_ylabel('指数点位');ax.set_ylim(4.05,5.1)
    ax.legend(aa+bb+[Patch(color=GOLD,alpha=.25)],[x.get_label() for x in aa+bb]+['此前半年最高20%'],loc='upper left',fontsize=9,ncol=3,frameon=False)
    for day,label in [('2026-06-30','今年局部高点'),(spec['end'],'最新')]:
        x=pd.Timestamp(day);v=g.mean_yield.loc[x]*100;ax.scatter([x],[v],color=GREEN,s=30,zorder=6)
        ax.annotate(f'{x:%m-%d} {label}\n股息率{v:.2f}%',(x,v),xytext=(-12,35 if day.endswith('06-30') else 20),textcoords='offset points',ha='right',fontsize=10,
            arrowprops=dict(arrowstyle='-',color=GREY),bbox=dict(boxstyle='round,pad=.4',fc='white',ec='#ccd8cc'))
    ax.set_title('2026年的机会更适合用“局部高息”观察，不能只等5%')
    axes[1].plot(g.index,g.local120_rank*100,color=GOLD,label='此前半年分位')
    axes[1].plot(g.index,g.yield_rank*100,color=GREEN,label='此前三年分位');axes[1].axhline(80,color=GREY,ls='--',lw=.7)
    axes[1].set_ylabel('分位（%）');axes[1].set_ylim(0,105);axes[1].legend(frameon=False,fontsize=9,ncol=2)
    axes[1].xaxis.set_major_locator(mdates.MonthLocator(bymonthday=[1,15]));axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    axes[1].tick_params(axis='x',rotation=45,labelsize=8);axes[1].grid(axis='y',alpha=.15);ax.grid(axis='y',alpha=.15)
    fig.tight_layout();save(fig,'12_2026_local_high')

    # 13: annual robustness of local half-year label, not hidden behind mean.
    years=list(range(2016,2027));fig,ax=plt.subplots(figsize=(11.5,4.7));matrix=np.full((4,11),np.nan)
    for i,h in enumerate(HORIZONS):
        for j,year in enumerate(years):
            row=find(lr['annual'],rule='local120',horizon=h,year=year)
            if row['high']['days'] and row['other']['days']:
                matrix[i,j]=(row['high']['mean_return']-row['other']['mean_return'])*100
    cmap=plt.get_cmap('RdYlGn').copy();cmap.set_bad('#e7ebe4');im=ax.imshow(matrix,cmap=cmap,vmin=-15,vmax=15,aspect='auto')
    for i in range(4):
        for j in range(11):ax.text(j,i,'-' if not np.isfinite(matrix[i,j]) else f'{matrix[i,j]:+.1f}',ha='center',va='center',fontsize=9)
    ax.set_xticks(range(11),years,rotation=45);ax.set_yticks(range(4),H_LABELS)
    ax.set_title('半年局部高息相对其他日期的收益差：并非每年都占优')
    fig.colorbar(im,ax=ax,fraction=.025,pad=.025,label='均值差（百分点）');fig.tight_layout();save(fig,'13_yearly_consistency')

    # PDF fixed-page structure, source evidence determines every number.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak,Image
    from pypdf import PdfReader
    pdfmetrics.registerFont(TTFont('BankCN','C:/Windows/Fonts/msyh.ttc',subfontIndex=0))
    body=ParagraphStyle('body',fontName='BankCN',fontSize=10,leading=16,textColor=colors.HexColor('#243e32'),spaceAfter=8,wordWrap='CJK')
    title=ParagraphStyle('title',parent=body,fontSize=22,leading=30,spaceAfter=15)
    head=ParagraphStyle('head',parent=body,fontSize=17,leading=25,spaceAfter=12)
    small=ParagraphStyle('small',parent=body,fontSize=8,leading=12,spaceAfter=6)
    call=ParagraphStyle('call',parent=body,fontSize=12,leading=19,spaceBefore=4,spaceAfter=10,textColor=colors.HexColor(GREEN))
    story=[];width=174*mm
    def para(text,style=body):story.append(Paragraph(escape(text),style))
    def page(name):story.append(PageBreak());para(name,head)
    def chart(name,height):
        from PIL import Image as PILImage
        path=chart_dir/(name+'.png')
        with PILImage.open(path) as im:w,h=im.size
        actual_h=min(height*mm,width*h/w)
        story.append(Image(str(path),width=actual_h*w/h,height=actual_h));story.append(Spacer(1,7))
    def table(headers,rows,widths=None):
        data=[[Paragraph(escape(str(x)),small) for x in headers]]+[[Paragraph(escape(str(x)),small) for x in row] for row in rows]
        t=Table(data,colWidths=[x*mm for x in widths] if widths else [width/len(headers)]*len(headers),repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#dfebe0')),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor(PAPER)]),('VALIGN',(0,0),(-1,-1),'TOP'),
            ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
            ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
            ('LINEBELOW',(0,0),(-1,0),.7,colors.HexColor('#91a28e'))]))
        story.extend([t,Spacer(1,8)])
    def stat(rule,h,name='high'):return group(rule,h,name)

    para('银行板块股息率与入场机会',title)
    para('2016年至2026年10月 · 近十年研究更新',call)
    para(f"实际数据：{spec['start']} 至 {spec['end']}。主图保留2016整年，覆盖约10年9个月；另外检查2016-10-10起的严格近十年范围。")
    para('明确结论：高息区间有研究价值，但高息不是自动买点。',call)
    para('你的观察有数据线索。固定5%以上的高息日期，后续半年和一年价格表现较好；半年或一年内的局部高息也有较好的平均表现。不过，刚进入高息区时可能继续跌，不能把整段高息时期的平均表现当作第一天买入的结果。')
    table(['要点','本轮结果'],[
        ['绝对高息','至少5%的日期共180天，全部在2022-2024年；没有新增2026高息事件。'],
        ['半年/一年局部高息',f"一年内最高20%的日期：后续半年均值{pct(local_group('local252',126)['mean_return'])}，其他日期{pct(local_group('local252',126,'other')['mean_return'])}。"],
        ['入场不能照搬持有均值','至少5%的4次入区事件：半年平均4.87%，一年16.53%；样本少，年度收益还会重叠。'],
        ['最新位置',f"{r['latest']['session']}平均股息率{pct(r['latest']['yield_mean'])}；不在半年、一年或三年最高20%区间。"],
        ['下一步方向','优先研究局部高息、分红可靠性和分批进入的组合；仓位比例暂不定死。']], [31,143])
    para('报告中的“收益”均指银行价格指数变化，不含分红、税费或交易成本；没有称为可交易策略。分红金额采用已实施一年周期，全年一次分红计一次，半年加年终覆盖一年，不再硬套365天。',small)
    para('阅读方式：先找高息区间，再看各持有期限；随后看入区事件与回撤，最后看今年位置和下一步研究。',small)

    page('01 / 高息区间到底在哪里')
    chart('01_full_history',135)
    para('固定5%的高息区主要在2022年秋、2023年夏至2024年初，以及2024年部分夏秋日期。三年相对高位从2020年就很多：它可能只表示相对过去更便宜，并不表示已经到绝对低价。')
    para('金色、绿色底纹都按当天信号画，没有把中间不满足条件的日期连成整段。上下图的高位定义不同；早期三年分位没有足够历史时留空。',small)
    para('这张图只能展示关系。股价跌会让股息率变高，这部分是公式本身带来的，是否值得买还要看后续表现。',call)

    page('02 / 每年股息率多高：不要只盯两个例子')
    chart('02_monthly_heatmap',88)
    table(['年份','平均','最低至最高','至少5%天数','有效银行数'],[
        [x['year'],pct(x['mean']),f"{pct(x['minimum'])} - {pct(x['maximum'])}",x['yield5_days'],f"{x['minimum_banks']} - {x['maximum_banks']}"] for x in r['annual']], [19,28,61,33,33])
    para('早年按当时有价格且股息可核实的银行计算，不能把今天42家套回所有历史。未知分红不算0；低于3%或高于6%也不凭范围裁掉。银行样本变化会影响板块均值，所以不是固定42家完全可比。2026年10月只含截至10-09的交易日。',small)

    page('03 / 把2022-2024高息区放大')
    chart('03_high_zones_zoom',132)
    table(['连续高息段举例','交易日数','这段股息率范围'],[
        ['2022-10-19 至 2022-11-22',25,'5.02% - 5.30%'],
        ['2023-10-19 至 2024-02-08',80,'5.01% - 5.44%'],
        ['2024-08-30 至 2024-09-23',15,'5.03% - 5.31%']], [89,30,55])
    para('这些是连续区间示例，不是只挑赢的交易。完整记录保留所有17段，包括只持续1天的高位。每段的结束只有事后才知道，不能拿结束日反推买卖。双轴线的交叉点没有数值含义。',small)

    page('04 / 高息之后：60日、120日、半年、一年')
    chart('04_holding_returns',104)
    table(['持有期限','至少5%日期','其他日期','高息上涨比例','高息平均最大回撤'],[
        [label,pct(stat('yield5',h)['mean_return']),pct(stat('yield5',h,'other')['mean_return']),pct(stat('yield5',h)['win_rate']),pct(stat('yield5',h)['mean_drawdown'])]
        for h,label in zip(HORIZONS,['60日','120日','半年126日','一年252日'])], [30,34,33,35,42])
    para('固定5%的表现很值得保留，但180个高息日期只是同几段行情中的连续日子，不是180次独立机会。按收益窗口去掉重叠，一年只剩2个高息窗口。')
    para('三年相对高位没那么强：一年均值3.05%，其他日期3.98%。这说明“比以前便宜”不够，价格还可能长期低迷。120日与126日接近，也不能算两份独立证据。',small)

    page('05 / 更贴近你的想法：近期局部高息')
    chart('05_local_high_returns',107)
    table(['持有期限','半年局部高息 / 其他','一年局部高息 / 其他'],[
        [label,f"{pct(local_group('local120',h)['mean_return'])} / {pct(local_group('local120',h,'other')['mean_return'])}",
         f"{pct(local_group('local252',h)['mean_return'])} / {pct(local_group('local252',h,'other')['mean_return'])}"]
        for h,label in zip(HORIZONS,['60日','120日','半年126日','一年252日'])],[32,71,71])
    para('“局部高息”就是当天股息率高于此前120日或252日中80%的日期；不含当天参与历史排序。没有为了匹配6月底而微调窗口或门槛。')
    para('这个方向比只等5%更能识别2026年的局部变化，值得继续研究。但这是看过历史后的新增探索；本轮80个检验加上以前420个，共500个统一检查，不能只报最好的一项。',small)

    page('06 / 5%-6%确实突出，但不要把档位当铁律')
    chart('06_yield_buckets',99)
    table(['半年口径','日期数','平均价格收益','去重后的窗口数'],[
        [name,find(r['buckets'],lower=a,horizon=126)['days'],pct(find(r['buckets'],lower=a,horizon=126).get('mean_return')),
         find(r['buckets'],lower=a,horizon=126)['nonoverlap_n']] for a,name in [(0,'低于4%'),(.04,'4%至不足5%'),(.05,'5%至不足6%'),(.06,'至少6%')]], [48,32,52,42])
    para('这里“至少5%”实际全是5%-6%：板块平均没有达到6%的日期。这不等于个股6%一定算错，也不等于早年2%-3%一定有问题；先核实金额、价格和样本，不能把数据修成预想区间。')
    para('2022-2024的高息样本后来遇到了行情修复。固定档位的好结果可能同时包含估值便宜、分红可靠和年份行情，下一步应拆开研究，而不是直接规定5%满仓。',small)

    page('07 / 最容易误会：在区间内持有，不等于入区就买')
    chart('07_state_vs_entry',88)
    e=pd.read_csv(base/'entry_episodes.csv',parse_dates=['signal'])
    rows=[]
    for rule in ['yield5','high80']:
        for day in sorted(e[e.rule.eq(rule)].signal.unique()):
            g=e[e.rule.eq(rule)&e.signal.eq(day)]
            rows.append(['至少5%' if rule=='yield5' else '三年高息',f'{pd.Timestamp(day):%Y-%m-%d}',
                pct(g[g.horizon.eq(126)]['return'].iloc[0]),pct(g[g.horizon.eq(252)]['return'].iloc[0]),pct(g[g.horizon.eq(252)].drawdown.iloc[0])])
    table(['条件','真实入区日','半年收益','一年收益','一年内最大回撤'],rows,[28,43,31,31,41])
    para('按前一天明确不高、当天变高选入区；固定至少间隔61个交易日。同一组信号先确定，再观察结果。不是独立年度机会，表中一年收益可能重叠。三年高息10次入区，一年平均-3.85%；直接买首日的风险比持有均值提示得大。',small)

    page('08 / 局部高息入场：有机会，也常要先承受下跌')
    chart('08_local_event_paths',104)
    rows=[]
    for rule,name in [('local120','半年局部高息'),('local252','一年局部高息')]:
        for h,label in [(60,'60日'),(126,'半年'),(252,'一年')]:
            v=le[le.rule.eq(rule)&le.horizon.eq(h)&le.status.eq('READY')]
            rows.append([name,label,len(v),pct(v['return'].mean()),pct(v['return'].gt(0).mean()),pct(v.drawdown.mean())])
    table(['入区条件','期限','完整次数','平均收益','上涨比例','平均最大回撤'],rows,[37,22,26,29,29,31])
    para('灰线是每次完整一年路径，绿线是平均，不会删掉亏损事件。一年局部高息入区的14次完整事件，一年均值约6.70%，上涨约57%；它不是稳赚，也不如连续高息日期的均值那么强。')
    para('本页是同一已看历史的事后诊断；仍按固定入区与61日间隔展示，不能当成独立验证。2026最近事件未满一年，全部留在记录里，未计为一年失败。',small)

    page('09 / 仓位如何进入：分批是控风险工具')
    chart('09_allocation',105)
    table(['入区条件','方式','一年资金收益','平均最大回撤','平均已投入比例'],[
        ['至少5%' if rule=='yield5' else '三年高息',name,
        pct(find(r['allocation_summary'],rule=rule,horizon=252,profile=profile)['mean_return']),
        pct(find(r['allocation_summary'],rule=rule,horizon=252,profile=profile)['mean_drawdown']),
        pct(find(r['allocation_summary'],rule=rule,horizon=252,profile=profile)['mean_exposure'])]
        for rule in ['yield5','high80'] for profile,name in PROFILES.items()], [31,34,35,38,36])
    para('一次投入在T+1收盘；三次投入分别在T+1、T+21、T+41，各用1/3；等待确认最多20日，出现过去20日上涨且价格在60日均线上，下一日收盘才投入。三种方式同一信号、同一退出日。',small)
    para('分批在这些案例里回撤较小，值得作为仓位研究候选。等待确认至少5%的4次事件只成交2次，另外2次按现金收益0计入；较小回撤也来自仓位更低，不代表判断更准。',small)

    page('10 / 配什么条件：分红可靠性比简单趋势更有线索')
    chart('10_extra_conditions',103)
    table(['同在三年高息区','半年收益','一年收益','完整日期数'],[
        ['分红广度稳定',pct(group('within_high80_stable',126)['mean_return']),pct(group('within_high80_stable',252)['mean_return']),739],
        ['分红广度不稳定',pct(group('within_high80_stable',126,'other')['mean_return']),pct(group('within_high80_stable',252,'other')['mean_return']),162],
        ['趋势改善',pct(group('within_high80_trend',126)['mean_return']),pct(group('within_high80_trend',252)['mean_return']),250],
        ['趋势未改善',pct(group('within_high80_trend',126,'other')['mean_return']),pct(group('within_high80_trend',252,'other')['mean_return']),651]], [55,39,39,41])
    para('稳定指对比同一批银行一年以前的分红参考金额，下降银行占比不超过25%。价格下跌不会直接把这个指标判成分红下降；但它是已实施金额的参考，不是未来分红保证。')
    para('稳定组表现更好，是可继续探索的线索；不稳定组162个日期全在2021-2022，仍可能混入年份差异。简单的趋势确认没有显示稳定增益，不能仅凭直觉加进策略。',small)

    page('11 / 股息率变化，究竟来自价格还是分红')
    chart('11_price_cash_decomposition',124)
    para('股息率 = 一年分红参考金额 / 当天股价。价格跌，股息率可以变高；分红参考减少，股息率可以变低。因此“股息率开始下降”不能直接解释为行情已反弹。')
    para('上图用过去20个交易日相同银行拆开价格与分红参考的影响，两项合起来就是股息率变化。现金金额与旧价格都调整到相同股本基础，避免送股前后单位不一致。')
    para('下图统计对比一年前参考金额下降的银行比例。这里的变化也可能来自已实施周期更新或分红频率过渡；没有逐家核实未来完整分红前，不直接称为“宣布减分红”。',small)

    page('12 / 2026年：6月底是局部高息，今天已经降下来了')
    chart('12_2026_local_high',136)
    june=lf.loc['2026-06-30'];latest=r['latest']
    table(['日期','平均股息率','半年分位','一年分位','三年分位'],[
        ['2026-06-30',pct(june.mean_yield),pct(june.local120_rank),pct(june.local252_rank),pct(june.yield_rank)],
        [spec['end'],pct(latest['yield_mean']),pct(lf.local120_rank.iloc[-1]),pct(lf.local252_rank.iloc[-1]),pct(latest['rank'])]],[37,35,34,34,34])
    para('6月底高于此前半年和一年所有日期，但只高于此前三年约67%的日期：你的“近期很高”的感觉和长期统计可以同时成立。最新平均约4.29%，41/42家有可核实周期；北京银行缺预期中期分红证据，没有拿0补。',small)
    para('今年部分信号可观察60日，半年、一年常未满窗口。本报告只计算走完的期限；6月底不作为唯一研究对象，也不据它重设入场线。',small)

    page('13 / 可靠性：有统计线索，仍不能跳到必胜')
    chart('13_yearly_consistency',89)
    all_tests=lr['base_tests_with_expanded_correction']+lr['tests']
    selected=[('yield5',126,'raw','至少5% / 半年 / 原始'),('yield5',252,'controlled','至少5% / 一年 / 加控制'),
        ('high80',126,'controlled','三年高息 / 半年 / 加控制'),('local120',126,'raw','半年局部高息 / 半年 / 原始'),
        ('local120',126,'controlled','半年局部高息 / 半年 / 加控制'),('local252',126,'raw','一年局部高息 / 半年 / 原始'),
        ('local252',126,'controlled','一年局部高息 / 半年 / 加控制')]
    table(['比较','收益差（百分点）','500项联合校正p'],[
        [name,f"{find(all_tests,rule=rule,horizon=h,model=model)['coefficient']*100:+.2f}",
         f"{find(all_tests,rule=rule,horizon=h,model=model)['holm_joint']:.3g}"] for rule,h,model,name in selected], [95,42,37])
    para('收益差的“原始”是两组均值相减；“加控制”是在比较时考虑年份、月份、此前涨跌和波动。p值越小，越难用该模型下的随机波动解释，但不等于成功率，也不是因果证明。')
    para('一些半年关系通过了多项校正，是积极线索；结果明显依赖模型。扩大到2026主要新增了非高息比较日期，5%以上的180天没有变化，不能说新增了一批高息机会来验证它。',small)

    page('14 / 下一步：研究组合，先不拍脑袋定仓位')
    para('我的判断：方向保留，下一步优先做“局部高息 + 分红可靠 + 分批投入”。',call)
    para('第一，把过去半年、一年的局部高息作为候选观察信号，固定5%作为更极端的价格补偿参考。两者分别检查，避免利率或银行样本变了以后，死守一个不再合适的绝对门槛。')
    para('第二，分红参考下降时先核实：是分红金额减少、周期更新、一次分红改两次，还是证据缺失。只有可核实的现金流变化才用于仓位判断。')
    para('第三，用同一信号日期比较一次与分批投入；加上含分红收益、税费、成交成本和可买标的之后，再讨论仓位上限、加减仓与退出规则。当前报告仍是价格研究，不能回答实际持有总收益。')
    para('第四，冻结候选条件，等未使用过的后续数据或分段留出验证。2026这段已经查看，不能再叫作未见样本。未来若半年/一年规则失效，也保留结果，不只保留有效年份。')
    table(['已核查','核查结果'],[
        ['个股算术与板块聚合',f"重算{audits[0]['individual_yield_checks']:,}个股日期；{audits[0]['aggregate_dates']:,}个交易日均值一致。"],
        ['后续收益与回撤',f"{audits[0]['label_checks']:,}条标签逐条验证；T+1进入，尾部不足留缺失。"],
        ['分组与入场路径',f"192个基础分组；168条仓位路径；入区日期与固定间隔通过检查。"],
        ['统计与时间排序',f"80个有结果模型独立复核HAC标准误；所有分位只看过去。"],
        ['来源与局限','原始价格/分红：bank_token.duckdb供应商历史快照；历史行业：alpha_research；价格指数：东方财富归档。首次披露版本、退出样本和非免费股本变化未完整认证。']], [42,132])
    para('指数说明：399986是银行价格指数，现金分红没有作为收益再投入；官方另列全收益指数H20180。因此它应理解为“不含分红的指数价格走势”，不是个股前复权曲线。等权平均股息率与指数权重也不同。',small)
    para('官方定义核验：中证银行指数事实表（2026-09-30版本），官网PDF第二页另列全收益指数。https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/399986factsheet.pdf',small)
    para(f'证据版本：{base.name}；局部高息探索：{local.name}。旧报告的可读文件会在新PDF检查通过后删除，原文归档保留以便核查。',small)

    output=ROOT/'output/pdf'/'银行板块股息率与入场研究_2016至2026年10月.pdf'
    output.parent.mkdir(exist_ok=True)
    if output.exists():raise ValueError('Existing report must be archived before replacing')
    def footer(canvas,doc):
        canvas.saveState();canvas.setStrokeColor(colors.HexColor('#dce3d8'));canvas.line(18*mm,16*mm,192*mm,16*mm)
        canvas.setFont('BankCN',8);canvas.setFillColor(colors.HexColor('#718271'))
        canvas.drawString(18*mm,11*mm,'银行板块研究 · 数据截至2026-10-09 · 价格收益，不含分红')
        canvas.drawRightString(192*mm,11*mm,str(doc.page));canvas.restoreState()
    doc=SimpleDocTemplate(str(output),pagesize=A4,leftMargin=18*mm,rightMargin=18*mm,topMargin=17*mm,bottomMargin=22*mm,
        title='银行板块股息率与入场研究：2016至2026年10月',author='Factor Evidence Workbench')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    reader=PdfReader(output)
    texts=[p.extract_text() for p in reader.pages]
    assert len(reader.pages)==15, f'Expected15 pages, actual{len(reader.pages)}; inspect layout'
    assert all(t.strip() for t in texts)
    write_json(base/'pdf_manifest.json',dict(path=str(output),sha256=sha(output),pages=len(reader.pages),chart_count=len(chart_names),
        report_script_sha256=sha(__file__),source_evidence_sha256=sha(base/'results.json'),local_evidence_sha256=sha(local/'results.json'),
        charts={p.name:sha(p) for p in chart_dir.iterdir() if p.is_file()},
        text_extraction_checked=True,visual_review_pending=True))
    print(json.dumps(dict(path=str(output),pages=len(reader.pages),charts=len(chart_names))))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('base',type=Path);p.add_argument('local',type=Path)
    a=p.parse_args();build(a.base.resolve(),a.local.resolve())
