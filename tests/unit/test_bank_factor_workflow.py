import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import bank_factor_workflow as workflow
from scripts import publish_bank_sector_factor as sector
from scripts import run_factor_batch as batch
from scripts.bank_processing import write_asset
from scripts.build_bank_timing_data import file_hash
from scripts.serve_m4_control_api import FactorBatchRequest, _batch_stage_closure


def fixture_pack(root):
    (root / 'config').mkdir()
    (root / 'config/bank_timing_data.json').write_text(json.dumps({
        'start': '2020-01-02', 'released_feature_end': '2025-12-31', 'historical_grade': 'RESEARCH_ONLY',
        'frozen_bank_input': 'source_not_to_be_read',
    }))
    days = [date(2024, 4, day) for day in range(1, 5)]
    history = pd.DataFrame([dict(session=day, instrument_id='A', book_to_price=2., cash_yield365=.05,
                                annual_earnings_yield=.1, quality_gate=1, bank_total_return_index=100.)
                           for day in days])
    factor_id = 'bank-sector-pb-history-percentile'
    ready = pd.DataFrame([dict(session=day, factor_id=factor_id, factor_version='2.0.0',
                              data_ready=day != days[-1], coverage=1. if day != days[-1] else .5,
                              valid_count=1, universe_count=1,
                              reason='' if day != days[-1] else 'COVERAGE_OR_HISTORY_WARMUP') for day in days])
    folder, manifest = write_asset(root, 'DERIVED', 'sha256:' + 'b' * 64,
                                  dict(bank_history_panel=history,
                                       bank_pb_history=history[['session', 'instrument_id']].assign(pb_daily=.5),
                                       stock_total_return_inputs=history[['session', 'instrument_id',
                                                                          'bank_total_return_index']]
                                       .assign(total_return_segment=0),
                                       bank_basket_history=pd.DataFrame(dict(session=days, basket_segment=0,
                                                                             bank_equal_total_return_index=100.)),
                                       benchmark_daily=pd.DataFrame(dict(session=days, benchmark_segment=0,
                                                                          gross_total_return_index=100.)),
                                       indicator_input_readiness=ready),
                                  dict(start=str(days[0]), end=str(days[-1])))
    spec = folder / 'build_spec.json'
    spec.write_text(json.dumps(dict(configuration=dict(history_sessions=3, minimum_history_sessions=2,
                                                       trend_sessions=2, relative_strength_sessions=1))))
    manifest['files'][spec.name] = file_hash(spec)
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    return manifest['asset_id'], days


def test_publisher_preserves_pre_start_history_and_reports_missing_latest_day(tmp_path, monkeypatch):
    source_id, days = fixture_pack(tmp_path)
    monkeypatch.setattr(sector, 'prepare_sector_inputs', lambda *args: source_id)
    result = sector.publish(tmp_path, days[1], days[-1], 'bank-sector-pb-history-percentile')
    assert result['observation_level'] == 'SECTOR'
    assert result['summary']['valid_sessions'] == 2
    assert result['summary']['latest_value'] is None
    assert result['data_warnings'] and '输入不足' in result['calculation']['message']
    cached = sector.publish(tmp_path, days[1], days[-1], 'bank-sector-pb-history-percentile')
    assert cached['cache_hit'] and cached['release_id'] == result['release_id']
    marker = next((tmp_path / 'reports/bank_factor_dependencies').glob('*.progress.json'))
    assert json.loads(marker.read_bytes())['status'] == 'PASS'


def test_dependency_range_rejected_before_reading_unreleased_sources(tmp_path):
    fixture_pack(tmp_path)
    with pytest.raises(ValueError, match='holdout'):
        workflow.dependency_preflight(tmp_path, date(2020, 1, 2), date(2026, 6, 30), 'frozen')


def test_data_update_detects_only_live_bank_dependency_workers(tmp_path, monkeypatch):
    folder = tmp_path / 'reports/bank_factor_dependencies'
    folder.mkdir(parents=True)
    marker = folder / 'worker.progress.json'
    marker.write_text(json.dumps(dict(status='RUNNING', worker_pid=456)))
    monkeypatch.setattr(workflow, 'process_alive', lambda pid: pid == 456)
    assert workflow.dependencies_running(tmp_path)
    monkeypatch.setattr(workflow, 'process_alive', lambda pid: False)
    assert not workflow.dependencies_running(tmp_path)


def test_stock_dependency_reuses_verified_snapshot_without_recalculating_bank_days(tmp_path, monkeypatch):
    fixture_pack(tmp_path)
    standard_id = 'sha256:' + 'c' * 64
    features = pd.DataFrame(dict(session=[date(2024, 4, 2)], instrument_id=['A'], close=[10.]))
    identity = workflow.content_hash(dict(standard_pack=standard_id, pipeline=workflow.pipeline_hash(tmp_path),
                                         features=workflow.frame_hash(features)))
    folder = tmp_path / 'data/bank_processing_store/feature_inputs' / identity.removeprefix('sha256:')
    folder.mkdir(parents=True)
    features.to_parquet(folder / 'features.parquet', index=False)
    manifest = dict(input_key=identity, standard_pack_id=standard_id,
                    files={'features.parquet': file_hash(folder / 'features.parquet')})
    (folder / 'input_manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(workflow, 'prepare_standard', lambda *args: (None, {'asset_id': standard_id}))
    monkeypatch.setattr(workflow, 'feature_snapshot',
                        lambda *args: pytest.fail('must not recalculate identical inputs'))
    actual, _ = workflow.prepare_stock_inputs(tmp_path, date(2024, 4, 2), date(2024, 4, 2))
    assert actual == folder
    (folder / 'features.parquet').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='完整性'):
        workflow.prepare_stock_inputs(tmp_path, date(2024, 4, 2), date(2024, 4, 2))


@pytest.mark.parametrize('stages', [[], ['m4_1'], ['m4_5']])
def test_mixed_batch_dispatches_sector_script_and_keeps_sector_out_of_stock_m4(tmp_path, monkeypatch, stages):
    selected = [dict(factor_id='bank-cash-dividend-yield-365', factor_version='1.0.0'),
                dict(factor_id='bank-common-book-to-price', factor_version='1.0.0'),
                dict(factor_id='bank-sector-trend-breadth', factor_version='1.0.0')]
    # Use a real stock ID from the current bank definitions.
    from alpha_research_os.factors.bank import bank_factor_catalog
    selected[1]['factor_id'] = next(item.factor_id for item in bank_factor_catalog()
                                   if item.factor_id != selected[0]['factor_id'])
    request = FactorBatchRequest(factors=selected, start='2020-01-02', end='2025-12-31', stages=stages)
    payload = request.model_dump(mode='json')
    payload['resolved_stages'] = _batch_stage_closure(request.stages)
    payload['factors'] = [dict(item, name=item['factor_id']) for item in payload['factors']]
    path = tmp_path / 'test.request.json'
    path.write_text(json.dumps(payload))
    calls, cohort_members = [], []
    monkeypatch.setattr(batch, 'PROJECT_ROOT', tmp_path)
    (tmp_path / 'reports/m4_runs').mkdir(parents=True)
    monkeypatch.setattr(batch, 'build_pipeline_config', lambda *args: SimpleNamespace(model_dump=lambda **kw: {}))

    def run(command, log_path):
        calls.append(command)
        if '--result' in command:
            factor_id = command[command.index('--factor-id') + 1]
            result = dict(release_id=factor_id, factor_version='1.0.0')
            if factor_id.startswith('bank-sector-'):
                result.update(summary={'valid_sessions': 1}, data_warnings=['最新日输入不足'],
                              calculation=dict(message='板块处理完成，输入不足'))
            from pathlib import Path
            Path(command[command.index('--result') + 1]).write_text(json.dumps(result))
        return True, ''

    monkeypatch.setattr(batch, '_run', run)
    monkeypatch.setattr(batch, 'publish_cohort', lambda root, ids, *args: cohort_members.extend(ids) or 'cohort')
    state = batch.run_batch(path)
    assert state['status'] == 'PASS'
    result = state['items'][-1]
    assert result['observation_level'] == 'SECTOR' and result['m4_job_id'] is None
    assert result['data_warnings'] == ['最新日输入不足']
    assert any('scripts/publish_bank_sector_factor.py' in command for command in calls)
    if stages == ['m4_5']:
        assert len(cohort_members) == 2 and not any(value.startswith('bank-sector-') for value in cohort_members)
    m4_calls = [command for command in calls if 'scripts/run_m4_pipeline.py' in command]
    assert len(m4_calls) == (0 if not stages else 3 if stages == ['m4_5'] else 2)
