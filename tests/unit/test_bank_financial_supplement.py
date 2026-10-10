from datetime import date

import pytest

from scripts.supplement_bank_financial_facts import extract_cell, supplement


def test_combined_basic_eps_and_previous_page_header():
    pages = ['项目 2024年 2023年 2022年\n营业收入 100 90 80',
             '2024年年度报告\n基本/稀释每股收益 2.15 2.25 2.20\n'
             '扣除非经常性损益后的基本/稀释每股收益 2.16 2.25 2.19']
    result = extract_cell(pages, 2024, 'ordinary_eps_basic', 2.15)
    assert result['value'] == 2.15
    assert result['previous_value'] == 2.25
    assert result['pdf_page'] == 2


def test_footnote_marker_and_split_basic_eps_label():
    pages = ['项目 2024年 2023年\n归属于公司股东的基\n本每股收益①\n0.44 0.43\n'
             '稀释每股收益 0.40 0.39']
    result = extract_cell(pages, 2024, 'ordinary_eps_basic', .44)
    assert result['value'] == .44


def test_spaced_footnote_before_next_numeric_row():
    pages = ['项目 2022年 2021年\n基本及稀释每股收益 4\n1.14 1.10 3.64\n'
             '4. 按照每股收益计算和披露规定计算。']
    assert extract_cell(pages, 2022, 'ordinary_eps_basic', 1.14)['value'] == 1.14


def test_single_year_interest_table_with_spaced_year_header():
    pages = ['项目\n2023 年\n平均余额 利息收支 平均利率',
             '生息资产\n贷款 100 4 4%\n利息净收入 2\n净息差 1.81%']
    assert extract_cell(pages, 2023, 'net_interest_margin', 1.81)['value'] == 1.81


def test_prior_year_first_header_and_later_comparative_are_not_backfilled():
    pages = ['项目 2023年 2024年\n基本每股收益 0.88 0.98']
    assert extract_cell(pages, 2024, 'ordinary_eps_basic', .98) is None
    later = ['项目 2025年 2024年\n基本每股收益 1.01 0.98']
    assert extract_cell(later, 2024, 'ordinary_eps_basic', .98) is None


def test_provider_candidate_mismatch_and_nonordinary_equity_are_rejected():
    pages = ['项目 2024年 2023年\n基本每股收益 1.31 1.30\n每股净资产 12.65 11.8']
    assert extract_cell(pages, 2024, 'ordinary_eps_basic', 1.40) is None
    assert extract_cell(pages, 2024, 'common_bvps', 12.65) is None


def test_profit_uses_explicit_units_and_current_report_comparative():
    pages = ['单位:人民币千元\n项目 2024年 2023年\n归属于上市公司股东的净利润 2,036,812 1,888,085']
    cell = extract_cell(pages, 2024, 'parent_profit', 2036812000.)
    assert cell['value'] == 2036812000.
    assert cell['previous_value'] == 1888085000.
    assert cell['original_display_unit_factor'] == 1000.


def test_regulatory_threshold_is_not_mistaken_for_a_ratio():
    pages = ['项目 监管标准 2024年 2023年\n不良贷款率(%) ≤5 1.83 1.73']
    assert extract_cell(pages, 2024, 'npl_ratio', 1.83)['value'] == 1.83


def test_provision_coverage_uses_original_loan_allowance_definition():
    pages = ['资产质量指标 项目 2024年 2023年\n'
             '贷款减值准备对不良贷款比率 186.96 173.51 上升13.45个百分点\n'
             '贷款减值准备对贷款总额比率 2.54 2.57']
    cell = extract_cell(pages, 2024, 'provision_coverage_ratio', 186.96)
    assert cell['value'] == 186.96
    assert cell['previous_value'] == 173.51


def test_relative_capital_header_uses_current_group_column():
    pages = ['2024 年年度报告\n报告期末 上年末\n项目 本集团 本行 本集团 本行\n'
             '核心一级资本充足率(%) 8.92 8.49 8.97 8.51']
    assert extract_cell(pages, 2024, 'cet1_ratio', 8.92)['value'] == 8.92
    assert extract_cell(pages, 2024, 'cet1_ratio', 8.49) is None
    assert extract_cell(pages, 2023, 'cet1_ratio', 8.92) is None


def test_visually_reviewed_scanned_header_still_requires_report_year_and_cell_value():
    pages = ['2023年年度报告\n2022年 本期比上年\n项目 2023年 同期增减 2021年\n'
             '调整前 调整后 调整后\n加权平均净资产收益率 5.98 5.77 5.77 0.21 5.76']
    assert extract_cell(pages, 2023, 'roe_weighted', 5.98) is None
    assert extract_cell(pages, 2023, 'roe_weighted', 5.98, reviewed_pages=(1,))['value'] == 5.98
    assert extract_cell(pages, 2023, 'roe_weighted', 5.77, reviewed_pages=(1,)) is None
    assert extract_cell(pages, 2022, 'roe_weighted', 5.98, reviewed_pages=(1,)) is None


def test_reviewed_continued_capital_table_is_bound_to_cover_year():
    pages = ['2023年度报告', '报告期末 上年末\n项目 本集团 本行 本集团 本行',
             '核心一级资本充足率(%) 8.97 8.51 9.19 8.79']
    assert extract_cell(pages, 2023, 'cet1_ratio', 8.97) is None
    assert extract_cell(pages, 2023, 'cet1_ratio', 8.97, reviewed_pages=(3,))['value'] == 8.97
    assert extract_cell(pages, 2023, 'cet1_ratio', 8.51, reviewed_pages=(3,)) is None
    assert extract_cell(pages, 2022, 'cet1_ratio', 8.97, reviewed_pages=(3,)) is None
    pages[0] = '公司2023年度分别按照企业会计准则编制财务报表'
    assert extract_cell(pages, 2023, 'cet1_ratio', 8.97, reviewed_pages=(3,))['value'] == 8.97
    assert extract_cell(pages, 2022, 'cet1_ratio', 8.97, reviewed_pages=(3,)) is None


def test_supplement_rejects_holdout_before_loading_any_sources(tmp_path):
    (tmp_path/'config').mkdir()
    (tmp_path/'config/bank_timing_data.json').write_text('{"released_feature_end":"2025-12-31"}')
    with pytest.raises(ValueError, match='未释放'):
        supplement(tmp_path, tmp_path/'absent.json', date(2026,1,1))
