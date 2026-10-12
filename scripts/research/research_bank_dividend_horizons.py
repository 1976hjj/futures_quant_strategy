"""Pre-registered exploration of bank dividend-cycle yields over longer horizons.

Features are frozen in a separate prepare process; all outcomes end in 2025.
This script reuses audited sources, adds no UI and changes no factor definitions.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from scripts.research.research_bank_dividend_rebound import (  # noqa: E402
    labels, read_daily, regression, sha, summary,
)
from scripts.research.research_bank_sector_timing import (  # noqa: E402
    bootstrap_contrast, holm_adjust, nonoverlap_positions, serial, write_json,
)

BASE = ROOT / 'reports/bank_sector_research/20261012-075242-dividend-rebound'
SECTOR = ROOT / 'reports/bank_sector_research/20261012-075203'
HORIZONS = (60, 120, 126, 252)
RULE_NAMES = {
    'high80': '股息率历史最高20%',
    'high90': '股息率历史最高10%',
    'high80_cash_not_shrinking': '高股息率且分红参考未下降',
    'high80_trend_recovery': '高股息率且价格趋势改善',
    'high80_activity_rising': '高股息率且交易升温',
    'within_high80_trend': '高位内部：趋势改善 vs 未改善',
    'within_high80_activity': '高位内部：交易升温 vs 未升温',
    'within_high80_cash': '高位内部：分红参考未下降 vs 下降',
}
RULES = tuple(RULE_NAMES)
TARGETS = ('price', 'basket_total')
DEPENDENCIES = ('scripts/research/research_bank_dividend_rebound.py',
                'scripts/research/research_bank_sector_timing.py')


def bounded_binary(condition, known):
    return condition.astype(float).where(known)


def add_features(base, prices, activity):
    f = base.copy()
    prices = prices.reindex(f.index)
    ma = prices.rolling(60, min_periods=60).mean()
    r20 = prices / prices.shift(20) - 1
    f['price_return20'] = r20
    f['price_ma60_ratio'] = prices / ma
    f['activity_ratio'] = activity.reindex(f.index)
    f['trend_recovery'] = bounded_binary(r20.gt(0) & prices.ge(ma), r20.notna() & ma.notna())
    f['activity_rising'] = bounded_binary(f.activity_ratio.gt(1), f.activity_ratio.notna())
    f['cash_nonshrinking'] = bounded_binary(f.cash_yoy252.ge(0), f.cash_yoy252.notna())
    for name, flag in (('trend', 'trend_recovery'), ('activity', 'activity_rising'),
                       ('cash', 'cash_nonshrinking')):
        known = f.high80.notna() & f[flag].notna()
        if name != 'cash':
            suffix = 'trend_recovery' if name == 'trend' else 'activity_rising'
            f['high80_' + suffix] = bounded_binary(f.high80.eq(1) & f[flag].eq(1), known)
        f['within_high80_' + name] = f[flag].where(known & f.high80.eq(1))
    return f


def entry_positions(signal):
    # Unknown -> high is not proof of a threshold crossing. Fixed spacing for
    # every horizon; outcomes must never decide which signal dates are chosen.
    crossing = signal.eq(1) & signal.shift(1).eq(0)
    return nonoverlap_positions(crossing.to_numpy(), 60)


def prepare():
    settings = json.loads((ROOT / 'config/bank_timing_data.json').read_bytes())
    end = settings['released_feature_end']
    if end > '2025-12-31':
        raise ValueError('2026 is chart-only, not released for these outcomes')
    bspec = json.loads((BASE / 'experiment_spec.json').read_bytes())
    sspec = json.loads((SECTOR / 'experiment_spec.json').read_bytes())
    baudit = json.loads((BASE / 'audit.json').read_bytes())
    saudit = json.loads((SECTOR / 'feature_manifest.json').read_bytes())
    for path, digest in ((BASE / 'features.parquet', baudit['files']['features.parquet']),
                         (SECTOR / 'features.parquet', saudit['files']['features.parquet'])):
        if sha(path) != digest:
            raise ValueError('Archived features changed')
    if bspec['yield_field'] != 'cash_yield_cycle' or bspec['end'] != end or sspec['end'] != end:
        raise ValueError('Sources must have corrected cycles and identical released ends')
    prior_paths = [ROOT / 'reports/bank_sector_research' / p / 'results.json' for p in (
        '20261011-215220', '20261012-072739-dividend-rebound',
        '20261012-075203', '20261012-075242-dividend-rebound')]
    prior = []
    for p in prior_paths:
        old = json.loads(p.read_bytes())
        count = len(old.get('tests', old.get('single_tests', []) + old.get('combinations', [])))
        prior.append(dict(path=str(p), sha256=sha(p), count=count))
    count = len(HORIZONS) * len(RULES) * len(TARGETS) * 2
    folder = ROOT / 'reports/bank_sector_research' / (
        datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S') + '-dividend-horizons')
    folder.mkdir(parents=True, exist_ok=False)
    source = Path(bspec['source_chart'])
    spec = dict(version='bank-dividend-horizon-exploration-1.0.0',
        created_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), end=end,
        status='EXPOSED_HISTORY_EXPLORATORY_NOT_OOS_NOT_TRADABLE',
        purpose='Explore horizon dependence and incremental trend/activity/cash conditions; no blanket rejection or best-horizon selection',
        horizons=list(HORIZONS), rules=RULE_NAMES, targets=list(TARGETS), models=['raw', 'controlled'],
        family_size=count, joint_family_size=count + sum(p['count'] for p in prior),
        prior_results=prior, source_chart=str(source),
        source_csv_sha256=bspec['source_sha256']['银行平均股息率与价格指数.csv'],
        base_feature_path=str(BASE / 'features.parquet'), base_feature_sha256=sha(BASE / 'features.parquet'),
        sector_feature_path=str(SECTOR / 'features.parquet'), sector_feature_sha256=sha(SECTOR / 'features.parquet'),
        basket_path=bspec['basket_path'], basket_sha256=bspec['basket_sha256'],
        label_policy='T+1 close to T+1+h close; reject unobserved tail and all broken segments',
        yield_policy='implemented annual dividend cycle 2.0.0; past756 midrank, minimum504, current excluded',
        trend_policy='prior20 price return>0 AND current index>=past/current60 arithmetic mean; available at T close',
        activity_policy='archived median_i(mean turnover20/mean turnover60)>1; no unknown->false; only2020-2025 covered',
        cash_policy='matched-bank annual-cycle reference cash同比252>=0; not a forecast',
        incremental_policy='within_high80_* compares flag1 vs0 only on high80 dates; missing flag stays missing',
        event_policy='known0->known1 crossings; fixed61-session spacing, chosen BEFORE reading labels; horizon252-complete anchors also compared',
        controls='year/month fixed effects, prior60 price return and volatility; not causal identification',
        inference='HAC Bartlett h+20; circular bootstrap max(60,h+20),2999 draws, fixedseed20261012; Holm all128 plus252 prior tests',
        comparable_samples='each horizon uses all observed windows, plus same eligible horizon252 dates for descriptive comparisons',
        chronology='2018-2021 vs2022-2025, windows must end within period; exposed diagnostics, not OOS',
        buckets='[0,4%),[4,5%),[5,6%),[6%,infinity); descriptive, no new cutpoint search',
        script_sha256=sha(__file__), dependencies={p: sha(ROOT / p) for p in DEPENDENCIES},
        constitution_sha256=sha(ROOT / 'RESEARCH_CONSTITUTION.md'),
        limits=bspec['limitations'] + ['120 and126 sessions nearly identical, not independent evidence',
            '252-day labels have fewer complete episodes/effective independent observations',
            'combinations vs all other dates are not incremental effects; within-high comparisons are separate',
            'positive terminal returns can coexist with large path losses; compare drawdown and worst entry return',
            'no evidence-based parameter changes during this round'])
    write_json(folder / 'experiment_spec.json', spec)
    f = pd.read_parquet(spec['base_feature_path'])
    sector = pd.read_parquet(spec['sector_feature_path']).set_index('session')
    if f.index.max() > pd.Timestamp(end) or sector.index.max() > pd.Timestamp(end):
        raise ValueError('Feature source beyond released domain')
    d = read_daily(source, end, ['session', 'price_index'])
    d.index = pd.DatetimeIndex(d.index)
    if not d.index.equals(f.index):
        raise ValueError('Feature calendar mismatch')
    f = add_features(f, d.price_index, sector['activity-turnover-ratio-20-60'])
    f.to_parquet(folder / 'features.parquet')
    write_json(folder / 'feature_manifest.json', dict(spec_sha256=sha(folder / 'experiment_spec.json'),
        feature_sha256=sha(folder / 'features.parquet'), forward_labels_computed=False,
        maximum_feature_date=str(f.index.max().date()), no2026_values=True,
        counts={r: dict(high=int(f[r].eq(1).sum()), other=int(f[r].eq(0).sum()),
                       missing=int(f[r].isna().sum())) for r in RULES}))
    print(json.dumps(dict(run_dir=str(folder), family_size=count, phase='PREPARED')))


def analyse(folder):
    spec = json.loads((folder / 'experiment_spec.json').read_bytes())
    fm = json.loads((folder / 'feature_manifest.json').read_bytes())
    for p, expected in [(folder / 'experiment_spec.json', fm['spec_sha256']),
                         (folder / 'features.parquet', fm['feature_sha256']),
                         (Path(__file__), spec['script_sha256']),
                         (Path(spec['basket_path']), spec['basket_sha256']),
                         *[(ROOT / p, s) for p, s in spec['dependencies'].items()]]:
        if sha(p) != expected:
            raise ValueError(f'Frozen dependency changed: {p}')
    if (folder / 'results.json').exists() or (folder / 'labels_price.parquet').exists():
        raise ValueError('Completed/partial results are immutable; prepare a new run')
    source = Path(spec['source_chart'])
    if sha(source / '银行平均股息率与价格指数.csv') != spec['source_csv_sha256']:
        raise ValueError('Archived price input changed')
    f = pd.read_parquet(folder / 'features.parquet')
    d = read_daily(source, spec['end'], ['session', 'price_index'])
    d.index = pd.DatetimeIndex(d.index)
    if not f.index.equals(d.index) or d.index.max() > pd.Timestamp(spec['end']):
        raise ValueError('Outcome calendar mismatch')
    basket = pd.read_parquet(spec['basket_path'])
    if pd.to_datetime(basket.session).max() > pd.Timestamp(spec['end']):
        raise ValueError('Basket beyond released end')
    basket = basket.set_index(pd.to_datetime(basket.session)).reindex(f.index)
    outcomes = dict(price=labels(d.price_index, f.index, horizons=spec['horizons']),
        basket_total=labels(basket.bank_equal_total_return_index, f.index, horizons=spec['horizons'],
                            segments=basket.basket_segment, ready=basket.status.eq('READY')))
    tests, groups, periods, events, buckets = [], [], [], [], []
    for target, lab in outcomes.items():
        lab.to_parquet(folder / f'labels_{target}.parquet', index=False)
        by_h = {h: lab[lab.horizon.eq(h)].set_index('session').reindex(f.index) for h in spec['horizons']}
        common = by_h[252]['return'].notna()
        for h, y in by_h.items():
            for ri, rule in enumerate(RULES):
                high, low = f[rule].eq(1), f[rule].eq(0)
                for sample, mask in (('all_observed', pd.Series(True, index=f.index)), ('year_complete', common)):
                    for name, flag in (('high', high), ('other', low)):
                        groups.append(dict(target=target, horizon=h, rule=rule, group=name, sample=sample,
                                           **summary(y, flag & mask, h)))
                boot = bootstrap_contrast(y['return'], high, low, max(60, h+20), 20261012+h+ri*100, 2999)
                for controlled in (False, True):
                    t = regression(f[rule], y['return'], f, h+20, controlled)
                    tests.append(dict(target=target, horizon=h, rule=rule,
                        model='controlled' if controlled else 'raw', **t,
                        bootstrap=None if controlled else boot))
                for start, end in (('2018-01-01', '2021-12-31'), ('2022-01-01', '2025-12-31')):
                    contained = f.index.to_series().between(start, end) & y.exit.le(pd.Timestamp(end))
                    periods.append(dict(target=target, horizon=h, rule=rule, period=start[:4]+'-'+end[:4],
                        high=summary(y, high & contained, h), other=summary(y, low & contained, h)))
                if not rule.startswith('within_'):
                    for i in entry_positions(f[rule]):
                        events.append(dict(target=target, rule=rule, session=f.index[i],
                            year_complete=bool(common.iloc[i]), **y.iloc[i].to_dict()))
            for a, b in ((0, .04), (.04, .05), (.05, .06), (.06, np.inf)):
                mask = f.yield_rank.notna() & f.mean_yield.ge(a) & f.mean_yield.lt(b)
                buckets.append(dict(target=target, horizon=h, lower=a, upper=b if np.isfinite(b) else None,
                                    **summary(y, mask, h)))
    if len(tests) != spec['family_size']:
        raise ValueError('Incomplete pre-registered test family')
    prior = []
    for item in spec['prior_results']:
        if sha(item['path']) != item['sha256']:
            raise ValueError('Prior results changed')
        old = json.loads(Path(item['path']).read_bytes())
        these = old.get('tests', old.get('single_tests', []) + old.get('combinations', []))
        if len(these) != item['count']:
            raise ValueError('Prior family changed')
        prior += these
    local = holm_adjust([t['p_value'] for t in tests])
    joint = holm_adjust([t['p_value'] for t in prior] + [t['p_value'] for t in tests])[len(prior):]
    for t, p, q in zip(tests, local, joint):
        t.update(holm_current=p, holm_joint=q)
    event_frame = pd.DataFrame(events)
    event_stats = []
    for (target, h, rule), e in event_frame.groupby(['target', 'horizon', 'rule'], sort=False):
        for sample in ('all_observed', 'year_complete'):
            good = e[e['return'].notna() & (e.year_complete if sample == 'year_complete' else True)]
            event_stats.append(dict(target=target, horizon=int(h), rule=rule, sample=sample,
                n=len(good), total_signal_events=len(e), mean_return=good['return'].mean(),
                win_rate=good['return'].gt(0).mean() if len(good) else None,
                mean_drawdown=good.drawdown.mean(), worst_return=good['return'].min(),
                mean_min_return=good.min_return.mean()))
    result = dict(status=spec['status'], spec_sha256=sha(folder / 'experiment_spec.json'),
        family_size=spec['family_size'], joint_family_size=spec['joint_family_size'],
        groups=groups, tests=tests, chronology=periods, events=event_stats, buckets=buckets,
        no2026_outcomes=True, latest_outcome_date=str(d.index.max().date()),
        source_limits=spec['limits'])
    write_json(folder / 'results.json', result)
    for name, records in (('group_statistics', groups), ('hypothesis_statistics', tests),
                          ('entry_episodes', events), ('entry_statistics', event_stats), ('yield_buckets', buckets)):
        pd.DataFrame([{k: v for k, v in row.items() if k != 'bootstrap'} for row in records]).to_csv(
            folder / (name + '.csv'), index=False, encoding='utf-8-sig')
    render(folder, result)
    write_json(folder / 'audit.json', dict(no2026_outcomes=True, frozen_dependencies_verified=True,
        max_outcome_date=str(d.index.max().date()), family_size=len(tests),
        files={p.name: sha(p) for p in folder.iterdir() if p.is_file()}))
    print(json.dumps(dict(run_dir=str(folder), phase='COMPLETE', family_size=len(tests))))


def find(records, **keys):
    return next(r for r in records if all(r[k] == v for k, v in keys.items()))


def render(folder, r):
    def pct(value):
        return '不足' if value is None or not np.isfinite(value) else f'{value*100:.2f}%'
    lines = ['# 银行股息率：期限与进场条件探索', '',
        '本轮探索60日、120日、半年126日、一年252日；沿用已实施一年分红周期口径。120与126高度接近，不是两份独立证据。所有收益截止2025年底，2026只开放描述图。', '',
        '没有因单一期限不显著而否定方向，也不把延长持有后自然增长的平均收益误当成择时优势。应同时比较其他日期、含分红收益、同一组日期及过程风险。', '',
        '## 高位条件在不同期限的表现', '',
        '|期限|高位价格收益|其他日期收益|均值差|高位上涨比例|高位平均最大回撤|高位日期/非重叠窗口数|HAC原始p|联合校正p|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for h in HORIZONS:
        a = find(r['groups'], target='price', horizon=h, rule='high80', group='high', sample='all_observed')
        b = find(r['groups'], target='price', horizon=h, rule='high80', group='other', sample='all_observed')
        t = find(r['tests'], target='price', horizon=h, rule='high80', model='raw')
        lines.append(f"|{h}交易日|{pct(a.get('mean_return'))}|{pct(b.get('mean_return'))}|{pct(t['coefficient'])}|{pct(a.get('win_rate'))}|{pct(a.get('mean_drawdown'))}|{a['days']}/{a['nonoverlap_n']}|{t['p_value']:.4f}|{t['holm_joint']:.4f}|")
        boot = t['bootstrap']
        lines.append(f"\n{h}日均值差的时间块95%区间：[{pct(boot['confidence_lower'])}, {pct(boot['confidence_upper'])}]，单位为百分点。\n")
    # Keep the table contiguous: confidence intervals belong below it.
    ci_lines = [line for line in lines if line.startswith('\n')]
    lines = [line for line in lines if not line.startswith('\n')] + ci_lines
    lines += ['', '## 固定同一批可观察满一年的信号日期', '',
        '避免一年窗口丢掉2025年晚期信号后，误把样本变化当成期限效应。下表各期限使用相同的可观察252日日期。', '',
        '|期限|高位价格收益|其他日期收益|高位/其他日期数|', '|---|---:|---:|---:|']
    for h in HORIZONS:
        a = find(r['groups'], target='price', horizon=h, rule='high80', group='high', sample='year_complete')
        b = find(r['groups'], target='price', horizon=h, rule='high80', group='other', sample='year_complete')
        lines.append(f"|{h}|{pct(a.get('mean_return'))}|{pct(b.get('mean_return'))}|{a['days']}/{b['days']}|")
    lines += ['', '## 刚进入高位，之后持有多久', '',
        '入区定义为前一天明确不在高位、当天进入高位；固定间隔61日选信号，再计算各期限。不是先看结果再删掉入场。不同事件的一年收益仍会重叠。', '',
        '|期限|完整事件数|平均价格收益|上涨比例|平均最大回撤|平均入场后最低收益|', '|---|---:|---:|---:|---:|---:|']
    for h in HORIZONS:
        e = find(r['events'], target='price', horizon=h, rule='high80', sample='all_observed')
        lines.append(f"|{h}|{e['n']}|{pct(e['mean_return'])}|{pct(e['win_rate'])}|{pct(e['mean_drawdown'])}|{pct(e['mean_min_return'])}|")
    lines += ['', '## 值得继续跟踪的条件：检验增量，而非只看组合收益', '',
        '趋势改善：20日价格收益为正且指数在60日均线上；交易升温：个股20/60日换手率比的板块中位数大于1；分红未下降：同批银行的一年周期金额同比不低于零。这些都是当时能观察的条件，分红参考不是未来承诺。', '',
        '下表只在高股息率区间内部比较，有助于区分股息率高位与趋势、热度的增量。换手率条件仅有2020—2025数据；不能直接把不同样本的系数排成优劣榜。控制模型使用年份、月份、前60日收益与波动。', '',
        '|高位内部条件|期限|条件成立/不成立日期数|价格收益均值差|控制后差异|联合校正p（控制）|', '|---|---:|---:|---:|---:|---:|']
    for rule in ('within_high80_trend', 'within_high80_activity', 'within_high80_cash'):
        for h in HORIZONS:
            t = find(r['tests'], target='price', horizon=h, rule=rule, model='raw')
            c = find(r['tests'], target='price', horizon=h, rule=rule, model='controlled')
            a = find(r['groups'], target='price', horizon=h, rule=rule, group='high', sample='all_observed')
            b = find(r['groups'], target='price', horizon=h, rule=rule, group='other', sample='all_observed')
            q = '不足' if c['holm_joint'] is None else f"{c['holm_joint']:.4f}"
            lines.append(f"|{RULE_NAMES[rule]}|{h}|{a['days']}/{b['days']}|{pct(t['coefficient'])}|{pct(c['coefficient'])}|{q}|")
    lines += ['', '## 含分红收益与年份稳定性', '',
        '下面使用另一条等权含分红银行篮子，仅2020—2025且断点窗口留空；不是399986官方价格指数加上一条固定股息率。', '',
        '|期限|高位含分红收益|其他日期收益|高位平均最大回撤|高位非重叠窗口数|', '|---|---:|---:|---:|---:|']
    for h in HORIZONS:
        a = find(r['groups'], target='basket_total', horizon=h, rule='high80', group='high', sample='all_observed')
        b = find(r['groups'], target='basket_total', horizon=h, rule='high80', group='other', sample='all_observed')
        lines.append(f"|{h}|{pct(a.get('mean_return'))}|{pct(b.get('mean_return'))}|{pct(a.get('mean_drawdown'))}|{a['nonoverlap_n']}|")
    lines += ['', '|历史子时期|期限|高位价格收益|其他日期收益|高位日期数|', '|---|---:|---:|---:|---:|']
    for c in r['chronology']:
        if c['rule'] == 'high80' and c['target'] == 'price':
            lines.append(f"|{c['period']}|{c['horizon']}|{pct(c['high'].get('mean_return'))}|{pct(c['other'].get('mean_return'))}|{c['high']['days']}|")
    lines += ['', '## 如何理解与后续方向', '',
        '应分别判断：高股息率是否提供中长期收益补偿、是否适合作为估值状态、是否能定位入场，以及是否需要趋势或活跃度确认。以上结论可以不同，不能用一个60日p值概括所有问题。', '',
        '若长期收益补偿较好、短期入区事件较差，更适合继续研究分批建仓和风险预算，而不是当天一次性买满；这是一条待验证的设计方向，不是本报告已经跑过的仓位策略。若热度或趋势仅某个期限较好，应先检查分年、同样本与独立事件，不能选出最好期限后忽略其他尝试。', '',
        f"本轮{r['family_size']}项检验连同前四轮合计{r['joint_family_size']}项联合校正。这里只探索已看过的历史，没有交易成本与真实成交，未做独立样本外验证。统计不显著不等于证明无效；显著也不等于可以实盘。", '',
        '所有条件、期限、窗口和失败结果保留于experiment_spec.json、results.json及CSV；没有根据本轮结果改阈值。', '',
        *['- ' + limit for limit in r['source_limits']], '',
        '![期限比较](期限与条件比较.png)']
    text = '\n'.join(lines)
    with (folder / '研究报告.md').open('x', encoding='utf-8') as out:
        out.write(text)
    (folder / '银行股息率期限探索.html').write_text('<!doctype html><meta charset="utf-8"><title>银行股息率期限探索</title><style>body{max-width:1200px;margin:32px auto;background:#f7f8f4;color:#24382e;font:16px/1.7 system-ui}pre{white-space:pre-wrap}img{width:100%}</style><pre>' + html.escape(text) + '</pre><img src="期限与条件比较.png">', encoding='utf-8')
    draw(folder, r)


def draw(folder, r):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    plt.rcParams['font.family'] = FontProperties(fname='C:/Windows/Fonts/msyh.ttc').get_name()
    plt.rcParams['axes.unicode_minus'] = False
    fig, axes = plt.subplots(2, 1, figsize=(14, 10))
    fig.patch.set_facecolor('#f7f8f4')
    colors = ['#166b56', '#bb7546']
    x = np.arange(len(HORIZONS))
    for i, group in enumerate(('high', 'other')):
        values = [find(r['groups'], target='price', horizon=h, rule='high80', group=group,
                       sample='all_observed')['mean_return'] * 100 for h in HORIZONS]
        bars = axes[0].bar(x+(i-.5)*.34, values, .34, color=colors[i],
                           label='股息率高位' if i == 0 else '其他日期')
        for bar in bars:
            value = bar.get_height()
            axes[0].text(bar.get_x()+bar.get_width()/2, value+.2 if value >= 0 else value-.2,
                         f'{value:.2f}%', ha='center', va='bottom' if value >= 0 else 'top', fontsize=10)
    axes[0].set_xticks(x, ['60日', '120日', '半年126日', '一年252日'])
    axes[0].set_ylabel('平均价格收益（%）')
    axes[0].legend(frameon=False)
    axes[0].margins(y=.25)
    axes[0].grid(axis='y', alpha=.15)
    axes[0].axhline(0, color='#889488', lw=.8)
    names = ('within_high80_trend', 'within_high80_activity', 'within_high80_cash')
    matrix = np.array([[find(r['tests'], target='price', horizon=h, rule=rule,
                            model='raw')['coefficient'] * 100 for h in HORIZONS] for rule in names])
    im = axes[1].imshow(matrix, cmap='RdYlGn', aspect='auto', vmin=-max(1, np.abs(matrix).max()),
                        vmax=max(1, np.abs(matrix).max()))
    axes[1].set_xticks(x, ['60日', '120日', '半年126日', '一年252日'])
    axes[1].set_yticks(range(3), ['高位内部：趋势改善', '高位内部：交易升温', '高位内部：分红未下降'])
    for row in range(3):
        for col in range(4):
            axes[1].text(col, row, f'{matrix[row,col]:+.2f}', ha='center', va='center', fontsize=12)
    fig.colorbar(im, ax=axes[1], label='条件成立与不成立的收益差（百分点）')
    axes[1].set_title('条件的增量：只在高股息率日期内部比较；样本覆盖不同', loc='left', fontsize=12)
    fig.suptitle('银行股息率：持有期限与入场条件探索', x=.09, ha='left', fontsize=21, fontweight='bold')
    fig.text(.09, .925, '已查看的2016—2025历史；T+1收盘起算；120与126日高度相关；未扣成本，未做独立样本外验证', fontsize=11)
    fig.text(.09, .04, '图中均值差是描述结果，不能单凭颜色判定有效。完整区间、检验校正、回撤及入区事件见报告。', fontsize=11)
    fig.subplots_adjust(left=.19, right=.95, top=.87, bottom=.10, hspace=.32)
    fig.savefig(folder / '期限与条件比较.png', dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('prepare')
    a = sub.add_parser('analyse')
    a.add_argument('run_dir', type=Path)
    args = p.parse_args()
    prepare() if args.command == 'prepare' else analyse(args.run_dir.resolve())
