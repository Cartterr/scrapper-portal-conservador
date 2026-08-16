from __future__ import annotations

import json
import mimetypes
import os
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
                self._send_json(_with_artifact_urls(payload))
                return
            if job_store is not None and parsed.path == "/api/jobs":
                self._send_json(
                    {"jobs": _with_job_artifact_urls(job_store.list_jobs(limit=_limit(parsed.query)))}
                )
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

        def _create_job(self) -> None:
            from .jobs import IdempotencyConflictError

            try:
                body = self._read_json()
                kind = str(body.get("kind") or ("text" if body.get("text") else "fna"))
                job, _created = job_store.create_job(
                    kind=kind,
                    input_data=body,
                    idempotency_key=body.get("idempotency_key"),
                )
            except IdempotencyConflictError as exc:
                self._send_api_error(HTTPStatus.CONFLICT, "idempotency_conflict", str(exc))
                return
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
                return
            self._send_json(
                {
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "status_url": f"/api/jobs/{job['job_id']}",
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
                        if not _hold_visual_captcha_session(
                            store,
                            settings,
                            config,
                            account_id=account_id,
                            confirmation=confirmation,
                        ):
                            return
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
) -> bool:
    from .account_pool import account_settings
    from .browser_session import BrowserSession

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
            browser.goto_index()
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
    return {
        "browser_backend": settings.browser_backend,
        "browser_headless": settings.headless,
        "browser_window_mode": settings.window_mode,
        "expected_egress_country": settings.expected_egress_country,
        "request_delay_seconds": settings.request_delay_seconds,
        "visual_url": os.environ.get(
            "CBRS_NOVNC_URL",
            "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale",
        ),
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
    .account.captcha_solving { border-color: var(--warn); background: var(--warn-soft); }
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
    @media (prefers-reduced-motion: reduce) {
      .account.captcha_pending,
      .account.captcha_pending::before {
        animation: none;
      }
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; }
    th { color: var(--muted); }
    a { color: var(--accent); text-decoration: none; font-weight: 700; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    @media (max-width: 900px) {
      .hero, .accounts, .kpis { grid-template-columns: 1fr; }
      .headline h2 { font-size: 34px; }
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
      <button id="stopButton">Detener</button>
      <div id="status" class="status">cargando</div>
    </div>
  </header>
  <main>
    <section id="alert" class="alert">
      <h2 id="alertTitle">Advertencia</h2>
      <div id="alertMessage">-</div>
    </section>

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
    <div class="muted" style="margin:-8px 0 16px">Cuentas: Ejecutivo 1 · Ejecutivo 2 · Ejecutivo 3</div>

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
      <table>
        <thead>
          <tr><th>Job</th><th>Tipo</th><th>Estado</th><th>Resultados</th><th>PDFs</th><th>Finalizado</th><th>Acción</th></tr>
        </thead>
        <tbody id="jobs"></tbody>
      </table>
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
    const label = (value) => statusLabels[value] || value || "-";
    const localTime = (value) => value ? new Date(value).toLocaleString() : "-";
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
    async function refresh() {
      const response = await fetch("/api/status");
      current = await response.json();
      render();
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
        const action = account.status === "captcha_pending"
          ? `<button class="account-action" data-captcha-account="${escapeHtml(account.account_id)}" data-captcha-action="trigger">Resolver captcha</button>`
          : account.status === "captcha_solving"
            ? `<button class="account-action" data-captcha-account="${escapeHtml(account.account_id)}" data-captcha-action="complete">Validar y reactivar</button>`
            : "";
        return `<section class="account ${escapeHtml(account.status)}" style="--wave-index:${index % 9}">
          <div class="account-top">
            <div class="account-name">${escapeHtml(account.label)}</div>
            <div class="status ${escapeHtml(account.status)}">${escapeHtml(label(account.status))}</div>
          </div>
          <div class="account-count">${account.used_today}/${account.daily_quota}</div>
          <div class="mini-bar"><span style="width:${pct}%"></span></div>
          <div class="muted" style="margin-top:8px">${escapeHtml(note)}</div>
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
      document.getElementById("jobs").innerHTML = jobs.map((job) => {
        const terminal = ["completed", "partial", "failed", "cancelled"].includes(job.status);
        const action = terminal ? "-" : `<button data-cancel-job="${escapeHtml(job.job_id)}" style="padding:5px 8px">Cancelar</button>`;
        return `<tr>
          <td><a href="/api/jobs/${encodeURIComponent(job.job_id)}" target="_blank"><code>${escapeHtml(job.job_id)}</code></a></td>
          <td>${escapeHtml(job.kind)}</td>
          <td><span class="status ${escapeHtml(job.status)}">${escapeHtml(label(job.status))}</span></td>
          <td>${job.result_count ?? "-"}</td>
          <td>${job.completed_items ?? 0}</td>
          <td>${localTime(job.finished_at)}</td>
          <td>${action}</td>
        </tr>`;
      }).join("");
    }
    document.getElementById("stopButton").addEventListener("click", async () => {
      await fetch("/api/stop", { method: "POST" });
      await refresh();
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
    document.getElementById("jobs").addEventListener("click", async (event) => {
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
