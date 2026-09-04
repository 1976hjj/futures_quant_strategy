import { useEffect, useMemo, useState } from "react";
import type { ExplorerData, FactorEntity, FoldDecision } from "./types";

type View = "overview" | "compare" | "clusters" | "about";
type Filters = { search: string; variant: string; family: string; cluster: string; route: string; sort: string };

const initialFilters: Filters = { search: "", variant: "", family: "", cluster: "", route: "", sort: "factor" };
const pageSize = 50;

function number(value: number | null | undefined, digits = 4) {
  return value == null ? "—" : value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function percent(value: number | null | undefined) {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function shortId(value: string | null | undefined) {
  return value ? `${value.slice(0, 14)}…${value.slice(-6)}` : "—";
}

function foldOutcome(row: FoldDecision): [string, string, string] {
  const outcomes = [row.hac_direction_outcome, row.bootstrap_direction_outcome];
  if (outcomes.includes("DIRECTION_CONTRADICTED")) return ["C", "contradicted", "方向证伪"];
  if (outcomes.includes("DIRECTION_SUPPORTED")) return ["S", "supported", "方向支持"];
  return ["N", "not-rejected", "未拒绝零假设"];
}

function latestTestRankIc(item: FactorEntity) {
  return item.folds.at(-1)?.test_mean_rank_ic_raw ?? null;
}

function RouteList({ routes }: { routes: string[] }) {
  return (
    <div className="route-list">
      {routes.map((route) => {
        const tone = route.includes("QUARANTINED") ? "blocked" : route.includes("OOS") || route.includes("DIAGNOSTIC") ? "warning" : route.includes("MODEL") ? "model" : "";
        return <span className={`route ${tone}`} key={route}>{route}</span>;
      })}
    </div>
  );
}

function DetailDrawer({ item, data, onClose }: { item: FactorEntity | null; data: ExplorerData; onClose: () => void }) {
  useEffect(() => {
    const close = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [onClose]);

  if (!item) return null;
  const basicWindow = item.basic_evidence?.window;
  const basicLabel = basicWindow ? `基础 RankIC (${basicWindow.start} → ${basicWindow.end})` : "基础 RankIC";
  return (
    <>
      <button className="backdrop" aria-label="关闭详情" onClick={onClose} />
      <aside className="drawer" aria-label={`${item.factor_id} 详情`}>
        <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        <div className="detail-hero">
          <div className="eyebrow">{item.family} · {item.variant}</div>
          <h2>{item.factor_id}</h2>
          <p>{item.name} · v{item.factor_version}</p>
          <RouteList routes={item.routes} />
        </div>

        <section className="panel detail-section">
          <h3>经济假设与实现</h3>
          <p>{item.economic_hypothesis ?? "未声明"}</p>
          <p className="muted">{item.expected_mechanism}</p>
          <dl><dt>公式 / 实现</dt><dd><code>{item.formula ?? item.implementation_type ?? "—"}</code></dd><dt>方向</dt><dd>{item.direction ?? "—"}</dd><dt>实现哈希</dt><dd>{item.implementation_hash ?? "—"}</dd></dl>
        </section>

        <section className="panel detail-section">
          <h3>关键画像</h3>
          <div className="detail-grid">
            {[
              ["Coverage", percent(item.quality?.coverage)],
              [basicLabel, number(item.basic_evidence?.mean_rank_ic)],
              ["最新 Test RankIC", number(latestTestRankIc(item))],
              ["短窗 HAC q", number(item.robustness?.hac_bh_q_value, 6)],
              ["本路径正交 RankIC", number(item.incremental?.mean_orthogonal_rank_ic_directed)],
              ["Canonical 正交 RankIC", number(item.canonical_incremental?.mean_orthogonal_rank_ic_directed)],
            ].map(([label, value]) => <div className="detail-stat" key={label}><span>{label}</span><strong>{value}</strong></div>)}
          </div>
        </section>

        <section className="panel detail-section table-card">
          <h3>Walk-Forward stability audit</h3>
          <table className="mini-table"><thead><tr><th>Fold</th><th>Train</th><th>Validation</th><th>Test</th><th>结果</th></tr></thead><tbody>
            {item.folds.map((row) => <tr key={row.fold_id}><td>{row.fold_id}</td><td>{number(row.train_mean_rank_ic)}</td><td>{number(row.validation_mean_rank_ic)}</td><td>{number(row.test_mean_rank_ic_raw)}</td><td>{foldOutcome(row)[2]}</td></tr>)}
          </tbody></table>
          <p className="footnote">当前模块不进行模型重训练；Train 仅锚定方向和部分 Regime 阈值。</p>
        </section>

        <section className="panel detail-section table-card">
          <h3>Regime</h3>
          <table className="mini-table"><thead><tr><th>Fold</th><th>维度</th><th>状态</th><th>样本</th><th>RankIC</th></tr></thead><tbody>
            {item.regimes.map((row, index) => <tr key={`${row.fold_id}-${row.regime_dimension}-${row.regime}-${index}`}><td>{row.fold_id}</td><td>{row.regime_dimension}</td><td>{row.regime}</td><td>{row.session_count}</td><td>{number(row.mean_rank_ic_raw)}</td></tr>)}
          </tbody></table>
        </section>

        <section className="panel detail-section">
          <h3>冗余与增量</h3>
          <dl><dt>Entity</dt><dd>{item.entity_id}</dd><dt>Cluster</dt><dd>{item.cluster?.cluster_id ?? "—"}</dd><dt>Canonical</dt><dd>{item.deduplication.is_canonical ? "YES" : item.deduplication.canonical_entity_id ?? "NO"}</dd><dt>本路径条件 RankIC</dt><dd>{number(item.incremental?.mean_conditional_rank_ic)}</dd><dt>样本分类</dt><dd>{item.incremental?.sample_classification ?? item.canonical_incremental?.sample_classification ?? data.report.sample_classification}</dd></dl>
        </section>
        <section className="panel detail-section"><h3>可执行性</h3><p className="not-available">{item.execution.status} · M4.6 尚未发布。缺失不表示收益或成本为零。</p></section>
        <section className="panel detail-section"><h3>模型贡献</h3><p className="not-available">{item.model_contribution.status} · M6 尚未发布。单因子结果不会替代模型级 Walk-Forward。</p></section>
      </aside>
    </>
  );
}

export default function App() {
  const [data, setData] = useState<ExplorerData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("overview");
  const [filters, setFilters] = useState(initialFilters);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  const [detailId, setDetailId] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${import.meta.env.BASE_URL}data/factor-explorer.json`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<ExplorerData>;
      })
      .then(setData)
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, []);

  useEffect(() => setPage(1), [filters]);

  const byId = useMemo(() => new Map(data?.factors.map((item) => [item.entity_id, item]) ?? []), [data]);
  const correlationMap = useMemo(() => {
    const map = new Map<string, ExplorerData["correlations"][number]>();
    data?.correlations.forEach((row) => {
      map.set(`${row.left_entity_id}\u0000${row.right_entity_id}`, row);
      map.set(`${row.right_entity_id}\u0000${row.left_entity_id}`, row);
    });
    return map;
  }, [data]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const search = filters.search.trim().toLowerCase();
    return data.factors.filter((item) => {
      const haystack = [item.factor_id, item.name, item.family, item.source_id, item.entity_id].join(" ").toLowerCase();
      return (!search || haystack.includes(search)) && (!filters.variant || item.variant === filters.variant) && (!filters.family || item.family === filters.family) && (!filters.cluster || item.cluster?.cluster_id === filters.cluster) && (!filters.route || item.routes.includes(filters.route));
    }).sort((left, right) => {
      if (filters.sort === "coverage") return (right.quality?.coverage ?? -1) - (left.quality?.coverage ?? -1);
      if (filters.sort === "rankic") return Math.abs(latestTestRankIc(right) ?? 0) - Math.abs(latestTestRankIc(left) ?? 0);
      if (filters.sort === "incremental") return Math.abs(right.incremental?.mean_orthogonal_rank_ic_directed ?? 0) - Math.abs(left.incremental?.mean_orthogonal_rank_ic_directed ?? 0);
      return left.entity_id.localeCompare(right.entity_id);
    });
  }, [data, filters]);

  if (error) return <main className="state"><h1>无法读取 Explorer 数据</h1><p>{error}</p><p>请先运行 <code>npm.cmd run sync:data</code>。</p></main>;
  if (!data) return <main className="state"><div className="loader" /><p>正在载入因子证据…</p></main>;

  const variants = [...new Set(data.factors.map((item) => item.variant))].sort();
  const families = [...new Set(data.factors.map((item) => item.family))].sort();
  const clusters = [...new Set(data.factors.map((item) => item.cluster?.cluster_id).filter(Boolean) as string[])].sort();
  const routes = [...new Set(data.factors.flatMap((item) => item.routes))].sort();
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const visible = filtered.slice((page - 1) * pageSize, page * pageSize);
  const selectedItems = selected.map((id) => byId.get(id)).filter((item): item is FactorEntity => Boolean(item));
  const detail = detailId ? byId.get(detailId) ?? null : null;

  const select = (id: string) => {
    setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : current.length < data.report.maximum_compare_entities ? [...current, id] : current);
  };
  const setFilter = (key: keyof Filters, value: string) => setFilters((current) => ({ ...current, [key]: value }));
  const exportFeatureSet = () => {
    if (selected.length < 2) return;
    const draft = { schema_version: "1", status: "DRAFT_NOT_REGISTERED", created_at: new Date().toISOString(), source_report_id: data.report.report_id, sample_classification: data.report.sample_classification, exposed_window: data.report.window, entities: selected };
    const url = URL.createObjectURL(new Blob([`${JSON.stringify(draft, null, 2)}\n`], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `featureset-draft-${Date.now()}.json`; link.click(); URL.revokeObjectURL(url);
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div><div className="eyebrow">ALPHA RESEARCH OS</div><h1>{data.report.title}</h1><p>研究证据库 · 只读前端</p></div>
        <div className="context">
          <span>{data.report.universe_id} · {data.report.universe_version}</span><span>{data.report.label_horizon_sessions}-session label</span><span>{data.report.constraint_level}</span><span>{data.report.window.start} → {data.report.window.end}</span><span>{data.report.sample_classification}</span>
        </div>
      </header>
      <nav className="tabs">
        {(["overview", "compare", "clusters", "about"] as View[]).map((name) => <button className={view === name ? "active" : ""} key={name} onClick={() => setView(name)}>{({ overview: "因子总览", compare: `因子对比 ${selected.length}`, clusters: "因子簇", about: "证据说明" })[name]}</button>)}
      </nav>

      <main>
        {view === "overview" && <>
          <div className="notice">当前窗口已经暴露。本页面用于诊断和组合参考，不是新的 OOS 确认。</div>
          <div className="metric-grid">
            {[['因子', data.summary.factor_count], ['因子×变体', data.summary.entity_count], ['Canonical', data.summary.canonical_count], ['Clusters', data.summary.cluster_count], ['完整性 Blocker', data.summary.integrity_blocker_count], ['Execution 可用', `${data.summary.execution_available_count}/${data.summary.entity_count}`]].map(([label, value]) => <article className="metric panel" key={label}><span>{label}</span><strong>{value}</strong></article>)}
          </div>
          <section className="filters panel">
            <label className="search"><span>搜索</span><input value={filters.search} onChange={(event) => setFilter("search", event.target.value)} placeholder="ID、名称、family、来源" /></label>
            <Select label="Variant" value={filters.variant} options={variants} onChange={(value) => setFilter("variant", value)} />
            <Select label="Family" value={filters.family} options={families} onChange={(value) => setFilter("family", value)} />
            <Select label="Cluster" value={filters.cluster} options={clusters} onChange={(value) => setFilter("cluster", value)} />
            <Select label="Route" value={filters.route} options={routes} onChange={(value) => setFilter("route", value)} />
            <Select label="排序" value={filters.sort} options={["factor", "coverage", "rankic", "incremental"]} labels={{ factor: "因子 ID", coverage: "覆盖率", rankic: "|Test RankIC|", incremental: "|正交 RankIC|" }} includeAll={false} onChange={(value) => setFilter("sort", value)} />
          </section>
          <div className="table-meta"><span>显示 {visible.length} / {filtered.length} 条 · 第 {page}/{pageCount} 页</span><span>单因子结果用于画像与组合参考，不是永久淘汰结论。</span></div>
          <section className="table-card panel"><table><thead><tr><th>选择</th><th>因子</th><th>Variant</th><th>Coverage</th><th>Walk-Forward</th><th>Cluster</th><th>正交 RankIC</th><th>执行</th><th>证据路由</th></tr></thead><tbody>
            {visible.map((item) => <tr key={item.entity_id}><td><input type="checkbox" checked={selected.includes(item.entity_id)} onChange={() => select(item.entity_id)} disabled={!selected.includes(item.entity_id) && selected.length >= data.report.maximum_compare_entities} /></td><td><button className="factor-link" onClick={() => setDetailId(item.entity_id)}>{item.factor_id}</button><small>{item.name} · v{item.factor_version}</small></td><td><span className="pill">{item.variant}</span></td><td className="number">{percent(item.quality?.coverage)}</td><td><div className="folds">{item.folds.map((fold) => { const [label, tone, title] = foldOutcome(fold); return <span className={`fold ${tone}`} title={`${fold.fold_id} · ${title}`} key={fold.fold_id}>{label}</span>; })}</div><small>Test {number(latestTestRankIc(item))}</small></td><td>{item.cluster?.cluster_id ?? "—"}<small>{item.deduplication.is_canonical ? "canonical" : "duplicate"}</small></td><td className="number">{number(item.incremental?.mean_orthogonal_rank_ic_directed)}</td><td className="not-available">{item.execution.status}</td><td><RouteList routes={item.routes} /></td></tr>)}
          </tbody></table></section>
          <div className="pagination"><button disabled={page === 1} onClick={() => setPage((current) => current - 1)}>上一页</button><button disabled={page === pageCount} onClick={() => setPage((current) => current + 1)}>下一页</button></div>
        </>}

        {view === "compare" && <>
          <div className="section-head"><div><div className="eyebrow">FEATURESET WORKBENCH</div><h2>因子对比</h2><p>选择 2～{data.report.maximum_compare_entities} 条路径；导出不会训练模型或修改生命周期。</p></div><button className="primary" disabled={selectedItems.length < 2} onClick={exportFeatureSet}>导出 FeatureSet 草案</button></div>
          {selectedItems.length < 2 ? <div className="empty panel">请先在因子总览中选择至少 2 条路径。</div> : <Compare items={selectedItems} correlationMap={correlationMap} />}
        </>}

        {view === "clusters" && <><div className="section-head"><div><div className="eyebrow">UNSUPERVISED STRUCTURE</div><h2>因子簇</h2><p>代表项用于导航和默认计算路径，不代表晋级。</p></div></div><div className="cluster-grid">{data.clusters.map((cluster) => <article className="cluster-card panel" key={cluster.cluster_id}><div className="cluster-head"><div><div className="eyebrow">{cluster.cluster_id}</div><h3>{cluster.members.length} 条 canonical 路径</h3></div><span className="pill">REP: {byId.get(cluster.representative_entity_id)?.factor_id ?? cluster.representative_entity_id}</span></div>{cluster.members.map((member) => <div className={member.is_representative ? "cluster-member representative" : "cluster-member"} key={member.entity_id}><button className="factor-link" onClick={() => setDetailId(member.entity_id)}>{member.entity_id}</button><span>d={number(member.mean_distance, 3)}</span></div>)}</article>)}</div></>}

        {view === "about" && <><div className="section-head"><div><div className="eyebrow">EVIDENCE BOUNDARY</div><h2>如何阅读</h2></div></div><div className="about-grid"><article className="panel"><h3>不是排行榜</h3><p>没有混合量纲总分。负 RankIC 或方向证伪不等于模型特征无用。</p></article><article className="panel"><h3>样本已经暴露</h3><p>2020～2025 结果用于诊断，不能重新包装为未见 OOS。</p></article><article className="panel"><h3>缺失不是零</h3><p>NOT_AVAILABLE、NULL 与观测值 0 严格区分。</p></article><article className="panel"><h3>展示不创造证据</h3><p>正式事实来自不可变 Manifest、Parquet 和 DuckDB。</p></article></div><section className="panel prose"><h3>当前限制</h3><ul>{data.report.limitations.map((item) => <li key={item}>{item}</li>)}</ul></section><section className="panel prose"><h3>报告血缘</h3><dl><dt>Report ID</dt><dd title={data.report.report_id}>{shortId(data.report.report_id)}</dd><dt>Walk-Forward</dt><dd title={data.report.walk_forward_id}>{shortId(data.report.walk_forward_id)}</dd><dt>Redundancy</dt><dd title={data.report.redundancy_id}>{shortId(data.report.redundancy_id)}</dd><dt>Robustness</dt><dd title={data.report.robustness_id ?? undefined}>{shortId(data.report.robustness_id)}</dd><dt>Generated</dt><dd>{data.report.generated_at}</dd></dl></section></>}
      </main>
      <DetailDrawer item={detail} data={data} onClose={() => setDetailId(null)} />
    </div>
  );
}

function Select({ label, value, options, labels = {}, includeAll = true, onChange }: { label: string; value: string; options: string[]; labels?: Record<string, string>; includeAll?: boolean; onChange: (value: string) => void }) {
  return <label><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{includeAll && <option value="">全部</option>}{options.map((option) => <option value={option} key={option}>{labels[option] ?? option}</option>)}</select></label>;
}

function Compare({ items, correlationMap }: { items: FactorEntity[]; correlationMap: Map<string, ExplorerData["correlations"][number]> }) {
  const correlation = (left: string, right: string, key: "mean_daily_spearman_value_correlation" | "daily_rank_ic_correlation") => left === right ? 1 : correlationMap.get(`${left}\u0000${right}`)?.[key];
  const metrics: [string, (item: FactorEntity) => string][] = [["Coverage", (item) => percent(item.quality?.coverage)], ["最新 Test RankIC", (item) => number(latestTestRankIc(item))], ["正交 RankIC", (item) => number(item.incremental?.mean_orthogonal_rank_ic_directed)], ["增量 R²", (item) => number(item.incremental?.mean_incremental_r_squared, 6)], ["Cluster", (item) => item.cluster?.cluster_id ?? "—"], ["Execution", (item) => item.execution.status]];
  return <><section className="compare-table panel"><table><thead><tr><th>指标</th>{items.map((item) => <th key={item.entity_id}>{item.factor_id}<small>{item.variant}</small></th>)}</tr></thead><tbody>{metrics.map(([label, getter]) => <tr key={label}><td>{label}</td>{items.map((item) => <td key={item.entity_id}>{getter(item)}</td>)}</tr>)}</tbody></table></section>{(["mean_daily_spearman_value_correlation", "daily_rank_ic_correlation"] as const).map((key) => <section className="compare-table panel" key={key}><table><thead><tr><th>{key === "mean_daily_spearman_value_correlation" ? "Factor-value Spearman" : "Daily RankIC correlation"}</th>{items.map((item) => <th key={item.entity_id}>{item.factor_id}</th>)}</tr></thead><tbody>{items.map((left) => <tr key={left.entity_id}><td>{left.factor_id}<small>{left.variant}</small></td>{items.map((right) => <td className="number" key={right.entity_id}>{number(correlation(left.entity_id, right.entity_id, key), 3)}</td>)}</tr>)}</tbody></table></section>)}</>;
}
