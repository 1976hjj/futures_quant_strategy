"""Read-only audit of completed bank backtests; append a report without running jobs."""
from pathlib import Path
import hashlib
import html
import json
import math

import duckdb
import numpy as np
import pandas as pd

ROOT = Path('D:/futures_quant_strategy')
RUNS = ROOT / 'reports/strategy_backtests'
INPUT = ROOT / 'data/factor_store/bank_inputs/d5590f6952b8a34075113c38567b3e56ae30ffa8895ae05172a683c8da0511e5'
OUT = ROOT / 'reports/bank_single_factor_results_20261009'
MAIN = Path('C:/Users/Adminis/Desktop/量化研究/研究成果/20261008_银行优质低估_v1/银行优质低估研究报告.html')
NAMES = {
 'bank-cash-dividend-yield-365': '现金股息率',
 'bank-quality-dividend-yield-365': '质量门槛后现金股息率',
}

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def pct(v):
    return f'{v * 100:.2f}%'

def svg_chart(results):
    colors = ['#137956', '#c18c20', '#526cb0', '#8a648d', '#888888']
    selected = results[:4]
    lines = [(r['name'], [d['nav']/r['config']['initial_cash_cny'] for d in r['daily']]) for r in selected]
    bench = selected[0]['benchmark']['daily']
    lines.append(('沪深300价格指数（非银行基准）', [d['close']/bench[0]['close'] for d in bench]))
    low = min(min(v) for _, v in lines) * .95
    high = max(max(v) for _, v in lines) * 1.04
    width, height, left, top, plotw, ploth = 1000, 440, 65, 90, 905, 285
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="银行因子组合净值曲线"><rect width="1000" height="440" fill="#fff"/>']
    for i, ((name, vals), color) in enumerate(zip(lines, colors)):
        x = 65 + (i % 3)*305; y = 25+(i//3)*24
        parts.append(f'<path d="M{x} {y}h20" stroke="{color}" stroke-width="3"/><text x="{x+27}" y="{y+4}" font-size="13">{html.escape(name)}</text>')
        points = ' '.join(f'{left+j/(len(vals)-1)*plotw:.1f},{top+(high-v)/(high-low)*ploth:.1f}' for j,v in enumerate(vals))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.8"/>')
    for value in np.linspace(low, high, 5):
        y = top+(high-value)/(high-low)*ploth
        parts.append(f'<path d="M{left} {y:.1f}h{plotw}" stroke="#ddd"/><text x="8" y="{y+4:.1f}" font-size="12">{value:.2f}</text>')
    sessions = [d['session'] for d in selected[0]['daily']]
    for year in range(2020,2026):
        j = next(i for i,d in enumerate(sessions) if d.startswith(str(year)))
        x = left+j/(len(sessions)-1)*plotw
        parts.append(f'<text x="{x:.1f}" y="403" font-size="13">{year}</text>')
    parts.append('</svg>')
    return ''.join(parts)

def main():
    db = duckdb.connect(str(ROOT/'data/warehouse/alpha_research.duckdb'), read_only=True)
    records = []
    originals = {}
    common = None
    for request_path in sorted(RUNS.glob('20261009-18*.request.json')):
        result_path = request_path.with_name(request_path.name.replace('.request.json','.result.json'))
        request = json.loads(request_path.read_text(encoding='utf-8'))
        result = json.loads(result_path.read_text(encoding='utf-8'))
        assert result['status'] == 'PASS'
        assert result['config'] == request
        compare = {k:v for k,v in request.items() if k not in ('name','score_rules')}
        if common is None: common = compare
        assert compare == common
        rule, = request['score_rules']
        factor = rule['factor_id']
        expected = 'LOW' if ('npl' in factor and 'improv' not in factor) or 'volatility' in factor else 'HIGH'
        assert rule['direction'] == expected, (factor, rule['direction'], expected)
        assert rule['transform'] == 'PERCENTILE' and rule['weight'] == 100
        original_name = request['name'].split('·')[1]
        name = NAMES.get(factor, original_name)
        result['name'] = name
        trades = pd.DataFrame(result['trades'])
        assert (trades['quantity']>0).all()
        assert trades[['session','ts_code','side','quantity']].duplicated().sum() == 0
        db.register('audit_trades', trades)
        nonbanks = db.execute("""select count(*) from audit_trades t where not exists(
            select 1 from read_parquet(?) m where m.ts_code=t.ts_code and m.l1_name='银行'
            and m.in_date<=cast(t.session as date) and (m.out_date is null or cast(t.session as date)<m.out_date))""",
            [str(INPUT/'membership.parquet')]).fetchone()[0]
        assert nonbanks == 0
        daily = pd.DataFrame(result['daily'])
        nav = daily['nav'].to_numpy()
        ret = daily['daily_return'].to_numpy()[1:]
        calc = {
          'total_return': nav[-1]/request['initial_cash_cny']-1,
          'annualized_return': (nav[-1]/nav[0])**(252/(len(nav)-1))-1,
          'maximum_drawdown': float(np.min(nav/np.maximum.accumulate(nav)-1)),
        }
        for key, val in calc.items():
            assert math.isclose(val,result['summary'][key],abs_tol=1e-9), (factor,key,val,result['summary'][key])
        for annual in result['annual']:
            rr = daily[daily.session.str.startswith(str(annual['year']))]['daily_return']
            assert math.isclose(float(np.prod(1+rr)-1),annual['return'],abs_tol=1e-9)
        assert math.isclose(float(trades.total_cost_cny.sum()),result['summary']['total_cost'],abs_tol=.02)
        assert math.isclose(float(daily.dividend_cash.sum()),result['summary']['total_dividend_cash'],abs_tol=.02)
        fpath = ROOT/'data/factor_store/releases'/rule['release_id'].split(':')[1]/'raw_factor_values.parquet'
        fv = pd.read_parquet(fpath)
        assert fv[['session','instrument_id']].duplicated().sum()==0
        assert (fv.available_at.dt.tz_convert('Asia/Shanghai').dt.date <= pd.to_datetime(fv.session).dt.date).all()
        assert set(fv.variant)=={'RAW'}
        counts = fv[fv.value.notna()].groupby('session').size()
        signals = daily.session.iloc[::request['rebalance_sessions']].tolist()
        pool = [int(counts.get(pd.Timestamp(s).date(),0)) for s in signals]
        # Identify missing marks using actual share positions, including share changes from trades.
        positions = {}
        missing = []
        trades_by_date = {s:g.to_dict('records') for s,g in trades.groupby('session')}
        market = pd.read_parquet(INPUT/'bank_market.parquet').set_index(['session','instrument_id'])['close']
        for d in result['daily']:
            for t in trades_by_date.get(d['session'],[]):
                positions[t['ts_code']] = t['post_quantity']
            if not d['missing_marks']: continue
            when = pd.Timestamp(d['session']).date()
            codes = [code for code,qty in positions.items() if qty>0 and pd.isna(market.get((when,code),np.nan))]
            for code in codes:
                susp = db.execute('select count(*) from raw.suspensions where ts_code=? and trade_date=? and suspend_type=\'S\'', [code,when]).fetchone()[0]
                missing.append({'session':str(when),'ts_code':code,'suspension_record':bool(susp)})
        assert len(missing)==int(daily.missing_marks.sum()), (factor,missing)
        # Ties at the top-five boundary quantify the deterministic code tie break.
        tie_dates = []
        for s in signals:
            rows = fv[(pd.to_datetime(fv.session).dt.date == pd.Timestamp(s).date()) & fv.value.notna()]
            if len(rows)<=5: continue
            vals = sorted(rows.value,reverse=rule['direction']=='HIGH')
            if vals[4]==vals[5]: tie_dates.append(s)
        years = {str(a['year']):a['return'] for a in result['annual']}
        other_years = float(np.prod([1+v for y,v in years.items() if y!='2024'])-1)
        early = float(np.prod([1+years[str(y)] for y in (2020,2021,2022)])-1)
        record = {'job_id':request_path.name.split('.request')[0], 'factor_id':factor, 'name':name,
          'release_id':rule['release_id'], 'direction':rule['direction'], 'summary':result['summary'],
          'annual':years,'independent_nav_metrics':calc,
          'bank_membership_failures':nonbanks, 'daily_count':len(daily),
          'under_five_days_excluding_initial':int((daily.positions.iloc[1:]<5).sum()),
          'minimum_positions_excluding_initial':int(daily.positions.iloc[1:].min()),
          'signal_pool_sizes':pool,'boundary_tie_dates':tie_dates,
          'missing_marks':missing, 'other_years_compound_diagnostic':other_years,
          '2020_2022_compound':early, 'request_sha256':sha(request_path),'result_sha256':sha(result_path),
          'factor_values_sha256':sha(fpath), 'average_exposure':float(daily.actual_stock_exposure.iloc[1:].mean())}
        records.append(record); originals[factor] = result
    assert len(records)==14
    records.sort(key=lambda r:r['summary']['annualized_return'], reverse=True)
    common_path = OUT.with_suffix('.json')
    common_path.write_text(json.dumps({'status':'AUDITED_RESEARCH_ONLY','common_parameters':common,'results':records,
      'limits':['Current 42-bank source cohort with PIT listing/membership masks; exhaustive historical universe and revision certification incomplete.',
      'Exposed 2020-2025 interval; no untouched out-of-sample evidence.',
      'CSI300 is a price index, not a bank total-return benchmark.']},ensure_ascii=False,indent=2),encoding='utf-8')
    selected = [originals[r['factor_id']] for r in records]
    chart = svg_chart(selected)
    OUT.with_suffix('.svg').write_text(chart,encoding='utf-8')
    table = '<table><tr><th>因子</th><th>系统年化</th><th>累计收益</th><th>最大回撤</th><th>夏普</th><th>不足5只天数*</th></tr>'
    for r in records:
        s = r['summary']
        table += f'<tr><td>{html.escape(r["name"])}</td><td>{pct(s["annualized_return"])}</td><td>{pct(s["total_return"])}</td><td>{pct(s["maximum_drawdown"])}</td><td>{s["sharpe"]:.2f}</td><td>{r["under_five_days_excluding_initial"]}</td></tr>'
    table += '</table><p>*扣除初始尚未买入日。不足5只主要是早期因子历史覆盖不足；不能把这些行视为始终持有5只的完全同口径比较。</p>'
    yearly = '<table><tr><th>因子</th>'+''.join(f'<th>{y}</th>' for y in range(2020,2026))+'</tr>'
    for r in records:
        yearly += '<tr><td>'+html.escape(r['name'])+'</td>'+''.join(f'<td>{pct(r["annual"][str(y)])}</td>' for y in range(2020,2026))+'</tr>'
    yearly += '</table>'
    cash = next(r for r in records if r['factor_id']=='bank-cash-dividend-yield-365')
    gate = next(r for r in records if r['factor_id']=='bank-quality-dividend-yield-365')
    qscore = next(r for r in records if r['factor_id']=='bank-quality-score-pit')
    benchmark = selected[0]['benchmark']['daily']
    btotal = benchmark[-1]['close']/benchmark[0]['close']-1
    test_table = '<table><tr><th>单因子</th><th>目标持股</th><th>保留排名</th><th>调仓间隔（交易日）</th><th>用途</th></tr>'
    for label in ['现金股息率','质量门槛后现金股息率']:
        for n,interval,purpose in [(42,63,'有值银行样本等权对照，分离行业/质量筛选与排序收益'),(10,63,'检验前5名是否过于集中'),(5,126,'检验换仓频率敏感性')]:
            test_table += f'<tr><td>{label}</td><td>{n}</td><td>{n}</td><td>{interval}</td><td>{purpose}</td></tr>'
    test_table += '</table>'
    section = f'''<section id="bank-single-factor-results-20261009" style="margin:32px auto;padding:28px;max-width:1200px;background:#fff;color:#25382f;font:15px/1.7 system-ui,Microsoft YaHei,sans-serif">
<style>#bank-single-factor-results-20261009 table{{border-collapse:collapse;width:100%;font-size:14px;margin:20px 0}}#bank-single-factor-results-20261009 td,#bank-single-factor-results-20261009 th{{padding:8px;border-bottom:1px solid #ddd;text-align:right}}#bank-single-factor-results-20261009 td:first-child,#bank-single-factor-results-20261009 th:first-child{{text-align:left}}#bank-single-factor-results-20261009 h2,#bank-single-factor-results-20261009 h3{{color:#137956}}</style>
<h2>追加研究：14项银行单因子组合回测与参数核验（2026-10-09）</h2>
<p><b>结论：</b>在这组已完成回测里，质量门槛后现金股息率、现金股息率表现领先。质量评分单独排名最弱；质量指标目前更适合作为筛选条件的研究候选。暂未证明长期稳定行业超额收益，也未证明质量门槛一定降低回撤。</p>
<h3>实际参数与执行核验</h3>
<p>14项均为PASS，提交请求与结果中的参数快照逐字段一致。区间2020-01-02至2025-12-31；目标5只、保留排名5、调仓间隔63交易日、现金预留2%、初始100万元、上市至少60交易日、排除ST及异常状态、S0不择时。单因子权重100%，百分位排序；不良率和ROE波动率取低，其余取高。质量门槛后股息率已在因子值层面过滤，不另加过滤器。</p>
<p>界面股票池虽为ALL-A-PIT，冻结因子只覆盖银行。全部实际成交均通过冻结行业成员时点交集检查，没有买入非银行。信号当日收盘形成，下一交易日开盘交易；因子可用时间不晚于对应信号日。该核验不能替代完整财报历史修订版本认证，输入仍标记RESEARCH_ONLY。</p>
<p>1455个交易日，24次调仓（初始建仓加后续23次）；预检显示23是估算口径，不是调仓参数错误。使用原价、整数真实股数和实际成交序列。佣金买卖各2bps、最低5元；印花税历史10bps/新5bps；过户费历史0.2bps/新0.1bps；基础滑点2bps、平方根冲击20bps、最高滑点100bps、最大参与率10%。现金分红已入账；滑点进入成交价。</p>
<p>独立按每日净值重新计算累计收益、年化和最大回撤，并对逐年收益、成交费用、分红现金逐项对账，均一致。系统年化按252交易日/年计算，不是按日历六年计算。没有重新发起训练或回测。</p>
{table}
<h3>净值与年度表现</h3>{chart}{yearly}
<p>沪深300价格指数同期间累计{pct(btotal)}，含现金分红的银行组合与它不是同收益口径；它也不是银行行业对照。所有14项在2024年均盈利，提示行业行情影响很强，应先补同样银行样本、同样成本及分红记账的等权对照。</p>
<p>现金股息率2020—2022合计{pct(cash['2020_2022_compound'])}，2024年单年{pct(cash['annual']['2024'])}；其余五个年份收益相乘为{pct(cash['other_years_compound_diagnostic'])}。质量门槛后股息率对应为{pct(gate['2020_2022_compound'])}、{pct(gate['annual']['2024'])}、{pct(gate['other_years_compound_diagnostic'])}。排除2024只是集中度诊断，绝不是可以交易的删年策略或独立样本外测试。</p>
<p>质量门槛后股息率相对普通现金股息率：年化增加{(gate['summary']['annualized_return']-cash['summary']['annualized_return'])*100:.2f}个百分点，累计收益增加{(gate['summary']['total_return']-cash['summary']['total_return'])*100:.2f}个百分点，但最大回撤由{pct(cash['summary']['maximum_drawdown'])}扩大到{pct(gate['summary']['maximum_drawdown'])}。门槛收益提升主要出现在2023/2024，早期2020/2021并不占优；更抗跌的目标仍待检验。</p>
<h3>核验发现及限制</h3>
<p>部分组合有6个持仓缺报价日；ROE三年变量组合各12个持仓日计数（6日各2只）。已逐项查询本地停牌记录，详见审计JSON。现金股息率的6日为浙商银行2023-06-15/16/19/20/21/26停牌，估值沿用最后有效收盘价，并非填零。停牌期曲线暂时平坦，不代表风险消失。停牌与配股等公司行动的完整历史认证仍受既有输入审计限制。</p>
<p>部分结果rejection_counts中出现FILLED：检查执行代码及成交记录后，是不足一手/剩余现金尝试的状态标签沿用；全部真实成交数量大于0，没有因此生成零数量成交。它影响未成交原因的展示，不改变已对账净值；本轮没有修改业务代码。</p>
<p>质量分是离散档位，规则质量分在{len(qscore['boundary_tie_dates'])}/24个调仓信号日都发生前5名边界并列，以证券代码打破并列。因此这条结果不足以否定银行质量研究，尤其不能理解为质量高的银行都不值得选；它反映该粗分档公式不适合单独挑前5只。并列日期已保留。当前输入是42家银行来源名单，经上市/行业时点过滤，不等同于完整历史银行全样本。2020—2025已被反复观察，补测是稳健性诊断，不能当作全新样本外证明。</p>
<h3>下一轮手动补测：先6个，不做大范围参数搜索</h3>
<p>从对应已完成任务复制参数；保留冻结因子版本、区间、方向HIGH、百分位、权重100%、RAW、成本、现金预留与S0设置，只修改下表三项。42是目标上限：当时不足42个有效值时买入全部有效银行，不补非银行。这两条对照分别回答普通银行样本收益和通过质量门槛的银行样本收益。</p>
{test_table}
<p>优先比较两条等权对照，再看10只或126日是否仍有优势；判断标准是净超额收益、回撤、年度持续性及参数邻近是否一致，而不是挑某一个最高年化。年度盈利收益率可列为第二轮对照，ROE波动率需统一充足历史区间后再测。当前没有自动提交任何新任务。</p>
<details><summary>任务追踪</summary><ul>{''.join('<li>'+html.escape(r['name'])+'：'+r['job_id']+'；'+html.escape(r['factor_id'])+'</li>' for r in records)}</ul></details>
</section>'''
    document = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>银行单因子结果核验</title></head><body>'+section+'</body></html>'
    OUT.with_suffix('.html').write_text(document,encoding='utf-8')
    old = MAIN.read_bytes()
    marker = b'id="bank-single-factor-results-20261009"'
    if marker not in old:
        archive = MAIN.parent/'prior_report_archive'/f'before_single_factor_results_20261009_{hashlib.sha256(old).hexdigest()[:12]}.html'
        archive.parent.mkdir(exist_ok=True)
        if not archive.exists(): archive.write_bytes(old)
        closing = old.lower().rfind(b'</body>')
        assert closing>=0
        new = old[:closing]+section.encode('utf-8')+b'\n'+old[closing:]
        assert new[:closing] == old[:closing] and new[-len(old[closing:]):] == old[closing:]
        MAIN.write_bytes(new)
        OUT.with_name(OUT.name+'_integration_receipt.json').write_text(json.dumps({
          'old_report_sha256':hashlib.sha256(old).hexdigest(),'new_report_sha256':hashlib.sha256(new).hexdigest(),
          'archive':str(archive),'prior_report_bytes_preserved':True,'inserted_before_final_body':True,
          'section_id':'bank-single-factor-results-20261009'},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(OUT.with_suffix('.html')),'integrated_report':str(MAIN),
      'runs':len(records),'bank_trade_failures':sum(r['bank_membership_failures'] for r in records),
      'unconfirmed_missing_marks':sum(not m['suspension_record'] for r in records for m in r['missing_marks']),
      'top':[{k:r[k] for k in ['name','2020_2022_compound','other_years_compound_diagnostic','boundary_tie_dates']} for r in records[:4]]},ensure_ascii=False))

if __name__ == '__main__':
    main()
