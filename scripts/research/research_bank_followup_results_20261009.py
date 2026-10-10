"""Audit and summarize eight frozen bank backtests; no training or job submission."""
from pathlib import Path
import hashlib
import html
import json
import math
import xml.etree.ElementTree as ET

import duckdb
import numpy as np
import pandas as pd

ROOT = Path('D:/futures_quant_strategy')
RUNS = ROOT/'reports/strategy_backtests'
OUT = ROOT/'reports/bank_followup_results_20261009'
MAIN = Path('C:/Users/Adminis/Desktop/量化研究/研究成果/20261008_银行优质低估_v1/银行优质低估研究报告.html')
MEMBERS = ROOT/'data/factor_store/bank_inputs/d5590f6952b8a34075113c38567b3e56ae30ffa8895ae05172a683c8da0511e5/membership.parquet'
SPECS = [
 ('20261009-191906-bd92c5','银行有效样本等权',42,63),
 ('20261009-191950-0d669c','质量门槛后等权',42,63),
 ('20261009-183052-a6a8de','股息率·前5·63日',5,63),
 ('20261009-192144-743a69','股息率·前10·63日',10,63),
 ('20261009-192226-84fcea','股息率·前5·126日',5,126),
 ('20261009-183622-52d73d','质量＋股息率·前5·63日',5,63),
 ('20261009-192158-d5f6de','质量＋股息率·前10·63日',10,63),
 ('20261009-192243-595edf','质量＋股息率·前5·126日',5,126),
]

def pct(v): return f'{v*100:.2f}%'

def curve(rows):
    colors=['#7a7e83','#167a56','#d39725','#6876ba']
    low=min(min(r['nav']) for r in rows)*.95
    high=max(max(r['nav']) for r in rows)*1.03
    top,ph,left,pw=85,275,65,900
    svg=['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 420" role="img" aria-label="银行组合净值对比"><rect width="1000" height="420" fill="white"/>']
    for i,r in enumerate(rows):
        x=65+(i%2)*455;y=24+(i//2)*25
        svg.append(f'<path d="M{x} {y}h20" stroke="{colors[i]}" stroke-width="3"/><text x="{x+27}" y="{y+4}" font-size="14">{html.escape(r["name"])}</text>')
    for v in np.linspace(low,high,5):
        y=top+(high-v)/(high-low)*ph
        svg.append(f'<path d="M65 {y:.1f}h900" stroke="#e4e4e4"/><text x="10" y="{y+4:.1f}" font-size="12">{v:.2f}</text>')
    for i,r in enumerate(rows):
        points=' '.join(f'{left+j/(len(r["nav"])-1)*pw:.1f},{top+(high-v)/(high-low)*ph:.1f}' for j,v in enumerate(r['nav']))
        svg.append(f'<polyline points="{points}" fill="none" stroke="{colors[i]}" stroke-width="1.8"/>')
    for year in range(2020,2026):
        j=next(i for i,s in enumerate(rows[0]['sessions']) if s.startswith(str(year)))
        x=left+j/(len(rows[0]['sessions'])-1)*pw
        svg.append(f'<text x="{x:.1f}" y="392" font-size="13">{year}</text>')
    svg.append('</svg>')
    return ''.join(svg)

def main():
    db=duckdb.connect(str(ROOT/'data/warehouse/alpha_research.duckdb'),read_only=True)
    rows=[]; common=None
    for run,label,n,interval in SPECS:
        reqpath=RUNS/(run+'.request.json');resultpath=RUNS/(run+'.result.json')
        q=json.loads(reqpath.read_text(encoding='utf-8'))
        r=json.loads(resultpath.read_text(encoding='utf-8'))
        assert r['status']=='PASS' and r['config']==q
        assert (q['target_count'],q['retention_rank'],q['rebalance_sessions'])==(n,n,interval)
        core={k:v for k,v in q.items() if k not in ['name','score_rules','target_count','retention_rank','rebalance_sessions']}
        if common is None: common=core
        assert core==common
        rule,=q['score_rules']
        assert rule['direction']=='HIGH' and rule['weight']==100 and rule['transform']=='PERCENTILE'
        expected={'bank-cash-dividend-yield-365':'sha256:5e1801b070a333895e4af6832ba5be9d1655ad350f520faef4083ea50e9d1d1b',
                  'bank-quality-dividend-yield-365':'sha256:9ed5977f7cbadde324ee4e979b5f130f1cee2e31a5d2c781601e690ed911da79'}
        assert rule['release_id']==expected[rule['factor_id']]
        daily=pd.DataFrame(r['daily']);nav=daily.nav.to_numpy();summary=r['summary']
        assert len(nav)==1455
        assert math.isclose(nav[-1]/nav[0]-1,summary['total_return'],abs_tol=1e-9)
        assert math.isclose((nav[-1]/nav[0])**(252/(len(nav)-1))-1,summary['annualized_return'],abs_tol=1e-9)
        assert math.isclose(float(min(nav/np.maximum.accumulate(nav)-1)),summary['maximum_drawdown'],abs_tol=1e-9)
        assert summary['rebalance_count']==(24 if interval==63 else 12)
        trades=pd.DataFrame(r['trades']);assert (trades.quantity>0).all()
        assert math.isclose(float(trades.total_cost_cny.sum()),summary['total_cost'],abs_tol=.02)
        assert math.isclose(float(daily.dividend_cash.sum()),summary['total_dividend_cash'],abs_tol=.02)
        db.register('audit_followup_trades',trades)
        assert db.execute("""select count(*) from audit_followup_trades t where not exists (
         select 1 from read_parquet(?) m where m.ts_code=t.ts_code and m.l1_name='银行'
         and m.in_date<=cast(t.session as date) and (m.out_date is null or cast(t.session as date)<m.out_date))""",[str(MEMBERS)]).fetchone()[0]==0
        annual={str(a['year']):a['return'] for a in r['annual']}
        for y,v in annual.items():
            assert math.isclose(float(np.prod(1+daily[daily.session.str.startswith(y)].daily_return)-1),v,abs_tol=1e-9)
        rows.append({'job_id':run,'name':label,'factor_id':rule['factor_id'],'release_id':rule['release_id'],
         'target_count':n,'retention_rank':n,'rebalance_sessions':interval,'summary':summary,'annual':annual,
         'nav':(nav/nav[0]).tolist(),'sessions':daily.session.tolist(),
         'average_exposure':float(daily.actual_stock_exposure.iloc[1:].mean()),
         'minimum_actual_positions':int(daily.positions.iloc[1:].min()),
         'maximum_actual_positions':int(daily.positions.iloc[1:].max()),
         'under_target_days':int((daily.positions.iloc[1:]<n).sum()),
         'compound_2020_2022':float(np.prod([1+annual[str(y)] for y in (2020,2021,2022)])-1),
         'compound_other_than_2024':float(np.prod([1+v for y,v in annual.items() if y!='2024'])-1),
         'request_sha256':hashlib.sha256(reqpath.read_bytes()).hexdigest(),
         'result_sha256':hashlib.sha256(resultpath.read_bytes()).hexdigest()})
    base=rows[0]
    for row in rows:
        assert row['sessions']==base['sessions']
        row['annualized_return_difference_pp']=(row['summary']['annualized_return']-base['summary']['annualized_return'])*100
        row['relative_terminal_wealth']=row['nav'][-1]/base['nav'][-1]-1
        row['annual_active_pp']={y:(v-base['annual'][y])*100 for y,v in row['annual'].items()}
        row['years_above_baseline']=sum(v>0 for v in row['annual_active_pp'].values())
    jsonout={'status':'EIGHT_RUNS_AUDITED_RESEARCH_ONLY','common_parameters':common,'results':rows,
             'limits':['Current 42-bank source cohort; historical universe and revisions not exhaustively certified.',
                       '2020-2025 exposed, not untouched out of sample.',
                       '42 target is all available eligible names, not 42 names on every day.',
                       '126-session ranked strategies currently only have 63-session equal-weight controls.']}
    OUT.with_suffix('.json').write_text(json.dumps(jsonout,ensure_ascii=False,indent=2),encoding='utf-8')
    charts=[curve([rows[0],rows[2],rows[3],rows[4]]),curve([rows[1],rows[5],rows[6],rows[7]])]
    for i,c in enumerate(charts):
        ET.fromstring(c);OUT.with_name(OUT.name+f'_curve_{i+1}.svg').write_text(c,encoding='utf-8')
    table='<table><tr><th>组合</th><th>年化</th><th>累计收益</th><th>最大回撤</th><th>夏普</th><th>年化较银行等权差值</th><th>费用（元）</th></tr>'
    for row in rows:
        s=row['summary']
        table+=f'<tr><td>{html.escape(row["name"])}</td><td>{pct(s["annualized_return"])}</td><td>{pct(s["total_return"])}</td><td>{pct(s["maximum_drawdown"])}</td><td>{s["sharpe"]:.2f}</td><td>{row["annualized_return_difference_pp"]:+.2f}个百分点</td><td>{s["total_cost"]:,.2f}</td></tr>'
    table+='</table>'
    year_table='<table><tr><th>组合</th>'+''.join(f'<th>{y}</th>' for y in range(2020,2026))+'<th>跑赢银行等权年份</th></tr>'
    for row in rows:
        year_table+='<tr><td>'+html.escape(row['name'])+'</td>'+''.join('<td>'+pct(row['annual'][str(y)])+'</td>' for y in range(2020,2026))+f'<td>{row["years_above_baseline"]}/6</td></tr>'
    year_table+='</table>'
    coverage='<table><tr><th>组合</th><th>平均股票仓位</th><th>实际持股范围</th><th>扣除2024的其余年份连乘*</th></tr>'
    for row in rows:
        coverage+=f'<tr><td>{html.escape(row["name"])}</td><td>{pct(row["average_exposure"])}</td><td>{row["minimum_actual_positions"]}—{row["maximum_actual_positions"]}</td><td>{pct(row["compound_other_than_2024"])}</td></tr>'
    coverage+='</table><p>*仅是收益集中度诊断，不是删年后可以交易的策略，也不是样本外验证。</p>'
    section=f'''<section id="bank-followup-results-20261009" style="margin:32px auto;padding:28px;max-width:1200px;background:#fff;color:#26382f;font:15px/1.7 system-ui,Microsoft YaHei,sans-serif">
<style>#bank-followup-results-20261009 table{{border-collapse:collapse;width:100%;font-size:14px;margin:18px 0}}#bank-followup-results-20261009 td,#bank-followup-results-20261009 th{{padding:8px;border-bottom:1px solid #ddd;text-align:right}}#bank-followup-results-20261009 td:first-child,#bank-followup-results-20261009 th:first-child{{text-align:left}}#bank-followup-results-20261009 h2,#bank-followup-results-20261009 h3{{color:#147956}}</style>
<h2>追加研究：银行等权对照与持股/调仓稳健性（2026-10-09）</h2>
<p><b>结论：</b>这一轮支持“先排除基本面较差的银行，再按已实施现金股息率找便宜银行”的研究方向。六条选股组合在同一历史区间都高于银行有效样本等权对照；前5扩大到前10、63日改成126日，收益优势仍存在。这是有限参数邻近的历史稳健性证据，不是长期稳定或独立样本外证明。</p>
<h3>参数与结果核验</h3>
<p>新6项均PASS；与旧2项组成8项比较。实际结果config逐字段等于提交请求；除名称、两条因子、目标持股/保留排名、调仓间隔外，其余参数一致。使用同一冻结因子版本、2020-01-02至2025-12-31、百分位HIGH排序、100%权重、实际持仓序列、S0、预留2%现金、100万元及相同交易成本。63日组24次、126日组12次调仓，实际次数与设置吻合。全部成交均为冻结行业时点范围中的银行。</p>
<p>按每日净值独立重算累计收益、252交易日口径年化、最大回撤，逐年收益连乘、成交费用及现金分红全部对账一致。没有重新提交任务、改变参数或获取新数据。</p>
{table}
<h3>优势来源：质量门槛和股息排序都有贡献</h3>
<p>银行有效样本等权年化4.21%，质量门槛后等权6.01%：增加约1.80个百分点，且六个年份都高于普通银行等权；最大回撤仅由21.99%改善至21.31%。因此质量门槛有一定筛选价值，但本身没有产生足够强的回撤保护。</p>
<p>普通股息率前5/前10、63日组年化分别10.46%/9.99%，高于未排序的4.21%；质量门槛内再选股息率前5/前10，分别12.08%/11.72%，高于门槛后等权6.01%。这说明这一阶段的差异不能只由行业普涨或质量门槛解释，股息排序也有历史增量。这里是组合对照，不能当作严格因果分解或把两个增量简单相加。</p>
<p>质量＋股息率前10、63日是当前兼顾分散和回撤的研究候选：年化11.72%、最大回撤14.35%，较质量前5、63日年化少0.36个百分点，回撤改善约0.97个百分点。质量＋股息率前5、126日年化最高12.52%、最大回撤16.04%；较63日费用从14,990.88降至8,228.84元，但早期2020—2022累计亏损11.25%，更高全期收益不代表每阶段更抗跌。普通现金股息率前5、63日回撤14.38%、夏普0.73，也应保留为简单对照。</p>
<p>三个参数组合中，质量门槛后股息率都提高了年化；回撤在前10/63日及前5/126日改善，在原前5/63日略差。不能把“质量门槛”笼统等同于“止损”或“保证抗跌”。当前都是近满仓银行股票组合，仍暴露于银行行业风险。</p>
<h3>曲线</h3><p>普通现金股息率：同有值银行样本等权与三个排序组合。</p>{charts[0]}
<p>质量门槛后现金股息率：同通过门槛银行样本等权与三个排序组合。</p>{charts[1]}
<h3>年度表现与收益集中度</h3>{year_table}
<p>2024年行业对照上涨35.56%，确实贡献很大，但2022/2023选股组合也显示优势。例如2023年银行等权−1.23%，六条选股组合为+15.02%至+22.38%。因此不应把结果全部归为2024年普涨。2025年多数63日组合落后于等权，说明股息排序并非年年占优。</p>
{coverage}
<h3>比较边界与下一步</h3>
<p>“等权42”是目标上限，不是每期固定42只：普通银行对照实际{rows[0]['minimum_actual_positions']}—{rows[0]['maximum_actual_positions']}只，质量门槛对照{rows[1]['minimum_actual_positions']}—{rows[1]['maximum_actual_positions']}只；这是名单随上市、可用值、质量筛选及成交约束变化的结果。四条新排序组合始终达到5/10只。对照平均仓位95.82%/96.40%，排序组合约97.3%—97.5%；少量仓位差影响收益，不能把全部差值都称为纯因子Alpha。整手和成交限制也会使实际权重偏离理想等权。</p>
<p>126日排序组合尚缺同频率等权对照。如果继续补测，只需“银行有效样本等权42/保留42/126日”和“质量门槛后等权42/保留42/126日”两项，其他参数和版本保持一致；目的为补齐比较口径，不再大范围挑最高收益参数。当前最合理的后续研究候选是质量＋股息率前10、63日，另保留前5、126日与普通股息前5、63日对照。冻结规则后可做向前观察；2020—2025已被反复查看，不能将其包装为未知未来。</p>
<p>数据仍来自当前42家银行来源名单，经时点上市/行业过滤；完整历史行业名单与财报修订版本认证尚不完备，维持RESEARCH_ONLY。分红、停牌和公司行动口径沿用已有审计限制。这轮没有训练LightGBM，也没有推出实时投资仓位建议。</p>
<details><summary>任务及版本追踪</summary><ul>{''.join('<li>'+html.escape(r['name'])+'：'+r['job_id']+'；'+r['release_id']+'</li>' for r in rows)}</ul></details></section>'''
    document='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>银行补测结果分析</title></head><body>'+section+'</body></html>'
    OUT.with_suffix('.html').write_text(document,encoding='utf-8')
    old=MAIN.read_bytes()
    if b'id="bank-followup-results-20261009"' not in old:
        digest=hashlib.sha256(old).hexdigest()
        archive=MAIN.parent/'prior_report_archive'/f'before_followup_results_20261009_{digest[:12]}.html'
        if not archive.exists():archive.write_bytes(old)
        i=old.lower().rfind(b'</body>');assert i>=0
        new=old[:i]+section.encode('utf-8')+b'\n'+old[i:]
        assert new[:i]==old[:i] and new.endswith(old[i:])
        MAIN.write_bytes(new)
        OUT.with_name(OUT.name+'_integration_receipt.json').write_text(json.dumps({'old_report_sha256':digest,
         'new_report_sha256':hashlib.sha256(new).hexdigest(),'archive':str(archive),
         'original_report_bytes_preserved':True,'section_id':'bank-followup-results-20261009'},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'VERIFIED','report':str(OUT.with_suffix('.html')),'main_report':str(MAIN),
                      'rows':len(rows),'curves':2,'production_code_changed':False},ensure_ascii=False))

if __name__=='__main__': main()
