import numpy as np
import pandas as pd

from scripts.research.research_bank_dividend_entry import capital_path,lagged_rates,stability


def test_rates_are_strictly_before_signal_and_stale_values_stay_unknown():
    rates=pd.DataFrame({'observation_date':pd.to_datetime(['2020-01-01','2020-01-02']), 'cgb10y':[.03,.09]})
    out=lagged_rates(rates,pd.to_datetime(['2020-01-02','2020-01-03','2020-01-20']))
    assert out.cgb10y.iloc[0]==.03 and out.cgb10y.iloc[1]==.09
    assert pd.isna(out.cgb10y.iloc[2])


def test_split_budget_never_invents_capital_and_uses_same_exit():
    values=np.full(80,10.)
    values[21:]=20
    trend=np.zeros(80)
    a=capital_path(values,0,60,'immediate',trend)
    b=capital_path(values,0,60,'split3',trend)
    assert a['capital_return']==1
    assert abs(b['capital_return']-1/3)<1e-12
    assert b['average_invested_fraction']<a['average_invested_fraction']


def test_confirmation_is_filled_next_close_and_no_confirmation_is_kept_as_cash():
    values=np.arange(80.)+10
    trend=np.zeros(80);trend[5]=1
    a=capital_path(values,0,60,'confirm20',trend)
    assert a['first_fill_offset']==6
    assert a['capital_return']==values[61]/values[6]-1
    trend[:]=0
    b=capital_path(values,0,60,'confirm20',trend)
    assert b['capital_return']==0 and not b['filled'] and b['drawdown']==0
    assert capital_path(values,70,60,'immediate',trend) is None


def test_cash_breadth_normalizes_stock_distributions_and_does_not_sum_per_share_units():
    calendar=pd.bdate_range('2020-01-01',periods=253)
    panel=pd.DataFrame([{'session':day,'ts_code':str(i),'cash_per_current_share_cycle':
                         (.5 if i==0 and day==calendar[-1] else 1.)}
                        for day in calendar for i in range(10)])
    events=pd.DataFrame([{'ts_code':'0','ex_date':calendar[-1],'imp_ann_date':calendar[-2],'stk_div':1.}])
    result=stability(panel,events,calendar)
    assert pd.isna(result.declining_cash_fraction.iloc[251])
    assert result.declining_cash_fraction.iloc[-1]==0
    assert result.median_cash_growth.iloc[-1]==0
    assert result.matched_cash_banks.iloc[-1]==10
