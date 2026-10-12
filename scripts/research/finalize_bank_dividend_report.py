"""Independent checks, final PDF audit appendix and report-text migration.

Archive-only command never deletes files. The explicit migration manifest is
reviewable before the user-authorized literal-path removal step.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import zipfile

import duckdb
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def verify(folder):
    spec=json.loads((folder/'experiment_spec.json').read_bytes());r=json.loads((folder/'results.json').read_bytes())
    audit=json.loads((folder/'audit.json').read_bytes())
    for name,digest in audit['files'].items():assert sha(folder/name)==digest,name
    f=pd.read_parquet(folder/'features.parquet')
    assert f.index.max()<=pd.Timestamp('2025-12-31')
    assert (f.observation_date.dropna()<f.index.to_series()[f.observation_date.notna()]).all()
    assert ((f.index.to_series()-f.observation_date).dt.days[f.cgb10y.notna()]<=10).all()
    with duckdb.connect() as c:
        prices=c.execute('SELECT session,price_index FROM read_csv_auto(?) WHERE session<=CAST(? AS DATE) ORDER BY session',
            [str(Path(spec['source_chart'])/'银行平均股息率与价格指数.csv'),spec['end']]).df()
    calendar=pd.DatetimeIndex(prices.session);position={day:i for i,day in enumerate(calendar)}
    assert calendar.equals(f.index)
    basket=pd.read_parquet(spec['basket_path']).set_index('session').reindex(calendar)
    series={'price':prices.price_index.to_numpy(),'basket_total':basket.bank_equal_total_return_index.to_numpy()}
    labs={};checks=0;groups_checked=0
    for target,levels in series.items():
        lab=pd.read_parquet(folder/f'labels_{target}.parquet');labs[target]=lab
        for row in lab.itertuples():
            a=position[row.session]+1;b=a+row.horizon;actual=lab.loc[row.Index,'return']
            if b>=len(levels):assert row.status=='TAIL' and pd.isna(actual)
            else:
                assert row.entry==calendar[a] and row.exit==calendar[b]
                path=levels[a:b+1];good=np.isfinite(path).all() and (path>0).all()
                if target=='basket_total':good=good and basket.status.iloc[a:b+1].eq('READY').all() and basket.basket_segment.iloc[a:b+1].nunique()==1
                if good:
                    np.testing.assert_allclose(actual,path[-1]/path[0]-1,atol=1e-13)
                    np.testing.assert_allclose(row.drawdown,min(path[j]/max(path[:j+1])-1 for j in range(len(path))),atol=1e-13)
                    assert row.status=='READY'
                else:assert row.status=='PATH_BREAK' and pd.isna(actual)
            checks+=1
    for g in r['groups']:
        lab=labs[g['target']];y=lab[lab.horizon.eq(g['horizon'])].set_index('session')['return'].reindex(f.index)
        mask=f[g['rule']].eq(1 if g['group']=='high' else 0)&y.notna()
        assert mask.sum()==g['days']
        if mask.any():np.testing.assert_allclose(y[mask].mean(),g['mean_return'],atol=1e-13)
        groups_checked+=1
    raw_checks=0
    for t in r['tests']:
        if t['model']!='raw' or t['coefficient'] is None:continue
        lab=labs[t['target']];y=lab[lab.horizon.eq(t['horizon'])].set_index('session')['return'].reindex(f.index).to_numpy()
        x=f[t['rule']].to_numpy();valid=np.isfinite(x)&np.isfinite(y);hi=valid&(x==1);lo=valid&(x==0)
        effect=y[hi].mean()-y[lo].mean();influence=np.zeros(len(f))
        influence[hi]=(y[hi]-y[hi].mean())/hi.sum();influence[lo]=-(y[lo]-y[lo].mean())/lo.sum()
        variance=float(np.dot(influence,influence));lag=t['horizon']+20
        for k in range(1,lag+1):variance+=2*(1-k/(lag+1))*float(np.dot(influence[k:],influence[:-k]))
        se=math.sqrt(max(0,variance*valid.sum()/(valid.sum()-2)))
        np.testing.assert_allclose([effect,se],[t['coefficient'],t['se']],atol=1e-12);raw_checks+=1
    rows=pd.read_csv(folder/'entry_paths.csv',parse_dates=['signal','exit']);entry_checks=0
    for row in rows.itertuples():
        i=position[row.signal];a=i+1;b=a+row.horizon;levels=series[row.target]
        if row.profile=='immediate':days=[a];weights=[1.]
        elif row.profile=='split3':days=[a,a+20,a+40];weights=[1/3]*3
        else:
            signal=next((j for j in range(i,i+21) if f.trend_recovery.iloc[j]==1),None)
            days=[] if signal is None else [signal+1];weights=[] if signal is None else [1.]
        budget=np.ones(b-a+1)
        for day,weight in zip(days,weights):budget[day-a:]+=weight*(levels[day:b+1]/levels[day]-1)
        np.testing.assert_allclose([budget[-1]-1,np.min(budget/np.maximum.accumulate(budget)-1)],
                                  [row.capital_return,row.drawdown],atol=1e-13)
        assert row.filled==bool(days) and row.exit==calendar[b];entry_checks+=1
    concentration=[]
    lab=labs['basket_total'];y=lab[lab.horizon.eq(252)].set_index('session')['return'].reindex(f.index)
    good=f.spread2.notna()&y.notna()
    for year,g in f.loc[good].groupby(f.loc[good].index.year):
        concentration.append(dict(year=int(year),high=int(g.spread2.eq(1).sum()),other=int(g.spread2.eq(0).sum())))
    duplicate_flags=bool(f.yield5.equals(f.yield5_stable) and f.spread2.equals(f.spread2_stable))
    result=dict(status='PASS',no2026_outcomes=True,label_checks=checks,group_checks=groups_checked,
        raw_hac_checks=raw_checks,capital_path_checks=entry_checks,strict_lagged_rates_verified=True,
        spread2_basket252_year_concentration=concentration,broad_stability_has_no_increment_here=duplicate_flags,
        caveats=['spread3 has only9 high days; not enough to compute inference',
                 'basket252 spread2 raw p is year-confounded; year-controlled coefficient unidentifiable',
                 'broad dividend filter does not exclude any high-yield or spread2 dates in this sample'])
    with (folder/'independent_audit.json').open('x',encoding='utf-8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))


def appendix(folder):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle
    from pypdf import PdfReader,PdfWriter
    from xml.sax.saxutils import escape
    audit=json.loads((folder/'independent_audit.json').read_bytes());assert audit['status']=='PASS'
    manifest=json.loads((folder/'pdf_manifest.json').read_bytes());source=Path(manifest['path'])
    assert sha(source)==manifest['sha256']
    final=source.with_name(source.stem+'_审计版.pdf')
    if final.exists():raise ValueError('Final report already exists')
    temporary=ROOT/'tmp/pdfs/entry-audit-appendix.pdf'
    pdfmetrics.registerFont(TTFont('AuditCN','C:/Windows/Fonts/msyh.ttc',subfontIndex=0))
    body=ParagraphStyle('body',fontName='AuditCN',fontSize=10,leading=17,spaceAfter=12,wordWrap='CJK',textColor=colors.HexColor('#223c32'))
    head=ParagraphStyle('head',parent=body,fontSize=19,leading=27,spaceAfter=20)
    story=[]
    def para(t,style=body):story.append(Paragraph(escape(t),style))
    para('07 / 最终复核：把漂亮结果放回样本背景',head)
    para(f"独立复核通过：{audit['label_checks']:,}个收益与路径标签、{audit['group_checks']}组均值、{audit['raw_hac_checks']}项无条件HAC，以及{audit['capital_path_checks']}条入场预算路径。利率观察日期严格早于信号日；未使用2026收益。")
    para('三个需要特别说明的结果')
    para('1. 分红广度稳定条件并未剔除任何高息或补偿至少2个百分点的日期。因此，本轮组合结果与原条件完全一致，不能宣称加入稳定性后获得改进。需要继续细化具体银行的分红削减与权重影响。')
    para('2. 补偿至少3个百分点的样本只有9日。表中很高的平均收益值得记录，但推断因样本不足未计算；联合校正按p=1保守处理，不代表真的算出一个证明无效的p值。')
    para('3. 一年含分红篮子中，“补偿至少2个百分点”原始检验非常显著，但样本按年份完全分开：2022全部不满足，2023和2024全部满足。没有同年两种状态可比较，年份控制模型无法识别。不能把它当成利率补偿的独立预测证据。')
    data=[['信号年份','满足补偿条件','不满足条件']]+[[str(x['year']),str(x['high']),str(x['other'])] for x in audit['spread2_basket252_year_concentration']]
    data=[[Paragraph(x,body) for x in row] for row in data]
    table=Table(data,colWidths=[58*mm]*3);table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#deebe4')),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),4),('VALIGN',(0,0),(-1,-1),'TOP')]))
    story.extend([table,Spacer(1,12)])
    para('仍然值得继续研究：绝对高息条件的平均差异在去掉任一信号年份后保持为正；利率补偿提供更长的状态区间；分批投入在少数完整事件中改善部分回撤。这些是后续验证的方向，而不是已经确定的门槛或仓位规则。')
    para('整理说明：旧Markdown报告散文件移除，原始文本与哈希压缩归档；结构化实验和数据保留。未来正式验证应优先增加独立事件、统一可比样本与成交成本，而不是只增加参数尝试。')
    def foot(canvas,doc):
        canvas.setFont('AuditCN',8);canvas.setFillColor(colors.HexColor('#61786d'));canvas.drawString(18*mm,12*mm,'银行板块研究 | 独立复核与样本限制');canvas.drawRightString(192*mm,12*mm,'9')
    SimpleDocTemplate(str(temporary),pagesize=A4,leftMargin=18*mm,rightMargin=18*mm,topMargin=18*mm,bottomMargin=22*mm).build(story,onFirstPage=foot,onLaterPages=foot)
    reader=PdfReader(temporary)
    if len(reader.pages)!=1:raise ValueError('Audit appendix must fit one page')
    writer=PdfWriter();writer.append(str(source));writer.append(str(temporary))
    writer.add_metadata({'/Title':'银行板块高股息率与入场研究 - 审计版','/Author':'本项目研究'})
    with final.open('xb') as out:writer.write(out)
    result=dict(path=str(final),sha256=sha(final),pages=len(PdfReader(final).pages),independent_audit_sha256=sha(folder/'independent_audit.json'),
                base_pdf_sha256=sha(source),script_sha256=sha(__file__))
    with (folder/'pdf_final_manifest.json').open('x',encoding='utf-8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))


def archive(folder):
    final=json.loads((folder/'pdf_final_manifest.json').read_bytes());assert sha(final['path'])==final['sha256']
    root=(ROOT/'reports/bank_sector_research').resolve();history=root/'history';history.mkdir(exist_ok=True)
    zip_path=history/(folder.name+'-report-texts.zip');manifest_path=history/(folder.name+'-report-text-migration.json')
    files=list(root.rglob('*.md'))
    preview=Path(json.loads((folder/'pdf_manifest.json').read_bytes())['path'])
    files.append(preview)
    entries=[]
    with zipfile.ZipFile(zip_path,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            path=path.resolve()
            if path.suffix=='.md' and not path.is_relative_to(root):raise ValueError('Outside intended report root')
            if path.suffix=='.pdf' and path!=preview.resolve():raise ValueError('Unexpected preview target')
            member=path.relative_to(ROOT).as_posix();archive.write(path,member)
            entries.append(dict(path=str(path),zip_member=member,sha256=sha(path),bytes=path.stat().st_size))
    with zipfile.ZipFile(zip_path) as archive:
        for item in entries:assert hashlib.sha256(archive.read(item['zip_member'])).hexdigest()==item['sha256']
    record=dict(authorized_scope='User requested deletion of earlier bank research Markdown reports; original bytes retained for constitution traceability',
        archive=str(zip_path),archive_sha256=sha(zip_path),final_pdf=final['path'],final_pdf_sha256=final['sha256'],
        entries=entries,deletion_pending=True)
    with manifest_path.open('x',encoding='utf-8') as out:json.dump(record,out,ensure_ascii=False,indent=2)
    print(json.dumps({'manifest':str(manifest_path),'markdown_count':len(files)-1,'archive_verified':True},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['verify','appendix','archive']);p.add_argument('folder',type=Path)
    a=p.parse_args();globals()[a.command](a.folder.resolve())
