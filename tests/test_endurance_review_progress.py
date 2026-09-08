from cbrs.jobs import JobStore
from cbrs.runtime_logic import finish_for_review
from cbrs.endurance import EnduranceController, EndurancePlan, EnduranceFixture


def test_unknown_job_stays_pending_without_certifying_failure(tmp_path):
    store=JobStore(tmp_path/'pool.sqlite3')
    controller=EnduranceController(store,EndurancePlan(True,(
        EnduranceFixture('fna',{'foja':1,'numero':2,'ano':2000},'one'),
        EnduranceFixture('fna',{'foja':3,'numero':4,'ano':2001},'two'))),None)
    first=controller.maybe_enqueue(force=True)
    assert not finish_for_review(store,first['job_id'],'search_outcome_unknown')
    old=store.get_job(first['job_id'])
    assert old['status']=='waiting_capacity' and old['finished_at'] is None
    assert controller.maybe_enqueue() is None  # Retains normal pacing.
    assert controller.maybe_enqueue(force=True) is None
    assert store.summary()['artifacts']==0


def test_in_flight_owner_operation_cannot_be_quarantined(tmp_path):
    store=JobStore(tmp_path/'pool.sqlite3')
    job,_=store.create_job(kind='fna',input_data={'foja':1,'numero':2,'ano':2000})
    assert store.acquire_lease('browser_operation:'+job['job_id'],'owner')
    assert not finish_for_review(store,job['job_id'],'search_outcome_unknown')
    assert store.get_job(job['job_id'])['finished_at'] is None
