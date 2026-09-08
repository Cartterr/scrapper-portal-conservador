"""Actual overview JS and local job API, isolated from production and CBRS."""
import pytest
from playwright.sync_api import sync_playwright, expect
from cbrs.account_pool import AccountPoolStore, PoolAccount, PoolConfig
from cbrs.account_pool_dashboard import start_pool_dashboard
from cbrs.config import load_settings
from cbrs.jobs import JobStore


@pytest.fixture(scope='module')
def chrome():
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome',headless=True)
        yield browser
        browser.close()


@pytest.fixture
def overview(tmp_path, chrome):
    settings=load_settings({'CBRS_PROFILE_DIR':str(tmp_path/'state'/'profile'),
                            'CBRS_OUTPUT_DIR':str(tmp_path/'outputs')},root=tmp_path)
    config=PoolConfig(accounts=(PoolAccount('test','Test'),),daily_quota_per_account=20,
        interval_minutes=0,dashboard_host='127.0.0.1',dashboard_port=0,targets=())
    store=JobStore(tmp_path/'pool.sqlite3')
    dashboard=start_pool_dashboard(AccountPoolStore(store.path),settings=settings,
        config=config,host='127.0.0.1',port=0,job_store=store)
    page=chrome.new_page()
    # External fonts/icons are irrelevant to request submission.
    page.route('**/*',lambda route: route.continue_() if route.request.url.startswith(dashboard.url)
               else route.abort())
    page.route('**/api/examples',lambda route:route.fulfill(json={'examples':[
        {'foja':12597,'numero':6347,'year':1992,'success_count':8}]}))
    page.goto(dashboard.url,wait_until='domcontentloaded')
    page.locator('#openExamples').click()
    page.locator('[data-example-foja="12597"]').click()
    expect(page.locator('#documentFoja')).to_have_value('12597')
    yield page,store
    page.close()
    dashboard.stop()


@pytest.mark.parametrize('action',['queue','instant'])
@pytest.mark.parametrize('refresh_fails',[False,True])
def test_example_submission_stays_successful_after_acceptance(overview,action,refresh_fails):
    page,store=overview
    if refresh_fails:
        page.evaluate("() => { refresh=async()=>{throw new Error('simulated refresh failure')}; }")
    page.locator(f'[data-request-action="{action}"]').click()
    expect(page.locator('#controlFeedback')).to_contain_text('Solicitud ')
    expect(page.locator('#controlFeedback')).not_to_contain_text('No se pudo')
    if refresh_fails:
        expect(page.locator('#controlFeedback')).to_contain_text('no necesitas enviarla otra vez')
    else:
        expect(page.locator('#controlFeedback')).to_contain_text('priorizada' if action=='instant' else 'agregada a la cola')
    jobs=store.list_jobs()
    assert len(jobs)==1 and jobs[0]['priority']==(1 if action=='instant' else 0)
    expect(page.locator('#documentFoja')).to_have_value('')
    for button in page.locator('[data-request-action]').all():
        expect(button).to_be_enabled()


def test_overlapping_submit_and_missing_submitter_create_one_job(overview):
    page,store=overview
    page.evaluate("""const originalFetch=window.fetch;
        window.fetch=async(...args)=>{
            const response=await originalFetch(...args);
            if(args[1]?.method==='POST') await new Promise(r=>setTimeout(r,500));
            return response;
        };
        const form=document.getElementById('requestComposer');
        form.dispatchEvent(new SubmitEvent('submit',{bubbles:true,cancelable:true}));
        form.dispatchEvent(new SubmitEvent('submit',{bubbles:true,cancelable:true}));
    """)
    for button in page.locator('[data-request-action]').all():
        expect(button).to_be_disabled()
    expect(page.locator('#controlFeedback')).to_contain_text('agregada a la cola')
    assert len(store.list_jobs())==1


def test_lost_confirmation_reuses_idempotency_key(overview):
    page,store=overview
    page.evaluate("""const originalFetch=window.fetch; let lost=false;
        window.fetch=async(...args)=>{
            const response=await originalFetch(...args);
            if(args[1]?.method==='POST'&&!lost){lost=true;throw new Error('lost response');}
            return response;
        };
    """)
    page.locator('[data-request-action="queue"]').click()
    expect(page.locator('#controlFeedback')).to_contain_text('No se pudo confirmar')
    assert len(store.list_jobs())==1
    page.locator('[data-request-action="queue"]').click()
    expect(page.locator('#controlFeedback')).to_contain_text('agregada a la cola')
    assert len(store.list_jobs())==1


def test_stale_status_is_visible_and_clears_after_recovery(overview):
    page,store=overview
    expect(page.locator('#overviewFreshness')).to_contain_text('Datos actualizados:')
    page.route('**/api/status',lambda route:route.fulfill(status=503,json={'error':'test'}))
    page.evaluate('() => refresh().catch(() => {})')
    expect(page.locator('#overviewFreshness')).to_contain_text('Vista sin actualizar')
    page.unroute('**/api/status')
    page.evaluate('() => refresh()')
    expect(page.locator('#overviewFreshness')).to_contain_text('Datos actualizados:')
    assert not store.list_jobs()
