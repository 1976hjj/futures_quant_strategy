"""Read-only historical mean-PB chart with independent aggregation checks.

This diagnostic retains suspicious provider inputs. It does not publish a
corrected factor, read unreleased history, or change an existing experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts.bank_factor_inputs import daily_pb_inputs  # noqa: E402

MEMBER = """EXISTS (SELECT 1 FROM research.sw_industry_membership i
    WHERE i.ts_code=d.ts_code AND i.l1_name='银行' AND i.in_date<=d.trade_date
    AND (i.out_date IS NULL OR d.trade_date<i.out_date))"""
JIANGYIN = "https://static.cninfo.com.cn/finalpage/2016-09-01/1202667165.PDF"
QRCB = "https://disc.static.szse.cn/disc/disk02/finalpage/2021-08-28/cba12632-80ef-4748-a1b3-92b4e8205e62.PDF"


def mean_pb(frame):
    frame = frame.copy()
    frame["session"] = pd.to_datetime(frame.session)
    if frame.duplicated(["session", "instrument_id"]).any():
        raise ValueError("duplicate bank/date keys")
    frame["pb_daily"] = frame.pb_daily.where(np.isfinite(frame.pb_daily) & (frame.pb_daily > 0))
    return frame.groupby("session").agg(
        mean_pb=("pb_daily", "mean"),
        median_pb=("pb_daily", "median"),
        valid_banks=("pb_daily", "count"),
        pb_sum=("pb_daily", "sum"),
    )


def draw(daily, folder):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    plt.rcParams["font.family"] = FontProperties(fname="C:/Windows/Fonts/msyh.ttc").get_name()
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.size"] = 11
    fig, axes = plt.subplots(3, 1, figsize=(15, 11.8), gridspec_kw={"height_ratios": [3, 3, 1.4]})
    fig.patch.set_facecolor("#f7f8f4")
    fig.suptitle("银行板块平均 PB｜完整可用历史", x=0.07, ha="left", fontsize=23, fontweight="bold", y=0.978)
    fig.text(
        0.07,
        0.937,
        f"{daily.index[0]:%Y-%m-%d} — {daily.index[-1]:%Y-%m-%d}  ·  每日有效银行 PB 等权平均"
        "  ·  原始归档曲线，含待核验异常",
        color="#54665e",
        fontsize=12,
    )
    recent = daily.loc[daily.index >= "2010-01-01"]
    for ax, data, title, years in [
        (axes[0], daily, "全部历史｜早期样本很少，不能代表当前板块", 5),
        (axes[1], recent, "2010 年以来放大｜虚线中位数辅助观察极端值影响", 2),
    ]:
        ax.plot(data.index, data.mean_pb, color="#126955", lw=1.35, label="PB 算术平均值")
        ax.plot(data.index, data.median_pb, color="#829d93", lw=1, ls="--", alpha=0.85, label="PB 中位数（参考）")
        ax.axhline(1, color="#a8a59b", lw=0.8, ls=":")
        ax.set_title(title, loc="left", pad=11, fontsize=13, fontweight="bold")
        ax.set_ylabel("PB（倍）")
        ax.set_ylim(0, data[["mean_pb", "median_pb"]].max().max() * 1.12)
        ax.xaxis.set_major_locator(mdates.YearLocator(years))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax.set_xlim(data.index[0], data.index[-1] + pd.Timedelta(days=220))
        ax.legend(loc="upper right", frameon=False, fontsize=10)
    peak = pd.Timestamp("2016-09-20")
    axes[1].annotate(
        "江阴银行归档 PB 异常抬高均值\n此处保留原数，尚未修正",
        xy=(peak, daily.loc[peak, "mean_pb"]),
        xytext=(pd.Timestamp("2018-06-01"), 2.45),
        fontsize=10,
        color="#9a4e36",
        arrowprops={"arrowstyle": "->", "color": "#b66d4f"},
    )
    qday = pd.Timestamp("2022-01-20")
    axes[1].annotate(
        "青农商行 PB 也发现疑点",
        xy=(qday, daily.loc[qday, "mean_pb"]),
        xytext=(pd.Timestamp("2020-10-01"), 1.5),
        fontsize=10,
        color="#9a4e36",
        arrowprops={"arrowstyle": "->", "color": "#b66d4f"},
    )
    latest = daily.iloc[-1]
    axes[1].scatter(daily.index[-1], latest.mean_pb, color="#126955", s=25, zorder=5)
    axes[1].annotate(
        f"末日 {latest.mean_pb:.4f} 倍\n{int(latest.valid_banks)} 家 · {daily.index[-1]:%Y-%m-%d}",
        xy=(daily.index[-1], latest.mean_pb),
        xytext=(pd.Timestamp("2023-10-01"), 0.26),
        fontsize=10,
        color="#126955",
        arrowprops={"arrowstyle": "-", "color": "#126955"},
    )
    axes[2].plot(daily.index, daily.valid_banks, color="#42665c", lw=1.2)
    axes[2].fill_between(daily.index, 0, daily.valid_banks, color="#c7ddd3", alpha=0.6)
    axes[2].set_ylabel("有效银行数")
    axes[2].set_ylim(0, 46)
    axes[2].set_yticks([1, 16, 30, 42])
    axes[2].set_xlim(axes[0].get_xlim())
    axes[2].xaxis.set_major_locator(mdates.YearLocator(5))
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axes[2].set_title("当日实际参与计算的银行数量｜缺失不填零，不用今天 42 家倒算历史", loc="left", fontsize=12, pad=9)
    for ax in axes:
        ax.set_facecolor("white")
        ax.grid(axis="y", color="#dde4dd", linewidth=0.65)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#c1ccc4")
    fig.text(
        0.07, 0.032, "口径：平均 PB = 当日有效正 PB 之和 ÷ 有效银行数；非市值加权，非 PB 历史分位均值。", fontsize=11
    )
    fig.text(
        0.07,
        0.011,
        "来源：本项目归档 daily_basic；按历史行业区间筛选。早期财务版本、行业完整性及异常值尚未全面认证。",
        fontsize=10,
        color="#64746c",
    )
    fig.subplots_adjust(left=0.07, right=0.965, top=0.89, bottom=0.085, hspace=0.39)
    for extension in ["png", "svg"]:
        fig.savefig(folder / f"银行板块平均PB_全部历史.{extension}", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--clean-known-anomalies", type=Path, help="prior chart directory with the frozen exclusion list"
    )
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end")
    parser.add_argument("--pb-view-authorization", type=Path)
    parser.add_argument("--overlay-price-data", type=Path, help="bounded Eastmoney 399986 price-index response")
    parser.add_argument("--source-clean-chart", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.overlay_price_data:
        if args.source_clean_chart is None:
            parser.error("--overlay-price-data requires --source-clean-chart")
        overlay_price_chart(root, args.source_clean_chart, args.overlay_price_data)
        return
    if args.clean_known_anomalies:
        clean_chart(root, args.clean_known_anomalies, args.start, args.end, args.pb_view_authorization)
        return
    config = json.loads((root / "config/bank_timing_data.json").read_bytes())
    end = pd.Timestamp(config["released_feature_end"]).date()
    with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as c:
        first = c.execute(
            f"SELECT min(trade_date) FROM research.daily_basic d WHERE d.trade_date<=? AND {MEMBER}", [end]
        ).fetchone()[0]
        if first is None:
            raise ValueError("no bounded bank data")
        sql = (
            c.execute(
                f"""SELECT trade_date AS session, count(*) AS valid_banks,
            sum(pb) AS pb_sum,avg(pb) AS mean_pb FROM research.daily_basic d
            WHERE trade_date BETWEEN ? AND ? AND pb>0 AND isfinite(pb) AND {MEMBER}
            GROUP BY trade_date ORDER BY trade_date""",
                [first, end],
            )
            .df()
            .set_index("session")
        )
        names = c.execute(
            "SELECT ts_code AS instrument_id,name AS current_name_label FROM research.security_master"
        ).df()
        calendar = c.execute(
            "SELECT DISTINCT cal_date AS session FROM research.trading_calendar "
            "WHERE is_open AND cal_date BETWEEN ? AND ? ORDER BY session",
            [first, end],
        ).df()
        expected = (
            c.execute(
                """WITH days AS (SELECT DISTINCT cal_date AS session FROM research.trading_calendar
            WHERE is_open AND cal_date BETWEEN ? AND ?)
            SELECT d.session, count(DISTINCT i.ts_code) AS recorded_listed_bank_count
            FROM days d LEFT JOIN research.sw_industry_membership i ON i.l1_name='银行'
                AND i.in_date<=d.session AND (i.out_date IS NULL OR d.session<i.out_date)
            LEFT JOIN research.security_master s ON i.ts_code=s.ts_code
            WHERE s.list_date<=d.session AND (s.delist_date IS NULL OR d.session<s.delist_date)
            GROUP BY d.session ORDER BY d.session""",
                [first, end],
            )
            .df()
            .set_index("session")
        )
    raw = daily_pb_inputs(root, first, end)
    raw["session"] = pd.to_datetime(raw.session)
    daily = mean_pb(raw)
    if not daily.index.equals(sql.index) or not daily.valid_banks.equals(
        sql.valid_banks.astype(daily.valid_banks.dtype)
    ):
        raise ValueError("SQL and pandas bank counts disagree")
    error = float((daily.mean_pb - sql.mean_pb).abs().max())
    if error > 1e-12:
        raise ValueError("SQL and pandas daily mean disagree")
    daily = daily.reindex(pd.DatetimeIndex(calendar.session)).join(expected)
    daily["coverage_of_recorded_listed_banks"] = daily.valid_banks / daily.recorded_listed_bank_count
    if (daily.valid_banks > daily.recorded_listed_bank_count).any():
        raise ValueError("PB contains a bank outside recorded listed membership")
    raw = raw.merge(names, on="instrument_id", how="left", validate="many_to_one")
    checks, components = [], []
    for date in [
        first.isoformat(),
        "2008-10-28",
        "2016-09-20",
        "2020-03-23",
        "2022-01-20",
        "2024-09-11",
        end.isoformat(),
    ]:
        day = pd.Timestamp(date)
        group = raw.loc[(raw.session == day) & raw.pb_daily.notna()].sort_values("instrument_id")
        if group.empty:
            raise ValueError(f"spot check has no data: {date}")
        exact_sum = sum((Decimal(str(x)) for x in group.pb_daily), Decimal(0))
        exact_mean = exact_sum / len(group)
        if abs(float(exact_mean) - daily.loc[day, "mean_pb"]) > 1e-12:
            raise ValueError("Decimal spot mean mismatch")
        checks.append(
            dict(
                session=date,
                valid_banks=len(group),
                pb_sum=str(exact_sum),
                decimal_mean=str(exact_mean),
                mean_pb=float(exact_mean),
                status="AGGREGATION_MATCH",
            )
        )
        components.append(group)
    run = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-pb-chart"
    folder = root / "reports/bank_sector_research" / run
    folder.mkdir(exist_ok=False)
    raw.to_parquet(folder / "pb_source.parquet", index=False)
    daily.to_csv(folder / "板块PB每日均值.csv", encoding="utf-8-sig", index_label="session")
    pd.concat(components).to_csv(folder / "抽查日期个股明细.csv", encoding="utf-8-sig", index=False)
    flags = raw.loc[
        ((raw.instrument_id == "002807.SZ") & (raw.session.dt.year == 2016) & (raw.pb_daily > 8))
        | ((raw.instrument_id == "002958.SZ") & (raw.session.dt.year.isin([2021, 2022])) & (raw.pb_daily > 4))
    ].copy()
    flags["implied_bvps"] = flags.close / flags.pb_daily
    flags.to_csv(folder / "待核验PB异常明细.csv", encoding="utf-8-sig", index=False)
    table = "\n".join(
        f"| {r['session']} | {r['valid_banks']} | {r['pb_sum']} | {r['mean_pb']:.8f} | 一致 |" for r in checks
    )
    report = f"""# 银行板块平均 PB 图与抽查

日期：{daily.index[0]:%Y-%m-%d} 至 {daily.index[-1]:%Y-%m-%d}，{len(daily):,} 个交易日；本轮不读取未开放的 2026 年数据。

平均 PB = 当日历史银行范围内、有限且正的 PB 之和 / 有效银行数量。
各银行等权，不限质量池，也不设504日预热；未上市银行不参加，缺失不补零。
中位数仅作异常敏感度参照，未用于替换均值。无有效日PB时该银行当日不参加，详见有效数和覆盖率。

这与研究报告里的“各银行 PB 历史分位的平均值”不同，也不是行业总市值 / 总净资产。

## 抽查结果

全量 {len(sql):,} 个有值日期：独立 SQL 与 pandas 均值一致，最大绝对差 {error:.3g}；逐日有效银行数一致。
下表另以十进制逐项加总核对，个股、PB及来源编号保存在明细文件。

| 日期 | 银行数 | PB之和 | 算术平均PB（倍） | 核对 |
|---|---:|---:|---:|---|
{table}

## 原始 PB 发现疑点，不能把整条曲线标为已认证

- 江阴银行：2016-09-20 归档收盘价16.41元、PB31.1366倍，隐含每股净资产仅0.527元。
  [2016-09-01上市公告书]({JIANGYIN})披露2016年6月底每股净资产4.85元。
  IPO使股本和净资产变化，不能直接用16.41/4.85作为精确修复值，但不能支持31倍这一量级。
  需要沿当时财务、发行净募资和股份变化重建分母，确认供应商异常原因。
- 青农商行：2022-01-20 收盘价3.97元、PB9.1247倍，隐含每股净资产约0.435元。
  [2021年半年报]({QRCB})披露普通股每股净资产5.00元。
  同样只用于说明数量级严重不一致；半年报不是该日最新季报，尚未据此生成修复PB。
- 异常归档原值保留并标注，没有猜测除以10或裁剪异常。
  上面的“核对一致”只证明聚合无误，不能证明每条供应商PB正确。
  已有PB历史分位研究也应检查这些输入的影响，再决定是否重算。

## 使用边界

早期样本只有1家，随新银行上市而扩大至42家；长期曲线变化同时包含估值变化和样本构成变化。行业成员区间来自现有归档，完整历史成员及早期财务修订版本尚未全面认证，不能声称无生存偏差。

均值源自本项目已归档 `research.daily_basic.pb`。[当前供应商公式说明](https://tushare.pro/document/2?doc_id=32)为总市值/(净资产-其他权益工具)；该说明不能替代逐条历史分母认证。

原始归档、既有因子、旧报告及前后端均未改动。此图为原始数据诊断，不新增择时阈值或收益检验。
"""
    (folder / "计算抽查说明.md").write_text(report, encoding="utf-8")
    draw(daily, folder)
    audit = dict(
        start=str(daily.index[0].date()),
        end=end.isoformat(),
        days=len(daily),
        source_rows=len(raw),
        source_banks=int(raw.instrument_id.nunique()),
        sql_aggregation_max_error=error,
        decimal_spot_checks=checks,
        flagged_rows=len(flags),
        status="AGGREGATION_VERIFIED_SOURCE_PB_ISSUES_FOUND",
        latest_mean_pb=float(daily.iloc[-1].mean_pb),
        latest_banks=int(daily.iloc[-1].valid_banks),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        files={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()},
    )
    (folder / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "folder": str(folder),
                **{
                    k: audit[k]
                    for k in [
                        "start",
                        "end",
                        "days",
                        "source_rows",
                        "sql_aggregation_max_error",
                        "flagged_rows",
                        "latest_mean_pb",
                        "latest_banks",
                        "status",
                    ]
                },
            },
            ensure_ascii=False,
        )
    )


def clean_chart(root, prior, start, requested_end, authorization_path=None):
    """Exclude only frozen bank/date/value keys; never infer a replacement PB."""
    config = json.loads((root / "config/bank_timing_data.json").read_bytes())
    released_end = pd.Timestamp(config["released_feature_end"])
    end = pd.Timestamp(requested_end or released_end)
    start = pd.Timestamp(start)
    if start > end:
        raise ValueError("start after end")
    prior = prior.resolve()
    prior_audit = json.loads((prior / "audit.json").read_bytes())
    for name in ["pb_source.parquet", "待核验PB异常明细.csv"]:
        if hashlib.sha256((prior / name).read_bytes()).hexdigest() != prior_audit["files"][name]:
            raise ValueError("prior chart source or exclusion list changed")
    raw = pd.read_parquet(prior / "pb_source.parquet")
    raw["session"] = pd.to_datetime(raw.session)
    raw = raw.loc[raw.session.between(start, min(end, released_end))].copy()
    authorization = None
    if end > released_end:
        if authorization_path is None:
            raise ValueError("unreleased PB view requires explicit scoped owner authorization")
        authorization = json.loads(authorization_path.read_bytes())
        if not (
            authorization.get("owner_confirmed") is True
            and authorization.get("scope") == "BANK_PB_DESCRIPTIVE_CHART_ONLY"
            and authorization.get("no_forward_returns") is True
            and pd.Timestamp(authorization["start"]) <= released_end + pd.Timedelta(days=1)
            and pd.Timestamp(authorization["end"]) >= end
        ):
            raise ValueError("PB view authorization does not cover this request")
        from alpha_research_os.kernel.holdout import ExposureLedger

        ledger = ExposureLedger(root / "data/metadata/bank_pb_view_exposures.ndjson")
        exposure = ledger.append(
            actor="project-owner-authorized-assistant",
            vintage=f"bank-pb:{released_end + pd.Timedelta(days=1):%Y-%m-%d}:{end:%Y-%m-%d}",
            purpose="Owner-approved descriptive bank mean PB chart; no returns, labels or tuning",
            capability_id="owner-authorization:sha256:" + hashlib.sha256(authorization_path.read_bytes()).hexdigest(),
        )
        authorization["exposure_event_id"] = exposure.event_id
        with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as c:
            extension = c.execute(
                f"""SELECT d.trade_date AS session,d.ts_code AS instrument_id,
                d.pb AS pb_daily,d.source_snapshot_id,d.source_payload_artifact_id
                FROM research.daily_basic d WHERE d.trade_date BETWEEN ? AND ? AND {MEMBER}
                ORDER BY d.trade_date,d.ts_code""",
                [(released_end + pd.Timedelta(days=1)).date(), end.date()],
            ).df()
        extension["session"] = pd.to_datetime(extension.session)
        if extension[["source_snapshot_id", "source_payload_artifact_id"]].isna().any().any():
            raise ValueError("PB extension lacks source lineage")
        raw = pd.concat([raw, extension], ignore_index=True)
    excluded = pd.read_csv(prior / "待核验PB异常明细.csv")
    excluded["session"] = pd.to_datetime(excluded.session)
    excluded = excluded.loc[excluded.session.between(start, end)]
    keyed = raw.merge(
        excluded[["session", "instrument_id", "pb_daily"]].rename(columns={"pb_daily": "excluded_pb"}),
        on=["session", "instrument_id"],
        how="left",
        validate="one_to_one",
    )
    mask = keyed.excluded_pb.notna()
    if mask.sum() != len(excluded) or not np.allclose(
        keyed.loc[mask, "pb_daily"], keyed.loc[mask, "excluded_pb"], rtol=0, atol=1e-12
    ):
        raise ValueError("frozen exclusion list does not match current inputs")
    kept = keyed.loc[~mask].drop(columns="excluded_pb")
    before, daily = mean_pb(raw), mean_pb(kept)
    if not before.index.equals(daily.index) or not ((before.valid_banks - daily.valid_banks).sum() == len(excluded)):
        raise ValueError("exclusion changed dates or unexpected sample counts")
    exact = kept.groupby("session").pb_daily.apply(
        lambda s: float(
            sum((Decimal(str(v)) for v in s.dropna() if np.isfinite(v) and v > 0), Decimal(0))
            / sum(np.isfinite(s) & (s > 0))
        )
    )
    max_error = float((exact - daily.mean_pb).abs().max())
    if max_error > 1e-12:
        raise ValueError("cleaned means disagree with Decimal sum/count")
    run = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-pb-clean-chart"
    folder = root / "reports/bank_sector_research" / run
    folder.mkdir(exist_ok=False)
    kept.to_parquet(folder / "clean_pb_inputs.parquet", index=False)
    excluded.to_csv(folder / "剔除记录.csv", encoding="utf-8-sig", index=False)
    comparison = daily.join(before[["mean_pb", "valid_banks"]].add_prefix("original_"))
    comparison["excluded_count"] = comparison.original_valid_banks - comparison.valid_banks
    comparison.to_csv(folder / "每日均值与剔除前对照.csv", encoding="utf-8-sig", index_label="session")
    draw_clean(daily, folder, len(excluded))
    latest = daily.iloc[-1]
    checks = {
        "exclusion_rows": len(excluded),
        "unchanged_dates": True,
        "decimal_aggregation_max_error": max_error,
        "start": daily.index[0].strftime("%Y-%m-%d"),
        "end": daily.index[-1].strftime("%Y-%m-%d"),
        "days": len(daily),
        "latest_mean_pb": float(latest.mean_pb),
        "latest_valid_banks": int(latest.valid_banks),
        "source_chart": str(prior),
        "authorization": authorization,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()},
    }
    (folder / "audit.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "folder": str(folder),
                **{
                    k: checks[k]
                    for k in [
                        "exclusion_rows",
                        "start",
                        "end",
                        "days",
                        "latest_mean_pb",
                        "latest_valid_banks",
                        "decimal_aggregation_max_error",
                    ]
                },
            },
            ensure_ascii=False,
        )
    )


def overlay_price_chart(root, clean_folder, price_file):
    """Overlay a published price index, with no return labels or fitted scales."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    clean_folder = clean_folder.resolve()
    audit = json.loads((clean_folder / "audit.json").read_bytes())
    daily_file = clean_folder / "每日均值与剔除前对照.csv"
    if hashlib.sha256(daily_file.read_bytes()).hexdigest() != audit["files"][daily_file.name]:
        raise ValueError("cleaned mean PB source changed")
    daily = pd.read_csv(daily_file, parse_dates=["session"]).set_index("session")
    settings = json.loads((root / "config/bank_timing_data.json").read_bytes())
    if daily.index.max() > pd.Timestamp(settings["released_feature_end"]):
        raise ValueError("overlay request extends beyond released price data")
    source = json.loads(price_file.read_bytes())
    params, payload = source["parameters"], source["response"]["data"]
    if params["secid"] != "0.399986" or params["fqt"] != "0" or payload["code"] != "399986":
        raise ValueError("requires unadjusted CSI bank price index 399986")
    values = pd.DataFrame([row.split(",") for row in payload["klines"]])
    prices = pd.DataFrame(
        {
            "session": pd.to_datetime(values[0]),
            "bank_price_index": pd.to_numeric(values[2]),
            "high": pd.to_numeric(values[3]),
            "low": pd.to_numeric(values[4]),
        }
    ).set_index("session")
    if prices.index.duplicated().any() or (prices.bank_price_index <= 0).any():
        raise ValueError("invalid or duplicate price-index rows")
    if prices.index.min() < daily.index.min() or prices.index.max() > daily.index.max():
        raise ValueError("index response outside requested chart dates")
    if not prices.index.equals(daily.index):
        raise ValueError("PB and price-index dates do not align completely")
    if not prices.bank_price_index.between(prices.low, prices.high).all():
        raise ValueError("price-index close outside daily range")
    combined = daily.join(prices[["bank_price_index"]], validate="one_to_one")
    run = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-pb-price-overlay"
    folder = root / "reports/bank_sector_research" / run
    folder.mkdir(exist_ok=False)
    (folder / "price_index_source.json").write_bytes(price_file.read_bytes())
    combined.to_csv(folder / "银行PB与价格指数.csv", encoding="utf-8-sig", index_label="session")
    plt.rcParams["font.family"] = FontProperties(fname="C:/Windows/Fonts/msyh.ttc").get_name()
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(2, 1, figsize=(14, 8.5), sharex=True, gridspec_kw={"height_ratios": [4.3, 1]})
    fig.patch.set_facecolor("#f7f8f4")
    fig.suptitle("银行板块平均 PB × 银行价格指数", x=0.075, ha="left", y=0.967, fontsize=22, fontweight="bold")
    fig.text(
        0.075,
        0.915,
        f"{combined.index[0]:%Y-%m-%d} — {combined.index[-1]:%Y-%m-%d}"
        f"  ·  PB 已剔除 {audit['exclusion_rows']} 条异常  ·  价格指数：中证银行 399986",
        fontsize=12,
        color="#56695f",
    )
    left, right = axes[0], axes[0].twinx()
    (a,) = left.plot(combined.index, combined.mean_pb, color="#126d55", lw=1.45, label="银行平均 PB（左轴，倍）")
    (b,) = right.plot(
        combined.index, combined.bank_price_index, color="#bb7142", lw=1.35, label="中证银行价格指数（右轴，点）"
    )
    left.set_ylabel("平均 PB（倍）", color="#126d55", fontsize=12)
    right.set_ylabel("中证银行价格指数（点）", color="#bb7142", fontsize=12)
    left.tick_params(axis="y", colors="#126d55")
    right.tick_params(axis="y", colors="#bb7142")
    left.set_ylim(0.35, max(1.9, combined.mean_pb.max() * 1.17))
    # Axes retain their native units; there is no fitted rescaling or normalization.
    span = combined.bank_price_index.max() - combined.bank_price_index.min()
    right.set_ylim(combined.bank_price_index.min() - span * 0.15, combined.bank_price_index.max() + span * 0.2)
    left.axhline(1, color="#a5afa4", lw=0.8, ls="--")
    left.legend(handles=[a, b], loc="upper left", frameon=False, fontsize=11)
    latest = combined.iloc[-1]
    left.text(
        0.985,
        0.975,
        f"末日 PB：{latest.mean_pb:.4f} 倍\n末日指数：{latest.bank_price_index:,.2f} 点",
        transform=left.transAxes,
        ha="right",
        va="top",
        fontsize=11,
        color="#40584e",
    )
    axes[1].plot(combined.index, combined.valid_banks, color="#648271", lw=1.1)
    axes[1].fill_between(combined.index, 0, combined.valid_banks, color="#d7e5dc", alpha=0.8)
    axes[1].set_ylabel("PB 有效银行数")
    axes[1].set_ylim(0, 46)
    axes[1].set_yticks([16, 30, 42])
    axes[1].set_xlim(pd.Timestamp("2016-01-01"), combined.index[-1])
    axes[1].xaxis.set_major_locator(mdates.YearLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for ax in axes:
        ax.set_facecolor("white")
        ax.grid(axis="y", color="#dde4dd", lw=0.7)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#c1ccc4")
    right.spines["top"].set_visible(False)
    right.spines["left"].set_visible(False)
    right.spines["right"].set_color("#c1ccc4")
    fig.text(
        0.075,
        0.048,
        "PB 为当日有效银行等权均值；中证银行为官方编制权重。两条线采用各自刻度，走势叠加不代表因果。",
        fontsize=10,
    )
    fig.text(
        0.075,
        0.02,
        "指数收盘点位由东方财富历史行情取得，价格口径不含现金分红再投资；图中没有填补缺失数据。",
        fontsize=10,
        color="#65746b",
    )
    fig.subplots_adjust(left=0.075, right=0.905, top=0.855, bottom=0.13, hspace=0.18)
    for extension in ["png", "svg"]:
        fig.savefig(folder / f"银行平均PB_叠加银行价格指数.{extension}", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    report = {
        "pb_source_folder": str(clean_folder),
        "price_source": "Eastmoney historical daily index close",
        "index_code": "399986",
        "return_basis": "PRICE_INDEX; no dividend reinvestment",
        "aligned_days": len(combined),
        "start": combined.index[0].strftime("%Y-%m-%d"),
        "end": combined.index[-1].strftime("%Y-%m-%d"),
        "missing_index_days": 0,
        "unchanged_pb": True,
        "price_ohlc_check": "PASS",
        "cross_provider_check": "NOT_VERIFIED",
        "definition_reference": "https://www.csindex.com.cn/zh-CN/indices/index-detail/399986",
        "latest_pb": float(latest.mean_pb),
        "latest_index_close": float(latest.bank_price_index),
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()},
    }
    (folder / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"folder": str(folder), "aligned_days": len(combined), "latest_index_close": report["latest_index_close"]},
            ensure_ascii=False,
        )
    )


def draw_clean(daily, folder, exclusions):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    plt.rcParams["font.family"] = FontProperties(fname="C:/Windows/Fonts/msyh.ttc").get_name()
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [4, 1]}, sharex=True)
    fig.patch.set_facecolor("#f7f8f4")
    fig.suptitle("银行板块平均 PB｜剔除已定位异常", x=0.075, ha="left", fontsize=21, fontweight="bold", y=0.966)
    fig.text(
        0.075,
        0.91,
        f"{daily.index[0]:%Y-%m-%d} — {daily.index[-1]:%Y-%m-%d}  ·  等权算术平均"
        f"  ·  剔除 {exclusions} 条，分母按剩余有效银行重新计算",
        fontsize=12,
        color="#556b60",
    )
    axes[0].plot(daily.index, daily.mean_pb, color="#146d55", lw=1.4)
    axes[0].axhline(1, color="#a79f8d", lw=0.9, ls="--")
    axes[0].text(daily.index[25], 1.02, "PB = 1", color="#8e8576", fontsize=10)
    axes[0].set_ylabel("平均 PB（倍）", fontsize=12)
    axes[0].set_ylim(0.35, max(1.9, daily.mean_pb.max() * 1.2))
    latest = daily.iloc[-1]
    axes[0].scatter(daily.index[-1], latest.mean_pb, color="#146d55", s=25, zorder=5)
    axes[0].annotate(
        f"{daily.index[-1]:%Y-%m-%d}\n{latest.mean_pb:.4f} 倍 · {int(latest.valid_banks)} 家",
        xy=(daily.index[-1], latest.mean_pb),
        xytext=(-120, 55),
        textcoords="offset points",
        fontsize=11,
        color="#146d55",
        arrowprops={"arrowstyle": "->", "color": "#146d55"},
    )
    axes[1].plot(daily.index, daily.valid_banks, color="#628675", lw=1.2)
    axes[1].fill_between(daily.index, 0, daily.valid_banks, color="#d3e3d9", alpha=0.75)
    axes[1].set_ylabel("有效银行数")
    axes[1].set_ylim(0, 46)
    axes[1].set_yticks([16, 30, 42])
    axes[1].xaxis.set_major_locator(mdates.YearLocator(1))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axes[1].set_xlim(daily.index[0], daily.index[-1] + pd.Timedelta(days=12))
    for ax in axes:
        ax.set_facecolor("white")
        ax.grid(axis="y", color="#dde4dd", linewidth=0.7)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#c1ccc4")
    fig.text(0.075, 0.04, "平均 PB = 有效 PB 加总 ÷ 当日有效银行数；缺失不补零，未上市银行不参加。", fontsize=11)
    fig.text(
        0.075,
        0.012,
        "来源：归档 Tushare 兼容接口；仅剔除已定位的 56 条历史异常，其他数值仍需持续核验。",
        fontsize=10,
        color="#65756c",
    )
    fig.subplots_adjust(left=0.075, right=0.97, top=0.855, bottom=0.115, hspace=0.18)
    for extension in ["png", "svg"]:
        fig.savefig(folder / f"银行板块平均PB_剔除异常.{extension}", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__ == "__main__":
    main()
