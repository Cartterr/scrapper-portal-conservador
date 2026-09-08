from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from .account_pool import (
    utc_now,
    AccountPoolStore,
    PoolConfig,
    account_settings,
    account_credentials,
    dashboard_status,
    load_account_pool_config,
    local_today,
    resolve_account_captcha,
)
from .config import SETTINGS, Settings
from .browser_preview import read_browser_preview
from .dataimpulse import DATAIMPULSE_STICKY_PROVIDERS
from .safety import redact

if TYPE_CHECKING:
    from .jobs import JobStore


@dataclass(frozen=True)
class PoolDashboardHandle:
    url: str
    server: ThreadingHTTPServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_pool_dashboard(
    store: AccountPoolStore,
    *,
    settings: Settings = SETTINGS,
    config: PoolConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    captcha_resolver: Callable[..., dict[str, Any]] | None = None,
    job_store: "JobStore | None" = None,
    allow_private_bind: bool = False,
) -> PoolDashboardHandle:
    if (
        job_store is not None
        and host not in {"127.0.0.1", "localhost"}
        and not allow_private_bind
    ):
        raise ValueError(
            "The jobs API must bind to a loopback address unless "
            "--allow-private-bind is explicitly set."
        )
    handler = _handler_factory(
        store,
        settings,
        config,
        captcha_resolver=captcha_resolver,
        job_store=job_store,
    )
    server = ThreadingHTTPServer((host, port), handler)
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return PoolDashboardHandle(
        url=f"http://{actual_host}:{actual_port}",
        server=server,
        thread=thread,
    )


def _handler_factory(
    store: AccountPoolStore,
    settings: Settings,
    config: PoolConfig,
    *,
    captcha_resolver: Callable[..., dict[str, Any]] | None = None,
    job_store: "JobStore | None" = None,
):
    resolver = captcha_resolver or resolve_account_captcha
    visual_confirmation_required = captcha_resolver is None
    captcha_threads: dict[str, threading.Thread] = {}
    captcha_confirmations: dict[str, threading.Event] = {}
    captcha_phases: dict[str, str] = {}
    endurance = None
    if job_store is not None:
        from .endurance import EnduranceController, load_endurance_plan

        endurance = EnduranceController(
            job_store,
            load_endurance_plan(settings.profile_dir.parent / "endurance-plan.json"),
            config,
        )

    class AccountPoolDashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(_dashboard_html())
                return
            if parsed.path == "/api/health":
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/status":
                payload = dashboard_status(store, config=config)
                payload["runtime"] = _runtime_summary(settings)
                payload["proxy_provider"] = _proxy_provider_summary(settings, config)
                if job_store is not None:
                    from .runtime_updates import read_status
                    payload["runtime"]["updates"] = read_status(job_store.path.parent / "runtime-updates")
                    payload["runtime"]["browser_owner"] = job_store.active_lease("browser_owner")
                    payload["runtime"]["owner_mode"] = os.environ.get("CBRS_BROWSER_OWNER_MODE", "embedded")
                    payload["runtime"]["owner_updates"] = read_status(settings.profile_dir.parent / "browser-owner" / "runtime-updates")
                    from .backup import backup_health
                    from .captcha_budget import CaptchaBudgetStore

                    payload = _with_job_pool_usage(payload, job_store, config)
                    payload["jobs"] = {
                        "summary": job_store.summary(),
                        "recent": _with_job_artifact_urls(job_store.list_jobs(limit=100)),
                    }
                    payload["endurance"] = endurance.status() if endurance else None
                    captcha_budget = CaptchaBudgetStore(
                        settings.captcha_state_path,
                        daily_limit=settings.two_captcha_daily_limit,
                        circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
                        rejection_cooldown_seconds=(
                            settings.two_captcha_rejection_cooldown_seconds
                        ),
                    )
                    payload["captcha_solver"] = captcha_budget.status()
                    payload["captcha_attempts"] = captcha_budget.recent_activity()
                    payload["backup"] = backup_health(settings)
                    payload = _with_proxy_state(payload, job_store, settings, config)
                payload = _with_account_username_prefixes(payload, config)
                payload = _with_captcha_phases(payload, captcha_phases)
                self._send_json(
                    _with_artifact_urls(payload),
                    reveal_proxy_endpoints=True,
                )
                return
            if job_store is not None and parsed.path.startswith("/api/browser-preview/"):
                account_id = unquote(parsed.path.rsplit("/", 1)[-1])
                self._send_browser_preview(account_id)
                return
            if job_store is not None and parsed.path.startswith("/api/error-screenshot/"):
                self._send_error_screenshot(unquote(parsed.path.rsplit("/", 1)[-1]))
                return
            if job_store is not None and parsed.path == "/api/settings":
                self._send_json(_production_settings_payload(settings, config))
                return
            if job_store is not None and parsed.path == "/api/jobs":
                self._send_json(
                    {"jobs": _with_job_artifact_urls(job_store.list_jobs(limit=_limit(parsed.query)))}
                )
                return
            if endurance is not None and parsed.path == "/api/endurance":
                self._send_json(endurance.status())
                return
            if job_store is not None and parsed.path == "/api/examples":
                self._send_json({"examples": job_store.successful_fna_examples()})
                return
            if job_store is not None and parsed.path.startswith("/api/jobs/"):
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 3:
                    job = job_store.get_job(parts[2])
                    if not job:
                        self._send_api_error(HTTPStatus.NOT_FOUND, "job_not_found")
                        return
                    job["artifacts"] = _with_job_artifact_urls(
                        [{"artifacts": job_store.artifacts(job_id=parts[2])}]
                    )[0]["artifacts"]
                    self._send_json(job)
                    return
                if len(parts) == 4 and parts[3] == "artifacts":
                    if not job_store.get_job(parts[2]):
                        self._send_api_error(HTTPStatus.NOT_FOUND, "job_not_found")
                        return
                    artifacts = job_store.artifacts(job_id=parts[2])
                    self._send_json({"artifacts": _with_job_artifact_urls(artifacts)})
                    return
            if job_store is not None and parsed.path.startswith("/api/artifacts/"):
                self._send_job_artifact(parsed.path.rsplit("/", 1)[-1])
                return
            if parsed.path == "/api/cycles":
                limit = _limit(parsed.query)
                run_id = _latest_run_id(store)
                self._send_json({"cycles": store.recent_cycles(run_id=run_id, limit=limit)})
                return
            if parsed.path == "/api/artifacts":
                run_id = _latest_run_id(store)
                artifacts = _with_artifact_urls({"artifacts": store.artifacts(run_id=run_id)})
                self._send_json(artifacts)
                return
            if parsed.path == "/api/events":
                limit = _limit(parsed.query)
                run_id = _latest_run_id(store)
                self._send_json({"events": store.recent_events(run_id=run_id, limit=limit)})
                return
            if parsed.path.startswith("/artifact/"):
                self._send_artifact(parsed.path.rsplit("/", 1)[-1])
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:
            nonlocal config, endurance
            parsed = urlparse(self.path)
            if not self._origin_allowed():
                self._send_api_error(HTTPStatus.FORBIDDEN, "cross_origin_request_rejected")
                return
            if job_store is not None and parsed.path == "/api/jobs/instant":
                self._create_job(run_now=True)
                return
            if job_store is not None and parsed.path == "/api/jobs":
                self._create_job()
                return
            if endurance is not None and parsed.path.startswith("/api/endurance/"):
                action = parsed.path.rsplit("/", 1)[-1]
                if action == "pause":
                    endurance.set_paused(True)
                elif action == "resume":
                    endurance.set_paused(False)
                elif action == "run-once":
                    job = endurance.maybe_enqueue(force=True)
                    self._send_json(
                        {"created": bool(job), "job": job},
                        status=HTTPStatus.ACCEPTED if job else HTTPStatus.CONFLICT,
                    )
                    return
                else:
                    self._send_api_error(HTTPStatus.NOT_FOUND, "not_found")
                    return
                self._send_json(endurance.status())
                return
            if job_store is not None and parsed.path == "/api/settings":
                status = str(dashboard_status(store, config=config).get("status") or "")
                if status in {"running", "waiting", "waiting_capacity", "waiting_captcha"}:
                    self._send_api_error(HTTPStatus.CONFLICT, "worker_must_be_stopped")
                    return
                try:
                    config = _save_production_settings(
                        settings,
                        config,
                        self._read_json(),
                    )
                    from .endurance import EnduranceController, load_endurance_plan

                    endurance = EnduranceController(
                        job_store,
                        load_endurance_plan(settings.profile_dir.parent / "endurance-plan.json"),
                        config,
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send_api_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_production_settings",
                        str(exc),
                    )
                    return
                self._send_json(
                    {
                        "ok": True,
                        "status": "settings_saved",
                        "settings": _production_settings_payload(settings, config),
                    }
                )
                return
            if (
                job_store is not None
                and parsed.path.startswith("/api/jobs/")
                and parsed.path.endswith("/cancel")
            ):
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) != 4:
                    self._send_api_error(HTTPStatus.NOT_FOUND, "not_found")
                    return
                job = job_store.request_cancel(parts[2])
                if not job:
                    self._send_api_error(HTTPStatus.NOT_FOUND, "job_not_found")
                    return
                self._send_json(job)
                return
            if parsed.path == "/api/stop":
                store.request_stop()
                self._send_json({"ok": True, "status": "stop_requested"})
                return
            if parsed.path == "/api/resume":
                status = str(dashboard_status(store, config=config).get("status") or "")
                if status in {"running", "waiting", "waiting_capacity", "waiting_captcha"}:
                    self._send_api_error(HTTPStatus.CONFLICT, "worker_already_active")
                    return
                _request_worker_resume(settings)
                self._send_json({"ok": True, "status": "resume_requested"})
                return
            if job_store is not None and parsed.path == "/api/captcha/automatic":
                provider = settings.external_captcha_provider
                provider_key = (
                    settings.capsolver_api_key
                    if provider == "capsolver"
                    else settings.two_captcha_api_key
                )
                if settings.captcha_solver_mode not in {
                    "2captcha_manual",
                    "2captcha_fallback",
                    "capsolver_manual",
                    "capsolver_fallback",
                } or not provider_key:
                    self._send_api_error(
                        HTTPStatus.CONFLICT, "automatic_external_solver_not_configured"
                    )
                    return
                payload = self._read_json()
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    self._send_api_error(
                        HTTPStatus.BAD_REQUEST,
                        "automatic_external_solver_enabled_must_be_boolean",
                    )
                    return
                from .captcha_budget import CaptchaBudgetStore

                budget = CaptchaBudgetStore(
                    settings.captcha_state_path,
                    daily_limit=settings.two_captcha_daily_limit,
                    circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
                    rejection_cooldown_seconds=(
                        settings.two_captcha_rejection_cooldown_seconds
                    ),
                )
                budget.set_automatic_enabled(enabled)
                released = 0
                worker_requested = False
                if enabled:
                    run = store.latest_run(dry_run=False)
                    if run:
                        run_id = str(run["run_id"])
                        for account_state in store.accounts(run_id):
                            if account_state["status"] == "captcha_pending":
                                store.mark_account_available(
                                    run_id, str(account_state["account_id"])
                                )
                    released = job_store.release_waiting_captcha()
                    if job_store.summary()["queued"] and job_store.active_lease() is None:
                        _request_worker_resume(settings)
                        worker_requested = True
                self._send_json(
                    {
                        "ok": True,
                        "automatic_enabled": enabled,
                        "released_jobs": released,
                        "worker_requested": worker_requested,
                    }
                )
                return
            if parsed.path == "/api/onboarding/accounts":
                status = str(dashboard_status(store, config=config).get("status") or "")
                if status in {"running", "waiting", "waiting_capacity", "waiting_captcha"}:
                    self._send_api_error(HTTPStatus.CONFLICT, "worker_must_be_stopped")
                    return
                try:
                    _request_account_configuration(settings, self._read_json())
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send_api_error(HTTPStatus.BAD_REQUEST, "invalid_account_configuration", str(exc))
                    return
                self._send_json(
                    {"ok": True, "status": "account_configuration_requested"},
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if (
                job_store is not None
                and parsed.path.startswith("/api/captcha/")
                and parsed.path.endswith("/solve-external")
            ):
                account_id = parsed.path.split("/")[3]
                known_ids = {account.account_id for account in config.accounts}
                if account_id not in known_ids:
                    self._send_api_error(HTTPStatus.NOT_FOUND, "unknown_account")
                    return
                if settings.captcha_solver_mode not in {
                    "2captcha_manual",
                    "capsolver_manual",
                }:
                    self._send_api_error(
                        HTTPStatus.CONFLICT, "manual_external_solver_mode_not_enabled"
                    )
                    return
                provider = settings.external_captcha_provider
                provider_key = (
                    settings.capsolver_api_key
                    if provider == "capsolver"
                    else settings.two_captcha_api_key
                )
                if not provider_key:
                    self._send_api_error(
                        HTTPStatus.CONFLICT, "external_solver_api_key_not_configured"
                    )
                    return
                run = store.latest_run(dry_run=False)
                account_state = next(
                    (
                        row
                        for row in store.accounts(str(run["run_id"]))
                        if row["account_id"] == account_id
                    ),
                    None,
                ) if run else None
                if not account_state or account_state["status"] != "captcha_pending":
                    self._send_api_error(
                        HTTPStatus.CONFLICT, "account_not_captcha_pending"
                    )
                    return
                from .captcha_budget import CaptchaBudgetError, CaptchaBudgetStore

                budget = CaptchaBudgetStore(
                    settings.captcha_state_path,
                    daily_limit=settings.two_captcha_daily_limit,
                    circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
                    rejection_cooldown_seconds=(
                        settings.two_captcha_rejection_cooldown_seconds
                    ),
                )
                try:
                    budget.arm_manual(account_id=account_id)
                except CaptchaBudgetError as exc:
                    self._send_api_error(HTTPStatus.CONFLICT, exc.code)
                    return
                released = job_store.release_waiting_captcha()
                store.mark_account_available(str(run["run_id"]), account_id)
                store.add_event(
                    str(run["run_id"]),
                    account_id=account_id,
                    message="one manual external CAPTCHA solve authorized",
                )
                worker_requested = False
                validation_job_id = None
                response_status = "one_solve_armed"
                if released:
                    job_store.set_next_account(account_id, config)
                    if job_store.active_lease() is None:
                        _request_worker_resume(settings)
                        worker_requested = True
                else:
                    examples = job_store.successful_fna_examples(limit=1)
                    coordinates = (
                        examples[0]
                        if examples
                        else {"foja": 9441, "numero": 4580, "year": 1980}
                    )
                    validation_job, _ = job_store.create_job(
                        kind="fna",
                        input_data={
                            "foja": coordinates["foja"],
                            "numero": coordinates["numero"],
                            "year": coordinates["year"],
                            "validation_only": True,
                            "target_account_id": account_id,
                        },
                        idempotency_key=(
                            f"captcha-validation:{account_id}:{time.time_ns()}"
                        ),
                        priority=2,
                        source="captcha_validation",
                    )
                    validation_job_id = str(validation_job["job_id"])
                    job_store.set_next_account(account_id, config)
                    if job_store.active_lease() is None:
                        _request_worker_resume(settings)
                        worker_requested = True
                    response_status = "captcha_validation_queued"
                    store.add_event(
                        str(run["run_id"]),
                        account_id=account_id,
                        message=(
                            "manual external CAPTCHA authorization queued for targeted "
                            "browser-first validation"
                        ),
                    )
                self._send_json(
                    {
                        "ok": True,
                        "status": response_status,
                        "account_id": account_id,
                        "released_jobs": released,
                        "worker_requested": worker_requested,
                        "validation_job_id": validation_job_id,
                    },
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if parsed.path.startswith("/api/captcha/") and parsed.path.endswith("/trigger"):
                account_id = parsed.path.split("/")[3]
                self._trigger_captcha(account_id)
                return
            if parsed.path.startswith("/api/captcha/") and parsed.path.endswith("/complete"):
                account_id = parsed.path.split("/")[3]
                confirmation = captcha_confirmations.get(account_id)
                if not confirmation:
                    self._send_api_error(HTTPStatus.CONFLICT, "captcha_recovery_not_running")
                    return
                confirmation.set()
                self._send_json(
                    {"ok": True, "status": "validation_requested", "account_id": account_id}
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def _create_job(self, *, run_now: bool = False) -> None:
            from .jobs import IdempotencyConflictError

            try:
                if run_now and not config.instant_jobs_enabled:
                    self._send_api_error(
                        HTTPStatus.CONFLICT,
                        "instant_jobs_disabled",
                    )
                    return
                if (
                    job_store.outstanding_job_count(source="production")
                    >= config.max_queued_production_jobs
                ):
                    self._send_api_error(
                        HTTPStatus.CONFLICT,
                        "production_queue_limit_reached",
                    )
                    return
                body = self._read_json()
                kind = str(body.get("kind") or ("text" if body.get("text") else "fna"))
                job, _created = job_store.create_job(
                    kind=kind,
                    input_data=body,
                    idempotency_key=body.get("idempotency_key"),
                    priority=1 if run_now else 0,
                )
            except IdempotencyConflictError as exc:
                self._send_api_error(HTTPStatus.CONFLICT, "idempotency_conflict", str(exc))
                return
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
                return
            worker_requested = False
            if run_now:
                status = str(dashboard_status(store, config=config).get("status") or "")
                if status not in {"running", "waiting", "waiting_capacity", "waiting_captcha"}:
                    _request_worker_resume(settings)
                    worker_requested = True
            self._send_json(
                {
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "status_url": f"/api/jobs/{job['job_id']}",
                    "priority": job["priority"],
                    "worker_requested": worker_requested,
                },
                status=HTTPStatus.ACCEPTED,
            )

        def _read_json(self) -> dict[str, Any]:
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise ValueError("Content-Type must be application/json")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("Content-Length is required")
            length = int(raw_length)
            if length <= 0 or length > 64 * 1024:
                raise ValueError("JSON body must be between 1 byte and 64 KiB")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host")
            return bool(host and origin in {f"http://{host}", f"https://{host}"})

        def _trigger_captcha(self, account_id: str) -> None:
            known_ids = {account.account_id for account in config.accounts}
            if account_id not in known_ids:
                self.send_error(HTTPStatus.NOT_FOUND, "unknown account")
                return
            existing = captcha_threads.get(account_id)
            if existing and existing.is_alive():
                self._send_json({"ok": True, "status": "already_running", "account_id": account_id})
                return
            if visual_confirmation_required and job_store is not None:
                store.request_stop()
                if endurance is not None:
                    endurance.set_paused(True)

            def run_recovery() -> None:
                try:
                    if visual_confirmation_required:
                        if job_store is not None:
                            _wait_for_worker_release(store, config)
                        confirmation = threading.Event()
                        captcha_confirmations[account_id] = confirmation
                        captcha_phases[account_id] = "automatic_login"
                        if not _hold_visual_captcha_session(
                            store,
                            settings,
                            config,
                            account_id=account_id,
                            confirmation=confirmation,
                            phase_changed=lambda phase: captcha_phases.__setitem__(
                                account_id, phase
                            ),
                        ):
                            return
                        captcha_phases[account_id] = "validating"
                    resolver(
                        settings=settings,
                        config=config,
                        store=store,
                        account_id=account_id,
                    )
                except Exception as exc:
                    run = store.latest_run()
                    if run:
                        store.add_event(
                            str(run["run_id"]),
                            account_id=account_id,
                            level="error",
                            message="pool captcha recovery failed",
                            data={"error": str(exc)},
                        )
                finally:
                    captcha_confirmations.pop(account_id, None)
                    captcha_phases.pop(account_id, None)

            thread = threading.Thread(
                target=run_recovery,
                name=f"cbrs-captcha-{account_id}",
                daemon=True,
            )
            captcha_threads[account_id] = thread
            thread.start()
            payload = {"ok": True, "status": "started", "account_id": account_id}
            if visual_confirmation_required:
                payload["visual_confirmation_required"] = True
            self._send_json(payload)

        def _send_artifact(self, cycle_id: str) -> None:
            match = None
            for artifact in store.artifacts(limit=1000):
                if artifact.get("cycle_id") == cycle_id:
                    match = artifact
                    break
            if not match or not match.get("artifact_path"):
                self.send_error(HTTPStatus.NOT_FOUND, "artifact not found")
                return
            path = Path(str(match["artifact_path"])).resolve()
            output_root = (settings.output_dir / "pool").resolve()
            if not path.exists() or not path.is_file() or not path.is_relative_to(output_root):
                self.send_error(HTTPStatus.NOT_FOUND, "artifact not available")
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            content = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.end_headers()
            self.wfile.write(content)

        def _send_job_artifact(self, artifact_id: str) -> None:
            assert job_store is not None
            artifact = job_store.artifact_record(artifact_id)
            if not artifact:
                self._send_api_error(HTTPStatus.NOT_FOUND, "artifact_not_found")
                return
            path = Path(str(artifact["path"])).resolve()
            output_root = (settings.output_dir / "jobs").resolve()
            if not path.exists() or not path.is_file() or not path.is_relative_to(output_root):
                self._send_api_error(HTTPStatus.NOT_FOUND, "artifact_not_available")
                return
            content = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(content)

        def _send_html(self, html: str) -> None:
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_error_screenshot(self, evidence_id: str) -> None:
            from .error_evidence import evidence_path
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self._send_api_error(HTTPStatus.FORBIDDEN, "local_only")
                return
            try:
                path = evidence_path(job_store.path, evidence_id)
                with job_store.connect() as db:
                    row = db.execute(
                        "SELECT capture_status FROM attempt_error_evidence WHERE evidence_id=?",
                        (evidence_id,),
                    ).fetchone()
                if not row or row["capture_status"] != "captured":
                    raise FileNotFoundError()
                content = path.read_bytes()
            except (ValueError, OSError):
                self._send_api_error(HTTPStatus.NOT_FOUND, "error_screenshot_unavailable")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def _send_browser_preview(self, account_id: str) -> None:
            configured_ids = {account.account_id for account in config.accounts}
            if account_id not in configured_ids:
                self._send_api_error(HTTPStatus.NOT_FOUND, "browser_preview_not_found")
                return
            try:
                is_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                is_loopback = False
            if not is_loopback:
                self._send_api_error(HTTPStatus.FORBIDDEN, "browser_preview_local_only")
                return
            active_worker = (job_store.active_lease("browser_owner") or job_store.active_lease()) if job_store is not None else None
            check = job_store.account_check(account_id) if job_store is not None else None
            owner = str((active_worker or {}).get("owner") or "")
            if (
                not check
                or not owner
                or not bool(check.get("browser_live"))
                or str(check.get("browser_owner") or "") != owner
            ):
                self._send_api_error(HTTPStatus.NOT_FOUND, "browser_preview_not_live")
                return
            try:
                preview = read_browser_preview(
                    job_store.path,
                    account_id,
                    max_age_seconds=settings.browser_preview_max_age_seconds,
                )
                content = preview.path.read_bytes()
            except OSError:
                self._send_api_error(HTTPStatus.NOT_FOUND, "browser_preview_not_available")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-CBRS-Captured-At", preview.captured_at)
            self.end_headers()
            self.wfile.write(content)

        def _send_json(
            self,
            payload: dict[str, Any],
            *,
            status: HTTPStatus = HTTPStatus.OK,
            reveal_proxy_endpoints: bool = False,
        ) -> None:
            safe_payload = redact(payload)
            if reveal_proxy_endpoints:
                try:
                    is_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
                except ValueError:
                    is_loopback = False
                if is_loopback:
                    safe_payload = _restore_proxy_endpoints(payload, safe_payload)
            encoded = json.dumps(safe_payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def _send_api_error(
            self,
            status: HTTPStatus,
            code: str,
            message: str | None = None,
        ) -> None:
            self._send_json(
                {"error": code, "message": message or code},
                status=status,
            )

    return AccountPoolDashboardHandler


def _with_artifact_urls(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    artifacts = []
    for artifact in enriched.get("artifacts", []):
        item = dict(artifact)
        item["artifact_url"] = f"/artifact/{item['cycle_id']}"
        artifacts.append(item)
    enriched["artifacts"] = artifacts
    return enriched


def _with_job_artifact_urls(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for value in values:
        item = dict(value)
        if "artifact_id" in item:
            item["artifact_url"] = f"/api/artifacts/{item['artifact_id']}"
        if isinstance(item.get("artifacts"), list):
            item["artifacts"] = _with_job_artifact_urls(item["artifacts"])
        enriched.append(item)
    return enriched


def _with_job_pool_usage(
    payload: dict[str, Any], job_store: "JobStore", config: PoolConfig
) -> dict[str, Any]:
    enriched = dict(payload)
    from .form_search import account_window
    accounts = []
    for account in enriched.get("accounts", []):
        item = dict(account)
        window = account_window(job_store.path, str(item.get('account_id')))
        item['quota_window'] = window
        used = window['used']
        quota = int(item.get("daily_quota") or config.daily_quota_per_account)
        item["used_today"] = used
        item["remaining_today"] = max(0, quota - used - window['reserved'])
        if item.get('status') == 'quota_reached' and used + window['reserved'] < quota:
            item['status'] = 'available'
        from .form_search import quota_hold
        item['portal_quota'] = quota_hold(job_store.path, str(item.get('account_id')))
        if item['portal_quota']:
            item['remaining_today'] = 0  # A due probe is not confirmed restored credit.
            item['status'] = 'portal_quota_exhausted' if item['portal_quota']['blocked'] else 'portal_quota_check_due'
        if item.get("status") == "available" and used >= quota:
            item["status"] = "quota_reached"
        accounts.append(item)
    enriched["accounts"] = accounts
    pool = dict(enriched.get("pool") or {})
    pool["used_today"] = sum(int(account.get("used_today") or 0) for account in accounts)
    pool['available_accounts'] = sum(account.get('status') == 'available' and account.get('remaining_today', 0) > 0 for account in accounts)
    pool['portal_quota_held_accounts'] = sum(bool(account.get('portal_quota')) for account in accounts)
    pool["remaining_today"] = max(
        0, sum(int(account.get('remaining_today') or 0) for account in accounts)
    )
    enriched["pool"] = pool
    stats = dict(enriched.get("stats") or {})
    stats["downloads"] = job_store.summary()["artifacts"]
    enriched["stats"] = stats
    return enriched


def _with_account_username_prefixes(
    payload: dict[str, Any], config: PoolConfig
) -> dict[str, Any]:
    """Expose only the non-email login prefix required by the local dashboard."""
    prefixes: dict[str, str] = {}
    for account in config.accounts:
        raw_username = os.environ.get(account.username_env or "", "").strip()
        if "@" in raw_username:
            prefix = raw_username.split("@", 1)[0].strip()
            if prefix:
                prefixes[account.account_id] = prefix[:80]
    enriched = dict(payload)
    accounts = []
    for account in enriched.get("accounts", []):
        item = dict(account)
        prefix = prefixes.get(str(item.get("account_id")))
        item["username_prefix"] = prefix
        if prefix:
            item["label"] = prefix
        accounts.append(item)
    enriched["accounts"] = accounts
    pool = dict(enriched.get("pool") or {})
    next_account_id = str(pool.get("next_account_id") or "")
    if next_account_id in prefixes:
        pool["next_account_label"] = prefixes[next_account_id]
    enriched["pool"] = pool
    return enriched


def _proxy_provider_summary(settings: Settings, config: PoolConfig) -> dict[str, Any]:
    from .proxy_provider import (
        TWO_CAPTCHA_DEDICATED_ISP_PROVIDER,
        TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
        dataimpulse_configuration_health,
        two_captcha_proxy_health,
    )

    providers = {
        account.proxy_provider for account in config.accounts if account.enabled
    }
    brands = {
        account.proxy_brand for account in config.accounts if account.enabled and account.proxy_brand
    }
    brand = next(iter(brands)) if len(brands) == 1 else "mixed" if brands else None
    dataimpulse_accounts = [
        account
        for account in config.accounts
        if account.enabled
        and account.proxy_provider in DATAIMPULSE_STICKY_PROVIDERS
    ]
    if dataimpulse_accounts and len(dataimpulse_accounts) == len(
        [account for account in config.accounts if account.enabled]
    ):
        result = dataimpulse_configuration_health(
            settings.dataimpulse_proxy_login,
            settings.dataimpulse_proxy_password,
            provider=dataimpulse_accounts[0].proxy_provider,
        )
        result["brand"] = "DataImpulse"
        result["configured_accounts"] = len(dataimpulse_accounts)
        result["sticky_ttl_minutes"] = settings.dataimpulse_sticky_ttl_minutes
        return result
    two_captcha_providers = {
        TWO_CAPTCHA_DEDICATED_ISP_PROVIDER,
        TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
    }
    managed_accounts = [
        account
        for account in config.accounts
        if account.enabled and account.proxy_provider in two_captcha_providers
    ]
    managed_count = len(managed_accounts)
    if not managed_count:
        summary = {
            "provider": "generic_static" if providers == {"generic_static"} else "mixed",
            "status": "not_applicable",
            "ok": True,
            "configured_accounts": 0,
        }
        if brand:
            summary["brand"] = brand
        return summary
    managed_provider = (
        managed_accounts[0].proxy_provider
        if len({account.proxy_provider for account in managed_accounts}) == 1
        else TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER
    )
    result = two_captcha_proxy_health(
        settings.two_captcha_api_key,
        provider=managed_provider,
    )
    result["brand"] = brand or "2Captcha"
    result["configured_accounts"] = managed_count
    return result


def _with_proxy_state(
    payload: dict[str, Any],
    job_store: "JobStore",
    settings: Settings,
    config: PoolConfig,
) -> dict[str, Any]:
    configured = {account.account_id: account for account in config.accounts}
    worker_lease = job_store.active_lease()
    active_worker = job_store.active_lease("browser_owner") or worker_lease
    active_worker_owner = str(active_worker.get("owner") or "") if active_worker else ""
    enriched = dict(payload)
    accounts = []
    for raw in enriched.get("accounts", []):
        item = dict(raw)
        account_id = str(item.get("account_id") or "")
        account = configured.get(account_id)
        profile_dir = (
            account.profile_dir
            if account and account.profile_dir
            else settings.profile_dir.parent / "accounts" / account_id / "chrome-profile"
        )
        baseline_path = profile_dir.parent / "fixed-egress-baseline.json"
        baseline_status = "missing"
        baseline_hash = None
        baseline_country = None
        if baseline_path.is_file():
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                if (
                    baseline.get("schema") == "cbrs-fixed-egress-baseline-v1"
                    and baseline.get("egress_hash")
                ):
                    baseline_status = "unverified"
                    baseline_hash = str(baseline["egress_hash"])
                    baseline_country = str(baseline.get("egress_country") or "") or None
                else:
                    baseline_status = "invalid"
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                baseline_status = "invalid"
        check = job_store.account_check(account_id) or {}
        route = job_store.dataimpulse_route(account_id) or {}
        checked_hash = str(check.get("egress_hash") or "") or None
        proxy_status = str(check.get("proxy_status") or "") or None
        if baseline_hash and checked_hash:
            baseline_status = "matched" if baseline_hash == checked_hash else "mismatch"
        if proxy_status == "failed" and baseline_status not in {"missing", "invalid"}:
            baseline_status = "failed"
        item["proxy_provider"] = (
            account.proxy_provider if account else "generic_static"
        )
        item["proxy_brand"] = account.proxy_brand if account else None
        item["proxy_health_status"] = proxy_status or "not_checked"
        item["egress_baseline_status"] = baseline_status
        item["egress_country"] = baseline_country
        route_hash = checked_hash or baseline_hash
        item["egress_route_id"] = (
            f"ip-{hashlib.sha256(route_hash.encode('utf-8')).hexdigest()[:10]}"
            if route_hash
            else None
        )
        item["proxy_checked_at"] = check.get("proxy_checked_at")
        active_port = int(route["active_port"]) if route.get("active_port") else None
        item["proxy_endpoint"] = _safe_proxy_endpoint(
            settings,
            account,
            dataimpulse_port=active_port,
        )
        item["proxy_sticky_port"] = active_port
        item["proxy_generation"] = int(route.get("generation") or 0)
        item["proxy_route_status"] = str(route.get("status") or "not_initialized")
        item["proxy_rotation_reason"] = route.get("last_rotation_reason")
        item["proxy_last_rotated_at"] = route.get("last_rotated_at")
        item["proxy_rotation_cooldown_until"] = route.get("cooldown_until")
        item["proxy_rotation_count_hour"] = int(route.get("rotation_count") or 0)
        item["proxy_rotation_limit_hour"] = int(
            settings.dataimpulse_max_rotations_per_hour
        )
        item["proxy_rotation_window_started_at"] = route.get("rotation_window_started_at")
        # Last candidate failure class (login rejected vs transport failure).
        item["proxy_last_candidate_outcome"] = route.get("last_error_code")
        cooldown_until = str(route.get("cooldown_until") or "")
        item["proxy_next_eligible_at"] = (
            cooldown_until if cooldown_until and cooldown_until > utc_now() else None
        )
        item["proxy_recent_candidates"] = job_store.recent_candidate_attempts(
            account_id, limit=6
        )
        item["proxy_sticky_ttl_minutes"] = (
            settings.dataimpulse_sticky_ttl_minutes
            if account
            and account.proxy_provider in DATAIMPULSE_STICKY_PROVIDERS
            else None
        )
        browser_owner = str(check.get("browser_owner") or "")
        item["worker_active"] = bool(worker_lease)
        item["browser_owner_active"] = bool(active_worker_owner)
        browser_live = bool(check.get("browser_live")) and bool(active_worker_owner)
        browser_live = browser_live and browser_owner == active_worker_owner
        item["browser_live"] = browser_live
        item["browser_authenticated"] = bool(
            browser_live
            and check.get("browser_authenticated")
            and str(check.get("browser_auth_state") or "") == "authenticated_form"
        )
        item["browser_mode"] = (
            "headless" if bool(check.get("browser_headless")) else "headed"
        ) if browser_live and check.get("browser_headless") is not None else None
        configured_engine = {
            "chrome": "native_chrome",
            "cloak": "cloakbrowser",
            "gologin": "gologin",
        }.get(settings.browser_backend, settings.browser_backend)
        item["browser_engine"] = str(
            check.get("browser_engine") or configured_engine or "unknown"
        )
        if browser_live:
            item["browser_status"] = str(check.get("browser_status") or "unknown")
        elif active_worker_owner and browser_owner == active_worker_owner:
            item["browser_status"] = str(check.get("browser_status") or "not_started")
        elif active_worker_owner:
            item["browser_status"] = "not_started"
        else:
            item["browser_status"] = "worker_stopped"
        item["browser_started_at"] = check.get("browser_started_at")
        item["browser_checked_at"] = check.get("browser_checked_at")
        item["browser_authenticated_at"] = check.get("browser_authenticated_at")
        item["browser_last_auth_error"] = check.get("browser_last_auth_error")
        item["browser_last_auth_http_status"] = check.get("browser_last_auth_http_status")
        item["browser_auth_state"] = str(
            check.get("browser_auth_state") or "unknown"
        )
        item["browser_preview_available"] = False
        item["browser_preview_captured_at"] = None
        item["browser_preview_age_seconds"] = None
        item["browser_preview_url"] = None
        if browser_live:
            try:
                preview = read_browser_preview(
                    job_store.path,
                    account_id,
                    max_age_seconds=settings.browser_preview_max_age_seconds,
                )
            except OSError:
                pass
            else:
                item["browser_preview_available"] = True
                item["browser_preview_captured_at"] = preview.captured_at
                item["browser_preview_age_seconds"] = round(preview.age_seconds, 1)
                item["browser_preview_url"] = (
                    f"/api/browser-preview/{quote(account_id, safe='')}"
                )
        accounts.append(item)
    enriched["accounts"] = accounts
    pool = dict(enriched.get("pool") or {})
    pool["browser_live_count"] = sum(bool(account["browser_live"]) for account in accounts)
    pool["browser_authenticated_count"] = sum(
        bool(account["browser_authenticated"]) for account in accounts
    )
    pool["browser_expected_count"] = len(accounts)
    enriched["pool"] = pool
    return enriched


def _safe_proxy_endpoint(
    settings: Settings,
    account: Any,
    *,
    dataimpulse_port: int | None = None,
) -> str | None:
    """Return only the proxy host and port, never credentials or the full URL."""
    if account is None:
        return None
    try:
        proxy_url = account_settings(
            settings,
            account,
            dataimpulse_port=dataimpulse_port,
        ).proxy_url
        parsed = urlparse(proxy_url or "")
        host = parsed.hostname
        port = parsed.port
    except (OSError, TypeError, ValueError):
        return None
    if not host:
        return None
    safe_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{safe_host}:{port}" if port else safe_host


def _restore_proxy_endpoints(
    original: dict[str, Any],
    redacted_payload: dict[str, Any],
) -> dict[str, Any]:
    """Reveal credential-free proxy endpoints only in the local admin status view."""
    original_accounts = original.get("accounts")
    safe_accounts = redacted_payload.get("accounts")
    if not isinstance(original_accounts, list) or not isinstance(safe_accounts, list):
        return redacted_payload
    originals_by_id = {
        str(account.get("account_id") or ""): account
        for account in original_accounts
        if isinstance(account, dict)
    }
    for account in safe_accounts:
        if not isinstance(account, dict):
            continue
        original_account = originals_by_id.get(str(account.get("account_id") or ""))
        if not original_account:
            continue
        endpoint = original_account.get("proxy_endpoint")
        if isinstance(endpoint, str) and endpoint:
            account["proxy_endpoint"] = endpoint
    return redacted_payload


def _with_captcha_phases(
    payload: dict[str, Any], phases: dict[str, str]
) -> dict[str, Any]:
    enriched = dict(payload)
    accounts = []
    for account in enriched.get("accounts", []):
        item = dict(account)
        phase = phases.get(str(item.get("account_id")))
        if phase:
            item["captcha_phase"] = phase
        accounts.append(item)
    enriched["accounts"] = accounts
    return enriched


def _request_worker_resume(settings: Settings) -> None:
    """Signal the fixed Linux service without accepting executable input."""
    _write_control_request(settings, "resume.request", "resume\n")


def _request_account_configuration(settings: Settings, payload: dict[str, Any]) -> None:
    """Queue one local account update without logging or returning its secrets."""
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 50:
        raise ValueError("Debe configurar entre 1 y 50 cuentas.")
    normalized: list[dict[str, Any]] = []
    account_ids: set[str] = set()
    for index, raw in enumerate(accounts, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"La cuenta {index} es inválida.")
        account_id = str(raw.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]+", account_id) or account_id in account_ids:
            raise ValueError("Cada cuenta debe tener un identificador único y seguro.")
        account_ids.add(account_id)
        item: dict[str, Any] = {"id": account_id}
        for field in (
            "username",
            "password",
            "proxy_url",
            "label",
            "egress_group",
            "proxy_provider",
            "proxy_brand",
        ):
            value = str(raw.get(field) or "")
            if len(value) > 1000 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"El campo {field} de la cuenta {index} es inválido.")
            item[field] = value
        raw_dataimpulse_port = raw.get("dataimpulse_port")
        if raw_dataimpulse_port not in {None, ""}:
            try:
                dataimpulse_port = int(raw_dataimpulse_port)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"El puerto DataImpulse de la cuenta {index} es inválido."
                ) from exc
            if not 10000 <= dataimpulse_port <= 20000:
                raise ValueError(
                    f"El puerto DataImpulse de la cuenta {index} debe estar entre 10000 y 20000."
                )
            item["dataimpulse_port"] = dataimpulse_port
        try:
            quota = int(raw.get("daily_quota", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"El cupo de la cuenta {index} es inválido.") from exc
        if not 1 <= quota <= 10_000:
            raise ValueError(f"El cupo de la cuenta {index} es inválido.")
        item["daily_quota"] = quota
        normalized.append(item)
    _write_control_json_request(settings, "account-configuration.json", {"accounts": normalized})


def _write_control_request(settings: Settings, name: str, content: str) -> None:
    if name != "resume.request":
        raise ValueError("unsupported local control request")
    control_dir = settings.profile_dir.parent / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    request = control_dir / name
    temporary = control_dir / f"{name}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, request)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _write_control_json_request(settings: Settings, name: str, payload: dict[str, Any]) -> None:
    if name != "account-configuration.json":
        raise ValueError("unsupported local control request")
    control_dir = settings.profile_dir.parent / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    request = control_dir / name
    temporary = control_dir / f".{name}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, request)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _production_settings_payload(
    settings: Settings,
    config: PoolConfig,
) -> dict[str, Any]:
    from .endurance import load_endurance_plan

    plan = load_endurance_plan(settings.profile_dir.parent / "endurance-plan.json")
    return {
        "pool": {
            "daily_quota_per_account": config.daily_quota_per_account,
            "human_like_behavior_enabled": config.human_like_behavior_enabled,
            "job_interval_min_seconds": config.job_interval_min_seconds,
            "job_interval_max_seconds": config.job_interval_max_seconds,
            "worker_poll_seconds": config.worker_poll_seconds,
            "max_queued_production_jobs": config.max_queued_production_jobs,
            "instant_jobs_enabled": config.instant_jobs_enabled,
        },
        "endurance": {
            "enabled": plan.enabled,
            "cooldown_seconds": plan.cooldown_seconds,
            "jobs_per_account_per_day": plan.jobs_per_account_per_day,
            "production_reserve_per_account": plan.production_reserve_per_account,
        },
        "runtime": {
            "request_delay_seconds": settings.request_delay_seconds,
            "proxy_recheck_seconds": settings.proxy_recheck_seconds,
            "captcha_solver_mode": settings.captcha_solver_mode,
            "captcha_solver_provider": settings.external_captcha_provider or "browser",
            "two_captcha_daily_limit": settings.two_captcha_daily_limit,
            "browser_headless": settings.headless,
            "expected_egress_country": settings.expected_egress_country,
        },
        "locked": {
            "selection_policy": "round_robin",
            "production_priority": True,
            "endurance_max_outstanding_jobs": 1,
            "endurance_no_catch_up": True,
            "quota_exhaustion_test_mode": plan.quota_exhaustion_test_mode,
            "pdf_transport": "browser_origin",
            "dashboard_bind": "loopback_only",
        },
    }


def _save_production_settings(
    settings: Settings,
    current: PoolConfig,
    payload: dict[str, Any],
) -> PoolConfig:
    from .endurance import load_endurance_plan

    pool_update = payload.get("pool")
    endurance_update = payload.get("endurance")
    if not isinstance(pool_update, dict) or not isinstance(endurance_update, dict):
        raise ValueError("pool and endurance settings are required")

    state_root = settings.profile_dir.parent
    pool_path = state_root / "account-pool.json"
    endurance_path = state_root / "endurance-plan.json"
    pool_raw = json.loads(pool_path.read_text(encoding="utf-8")) if pool_path.exists() else {}
    endurance_raw = (
        json.loads(endurance_path.read_text(encoding="utf-8"))
        if endurance_path.exists()
        else {}
    )
    if not isinstance(pool_raw, dict) or not isinstance(endurance_raw, dict):
        raise ValueError("runtime configuration files must contain JSON objects")

    pool_raw.update(
        {
            "daily_quota_per_account": _bounded_int(
                pool_update,
                "daily_quota_per_account",
                minimum=1,
                maximum=20,
            ),
            "human_like_behavior_enabled": _strict_bool(
                pool_update,
                "human_like_behavior_enabled",
            ),
            "job_interval_min_seconds": _bounded_float(
                pool_update,
                "job_interval_min_seconds",
                minimum=0,
                maximum=3600,
            ),
            "job_interval_max_seconds": _bounded_float(
                pool_update,
                "job_interval_max_seconds",
                minimum=0,
                maximum=3600,
            ),
            "worker_poll_seconds": _bounded_float(
                pool_update,
                "worker_poll_seconds",
                minimum=0.1,
                maximum=300,
            ),
            "max_queued_production_jobs": _bounded_int(
                pool_update,
                "max_queued_production_jobs",
                minimum=1,
                maximum=10_000,
            ),
            "instant_jobs_enabled": _strict_bool(
                pool_update,
                "instant_jobs_enabled",
            ),
            "selection_policy": "round_robin",
        }
    )
    if pool_raw["job_interval_max_seconds"] < pool_raw["job_interval_min_seconds"]:
        raise ValueError("maximum jitter must be greater than or equal to minimum jitter")

    endurance_raw.update(
        {
            "enabled": _strict_bool(endurance_update, "enabled"),
            "cooldown_seconds": _bounded_float(
                endurance_update,
                "cooldown_seconds",
                minimum=60,
                maximum=300,
            ),
            "jobs_per_account_per_day": _bounded_int(
                endurance_update,
                "jobs_per_account_per_day",
                minimum=1,
                maximum=20,
            ),
            "production_reserve_per_account": _bounded_int(
                endurance_update,
                "production_reserve_per_account",
                minimum=0,
                maximum=20,
            ),
            "max_outstanding_jobs": 1,
            "no_catch_up": True,
        }
    )

    pool_temporary = _write_json_temporary(pool_path, pool_raw)
    endurance_temporary = _write_json_temporary(endurance_path, endurance_raw)
    try:
        validated_config = load_account_pool_config(settings, path=pool_temporary)
        validated_plan = load_endurance_plan(endurance_temporary)
        enabled_quotas = [
            validated_config.quota_for(account)
            for account in validated_config.accounts
            if account.enabled
        ]
        minimum_quota = min(enabled_quotas) if enabled_quotas else 0
        if (
            not validated_plan.quota_exhaustion_test_mode
            and validated_plan.jobs_per_account_per_day
            + validated_plan.production_reserve_per_account
            > minimum_quota
        ):
            raise ValueError(
                "endurance allocation plus production reserve cannot exceed an enabled account quota"
            )
        os.replace(pool_temporary, pool_path)
        os.replace(endurance_temporary, endurance_path)
        return load_account_pool_config(settings, path=pool_path)
    finally:
        pool_temporary.unlink(missing_ok=True)
        endurance_temporary.unlink(missing_ok=True)


def _strict_bool(values: dict[str, Any], name: str) -> bool:
    value = values.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def _bounded_float(
    values: dict[str, Any],
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(values.get(name))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _bounded_int(
    values: dict[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = values.get(name)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed != value and not (isinstance(value, str) and str(parsed) == value.strip()):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _write_json_temporary(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _latest_run_id(store: AccountPoolStore) -> str | None:
    run = store.latest_run()
    return str(run["run_id"]) if run else None


def _hold_visual_captcha_session(
    store: AccountPoolStore,
    settings: Settings,
    config: PoolConfig,
    *,
    account_id: str,
    confirmation: threading.Event,
    phase_changed: Callable[[str], None] | None = None,
) -> bool:
    from .account_pool import account_settings
    from .browser_session import BrowserSession
    from .safety import SafetyStopException, StopReason

    run = store.latest_run(dry_run=False)
    if not run:
        raise ValueError("No live account run exists for CAPTCHA recovery.")
    run_id = str(run["run_id"])
    account = next(
        (candidate for candidate in config.accounts if candidate.account_id == account_id),
        None,
    )
    if account is None:
        raise ValueError(f"Unknown account: {account_id}")
    timeout_raw = os.environ.get("CBRS_CAPTCHA_RECOVERY_TIMEOUT_SECONDS", "900")
    try:
        timeout = max(60, min(int(timeout_raw), 3600))
    except ValueError:
        timeout = 900
    store.mark_account_captcha_solving(run_id, account_id)
    store.add_event(
        run_id,
        account_id=account_id,
        level="warning",
        message="visual captcha session opened",
        data={"timeout_seconds": timeout},
    )
    try:
        with BrowserSession(account_settings(settings, account), headless=False) as browser:
            if account.username_env and account.password_env:
                username, password = account_credentials(account)
                try:
                    # Use the real visible form: fill both fields and submit it
                    # exactly as an operator would. This is the only reliable
                    # way to distinguish an expired session from a challenge
                    # that genuinely needs visual intervention.
                    browser.login_with_visible_form(username, password)
                    browser.reload_current_page()
                    store.add_event(
                        run_id,
                        account_id=account_id,
                        message="visual captcha automatic login succeeded",
                    )
                    return True
                except SafetyStopException as exc:
                    if exc.reason in {
                        StopReason.CAPTCHA_REJECTED,
                        StopReason.WAF_CHALLENGE,
                    }:
                        if phase_changed:
                            phase_changed("waiting_operator")
                    elif exc.reason == StopReason.AUTH_REQUIRED:
                        browser.prepare_interactive_login(username, password)
                        if phase_changed:
                            phase_changed("waiting_operator")
                    else:
                        raise
            else:
                browser.goto_index()
                if phase_changed:
                    phase_changed("waiting_operator")
            confirmed = confirmation.wait(timeout=timeout)
    except Exception:
        store.mark_account_captcha_pending(run_id, account_id, reason="visual_recovery_failed")
        raise
    if not confirmed:
        store.mark_account_captcha_pending(run_id, account_id, reason="visual_recovery_timed_out")
        store.add_event(
            run_id,
            account_id=account_id,
            level="warning",
            message="visual captcha session timed out",
        )
        return False
    store.add_event(
        run_id,
        account_id=account_id,
        message="operator requested captcha validation",
    )
    return True


def _wait_for_worker_release(
    store: AccountPoolStore, config: PoolConfig, *, timeout_seconds: float = 180
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = str(dashboard_status(store, config=config).get("status") or "")
        if status not in {"running", "waiting", "waiting_capacity", "waiting_captcha"}:
            return
        time.sleep(1)
    raise RuntimeError("Worker did not release browser profiles for visual recovery.")


def _limit(query: str) -> int:
    raw = parse_qs(query).get("limit", ["100"])[0]
    try:
        return max(1, min(int(raw), 1000))
    except ValueError:
        return 100


def _runtime_summary(settings: Settings) -> dict[str, Any]:
    # noVNC belongs exclusively to the legacy Linux display path. Native
    # Windows recovery opens the configured Chrome executable directly, so a
    # fabricated localhost:6080 link would always be a broken destination.
    visual_url = os.environ.get("CBRS_NOVNC_URL", "").strip() or None
    if visual_url:
        # Keep the recovery endpoint loopback-only while avoiding the general
        # IP redactor turning 127.0.0.1 into an unusable browser URL.
        visual_url = visual_url.replace("://127.0.0.1", "://localhost", 1)
    return {
        "browser_backend": settings.browser_backend,
        "browser_headless": settings.headless,
        "browser_window_mode": settings.window_mode,
        "browser_preview_interval_seconds": settings.browser_preview_interval_seconds,
        "browser_preview_max_age_seconds": settings.browser_preview_max_age_seconds,
        "expected_egress_country": settings.expected_egress_country,
        "request_delay_seconds": settings.request_delay_seconds,
        "captcha_solver_mode": settings.captcha_solver_mode,
        "visual_url": visual_url,
        "visual_recovery_mode": "noVNC" if visual_url else "native_chrome",
    }


def _dashboard_html() -> str:
    # UI edits appear on page refresh without changing the browser owner.
    return (Path(__file__).parent / "web" / "overview.html").read_text(encoding="utf-8")
