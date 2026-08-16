from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, urlparse

from .account_pool import (
    AccountPoolStore,
    PoolConfig,
    account_credentials,
    dashboard_status,
    local_today,
    resolve_account_captcha,
)
from .config import SETTINGS, Settings
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
                if job_store is not None:
                    from .backup import backup_health

                    payload = _with_job_pool_usage(payload, job_store, config)
                    payload["jobs"] = {
                        "summary": job_store.summary(),
                        "recent": _with_job_artifact_urls(job_store.list_jobs(limit=100)),
                    }
                    payload["backup"] = backup_health(settings)
                payload = _with_account_username_prefixes(payload, config)
                payload = _with_captcha_phases(payload, captcha_phases)
                self._send_json(_with_artifact_urls(payload))
                return
            if job_store is not None and parsed.path == "/api/jobs":
                self._send_json(
                    {"jobs": _with_job_artifact_urls(job_store.list_jobs(limit=_limit(parsed.query)))}
                )
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

            def run_recovery() -> None:
                try:
                    if visual_confirmation_required:
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
        ) -> None:
            encoded = json.dumps(redact(payload), ensure_ascii=False, indent=2).encode("utf-8")
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
    """Signal the fixed root-owned systemd path unit to start the worker.

    The local dashboard can create only this one request file; the path unit
    owns the privileged service start and accepts no command or user input.
    """
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
        for field in ("username", "password", "proxy_url", "label", "egress_group"):
            value = str(raw.get(field) or "")
            if len(value) > 1000 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"El campo {field} de la cuenta {index} es inválido.")
            item[field] = value
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


def _limit(query: str) -> int:
    raw = parse_qs(query).get("limit", ["100"])[0]
    try:
        return max(1, min(int(raw), 1000))
    except ValueError:
        return 100


def _runtime_summary(settings: Settings) -> dict[str, Any]:
    visual_url = os.environ.get(
        "CBRS_NOVNC_URL",
        "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale",
    )
    # Keep the recovery endpoint loopback-only while avoiding the general IP
    # redactor turning 127.0.0.1 into an unusable browser URL.
    visual_url = visual_url.replace("://127.0.0.1", "://localhost", 1)
    return {
        "browser_backend": settings.browser_backend,
        "browser_headless": settings.headless,
        "browser_window_mode": settings.window_mode,
        "expected_egress_country": settings.expected_egress_country,
        "request_delay_seconds": settings.request_delay_seconds,
        "visual_url": visual_url,
    }


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pool de Consultas CBRS</title>
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
    .request-type-tabs { display: flex; gap: 7px; margin-top: 12px; }
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
    .account-count { font-size: 28px; font-weight: 900; margin: 8px 0; }
    .mini-bar { height: 10px; border-radius: 999px; overflow: hidden; background: #e8eef5; }
    .mini-bar span { display: block; height: 100%; background: var(--accent); }
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
    .table-scroll { width: 100%; max-width: 100%; overflow: visible; }
    .jobs-table th, .jobs-table td { padding: 8px 6px; }
    .jobs-table thead th {
      position: sticky;
      top: 64px;
      z-index: 4;
      background: var(--panel);
      box-shadow: inset 0 -1px 0 var(--line), 0 5px 10px rgba(31, 41, 55, .06);
    }
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
    .job-reason { min-width: 210px; max-width: 360px; color: var(--muted); font-size: 11px; line-height: 1.45; }
    .attempt-count { color: var(--accent); font-size: 11px; font-weight: 850; white-space: nowrap; }
    .attempt-details summary { cursor: pointer; color: var(--accent); font-weight: 800; user-select: none; }
    .attempt-list { display: grid; gap: 5px; margin-top: 7px; }
    .attempt-tuple {
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr);
      grid-template-areas:
        "number account"
        ". outcome";
      align-items: center;
      column-gap: 6px;
      row-gap: 3px;
      padding: 5px 6px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f8fafc;
    }
    .attempt-number { grid-area: number; align-self: start; padding-top: 2px; color: var(--muted); font-weight: 850; }
    .attempt-account { grid-area: account; min-width: 0; }
    .attempt-outcome { grid-area: outcome; min-width: 0; color: var(--ink); overflow-wrap: anywhere; }
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
      .jobs-table thead th { position: static; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Pool de Consultas CBRS</h1>
      <div id="accountSummary" class="muted">Cuentas autorizadas · capacidad controlada</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <button id="stopButton" class="worker-action" disabled>Comprobando…</button>
      <div>
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
          <button id="configureAccounts" class="onboarding-action" type="button">⚙ Configurar cuentas</button>
        </div>
        <div id="controlFeedback" class="control-feedback" role="status"></div>
      </div>
      <div>
        <h2>Crear solicitud</h2>
        <p class="muted">Elige cómo localizar la inscripción y qué hacer con ella.</p>
        <div class="request-type-tabs" role="tablist" aria-label="Tipo de búsqueda">
          <button class="request-type-tab active" data-request-type="text" type="button">Por empresa</button>
          <button class="request-type-tab" data-request-type="fna" type="button">Por documento</button>
          <button id="openExamples" class="example-trigger" type="button" title="Ejemplos ya descargados correctamente">Ej.</button>
        </div>
        <form id="requestComposer" class="request-composer">
          <div id="textRequestFields"><input id="jobText" type="text" maxlength="500" autocomplete="off" placeholder="Razón social autorizada" aria-label="Razón social autorizada" /></div>
          <div id="fnaRequestFields" class="request-document-fields" hidden>
            <input id="documentFoja" type="number" min="1" placeholder="Foja" aria-label="Foja" />
            <input id="documentNumero" type="number" min="1" placeholder="Número" aria-label="Número" />
            <input id="documentYear" type="number" min="1800" max="2200" placeholder="Año" aria-label="Año" />
          </div>
          <div class="request-actions">
            <button class="onboarding-action" data-request-action="queue" type="submit">＋ Agregar a cola</button>
            <button class="instant-action" data-request-action="instant" type="submit">⇩ Buscar y descargar ahora</button>
          </div>
        </form>
      </div>
    </section>

    <dialog id="examplesModal" class="config-modal" aria-labelledby="examplesModalTitle">
      <div class="config-modal-content">
        <div class="config-modal-header">
          <div><h2 id="examplesModalTitle">Ejemplos comprobados</h2><p class="muted">Estas coordenadas ya generaron al menos un PDF correctamente en este equipo.</p></div>
          <button id="closeExamplesModal" class="modal-close" type="button" aria-label="Cerrar">×</button>
        </div>
        <div id="exampleList" class="example-list"><span class="muted">Cargando ejemplos…</span></div>
      </div>
    </dialog>

    <dialog id="pdfPreviewModal" class="pdf-preview-modal" aria-labelledby="pdfPreviewTitle">
      <div class="pdf-preview-content">
        <div class="pdf-preview-header"><h2 id="pdfPreviewTitle">Vista previa del PDF</h2><button id="closePdfPreview" class="modal-close" type="button" aria-label="Cerrar">×</button></div>
        <div id="pdfPreviewFiles" class="pdf-preview-files"></div>
        <iframe id="pdfPreviewFrame" class="pdf-preview-frame" title="Vista previa del PDF" referrerpolicy="no-referrer"></iframe>
      </div>
    </dialog>

    <dialog id="configModal" class="config-modal" aria-labelledby="configModalTitle">
      <div class="config-modal-content">
        <div class="config-modal-header">
          <div><h2 id="configModalTitle">Cuentas autorizadas</h2><p class="muted">Agrega, actualiza o elimina cuentas locales. El worker debe estar detenido.</p></div>
          <button id="closeConfigModal" class="modal-close" type="button" aria-label="Cerrar">×</button>
        </div>
        <div id="accountEditorList" class="account-editor-list"></div>
        <div class="modal-actions">
          <button id="addAccount" class="onboarding-action results" type="button">＋ Agregar cuenta</button>
          <button id="saveAccounts" class="modal-save" type="button">Guardar configuración</button>
        </div>
        <div id="configModalFeedback" class="control-feedback" role="status"></div>
        <p class="modal-note">Las contraseñas existentes nunca se cargan aquí. Déjalas vacías para conservarlas; usa <strong>Ver</strong> solo para revisar una contraseña que hayas escrito durante esta sesión.</p>
      </div>
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
      <div class="kpi"><div class="muted">Usadas hoy</div><div id="usedToday" class="value">-</div></div>
      <div class="kpi"><div class="muted">Restantes hoy</div><div id="remainingToday" class="value">-</div></div>
      <div class="kpi"><div class="muted">PDFs generados</div><div id="downloads" class="value">-</div></div>
      <div class="kpi"><div class="muted">Captchas pendientes</div><div id="captchaPending" class="value">-</div></div>
      <div class="kpi"><div class="muted">Jobs en cola</div><div id="queuedJobs" class="value">-</div></div>
      <div class="kpi"><div class="muted">Respaldo</div><div id="backupState" class="value" style="font-size:18px">-</div></div>
    </div>

    <section id="jobsPanel" class="panel" style="margin-bottom:16px;display:none">
      <h2>Solicitudes recientes</h2>
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
      <h2>Ciclos recientes</h2>
      <table>
        <thead>
          <tr><th>#</th><th>Cuenta</th><th>Estado</th><th>Resultados</th><th>PDF</th><th>Parada</th><th>Finalizado</th></tr>
        </thead>
        <tbody id="cycles"></tbody>
      </table>
    </section>
  </main>
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
    let current = null;
    let lastCaptchaNoticeKey = "";
    let stopRequested = false;
    let resumeRequested = false;
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
        button.innerHTML = stopRequested ? "⌛ Deteniendo…" : "■ Detener";
        hint.textContent = stopRequested
          ? "No se tomarán más trabajos; se espera el punto seguro actual."
          : "Detiene el worker de forma segura al terminar el trabajo actual.";
        return;
      }
      stopRequested = false;
      button.className = "worker-action resume";
      button.disabled = resumeRequested;
      button.innerHTML = resumeRequested ? "⌛ Reanudando…" : "▶ Reanudar worker";
      hint.textContent = resumeRequested
        ? "Solicitando el arranque seguro del servicio CBRS."
        : status === "stopped"
          ? "El test está detenido. PDFs, SQLite y trabajos en cola se conservan."
          : "No hay un worker activo. Puedes iniciar el servicio CBRS.";
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
      const routeCount = pool.egress_routes ?? (current.accounts || []).length;
      const routeMode = pool.shared_egress ? `${routeCount} salida Chile compartida` : `${routeCount} salidas dedicadas`;
      document.getElementById("accountSummary").textContent = `${(current.accounts || []).length} cuentas autorizadas · ${quota} consultas teóricas por día · ${routeMode}`;
      const jobSummary = current.jobs?.summary || null;
      document.getElementById("queuedJobs").textContent = jobSummary ? (jobSummary.queued ?? 0) : "-";
      const backupLabels = { healthy: "saludable", stale: "atrasado", failed: "fallido", low_disk: "poco espacio", invalid: "inválido", not_configured: "no configurado" };
      document.getElementById("backupState").textContent = current.backup ? (backupLabels[current.backup.status] || current.backup.status) : "-";
      renderAlert(current.alert);
      renderPoolFacts(pool, run, nextSeconds);
      renderAccounts(current.accounts || []);
      renderCycles(current.cycles || [], current.artifacts || []);
      renderJobs(current.jobs?.recent || []);
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
          : "Salida dedicada";
        const note = account.paused_reason
          ? `Motivo: ${account.paused_reason} · ${route}`
          : `${account.remaining_today} restantes hoy · ${route}`;
        const phase = account.captcha_phase || "";
        const phaseLabel = captchaPhaseLabels[phase] || "";
        const action = account.status === "captcha_pending"
          ? `<button class="account-action" data-captcha-account="${escapeHtml(account.account_id)}" data-captcha-action="trigger">Resolver captcha</button>`
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
            <div class="status ${escapeHtml(account.status)}">${escapeHtml(phaseLabel || label(account.status))}</div>
          </div>
          <div class="account-count">${account.used_today}/${account.daily_quota}</div>
          <div class="mini-bar"><span style="width:${pct}%"></span></div>
          <div class="muted" style="margin-top:8px">${escapeHtml(note)}</div>
          ${phaseView}
          ${action}
        </section>`;
      }).join("");
    }
    async function triggerCaptcha(accountId, action, button) {
      if (!accountId) return;
      if (button) {
        button.disabled = true;
        button.textContent = action === "complete" ? "Validando..." : "Abriendo Chrome...";
      }
      await fetch(`/api/captcha/${encodeURIComponent(accountId)}/${action}`, { method: "POST" });
      if (action === "trigger" && current?.runtime?.visual_url) {
        window.open(current.runtime.visual_url, "cbrsVisualRecovery", "noopener");
      }
      await refresh();
    }
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
      document.getElementById("jobs").innerHTML = jobs.map((job) => {
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
          const open = attempts.length && ["waiting_capacity", "waiting_captcha"].includes(job.status) ? " open" : "";
          reason = `<span class="job-reason"><details class="attempt-details"${open}><summary>${escapeHtml(summaryParts.join(" · "))}</summary><span class="attempt-list">${failedEntries.map((entry) => {
            const rawReason = entry.reason || "failed";
            const reasonText = stopReasonLabels[rawReason] || label(rawReason);
            const actionText = entry.attempted ? "Intentado" : "No intentado";
            return `<span class="attempt-tuple"><span class="attempt-number">${escapeHtml(entry.number)}</span><span class="attempt-account">${accountBadge(entry.accountId)}</span><span class="attempt-outcome"><strong>${actionText}:</strong> ${escapeHtml(reasonText)}</span></span>`;
          }).join("")}</span></details></span>`;
        } else if (job.status === "waiting_capacity") {
          reason = `<span class="job-reason">No se envió: no había cuentas elegibles.</span>`;
        } else if (job.status === "waiting_captcha") {
          reason = `<span class="job-reason">No se envió: todas las cuentas requerían captcha.</span>`;
        } else if (job.error_code) {
          reason = `<span class="job-reason">${escapeHtml(stopReasonLabels[job.error_code] || label(job.error_code))}</span>`;
        }
        return `<tr>
          <td><a class="job-id" href="/api/jobs/${encodeURIComponent(job.job_id)}" target="_blank" title="${escapeHtml(job.job_id)}" aria-label="Abrir ${escapeHtml(job.job_id)}"><code>${escapeHtml(shortJobId(job.job_id))}</code></a></td>
          <td>${escapeHtml(job.kind)}</td>
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
          daily_quota: Number(field("daily_quota")),
          existing: row.dataset.existing === "true",
        };
      });
      const incompleteNewAccount = accounts.some((account) => !account.existing && (!account.username || !account.password || !account.proxy_url));
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
        exampleList.innerHTML = examples.map((example) => `<button class="example-choice" type="button" data-example-foja="${example.foja}" data-example-numero="${example.numero}" data-example-year="${example.year}"><span>Foja ${example.foja} · Número ${example.numero} · Año ${example.year}<br><small>${example.success_count} descarga${example.success_count === 1 ? "" : "s"} correcta${example.success_count === 1 ? "" : "s"}</small></span><small>Usar ejemplo →</small></button>`).join("");
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
    refresh().catch(console.error);
    setInterval(() => refresh().catch(console.error), 2000);
  </script>
</body>
</html>"""
