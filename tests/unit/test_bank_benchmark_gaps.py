import hashlib
import json
from datetime import date

import duckdb
import pandas as pd
import pytest

from scripts.sync_bank_timing_benchmark import fill_gaps


def sources(root):
    (root/'config').mkdir()
    (root/'config/bank_timing_data.json').write_text('{"released_feature_end":"2025-12-31"}')
    folder=root/'data/bank_timing_store/benchmark_sources/base'
    folder.mkdir(parents=True)
    frame=pd.DataFrame(dict(session=[date(2020,6,17),date(2020,6,19)],
                            gross_total_return_index=[5161.5912,5270.5251],
                            available_at=pd.to_datetime(['2020-06-17T09:00:00Z','2020-06-19T09:00:00Z'])))
    frame.to_parquet(folder/'benchmark.parquet',index=False)
    manifest=dict(index_code='H00300',return_basis='gross_total_return_index',
                  start='2020-06-17',end='2020-06-19',retrieved_at='2026-10-09',
                  parquet_sha256=hashlib.sha256((folder/'benchmark.parquet').read_bytes()).hexdigest())
    (folder/'manifest.json').write_text(json.dumps(manifest))
    (root/'data/warehouse').mkdir()
    with duckdb.connect(str(root/'data/warehouse/alpha_research.duckdb')) as c:
        c.execute('CREATE SCHEMA research')
        c.execute("CREATE TABLE research.market_daily AS SELECT DATE '2020-06-17'+i::INT trade_date FROM range(3) t(i)")
    return folder,frame


class Response:
    def __init__(self,values):
        self.values=values

    def raise_for_status(self):
        pass

    def json(self):
        return {'data':[dict(indexCode='H00300',tradeDate=day,close=value)
                        for day,value in zip(['20200617','20200618','20200619'],self.values,strict=True)]}


def test_official_gap_merge_preserves_existing_values_and_immutable_archive(tmp_path,monkeypatch):
    source,before=sources(tmp_path)
    digest=hashlib.sha256((source/'benchmark.parquet').read_bytes()).hexdigest()
    monkeypatch.setattr('scripts.sync_bank_timing_benchmark.requests.get',
                        lambda *a,**k:Response([5161.59,5198.3,5270.53]))
    result=fill_gaps(tmp_path,date(2020,6,17),date(2020,6,19))
    after=pd.read_parquet(result['folder']+'/benchmark.parquet')
    assert after.gross_total_return_index.tolist()==[5161.5912,5198.3,5270.5251]
    assert hashlib.sha256((source/'benchmark.parquet').read_bytes()).hexdigest()==digest
    assert result['filled_sessions']==['2020-06-18']
    cached=fill_gaps(tmp_path,date(2020,6,17),date(2020,6,19))
    assert cached['cache_hit'] is True


def test_different_price_basis_is_rejected_instead_of_replacing_history(tmp_path,monkeypatch):
    sources(tmp_path)
    monkeypatch.setattr('scripts.sync_bank_timing_benchmark.requests.get',lambda *a,**k:Response([4100.,4110.,4120.]))
    with pytest.raises(ValueError,match='disagree'):
        fill_gaps(tmp_path,date(2020,6,17),date(2020,6,19))
