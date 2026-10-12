"""Independent checks and a plain-language interpretation of frozen evidence."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from research_bank_dividend_rebound import sha


def verify(folder):
    spec=json.loads((folder/"experiment_spec.json").read_bytes())
    audit=json.loads((folder/"audit.json").read_bytes())
    for name,expected in audit["files"].items():
        if sha(folder/name)!=expected:
            raise ValueError(f"Evidence changed: {name}")
    r=json.loads((folder/"results.json").read_bytes())
    f=pd.read_parquet(folder/"features.parquet")
    lab=pd.read_parquet(folder/"labels_price.parquet")
    with duckdb.connect() as c:
        prices=c.execute("SELECT session,price_index FROM read_csv_auto(?) WHERE session<=CAST(? AS DATE) ORDER BY session",
            [str(Path(spec["source_chart"])/"银行平均股息率与价格指数.csv"),spec["end"]]).df()
    calendar=pd.DatetimeIndex(prices.session); values=prices.price_index.to_numpy()
    position={d:i for i,d in enumerate(calendar)}
    checked=0
    for row in lab.itertuples():
        i=position[row.session]; a,b=i+1,i+1+row.horizon
        if b>=len(values):
            assert row.status=="TAIL" and pd.isna(lab.loc[checked,"return"])
        else:
            actual=lab.loc[checked,"return"]
            np.testing.assert_allclose(actual,values[b]/values[a]-1,atol=1e-14)
            assert row.entry==calendar[a] and row.exit==calendar[b]
            assert row.exit<=pd.Timestamp(spec["end"])
        checked+=1
    # Independent direct binary-group formula for the actual primary effect/HAC.
    y=lab[lab.horizon.eq(60)].set_index("session")["return"].reindex(f.index).to_numpy()
    x=f.high80.to_numpy(); valid=np.isfinite(x)&np.isfinite(y)
    high=valid&(x==1); low=valid&(x==0)
    difference=y[high].mean()-y[low].mean()
    scores=np.zeros(len(y))
    scores[high]=(y[high]-y[high].mean())/high.sum()
    scores[low]=-(y[low]-y[low].mean())/low.sum()
    variance=np.sum(scores**2)
    for k in range(1,81):
        variance+=2*(1-k/81)*float(np.dot(scores[k:],scores[:-k]))
    se=math.sqrt(variance*valid.sum()/(valid.sum()-2))
    np.testing.assert_allclose([difference,se],[r["primary"]["coefficient"],r["primary"]["se"]],rtol=1e-10)
    np.testing.assert_allclose(math.erfc(abs(difference/se)/math.sqrt(2)),r["primary"]["p_value"],rtol=1e-10)
    covered=f.valid_banks.ge(10)&f.valid_banks.div(f.priced_banks).ge(.7)
    for i in (504,620,1000,len(f)-1):
        hist=f.mean_yield.where(covered).iloc[max(0,i-756):i].dropna()
        percentile=np.nan if not covered.iloc[i] or len(hist)<504 else (hist.lt(f.mean_yield.iloc[i]).sum()+hist.eq(f.mean_yield.iloc[i]).sum()/2)/len(hist)
        np.testing.assert_allclose(percentile,f.yield_rank.iloc[i],atol=1e-14)
    for g in r["groups"]:
        if g["target"]!="price" or g["rule"]!="high80":continue
        yy=lab[lab.horizon.eq(g["horizon"])].set_index("session")["return"].reindex(f.index)
        mask=f.high80.eq(1 if g["group"]=="high" else 0)&yy.notna()
        assert mask.sum()==g["days"]
        np.testing.assert_allclose([yy[mask].mean(),yy[mask].gt(0).mean()],[g["mean_return"],g["win_rate"]],atol=1e-14)
    events=pd.read_csv(folder/"entry_episodes.csv",parse_dates=["session"])
    e=events[events.target.eq("price")&events.horizon.eq(60)&events.rule.eq("high80")].copy()
    yframe=lab[lab.horizon.eq(60)].set_index("session").join(f)
    highdays=yframe[yframe.high80.eq(1)&yframe["return"].notna()]
    annual=[]
    for year,g in highdays.groupby(highdays.index.year):
        annual.append({"year":int(year),"days":len(g),"mean_return":float(g["return"].mean()),"win_rate":float(g["return"].gt(0).mean())})
    checks={"status":"PASS","all_source_output_hashes_verified":True,"maximum_outcome_date":str(calendar.max().date()),
        "no2026_outcomes":True,"independent_price_label_checks":checked,"primary_group_mean_hac_and_p_independently_checked":True,
        "past_only_rank_anchor_checks":4,"price_groups_independently_checked":6,
        "high80_first_entry60":{"events":len(e),"mean_return":float(e["return"].mean()),
            "win_rate":float(e["return"].gt(0).mean()),"worst_return":float(e["return"].min()),
            "average_min_return":float(e.min_return.mean()),"examples":e[["session","return","min_return"]].astype({"session":str}).to_dict("records")},
        "year_diagnostic":annual,"cash_yoy_negative_high_days":int(highdays.cash_yoy252.lt(0).sum()),
        "high_days":len(highdays),"mean_price_contribution20":float(highdays.price_contribution20.mean()),
        "mean_cash_contribution20":float(highdays.cash_contribution20.mean()),
        "note":"No new thresholds, models or hypothesis tests; summaries of pre-registered outputs only"}
    with (folder/"独立复核.json").open("x",encoding="utf-8") as stream:
        json.dump(checks,stream,ensure_ascii=False,indent=2,allow_nan=False)
    interpretation(folder,r,checks)
    print(json.dumps(checks,ensure_ascii=False))


def interpretation(folder,r,c):
    def pct(v):return f"{v*100:.2f}%"
    joint=r.get('joint_family_size',126)
    primary_joint=r['primary'].get('holm_joint',r['primary'].get('holm_joint126'))
    lines=["# 银行股息率反弹研究：周期口径修正与复核", "",
        "本轮使用按所属期间覆盖一年的分红周期口径，重新计算全部信号和收益组。以下区分高位区间内所有日期与刚进入高位的事件；二者不能等同为同一个建仓规则。", "",
        f"用2016年起的数据预热，信号从{r['first_signal']}开始，所有后续收益截至2025-12-31。2026此前仅用于描述图，本轮未计算2026收益标签。原始归档保留；正式个股与板块因子已另行发布新版本，旧研究保留。", "",
        "高位按过去756个交易日的最高20%定义，至少504天历史，当日不进入自身排名的历史基准。使用T日收盘信号、T+1收盘到T+1+h收盘的价格指数收益。所有窗口与规则在看结果之前冻结。", "",
        "| 后续期限 | 高位平均价格收益 | 其他日期平均收益 | 高位上涨比例 | 其他上涨比例 |",
        "|---|---:|---:|---:|---:|"]
    for h in (20,60,120):
        a=next(g for g in r["groups"] if (g["target"],g["horizon"],g["rule"],g["group"])==("price",h,"high80","high"))
        b=next(g for g in r["groups"] if (g["target"],g["horizon"],g["rule"],g["group"])==("price",h,"high80","other"))
        lines.append(f"| {h}交易日 | {pct(a['mean_return'])} | {pct(b['mean_return'])} | {pct(a['win_rate'])} | {pct(b['win_rate'])} |")
    p=r["primary"];bt=p["bootstrap"];first=c["high80_first_entry60"]
    lines += ["", "## 能支持什么", "",
        f"60日平均收益差为{p['coefficient']*100:.2f}个百分点，时间块重采样95%区间为[{bt['confidence_lower']*100:.2f}, {bt['confidence_upper']*100:.2f}]个百分点。原始p={p['p_value']:.4f}，本轮36项校正p={p['holm_current36']:.4f}；保留先前研究的{joint}项联合校正p={primary_joint:.4f}。", "",
        "控制模型包含年份、月份、此前60日收益与波动。即使条件模型显著，也不能视为独立可盈利验证；图形已经被查看，尚无严格样本外验证。", "",
        f"| 预设条件模型 | 高位条件效应（百分点） | {joint}项校正p |", "|---|---:|---:|"]
    for target,h,rule in (("price",20,"high80"),("price",60,"high80"),("price",120,"high80"),("basket_total",60,"high80")):
        t=next(t for t in r["tests"] if (t["target"],t["horizon"],t["rule"],t["model"])==(target,h,rule,"controlled"))
        q=t.get('holm_joint',t.get('holm_joint126'))
        lines.append(f"| {'价格指数' if target=='price' else '等权含分红篮子'} {h}日 | {t['coefficient']*100:.2f} | {q:.6f} |")
    state=next(g for g in r['groups'] if (g['target'],g['horizon'],g['rule'],g['group'])==('price',60,'high80','high'))
    lines += ["", "## 对实际建仓更关键的结果", "",
        f"刚进入最高20%区间的事件，按至少61个交易日间隔去除重叠，共{first['events']}次。未来60日平均价格收益{pct(first['mean_return'])}，上涨比例{pct(first['win_rate'])}。高位区间内所有日期的对应结果为{pct(state['mean_return'])}、{pct(state['win_rate'])}。", "",
        "高位尚不能等同于立即买入触发器。持久高位可能经历较长下跌，日度统计包含后期反弹前夕的日期。事件数少，胜率仍有很大不确定性。", "",
        "| 高位首次进入日期（非重叠） | 后续60日价格收益 | 期间最差相对入场收益 |", "|---|---:|---:|"]
    for e in first["examples"]:
        lines.append(f"| {e['session'][:10]} | {pct(e['return'])} | {pct(e['min_return'])} |")
    lines += ["", "所有预设事件均列出，包含失败案例，没有只挑成功案例。", "",
        "## 高位也会失效", "", "| 信号年份 | 高位有效日期数 | 后续60日平均价格收益 | 上涨比例 |", "|---|---:|---:|---:|"]
    for a in c["year_diagnostic"]:
        lines.append(f"| {a['year']} | {a['days']} | {pct(a['mean_return'])} | {pct(a['win_rate'])} |")
    lines += ["", "年份为信号发生年，窗口可跨到下一年，但不会超过2025年底；前后子时期对比另已要求收益窗口完全结束在子时期内。2022年是重要反例，不能用全期平均掩盖。", "",
        "## 分红变化如何区分", "",
        f"在{c['high_days']}个具有完整60日收益的高位日中，{c['cash_yoy_negative_high_days']}日的同批银行一年分红周期参考同比减少。20日变化按同一批银行分解，对送转股统一每股单位，恒等式已复核。", "",
        "新版不会因为旧分红超过365天而自动删掉它。一年一次取一个年度，两次按中期与末期覆盖一年。首次增加中期分红先保留最后完整年度参考；缺失不等同于零。参考金额下降仍不能单凭数值认定未来公司减少分红。", "",
        "含分红篮子仅覆盖2020–2025且有真实断点，与官方价格指数的权重和有效样本不同，不能直接混比。", "",
        "## 如何推进", "",
        "下一步可以研究‘高股息率条件 + 当时可观察的止跌/趋势改善’，并检查分红预期、利率与信用风险。组合要另行冻结规格并保留搜索记录，不能从这些事件中倒推最佳买点。", "",
        f"高位占本轮60日有效信号的{state['days']/p['n']*100:.1f}%，不是全样本只有20%。滚动历史排名与全样本分组不同。绝对股息率还受分红政策、利率环境与银行样本变化影响。", "",
        "来源与限制：bank_token股价与分红归档、alpha_research历史行业、东方财富399986价格指数；供应商快照未完成全部披露修订认证。当前42家归档子集、股息率等权与指数权重不同、配股等非送转行为及未核验的取消分红均存在误差风险。收益未扣手续费、税、滑点，没有真实成交或仓位模拟。", "",
        "## 复核", "",
        f"独立复算{c['independent_price_label_checks']}个价格收益标签、主检验均值差/HAC标准误/p值、历史排名锚点和各收益组；检查所有文件哈希。研究代码的5项检查覆盖未来数据隔离、T+1、尾部、断点、送转股分解与日历相关性。", "",
        "统计标准误方法参考：[Newey–West/HAC官方说明](https://www.statsmodels.org/dev/generated/statsmodels.stats.sandwich_covariance.cov_hac.html)。这是渐近近似，事件数少时不能只信一个p值。", "",
        "![银行股息率反弹研究](股息率高位反弹研究.png)"]
    with (folder/"研究结论与复核.md").open("x",encoding="utf-8") as stream:
        stream.write("\n".join(lines))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("run_dir",type=Path)
    verify(parser.parse_args().run_dir.resolve())
