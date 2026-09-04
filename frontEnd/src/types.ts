export type EvidenceValue = string | number | boolean | null;

export interface FoldDecision {
  fold_id: string;
  train_mean_rank_ic: number | null;
  validation_mean_rank_ic: number | null;
  test_mean_rank_ic_raw: number | null;
  hac_direction_outcome: string | null;
  bootstrap_direction_outcome: string | null;
  exposure_status_before_run: string;
}

export interface RegimeRow {
  fold_id: string;
  regime_dimension: string;
  regime: string;
  session_count: number;
  mean_rank_ic_raw: number | null;
}

export interface IncrementalEvidence {
  mean_orthogonal_rank_ic_directed: number | null;
  mean_conditional_rank_ic: number | null;
  mean_incremental_r_squared: number | null;
  sample_classification: string;
}

export interface FactorEntity {
  entity_id: string;
  factor_id: string;
  factor_version: string;
  variant: string;
  name: string;
  family: string;
  source_id: string | null;
  lifecycle: string | null;
  direction: string | null;
  economic_hypothesis: string | null;
  expected_mechanism: string | null;
  formula: string | null;
  implementation_type: string | null;
  implementation_hash: string | null;
  quality: { coverage?: number | null } | null;
  basic_evidence: ({ mean_rank_ic?: number | null; window?: { start: string; end: string } } & Record<string, EvidenceValue | object>) | null;
  robustness: ({ hac_bh_q_value?: number | null } & Record<string, EvidenceValue>) | null;
  folds: FoldDecision[];
  regimes: RegimeRow[];
  deduplication: { is_canonical?: boolean; canonical_entity_id?: string };
  cluster: { cluster_id?: string } | null;
  incremental: IncrementalEvidence | null;
  canonical_incremental: IncrementalEvidence | null;
  routes: string[];
  execution: { status: string; reason: string };
  model_contribution: { status: string; reason: string };
}

export interface ClusterMember {
  entity_id: string;
  mean_distance: number | null;
  mean_coverage: number | null;
  is_representative: boolean;
}

export interface ClusterGroup {
  cluster_id: string;
  representative_entity_id: string;
  members: ClusterMember[];
}

export interface CorrelationRow {
  left_entity_id: string;
  right_entity_id: string;
  mean_daily_spearman_value_correlation: number | null;
  daily_rank_ic_correlation: number | null;
}

export interface ExplorerData {
  report: {
    title: string;
    report_id: string;
    generator_version: string;
    generated_at: string;
    maximum_compare_entities: number;
    sample_classification: string;
    window: { start: string; end: string };
    universe_id: string;
    universe_version: string;
    constraint_level: string;
    label_horizon_sessions: number;
    walk_forward_id: string;
    redundancy_id: string;
    robustness_id: string | null;
    limitations: string[];
  };
  summary: {
    entity_count: number;
    factor_count: number;
    canonical_count: number;
    cluster_count: number;
    integrity_blocker_count: number;
    execution_available_count: number;
  };
  factors: FactorEntity[];
  clusters: ClusterGroup[];
  correlations: CorrelationRow[];
}
