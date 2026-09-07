from cbrs.jobs import JobStore
from deploy.record_manual_search import record


def test_manual_search_counts_once_without_pdf_or_replay(tmp_path):
    s=JobStore(tmp_path/'pool.sqlite3')
    from cbrs.account_pool import AccountPoolStore
    AccountPoolStore(s.path)
    args=dict(account='a1',foja='3077',numero='2507',ano='1996',receipt='test',quota_date='2026-09-06')
    assert record(s,**args)
    assert not record(s,**args)
    assert s.usage_by_account('2026-09-06')['a1']==1
    assert s.summary()['artifacts']==0
    assert s.claim_next('test-worker') is None
