import json
from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from alpha_research_os.data.bank_sector_indicators import calculate_sector_indicators, historical_percentiles
from alpha_research_os.factors.bank_timing import bank_timing_catalog
from scripts.bank_processing import ProcessingRequest, execute, processing_plan, table_page, write_asset
from scripts.bank_processing_api import BankProcessingManager
from scripts.build_bank_timing_data import file_hash


def settings(root):
    (root / 'config').mkdir()
    (root / 'config/bank_timing_data.json').write_text(json.dumps({
        'start': '2020-01-02', 'released_feature_end': '2025-12-31', 'historical_grade': 'RESEARCH_ONLY',
        'frozen_bank_input': 'missing_source_must_not_be_read',
    }))


def test_holdout_range_is_rejected_before_source_access(tmp_path):
    settings(tmp_path)
    with pytest.raises(ValueError, match='holdout'):
        processing_plan(tmp_path, ProcessingRequest(stage='STANDARD', end=date(2026, 6, 30)))


def test_benchmark_source_change_invalidates_derived_processing_cache(tmp_path):
    settings(tmp_path)
    _, standard = write_asset(tmp_path, 'STANDARD', 'sha256:'+'c'*64, {},
                              {'start':'2020-01-02','end':'2025-12-31'})
    request = ProcessingRequest(stage='DERIVED', source_id=standard['asset_id'])
    before = processing_plan(tmp_path, request)
    folder = tmp_path/'data/bank_timing_store/benchmark_sources/source'
    folder.mkdir(parents=True)
    (folder/'manifest.json').write_text(json.dumps(dict(index_code='H00300',
        return_basis='gross_total_return_index',start='2019-01-01',end='2025-12-31',retrieved_at='2026-10-10')))
    after = processing_plan(tmp_path, request)
    assert before['processing_key'] != after['processing_key']


@pytest.mark.parametrize('payload', [dict(stage='OTHER'), dict(stage='DERIVED', source_id='../escape'),
                                   dict(stage='STANDARD', factor_ids=['bank-sector-relative-strength']),
                                   dict(stage='INDICATORS', factor_ids=['unknown'])])
def test_processing_contract_rejects_bad_stage_paths_and_indicator_choices(payload):
    with pytest.raises(ValidationError):
        ProcessingRequest(**payload)


def test_percentiles_use_midrank_current_history_and_consume_calendar_gaps():
    days = [date(2024, 4, day) for day in range(1, 6)]
    panel = pd.DataFrame([{'session': day, 'instrument_id': 'A', 'book_to_price': 1 / value,
                           'cash_yield365': value, 'annual_earnings_yield': value}
                          for day, value in zip([days[0], days[1], days[3], days[4]], [1, 1, 2, 99], strict=True)])
    output = historical_percentiles(panel, days, window=3, minimum=2)
    assert pd.isna(output.pb_percentile.iloc[0])
    assert output.pb_percentile.iloc[1] == .5
    assert pd.isna(output.pb_percentile.iloc[2])
    assert output.pb_percentile.iloc[3] == .75
    prefix = historical_percentiles(panel[panel.session < days[4]], days[:-1], window=3, minimum=2)
    pd.testing.assert_series_equal(output.pb_percentile.iloc[:4].reset_index(drop=True), prefix.pb_percentile)


def test_gap_pagination_count_filter_and_immutable_hash_validation(tmp_path):
    settings(tmp_path)
    rows = pd.DataFrame([dict(session=date(2025, 12, 31), instrument_id=f'BANK-{i:02}', field='book_to_price',
                              priority=1, gap_status='MISSING') for i in range(23)]
                        + [dict(session=date(2025, 12, 31), instrument_id='OTHER', field='nim_change',
                                priority=2, gap_status='MISSING')])
    folder, manifest = write_asset(tmp_path, 'DERIVED', 'sha256:' + 'a' * 64, {'latest_input_gaps': rows},
                                   {'start': '2020-01-02', 'end': '2025-12-31'})
    page = table_page(tmp_path, 'DERIVED', manifest['asset_id'], page=2, page_size=20, field='book_to_price')
    assert page['totalItems'] == 23 and page['totalPages'] == 2
    assert [item['instrument_id'] for item in page['items']] == ['BANK-20', 'BANK-21', 'BANK-22']
    assert table_page(tmp_path, 'DERIVED', manifest['asset_id'], page=3, field='book_to_price')['items'] == []
    with pytest.raises(ValueError, match='分页'):
        table_page(tmp_path, 'DERIVED', page_size=101)
    (folder / 'latest_input_gaps.parquet').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='完整性'):
        table_page(tmp_path, 'DERIVED', manifest['asset_id'])


def test_changed_preflight_is_rejected_without_launching_worker(tmp_path, monkeypatch):
    manager = BankProcessingManager(tmp_path)
    monkeypatch.setattr('scripts.bank_processing_api.processing_plan', lambda *args: {'processing_key': 'new'})
    with pytest.raises(RuntimeError, match='过期'):
        manager.start(dict(stage='STANDARD', expected_processing_key='old'))
    assert not list(manager.run_root.glob('*.request.json'))


def test_sector_release_uses_pre_start_history_and_supports_result_reuse(tmp_path):
    settings(tmp_path)
    days = [date(2024, 4, day) for day in range(1, 5)]
    history = pd.DataFrame([dict(session=day, instrument_id='A', book_to_price=2., cash_yield365=.05,
                                annual_earnings_yield=.1, quality_gate=1, nim_change=-.1,
                                bank_total_return_index=100.) for day in days])
    technical = history[['session', 'instrument_id', 'bank_total_return_index']].assign(total_return_segment=0)
    basket = pd.DataFrame(dict(session=days, basket_segment=0, bank_equal_total_return_index=100.))
    benchmark = pd.DataFrame(dict(session=days, benchmark_segment=0, gross_total_return_index=100.))
    definition = next(item for item in bank_timing_catalog() if item.factor_id == 'bank-sector-pb-history-percentile')
    readiness = pd.DataFrame([dict(session=day, factor_id=definition.factor_id,
                                  factor_version=definition.factor_version, data_ready=True,
                                  coverage=1., valid_count=1, universe_count=1, reason='') for day in days])
    folder, manifest = write_asset(tmp_path, 'DERIVED', 'sha256:' + 'b' * 64,
                                  dict(bank_history_panel=history, stock_total_return_inputs=technical,
                                       bank_pb_history=history[['session', 'instrument_id']].assign(pb_daily=.5),
                                       bank_basket_history=basket, benchmark_daily=benchmark,
                                       indicator_input_readiness=readiness),
                                  dict(start=str(days[0]), end=str(days[-1])))
    spec = folder / 'build_spec.json'
    spec.write_text(json.dumps(dict(configuration=dict(history_sessions=3, minimum_history_sessions=2,
                                                       trend_sessions=2, relative_strength_sessions=1))))
    manifest['files'][spec.name] = file_hash(spec)
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    request = ProcessingRequest(stage='INDICATORS', start=days[1], end=days[-1],
                                source_id=manifest['asset_id'], factor_ids=[definition.factor_id])
    output = execute(tmp_path, request, lambda *args: None)
    page = table_page(tmp_path, 'INDICATORS', output['asset_id'])
    assert page['totalItems'] == 3
    assert all(item['value'] == .5 for item in page['items'])
    assert output['cache_hit'] is False
    cached = execute(tmp_path, request, lambda *args: None)
    assert cached['asset_id'] == output['asset_id'] and cached['cache_hit'] is True


def test_sector_formulas_and_invalid_relative_endpoints_preserve_missing_values():
    days = [date(2024, 4, day) for day in range(1, 5)]
    history = pd.DataFrame([dict(session=day, instrument_id='A', book_to_price=2., cash_yield365=.05,
                                annual_earnings_yield=.1, quality_gate=1, nim_change=-.1, profit_growth=-.1,
                                npl_improvement=-.1, provision_change_yoy=-.1, cet1_change_yoy=-.1,
                                bank_total_return_index=100.) for day in days])
    technical = history[['session', 'instrument_id', 'bank_total_return_index']].assign(total_return_segment=0)
    basket = pd.DataFrame(dict(session=days, basket_segment=[0, 0, 1, 1],
                                bank_equal_total_return_index=[100., 110., None, 121.]))
    benchmark = pd.DataFrame(dict(session=days, benchmark_segment=[0, 0, 0, 0],
                                   gross_total_return_index=[100., 101., 102., 103.]))
    readiness = pd.DataFrame([dict(session=day, factor_id=item.factor_id, factor_version=item.factor_version,
                                  data_ready=True, coverage=1., valid_count=1, universe_count=1, reason='')
                             for day in [days[1], days[3]] for item in bank_timing_catalog()])
    result = calculate_sector_indicators(history, technical, basket, benchmark, readiness, days,
                                         dict(history_sessions=3, minimum_history_sessions=2,
                                              trend_sessions=2, relative_strength_sessions=1),
                                         pb_history=history[['session', 'instrument_id']].assign(pb_daily=.5))
    first = result[result.session == days[1]].set_index('factor_id')
    assert first.loc['bank-sector-pb-history-percentile', 'value'] == .5
    assert first.loc['bank-sector-cheap-high-yield-breadth', 'value'] == 0
    assert first.loc['bank-sector-operating-deterioration', 'value'] == 1
    assert first.loc['bank-sector-relative-strength', 'value'] == pytest.approx(.09)
    last = result[(result.session == days[3]) & (result.factor_id == 'bank-sector-relative-strength')].iloc[0]
    assert pd.isna(last.value) and last.status == 'INSUFFICIENT_DATA'


def test_new_pb_sector_cannot_silently_use_legacy_common_equity_inputs():
    readiness = pd.DataFrame([dict(factor_id='bank-sector-pb-history-percentile')])
    with pytest.raises(ValueError, match='日PB历史'):
        calculate_sector_indicators(None, None, None, None, readiness, [], {},
                                    selected=['bank-sector-pb-history-percentile'])


def test_dividend_sector_ranks_pre_start_warmup_and_excludes_future():
    days = [date(2024, 4, day) for day in range(1, 6)]
    valuation = pd.DataFrame([dict(session=day, instrument_id='A', book_to_price=2.,
                                  cash_yield365=value, annual_earnings_yield=.1)
                             for day, value in zip(days, [.01, .02, .03, .04, 999.], strict=True)])
    history = valuation[valuation.session == days[3]].assign(bank_total_return_index=100.)
    technical = history[['session', 'instrument_id', 'bank_total_return_index']].assign(total_return_segment=0)
    basket = pd.DataFrame(dict(session=[days[3]], basket_segment=0, bank_equal_total_return_index=100.))
    benchmark = pd.DataFrame(dict(session=[days[3]], benchmark_segment=0, gross_total_return_index=100.))
    readiness = pd.DataFrame([dict(session=days[3], factor_id='bank-sector-dividend-history-percentile',
                                   factor_version='1.0.0', data_ready=True, coverage=1., valid_count=1,
                                   universe_count=1, reason='')])
    output = calculate_sector_indicators(history, technical, basket, benchmark, readiness, [days[3]],
                                         dict(history_sessions=4, minimum_history_sessions=4,
                                              trend_sessions=1, relative_strength_sessions=1),
                                         valuation_history=valuation)
    assert output.value.iloc[0] == .875
