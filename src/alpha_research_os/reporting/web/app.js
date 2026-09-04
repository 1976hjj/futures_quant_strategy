(() => {
  "use strict";
  const data = window.__EXPLORER_DATA__;
  if (!data) throw new Error("Explorer data is missing");

  const byId = new Map(data.factors.map(item => [item.entity_id, item]));
  const selected = new Set();
  const state = { search: "", variant: "", family: "", cluster: "", route: "", sort: "factor" };
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
  const formatNumber = (value, digits = 4) => value == null ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
  const formatPercent = value => value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
  const shortId = value => value ? `${value.slice(0, 14)}…${value.slice(-6)}` : "—";

  function foldOutcome(row) {
    const outcomes = [row.hac_direction_outcome, row.bootstrap_direction_outcome];
    if (outcomes.includes("DIRECTION_CONTRADICTED")) return ["C", "contradicted", "方向证伪"];
    if (outcomes.includes("DIRECTION_SUPPORTED")) return ["S", "supported", "方向支持"];
    return ["N", "not-rejected", "未拒绝零假设"];
  }

  function routeClass(route) {
    if (route.includes("QUARANTINED")) return "blocked";
    if (route.includes("OOS")) return "exposed";
    if (route.includes("DIAGNOSTIC")) return "diagnostic";
    if (route.includes("MODEL")) return "model";
    return "";
  }

  function routeHtml(routes) {
    return `<div class="route-list">${routes.map(route => `<span class="route ${routeClass(route)}">${escapeHtml(route)}</span>`).join("")}</div>`;
  }

  function latestTestRankIc(item) {
    const fold = item.folds[item.folds.length - 1];
    return fold ? fold.test_mean_rank_ic_raw : null;
  }

  function renderHeader() {
    document.title = data.report.title;
    $("#report-title").textContent = data.report.title;
    const windowLabel = `${data.report.window.start} → ${data.report.window.end}`;
    $("#context").innerHTML = [
      `${data.report.universe_id} · ${data.report.universe_version}`,
      `${data.report.label_horizon_sessions}-session label`,
      data.report.constraint_level,
      windowLabel,
      data.report.sample_classification
    ]
      .map(value => `<span>${escapeHtml(value)}</span>`).join("");
    $("#sample-notice").textContent = `当前窗口 ${windowLabel} 已是暴露研究样本。本页面用于诊断和组合参考，不是新的 OOS 确认。`;
  }

  function renderSummary() {
    const cards = [
      ["因子", data.summary.factor_count], ["因子×变体", data.summary.entity_count],
      ["Canonical", data.summary.canonical_count], ["Clusters", data.summary.cluster_count],
      ["完整性 Blocker", data.summary.integrity_blocker_count],
      ["Execution 可用", `${data.summary.execution_available_count}/${data.summary.entity_count}`]
    ];
    $("#summary-cards").innerHTML = cards.map(([label, value]) => `<article class="metric panel"><div class="label">${label}</div><div class="value">${value}</div></article>`).join("");
  }

  function optionValues(key) {
    if (key === "cluster") return [...new Set(data.factors.map(item => item.cluster?.cluster_id).filter(Boolean))].sort();
    if (key === "route") return [...new Set(data.factors.flatMap(item => item.routes))].sort();
    return [...new Set(data.factors.map(item => item[key]).filter(Boolean))].sort();
  }

  function fillFilters() {
    [["#variant-filter", "variant"], ["#family-filter", "family"], ["#cluster-filter", "cluster"], ["#route-filter", "route"]].forEach(([selector, key]) => {
      $(selector).insertAdjacentHTML("beforeend", optionValues(key).map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join(""));
    });
  }

  function filteredFactors() {
    const search = state.search.toLowerCase();
    const items = data.factors.filter(item => {
      const haystack = [item.factor_id, item.name, item.family, item.source_id, item.entity_id].join(" ").toLowerCase();
      return (!search || haystack.includes(search)) && (!state.variant || item.variant === state.variant) &&
        (!state.family || item.family === state.family) && (!state.cluster || item.cluster?.cluster_id === state.cluster) &&
        (!state.route || item.routes.includes(state.route));
    });
    const scores = {
      factor: item => item.entity_id,
      coverage: item => -(item.quality?.coverage ?? -1),
      rankic: item => -Math.abs(latestTestRankIc(item) ?? 0),
      incremental: item => -Math.abs(item.incremental?.mean_orthogonal_rank_ic_directed ?? 0)
    };
    return items.sort((a, b) => {
      const left = scores[state.sort](a), right = scores[state.sort](b);
      return typeof left === "string" ? left.localeCompare(right) : left - right || a.entity_id.localeCompare(b.entity_id);
    });
  }

  function renderTable() {
    const items = filteredFactors();
    $("#visible-count").textContent = `显示 ${items.length} / ${data.factors.length} 条路径`;
    $("#factor-table").innerHTML = items.map(item => {
      const foldBadges = item.folds.map(row => {
        const [label, cls, title] = foldOutcome(row);
        return `<span class="fold ${cls}" title="${escapeHtml(row.fold_id)} · ${title}">${label}</span>`;
      }).join("");
      return `<tr>
        <td><input class="factor-select" type="checkbox" data-id="${escapeHtml(item.entity_id)}" ${selected.has(item.entity_id) ? "checked" : ""} aria-label="选择 ${escapeHtml(item.factor_id)}"></td>
        <td><button class="factor-link" data-detail="${escapeHtml(item.entity_id)}">${escapeHtml(item.factor_id)}</button><div class="sub">${escapeHtml(item.name)} · v${escapeHtml(item.factor_version)}</div></td>
        <td><span class="pill">${escapeHtml(item.variant)}</span></td>
        <td class="number">${formatPercent(item.quality?.coverage)}</td>
        <td><div class="folds">${foldBadges}</div><div class="sub">Test ${formatNumber(latestTestRankIc(item))}</div></td>
        <td>${escapeHtml(item.cluster?.cluster_id ?? "—")}<div class="sub">${item.deduplication?.is_canonical ? "canonical" : "duplicate"}</div></td>
        <td class="number">${formatNumber(item.incremental?.mean_orthogonal_rank_ic_directed)}</td>
        <td><span class="na">${escapeHtml(item.execution.status)}</span></td>
        <td>${routeHtml(item.routes)}</td>
      </tr>`;
    }).join("");
    bindTableEvents();
  }

  function bindTableEvents() {
    $$("[data-detail]").forEach(button => button.addEventListener("click", () => openDetail(button.dataset.detail)));
    $$(".factor-select").forEach(box => box.addEventListener("change", () => {
      if (box.checked && selected.size >= data.report.maximum_compare_entities) {
        box.checked = false; toast(`最多选择 ${data.report.maximum_compare_entities} 条路径`); return;
      }
      box.checked ? selected.add(box.dataset.id) : selected.delete(box.dataset.id);
      updateSelection();
    }));
  }

  function detailStat(label, value) { return `<div class="detail-stat"><div class="label">${label}</div><div class="value">${value}</div></div>`; }

  function openDetail(id) {
    const item = byId.get(id);
    const foldRows = item.folds.map(row => {
      const [, , title] = foldOutcome(row);
      return `<tr><td>${escapeHtml(row.fold_id)}</td><td>${formatNumber(row.train_mean_rank_ic)}</td><td>${formatNumber(row.validation_mean_rank_ic)}</td><td>${formatNumber(row.test_mean_rank_ic_raw)}</td><td>${title}</td></tr>`;
    }).join("");
    const regimeRows = item.regimes.map(row => `<tr><td>${escapeHtml(row.fold_id)}</td><td>${escapeHtml(row.regime_dimension)}</td><td>${escapeHtml(row.regime)}</td><td>${row.session_count}</td><td>${formatNumber(row.mean_rank_ic_raw)}</td></tr>`).join("");
    const robustness = item.robustness;
    const incremental = item.incremental;
    const canonicalIncremental = item.canonical_incremental;
    const basicWindow = item.basic_evidence?.window;
    const basicLabel = basicWindow ? `基础 RankIC (${basicWindow.start} → ${basicWindow.end})` : "基础 RankIC";
    $("#detail-content").innerHTML = `
      <div class="detail-hero"><div class="eyebrow">${escapeHtml(item.family)} · ${escapeHtml(item.variant)}</div><h2>${escapeHtml(item.factor_id)}</h2><p>${escapeHtml(item.name)} · v${escapeHtml(item.factor_version)}</p>${routeHtml(item.routes)}</div>
      <section class="panel detail-section"><h3>经济假设与实现</h3><p>${escapeHtml(item.economic_hypothesis ?? "未声明")}</p><p class="sub">${escapeHtml(item.expected_mechanism ?? "")}</p><dl><dt>公式 / 实现</dt><dd><code>${escapeHtml(item.formula ?? item.implementation_type ?? "—")}</code></dd><dt>方向</dt><dd>${escapeHtml(item.direction ?? "—")}</dd><dt>实现哈希</dt><dd>${escapeHtml(item.implementation_hash ?? "—")}</dd></dl></section>
      <section class="panel detail-section"><h3>关键画像</h3><div class="detail-grid">
        ${detailStat("Coverage", formatPercent(item.quality?.coverage))}
        ${detailStat(basicLabel, formatNumber(item.basic_evidence?.mean_rank_ic))}
        ${detailStat("长窗 Test RankIC", formatNumber(latestTestRankIc(item)))}
        ${detailStat("短窗 HAC q", formatNumber(robustness?.hac_bh_q_value, 6))}
        ${detailStat("正交 RankIC", formatNumber(incremental?.mean_orthogonal_rank_ic_directed))}
        ${detailStat("增量 R²", formatNumber(incremental?.mean_incremental_r_squared, 6))}
      </div></section>
      <section class="panel detail-section"><h3>Walk-Forward</h3><table class="mini-table"><thead><tr><th>Fold</th><th>Train</th><th>Validation</th><th>Test</th><th>结果</th></tr></thead><tbody>${foldRows}</tbody></table></section>
      <section class="panel detail-section"><h3>Regime</h3><table class="mini-table"><thead><tr><th>Fold</th><th>维度</th><th>状态</th><th>样本</th><th>RankIC</th></tr></thead><tbody>${regimeRows}</tbody></table></section>
      <section class="panel detail-section"><h3>冗余与增量</h3><dl><dt>Entity</dt><dd>${escapeHtml(item.entity_id)}</dd><dt>Cluster</dt><dd>${escapeHtml(item.cluster?.cluster_id ?? "—")}</dd><dt>Canonical</dt><dd>${item.deduplication?.is_canonical ? "YES" : escapeHtml(item.deduplication?.canonical_entity_id ?? "NO")}</dd><dt>本路径条件 RankIC</dt><dd>${formatNumber(incremental?.mean_conditional_rank_ic)}</dd><dt>Canonical 正交 RankIC</dt><dd>${formatNumber(canonicalIncremental?.mean_orthogonal_rank_ic_directed)}</dd><dt>样本分类</dt><dd>${escapeHtml(incremental?.sample_classification ?? canonicalIncremental?.sample_classification ?? data.report.sample_classification)}</dd></dl></section>
      <section class="panel detail-section"><h3>可执行性</h3><p class="na">NOT_AVAILABLE · M4.6 尚未发布。缺失不表示收益或成本为零。</p></section>
      <section class="panel detail-section"><h3>模型贡献</h3><p class="na">NOT_AVAILABLE · M6 尚未发布。单因子结果不会替代模型级 Walk-Forward。</p></section>
      <section class="panel detail-section"><h3>Evidence lineage</h3><dl><dt>Walk-Forward</dt><dd>${escapeHtml(data.report.walk_forward_id)}</dd><dt>Redundancy</dt><dd>${escapeHtml(data.report.redundancy_id)}</dd><dt>Robustness</dt><dd>${escapeHtml(data.report.robustness_id ?? "NOT_AVAILABLE")}</dd></dl></section>`;
    $("#detail-drawer").classList.add("open"); $("#backdrop").classList.add("open"); $("#detail-drawer").setAttribute("aria-hidden", "false");
  }

  function closeDetail() { $("#detail-drawer").classList.remove("open"); $("#backdrop").classList.remove("open"); $("#detail-drawer").setAttribute("aria-hidden", "true"); }

  function correlation(left, right) {
    if (left === right) return { mean_daily_spearman_value_correlation: 1, daily_rank_ic_correlation: 1 };
    return data.correlations.find(row => (row.left_entity_id === left && row.right_entity_id === right) || (row.left_entity_id === right && row.right_entity_id === left));
  }

  function renderCompare() {
    const items = [...selected].map(id => byId.get(id)).filter(Boolean);
    $("#compare-empty").style.display = items.length >= 2 ? "none" : "block";
    $("#compare-content").innerHTML = items.length < 2 ? "" : `
      <div class="panel compare-table"><table><thead><tr><th>指标</th>${items.map(item => `<th>${escapeHtml(item.factor_id)}<div class="sub">${escapeHtml(item.variant)}</div></th>`).join("")}</tr></thead><tbody>
      ${[
        ["Coverage", item => formatPercent(item.quality?.coverage)], ["最新 Test RankIC", item => formatNumber(latestTestRankIc(item))],
        ["正交 RankIC", item => formatNumber(item.incremental?.mean_orthogonal_rank_ic_directed)], ["增量 R²", item => formatNumber(item.incremental?.mean_incremental_r_squared, 6)],
        ["Cluster", item => escapeHtml(item.cluster?.cluster_id ?? "—")], ["Execution", item => `<span class="na">${item.execution.status}</span>`]
      ].map(([label, getter]) => `<tr><td>${label}</td>${items.map(item => `<td>${getter(item)}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div>
      <div class="panel compare-table"><table class="matrix"><thead><tr><th>Factor-value Spearman</th>${items.map(item => `<th>${escapeHtml(item.factor_id)}</th>`).join("")}</tr></thead><tbody>${items.map(left => `<tr><td>${escapeHtml(left.factor_id)}<div class="sub">${escapeHtml(left.variant)}</div></td>${items.map(right => `<td>${formatNumber(correlation(left.entity_id, right.entity_id)?.mean_daily_spearman_value_correlation, 3)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      <div class="panel compare-table"><table class="matrix"><thead><tr><th>Daily RankIC correlation</th>${items.map(item => `<th>${escapeHtml(item.factor_id)}</th>`).join("")}</tr></thead><tbody>${items.map(left => `<tr><td>${escapeHtml(left.factor_id)}<div class="sub">${escapeHtml(left.variant)}</div></td>${items.map(right => `<td>${formatNumber(correlation(left.entity_id, right.entity_id)?.daily_rank_ic_correlation, 3)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }

  function renderClusters() {
    $("#cluster-grid").innerHTML = data.clusters.map(cluster => `<article class="panel cluster-card"><div class="cluster-head"><div><div class="eyebrow">${escapeHtml(cluster.cluster_id)}</div><h3>${cluster.members.length} 条 canonical 路径</h3></div><span class="pill">REP: ${escapeHtml(byId.get(cluster.representative_entity_id)?.factor_id ?? cluster.representative_entity_id)}</span></div>${cluster.members.map(member => `<div class="cluster-member ${member.is_representative ? "representative" : ""}"><button class="factor-link" data-detail="${escapeHtml(member.entity_id)}">${escapeHtml(member.entity_id)}</button><span class="number">d=${formatNumber(member.mean_distance, 3)}</span></div>`).join("")}</article>`).join("");
    $$("#cluster-grid [data-detail]").forEach(button => button.addEventListener("click", () => openDetail(button.dataset.detail)));
  }

  function renderAbout() {
    $("#limitations").innerHTML = data.report.limitations.map(item => `<li>${escapeHtml(item)}</li>`).join("");
    const rows = [["Report ID", data.report.report_id], ["Generator", data.report.generator_version], ["Walk-Forward", data.report.walk_forward_id], ["Redundancy", data.report.redundancy_id], ["Robustness", data.report.robustness_id ?? "NOT_AVAILABLE"], ["Generated", data.report.generated_at]];
    $("#report-lineage").innerHTML = rows.map(([label, value]) => `<dt>${label}</dt><dd title="${escapeHtml(value)}">${label.includes("ID") || label === "Walk-Forward" || label === "Redundancy" || label === "Robustness" ? shortId(value) : escapeHtml(value)}</dd>`).join("");
  }

  function updateSelection() { $("#selected-count").textContent = selected.size; renderCompare(); }

  function switchView(name) {
    $$(".view").forEach(view => view.classList.toggle("active", view.id === `${name}-view`));
    $$(".tab").forEach(tab => tab.classList.toggle("active", tab.dataset.view === name));
    if (name === "compare") renderCompare();
  }

  function exportFeatureSet() {
    const entities = [...selected];
    if (entities.length < 2) { toast("至少选择 2 条路径"); return; }
    const draft = { schema_version: "1", status: "DRAFT_NOT_REGISTERED", created_at: new Date().toISOString(), source_report_id: data.report.report_id, sample_classification: data.report.sample_classification, exposed_window: data.report.window, entities };
    const blob = new Blob([JSON.stringify(draft, null, 2) + "\n"], { type: "application/json" });
    const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `featureset-draft-${Date.now()}.json`; link.click(); URL.revokeObjectURL(link.href);
    toast("FeatureSet 草案已导出；尚未注册或训练");
  }

  function toast(message) { const element = $("#toast"); element.textContent = message; element.classList.add("show"); setTimeout(() => element.classList.remove("show"), 2200); }

  function bind() {
    $$(".tab").forEach(tab => tab.addEventListener("click", () => switchView(tab.dataset.view)));
    [["#search", "search", "input"], ["#variant-filter", "variant", "change"], ["#family-filter", "family", "change"], ["#cluster-filter", "cluster", "change"], ["#route-filter", "route", "change"], ["#sort", "sort", "change"]].forEach(([selector, key, event]) => $(selector).addEventListener(event, e => { state[key] = e.target.value; renderTable(); }));
    $("#close-detail").addEventListener("click", closeDetail); $("#backdrop").addEventListener("click", closeDetail); document.addEventListener("keydown", event => { if (event.key === "Escape") closeDetail(); });
    $("#export-features").addEventListener("click", exportFeatureSet);
  }

  renderHeader(); renderSummary(); fillFilters(); renderTable(); renderClusters(); renderAbout(); bind(); updateSelection();
})();
