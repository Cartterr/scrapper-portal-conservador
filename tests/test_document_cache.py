from PIL import Image
import pytest
from cbrs import jobs
from cbrs.endurance import EnduranceController, EndurancePlan, EnduranceFixture


def test_cached_pages_survive_pdf_failure_and_need_no_network(tmp_path, monkeypatch):
    class Scraper:
        def get_image_refs(self, ticket):
            return {}, [{'pageNumber': 1, 'dataRef': 'private-ref'}]
        def download_image(self, ref, path):
            Image.new('RGB',(20,20),'white').save(path)
            return path
    item={'item_id':'item-1','sequence':1,'ticket_ref':'private-ticket','result':{}}
    original=jobs.create_pdf
    def fail(*args):
        raise RuntimeError('assembly failed')
    monkeypatch.setattr(jobs,'create_pdf',fail)
    with pytest.raises(RuntimeError):
        jobs.download_job_item(Scraper(),item,job_id='job-1',output_root=tmp_path)
    assert list(tmp_path.rglob('page_00001.jpg'))
    monkeypatch.setattr(jobs,'create_pdf',original)
    path,pages,digest,size=jobs.download_job_item(object(),item,job_id='job-1',output_root=tmp_path)
    assert path.exists() and pages==1 and size>0 and digest


def test_pending_document_blocks_next_search_and_is_revived(tmp_path):
    store=jobs.JobStore(tmp_path/'pool.sqlite3')
    controller=EnduranceController(store,EndurancePlan(True,(
        EnduranceFixture('fna',{'foja':1,'numero':2,'ano':2000},'one'),)),None)
    first=controller.maybe_enqueue(force=True)
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='failed',result_count=1,error_code='document_retrieval_deferred',updated_at='2020-01-01T00:00:00+00:00' WHERE job_id=?",(first['job_id'],))
    assert controller.maybe_enqueue(force=True) is None
    assert store.get_job(first['job_id'])['status']=='queued'


def test_document_retry_budget_terminates_without_manual_review(tmp_path):
    store=jobs.JobStore(tmp_path/'pool.sqlite3')
    controller=EnduranceController(store,EndurancePlan(True,(
        EnduranceFixture('fna',{'foja':1,'numero':2,'ano':2000},'one'),)),None)
    first=controller.maybe_enqueue(force=True)
    controller.set_paused(True)
    for attempt in range(4):
        with store.connect() as db:
            db.execute("UPDATE jobs SET status='failed',result_count=1,error_code='document_retrieval_deferred',updated_at='2020-01-01T00:00:00+00:00' WHERE job_id=?",(first['job_id'],))
        assert controller.maybe_enqueue() is None
        saved=store.get_job(first['job_id'])
        assert saved['status']==('queued' if attempt<3 else 'failed')
    assert saved['error_code']=='document_recovery_exhausted'
    assert controller.maybe_enqueue() is None  # New searches remain paused.
