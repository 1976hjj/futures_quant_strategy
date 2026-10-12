from datetime import date

import pytest

from alpha_research_os.factors.bank import bank_factor_catalog, dividend_cycle
from alpha_research_os.factors.bank_timing import bank_timing_catalog


def event(period, day, amount, stock=0, announcement=None):
    return dict(end_date=date.fromisoformat(period),ex_date=date.fromisoformat(day),cash_div_tax=amount,
                stk_div=stock,imp_ann_date=date.fromisoformat(announcement or day)-__import__('datetime').timedelta(days=1))


def test_annual_once_never_adds_two_years_or_doubles_amount():
    ev=[event('2022-12-31','2023-07-10',1.8),event('2023-12-31','2024-07-20',2)]
    assert dividend_cycle(ev,date(2024,7,19)).cash==1.8
    assert dividend_cycle(ev,date(2024,7,20)).cash==2
    assert dividend_cycle(ev,date(2025,7,25)).cash==2  # older than365, not dropped.


def test_payment_shift_does_not_create_three_dividends():
    ev=[event('2023-06-30','2023-11-10',.12),event('2023-12-31','2024-07-15',.18),
        event('2024-06-30','2024-10-10',.14)]
    got=dividend_cycle(ev,date(2024,10,11))
    assert got.cash==pytest.approx(.32)  # new interim + previous final, not .44.
    assert got.event_count==2 and got.period_end==date(2024,6,30)


def test_split_year_final_combines_same_fiscal_year():
    ev=[event('2023-12-31','2024-07-16',.3064),event('2024-06-30','2025-01-07',.1434),
        event('2024-12-31','2025-07-14',.1646),event('2025-06-30','2025-12-15',.1414)]
    # First ever interim: old full year + new half year would cover18months.
    transition=dividend_cycle(ev,date(2025,2,1))
    assert transition.cash==.3064
    assert transition.status.startswith('FREQUENCY_TRANSITION')
    assert dividend_cycle(ev,date(2025,8,1)).cash==pytest.approx(.3080)
    assert dividend_cycle(ev,date(2025,12,16)).cash==pytest.approx(.3060)


def test_missing_interim_is_not_invented_or_called_cancelled():
    ev=[event('2023-06-30','2023-11-10',.1),event('2023-12-31','2024-07-15',.2),
        event('2024-12-31','2025-07-15',.18)]
    got=dividend_cycle(ev,date(2025,7,16))
    assert got.cash is None and got.status=='EXPECTED_INTERIM_MISSING_NOT_PROVEN_CANCELLED'


def test_explicit_zero_is_zero_but_empty_archive_is_unknown():
    assert dividend_cycle([event('2023-12-31','2024-07-10',0)],date(2024,8,1)).cash==0
    assert dividend_cycle([],date(2024,8,1)).cash is None
    assert dividend_cycle([],date(2024,8,1),False).cash is None


def test_future_and_not_yet_implemented_dividends_do_not_change_reference():
    ev=[event('2023-12-31','2024-07-10',2)]
    newer=event('2024-12-31','2025-07-10',200)
    assert dividend_cycle(ev+[newer],date(2024,8,1))==dividend_cycle(ev,date(2024,8,1))
    delayed=event('2023-12-31','2024-07-10',9,announcement='2024-08-10')
    assert dividend_cycle([delayed],date(2024,8,1)).cash is None


def test_same_share_basis_stock_distribution_and_conflict():
    ev=[event('2023-06-30','2023-11-10',.5),event('2023-12-31','2024-07-10',1,.2)]
    assert dividend_cycle(ev,date(2024,8,1)).cash==pytest.approx(1.5/1.2)
    ev[-1]['event_conflict']=True
    assert dividend_cycle(ev,date(2024,8,1)).cash is None


def test_nonstandard_period_and_duplicate_payment_require_review():
    ev=[event('2016-12-31','2017-07-10',.5),event('2017-11-09','2017-11-16',4.6)]
    assert dividend_cycle(ev,date(2017,11,17)).status=='NONSTANDARD_DIVIDEND_PERIOD_REQUIRES_REVIEW'
    same=[event('2023-12-31','2024-07-10',2),event('2023-12-31','2024-08-10',2)]
    assert dividend_cycle(same,date(2024,9,1)).cash is None


def test_no_yield_range_clipping_and_stale_reference_flag():
    ev=[event('2023-12-31','2024-07-10',1)]
    cash=dividend_cycle(ev,date(2024,8,1)).cash
    assert cash/12.5==.08 and cash/50==.02
    assert dividend_cycle(ev,date(2026,3,1)).status=='STALE_CYCLE_REQUIRES_DISCLOSURE_REVIEW'


def test_versions_and_dependencies_identify_changed_formula():
    yields=[f for f in bank_factor_catalog() if f.field in ('cash_yield_cycle','gated_cash_yield_cycle')]
    assert len(yields)==2 and all(f.factor_version=='2.0.0' for f in yields)
    sector=[f for f in bank_timing_catalog() if 'cash_yield_cycle' in f.required_fields]
    assert len(sector)==4
    assert next(f for f in sector if f.factor_id.endswith('dividend-history-percentile')).factor_version=='2.0.0'
    assert all('cash_yield365' not in f.required_fields for f in sector)
