"""Leakage, timing, decomposition and dependence checks for research script."""
import math

import numpy as np
import pandas as pd

from scripts.research.research_bank_dividend_rebound import decomposition, labels, past_rank, regression


def test_rank_excludes_current_and_future_and_handles_ties():
    dates = pd.date_range("2020-01-01", periods=8)
    a = pd.Series([1., 1., 3., 2., 4., 8., 9., 10.], index=dates)
    got = past_rank(a, window=4, minimum=3)
    assert np.isnan(got.iloc[2])
    assert got.iloc[3] == 2/3
    changed = a.copy(); changed.iloc[5:] = -999
    pd.testing.assert_series_equal(got.iloc[:5], past_rank(changed,4,3).iloc[:5])
    a.iloc[3] = 1
    assert past_rank(a,4,3).iloc[3] == 1/3


def test_label_next_close_tail_and_segment_break():
    sessions = pd.bdate_range("2025-12-23", periods=6)
    levels = [10.,20.,30.,40.,50.,60.]
    good = labels(levels,sessions,horizons=[2])
    assert good.iloc[0]["return"] == 1.0  # 40/20, never 40/10.
    assert good.iloc[0].entry == sessions[1]
    assert good.iloc[0].exit == sessions[3]
    assert good.iloc[-1].status == "TAIL"
    broken = labels(levels,sessions,horizons=[2],segments=[0,0,0,1,1,1])
    assert broken.iloc[0].status == "PATH_BREAK"
    assert np.isnan(broken.iloc[0]["return"])
    invalid = labels(levels,sessions,horizons=[2],ready=[True,True,False,True,True,True])
    assert invalid.iloc[0].status == "PATH_BREAK"


def test_numerator_denominator_identity_including_stock_distribution():
    # Historical cash and price each divided by 2 after a 1:1 stock distribution.
    p,d = decomposition(np.array([.55,.0]),np.array([5.,3.]),np.array([1.,.2])/2,np.array([12.,6.])/2)
    actual = np.array([.55/5.,0.])-np.array([1/12.,.2/6.])
    np.testing.assert_allclose(p+d,actual,atol=1e-14)


def test_hac_independent_scalar_reference_preserves_calendar_gaps():
    rng = np.random.default_rng(716)
    n=260; lag=8
    dates=pd.bdate_range("2020-01-01",periods=n)
    f=pd.DataFrame(index=dates)
    signal=rng.integers(0,2,size=n).astype(float)
    outcome=.03*signal+rng.normal(0,.02,n)
    signal[70:91]=np.nan
    result=regression(signal,outcome,f,lag,False)
    valid=np.isfinite(signal)
    x=np.column_stack([np.ones(n),np.nan_to_num(signal)])
    beta=np.linalg.lstsq(x[valid],outcome[valid],rcond=None)[0]
    # Scalar influence function, independently summing each day and lag.
    inv=np.linalg.inv(x[valid].T@x[valid])
    influence=np.zeros(n)
    for i in np.flatnonzero(valid):
        influence[i]=float(inv[1]@x[i])*(outcome[i]-x[i]@beta)
    variance=sum(v*v for v in influence)
    for k in range(1,lag+1):
        variance+=2*(1-k/(lag+1))*sum(influence[i]*influence[i-k] for i in range(k,n))
    expected_se=math.sqrt(variance*valid.sum()/(valid.sum()-2))
    np.testing.assert_allclose(result["coefficient"],beta[1],rtol=1e-12)
    np.testing.assert_allclose(result["se"],expected_se,rtol=1e-12)


def test_year_controls_after_warmup_do_not_make_design_singular():
    dates=pd.bdate_range("2016-01-01", "2020-12-31")
    rng=np.random.default_rng(19)
    f=pd.DataFrame({"past_return60":rng.normal(size=len(dates)),"past_vol60":rng.uniform(.01,.04,len(dates))},index=dates)
    x=rng.integers(0,2,len(dates)).astype(float)
    x[dates.year<2018]=np.nan
    y=rng.normal(size=len(dates))
    result=regression(x,y,f,20,True)
    assert result["p_value"] is not None
