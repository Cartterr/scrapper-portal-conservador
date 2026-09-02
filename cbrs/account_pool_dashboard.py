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
from urllib.parse import parse_qs, urlparse

from .account_pool import (
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
from .dataimpulse import DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER
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
    usage = job_store.usage_by_account(local_today())
    accounts = []
    for account in enriched.get("accounts", []):
        item = dict(account)
        used = usage.get(str(item.get("account_id")), 0)
        quota = int(item.get("daily_quota") or config.daily_quota_per_account)
        item["used_today"] = used
        item["remaining_today"] = max(0, quota - used)
        if item.get("status") == "available" and used >= quota:
            item["status"] = "quota_reached"
        accounts.append(item)
    enriched["accounts"] = accounts
    pool = dict(enriched.get("pool") or {})
    pool["used_today"] = sum(int(account.get("used_today") or 0) for account in accounts)
    pool["remaining_today"] = max(
        0, int(pool.get("daily_quota") or config.pool_daily_quota) - pool["used_today"]
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
        DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER,
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
        and account.proxy_provider == DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER
    ]
    if dataimpulse_accounts and len(dataimpulse_accounts) == len(
        [account for account in config.accounts if account.enabled]
    ):
        result = dataimpulse_configuration_health(
            settings.dataimpulse_proxy_login,
            settings.dataimpulse_proxy_password,
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
    active_worker = job_store.active_lease()
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
        item["proxy_sticky_ttl_minutes"] = (
            settings.dataimpulse_sticky_ttl_minutes
            if account
            and account.proxy_provider == DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER
            else None
        )
        browser_owner = str(check.get("browser_owner") or "")
        item["worker_active"] = bool(active_worker_owner)
        browser_live = bool(check.get("browser_live")) and bool(active_worker_owner)
        browser_live = browser_live and browser_owner == active_worker_owner
        item["browser_live"] = browser_live
        item["browser_authenticated"] = browser_live and bool(
            check.get("browser_authenticated")
        )
        item["browser_mode"] = (
            "headless" if bool(check.get("browser_headless")) else "headed"
        ) if check.get("browser_headless") is not None else None
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
        item["browser_auth_state"] = str(
            check.get("browser_auth_state") or "unknown"
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
    """Start the fixed native task or signal the legacy systemd path unit.

    Both routes accept no command or user-provided executable input.
    """
    native_state_root = settings.profile_dir.parent
    if os.name == "nt" and native_state_root.drive.upper() == "G:":
        present = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", "CBRS Worker"],
            capture_output=True,
            text=True,
            check=False,
        )
        if present.returncode == 0:
            result = subprocess.run(
                ["schtasks.exe", "/Run", "/TN", "CBRS Worker"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("The native CBRS Worker task could not be started.")
            return
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
        "expected_egress_country": settings.expected_egress_country,
        "request_delay_seconds": settings.request_delay_seconds,
        "captcha_solver_mode": settings.captcha_solver_mode,
        "visual_url": visual_url,
        "visual_recovery_mode": "noVNC" if visual_url else "native_chrome",
    }


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pool de Consultas CBRS</title>
  <script>
    (() => {
      try {
        const savedTheme = localStorage.getItem("cbrs-dashboard-theme");
        const preferredTheme = window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light";
        document.documentElement.dataset.theme = savedTheme || preferredTheme;
      } catch (_) {
        document.documentElement.dataset.theme = "light";
      }
    })();
  </script>
  <style>
    :root {
      --bg: #edf3f8;
      --ink: #111827;
      --muted: #667085;
      --panel: #fff;
      --line: #d8e0ea;
      --ok: #11845b;
      --ok-soft: #e7f7ef;
      --warn: #b76e00;
      --warn-soft: #fff4de;
      --bad: #b42318;
      --bad-soft: #fff1f0;
      --captcha: #d97706;
      --captcha-soft: #fff7ed;
      --captcha-wave: #0891b2;
      --accent: #1d4ed8;
      --accent-soft: #e8efff;
      --shadow: 0 18px 50px rgba(31, 41, 55, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    main { max-width: 1240px; margin: 0 auto; padding: 22px 24px 36px; }
    button {
      border: 1px solid var(--bad);
      border-radius: 6px;
      color: #fff;
      background: var(--bad);
      padding: 8px 12px;
      font-weight: 800;
      cursor: pointer;
    }
    .worker-action { min-width: 148px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; }
    .worker-action.resume { border-color: var(--ok); background: var(--ok); }
    .worker-action.stop { border-color: var(--bad); background: var(--bad); }
    .worker-control-hint { max-width: 220px; font-size: 11px; line-height: 1.3; text-align: right; }
    .onboarding {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.3fr);
      gap: 18px;
      align-items: center;
      margin-bottom: 16px;
      border-color: #c7d2fe;
      background: linear-gradient(135deg, #f8faff, #eef8ff);
    }
    .onboarding h2 { margin-bottom: 6px; }
    .onboarding p { margin: 0; }
    .onboarding-actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .onboarding-action {
      min-height: 38px;
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
    }
    .onboarding-action.visual { border-color: #7c3aed; background: #7c3aed; }
    .onboarding-action.results { border-color: #475569; background: #475569; }
    .auto-captcha-card {
      display: flex; align-items: center; justify-content: space-between; gap: 20px;
      margin-bottom: 16px; padding: 18px 20px; border: 2px solid #7c3aed;
      border-radius: 14px; background: linear-gradient(135deg, #f5f3ff, #eef2ff);
      box-shadow: 0 8px 24px rgba(124,58,237,.12);
    }
    .auto-captcha-copy strong { display: block; font-size: 18px; font-weight: 950; color: #4c1d95; }
    .auto-captcha-copy small { display: block; margin-top: 5px; color: #5b6475; line-height: 1.45; }
    .auto-captcha-control { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; font-weight: 900; }
    .auto-captcha-control .switch { width: 58px; height: 32px; }
    .auto-captcha-control .switch span::after { width: 24px; height: 24px; left: 4px; top: 4px; }
    .auto-captcha-control .switch input:checked + span::after { transform: translateX(26px); }
    .request-type-tabs { display: flex; gap: 7px; margin-top: 12px; }
    .request-type-tab,
    .example-trigger,
    .instant-action,
    [data-endurance-action],
    .modal-close,
    .modal-save { display: inline-flex; align-items: center; justify-content: center; gap: 7px; }
    .request-type-tab { border-color: #cbd5e1; background: #fff; color: #475569; }
    .request-type-tab.active { border-color: var(--accent); background: var(--accent); color: #fff; }
    .example-trigger { margin-left: auto; min-height: 30px; padding: 4px 8px; border-color: #bfdbfe; background: #eff6ff; color: #1d4ed8; font-size: 12px; }
    .request-composer { margin-top: 10px; }
    .request-composer input { width: 100%; box-sizing: border-box; min-width: 0; border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; font: inherit; }
    .request-document-fields { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
    .request-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .request-actions .instant-action { border-color: #0f766e; background: #0f766e; }
    .control-feedback { min-height: 18px; margin-top: 8px; font-size: 12px; color: var(--muted); }
    dialog.config-modal { width: min(760px, calc(100vw - 28px)); max-height: min(760px, calc(100vh - 28px)); border: 0; border-radius: 16px; padding: 0; box-shadow: 0 26px 80px rgba(15, 23, 42, .35); color: var(--ink); }
    dialog.config-modal::backdrop { background: rgba(15, 23, 42, .58); backdrop-filter: blur(3px); }
    .config-modal-content { padding: 24px; background: #fff; }
    .config-modal-header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 16px; }
    .config-modal-header h2 { margin-bottom: 5px; font-size: 21px; }
    .modal-close { min-width: 36px; padding: 6px 10px; border-color: var(--line); background: #fff; color: var(--ink); font-size: 18px; }
    .account-editor-list { display: grid; gap: 12px; max-height: 460px; overflow-y: auto; padding-right: 4px; }
    .account-editor { border: 1px solid var(--line); border-radius: 10px; padding: 14px; background: #f8fafc; }
    .account-editor-top { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 12px; }
    .account-editor-title { font-weight: 900; }
    .account-editor-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .account-editor label { display: grid; gap: 5px; color: #475569; font-size: 12px; font-weight: 800; }
    .account-editor input { width: 100%; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 7px; padding: 9px 10px; color: var(--ink); background: #fff; font: inherit; }
    .password-input { display: flex; gap: 6px; }
    .password-input input { min-width: 0; }
    .password-toggle { border-color: #cbd5e1; background: #fff; color: #334155; padding: 7px 9px; font-size: 12px; }
    .remove-account { border-color: #fecaca; background: #fff1f2; color: #be123c; padding: 6px 9px; font-size: 12px; }
    .modal-actions { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
    .modal-save { border-color: var(--ok); background: var(--ok); }
    .modal-note { margin-top: 12px; font-size: 12px; color: var(--muted); line-height: 1.45; }
    .example-list { display: grid; gap: 8px; margin-top: 14px; }
    .example-choice { display: flex; justify-content: space-between; align-items: center; gap: 12px; border-color: #cbd5e1; background: #f8fafc; color: var(--ink); text-align: left; }
    .example-choice:hover { border-color: var(--accent); background: #eff6ff; }
    .pdf-preview-modal { width: min(1120px, calc(100vw - 28px)); height: min(820px, calc(100vh - 28px)); border: 0; border-radius: 14px; padding: 0; overflow: hidden; box-shadow: 0 26px 80px rgba(15, 23, 42, .38); }
    .pdf-preview-modal::backdrop { background: rgba(15, 23, 42, .65); backdrop-filter: blur(3px); }
    .pdf-preview-content { display: grid; grid-template-rows: auto auto minmax(0, 1fr); height: 100%; background: #fff; }
    .pdf-preview-header { display: flex; justify-content: space-between; gap: 16px; align-items: center; padding: 14px 18px; border-bottom: 1px solid var(--line); }
    .pdf-preview-header h2 { margin: 0; }
    .pdf-preview-files { display: flex; gap: 8px; flex-wrap: wrap; padding: 10px 18px; border-bottom: 1px solid var(--line); }
    .pdf-preview-file { border-color: #cbd5e1; background: #f8fafc; color: #334155; padding: 6px 9px; font-size: 12px; }
    .pdf-preview-file.active { border-color: var(--accent); background: var(--accent); color: #fff; }
    .pdf-preview-frame { width: 100%; height: 100%; border: 0; background: #e2e8f0; }
    .preview-pdf { border-color: #0f766e; background: #0f766e; padding: 5px 8px; font-size: 12px; }
    .example-choice small { color: var(--muted); font-weight: 700; }
    .muted { color: var(--muted); }
    .status {
      display: inline-flex;
      padding: 7px 10px;
      border-radius: 6px;
      background: var(--accent-soft);
      color: var(--accent);
      font-weight: 800;
      text-transform: uppercase;
      font-size: 12px;
    }
    .status.completed, .status.running, .status.waiting { background: var(--ok-soft); color: var(--ok); }
    .status.waiting_capacity { background: var(--warn-soft); color: var(--warn); }
    .status.stale, .status.blocked, .status.captcha_pending { background: var(--bad-soft); color: var(--bad); }
    .status.captcha_solving { background: var(--warn-soft); color: var(--warn); }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.6fr) minmax(320px, .8fr);
      gap: 16px;
      margin-bottom: 16px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 18px;
      min-width: 0;
    }
    .headline {
      min-height: 240px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      background: linear-gradient(135deg, #fff 0%, #eef8ff 100%);
    }
    .headline h2 { font-size: 42px; line-height: 1.05; margin: 0; max-width: 760px; }
    .capacity {
      margin-top: 18px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.8);
    }
    .capacity-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-weight: 800;
      margin-bottom: 10px;
    }
    .bar { height: 18px; border-radius: 999px; overflow: hidden; background: #e8eef5; display: flex; }
    .bar span { display: block; min-width: 0; transition: width .2s ease; }
    .bar .used { background: var(--ok); }
    .bar .remaining { background: #dfe7f0; }
    .accounts {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0;
    }
    .account {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      position: relative;
    }
    .account.paused { border-color: var(--bad); background: var(--bad-soft); }
    .account.captcha_pending {
      border-color: var(--captcha);
      background: linear-gradient(135deg, var(--captcha-soft), #fff);
      animation: captchaBreath 2.8s ease-in-out infinite;
      animation-delay: calc(var(--wave-index, 0) * 110ms);
      box-shadow: 0 0 0 1px rgba(217, 119, 6, .16), 0 16px 40px rgba(217, 119, 6, .12);
    }
    .account.captcha_pending::before {
      content: "";
      position: absolute;
      inset: -3px;
      border: 2px solid rgba(8, 145, 178, .45);
      border-radius: 10px;
      pointer-events: none;
      animation: captchaBreath 2.8s ease-in-out infinite;
      animation-delay: calc(var(--wave-index, 0) * 110ms);
    }
    .account.captcha_solving {
      border-color: var(--warn);
      background: linear-gradient(110deg, var(--warn-soft) 20%, #fff 42%, var(--warn-soft) 64%);
      background-size: 240% 100%;
      animation: recoverySweep 1.8s linear infinite;
    }
    .account.quota_reached { border-color: var(--warn); background: var(--warn-soft); }
    .account-top { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
    .account-name { font-weight: 850; }
    .account-status-stack { display: grid; justify-items: end; gap: 4px; text-align: right; }
    .account-eligibility { color: var(--muted); font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: .03em; }
    .status.browser_authenticated { background: var(--ok-soft); color: var(--ok); }
    .status.browser_unconfirmed { background: var(--warn-soft); color: var(--warn); }
    .status.browser_offline { background: var(--bad-soft); color: var(--bad); }
    .account-count { font-size: 28px; font-weight: 900; margin: 8px 0; }
    .mini-bar { height: 10px; border-radius: 999px; overflow: hidden; background: #e8eef5; }
    .mini-bar span { display: block; height: 100%; background: var(--accent); }
    .account-route {
      margin-top: 12px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: color-mix(in srgb, var(--panel) 82%, var(--accent-soft));
    }
    .account-route-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 9px;
    }
    .account-route-title { font-size: 12px; font-weight: 900; }
    .route-live {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: var(--ok);
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .03em;
      white-space: nowrap;
    }
    .route-live::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 18%, transparent);
    }
    .route-live.standby { color: var(--muted); }
    .account-route-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px 12px;
      margin: 0;
    }
    .account-route-grid div { min-width: 0; }
    .account-route-grid dt {
      margin: 0 0 2px;
      color: var(--muted);
      font-size: 9px;
      font-weight: 900;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    .account-route-grid dd {
      margin: 0;
      color: var(--ink);
      font-size: 11px;
      font-weight: 800;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .account-action {
      margin-top: 12px;
      border-color: var(--bad);
      background: var(--bad);
      width: 100%;
    }
    .account-action[disabled] {
      cursor: not-allowed;
      opacity: .65;
    }
    .recovery-phase {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      color: var(--warn);
      font-size: 12px;
      font-weight: 850;
      text-transform: uppercase;
    }
    .recovery-spinner {
      width: 14px;
      height: 14px;
      border: 2px solid rgba(217, 119, 6, .24);
      border-top-color: var(--warn);
      border-radius: 50%;
      animation: recoverySpin .75s linear infinite;
      flex: 0 0 auto;
    }
    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .kpi .value { font-size: 28px; font-weight: 900; margin-top: 6px; }
    .alert {
      display: none;
      margin-bottom: 16px;
      border: 2px solid var(--bad);
      background: var(--bad-soft);
      border-radius: 8px;
      padding: 16px;
    }
    .alert.show { display: block; }
    .alert h2 { color: var(--bad); font-size: 22px; }
    @keyframes captchaBreath {
      0%, 100% {
        box-shadow: 0 0 0 1px rgba(217, 119, 6, .16), 0 16px 40px rgba(217, 119, 6, .12);
        transform: translateY(0);
      }
      50% {
        box-shadow: 0 0 0 4px rgba(8, 145, 178, .22), 0 20px 48px rgba(217, 119, 6, .2);
        transform: translateY(-1px);
      }
    }
    @keyframes recoverySpin { to { transform: rotate(360deg); } }
    @keyframes recoverySweep {
      from { background-position: 100% 0; }
      to { background-position: -100% 0; }
    }
    @media (prefers-reduced-motion: reduce) {
      .account.captcha_pending,
      .account.captcha_pending::before,
      .account.captcha_solving,
      .recovery-spinner {
        animation: none;
      }
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; }
    th { color: var(--muted); }
    .table-scroll { width: 100%; max-width: 100%; overflow-x: auto; overscroll-behavior-x: contain; }
    .jobs-table { min-width: 1080px; }
    .jobs-table th, .jobs-table td { padding: 8px 6px; }
    /* Tables scroll horizontally inside their own container. Keeping their
       headers in normal flow avoids overlays from the page-level header. */
    .jobs-table thead th { position: static; background: var(--panel); }
    .captcha-attempts-table { min-width: 1220px; table-layout: fixed; }
    .captcha-attempts-table th:nth-child(1) { width: 12%; }
    .captcha-attempts-table th:nth-child(2) { width: 13%; }
    .captcha-attempts-table th:nth-child(3) { width: 14%; }
    .captcha-attempts-table th:nth-child(4) { width: 11%; }
    .captcha-attempts-table th:nth-child(5) { width: 10%; }
    .captcha-attempts-table th:nth-child(6),
    .captcha-attempts-table th:nth-child(7) { width: 7%; }
    .captcha-attempts-table th:nth-child(8) { width: 12%; }
    .captcha-attempts-table th:nth-child(9) { width: 14%; }
    .captcha-attempts-table td:nth-child(3) { overflow: hidden; }
    .captcha-action {
      display: block;
      width: 100%;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      box-sizing: border-box;
      padding: 3px 4px;
      border-radius: 4px;
      cursor: help;
      font-size: 11px;
      line-height: 1.35;
    }
    .captcha-action:hover,
    .captcha-action:focus-visible {
      color: var(--accent);
      background: var(--accent-soft);
      outline: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
    }
    .captcha-provider {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 84px;
      padding: 5px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 11px;
      font-weight: 900;
      letter-spacing: .01em;
      white-space: nowrap;
    }
    .captcha-provider.capsolver { color: #087f5b; background: #e6fcf5; border-color: #96f2d7; }
    .captcha-provider.two-captcha { color: #5f3dc4; background: #f3f0ff; border-color: #d0bfff; }
    .captcha-provider.external { color: var(--muted); background: var(--soft); }
    .captcha-attempts-table td:last-child { overflow-wrap: anywhere; }
    .job-id { display: inline-block; max-width: 94px; white-space: nowrap; font-size: 11px; }
    .job-account { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; font-size: 12px; font-weight: 700; }
    .job-account-icon {
      width: 20px;
      height: 20px;
      display: inline-grid;
      place-items: center;
      border-radius: 50%;
      background: hsl(var(--avatar-hue) 72% 90%);
      color: hsl(var(--avatar-hue) 68% 32%);
      border: 1px solid hsl(var(--avatar-hue) 55% 72%);
      font-size: 11px;
      font-weight: 900;
    }
    .job-reason { display: block; min-width: 280px; max-width: 420px; color: var(--muted); font-size: 11px; line-height: 1.45; }
    .attempt-count { color: var(--accent); font-size: 11px; font-weight: 850; white-space: nowrap; }
    .attempt-summary,
    .attempt-account-summary { cursor: pointer; user-select: none; border: 0; box-shadow: none; }
    .attempt-disclosure-input {
      position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none;
    }
    .attempt-summary {
      display: inline-flex; align-items: center; gap: 6px; color: var(--accent); font-weight: 850;
      padding: 4px 6px; margin: -4px -6px; border-radius: 7px; background: transparent;
    }
    .attempt-summary:hover { background: var(--accent-soft); }
    .attempt-summary::before,
    .attempt-account-summary::before {
      content: "›"; display: inline-block; font-size: 16px; line-height: 1; transition: transform .15s ease;
    }
    .attempt-disclosure-input:checked + .attempt-summary::before,
    .attempt-disclosure-input:checked + .attempt-account-summary::before { transform: rotate(90deg); }
    .attempt-account-groups {
      display: none; gap: 7px; margin-top: 10px; padding-left: 12px;
      border-left: 2px solid var(--accent-soft);
    }
    .attempt-details > .attempt-disclosure-input:checked ~ .attempt-account-groups { display: grid; }
    .attempt-account-group {
      min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 9px;
      background: #f8fafc;
    }
    .attempt-account-summary {
      width: 100%;
      display: grid; grid-template-columns: 12px minmax(118px, auto) minmax(0, 1fr) auto;
      align-items: center; gap: 7px; min-height: 42px; padding: 7px 9px; color: var(--ink); background: transparent;
      text-align: left;
    }
    .attempt-account-summary:hover { background: var(--accent-soft); }
    .attempt-account-summary::before { color: var(--accent); }
    .attempt-latest { min-width: 0; color: var(--muted); line-height: 1.3; overflow-wrap: anywhere; }
    .attempt-latest strong { color: var(--ink); }
    .attempt-account-count {
      padding: 3px 7px; border-radius: 999px; color: var(--accent); background: var(--accent-soft);
      font-size: 10px; font-weight: 900; white-space: nowrap;
    }
    .attempt-history {
      position: relative; display: none; gap: 5px; margin: 0 9px 9px 27px; padding: 8px 0 0 15px;
      border-top: 1px solid var(--line); border-left: 1px solid var(--line);
    }
    .attempt-account-group > .attempt-disclosure-input:checked ~ .attempt-history { display: grid; }
    .attempt-history::before {
      content: "Historial"; position: absolute; top: 8px; left: 14px; color: var(--muted);
      font-size: 9px; font-weight: 900; letter-spacing: .07em; text-transform: uppercase;
    }
    .attempt-history-row {
      display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 8px; align-items: start;
      min-width: 0; padding: 5px 7px; margin-top: 14px; border-radius: 7px; color: var(--ink); background: var(--panel);
    }
    .attempt-history-row + .attempt-history-row { margin-top: 0; }
    .attempt-history-row.latest { box-shadow: inset 3px 0 0 var(--accent); }
    .attempt-number { color: var(--accent); font-weight: 900; }
    .attempt-outcome { min-width: 0; color: var(--ink); overflow-wrap: anywhere; }
    .attempt-outcome strong { font-weight: 850; }
    .document-ordinal {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 38px;
      padding: 4px 7px;
      border-radius: 999px;
      background: var(--ok-soft);
      color: var(--ok);
      font-size: 11px;
      font-weight: 900;
      white-space: nowrap;
    }
    a { color: var(--accent); text-decoration: none; font-weight: 700; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    @media (max-width: 900px) {
      .hero, .accounts, .kpis, .onboarding { grid-template-columns: 1fr; }
      .request-document-fields { grid-template-columns: 1fr; }
      .headline h2 { font-size: 34px; }
      .table-scroll { overflow-x: auto; }
    }
    /* Operator console refresh */
    :root {
      --bg: #f4f7fb;
      --ink: #162033;
      --muted: #68748a;
      --panel: #ffffff;
      --line: #dce3ee;
      --accent: #4f46e5;
      --accent-soft: #eef2ff;
      --ok: #078766;
      --ok-soft: #e9f8f3;
      --shadow: 0 12px 32px rgba(21, 32, 51, .07);
      color-scheme: light;
    }
    :root[data-theme="dark"] {
      --bg: #0b1220;
      --ink: #e7edf7;
      --muted: #9aa9bd;
      --panel: #111b2d;
      --line: #29384f;
      --accent: #818cf8;
      --accent-soft: #222952;
      --ok: #34d399;
      --ok-soft: #10352d;
      --warn: #fbbf24;
      --warn-soft: #3b2b0d;
      --bad: #fb7185;
      --bad-soft: #3a1822;
      --captcha: #f59e0b;
      --captcha-soft: #352611;
      --captcha-wave: #22d3ee;
      --shadow: 0 14px 38px rgba(0, 0, 0, .28);
      color-scheme: dark;
    }
    body { min-height: 100vh; }
    [hidden] { display: none !important; }
    header {
      min-height: 76px;
      padding: 14px max(24px, calc((100vw - 1440px) / 2));
      color: #f8fafc;
      background: #111827;
      border-bottom: 1px solid rgba(255,255,255,.08);
      box-shadow: 0 10px 30px rgba(15, 23, 42, .16);
      z-index: 20;
    }
    header .muted { color: #aab5c5; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-mark {
      width: 42px; height: 42px; display: grid; place-items: center;
      color: #c7d2fe; background: rgba(99,102,241,.18);
      border: 1px solid rgba(165,180,252,.26); border-radius: 13px;
    }
    .brand-mark svg { width: 22px; height: 22px; }
    .header-actions { display: flex; gap: 8px; align-items: stretch; }
    .header-button {
      min-height: 44px; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      border-color: rgba(255,255,255,.18); color: #f8fafc; background: rgba(255,255,255,.08);
      border-radius: 10px; padding: 8px 12px;
    }
    .header-button:hover { background: rgba(255,255,255,.14); }
    .theme-toggle { min-width: 132px; }
    .theme-toggle svg { transition: transform .2s ease; }
    .theme-toggle:hover svg { transform: rotate(-10deg); }
    .header-button svg, button svg, .section-title svg { width: 17px; height: 17px; stroke-width: 2; }
    .icon-label { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
    main { max-width: 1440px; padding-top: 24px; }
    button { border-radius: 10px; transition: transform .12s ease, box-shadow .12s ease, background .12s ease; }
    button:not([disabled]):hover { transform: translateY(-1px); box-shadow: 0 7px 18px rgba(15,23,42,.12); }
    button:focus-visible, input:focus-visible { outline: 3px solid rgba(79,70,229,.24); outline-offset: 2px; }
    .panel, .account, .kpi { border-radius: 14px; box-shadow: var(--shadow); }
    .panel { padding: 20px; }
    .onboarding { border-color: #d9defd; background: linear-gradient(135deg, #fff 0%, #f5f7ff 100%); }
    .onboarding-action { border-color: var(--accent); background: var(--accent); }
    .headline { background: radial-gradient(circle at 100% 0, #e7edff 0, transparent 42%), #fff; }
    .headline h2 { letter-spacing: -.035em; }
    .status { border-radius: 999px; letter-spacing: .04em; }
    .worker-action { min-height: 44px; border-radius: 10px; }
    .runtime-status-card {
      min-height: 44px;
      display: grid;
      grid-template-columns: auto minmax(150px, 210px);
      align-items: center;
      gap: 9px;
      padding: 6px 10px;
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 11px;
      background: rgba(255,255,255,.055);
    }
    .runtime-status-card .status { padding: 5px 8px; font-size: 10px; line-height: 1; }
    .runtime-status-card .worker-control-hint {
      max-width: 210px;
      margin: 0;
      color: #c1cad8;
      font-size: 10.5px;
      line-height: 1.25;
      text-align: left;
    }
    .section-title { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; }
    .section-title h2 { margin: 0; }
    .section-title-icon {
      width: 32px; height: 32px; display: grid; place-items: center;
      border-radius: 9px; color: var(--accent); background: var(--accent-soft);
    }
    .kpi { min-height: 112px; display: flex; flex-direction: column; justify-content: space-between; }
    .kpi-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .kpi-icon {
      width: 34px; height: 34px; display: grid; place-items: center;
      color: var(--accent); background: var(--accent-soft); border-radius: 10px;
    }
    .kpi-icon.ok { color: var(--ok); background: var(--ok-soft); }
    .kpi-icon.warn { color: var(--warn); background: var(--warn-soft); }
    dialog.config-modal { border-radius: 20px; }
    .settings-modal {
      width: min(1040px, calc(100vw - 28px)) !important;
      height: min(880px, calc(100vh - 28px));
      max-height: min(880px, calc(100vh - 28px)) !important;
      overflow: hidden;
    }
    .settings-modal .config-modal-content {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      height: 100%;
      min-height: 0;
      padding: 0;
    }
    .settings-modal .config-modal-header { margin: 0; padding: 22px 24px; border-bottom: 1px solid var(--line); }
    .settings-body {
      min-height: 0;
      padding: 20px 24px 24px;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      background: #f8fafc;
    }
    .settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 16px; }
    .settings-section { padding: 18px; border: 1px solid var(--line); border-radius: 14px; background: #fff; }
    .settings-section.wide { grid-column: 1 / -1; }
    .settings-section p { margin: -4px 0 16px; font-size: 12px; line-height: 1.5; }
    .field-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; }
    .setting-field { display: grid; gap: 6px; min-width: 0; }
    .setting-field > span { color: #43506a; font-size: 12px; font-weight: 800; }
    .setting-field input[type="number"] {
      width: 100%; min-height: 42px; border: 1px solid #cbd5e1; border-radius: 10px;
      padding: 9px 11px; color: var(--ink); background: #fff; font: inherit;
    }
    .setting-field small { color: var(--muted); line-height: 1.35; }
    .switch-row {
      display: flex; justify-content: space-between; align-items: center; gap: 16px;
      padding: 12px 0; border-bottom: 1px solid #edf1f6;
    }
    .switch-row:last-child { border-bottom: 0; padding-bottom: 0; }
    .switch-copy strong { display: block; font-size: 13px; }
    .switch-copy small { display: block; margin-top: 3px; color: var(--muted); line-height: 1.35; }
    .switch { position: relative; width: 44px; height: 24px; flex: 0 0 auto; }
    .switch input { position: absolute; opacity: 0; pointer-events: none; }
    .switch span { position: absolute; inset: 0; border-radius: 999px; background: #cbd5e1; cursor: pointer; transition: .18s ease; }
    .switch span::after { content: ""; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px; border-radius: 50%; background: #fff; box-shadow: 0 2px 6px rgba(15,23,42,.24); transition: .18s ease; }
    .switch input:checked + span { background: var(--accent); }
    .switch input:checked + span::after { transform: translateX(20px); }
    .locked-grid { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 10px; }
    .locked-item { display: flex; gap: 9px; align-items: flex-start; padding: 11px; border: 1px solid #e2e8f0; border-radius: 11px; background: #f8fafc; }
    .locked-item svg { width: 17px; height: 17px; flex: 0 0 auto; margin-top: 1px; color: var(--ok); }
    .locked-item strong { display: block; font-size: 12px; }
    .locked-item small { display: block; color: var(--muted); margin-top: 3px; line-height: 1.3; }
    .settings-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 24px; border-top: 1px solid var(--line); background: #fff; }
    .settings-save { display: inline-flex; align-items: center; gap: 8px; border-color: var(--accent); background: var(--accent); }
    .settings-save[disabled] { opacity: .55; cursor: not-allowed; }
    .danger-note { margin-top: 12px; padding: 10px 12px; border-radius: 10px; color: #92400e; background: #fffbeb; border: 1px solid #fde68a; font-size: 12px; line-height: 1.45; }
    :root[data-theme="dark"] header { background: #080f1c; }
    :root[data-theme="dark"] header .muted { color: #9eacc0; }
    :root[data-theme="dark"] .onboarding {
      border-color: #343f72;
      background: linear-gradient(135deg, #111b2d 0%, #171d3a 100%);
    }
    :root[data-theme="dark"] .auto-captcha-card { border-color: #8b5cf6; background: linear-gradient(135deg, #21183d, #17203b); }
    :root[data-theme="dark"] .auto-captcha-copy strong { color: #ddd6fe; }
    :root[data-theme="dark"] .auto-captcha-copy small { color: #aebbd0; }
    :root[data-theme="dark"] .headline {
      background: radial-gradient(circle at 100% 0, #202950 0, transparent 44%), var(--panel);
    }
    :root[data-theme="dark"] .capacity { background: rgba(7, 13, 24, .48); }
    :root[data-theme="dark"] .bar,
    :root[data-theme="dark"] .mini-bar { background: #26354a; }
    :root[data-theme="dark"] .bar .remaining { background: #34445b; }
    :root[data-theme="dark"] .account.captcha_pending {
      background: linear-gradient(135deg, var(--captcha-soft), var(--panel));
    }
    :root[data-theme="dark"] .account.captcha_solving {
      background: linear-gradient(110deg, var(--warn-soft) 20%, var(--panel) 42%, var(--warn-soft) 64%);
      background-size: 240% 100%;
    }
    :root[data-theme="dark"] .config-modal-content,
    :root[data-theme="dark"] .pdf-preview-content,
    :root[data-theme="dark"] .settings-footer { background: var(--panel); }
    :root[data-theme="dark"] .settings-body { background: #0d1626; }
    :root[data-theme="dark"] .settings-section,
    :root[data-theme="dark"] .account-editor,
    :root[data-theme="dark"] .attempt-account-group,
    :root[data-theme="dark"] .locked-item { background: #142034; }
    :root[data-theme="dark"] .request-composer input,
    :root[data-theme="dark"] .account-editor input,
    :root[data-theme="dark"] .setting-field input[type="number"] {
      border-color: #35465f;
      color: var(--ink);
      background: #0d1728;
    }
    :root[data-theme="dark"] .modal-close,
    :root[data-theme="dark"] .password-toggle,
    :root[data-theme="dark"] .request-type-tab,
    :root[data-theme="dark"] .example-choice,
    :root[data-theme="dark"] .pdf-preview-file {
      border-color: #3a4a63;
      color: var(--ink);
      background: #162237;
    }
    :root[data-theme="dark"] .example-choice:hover { border-color: var(--accent); background: #222952; }
    :root[data-theme="dark"] .remove-account { border-color: #713243; color: #fda4af; background: #341822; }
    :root[data-theme="dark"] .switch-row { border-bottom-color: #26354a; }
    :root[data-theme="dark"] .locked-item { border-color: #2c3b52; }
    :root[data-theme="dark"] .danger-note { color: #fcd34d; background: #34270e; border-color: #6d5114; }
    :root[data-theme="dark"] .pdf-preview-frame { background: #060b13; }
    :root[data-theme="dark"] .job-account-icon {
      background: hsl(var(--avatar-hue) 40% 24%);
      color: hsl(var(--avatar-hue) 80% 82%);
      border-color: hsl(var(--avatar-hue) 38% 38%);
    }
    @media (max-width: 900px) {
      header { align-items: flex-start; gap: 12px; }
      .header-actions { flex-wrap: wrap; justify-content: flex-end; }
      .runtime-status-card { grid-template-columns: auto minmax(130px, 190px); }
      .settings-grid, .field-grid, .locked-grid { grid-template-columns: 1fr; }
      .settings-section.wide { grid-column: auto; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-mark"><i data-lucide="landmark" aria-hidden="true"></i></div>
      <div>
        <h1>Pool de Consultas CBRS</h1>
        <div id="accountSummary" class="muted">Cuentas autorizadas · capacidad controlada</div>
      </div>
    </div>
    <div class="header-actions">
      <button id="themeToggle" class="header-button theme-toggle" type="button" aria-label="Activar modo oscuro" aria-pressed="false"><i data-lucide="moon" aria-hidden="true"></i> <span>Modo oscuro</span></button>
      <button id="configureProduction" class="header-button" type="button"><i data-lucide="sliders-horizontal" aria-hidden="true"></i> Configuración</button>
      <button id="stopButton" class="worker-action" disabled><i data-lucide="loader-circle" aria-hidden="true"></i> Comprobando…</button>
      <div class="runtime-status-card" aria-label="Estado operativo del worker">
        <div id="status" class="status">cargando</div>
        <div id="workerControlHint" class="muted worker-control-hint"></div>
      </div>
    </div>
  </header>
  <main>
    <section id="alert" class="alert">
      <h2 id="alertTitle">Advertencia</h2>
      <div id="alertMessage">-</div>
    </section>

    <section class="panel onboarding" aria-label="Centro de control">
      <div>
        <h2>Centro de control</h2>
        <p class="muted">Administra la configuración local, la recuperación visual y las solicitudes. El panel nunca muestra ni transmite contraseñas.</p>
        <div class="onboarding-actions" style="margin-top:12px">
          <button id="configureAccounts" class="onboarding-action" type="button"><i data-lucide="users-round" aria-hidden="true"></i> Configurar cuentas</button>
        </div>
        <div id="controlFeedback" class="control-feedback" role="status"></div>
      </div>
      <div>
        <h2>Crear solicitud</h2>
        <p class="muted">Elige cómo localizar la inscripción y qué hacer con ella.</p>
        <div class="request-type-tabs" role="tablist" aria-label="Tipo de búsqueda">
          <button class="request-type-tab active" data-request-type="text" type="button"><i data-lucide="building-2" aria-hidden="true"></i> Por empresa</button>
          <button class="request-type-tab" data-request-type="fna" type="button"><i data-lucide="file-search" aria-hidden="true"></i> Por documento</button>
          <button id="openExamples" class="example-trigger" type="button" title="Ejemplos ya descargados correctamente"><i data-lucide="history" aria-hidden="true"></i></button>
        </div>
        <form id="requestComposer" class="request-composer">
          <div id="textRequestFields"><input id="jobText" type="text" maxlength="500" autocomplete="off" placeholder="Razón social autorizada" aria-label="Razón social autorizada" /></div>
          <div id="fnaRequestFields" class="request-document-fields" hidden>
            <input id="documentFoja" type="number" min="1" placeholder="Foja" aria-label="Foja" />
            <input id="documentNumero" type="number" min="1" placeholder="Número" aria-label="Número" />
            <input id="documentYear" type="number" min="1800" max="2200" placeholder="Año" aria-label="Año" />
          </div>
          <div class="request-actions">
            <button class="onboarding-action" data-request-action="queue" type="submit"><i data-lucide="list-plus" aria-hidden="true"></i> Agregar a cola</button>
            <button class="instant-action" data-request-action="instant" type="submit"><i data-lucide="download" aria-hidden="true"></i> Buscar y descargar ahora</button>
          </div>
        </form>
      </div>
    </section>

    <dialog id="examplesModal" class="config-modal" aria-labelledby="examplesModalTitle">
      <div class="config-modal-content">
        <div class="config-modal-header">
          <div><h2 id="examplesModalTitle">Ejemplos comprobados</h2><p class="muted">Estas coordenadas ya generaron al menos un PDF correctamente en este equipo.</p></div>
          <button id="closeExamplesModal" class="modal-close" type="button" aria-label="Cerrar"><i data-lucide="x" aria-hidden="true"></i></button>
        </div>
        <div id="exampleList" class="example-list"><span class="muted">Cargando ejemplos…</span></div>
      </div>
    </dialog>

    <dialog id="pdfPreviewModal" class="pdf-preview-modal" aria-labelledby="pdfPreviewTitle">
      <div class="pdf-preview-content">
        <div class="pdf-preview-header"><h2 id="pdfPreviewTitle">Vista previa del PDF</h2><button id="closePdfPreview" class="modal-close" type="button" aria-label="Cerrar"><i data-lucide="x" aria-hidden="true"></i></button></div>
        <div id="pdfPreviewFiles" class="pdf-preview-files"></div>
        <iframe id="pdfPreviewFrame" class="pdf-preview-frame" title="Vista previa del PDF" referrerpolicy="no-referrer"></iframe>
      </div>
    </dialog>

    <dialog id="configModal" class="config-modal" aria-labelledby="configModalTitle">
      <div class="config-modal-content">
        <div class="config-modal-header">
          <div><h2 id="configModalTitle">Cuentas autorizadas</h2><p class="muted">Agrega, actualiza o elimina cuentas locales. El worker debe estar detenido.</p></div>
          <button id="closeConfigModal" class="modal-close" type="button" aria-label="Cerrar"><i data-lucide="x" aria-hidden="true"></i></button>
        </div>
        <div id="accountEditorList" class="account-editor-list"></div>
        <div class="modal-actions">
          <button id="addAccount" class="onboarding-action results" type="button"><i data-lucide="user-plus" aria-hidden="true"></i> Agregar cuenta</button>
          <button id="saveAccounts" class="modal-save" type="button"><i data-lucide="save" aria-hidden="true"></i> Guardar configuración</button>
        </div>
        <div id="configModalFeedback" class="control-feedback" role="status"></div>
        <p class="modal-note">Las contraseñas existentes nunca se cargan aquí. Déjalas vacías para conservarlas; usa <strong>Ver</strong> solo para revisar una contraseña que hayas escrito durante esta sesión.</p>
      </div>
    </dialog>

    <dialog id="productionSettingsModal" class="config-modal settings-modal" aria-labelledby="productionSettingsTitle">
      <form id="productionSettingsForm" class="config-modal-content">
        <div class="config-modal-header">
          <div>
            <h2 id="productionSettingsTitle">Configuración de producción</h2>
            <p class="muted">Ajusta tiempos y límites operativos. El worker debe estar detenido para guardar.</p>
          </div>
          <button id="closeProductionSettings" class="modal-close" type="button" aria-label="Cerrar"><i data-lucide="x" aria-hidden="true"></i></button>
        </div>
        <div class="settings-body">
          <div class="settings-grid">
            <section class="settings-section">
              <div class="section-title"><span class="section-title-icon"><i data-lucide="activity" aria-hidden="true"></i></span><h2>Comportamiento humano</h2></div>
              <p class="muted">Añade variación entre trabajos; la demora mínima segura por petición siempre permanece activa.</p>
              <div class="switch-row">
                <div class="switch-copy"><strong>Jitter entre trabajos</strong><small>Activa una pausa aleatoria dentro del rango configurado.</small></div>
                <label class="switch"><input id="settingHumanLike" type="checkbox"><span></span></label>
              </div>
              <div class="field-grid" style="margin-top:14px">
                <label class="setting-field"><span>Mínimo</span><input id="settingJitterMin" type="number" min="0" max="3600" step="1"><small>segundos</small></label>
                <label class="setting-field"><span>Máximo</span><input id="settingJitterMax" type="number" min="0" max="3600" step="1"><small>segundos</small></label>
              </div>
            </section>

            <section class="settings-section">
              <div class="section-title"><span class="section-title-icon"><i data-lucide="list-ordered" aria-hidden="true"></i></span><h2>Cola de producción</h2></div>
              <p class="muted">Controla cuánto trabajo aceptar y con qué frecuencia revisar la cola.</p>
              <div class="field-grid">
                <label class="setting-field"><span>Máximo pendiente</span><input id="settingQueueMax" type="number" min="1" max="10000" step="1"><small>jobs de producción</small></label>
                <label class="setting-field"><span>Polling del worker</span><input id="settingWorkerPoll" type="number" min="0.1" max="300" step="0.1"><small>segundos</small></label>
              </div>
              <div class="switch-row" style="margin-top:8px">
                <div class="switch-copy"><strong>Descarga inmediata</strong><small>Permite que “Buscar y descargar ahora” solicite el arranque del worker.</small></div>
                <label class="switch"><input id="settingInstantJobs" type="checkbox"><span></span></label>
              </div>
            </section>

            <section class="settings-section">
              <div class="section-title"><span class="section-title-icon"><i data-lucide="repeat-2" aria-hidden="true"></i></span><h2>Endurance</h2></div>
              <p class="muted">Mantiene como máximo un trabajo de endurance pendiente o ejecutándose.</p>
              <div class="switch-row">
                <div class="switch-copy"><strong>Generación automática</strong><small>El run-once manual sigue disponible cuando está apagada.</small></div>
                <label class="switch"><input id="settingEnduranceEnabled" type="checkbox"><span></span></label>
              </div>
              <div class="field-grid" style="margin-top:14px">
                <label class="setting-field"><span>Cooldown</span><input id="settingCooldown" type="number" min="60" max="300" step="60"><small>máximo 5 minutos</small></label>
                <label class="setting-field"><span>Asignación diaria</span><input id="settingEnduranceQuota" type="number" min="1" max="20" step="1"><small>jobs por cuenta</small></label>
                <label class="setting-field"><span>Reserva producción</span><input id="settingProductionReserve" type="number" min="0" max="20" step="1"><small>cupos por cuenta</small></label>
                <label class="setting-field"><span>Cupo base</span><input id="settingDailyQuota" type="number" min="1" max="20" step="1"><small>máximo diario por cuenta</small></label>
              </div>
            </section>

            <section class="settings-section wide">
              <div class="section-title"><span class="section-title-icon"><i data-lucide="shield-check" aria-hidden="true"></i></span><h2>Protecciones activas</h2></div>
              <p class="muted">Estos controles no se pueden relajar desde el dashboard.</p>
              <div class="locked-grid">
                <div class="locked-item"><i data-lucide="rotate-cw" aria-hidden="true"></i><div><strong>Round-robin estricto</strong><small>Producción tiene prioridad.</small></div></div>
                <div class="locked-item"><i data-lucide="badge-check" aria-hidden="true"></i><div><strong>Una IP por cuenta</strong><small>Egreso chileno verificado.</small></div></div>
                <div class="locked-item"><i data-lucide="bot-off" aria-hidden="true"></i><div><strong>Solver externo manual</strong><small>Un solve pagado por autorización.</small></div></div>
                <div class="locked-item"><i data-lucide="file-lock-2" aria-hidden="true"></i><div><strong>Transporte browser-only</strong><small>PDFs por el perfil asignado.</small></div></div>
                <div class="locked-item"><i data-lucide="gauge" aria-hidden="true"></i><div><strong>Sin catch-up</strong><small>No crea ráfagas tras downtime.</small></div></div>
                <div class="locked-item"><i data-lucide="server" aria-hidden="true"></i><div><strong>Loopback-only</strong><small>Dashboard sólo en este equipo.</small></div></div>
              </div>
              <div id="runtimeSettingsSummary" class="danger-note">Cargando protecciones del runtime…</div>
            </section>
          </div>
        </div>
        <div class="settings-footer">
          <div><div id="productionSettingsFeedback" class="control-feedback" role="status"></div><small class="muted">Los cambios se aplican al siguiente arranque del worker.</small></div>
          <button id="saveProductionSettings" class="settings-save" type="submit"><i data-lucide="save" aria-hidden="true"></i> Guardar cambios</button>
        </div>
      </form>
    </dialog>

    <div class="hero">
      <section class="panel headline">
        <div>
          <div class="muted">Evidencia actual</div>
          <h2 id="headline">Cargando pool de consultas</h2>
          <p id="headlineSub" class="muted">El panel está leyendo el estado local.</p>
        </div>
        <div class="capacity">
          <div class="capacity-row">
            <span>Consultas disponibles hoy</span>
            <span id="capacityNumber">- / 60</span>
          </div>
          <div class="bar">
            <span id="usedBar" class="used"></span>
            <span id="remainingBar" class="remaining"></span>
          </div>
        </div>
      </section>

      <section class="panel">
        <h2>Estado del pool</h2>
        <table>
          <tbody id="poolFacts"></tbody>
        </table>
      </section>
    </div>

    <div id="accounts" class="accounts"></div>

    <div class="kpis">
      <div class="kpi"><div class="kpi-top"><div class="muted">Usadas hoy</div><span class="kpi-icon"><i data-lucide="circle-check-big"></i></span></div><div id="usedToday" class="value">-</div></div>
      <div class="kpi"><div class="kpi-top"><div class="muted">Restantes hoy</div><span class="kpi-icon ok"><i data-lucide="battery-charging"></i></span></div><div id="remainingToday" class="value">-</div></div>
      <div class="kpi"><div class="kpi-top"><div class="muted">PDFs generados</div><span class="kpi-icon"><i data-lucide="file-text"></i></span></div><div id="downloads" class="value">-</div></div>
      <div class="kpi"><div class="kpi-top"><div class="muted">Captchas pendientes</div><span class="kpi-icon warn"><i data-lucide="shield-question"></i></span></div><div id="captchaPending" class="value">-</div></div>
      <div class="kpi"><div class="kpi-top"><div class="muted">Solves pagados hoy</div><span class="kpi-icon"><i data-lucide="key-round"></i></span></div><div id="paidCaptcha" class="value">-</div></div>
      <div class="kpi"><div class="kpi-top"><div class="muted">Jobs en cola</div><span class="kpi-icon"><i data-lucide="list-todo"></i></span></div><div id="queuedJobs" class="value">-</div></div>
      <div class="kpi"><div class="kpi-top"><div class="muted">Respaldo</div><span class="kpi-icon ok"><i data-lucide="database-backup"></i></span></div><div id="backupState" class="value" style="font-size:18px">-</div></div>
    </div>

    <section class="panel" style="margin-bottom:16px">
      <div class="section-title"><span class="section-title-icon"><i data-lucide="repeat-2"></i></span><h2>Endurance</h2></div>
      <p id="enduranceStatus" class="muted">Cargando estado…</p>
      <div class="request-actions">
        <button data-endurance-action="pause" type="button"><i data-lucide="pause"></i> Pausar</button>
        <button class="onboarding-action" data-endurance-action="resume" type="button"><i data-lucide="play"></i> Reanudar</button>
        <button class="instant-action" data-endurance-action="run-once" type="button"><i data-lucide="play-circle"></i> Ejecutar una vez</button>
      </div>
    </section>

    <section class="auto-captcha-card" aria-label="Control automático del solver externo">
      <div class="auto-captcha-copy">
        <strong>🤖 SOLVER EXTERNO AUTOMÁTICO</strong>
        <small>Si lo activas, cada cuenta prueba primero el token del navegador y usa un solve pagado únicamente tras un rechazo real de CBRS. Los intentos y costos siguen visibles abajo.</small>
      </div>
      <div class="auto-captcha-control">
        <span id="automaticCaptchaLabel">DESACTIVADO</span>
        <label class="switch" title="Permitir solves automáticos del proveedor configurado">
          <input id="automaticCaptchaToggle" type="checkbox" aria-label="Activar solves automáticos del proveedor configurado">
          <span></span>
        </label>
      </div>
    </section>

    <section id="jobsPanel" class="panel" style="margin-bottom:16px;display:none">
      <div class="section-title"><span class="section-title-icon"><i data-lucide="inbox"></i></span><h2>Solicitudes recientes</h2></div>
      <div class="table-scroll">
        <table class="jobs-table">
          <thead>
            <tr><th>Job</th><th>Tipo</th><th>Cuenta</th><th>N.º diario</th><th>Estado</th><th>Motivo</th><th>Resultados</th><th>PDFs</th><th>Finalizado</th><th>Acción</th></tr>
          </thead>
          <tbody id="jobs"></tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="section-title"><span class="section-title-icon"><i data-lucide="history"></i></span><h2>Ciclos recientes</h2></div>
      <table>
        <thead>
          <tr><th>#</th><th>Cuenta</th><th>Estado</th><th>Resultados</th><th>PDF</th><th>Parada</th><th>Finalizado</th></tr>
        </thead>
        <tbody id="cycles"></tbody>
      </table>
    </section>

    <section class="panel" style="margin-top:16px">
      <div class="section-title"><span class="section-title-icon"><i data-lucide="key-round"></i></span><h2>Intentos de solver externo</h2></div>
      <p class="muted">Registra el proveedor, si realmente creó un token y si CBRS lo aceptó. Nunca incluye tokens, claves, proxies ni datos del worker.</p>
      <div class="table-scroll">
        <table class="jobs-table captcha-attempts-table">
          <thead>
            <tr><th>Inicio</th><th>Cuenta</th><th title="Pasa el cursor sobre una acción para ver su nombre completo">Acción</th><th>Servicio CAPTCHA</th><th>Resultado solver</th><th>Costo</th><th>Demora</th><th>Resultado CBRS</th><th>Detalle</th></tr>
          </thead>
          <tbody id="captchaAttempts"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script src="https://unpkg.com/lucide@1.33.0/dist/umd/lucide.min.js" crossorigin="anonymous"></script>
  <script>
    const statusLabels = {
      available: "disponible",
      blocked: "bloqueado",
      captcha_pending: "captcha pendiente",
      captcha_solving: "resolviendo captcha",
      cancelled: "cancelado",
      completed: "completado",
      disabled: "deshabilitada",
      failed: "fallido",
      partial: "parcial",
      queued: "en cola",
      healthy: "saludable",
      low_disk: "poco espacio",
      invalid: "inválido",
      not_configured: "no configurado",
      not_started: "no iniciado",
      passed: "correcto",
      paused: "pausada",
      quota_reached: "cupo usado",
      running: "ejecutando",
      stale: "sin latido",
      stopped: "detenido",
      waiting: "en espera",
      waiting_capacity: "sin capacidad",
      waiting_captcha: "esperando captcha"
    };
    const captchaPhaseLabels = {
      automatic_login: "iniciando sesión automáticamente",
      waiting_operator: "esperando intervención visual",
      validating: "validando sesión y reCAPTCHA"
    };
    const label = (value) => statusLabels[value] || value || "-";
    const providerLabel = (value) => value === "2captcha_dedicated_isp"
      ? "2Captcha ISP dedicado"
      : value === "2captcha_residential_sticky"
        ? "2Captcha residencial · 120 min"
        : value === "dataimpulse_residential_sticky"
          ? "DataImpulse residencial sticky"
        : value === "generic_static" ? "Estático genérico" : value || "-";
    const providerHealthLabel = (provider) => {
      if (!provider) return "No aplica";
      const labels = {
        healthy: "Activo · tráfico disponible",
        configured: "Configurado · validación por ruta",
        inactive: "Cuenta proxy inactiva",
        depleted: "Sin tráfico disponible",
        unavailable: "API no disponible",
        not_configured: "API key no configurada",
      };
      return labels[provider.status] || provider.status || "-";
    };
    const localTime = (value) => value ? new Date(value).toLocaleString() : "-";
    const shortJobId = (value) => {
      const suffix = String(value || "").split("-").at(-1);
      return suffix ? `…${suffix}` : "-";
    };
    const avatarHue = (value) => Array.from(value || "").reduce(
      (hue, character) => (hue * 31 + character.codePointAt(0)) % 360,
      210,
    );
    const stopReasonLabels = {
      auth_expired: "sesión vencida",
      auth_required: "sesión requerida",
      captcha_pending: "captcha pendiente",
      captcha_rejected: "captcha pendiente",
      credentials_invalid: "credenciales inválidas",
      credentials_missing: "faltan credenciales",
      daily_limit: "límite diario informado por CBRS",
      egress_preflight_failed: "egress no aprobado",
      failed: "fallo de ejecución",
      gate_failed: "validación de red fallida",
      proxy_health_failed: "proxy no saludable",
      rate_limit: "rate limit global",
      startup_auth_failed: "falló autenticación inicial",
      unexpected_worker_failure: "fallo inesperado del navegador",
      waf_challenge: "bloqueo WAF",
      waiting_capacity: "sin cuentas elegibles",
      waiting_captcha: "esperando captcha",
    };
    const secondsUntil = (value) => {
      if (!value) return null;
      return Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 1000));
    };
    const fmtSeconds = (value) => {
      if (value === null || value === undefined) return "-";
      const h = Math.floor(value / 3600);
      const m = Math.floor((value % 3600) / 60);
      const s = Math.floor(value % 60);
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s}s`;
      return `${s}s`;
    };
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }[char]));
    const refreshIcons = () => {
      if (window.lucide) window.lucide.createIcons();
    };
    const applyTheme = (theme, { persist = false } = {}) => {
      const selected = theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = selected;
      const toggle = document.getElementById("themeToggle");
      const dark = selected === "dark";
      toggle.setAttribute("aria-pressed", String(dark));
      toggle.setAttribute("aria-label", dark ? "Activar modo claro" : "Activar modo oscuro");
      toggle.innerHTML = dark
        ? '<i data-lucide="sun" aria-hidden="true"></i> <span>Modo claro</span>'
        : '<i data-lucide="moon" aria-hidden="true"></i> <span>Modo oscuro</span>';
      if (persist) {
        try { localStorage.setItem("cbrs-dashboard-theme", selected); } catch (_) {}
      }
      refreshIcons();
    };
    applyTheme(document.documentElement.dataset.theme);
    document.getElementById("themeToggle").addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", { persist: true });
    });
    let current = null;
    let lastCaptchaNoticeKey = "";
    let stopRequested = false;
    let resumeRequested = false;
    let renderedJobsSignature = "";
    async function refresh() {
      const response = await fetch("/api/status");
      current = await response.json();
      render();
    }
    function renderStopButton(status) {
      const button = document.getElementById("stopButton");
      const active = ["running", "waiting", "waiting_capacity", "waiting_captcha"].includes(status);
      const hint = document.getElementById("workerControlHint");
      if (active) {
        resumeRequested = false;
        button.className = "worker-action stop";
        button.disabled = stopRequested;
        button.innerHTML = stopRequested
          ? '<i data-lucide="loader-circle" aria-hidden="true"></i> Deteniendo…'
          : '<i data-lucide="square" aria-hidden="true"></i> Detener';
        hint.textContent = stopRequested
          ? "Esperando el punto seguro actual."
          : "Se detiene al terminar el trabajo actual.";
        return;
      }
      stopRequested = false;
      button.className = "worker-action resume";
      button.disabled = resumeRequested;
      button.innerHTML = resumeRequested
        ? '<i data-lucide="loader-circle" aria-hidden="true"></i> Reanudando…'
        : '<i data-lucide="play" aria-hidden="true"></i> Reanudar worker';
      hint.textContent = resumeRequested
        ? "Solicitando arranque seguro."
        : status === "stopped"
          ? "Detenido · cola y PDFs conservados."
          : "Sin worker activo · listo para iniciar.";
    }
    function render() {
      if (!current) return;
      const pool = current.pool || {};
      const stats = current.stats || {};
      const run = current.run || {};
      const remaining = pool.remaining_today || 0;
      const quota = pool.daily_quota || 60;
      const used = pool.used_today || 0;
      const usedPct = quota ? Math.min(100, (used / quota) * 100) : 0;
      const remainingPct = Math.max(0, 100 - usedPct);
      const nextSeconds = secondsUntil(run.next_cycle_at);
      const status = document.getElementById("status");
      status.textContent = label(current.status);
      status.className = `status ${current.status || ""}`;
      renderStopButton(current.status);
      document.getElementById("headline").textContent = headline(current, nextSeconds);
      document.getElementById("headlineSub").textContent = subline(current, nextSeconds);
      document.getElementById("capacityNumber").textContent = `${remaining} / ${quota}`;
      document.getElementById("usedBar").style.width = `${usedPct}%`;
      document.getElementById("remainingBar").style.width = `${remainingPct}%`;
      document.getElementById("usedToday").textContent = used;
      document.getElementById("remainingToday").textContent = remaining;
      document.getElementById("downloads").textContent = stats.downloads ?? 0;
      document.getElementById("captchaPending").textContent = pool.captcha_pending_accounts ?? 0;
      const captchaSolver = current.captcha_solver || {};
      document.getElementById("paidCaptcha").textContent = `${captchaSolver.attempts ?? 0}/${captchaSolver.daily_limit ?? 0}`;
      const automaticToggle = document.getElementById("automaticCaptchaToggle");
      automaticToggle.checked = Boolean(captchaSolver.automatic_enabled);
      document.getElementById("automaticCaptchaLabel").textContent = automaticToggle.checked ? "ACTIVADO" : "DESACTIVADO";
      const routeCount = pool.egress_routes ?? (current.accounts || []).length;
      const routeMode = pool.shared_egress ? `${routeCount} salida Chile compartida` : `${routeCount} salidas dedicadas`;
      document.getElementById("accountSummary").textContent = `${(current.accounts || []).length} cuentas autorizadas · ${quota} consultas teóricas por día · ${routeMode}`;
      const jobSummary = current.jobs?.summary || null;
      document.getElementById("queuedJobs").textContent = jobSummary ? (jobSummary.queued ?? 0) : "-";
      const backupLabels = { healthy: "saludable", stale: "atrasado", failed: "fallido", low_disk: "poco espacio", invalid: "inválido", not_configured: "no configurado" };
      const restoreLabels = { verified: "restore probado", stale: "restore vencido", failed: "restore fallido", invalid: "restore inválido", not_verified: "restore pendiente" };
      document.getElementById("backupState").textContent = current.backup
        ? `${backupLabels[current.backup.status] || current.backup.status} · ${restoreLabels[current.backup.restore_status] || "restore pendiente"}`
        : "-";
      const endurance = current.endurance || {};
      document.getElementById("enduranceStatus").textContent = endurance.active_job
        ? `Activo: ${endurance.active_job.status} · ${endurance.active_job.job_id}`
        : endurance.paused ? "Pausado" : endurance.enabled ? "Habilitado, esperando cooldown" : "Deshabilitado; run-once sigue disponible";
      renderAlert(current.alert);
      renderPoolFacts(pool, run, nextSeconds);
      renderAccounts(current.accounts || []);
      renderCycles(current.cycles || [], current.artifacts || []);
      renderJobs(current.jobs?.recent || []);
      renderCaptchaAttempts(current.captcha_attempts || []);
      refreshIcons();
    }
    function headline(data, nextSeconds) {
      const pool = data.pool || {};
      if (!data.run) return "Listo para iniciar el pool";
      if (data.status === "waiting_capacity") return "Pool sin capacidad disponible";
      if (data.status === "waiting" && nextSeconds !== null) return `Próxima consulta en ${fmtSeconds(nextSeconds)}`;
      if (data.status === "running") return `Ejecutando con ${pool.next_account_label || "cuenta disponible"}`;
      return `${pool.remaining_today ?? 0} consultas disponibles hoy`;
    }
    function subline(data, nextSeconds) {
      const pool = data.pool || {};
      if (data.status === "waiting" && nextSeconds !== null) {
        return `Siguiente cuenta: ${pool.next_account_label || "-"} · ${localTime(data.run.next_cycle_at)}`;
      }
      if (data.status === "waiting_capacity") {
        return "El runner queda vivo, pero no hará más tráfico hasta tener cuentas con cupo.";
      }
      return `${pool.used_today || 0} usadas de ${pool.daily_quota || 60} consultas teóricas diarias.`;
    }
    function renderAlert(alert) {
      const box = document.getElementById("alert");
      if (!alert || !alert.active) {
        box.classList.remove("show");
        return;
      }
      box.classList.add("show");
      document.getElementById("alertTitle").textContent = alert.title || "Advertencia";
      document.getElementById("alertMessage").textContent = alert.message || "-";
      maybeNotifyCaptcha(alert);
    }
    function renderPoolFacts(pool, run, nextSeconds) {
      const rows = [
        ["Cupo diario", `${pool.daily_quota || 60}`],
        ["Disponible", `${pool.available_accounts ?? 0} cuenta(s)`],
        ["Rutas de salida", `${pool.egress_routes ?? "-"}${pool.shared_egress ? " (compartida)" : ""}`],
        ["Proveedor proxy", current.proxy_provider?.brand || providerLabel(current.proxy_provider?.provider)],
        ["Salud proveedor", providerHealthLabel(current.proxy_provider)],
        ["Chrome persistente", `${pool.browser_live_count ?? 0}/${pool.browser_expected_count ?? 0} abiertos`],
        ["Acceso protegido", `${pool.browser_authenticated_count ?? 0}/${pool.browser_expected_count ?? 0} formularios verificados`],
        ["Siguiente cuenta", pool.next_account_label || "-"],
        ["Próximo ciclo", nextSeconds === null ? "-" : fmtSeconds(nextSeconds)],
        ["Fecha de cupo", pool.quota_date || "-"],
        ["ID de ejecución", run.run_id || "-"]
      ];
      document.getElementById("poolFacts").innerHTML = rows
        .map(([key, value]) => `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(value)}</td></tr>`)
        .join("");
    }
    function renderAccounts(accounts) {
      document.getElementById("accounts").innerHTML = accounts.map((account, index) => {
        const pct = account.daily_quota ? Math.min(100, (account.used_today / account.daily_quota) * 100) : 0;
        const route = account.egress_shared
          ? `Salida compartida: ${account.egress_group || "Chile"}`
          : `Salida aislada · ${account.proxy_brand || providerLabel(account.proxy_provider)}`;
        const baseline = account.egress_baseline_status
          ? ` · baseline ${account.egress_baseline_status}`
          : "";
        const retrySeconds = secondsUntil(account.resume_at);
        const retryNote = retrySeconds === null ? "" : ` · reintento en ${fmtSeconds(retrySeconds)}`;
        const accountAttempts = (current.jobs?.recent || [])
          .flatMap((job) => job.attempts || [])
          .filter((attempt) => attempt.account_id === account.account_id)
          .sort((left, right) => String(right.finished_at || right.started_at || "")
            .localeCompare(String(left.finished_at || left.started_at || "")));
        const latestAttempt = accountAttempts[0] || null;
        const protectedAccess = !latestAttempt
          ? "Sin intento registrado"
          : latestAttempt.status === "search_completed" || latestAttempt.status === "completed"
            ? `ACEPTADO · ${localTime(latestAttempt.finished_at)}`
            : latestAttempt.reason === "temporary_unavailable"
              ? `BLOQUEADO · temporary_unavailable · ${localTime(latestAttempt.finished_at)}`
              : `${label(latestAttempt.status)} · ${label(latestAttempt.reason)} · ${localTime(latestAttempt.finished_at)}`;
        const note = account.paused_reason
          ? `Motivo: ${account.paused_reason}${retryNote} · ${route}${baseline}`
          : `${account.remaining_today} restantes hoy · ${route}${baseline}`;
        const routeIsHealthy = account.proxy_health_status === "passed"
          && account.egress_baseline_status === "matched";
        const routeState = routeIsHealthy ? "Ruta validada" : "Ruta sin validar";
        const routeStateClass = routeIsHealthy ? "" : " standby";
        const commerceBlocked = account.browser_authenticated
          && account.paused_reason === "temporary_unavailable";
        const authMethod = account.browser_status === "authenticated_login_api"
          ? "Login API aceptado"
          : account.browser_status === "authenticated_login_form"
            ? "Formulario aceptado"
            : account.browser_status === "authenticated_form_visible"
              ? "Formulario protegido visible"
              : "Formulario protegido confirmado";
        const chromeState = account.browser_live ? "ABIERTA" : "CERRADA";
        const authenticationState = account.browser_authenticated
          ? `SÍ · ${authMethod}`
          : account.browser_status === "login_gate_visible"
            ? "NO · Portal solicita iniciar sesión"
          : account.browser_live
            ? "NO CONFIRMADA"
            : "NO · Chrome cerrada";
        const chromeStateLabel = commerceBlocked
          ? "LOGUEADA · BÚSQUEDA BLOQUEADA"
          : account.browser_authenticated
          ? `LOGUEADA · ${authMethod.toUpperCase()}`
          : account.browser_status === "login_gate_visible"
            ? "NO LOGUEADA · LOGIN REQUERIDO"
          : account.browser_live
            ? "LOGIN NO CONFIRMADO"
            : "NO LOGUEADA";
        const chromeStateClass = account.browser_authenticated ? "" : " standby";
        const routeView = `<div class="account-route" aria-label="Ruta proxy configurada de ${escapeHtml(account.username_prefix || account.label || account.account_id)}">
          <div class="account-route-head">
            <span class="account-route-title">Ruta proxy</span>
            <span class="route-live${routeStateClass}">${escapeHtml(routeState)}</span>
          </div>
          <dl class="account-route-grid">
            <div><dt>Marca / servicio</dt><dd>${escapeHtml(account.proxy_brand || providerLabel(account.proxy_provider))}</dd></div>
            <div><dt>IP / host proxy</dt><dd><code>${escapeHtml(account.proxy_endpoint || "No configurado")}</code></dd></div>
            <div><dt>País de salida</dt><dd>${escapeHtml(account.egress_country || "No verificado")}</dd></div>
            <div><dt>Salud / baseline</dt><dd>${escapeHtml(label(account.proxy_health_status))} · ${escapeHtml(account.egress_baseline_status || "-")}</dd></div>
            <div><dt>Puerto sticky</dt><dd>${escapeHtml(account.proxy_sticky_port ?? "-")}</dd></div>
            <div><dt>TTL / generación</dt><dd>${account.proxy_sticky_ttl_minutes ? `${escapeHtml(account.proxy_sticky_ttl_minutes)} min` : "-"} · ${escapeHtml(account.proxy_generation ?? 0)}</dd></div>
            <div><dt>Estado de recuperación</dt><dd>${escapeHtml(label(account.proxy_route_status || "not_initialized"))}</dd></div>
            <div><dt>Última rotación</dt><dd>${escapeHtml(localTime(account.proxy_last_rotated_at))}</dd></div>
            <div><dt>Aislamiento</dt><dd>${account.egress_shared ? "Compartida" : "Exclusiva por cuenta"}</dd></div>
            <div><dt>Último control</dt><dd>${escapeHtml(localTime(account.proxy_checked_at))}</dd></div>
            <div><dt>Chrome</dt><dd><span class="route-live${account.browser_live ? "" : " standby"}">${escapeHtml(chromeState)}</span></dd></div>
            <div><dt>Modo Chrome</dt><dd>${escapeHtml(account.browser_mode || "-")}</dd></div>
            <div><dt>Cuenta autenticada</dt><dd><span class="route-live${chromeStateClass}">${escapeHtml(authenticationState)}</span></dd></div>
            <div><dt>Evidencia DOM</dt><dd><code>${escapeHtml(account.browser_auth_state || "unknown")}</code></dd></div>
            <div><dt>Autenticación verificada</dt><dd>${escapeHtml(localTime(account.browser_checked_at))}</dd></div>
            <div><dt>Última consulta protegida</dt><dd>${escapeHtml(protectedAccess)}</dd></div>
          </dl>
        </div>`;
        const phase = account.captcha_phase || "";
        const phaseLabel = captchaPhaseLabels[phase] || "";
        const browserBadge = commerceBlocked
          ? { label: "LOGUEADA · BÚSQUEDA BLOQUEADA", css: "browser_unconfirmed" }
          : account.browser_authenticated
          ? { label: chromeStateLabel, css: "browser_authenticated" }
          : account.browser_status === "login_gate_visible"
            ? { label: "No logueada · Login requerido", css: "browser_offline" }
          : account.browser_live
            ? { label: "Login no confirmado", css: "browser_unconfirmed" }
            : account.worker_active
              ? { label: "Chrome no iniciado", css: "browser_offline" }
              : { label: "Chrome detenido", css: "browser_offline" };
        const eligibilityLabel = phaseLabel || label(account.status);
        const latestPaidAttempt = (current.captcha_attempts || []).find(
          (attempt) => attempt.account_id === account.account_id
        );
        const paidRetryBlocked = Boolean(
          latestPaidAttempt?.paid_retry_blocked_until
          && new Date(latestPaidAttempt.paid_retry_blocked_until).getTime() > Date.now()
        );
        const paidSolveButton = current.runtime?.captcha_solver_mode === "2captcha_manual"
          ? paidRetryBlocked
            ? `<button class="account-action" disabled title="CBRS rechazó el solve anterior; usa recuperación visual.">Solver en cooldown</button>`
            : `<button class="account-action" data-captcha-account="${escapeHtml(account.account_id)}" data-captcha-action="solve-external">Autorizar 1 solve externo</button>`
          : "";
        const action = account.status === "captcha_pending"
          ? `${paidSolveButton}<button class="account-action" data-captcha-account="${escapeHtml(account.account_id)}" data-captcha-action="trigger">Resolver captcha visualmente</button>`
          : account.status === "captcha_solving" && phase === "waiting_operator"
            ? `<button class="account-action" data-captcha-account="${escapeHtml(account.account_id)}" data-captcha-action="complete">Validar y reactivar</button>`
            : account.status === "captcha_solving"
              ? `<button class="account-action" disabled>${escapeHtml(phaseLabel || "Procesando...")}</button>`
            : "";
        const phaseView = phaseLabel
          ? `<div class="recovery-phase"><span class="recovery-spinner"></span>${escapeHtml(phaseLabel)}</div>`
          : "";
        return `<section class="account ${escapeHtml(account.status)}" style="--wave-index:${index % 9}">
          <div class="account-top">
            <div class="account-name">${escapeHtml(account.username_prefix || account.label || account.account_id)}</div>
            <div class="account-status-stack">
              <div class="status ${browserBadge.css}">${escapeHtml(browserBadge.label)}</div>
              <div class="account-eligibility">Cupo: ${escapeHtml(eligibilityLabel)}</div>
            </div>
          </div>
          <div class="account-count">${account.used_today}/${account.daily_quota}</div>
          <div class="mini-bar"><span style="width:${pct}%"></span></div>
          <div class="muted" style="margin-top:8px">${escapeHtml(note)}</div>
          ${routeView}
          ${phaseView}
          ${action}
        </section>`;
      }).join("");
    }
    async function triggerCaptcha(accountId, action, button) {
      if (!accountId) return;
      if (button) {
        button.disabled = true;
        button.textContent = action === "complete"
          ? "Validando..."
          : action === "solve-external" ? "Autorizando 1 solve..." : "Abriendo Chrome...";
      }
      const response = await fetch(`/api/captcha/${encodeURIComponent(accountId)}/${action}`, { method: "POST" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const errors = {
          RECENT_PORTAL_REJECTION: "CBRS rechazó el solve anterior. El proveedor queda temporalmente bloqueado para evitar gasto repetido; usa la recuperación visual.",
          DAILY_LIMIT: "Se alcanzó el límite diario de solves pagados.",
          CIRCUIT_OPEN: "El solver externo está en cooldown por un fallo temporal.",
        };
        setControlFeedback(errors[result.error] || "No se pudo autorizar el solve pagado.", true);
        await refresh();
        return;
      }
      if (action === "trigger") {
        if (current?.runtime?.visual_url) {
          window.open(current.runtime.visual_url, "cbrsVisualRecovery", "noopener");
        } else {
          setControlFeedback("Chrome se está abriendo localmente para la recuperación visual.");
        }
      }
      if (action === "solve-external") {
        setControlFeedback(result.status === "captcha_validation_queued"
          ? "Validación dirigida iniciada: probará primero un token del navegador y solo recurrirá al proveedor externo si CBRS vuelve a rechazarlo. No descargará ningún PDF."
          : "Se autorizó un solve. El próximo intento usará primero un token del navegador y solo recurrirá al proveedor externo si CBRS vuelve a rechazarlo.");
      }
      await refresh();
    }
    document.getElementById("automaticCaptchaToggle").addEventListener("change", async (event) => {
      const toggle = event.currentTarget;
      const enabled = Boolean(toggle.checked);
      toggle.disabled = true;
      document.getElementById("automaticCaptchaLabel").textContent = enabled ? "ACTIVANDO…" : "DESACTIVANDO…";
      try {
        const response = await fetch("/api/captcha/automatic", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.message || result.error || `HTTP ${response.status}`);
        setControlFeedback(enabled
          ? "Solver externo automático activado. Los rechazos reales usarán el fallback pagado dentro de sus límites y cooldowns."
          : "Solver externo automático desactivado. Los solves pagados vuelven a requerir autorización individual.");
      } catch (error) {
        toggle.checked = !enabled;
        setControlFeedback(`No se pudo cambiar el solver automático: ${error.message}`, true);
      } finally {
        toggle.disabled = false;
        await refresh();
      }
    });
    function maybeNotifyCaptcha(alert) {
      if (!("Notification" in window)) return;
      if (alert.reason !== "captcha_rejected") return;
      const key = `${alert.account_id || alert.account_label || ""}:${alert.reason || ""}`;
      if (!key || key === lastCaptchaNoticeKey) return;
      lastCaptchaNoticeKey = key;
      const send = () => new Notification("CBRS: captcha pendiente", {
        body: `${alert.account_label || "Una cuenta"} necesita resolución manual.`,
      });
      if (Notification.permission === "granted") send();
      else if (Notification.permission !== "denied") {
        Notification.requestPermission().then((permission) => {
          if (permission === "granted") send();
        });
      }
    }
    function renderCycles(cycles, artifacts) {
      const artifactMap = new Map((artifacts || []).map((item) => [item.cycle_id, item]));
      document.getElementById("cycles").innerHTML = (cycles || []).map((cycle) => {
        const artifact = artifactMap.get(cycle.cycle_id);
        const pdf = artifact ? `<a href="${artifact.artifact_url}" target="_blank">PDF</a>` : "-";
        return `<tr>
          <td>${cycle.sequence}</td>
          <td>${escapeHtml(cycle.account_label || cycle.account_id)}</td>
          <td>${escapeHtml(label(cycle.status))}</td>
          <td>${cycle.result_count ?? "-"}</td>
          <td>${pdf}</td>
          <td><code>${escapeHtml(cycle.safety_stop || "-")}</code></td>
          <td>${localTime(cycle.finished_at)}</td>
        </tr>`;
      }).join("");
    }
    function renderJobs(jobs) {
      const panel = document.getElementById("jobsPanel");
      const jobsBody = document.getElementById("jobs");
      const jobsSignature = JSON.stringify({
        jobs,
        accounts: (current.accounts || []).map((account) => ({
          account_id: account.account_id,
          label: account.username_prefix || account.label,
          status: account.status,
          paused_reason: account.paused_reason,
        })),
      });
      if (jobsSignature === renderedJobsSignature) return;
      renderedJobsSignature = jobsSignature;
      if (!jobs.length && !current.jobs) {
        panel.style.display = "none";
        return;
      }
      panel.style.display = "block";
      const accountLabels = new Map((current.accounts || []).map((account) => [
        account.account_id,
        account.username_prefix || account.label,
      ]));
      const accountBadge = (accountId) => {
        const name = accountId ? (accountLabels.get(accountId) || accountId) : "";
        if (!name) return "-";
        const initial = Array.from(name)[0].toUpperCase();
        return `<span class="job-account"><span class="job-account-icon" style="--avatar-hue:${avatarHue(name)}" aria-hidden="true">${escapeHtml(initial)}</span><span>${escapeHtml(name)}</span></span>`;
      };
      const santiagoDay = new Intl.DateTimeFormat("en-CA", {
        timeZone: "America/Santiago",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      });
      const documentOrdinals = new Map();
      const documentTotals = new Map();
      [...jobs]
        .filter((job) => job.finished_at && job.account_id && (job.completed_items || 0) > 0)
        .sort((left, right) => new Date(left.finished_at) - new Date(right.finished_at))
        .forEach((job) => {
          const day = santiagoDay.format(new Date(job.finished_at));
          const key = `${job.account_id}:${day}`;
          const start = (documentTotals.get(key) || 0) + 1;
          const end = start + Number(job.completed_items || 0) - 1;
          documentTotals.set(key, end);
          documentOrdinals.set(job.job_id, { start, end, day });
        });
      jobsBody.innerHTML = jobs.map((job) => {
        const terminal = ["completed", "partial", "failed", "cancelled"].includes(job.status);
        const preview = (job.completed_items || 0) > 0
          ? `<button class="preview-pdf" data-preview-job="${escapeHtml(job.job_id)}">Ver PDF</button>`
          : "";
        const action = terminal ? (preview || "-") : `${preview}<button data-cancel-job="${escapeHtml(job.job_id)}" style="padding:5px 8px;margin-left:${preview ? "5px" : "0"}">Cancelar</button>`;
        const attempts = job.attempts || [];
        const accountId = job.account_id || job.current_account_id;
        const accountName = accountId ? (accountLabels.get(accountId) || accountId) : "";
        const account = attempts.length > 1 && !job.completed_items
          ? `<span class="attempt-count">${attempts.length} intentos</span>`
          : accountBadge(accountId);
        const documentOrdinal = documentOrdinals.get(job.job_id);
        const documentNumber = documentOrdinal
          ? `<span class="document-ordinal" title="Documento exitoso de ${escapeHtml(accountName)} para ${escapeHtml(documentOrdinal.day)}">#${documentOrdinal.start}${documentOrdinal.end > documentOrdinal.start ? `–#${documentOrdinal.end}` : ""}</span>`
          : "-";
        const describedAccounts = new Set();
        const detailEntries = [];
        attempts.forEach((attempt, index) => {
          describedAccounts.add(attempt.account_id);
          detailEntries.push({
            number: `#${index + 1}`,
            accountId: attempt.account_id,
            reason: attempt.reason,
            attempted: true,
          });
        });
        if (["waiting_capacity", "waiting_captcha"].includes(job.status)) {
          for (const poolAccount of current.accounts || []) {
            if (describedAccounts.has(poolAccount.account_id) || poolAccount.status === "available") continue;
            detailEntries.push({
              number: "—",
              accountId: poolAccount.account_id,
              reason: poolAccount.paused_reason || poolAccount.status,
              attempted: false,
            });
            describedAccounts.add(poolAccount.account_id);
          }
        }
        let reason = "-";
        const failedEntries = detailEntries.filter((entry) => !["completed", "search_completed"].includes(entry.reason));
        if (failedEntries.length) {
          const unavailableCount = failedEntries.filter((entry) => !entry.attempted).length;
          const summaryParts = [`${attempts.length} intento${attempts.length === 1 ? "" : "s"}`];
          if (unavailableCount) summaryParts.push(`${unavailableCount} no elegible${unavailableCount === 1 ? "" : "s"}`);
          const entriesByAccount = new Map();
          failedEntries.forEach((entry) => {
            if (!entriesByAccount.has(entry.accountId)) entriesByAccount.set(entry.accountId, []);
            entriesByAccount.get(entry.accountId).push(entry);
          });
          summaryParts.push(`${entriesByAccount.size} cuenta${entriesByAccount.size === 1 ? "" : "s"}`);
          const accountGroups = Array.from(entriesByAccount.entries()).map(([entryAccountId, entries]) => {
            const latest = entries.at(-1);
            const latestRawReason = latest.reason || "failed";
            const latestReason = stopReasonLabels[latestRawReason] || label(latestRawReason);
            const attemptedEntries = entries.filter((entry) => entry.attempted);
            const countLabel = attemptedEntries.length
              ? `${attemptedEntries.length} intento${attemptedEntries.length === 1 ? "" : "s"}`
              : "No elegible";
            const groupKey = `${job.job_id}:${entryAccountId}`;
            const groupControlId = `attempt-account-${groupKey.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
            const historyRows = [...entries].reverse().map((entry, index) => {
              const rawReason = entry.reason || "failed";
              const reasonText = stopReasonLabels[rawReason] || label(rawReason);
              const actionText = entry.attempted ? "Intentado" : "No intentado";
              return `<span class="attempt-history-row${index === 0 ? " latest" : ""}"><span class="attempt-number">${escapeHtml(entry.number)}</span><span class="attempt-outcome"><strong>${actionText}:</strong> ${escapeHtml(reasonText)}</span></span>`;
            }).join("");
            return `<span class="attempt-account-group" data-attempt-account="${escapeHtml(groupKey)}"><input class="attempt-disclosure-input" id="${escapeHtml(groupControlId)}" type="checkbox"><label class="attempt-account-summary" for="${escapeHtml(groupControlId)}">${accountBadge(entryAccountId)}<span class="attempt-latest"><strong>Último ${escapeHtml(latest.number)}:</strong> ${escapeHtml(latestReason)}</span><span class="attempt-account-count">${escapeHtml(countLabel)}</span></label><span class="attempt-history">${historyRows}</span></span>`;
          }).join("");
          const jobControlId = `attempt-job-${String(job.job_id).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
          reason = `<span class="job-reason"><span class="attempt-details" data-attempt-job="${escapeHtml(job.job_id)}"><input class="attempt-disclosure-input" id="${escapeHtml(jobControlId)}" type="checkbox"><label class="attempt-summary" for="${escapeHtml(jobControlId)}">${escapeHtml(summaryParts.join(" · "))}</label><span class="attempt-account-groups">${accountGroups}</span></span></span>`;
        } else if (job.status === "waiting_capacity") {
          reason = `<span class="job-reason">No se envió: no había cuentas elegibles.</span>`;
        } else if (job.status === "waiting_captcha") {
          reason = `<span class="job-reason">No se envió: todas las cuentas requerían captcha.</span>`;
        } else if (job.error_code) {
          reason = `<span class="job-reason">${escapeHtml(stopReasonLabels[job.error_code] || label(job.error_code))}</span>`;
        }
        return `<tr>
          <td><a class="job-id" href="/api/jobs/${encodeURIComponent(job.job_id)}" target="_blank" title="${escapeHtml(job.job_id)}" aria-label="Abrir ${escapeHtml(job.job_id)}"><code>${escapeHtml(shortJobId(job.job_id))}</code></a></td>
          <td>${escapeHtml(job.kind)}${job.source === "endurance" ? " · endurance" : ""}</td>
          <td>${account}</td>
          <td>${documentNumber}</td>
          <td><span class="status ${escapeHtml(job.status)}">${escapeHtml(label(job.status))}</span></td>
          <td>${reason}</td>
          <td>${job.result_count ?? "-"}</td>
          <td>${job.completed_items ?? 0}</td>
          <td>${localTime(job.finished_at)}</td>
          <td>${action}</td>
        </tr>`;
      }).join("");
    }
    function renderCaptchaAttempts(attempts) {
      const body = document.getElementById("captchaAttempts");
      const accountLabels = new Map((current.accounts || []).map((account) => [
        account.account_id,
        account.username_prefix || account.label || account.account_id,
      ]));
      const solverLabels = {
        reserved: "en espera",
        succeeded: "token resuelto",
        failed: "falló",
        armed: "autorizado",
        consumed: "autorización usada",
        not_required: "no fue necesario",
        cancelled: "cancelado",
        expired: "expiró",
        replaced: "reemplazado",
      };
      const portalLabels = { accepted: "aceptado", rejected: "rechazado", indeterminate: "sin decisión", not_submitted: "no enviado", not_required: "no requerido" };
      const captchaProvider = (value) => {
        const normalized = String(value || "external").trim().toLowerCase();
        if (normalized.includes("capsolver")) return { label: "CapSolver", css: "capsolver" };
        if (normalized.includes("2captcha")) return { label: "2Captcha", css: "two-captcha" };
        return { label: normalized === "external" ? "Sin proveedor" : value, css: "external" };
      };
      if (!attempts.length) {
        body.innerHTML = '<tr><td colspan="9" class="muted">Aún no hay intentos pagados del solver externo.</td></tr>';
        return;
      }
      body.innerHTML = attempts.map((attempt) => {
        const provider = captchaProvider(attempt.provider);
        return `<tr>
        <td>${localTime(attempt.started_at)}</td>
        <td>${escapeHtml(accountLabels.get(attempt.account_id) || attempt.account_id || "-")}</td>
        <td><code class="captcha-action" tabindex="0" title="${escapeHtml(attempt.action || "-")}" aria-label="Acción completa: ${escapeHtml(attempt.action || "-")}">${escapeHtml(attempt.action || "-")}</code></td>
        <td><span class="captcha-provider ${provider.css}" title="Servicio usado para generar este intento">${escapeHtml(provider.label)}</span></td>
        <td><span class="status ${escapeHtml(attempt.status || "")}">${escapeHtml(solverLabels[attempt.status] || attempt.status || "-")}</span></td>
        <td>${attempt.cost_usd == null ? "-" : `$${Number(attempt.cost_usd).toFixed(5)}`}</td>
        <td>${attempt.latency_seconds == null ? "-" : `${Number(attempt.latency_seconds).toFixed(1)} s`}</td>
        <td><span class="status ${attempt.portal_status === "accepted" ? "completed" : attempt.portal_status === "rejected" ? "failed" : ""}">${escapeHtml(portalLabels[attempt.portal_status] || "pendiente de confirmación")}</span></td>
        <td><code>${escapeHtml(attempt.portal_error_code || attempt.error_code || "sin error")}</code>${attempt.paid_retry_blocked_until ? `<br><small class="muted">Nuevo solve bloqueado hasta ${escapeHtml(localTime(attempt.paid_retry_blocked_until))}</small>` : ""}</td>
      </tr>`;
      }).join("");
    }
    document.getElementById("stopButton").addEventListener("click", async () => {
      const button = document.getElementById("stopButton");
      if (button.disabled) return;
      const active = ["running", "waiting", "waiting_capacity", "waiting_captcha"].includes(current?.status);
      if (active) stopRequested = true;
      else resumeRequested = true;
      renderStopButton(current?.status || "");
      try {
        const endpoint = active ? "/api/stop" : "/api/resume";
        const response = await fetch(endpoint, { method: "POST" });
        if (!response.ok) throw new Error(`Control request failed: ${response.status}`);
      } finally {
        await refresh();
        if (resumeRequested) {
          window.setTimeout(() => refresh().catch(console.error), 1200);
        }
      }
    });
    function setControlFeedback(message, isError = false) {
      const feedback = document.getElementById("controlFeedback");
      feedback.textContent = message;
      feedback.style.color = isError ? "var(--bad)" : "var(--muted)";
    }
    const configModal = document.getElementById("configModal");
    const accountEditorList = document.getElementById("accountEditorList");
    let addedAccountSequence = 0;
    function accountEditor(account = {}) {
      const existing = Boolean(account.account_id);
      const accountId = account.account_id || `cuenta_${Date.now()}_${++addedAccountSequence}`;
      const displayName = account.username_prefix || account.label || "Nueva cuenta";
      const quota = account.daily_quota || 20;
      return `<section class="account-editor" data-account-id="${escapeHtml(accountId)}" data-existing="${existing ? "true" : "false"}">
        <div class="account-editor-top"><span class="account-editor-title">${escapeHtml(displayName)}</span><button class="remove-account" type="button">Eliminar</button></div>
        <div class="account-editor-grid">
          <label>Correo de acceso<input data-field="username" type="email" autocomplete="username" placeholder="${existing ? "Se conserva si está vacío" : "nombre@empresa.cl"}" /></label>
          <label>Contraseña<div class="password-input"><input data-field="password" type="password" autocomplete="new-password" placeholder="${existing ? "Se conserva si está vacía" : "Contraseña"}" /><button class="password-toggle" type="button">Ver</button></div></label>
          <label>Proxy dedicado<input data-field="proxy_url" type="text" autocomplete="off" placeholder="${existing ? "Se conserva si está vacío" : "http://usuario:clave@host:puerto"}" /></label>
          <label>Proveedor<select data-field="proxy_provider">
            <option value="generic_static" ${(account.proxy_provider || "generic_static") === "generic_static" ? "selected" : ""}>Estático genérico</option>
            <option value="2captcha_dedicated_isp" ${account.proxy_provider === "2captcha_dedicated_isp" ? "selected" : ""}>2Captcha ISP dedicado</option>
            <option value="2captcha_residential_sticky" ${account.proxy_provider === "2captcha_residential_sticky" ? "selected" : ""}>2Captcha residencial · 120 min</option>
            <option value="dataimpulse_residential_sticky" ${account.proxy_provider === "dataimpulse_residential_sticky" ? "selected" : ""}>DataImpulse residencial sticky</option>
          </select></label>
          <label>Puerto sticky DataImpulse<input data-field="dataimpulse_port" type="number" min="10000" max="20000" value="${escapeHtml(account.proxy_sticky_port || account.dataimpulse_port || "")}" placeholder="10000" /></label>
          <label>Marca del proxy<input data-field="proxy_brand" type="text" maxlength="80" value="${escapeHtml(account.proxy_brand || "")}" placeholder="Ej.: Proxy-Cheap" /></label>
          <label>Cupo diario<input data-field="daily_quota" type="number" min="1" max="10000" value="${escapeHtml(quota)}" /></label>
        </div>
      </section>`;
    }
    function setConfigFeedback(message, isError = false) {
      const feedback = document.getElementById("configModalFeedback");
      feedback.textContent = message;
      feedback.style.color = isError ? "var(--bad)" : "var(--muted)";
    }
    document.getElementById("configureAccounts").addEventListener("click", () => {
      if (["running", "waiting", "waiting_capacity", "waiting_captcha"].includes(current?.status)) {
        setControlFeedback("Detén el worker antes de modificar cuentas.", true);
        return;
      }
      accountEditorList.innerHTML = (current?.accounts || []).map((account) => accountEditor(account)).join("");
      if (!accountEditorList.children.length) accountEditorList.innerHTML = accountEditor();
      setConfigFeedback("");
      configModal.showModal();
    });
    document.getElementById("closeConfigModal").addEventListener("click", () => configModal.close());
    document.getElementById("addAccount").addEventListener("click", () => {
      accountEditorList.insertAdjacentHTML("beforeend", accountEditor());
      accountEditorList.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    accountEditorList.addEventListener("click", (event) => {
      const toggle = event.target.closest(".password-toggle");
      if (toggle) {
        const input = toggle.parentElement.querySelector("input");
        const visible = input.type === "text";
        input.type = visible ? "password" : "text";
        toggle.textContent = visible ? "Ver" : "Ocultar";
        return;
      }
      const remove = event.target.closest(".remove-account");
      if (!remove) return;
      if (accountEditorList.children.length === 1) {
        setConfigFeedback("Debe existir al menos una cuenta.", true);
        return;
      }
      remove.closest(".account-editor").remove();
    });
    document.getElementById("saveAccounts").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const accounts = Array.from(accountEditorList.querySelectorAll(".account-editor")).map((row) => {
        const field = (name) => row.querySelector(`[data-field="${name}"]`).value.trim();
        return {
          id: row.dataset.accountId,
          username: field("username"),
          password: field("password"),
          proxy_url: field("proxy_url"),
          proxy_provider: field("proxy_provider"),
          dataimpulse_port: field("dataimpulse_port") ? Number(field("dataimpulse_port")) : null,
          proxy_brand: field("proxy_brand"),
          daily_quota: Number(field("daily_quota")),
          existing: row.dataset.existing === "true",
        };
      });
      const incompleteNewAccount = accounts.some((account) => !account.existing && (
        !account.username || !account.password || (
          account.proxy_provider === "dataimpulse_residential_sticky"
            ? !account.dataimpulse_port
            : !account.proxy_url
        )
      ));
      if (incompleteNewAccount) {
        setConfigFeedback("Las cuentas nuevas requieren correo, contraseña y proxy dedicado.", true);
        return;
      }
      if (accounts.some((account) => !Number.isInteger(account.daily_quota) || account.daily_quota < 1 || account.daily_quota > 10000)) {
        setConfigFeedback("Cada cupo diario debe ser un número entre 1 y 10000.", true);
        return;
      }
      button.disabled = true;
      setConfigFeedback("Guardando configuración local protegida…");
      try {
        const response = await fetch("/api/onboarding/accounts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ accounts: accounts.map(({ existing, ...account }) => account) }),
        });
        if (!response.ok) throw new Error(`account configuration failed: ${response.status}`);
        setConfigFeedback("Guardado. El dashboard se actualizará en unos segundos; el worker sigue detenido.");
        setControlFeedback("Configuración de cuentas actualizada. Reanuda el worker cuando estés listo.");
        window.setTimeout(() => window.location.reload(), 1800);
      } catch (error) {
        setConfigFeedback("No se pudo guardar. Verifica los campos y que el worker esté detenido.", true);
      } finally {
        button.disabled = false;
      }
    });
    const productionSettingsModal = document.getElementById("productionSettingsModal");
    const productionSettingsForm = document.getElementById("productionSettingsForm");
    const saveProductionSettings = document.getElementById("saveProductionSettings");
    const settingHumanLike = document.getElementById("settingHumanLike");
    function setProductionSettingsFeedback(message, isError = false) {
      const feedback = document.getElementById("productionSettingsFeedback");
      feedback.textContent = message;
      feedback.style.color = isError ? "var(--bad)" : "var(--muted)";
    }
    function setJitterFieldsEnabled() {
      document.getElementById("settingJitterMin").disabled = !settingHumanLike.checked;
      document.getElementById("settingJitterMax").disabled = !settingHumanLike.checked;
    }
    function fillProductionSettings(settings) {
      const pool = settings.pool || {};
      const endurance = settings.endurance || {};
      const runtime = settings.runtime || {};
      settingHumanLike.checked = Boolean(pool.human_like_behavior_enabled);
      document.getElementById("settingJitterMin").value = pool.job_interval_min_seconds ?? 0;
      document.getElementById("settingJitterMax").value = pool.job_interval_max_seconds ?? 0;
      document.getElementById("settingQueueMax").value = pool.max_queued_production_jobs ?? 100;
      document.getElementById("settingWorkerPoll").value = pool.worker_poll_seconds ?? 5;
      document.getElementById("settingInstantJobs").checked = Boolean(pool.instant_jobs_enabled);
      document.getElementById("settingEnduranceEnabled").checked = Boolean(endurance.enabled);
      document.getElementById("settingCooldown").value = endurance.cooldown_seconds ?? 300;
      document.getElementById("settingEnduranceQuota").value = endurance.jobs_per_account_per_day ?? 20;
      document.getElementById("settingProductionReserve").value = endurance.production_reserve_per_account ?? 0;
      document.getElementById("settingDailyQuota").value = pool.daily_quota_per_account ?? 20;
      document.getElementById("runtimeSettingsSummary").textContent =
        `Demora segura por petición: ${runtime.request_delay_seconds ?? "-"}s · ` +
        `revalidación proxy: ${runtime.proxy_recheck_seconds ?? "-"}s · ` +
        `Solver: ${runtime.captcha_solver_provider || "-"} (${runtime.captcha_solver_mode || "-"}), límite ${runtime.two_captcha_daily_limit ?? "-"}/día · ` +
        `egreso esperado: ${runtime.expected_egress_country || "-"}.`;
      setJitterFieldsEnabled();
    }
    settingHumanLike.addEventListener("change", setJitterFieldsEnabled);
    document.getElementById("configureProduction").addEventListener("click", async () => {
      setProductionSettingsFeedback("Cargando configuración…");
      saveProductionSettings.disabled = true;
      productionSettingsModal.showModal();
      refreshIcons();
      try {
        const response = await fetch("/api/settings", { cache: "no-store" });
        if (!response.ok) throw new Error(`settings request failed: ${response.status}`);
        fillProductionSettings(await response.json());
        const workerActive = ["running", "waiting", "waiting_capacity", "waiting_captcha"].includes(current?.status);
        saveProductionSettings.disabled = workerActive;
        setProductionSettingsFeedback(
          workerActive ? "Detén el worker para editar y guardar estos valores." : "Listo para editar."
        );
      } catch (error) {
        setProductionSettingsFeedback("No se pudo cargar la configuración de producción.", true);
      }
    });
    document.getElementById("closeProductionSettings").addEventListener("click", () => productionSettingsModal.close());
    productionSettingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const number = (id) => Number(document.getElementById(id).value);
      const payload = {
        pool: {
          daily_quota_per_account: number("settingDailyQuota"),
          human_like_behavior_enabled: settingHumanLike.checked,
          job_interval_min_seconds: number("settingJitterMin"),
          job_interval_max_seconds: number("settingJitterMax"),
          worker_poll_seconds: number("settingWorkerPoll"),
          max_queued_production_jobs: number("settingQueueMax"),
          instant_jobs_enabled: document.getElementById("settingInstantJobs").checked,
        },
        endurance: {
          enabled: document.getElementById("settingEnduranceEnabled").checked,
          cooldown_seconds: number("settingCooldown"),
          jobs_per_account_per_day: number("settingEnduranceQuota"),
          production_reserve_per_account: number("settingProductionReserve"),
        },
      };
      if (payload.pool.job_interval_max_seconds < payload.pool.job_interval_min_seconds) {
        setProductionSettingsFeedback("El jitter máximo no puede ser menor que el mínimo.", true);
        return;
      }
      if (payload.endurance.jobs_per_account_per_day + payload.endurance.production_reserve_per_account > payload.pool.daily_quota_per_account) {
        setProductionSettingsFeedback("La asignación endurance más la reserva de producción supera el cupo diario.", true);
        return;
      }
      saveProductionSettings.disabled = true;
      setProductionSettingsFeedback("Validando y guardando…");
      try {
        const response = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.message || result.error || `HTTP ${response.status}`);
        fillProductionSettings(result.settings);
        setProductionSettingsFeedback("Configuración guardada. Se aplicará en el siguiente arranque del worker.");
        await refresh();
      } catch (error) {
        setProductionSettingsFeedback(`No se pudo guardar: ${error.message}`, true);
      } finally {
        saveProductionSettings.disabled = false;
      }
    });
    let requestType = "text";
    document.querySelectorAll("[data-request-type]").forEach((button) => {
      button.addEventListener("click", () => {
        requestType = button.dataset.requestType;
        document.querySelectorAll("[data-request-type]").forEach((tab) => tab.classList.toggle("active", tab === button));
        document.getElementById("textRequestFields").hidden = requestType !== "text";
        document.getElementById("fnaRequestFields").hidden = requestType !== "fna";
      });
    });
    const examplesModal = document.getElementById("examplesModal");
    const exampleList = document.getElementById("exampleList");
    document.getElementById("openExamples").addEventListener("click", async () => {
      exampleList.innerHTML = '<span class="muted">Cargando ejemplos comprobados…</span>';
      examplesModal.showModal();
      try {
        const response = await fetch("/api/examples");
        if (!response.ok) throw new Error(`examples request failed: ${response.status}`);
        const { examples } = await response.json();
        if (!examples.length) {
          exampleList.innerHTML = '<span class="muted">Todavía no hay búsquedas por documento completadas para usar como ejemplo.</span>';
          return;
        }
        exampleList.innerHTML = examples.map((example) => `<button class="example-choice" type="button" data-example-foja="${example.foja}" data-example-numero="${example.numero}" data-example-year="${example.year}"><span>Foja ${example.foja} · Número ${example.numero} · Año ${example.year}<br><small>${example.success_count} descarga${example.success_count === 1 ? "" : "s"} correcta${example.success_count === 1 ? "" : "s"}</small></span><small class="icon-label">Usar ejemplo <i data-lucide="arrow-right" aria-hidden="true"></i></small></button>`).join("");
        refreshIcons();
      } catch (error) {
        exampleList.innerHTML = '<span class="muted">No se pudieron cargar los ejemplos comprobados.</span>';
      }
    });
    document.getElementById("closeExamplesModal").addEventListener("click", () => examplesModal.close());
    exampleList.addEventListener("click", (event) => {
      const choice = event.target.closest("[data-example-foja]");
      if (!choice) return;
      requestType = "fna";
      document.querySelectorAll("[data-request-type]").forEach((tab) => tab.classList.toggle("active", tab.dataset.requestType === "fna"));
      document.getElementById("textRequestFields").hidden = true;
      document.getElementById("fnaRequestFields").hidden = false;
      document.getElementById("documentFoja").value = choice.dataset.exampleFoja;
      document.getElementById("documentNumero").value = choice.dataset.exampleNumero;
      document.getElementById("documentYear").value = choice.dataset.exampleYear;
      examplesModal.close();
      document.getElementById("documentFoja").focus();
      setControlFeedback("Ejemplo comprobado cargado. Elige agregarlo a la cola o descargarlo ahora.");
    });
    document.getElementById("requestComposer").addEventListener("submit", async (event) => {
      event.preventDefault();
      const action = event.submitter?.dataset.requestAction || "queue";
      const input = document.getElementById("jobText");
      let payload;
      if (requestType === "text") {
        const text = input.value.trim();
        if (!text) {
          setControlFeedback("Indica una razón social autorizada.", true);
          return;
        }
        payload = { kind: "text", text };
      } else {
        const foja = Number(document.getElementById("documentFoja").value);
        const numero = Number(document.getElementById("documentNumero").value);
        const year = Number(document.getElementById("documentYear").value);
        if (![foja, numero, year].every(Number.isInteger) || foja < 1 || numero < 1 || year < 1800 || year > 2200) {
          setControlFeedback("Indica foja, número y año válidos.", true);
          return;
        }
        payload = { kind: "fna", foja, numero, year };
      }
      const submit = event.submitter;
      submit.disabled = true;
      const instant = action === "instant";
      setControlFeedback(instant ? "Priorizando la descarga y preparando el worker…" : "Agregando solicitud a la cola…");
      try {
        const response = await fetch(instant ? "/api/jobs/instant" : "/api/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error(`job request failed: ${response.status}`);
        const created = await response.json();
        event.currentTarget.reset();
        const workerMessage = instant
          ? (created.worker_requested ? " El worker se está iniciando." : " El worker ya estaba activo.")
          : "";
        setControlFeedback(`Solicitud ${shortJobId(created.job_id)} ${instant ? "priorizada" : "agregada a la cola"}.${workerMessage}`);
        await refresh();
        document.getElementById("jobsPanel").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        setControlFeedback(instant ? "No se pudo iniciar la descarga. Revisa las cuentas, cupo y estado de seguridad." : "No se pudo agregar la solicitud. Revisa que el dashboard de jobs esté activo.", true);
      } finally {
        submit.disabled = false;
      }
    });
    document.getElementById("accounts").addEventListener("click", (event) => {
      const button = event.target.closest("[data-captcha-account]");
      if (!button) return;
      triggerCaptcha(
        button.dataset.captchaAccount,
        button.dataset.captchaAction || "trigger",
        button,
      ).catch(console.error);
    });
    const pdfPreviewModal = document.getElementById("pdfPreviewModal");
    const pdfPreviewFrame = document.getElementById("pdfPreviewFrame");
    const pdfPreviewFiles = document.getElementById("pdfPreviewFiles");
    function showPdfArtifact(artifact, index) {
      pdfPreviewFrame.src = artifact.artifact_url;
      pdfPreviewFiles.querySelectorAll("[data-preview-artifact]").forEach((button) => {
        button.classList.toggle("active", Number(button.dataset.previewArtifact) === index);
      });
    }
    document.getElementById("closePdfPreview").addEventListener("click", () => {
      pdfPreviewFrame.removeAttribute("src");
      pdfPreviewModal.close();
    });
    pdfPreviewFiles.addEventListener("click", (event) => {
      const button = event.target.closest("[data-preview-artifact]");
      if (!button || !pdfPreviewModal._artifacts) return;
      showPdfArtifact(pdfPreviewModal._artifacts[Number(button.dataset.previewArtifact)], Number(button.dataset.previewArtifact));
    });
    document.getElementById("jobs").addEventListener("click", async (event) => {
      const preview = event.target.closest("[data-preview-job]");
      if (preview) {
        preview.disabled = true;
        try {
          const response = await fetch(`/api/jobs/${encodeURIComponent(preview.dataset.previewJob)}/artifacts`);
          if (!response.ok) throw new Error(`artifact request failed: ${response.status}`);
          const { artifacts } = await response.json();
          if (!artifacts.length) throw new Error("no artifacts");
          pdfPreviewModal._artifacts = artifacts;
          document.getElementById("pdfPreviewTitle").textContent = artifacts.length === 1 ? "Vista previa del PDF" : `Vista previa · ${artifacts.length} PDFs`;
          pdfPreviewFiles.innerHTML = artifacts.map((artifact, index) => `<button class="pdf-preview-file" data-preview-artifact="${index}" type="button">PDF ${index + 1}</button>`).join("");
          pdfPreviewModal.showModal();
          showPdfArtifact(artifacts[0], 0);
        } catch (error) {
          setControlFeedback("No se pudo abrir el PDF generado.", true);
        } finally {
          preview.disabled = false;
        }
        return;
      }
      const button = event.target.closest("[data-cancel-job]");
      if (!button) return;
      button.disabled = true;
      await fetch(`/api/jobs/${encodeURIComponent(button.dataset.cancelJob)}/cancel`, { method: "POST" });
      await refresh();
    });
    document.querySelectorAll("[data-endurance-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await fetch(`/api/endurance/${button.dataset.enduranceAction}`, { method: "POST" });
          await refresh();
        } finally {
          button.disabled = false;
        }
      });
    });
    refreshIcons();
    refresh().catch(console.error);
    setInterval(() => refresh().catch(console.error), 2000);
  </script>
</body>
</html>"""
