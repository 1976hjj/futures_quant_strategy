"""Independent arithmetic, timing, family and concentration audit for horizons."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def adjusted(ps):
    values = np.array([1. if p is None else p for p in ps])
    order = np.argsort(values)
    sorted_adjusted = np.minimum(1, np.maximum.accumulate(values[order] * np.arange(len(values), 0, -1)))
    result = np.empty(len(values)); result[order] = sorted_adjusted
    return result


def verify(folder):
    spec = json.loads((folder / 'experiment_spec.json').read_bytes())
    audit = json.loads((folder / 'audit.json').read_bytes())
    for name, digest in audit['files'].items():
        assert sha(folder / name) == digest, name
    r = json.loads((folder / 'results.json').read_bytes())
    f = pd.read_parquet(folder / 'features.parquet')
    with duckdb.connect() as c:
        d = c.execute('SELECT session,price_index FROM read_csv_auto(?) WHERE session<=CAST(? AS DATE) ORDER BY session',
            [str(Path(spec['source_chart']) / '银行平均股息率与价格指数.csv'), spec['end']]).df()
    calendar = pd.DatetimeIndex(d.session)
    assert calendar.equals(f.index) and calendar.max() <= pd.Timestamp('2025-12-31')
    price = d.price_index.to_numpy()
    b = pd.read_parquet(spec['basket_path'])
    assert pd.to_datetime(b.session).max() <= pd.Timestamp(spec['end'])
    b = b.set_index(pd.to_datetime(b.session)).reindex(calendar)
    sources = {'price': price, 'basket_total': b.bank_equal_total_return_index.to_numpy()}
    position = {day: i for i, day in enumerate(calendar)}
    labs = {}
    label_checks = 0
    for target, levels in sources.items():
        lab = pd.read_parquet(folder / ('labels_' + target + '.parquet'))
        labs[target] = lab
        for row in lab.itertuples():
            i = position[row.session]; a, z = i+1, i+1+row.horizon
            actual = lab.loc[row.Index, 'return']
            if z >= len(levels):
                assert row.status == 'TAIL' and pd.isna(actual)
            else:
                assert row.entry == calendar[a] and row.exit == calendar[z]
                path = levels[a:z+1]
                good = np.isfinite(path).all() and (path > 0).all()
                if target == 'basket_total':
                    good = good and b.status.iloc[a:z+1].eq('READY').all() and b.basket_segment.iloc[a:z+1].nunique()==1
                if good:
                    expected_drawdown = min(path[j] / max(path[:j+1]) - 1 for j in range(len(path)))
                    np.testing.assert_allclose([actual, row.drawdown, row.min_return, row.max_return],
                        [path[-1]/path[0]-1, expected_drawdown, min(path/path[0]-1), max(path/path[0]-1)], atol=1e-13)
                    assert row.status == 'READY'
                else:
                    assert row.status == 'PATH_BREAK' and pd.isna(actual)
            label_checks += 1
    # Independent past-only rank for every date, including coverage failures.
    values = f.mean_yield.to_numpy()
    covered = f.valid_banks.ge(10) & f.valid_banks.div(f.priced_banks).ge(.7)
    for i in range(len(f)):
        past = values[max(0,i-756):i]
        past = past[covered.iloc[max(0,i-756):i].to_numpy() & np.isfinite(past)]
        expected = np.nan
        if covered.iloc[i] and np.isfinite(values[i]) and len(past) >= 504:
            expected = (sum(past < values[i]) + .5*sum(past == values[i]))/len(past)
        np.testing.assert_allclose(expected, f.yield_rank.iloc[i], atol=1e-14)
    trend = np.full(len(f), np.nan)
    for i in range(59, len(f)):
        trend[i] = float(price[i] > price[i-20] and price[i] >= np.mean(price[i-59:i+1]))
    np.testing.assert_allclose(trend, f.trend_recovery, atol=1e-14)
    # All group means and raw binary contrasts use independent scalar formulas.
    for g in r['groups']:
        y = labs[g['target']]; y = y[y.horizon.eq(g['horizon'])].set_index('session').reindex(f.index)
        mask = f[g['rule']].eq(1 if g['group']=='high' else 0) & y['return'].notna()
        if g['sample'] == 'year_complete':
            ly = labs[g['target']]
            common = ly[ly.horizon.eq(252)].set_index('session')['return'].notna().reindex(f.index)
            mask &= common
        assert int(mask.sum()) == g['days']
        if mask.any():
            np.testing.assert_allclose([y.loc[mask,'return'].mean(), y.loc[mask,'return'].gt(0).mean()],
                                      [g['mean_return'], g['win_rate']], atol=1e-14)
    raw_checks = 0
    for t in r['tests']:
        if t['model'] != 'raw' or t['coefficient'] is None:
            continue
        x = f[t['rule']].to_numpy()
        lab = labs[t['target']]
        y = lab[lab.horizon.eq(t['horizon'])].set_index('session')['return'].reindex(f.index).to_numpy()
        valid = np.isfinite(x) & np.isfinite(y)
        hi, lo = valid & (x==1), valid & (x==0)
        difference = y[hi].mean()-y[lo].mean()
        influence = np.zeros(len(f))
        influence[hi] = (y[hi]-y[hi].mean())/hi.sum()
        influence[lo] = -(y[lo]-y[lo].mean())/lo.sum()
        variance = sum(influence**2)
        lag = t['horizon'] + 20
        for k in range(1, lag+1):
            variance += 2*(1-k/(lag+1))*float(np.dot(influence[k:], influence[:-k]))
        se = math.sqrt(max(0, variance*valid.sum()/(valid.sum()-2)))
        p = math.erfc(abs(difference/se)/math.sqrt(2)) if se>0 else 1.
        np.testing.assert_allclose([difference, se, p], [t['coefficient'], t['se'], t['p_value']], rtol=1e-8, atol=1e-13)
        raw_checks += 1
    prior = []
    for item in spec['prior_results']:
        assert sha(item['path']) == item['sha256']
        old = json.loads(Path(item['path']).read_bytes())
        prior += old.get('tests',old.get('single_tests',[])+old.get('combinations',[]))
    assert len(prior)+len(r['tests']) == r['joint_family_size'] == 380
    np.testing.assert_allclose(adjusted([t['p_value'] for t in prior+r['tests']])[len(prior):],
                               [t['holm_joint'] for t in r['tests']], atol=1e-14)
    events = pd.read_csv(folder / 'entry_episodes.csv', parse_dates=['session'])
    for rule in spec['rules']:
        if rule.startswith('within_'):
            continue
        x = f[rule].to_numpy(); chosen=[]; next_allowed=0
        for i in range(1, len(f)):
            if x[i]==1 and x[i-1]==0 and i>=next_allowed:
                chosen.append(f.index[i]); next_allowed=i+61
        for target in spec['targets']:
            for h in spec['horizons']:
                e = events[events.rule.eq(rule) & events.target.eq(target) & events.horizon.eq(h)]
                assert list(e.session) == chosen
    # Audit apparent strong results for lack of within-year support.
    concentration = []
    for target in spec['targets']:
        for h in spec['horizons']:
            lab = labs[target]
            y = lab[lab.horizon.eq(h)].set_index('session')['return'].reindex(f.index)
            good = y.notna() & f.high80.eq(1) & f.within_high80_cash.notna()
            table = f.loc[good,['within_high80_cash']].copy()
            counts = []
            for year, part in table.groupby(table.index.year):
                counts.append(dict(year=int(year), not_shrinking=int(part.within_high80_cash.eq(1).sum()),
                                   shrinking=int(part.within_high80_cash.eq(0).sum())))
            concentration.append(dict(target=target, horizon=h, by_year=counts,
                years_with_both_conditions=sum(c['not_shrinking']>0 and c['shrinking']>0 for c in counts)))
    check = dict(status='PASS', no2026_outcomes=True, independent_label_and_path_checks=label_checks,
        rank_checks=len(f), group_checks=len(r['groups']), independent_raw_hac_checks=raw_checks,
        joint_family_verified=380, fixed_entry_dates_verified=True,
        cash_condition_concentration=concentration,
        caveat='Arithmetic PASS is not statistical robustness. Cash condition is concentrated in two years; basket252 raw contrast confounds years, and year-controlled coefficient cannot be identified.')
    with (folder / '独立复核.json').open('x', encoding='utf-8') as out:
        json.dump(check, out, ensure_ascii=False, indent=2)
    print(json.dumps({k:v for k,v in check.items() if k != 'cash_condition_concentration'}, ensure_ascii=False))


def render_notes(folder):
    r = json.loads((folder / 'results.json').read_bytes())
    c = json.loads((folder / '独立复核.json').read_bytes())
    audit = json.loads((folder / 'audit.json').read_bytes())
    assert c['status'] == 'PASS'
    for name, digest in audit['files'].items():
        assert sha(folder / name) == digest
    f = pd.read_parquet(folder / 'features.parquet')
    # Descriptive summaries of the already frozen absolute buckets; no new
    # threshold/event/horizon search or new hypothesis tests are conducted.
    by_year = {}
    for low, high, name in ((0,.04,'below4'),(.04,.05,'4to5'),(.05,.06,'5to6'),(.06,np.inf,'above6')):
        sample = f[f.yield_rank.notna() & f.mean_yield.ge(low) & f.mean_yield.lt(high)]
        by_year[name] = {str(year): len(g) for year,g in sample.groupby(sample.index.year)}
    note_evidence = dict(status='DESCRIPTIVE_ONLY_NOT_NEW_TESTS', bucket_signal_year_counts=by_year,
                         independent_audit_sha256=sha(folder / '独立复核.json'),
                         results_sha256=sha(folder / 'results.json'))
    with (folder / '探索发现.json').open('x', encoding='utf-8') as out:
        json.dump(note_evidence, out, ensure_ascii=False, indent=2)
    def find(records, **keys):
        return next(x for x in records if all(x[k]==v for k,v in keys.items()))
    def pct(v):
        return '不足' if v is None else f'{100*v:.2f}%'
    lines = ['# 本轮发现：继续探索更具体的条件', '',
        '本轮已经检查60日、120日、半年126日、一年252日，并比较价格收益、含分红收益、持续高位和刚进入高位。结论应限定到具体条件，不能一句话概括成“股息率择时不行”。', '',
        '## 线索一：绝对股息率高位比宽泛的滚动高位更值得继续查', '',
        '预先固定的绝对股息率分桶出现明显差异。以下是区间内各日期之后的平均价格收益，T+1收盘起算，不含现金分红；不是每次买入的可实现策略收益。', '',
        '|期限|均值低于4%|均值4%—5%|均值5%—6%|5%—6%日期数/非重叠窗口数|',
        '|---|---:|---:|---:|---:|']
    for h in (60,120,126,252):
        gs = [find(r['buckets'], target='price', horizon=h, lower=a) for a in (0,.04,.05)]
        lines.append(f"|{h}交易日|{pct(gs[0].get('mean_return'))}|{pct(gs[1].get('mean_return'))}|{pct(gs[2].get('mean_return'))}|{gs[2]['days']}/{gs[2]['nonoverlap_n']}|")
    counts = by_year['5to6']
    lines += ['', '5%—6%样本的信号年份：' + '、'.join(f'{y}年{n}日' for y,n in counts.items()) + '。一年窗口只有2个按当前规则选出的非重叠观察，不能把180日算成180次独立成功。6%以上没有有效样本，不能推断更高更好。', '',
        '这支持继续研究“绝对估值补偿是否足够”，而不是只用过去三年的最高20%作为入场状态。但5%不是已经验证的通用门槛：利率环境、未来分红预期、样本银行变化都影响这个数值；下一轮应比较股息率与无风险收益的差额。', '',
        '## 线索二：高位内部，分红参考稳定比单纯高息更有信息', '',
        '|期限|分红参考未下降后的价格收益|下降后的价格收益|未下降/下降日期数|', '|---|---:|---:|---:|']
    for h in (60,120,126,252):
        a = find(r['groups'], target='price', horizon=h, rule='within_high80_cash', sample='all_observed', group='high')
        b = find(r['groups'], target='price', horizon=h, rule='within_high80_cash', sample='all_observed', group='other')
        lines.append(f"|{h}|{pct(a['mean_return'])}|{pct(b['mean_return'])}|{a['days']}/{b['days']}|")
    lines += ['', '半年价格模型在控制年份、月份、此前收益和波动后，条件差异仍为正，且通过本轮与历史轮次的联合校正。这是值得保留的线索。但下降条件只有153日，全部集中在2021和2022，半年下降样本只有2个非重叠窗口；尚不足以认定普遍有效。', '',
        '“分红未下降”是同批银行的已实施周期金额同比参考，不是未来现金流承诺，也不是直接按市场总额加权的行业分红增速。下一轮需要追到具体银行和具体分红周期，检查频率变化、单位与少数银行的贡献。', '',
        '## 线索三：更极端的相对高位，表现与最高20%不同', '',
        '|期限|历史最高10%价格收益|其他日期价格收益|', '|---|---:|---:|']
    for h in (60,120,126,252):
        a = find(r['groups'], target='price', horizon=h, rule='high90', group='high', sample='all_observed')
        b = find(r['groups'], target='price', horizon=h, rule='high90', group='other', sample='all_observed')
        lines.append(f"|{h}|{pct(a['mean_return'])}|{pct(b['mean_return'])}|")
    lines += ['', '控制模型的部分半年、一年结果显著，无条件模型经联合校正则不显著；不能只引用支持的一种。最高10%的刚入区事件为12次，一年平均价格收益2.88%，明显弱于持续处于区间时的日度统计。它可能适合作为估值状态，而不是一触发就买满。', '',
        '## 趋势与活跃度：可以继续研究，但目前定义不应直接拿来用', '',
        '本轮20日上涨且站上60日均线的趋势条件、20/60日换手率升温条件，没有在高位内部表现出稳定的新增优势。交易升温入区事件的一年均值为正，但只有13次；不据此放弃整个热度方向，也不据此宣称确认了买点。后续更合理的是区分低位温和回暖与涨后过热，并把进入高位时的估值深度、分红稳定性一起记录。', '',
        '## 一条很漂亮的结果为什么暂不能用', '',
        '一年含分红篮子在高位内部，分红参考未下降组均值很高、下降组很低，原始p值很小。但复核发现：下降组44个日期全部来自2022，未下降组178日来自2023和2024；没有任何一个信号年份同时出现两种条件。年份控制后模型无法独立识别该条件，所以这主要是年份混杂，不能证明分红条件预测了收益。已标记为异常优秀结果审计，不据其晋级策略。', '',
        '## 下一步研究顺序', '',
        '1. 优先验证“绝对股息率/利率补偿 + 分红稳定”，追踪构成分红下降的银行及现金金额，按市场年份和非重叠事件检查。',
        '2. 对每个完整高息事件保留起点、最低价格、最终收益和最长等待期，研究分批建仓的合理性，避免只计算最后收益。',
        '3. 再研究适度交易回暖与过热的区分，检验它能否改善入场等待和回撤，而非只追求提高平均收益。',
        '以上是下一轮待冻结的研究方向；本轮没有事后改参数、没有宣称通过样本外或已经完成仓位回测。', '',
        f"计算复核通过：{c['independent_label_and_path_checks']:,}个收益及路径标签、{c['rank_checks']:,}个历史排名、{c['group_checks']}组均值、{c['independent_raw_hac_checks']}个无条件HAC检验及{c['joint_family_verified']}项联合校正。", '',
        '[完整期限与条件报告](研究报告.md) · [对比图](期限与条件比较.png) · [结构化结果](results.json) · [独立复核](独立复核.json)']
    with (folder / '发现与复核.md').open('x', encoding='utf-8') as out:
        out.write('\n'.join(lines))
    print(json.dumps(dict(notes_created=True, bucket_signal_year_counts=by_year)))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('run_dir', type=Path)
    p.add_argument('--notes-only', action='store_true')
    args = p.parse_args()
    render_notes(args.run_dir.resolve()) if args.notes_only else verify(args.run_dir.resolve())
