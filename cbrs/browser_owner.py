"""Independent native Chrome owner. Worker lifecycle never owns this process."""
from __future__ import annotations
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .owner_protocol import OWNER_LEASE, OwnerCommands, command_path, binding_fingerprint
from .browser_session import CommerceAuthState, CredentialsRejectedError
from .safety import SafetyStopException


class BrowserOwner:
    def __init__(self, settings, config, store, *, scraper_factory=None):
        from .jobs import _PersistentAccountBrowsers
        from .scraper import CBRSScraper
        self.settings, self.config, self.store = settings, config, store
        self.identity = f"browser-owner-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.commands = OwnerCommands(command_path(settings))
        self.pool = _PersistentAccountBrowsers(scraper_factory=scraper_factory or CBRSScraper,
                        headless=settings.headless, store=store, worker_id=self.identity)
        self.accounts = {a.account_id: a for a in config.accounts if a.enabled}
        self.bindings = {}
        self.operation_lease = None
        self.heartbeat_stop = threading.Event()

    def execute(self, command):
        from .jobs import _runtime_account_settings
        from .account_pool import account_credentials
        account_id = command["account"]
        if command["version"] != 1 or account_id not in self.accounts:
            raise ValueError("Unsupported owner command")
        worker = self.store.active_lease()
        if not worker or worker["owner"] != command["worker"]:
            raise ValueError("Worker lease is not current")
        payload = json.loads(command["payload"])
        operation = command["operation"]
        account = self.accounts[account_id]
        if operation == "recover_route":
            # Recovery is a bounded owner operation, never a raw proxy/close RPC.
            # Recheck current DOM here; worker-side evidence alone cannot close
            # or replace a formerly authenticated browser.
            if not self.pool.can_replace_rejected_login(account_id) or self.store.global_cooldown():
                return False
            from .jobs import _rotate_dataimpulse_route
            from .account_pool import AccountPoolStore
            from .preflight import run_preflight
            from .proxy_health import run_proxy_health
            pool_store = AccountPoolStore(self.store.path)
            run = pool_store.latest_run(dry_run=False)
            if not run or run.get("finished_at"):
                return False
            ok = _rotate_dataimpulse_route(account, self.settings, self.store, pool_store,
                run["run_id"], self.pool, run_preflight, run_proxy_health,
                reason="scoped_visible_login_rejection", _owner_execution=True)
            if ok:
                self.bindings[account_id] = binding_fingerprint(
                    _runtime_account_settings(self.settings, account, self.store), self.settings.headless)
            return ok
        if operation == "ensure":
            settings = _runtime_account_settings(self.settings, account, self.store)
            binding = binding_fingerprint(settings, self.settings.headless)
            if payload.get("binding") != binding or (account_id in self.bindings and self.bindings[account_id] != binding):
                raise ValueError("Live browser binding cannot change")
            entry = self.pool._entries.get(account_id)
            if entry and not payload.get("force"):
                state = entry.scraper.browser.detect_commerce_auth_state()
                if state is CommerceAuthState.AUTHENTICATED_FORM:
                    return "browser_form"
            self.bindings[account_id] = binding
            username, password = account_credentials(account)
            with self.pool.session(account_id, settings, username, password, force=bool(payload.get("force"))):
                return "browser_form"
        entry = self.pool._entries.get(account_id)
        if entry is None:
            raise ValueError("Browser has not been acquired")
        scraper = entry.scraper
        if operation in {"search_fna", "search_text"}:
            job_id = command.get("job_id")
            if not job_id:
                raise ValueError("Search requires a durable job")
            checkpoint = self.store.search_checkpoint(job_id)
            if checkpoint["saved"] or checkpoint["uncertain"] or checkpoint["incomplete_receipt"]:
                raise ValueError("Search already accepted; resume its document phase")
            with self.store.connect() as db:
                attempt = db.execute("SELECT attempt_id FROM job_attempts WHERE job_id=? AND account_id=? AND status='running' AND quota_consumed=1 ORDER BY started_at DESC LIMIT 1", (job_id, account_id)).fetchone()
            if not attempt:
                raise ValueError("Search requires a quota reservation")
            job = self.store.get_job(job_id, include_input=True)
            if not job or job.get("cancel_requested"):
                raise ValueError("Search job unavailable")
            # The saved job is authoritative; never trust arbitrary query values
            # in a worker command, nor provide a direct arbitrary HTTP primitive.
            data = job["input"]
            if job["kind"] == "fna" and operation == "search_fna":
                if payload != {"foja": data["foja"], "numero": data["numero"], "ano": data["year"]}:
                    raise ValueError("Search command differs from job")
                result = scraper.search_by_fna(data["foja"], data["numero"], data["year"])
            elif job["kind"] == "text" and operation == "search_text" and payload == {"texto": data["text"]}:
                result = scraper.search_by_text(data["text"])
            else:
                raise ValueError("Search command differs from job")
            # Commit portal acceptance before returning across the process
            # boundary. Worker death cannot lose this receipt or repeat search.
            self.store.add_results(job_id, result, attempt_id=attempt["attempt_id"],
                materialize_items=not (job.get("source") == "captcha_validation" and data.get("validation_only")))
            return result
        if operation == "image_refs":
            return scraper.get_image_refs(payload["ticket"])
        if operation == "download_image":
            target = Path(payload["output_path"]).resolve()
            permitted = (self.settings.output_dir / "jobs").resolve()
            if not target.is_relative_to(permitted) or target.suffix.lower() != ".jpg" or ".staging" not in target.parts:
                raise ValueError("Image destination outside permitted staging root")
            scraper.download_image(payload["data_ref"], target)
            return {"saved": True}
        raise ValueError("Unsupported owner operation")

    def tick(self):
        worker = self.store.active_lease()
        command = self.commands.claim(worker["owner"] if worker else None)
        if command:
            job_id = command.get("job_id")
            lease = f"browser_operation:{job_id}" if job_id else None
            if lease:
                if not self.store.acquire_lease(lease, self.identity):
                    self.commands.finish(command["id"], error={"reason": "operation_busy"}, uncertain=True)
                    return
                self.operation_lease = lease
            try:
                result = self.execute(command)
                self.commands.finish(command["id"], result=result)
            except Exception as exc:
                from .jobs import capture_error
                entry = self.pool._entries.get(command["account"])
                if entry:
                    try:
                        capture_error(self.store, command["account"], entry.scraper.browser, exc)
                    except Exception:
                        pass  # Evidence failure must not strand the command.
                error = {"reason": exc.reason.value, "status": exc.status,
                         "context": exc.context if exc.context in {"auth login", "auth navigation", "form search"} else "browser owner"} if isinstance(exc, SafetyStopException) else {"reason": "credentials_invalid" if isinstance(exc, CredentialsRejectedError) else "owner_operation_failed"}
                self.commands.finish(command["id"], error=error,
                    uncertain=command["operation"].startswith("search_") and not isinstance(exc, SafetyStopException))
            finally:
                if lease:
                    self.store.release_lease(lease, self.identity)
                    self.operation_lease = None
        # No worker required for passive evidence and previews. No worker-side
        # sleep, crash, replacement or PDF generation can close these contexts.
        self.pool.capture_previews()

    def _heartbeat(self):
        while not self.heartbeat_stop.wait(10):
            try:
                self.store.heartbeat_lease(OWNER_LEASE, self.identity)
                if self.operation_lease:
                    self.store.heartbeat_lease(self.operation_lease, self.identity)
            except Exception:
                # A transient locked/unavailable DB must not permanently kill
                # the heartbeat thread or trigger a destructive restart.
                continue

    def run(self):
        from .owner_lock import OwnerLock
        with OwnerLock(self.commands.path.parent / 'owner.lock'):
            self._run_locked()

    def _run_locked(self):
        from .runtime_updates import RuntimeUpdates
        if not self.store.acquire_lease(OWNER_LEASE, self.identity):
            raise RuntimeError("Another browser owner is active")
        updates = RuntimeUpdates(Path(__file__).parent, self.commands.path.parent / "runtime-updates", owner=self.identity)
        self.commands.recover_owner_crash()
        self.commands.clear_stop()
        thread = threading.Thread(target=self._heartbeat, daemon=True)
        thread.start()
        try:
            while not self.commands.stopping():
                try:
                    updates.poll()
                    self.tick()
                except Exception:
                    # Keep expensive Chrome state alive on infrastructure errors;
                    # operators inspect the owner lease rather than a crash loop.
                    time.sleep(1)
                time.sleep(.1)
        finally:
            self.pool.close_all(service_shutdown=True)
            updates.close()
            self.heartbeat_stop.set()
            thread.join(timeout=12)
            self.store.release_lease(OWNER_LEASE, self.identity)


def main():
    import argparse
    from .config import SETTINGS
    from .account_pool import load_account_pool_config
    from .jobs import default_job_store
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "stop"))
    args = parser.parse_args()
    if args.action == "stop":
        OwnerCommands(command_path(SETTINGS)).stop()
    else:
        if os.environ.get("CBRS_BROWSER_OWNER_MODE") != "external":
            raise RuntimeError("Independent owner requires explicit external mode")
        BrowserOwner(SETTINGS, load_account_pool_config(SETTINGS), default_job_store(SETTINGS)).run()


if __name__ == "__main__":
    main()
