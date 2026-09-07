"""Versioned job logic. Durable state and browser ownership remain in core."""
from __future__ import annotations
from cbrs import jobs as core
from .runtime_updates import runtime_module

def process_job(job: Job, *, settings: Settings, config: PoolConfig, store: JobStore, pool_store: AccountPoolStore, run_id: str, browser_pool: _PersistentAccountBrowsers, preflight_runner: Callable[..., Any], proxy_health_runner: Callable[..., Any], endurance_plan: Any | None=None) -> str:
    target_account_id = str(job.input.get('target_account_id') or '') if job.source == 'captcha_validation' else ''
    excluded: set[str] = {account.account_id for account in config.accounts if account.account_id != target_account_id} if target_account_id else set()
    quota_date = core.local_today()
    while True:
        if store.cancel_requested(job.job_id):
            return store.finalize_job(job.job_id)
        checkpoint = store.search_checkpoint(job.job_id)
        quota_policy = runtime_module('form_search')
        if not checkpoint['saved']:
            excluded.update(a.account_id for a in config.accounts
                            if (quota_policy.quota_hold(store.path, a.account_id) or {}).get('blocked'))
        if checkpoint['incomplete_receipt'] or checkpoint['uncertain']:
            store.set_waiting(job.job_id, 'waiting_capacity', reason='search_outcome_unknown' if checkpoint['uncertain'] else 'search_receipt_incomplete')
            return 'waiting_capacity'
        if checkpoint['saved']:
            if checkpoint['result_count'] == 0:
                return store.finalize_job(job.job_id)
            if not checkpoint['account_id']:
                store.set_waiting(job.job_id, 'waiting_capacity', reason='search_owner_unknown')
                return 'waiting_capacity'
            excluded.update((account.account_id for account in config.accounts if account.account_id != checkpoint['account_id']))
        account = store.select_account(run_id=run_id, config=config, quota_date=quota_date, excluded=excluded, source=job.source, require_search_capacity=not checkpoint['saved'], source_quota_by_account=endurance_plan.source_quota(config) if job.source == 'endurance' and endurance_plan is not None else None)
        if account is None:
            if target_account_id:
                target_state = next((str(row['status']) for row in pool_store.accounts(run_id) if str(row['account_id']) == target_account_id), 'paused')
                status = 'waiting_captcha' if target_state == core.CAPTCHA_PENDING_STATUS else 'waiting_capacity'
            else:
                status = core._unavailable_job_status(pool_store, run_id, config, excluded)
            store.set_waiting(job.job_id, status, reason=status)
            pool_store.update_run(run_id, status=status, next_cycle_at=core.next_quota_reset_at() if status == 'waiting_capacity' else '', blocked_reason=status)
            return status
        excluded.add(account.account_id)
        try:
            runtime_settings = core._runtime_account_settings(settings, account, store)
        except ValueError as exc:
            pool_store.pause_account(run_id, account.account_id, reason='account_configuration_invalid')
            store.add_event('account_configuration_invalid', job_id=job.job_id, account_id=account.account_id, level='error', data={'error': str(exc)})
            continue
        if not core._ensure_account_gate(account, runtime_settings, store, pool_store, run_id, preflight_runner, proxy_health_runner):
            continue
        try:
            username, password = core.account_credentials(account)
        except ValueError as exc:
            pool_store.pause_account(run_id, account.account_id, reason='credentials_missing')
            store.add_event('account_credentials_missing', job_id=job.job_id, account_id=account.account_id, level='error', data={'error': str(exc)})
            continue
        attempt_id: str | None = None
        try:
            with browser_pool.session(account.account_id, runtime_settings, username, password) as scraper:
                if hasattr(scraper, 'set_job_context'):
                    scraper.set_job_context(job.job_id)
                store.set_account_check(account.account_id, session_checked=True)
                items = store.items(job.job_id, public=False)
                if not checkpoint['saved']:
                    attempt_id = store.begin_attempt(job_id=job.job_id, account_id=account.account_id, quota_date=quota_date, quota=config.quota_for(account), run_id=run_id, consume_quota=True)
                    if not attempt_id:
                        continue
                    try:
                        if job.kind != 'fna' and not quota_policy.admit_quota_check(store.path, account.account_id):
                            raise core.SafetyStopException(core.StopReason.DAILY_LIMIT, 'Portal quota hold remains active')
                        results = core._search_job(scraper, job)
                    except core.SafetyStopException as exc:
                        if exc.reason == core.StopReason.AUTH_REQUIRED:
                            store.finish_attempt(attempt_id, status='auth_expired', safety_stop=exc.reason.value, error=str(exc))
                            with browser_pool.session(account.account_id, runtime_settings, username, password, force=True) as scraper:
                                pass
                            attempt_id = store.begin_attempt(job_id=job.job_id, account_id=account.account_id, quota_date=quota_date, quota=config.quota_for(account), run_id=run_id, consume_quota=True)
                            if not attempt_id:
                                continue
                            results = core._search_job(scraper, job)
                        else:
                            raise
                    store.clear_external_outage_backoff()
                    quota_policy.clear_quota_hold(store.path, account.account_id)
                    items = store.add_results(job.job_id, results, attempt_id=attempt_id, materialize_items=not (job.source == 'captcha_validation' and job.input.get('validation_only')))
                    attempt_id = None
                    if job.source == 'captcha_validation' and job.input.get('validation_only'):
                        from .captcha_budget import CaptchaBudgetStore
                        CaptchaBudgetStore(settings.captcha_state_path, daily_limit=settings.two_captcha_daily_limit, circuit_seconds=settings.two_captcha_circuit_breaker_seconds, rejection_cooldown_seconds=settings.two_captcha_rejection_cooldown_seconds).finish_manual_authorization(account_id=account.account_id, status='not_required', reason='browser_token_accepted')
                        store.add_event('captcha_validation_completed', job_id=job.job_id, account_id=account.account_id, data={'result_count': len(results)})
                        return store.finalize_job(job.job_id)
                else:
                    attempt_id = store.begin_attempt(job_id=job.job_id, account_id=account.account_id, quota_date=quota_date, quota=config.quota_for(account), run_id=run_id, consume_quota=False)
                for item in items:
                    if item['status'] == 'completed':
                        continue
                    if store.cancel_requested(job.job_id):
                        if attempt_id:
                            store.finish_attempt(attempt_id, status='cancelled')
                        return store.finalize_job(job.job_id)
                    sample_pages = int(job.input.get('sample_pages') or 0) or None if job.source == 'endurance' else None
                    final_path = core._expected_artifact_path(settings.output_dir, job.job_id, item, sample_pages=sample_pages)
                    store.mark_item_downloading(str(item['item_id']), final_path)
                    try:
                        if final_path.exists():
                            expected_pages = int(item.get('expected_pages') or 1)
                            sha256, size = core.validate_pdf(final_path, expected_pages=expected_pages)
                            page_count = expected_pages
                        else:
                            try:
                                final_path, page_count, sha256, size = core.download_job_item(scraper, item, job_id=job.job_id, output_root=settings.output_dir, sample_pages=sample_pages, on_expected_pages=lambda count: store.set_item_expected_pages(str(item['item_id']), count))
                            except core.SafetyStopException as exc:
                                if exc.reason != core.StopReason.AUTH_REQUIRED:
                                    raise
                                with browser_pool.session(account.account_id, runtime_settings, username, password, force=True) as scraper:
                                    pass
                                final_path, page_count, sha256, size = core.download_job_item(scraper, item, job_id=job.job_id, output_root=settings.output_dir, sample_pages=sample_pages, on_expected_pages=lambda count: store.set_item_expected_pages(str(item['item_id']), count))
                        store.complete_item(str(item['item_id']), expected_pages=page_count, output_path=final_path, sha256=sha256, bytes_count=size)
                    except core.SafetyStopException:
                        raise
                    except Exception as exc:
                        core.capture_error(store, account.account_id, getattr(scraper, 'browser', scraper), exc)
                        if core._looks_like_connection_failure(exc):
                            raise
                        store.fail_item(str(item['item_id']), code='download_failed', message=str(exc))
                if attempt_id:
                    store.finish_attempt(attempt_id, status='completed')
                return store.finalize_job(job.job_id)
        except core.CredentialsRejectedError as exc:
            browser_pool.discard(account.account_id, status='credentials_invalid')
            if attempt_id:
                store.finish_attempt(attempt_id, status='credentials_invalid')
            pool_store.pause_account(run_id, account.account_id, reason='credentials_invalid')
            store.add_event('account_credentials_invalid', job_id=job.job_id, account_id=account.account_id, level='error', data={'http_status': exc.status, 'response_code': exc.response_code})
        except core.SafetyStopException as exc:
            if attempt_id:
                store.finish_attempt(attempt_id, status='safety_stop', safety_stop=exc.reason.value, error=str(exc))
            if exc.reason == core.StopReason.DAILY_LIMIT:
                quota_policy.record_quota_hold(store.path, account.account_id, evidence='portal_daily_limit_response')
                store.add_event('portal_quota_exhausted', job_id=job.job_id, account_id=account.account_id,
                                data={'reset_confirmed': False, 'browser_preserved': True})
                continue
            outcome = core._handle_account_safety_stop(exc, job_id=job.job_id, account=account, store=store, pool_store=pool_store, run_id=run_id, config=config, settings=settings, browser_pool=browser_pool, preflight_runner=preflight_runner, proxy_health_runner=proxy_health_runner)
            if outcome in {'safety_stop', 'cooldown'}:
                store.set_waiting(job.job_id, 'queued', reason=exc.reason.value)
                return outcome
        except Exception as exc:
            safe_error = core._redact_known_values(str(exc), username, password)
            if attempt_id and (not store.search_checkpoint(job.job_id)['saved']):
                store.finish_attempt(attempt_id, status='failed', safety_stop='search_outcome_unknown', error=safe_error)
                store.set_waiting(job.job_id, 'waiting_capacity', reason='search_outcome_unknown')
                return 'waiting_capacity'
            if attempt_id:
                store.finish_attempt(attempt_id, status='failed', error=safe_error)
            connection_failure = core._looks_like_connection_failure(exc)
            dataimpulse_failure = core._dataimpulse_failure_kind(exc) if core._is_dataimpulse_account(account) else 'unknown'
            recovered_route = False
            if core._is_dataimpulse_account(account) and dataimpulse_failure != 'provider_terminal' and (connection_failure or dataimpulse_failure == 'transient_route'):
                recovered_route = core._rotate_dataimpulse_route(account, settings, store, pool_store, run_id, browser_pool, preflight_runner, proxy_health_runner, reason='confirmed_connection_failure')
            if dataimpulse_failure == 'provider_terminal':
                pool_store.pause_account(run_id, account.account_id, reason='dataimpulse_provider_terminal', cooldown_seconds=None)
                store.add_event('dataimpulse_provider_terminal', job_id=job.job_id, account_id=account.account_id, level='error', data={'action': 'operator_required'})
                continue
            if connection_failure and (not recovered_route):
                browser_pool.discard(account.account_id, status='browser_context_failed')
            if recovered_route:
                store.add_event('account_route_recovered_after_failure', job_id=job.job_id, account_id=account.account_id)
                continue
            gate_ok = core._ensure_account_gate(account, runtime_settings, store, pool_store, run_id, preflight_runner, proxy_health_runner, force=True)
            if gate_ok:
                pool_store.pause_account(run_id, account.account_id, reason='browser_context_failed' if core._looks_like_connection_failure(exc) else 'unexpected_worker_failure')
            store.add_event('account_paused_after_failure', job_id=job.job_id, account_id=account.account_id, level='error', data={'error': safe_error})
