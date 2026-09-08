from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .account_pool import (
    CAPTCHA_PENDING_STATUS,
    CAPTCHA_SOLVING_STATUS,
    AccountPoolStore,
    PoolAccount,
    PoolConfig,
    account_credentials,
    account_settings,
    load_account_pool_config,
    local_today,
    next_quota_reset_at,
    seconds_since,
    utc_now,
)
from .browser_session import CommerceAuthState, CredentialsRejectedError
from .browser_runtime import validate_service_browser
from .browser_preview import capture_browser_preview, remove_browser_preview
def capture_error(*args, **kwargs):
    from .runtime_updates import runtime_module
    return runtime_module("error_evidence").capture_error(*args, **kwargs)
from .config import SETTINGS, Settings
from .dataimpulse import (
    DATAIMPULSE_STICKY_PROVIDERS,
    next_unused_sticky_port,
)
def create_pdf(*args, **kwargs):
    from .runtime_updates import runtime_module
    return runtime_module("pdf").create_pdf(*args, **kwargs)
from .safety import SafetyStopException, StopReason, redact, redact_text

JOB_STATES = frozenset(
    {
        "queued",
        "running",
        "waiting_capacity",
        "waiting_captcha",
        "completed",
        "partial",
        "failed",
        "cancelled",
    }
)
CLAIMABLE_JOB_STATES = ("queued", "waiting_capacity", "waiting_captcha")
TERMINAL_JOB_STATES = frozenset({"completed", "partial", "failed", "cancelled"})
GLOBAL_SAFETY_REASONS = frozenset(
    {StopReason.RATE_LIMIT, StopReason.WAF_CHALLENGE}
)
SAFETY_COOLDOWN_SECONDS = {
    StopReason.CAPTCHA_REJECTED: 120.0,
    StopReason.CAPTCHA_SOLVER: 300.0,
    StopReason.AUTH_REQUIRED: 300.0,
    StopReason.EGRESS_PREFLIGHT: 60.0,
    StopReason.PROXY_HEALTH: 60.0,
    StopReason.TEMPORARY_UNAVAILABLE: 120.0,
    StopReason.UNEXPECTED_HTML: 120.0,
    StopReason.UNEXPECTED_STATUS: 120.0,
    StopReason.RATE_LIMIT: 300.0,
    StopReason.WAF_CHALLENGE: 300.0,
}
WORKER_LEASE_NAME = "portal_worker"
WORKER_STALE_SECONDS = 120
JOB_LEASE_SECONDS = 180
EXTERNAL_OUTAGE_BACKOFF_KEY = "external_outage_backoff"
EXTERNAL_OUTAGE_REASON = "temporary_unavailable_all_accounts"
DATAIMPULSE_ROTATION_REQUEST_KEY = "dataimpulse_rotation_request"
# Fixed hourly retry-allowance window for candidate reservations per account.
ROTATION_WINDOW_SECONDS = 3600.0
# Route status when the per-account candidate allowance for the current window
# is used up. It describes OUR retry budget, not provider traffic nor proof that
# every available proxy failed. ``proxy_recovery_exhausted`` is the legacy name.
RETRY_ALLOWANCE_EXHAUSTED = "retry_allowance_exhausted"
# Candidate outcomes (durable, sanitized). Transport failures are kept apart
# from portal login rejections so they can be paced and labelled differently.
CANDIDATE_PROMOTED = "promoted"
CANDIDATE_CONNECTIVITY_FAILED = "candidate_connectivity_failed"
CANDIDATE_EXIT_REUSED = "candidate_exit_reused"
CANDIDATE_LOGIN_REJECTED = "candidate_login_rejected"
CANDIDATE_FORM_UNCONFIRMED = "candidate_form_unconfirmed"
CANDIDATE_LAUNCH_FAILED = "candidate_launch_failed"
CANDIDATE_PROVIDER_TERMINAL = "candidate_provider_terminal"
CANDIDATE_CREDENTIALS_REJECTED = "candidate_credentials_rejected"
CANDIDATE_PROVEN_UNPERSISTED = "candidate_proven_unpersisted"
# Browser auth error codes published to the overview (more specific than the
# shared ``temporary_unavailable`` stop reason they derive from).
AUTH_LOGIN_REJECTED = "login_rejected"
AUTH_LOGIN_PAGE_REJECTED = "login_page_rejected"
DATAIMPULSE_ROTATION_RESULT_KEY = "dataimpulse_rotation_result"
# `temporary_unavailable` is CBRS's generic retry response, not proof of a
# CAPTCHA failure.  Once every account returns it, however, repeating the same
# protected request every two minutes only amplifies a route- or portal-wide
# outage.  Escalate quickly and cap control probes at one per hour.  A
# successful search clears the streak immediately.
EXTERNAL_OUTAGE_BACKOFF_SECONDS = (300.0, 900.0, 3600.0)
# A slot is reserved before the portal request to keep concurrent workers from
# exceeding the account cap.  It becomes real usage only after CBRS accepts
# the search; every failure path must release that reservation.
QUOTA_SUCCESS_ATTEMPT_STATUSES = frozenset({"search_completed", "completed"})
PDF_PAGE_OBJECT_RE = re.compile(rb"/Type\s*/Page(?!s)\b")


class IdempotencyConflictError(ValueError):
    pass


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: str
    input: dict[str, Any]
    status: str
    idempotency_key: str | None
    created_at: str
    updated_at: str
    cancel_requested: bool = False
    source: str = "production"


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    worker_id: str
    run_id: str | None
    status: str
    processed_jobs: int


@dataclass
class _ManagedAccountScraper:
    manager: Any
    scraper: Any
    settings: Settings | None = None
    username: str = ""
    password: str = ""
    unknown_checks: int = 0
    last_reauth_at: float = 0.0
    last_restart_at: float = 0.0
    reauth_required: bool = False
    authenticated_once: bool = False


def _browser_engine(settings: Settings | None) -> str:
    """Return the stable, non-secret engine identifier published to operators."""
    backend = str(getattr(settings, "browser_backend", "chrome") or "chrome").lower()
    return {
        "chrome": "native_chrome",
        "cloak": "cloakbrowser",
        "gologin": "gologin",
    }.get(backend, backend)


class _PersistentAccountBrowsers:
    """Own one long-lived scraper/browser context per worker account."""

    def __init__(
        self,
        *,
        scraper_factory: Callable[..., Any],
        headless: bool,
        store: "JobStore",
        worker_id: str,
    ) -> None:
        self.scraper_factory = scraper_factory
        self.headless = headless
        self.store = store
        self.worker_id = worker_id
        self._entries: dict[str, _ManagedAccountScraper] = {}
        self._retained_entries: list[tuple[str, _ManagedAccountScraper]] = []
        self._known_accounts: dict[str, tuple[Settings, str, str]] = {}
        self._last_reconcile_at = 0.0
        self._last_preview_at = 0.0
        self.on_auth_failure: Callable[[str, Exception], None] | None = None
        self.on_auth_success: Callable[[str], None] | None = None
        self.can_reauthenticate: Callable[[str], bool] = lambda _account_id: True

    def capture_previews(self, *, force: bool = False) -> None:
        """Publish low-frequency viewport frames without changing browser state."""
        now = time.monotonic()
        intervals = [
            settings.browser_preview_interval_seconds
            for settings, _username, _password in self._known_accounts.values()
        ]
        interval = min(intervals, default=5.0)
        from .runtime_updates import runtime_module
        interval = runtime_module("runtime_observation").preview_interval(interval)
        if not force and now - self._last_preview_at < interval:
            return
        self._last_preview_at = now
        for account_id, entry in tuple(self._entries.items()):
            browser = getattr(entry.scraper, "browser", entry.scraper)
            page = getattr(browser, "page", None)
            if page is None or not callable(getattr(page, "screenshot", None)):
                continue
            try:
                if callable(getattr(page, "is_closed", None)) and page.is_closed():
                    continue
                # Keep the badge's DOM evidence on the same cadence as its
                # preview, including during account cooldowns. This is purely
                # observational: no login, navigation, or cooldown reset.
                self.refresh_page_auth_states(account_ids={account_id})
                capture_browser_preview(page, self.store.path, account_id)
            except Exception:
                # Preview capture is observational and must never interrupt the
                # login, search, recovery, or exclusive browser-owner lease.
                continue

    def refresh_page_auth_states(self, *, account_ids: set[str] | None = None) -> None:
        from .runtime_updates import runtime_module
        runtime_module("runtime_observation").sample_auth(self, account_ids=account_ids)

    def reconcile(self) -> None:
        """Keep successful per-account contexts alive without cross-account resets."""
        now = time.monotonic()
        intervals = [
            settings.browser_healthcheck_seconds
            for settings, _username, _password in self._known_accounts.values()
        ]
        interval = min(intervals, default=30.0)
        healthcheck_due = now - self._last_reconcile_at >= interval
        if healthcheck_due:
            self._last_reconcile_at = now
            self.refresh_page_auth_states()
        for account_id, credentials in tuple(self._known_accounts.items()):
            settings, username, password = credentials
            entry = self._entries.get(account_id)
            # An already-known failed login need not wait for the next passive
            # health scan after its cooldown expires. This is scheduling only:
            # account/global/route gates and the retry floor still apply.
            now = time.monotonic()
            if not healthcheck_due:
                if entry is None or not entry.reauth_required:
                    continue
                if now - entry.last_reauth_at < settings.browser_reauth_backoff_seconds:
                    continue
            if not self.can_reauthenticate(account_id):
                continue
            if not healthcheck_due:
                # The operator may already have completed login manually.
                # Re-check that account's DOM before any active recovery.
                self.refresh_page_auth_states(account_ids={account_id})
            if entry is None:
                try:
                    with self.session(account_id, settings, username, password):
                        pass
                except Exception as exc:
                    if self.on_auth_failure:
                        self.on_auth_failure(account_id, exc)
                else:
                    if self.on_auth_success:
                        self.on_auth_success(account_id)
                continue
            try:
                browser = getattr(entry.scraper, "browser", entry.scraper)
                page = browser.page
                if callable(getattr(page, "is_closed", None)) and page.is_closed():
                    self.discard(account_id, status="browser_context_closed")
                    with self.session(account_id, settings, username, password):
                        pass
                    if self.on_auth_success:
                        self.on_auth_success(account_id)
                    continue
                raw_state = browser.detect_commerce_auth_state()
                state = (
                    raw_state
                    if isinstance(raw_state, CommerceAuthState)
                    else CommerceAuthState(str(raw_state))
                )
                if self.can_replace_rejected_login(account_id):
                    # Do not repeatedly submit the same visibly rejected login.
                    # Route it to the existing bounded account-recovery policy.
                    if now - entry.last_reauth_at < settings.browser_reauth_backoff_seconds:
                        continue
                    entry.last_reauth_at = now
                    check = self.store.account_check(account_id) or {}
                    exc = SafetyStopException(
                        StopReason.TEMPORARY_UNAVAILABLE,
                        "Visible rejected login requires scoped recovery",
                        status=check.get("browser_last_auth_http_status"),
                        context="auth login",
                    )
                    capture_error(self.store, account_id, browser, exc)
                    if self.on_auth_failure:
                        self.on_auth_failure(account_id, exc)
                    continue
                if (state is CommerceAuthState.UNKNOWN and entry.unknown_checks >= 2
                        and not entry.authenticated_once and not entry.reauth_required):
                    browser.reload_current_page()
                    state = browser.wait_for_commerce_auth_state()
                    entry.unknown_checks = 0
                should_reauthenticate = state is CommerceAuthState.LOGIN_GATE or (
                    state is CommerceAuthState.UNKNOWN and entry.reauth_required
                    and not entry.authenticated_once
                )
                if should_reauthenticate:
                    now = time.monotonic()
                    if now - entry.last_reauth_at < settings.browser_reauth_backoff_seconds:
                        continue
                    entry.last_reauth_at = now
                    with self.session(
                        account_id,
                        settings,
                        username,
                        password,
                        force=True,
                    ):
                        pass
                    if self.on_auth_success:
                        self.on_auth_success(account_id)
            except Exception as exc:
                if _looks_like_connection_failure(exc):
                    self.discard(account_id, status="browser_context_failed")
                if self.on_auth_failure:
                    self.on_auth_failure(account_id, exc)

    @contextmanager
    def session(
        self,
        account_id: str,
        settings: Settings,
        username: str,
        password: str,
        *,
        force: bool = False,
    ) -> Iterator[Any]:
        validate_service_browser(settings)
        self._known_accounts[account_id] = (settings, username, password)
        entry = self._entries.get(account_id)
        if entry is None:
            manager = self.scraper_factory(headless=self.headless, settings=settings)
            try:
                scraper = manager.__enter__() if hasattr(manager, "__enter__") else manager
            except Exception:
                self.store.set_account_browser_state(
                    account_id,
                    live=False,
                    authenticated=False,
                    headless=self.headless,
                    owner=self.worker_id,
                    engine=_browser_engine(settings),
                    status="launch_failed",
                )
                raise
            entry = _ManagedAccountScraper(
                manager=manager,
                scraper=scraper,
                settings=settings,
                username=username,
                password=password,
                last_restart_at=time.monotonic(),
            )
            self._entries[account_id] = entry
            self.store.set_account_browser_state(
                account_id,
                live=True,
                authenticated=False,
                headless=self.headless,
                owner=self.worker_id,
                engine=_browser_engine(settings),
                status="authenticating",
            )

        browser = getattr(entry.scraper, "browser", entry.scraper)
        preserve = getattr(browser, "preserve_for_service_lifetime", None)
        browser.error_capture_callback = lambda exc: capture_error(self.store, account_id, browser, exc)
        if callable(preserve):
            preserve()
        preview_setter = getattr(browser, "set_preview_callback", None)
        if callable(preview_setter):
            preview_setter(self.capture_previews)

        # Publish the existing viewport before authentication begins. This
        # gives the operator immediate visual evidence even if CBRS, Imperva,
        # or reCAPTCHA makes the login step slow.
        self.capture_previews(force=True)
        try:
            auth_method = entry.scraper.ensure_authenticated(
                username, password, force=force
            )
        except Exception as exc:
            capture_error(self.store, account_id, browser, exc)
            if _looks_like_connection_failure(exc):
                self.discard(account_id, status="browser_context_failed")
            else:
                entry.reauth_required = True
                entry.last_reauth_at = time.monotonic()
                self.store.set_account_browser_state(
                    account_id,
                    live=True,
                    authenticated=False,
                    headless=self.headless,
                    owner=self.worker_id,
                    engine=_browser_engine(entry.settings),
                    status="authentication_unconfirmed",
                    auth_state=CommerceAuthState.UNKNOWN.value,
                    auth_error=_auth_failure_code(exc),
                    auth_http_status=getattr(exc, "status", None),
                )
            raise

        entry.reauth_required = False
        entry.authenticated_once = True
        self.store.set_account_browser_state(
            account_id,
            live=True,
            authenticated=True,
            headless=self.headless,
            owner=self.worker_id,
            engine=_browser_engine(entry.settings),
            status={
                "refreshed": "authenticated_refresh",
                "browser_fetch": "authenticated_login_api",
                "browser_form": "authenticated_login_form",
            }.get(str(auth_method or ""), "authenticated"),
            auth_state=CommerceAuthState.AUTHENTICATED_FORM.value,
        )
        self.capture_previews(force=True)
        try:
            yield entry.scraper
        except Exception as exc:
            capture_error(self.store, account_id, browser, exc)
            raise
        finally:
            self.capture_previews(force=True)

    def has_protected_session(self, account_id: str) -> bool:
        entries = [entry for key, entry in self._retained_entries if key == account_id]
        current = self._entries.get(account_id)
        if current is not None:
            entries.append(current)
            browser = getattr(current.scraper, "browser", current.scraper)
            detector = getattr(browser, "detect_commerce_auth_state", None)
            if callable(detector):
                try:
                    if detector() == CommerceAuthState.AUTHENTICATED_FORM:
                        current.authenticated_once = True
                except Exception:
                    pass
        return any(entry.authenticated_once for entry in entries)

    def can_replace_rejected_login(self, account_id: str) -> bool:
        allowed = {v.strip() for v in os.environ.get(
            "CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS", ""
        ).split(",") if v.strip()}
        if account_id not in allowed:
            return False
        current = self._entries.get(account_id)
        if current is None:
            return False
        browser = getattr(current.scraper, "browser", current.scraper)
        if getattr(browser, 'is_remote', False):
            from .owner_protocol import owner_preserves_recovery_contexts
            if not owner_preserves_recovery_contexts(current.settings, self.store):
                return False  # Keep normal same-browser login retries enabled.
        detector = getattr(browser, "has_visible_rejected_login", None)
        try:
            return bool(callable(detector) and detector())
        except Exception:
            return False

    def adopt_authenticated_candidate(self, account_id: str, entry: _ManagedAccountScraper) -> None:
        """Keep the exact proven browser; retain the older failed context open."""
        previous = self._entries.get(account_id)
        if previous is not None:
            # Candidate authorization is NOT authorization to close the old
            # production context, even after a later visible login rejection.
            # Keep both exact instances; only explicit owner shutdown may close
            # retained production contexts. No DOM uncertainty can weaken this.
            self._retained_entries.append((account_id, previous))
        self._entries[account_id] = entry
        self._known_accounts[account_id] = (entry.settings, entry.username, entry.password)
        browser = getattr(entry.scraper, "browser", entry.scraper)
        setter = getattr(browser, "set_preview_callback", None)
        if callable(setter):
            setter(self.capture_previews)
        self.store.set_account_browser_state(
            account_id, live=True, authenticated=True, headless=self.headless,
            owner=self.worker_id, engine=_browser_engine(entry.settings),
            status="authenticated_candidate_retained", auth_state="authenticated_form",
        )
        self.capture_previews(force=True)

    def discard(self, account_id: str, *, status: str,
                service_shutdown: bool = False) -> None:
        entry = self._entries.get(account_id)
        if service_shutdown and entry and getattr(getattr(entry.scraper, "browser", None), "is_remote", False):
            self._entries.pop(account_id, None)
            return  # worker detaches; owner keeps previews, evidence and Chrome
        # HARD LIFECYCLE RULE: an existing production context is irreplaceable
        # during service operation. A network error is not browser termination.
        # Keep even unknown/disconnected contexts for operator inspection.
        if account_id in self._entries and not service_shutdown:
            self.store.add_event(
                "browser_close_blocked", account_id=account_id, level="warning",
                data={"reason": status, "policy": "preserve_until_service_stop"},
            )
            return
        entry = self._entries.pop(account_id, None)
        engine = _browser_engine(entry.settings if entry is not None else None)
        if entry is not None:
            try:
                browser = getattr(entry.scraper, "browser", entry.scraper)
                preview_setter = getattr(browser, "set_preview_callback", None)
                if callable(preview_setter):
                    preview_setter(None)
                shutdown = getattr(browser, "shutdown_service_context", None)
                if service_shutdown and callable(shutdown):
                    shutdown()
                if hasattr(entry.manager, "__exit__"):
                    entry.manager.__exit__(None, None, None)
                elif hasattr(entry.scraper, "close"):
                    entry.scraper.close()
            except Exception:
                pass
        remove_browser_preview(self.store.path, account_id)
        self.store.set_account_browser_state(
            account_id,
            live=False,
            authenticated=False,
            headless=self.headless,
            owner=self.worker_id,
            engine=engine,
            status=status,
        )

    def close_all(self, *, status: str = "worker_stopped",
                  service_shutdown: bool = False) -> None:
        for account_id in tuple(self._entries):
            self.discard(account_id, status=status, service_shutdown=service_shutdown)
        if service_shutdown:
            for _account_id, entry in self._retained_entries:
                try:
                    browser = getattr(entry.scraper, "browser", entry.scraper)
                    shutdown = getattr(browser, "shutdown_service_context", None)
                    if callable(shutdown):
                        shutdown()
                    if hasattr(entry.manager, "__exit__"):
                        entry.manager.__exit__(None, None, None)
                    elif hasattr(entry.scraper, "close"):
                        entry.scraper.close()
                except Exception:
                    pass
            self._retained_entries.clear()
            self._known_accounts.clear()


class JobStore:
    """Durable production queue stored beside the existing account-pool state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA journal_mode = WAL")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attempt_error_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    capture_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    http_status INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_attempt_error_evidence
                ON attempt_error_evidence(attempt_id);

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'production',
                    priority INTEGER NOT NULL DEFAULT 0,
                    result_count INTEGER,
                    completed_items INTEGER NOT NULL DEFAULT 0,
                    failed_items INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    current_account_id TEXT,
                    next_run_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    worker_owner TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                ON jobs(status, next_run_at, created_at);

                CREATE TABLE IF NOT EXISTS job_items (
                    item_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    ticket_ref TEXT,
                    result_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_pages INTEGER,
                    output_path TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(job_id, sequence),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS archived_jobs (
                    job_id TEXT PRIMARY KEY, archived_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS search_retry_clearance (
                    job_id TEXT PRIMARY KEY, authorized_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    item_id TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    page_count INTEGER,
                    valid INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
                    FOREIGN KEY(item_id) REFERENCES job_items(item_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS job_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    quota_date TEXT NOT NULL,
                    quota_consumed INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    safety_stop TEXT,
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_job_attempts_usage
                ON job_attempts(account_id, quota_date, quota_consumed);

                CREATE TABLE IF NOT EXISTS account_daily_usage (
                    account_id TEXT NOT NULL,
                    quota_date TEXT NOT NULL,
                    used INTEGER NOT NULL,
                    last_used_at TEXT,
                    PRIMARY KEY(account_id, quota_date)
                );

                CREATE TABLE IF NOT EXISTS account_checks (
                    account_id TEXT PRIMARY KEY,
                    session_checked_date TEXT,
                    proxy_checked_date TEXT,
                    proxy_checked_at TEXT,
                    proxy_status TEXT,
                    egress_hash TEXT,
                    browser_live INTEGER NOT NULL DEFAULT 0,
                    browser_authenticated INTEGER NOT NULL DEFAULT 0,
                    browser_headless INTEGER,
                    browser_owner TEXT,
                    browser_engine TEXT,
                    browser_status TEXT,
                    browser_auth_state TEXT,
                    browser_started_at TEXT,
                    browser_checked_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leases (
                    lease_name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    account_id TEXT,
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS job_control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_rotation (
                    name TEXT PRIMARY KEY,
                    next_index INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_proxy_routes (
                    account_id TEXT PRIMARY KEY,
                    active_port INTEGER NOT NULL,
                    pending_port INTEGER,
                    generation INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'configured',
                    last_error_code TEXT,
                    last_rotation_reason TEXT,
                    last_rotated_at TEXT,
                    cooldown_until TEXT,
                    rotation_window_started_at TEXT,
                    rotation_count INTEGER NOT NULL DEFAULT 0,
                    temporary_window_started_at TEXT,
                    temporary_failure_count INTEGER NOT NULL DEFAULT 0,
                    rejected_ports_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS proxy_candidate_exits (
                    account_id TEXT NOT NULL,
                    egress_hash TEXT NOT NULL,
                    failed_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, egress_hash)
                );

                CREATE TABLE IF NOT EXISTS proxy_candidate_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    sticky_port INTEGER,
                    egress_route_id TEXT,
                    outcome TEXT NOT NULL,
                    http_status INTEGER,
                    response_code TEXT,
                    reason TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proxy_candidate_attempts_account
                    ON proxy_candidate_attempts(account_id, finished_at);

                CREATE TABLE IF NOT EXISTS endurance_state (
                    name TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    fixture_index INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            db.execute(
                """
                INSERT INTO schema_versions(component, version, applied_at)
                VALUES ('jobs', 9, ?)
                ON CONFLICT(component) DO UPDATE SET
                    version = MAX(version, excluded.version),
                    applied_at = CASE
                        WHEN version < excluded.version THEN excluded.applied_at
                        ELSE applied_at
                    END
                """,
                (utc_now(),),
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(account_checks)").fetchall()
            }
            if "egress_hash" not in columns:
                db.execute("ALTER TABLE account_checks ADD COLUMN egress_hash TEXT")
            if "proxy_checked_at" not in columns:
                db.execute("ALTER TABLE account_checks ADD COLUMN proxy_checked_at TEXT")
            browser_columns = {
                "browser_live": "INTEGER NOT NULL DEFAULT 0",
                "browser_authenticated": "INTEGER NOT NULL DEFAULT 0",
                "browser_headless": "INTEGER",
                "browser_owner": "TEXT",
                "browser_engine": "TEXT",
                "browser_status": "TEXT",
                "browser_auth_state": "TEXT",
                "browser_started_at": "TEXT",
                "browser_checked_at": "TEXT",
                "browser_authenticated_at": "TEXT",
                "browser_last_auth_error": "TEXT",
                "browser_last_auth_http_status": "INTEGER",
            }
            for name, definition in browser_columns.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE account_checks ADD COLUMN {name} {definition}")
            route_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(account_proxy_routes)"
                ).fetchall()
            }
            if "rejected_ports_json" not in route_columns:
                db.execute(
                    "ALTER TABLE account_proxy_routes ADD COLUMN "
                    "rejected_ports_json TEXT NOT NULL DEFAULT '[]'"
                )
            job_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "priority" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "source" not in job_columns:
                db.execute(
                    "ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'production'"
                )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_endurance_job
                ON jobs(source)
                WHERE source = 'endurance'
                  AND status IN ('queued','running','waiting_capacity','waiting_captcha')
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_claim_priority
                ON jobs(status, next_run_at, priority DESC, created_at)
                """
            )

    def create_job(
        self,
        *,
        kind: str,
        input_data: Mapping[str, Any],
        idempotency_key: str | None = None,
        priority: int = 0,
        source: str = "production",
    ) -> tuple[dict[str, Any], bool]:
        normalized = normalize_job_input(kind, input_data)
        key = _normalize_idempotency_key(idempotency_key)
        priority = max(-100, min(int(priority), 100))
        if source not in {"production", "endurance", "captcha_validation"}:
            raise ValueError(
                "job source must be production, endurance, or captcha_validation"
            )
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                existing = db.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing:
                    if existing["kind"] != kind or existing["input_json"] != stable_json(normalized):
                        raise IdempotencyConflictError(
                            "The idempotency key is already associated with a different request."
                        )
                    return self._job_payload(db, existing), False

            job_id = f"job-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(5)}"
            db.execute(
                """
                INSERT INTO jobs(
                    job_id, kind, input_json, idempotency_key, status, source,
                    priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (job_id, kind, stable_json(normalized), key, source, priority, now, now),
            )
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            self._add_event_db(
                db, job_id, "job_enqueued", {"kind": kind, "priority": priority, "source": source}
            )
            return self._job_payload(db, row), True

    def get_job(self, job_id: str, *, include_input: bool = False) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return self._job_payload(db, row, include_input=include_input)

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE job_id NOT IN (SELECT job_id FROM archived_jobs) ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._job_payload(db, row) for row in rows]

    def successful_fna_examples(self, *, limit: int = 8) -> list[dict[str, int]]:
        """Return recent successful document coordinates for the local UI only."""
        limit = max(1, min(int(limit), 20))
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT input_json, COUNT(*) AS success_count FROM jobs
                WHERE kind = 'fna'
                  AND status IN ('completed', 'partial')
                  AND completed_items > 0
                GROUP BY input_json
                ORDER BY MAX(finished_at) DESC, MAX(rowid) DESC
                LIMIT 200
                """
            ).fetchall()
        examples_by_coordinates: dict[tuple[int, int, int], dict[str, int]] = {}
        for row in rows:
            try:
                request = json.loads(str(row["input_json"]))
                example = {
                    "foja": int(request["foja"]),
                    "numero": int(request["numero"]),
                    "year": int(request["year"]),
                    "success_count": int(row["success_count"]),
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            key = (example["foja"], example["numero"], example["year"])
            prior = examples_by_coordinates.get(key)
            if prior:
                prior["success_count"] += example["success_count"]
            else:
                examples_by_coordinates[key] = example
        return list(examples_by_coordinates.values())[:limit]

    def claim_next(self, owner: str, *, lease_seconds: int = JOB_LEASE_SECONDS) -> Job | None:
        now = utc_now()
        expires = _utc_after(lease_seconds)
        placeholders = ",".join("?" for _ in CLAIMABLE_JOB_STATES)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"""
                SELECT * FROM jobs
                WHERE status IN ({placeholders})
                  AND cancel_requested = 0
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY priority DESC, created_at, rowid
                LIMIT 1
                """,
                (*CLAIMABLE_JOB_STATES, now),
            ).fetchone()
            if not row:
                return None
            changed = db.execute(
                f"""
                UPDATE jobs
                SET status = 'running', worker_owner = ?, lease_expires_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?,
                    error_code = NULL, error_message = NULL
                WHERE job_id = ? AND status IN ({placeholders})
                """,
                (owner, expires, now, now, row["job_id"], *CLAIMABLE_JOB_STATES),
            ).rowcount
            if changed != 1:
                return None
            claimed = db.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            self._add_event_db(db, str(row["job_id"]), "job_claimed", {})
            return _row_to_job(claimed)

    def heartbeat_job(self, job_id: str, owner: str, *, lease_seconds: int = JOB_LEASE_SECONDS) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND worker_owner = ? AND status = 'running'
                """,
                (_utc_after(lease_seconds), utc_now(), job_id, owner),
            )

    def recover_abandoned_jobs(self) -> int:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT job_id FROM jobs
                WHERE status = 'running'
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                  AND NOT EXISTS (SELECT 1 FROM leases
                    WHERE lease_name = 'browser_operation:' || jobs.job_id
                    AND expires_at >= ?)
                """,
                (now, now),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            for job_id in job_ids:
                attempts = db.execute(
                    """
                    SELECT attempt_id, account_id, quota_date, quota_consumed
                    FROM job_attempts
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (job_id,),
                ).fetchall()
                for attempt in attempts:
                    db.execute(
                        """
                        UPDATE job_attempts
                        SET status = 'worker_recovered', quota_consumed = 0,
                            safety_stop = CASE WHEN quota_consumed = 1
                                THEN 'search_outcome_unknown' ELSE safety_stop END,
                            error_message = 'Released after an expired worker lease.',
                            finished_at = ?
                        WHERE attempt_id = ?
                        """,
                        (now, attempt["attempt_id"]),
                    )
                    if int(attempt["quota_consumed"] or 0):
                        self._sync_account_daily_usage_db(
                            db,
                            str(attempt["account_id"]),
                            str(attempt["quota_date"]),
                        )
                        self._add_event_db(
                            db,
                            job_id,
                            "attempt_quota_released",
                            {"status": "worker_recovered", "reason": "expired_worker_lease"},
                            account_id=str(attempt["account_id"]),
                        )
                db.execute(
                    """
                    UPDATE jobs SET status = 'queued', worker_owner = NULL,
                        lease_expires_at = NULL, current_account_id = NULL,
                        updated_at = ?, error_code = 'worker_recovered',
                        error_message = 'Recovered after an expired worker lease.'
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
                db.execute(
                    """
                    UPDATE job_items SET status = 'pending', updated_at = ?
                    WHERE job_id = ? AND status = 'downloading'
                    """,
                    (now, job_id),
                )
                self._add_event_db(db, job_id, "job_recovered", {}, level="warning")
            return len(job_ids)

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] in TERMINAL_JOB_STATES:
                return self._job_payload(db, row)
            if row["status"] == "running":
                db.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
            else:
                db.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
                        finished_at = ?, updated_at = ?, worker_owner = NULL,
                        lease_expires_at = NULL,
                        completed_items = (
                            SELECT COUNT(*) FROM job_items
                            WHERE job_items.job_id = jobs.job_id AND status = 'completed'
                        ),
                        failed_items = (
                            SELECT COUNT(*) FROM job_items
                            WHERE job_items.job_id = jobs.job_id AND status = 'failed'
                        )
                    WHERE job_id = ?
                    """,
                    (now, now, job_id),
                )
            self._add_event_db(db, job_id, "job_cancel_requested", {})
            updated = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._job_payload(db, updated)

    def cancel_requested(self, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def search_checkpoint(self, job_id: str) -> dict[str, Any]:
        """A saved empty result list is also a final search, never a retry signal."""
        with self.connect() as db:
            row = db.execute(
                "SELECT result_count, current_account_id FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            receipt = db.execute(
                "SELECT account_id FROM job_attempts WHERE job_id = ? "
                "AND status = 'search_completed' ORDER BY started_at LIMIT 1", (job_id,)
            ).fetchone()
            uncertain = db.execute(
                "SELECT 1 FROM job_attempts WHERE job_id = ? AND safety_stop = 'search_outcome_unknown' "
                "AND NOT EXISTS (SELECT 1 FROM search_retry_clearance WHERE job_id=job_attempts.job_id)",
                (job_id,),
            ).fetchone()
            return {
                "saved": row["result_count"] is not None,
                "result_count": row["result_count"],
                "account_id": receipt["account_id"] if receipt else row["current_account_id"],
                "incomplete_receipt": receipt is not None and row["result_count"] is None,
                "uncertain": uncertain is not None and row["result_count"] is None,
            }

    def authorize_alternate_search(self, job_id: str) -> bool:
        """Allow a different account only after the previous owner is finished.

        Retain uncertainty and its quota reservation. Never clear receipts or
        authorize the same account again; begin_attempt enforces that boundary.
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT result_count,cancel_requested FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not row or row['result_count'] is not None or row['cancel_requested']:
                return False
            if db.execute("SELECT 1 FROM job_attempts WHERE job_id=? AND (status='search_completed' OR (status='running' AND quota_consumed=1))", (job_id,)).fetchone():
                return False
            if db.execute('SELECT 1 FROM leases WHERE lease_name=? AND expires_at>=?',
                          ('browser_operation:'+job_id, utc_now())).fetchone():
                return False
            # Older versions released these ambiguous reservations. Restore the
            # conservative hold when admitting their explicitly requested retry.
            db.execute("UPDATE job_attempts SET quota_consumed=1 WHERE job_id=? AND safety_stop='search_outcome_unknown'", (job_id,))
            changed = db.execute('INSERT OR IGNORE INTO search_retry_clearance VALUES (?,?)', (job_id,utc_now())).rowcount
            if changed:
                self._add_event_db(db, job_id, 'alternate_search_authorized',
                    {'same_account_retry': False, 'uncertainty_retained': True})
            return True

    def add_results(
        self, job_id: str, results: list[dict[str, Any]], *, attempt_id: str | None = None,
        materialize_items: bool = True,
    ) -> list[dict[str, Any]]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT result_count FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(job_id)
            if existing["result_count"] is None:
                attempt = None
                if attempt_id is not None:
                    attempt = db.execute(
                        "SELECT * FROM job_attempts WHERE attempt_id = ? AND job_id = ? "
                        "AND status = 'running' AND quota_consumed = 1", (attempt_id, job_id)
                    ).fetchone()
                    if attempt is None:
                        raise ValueError("Search receipt requires its active reserved attempt")
                for sequence, result in enumerate(results if materialize_items else [], 1):
                    ticket = result.get("ticket")
                    public_result = {key: value for key, value in result.items() if key != "ticket"}
                    db.execute(
                        """
                        INSERT INTO job_items(
                            item_id, job_id, sequence, ticket_ref, result_json,
                            status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            f"{job_id}-item-{sequence:04d}",
                            job_id,
                            sequence,
                            str(ticket) if ticket else None,
                            stable_json(public_result),
                            now,
                            now,
                        ),
                    )
                db.execute(
                    "UPDATE jobs SET result_count = ?, updated_at = ? WHERE job_id = ?",
                    (len(results), now, job_id),
                )
                self._add_event_db(
                    db, job_id, "search_results_saved", {"result_count": len(results)}
                )
                if attempt is not None:
                    # Results, successful-search receipt and quota are one commit.
                    db.execute(
                        "UPDATE job_attempts SET status = 'search_completed', finished_at = ? "
                        "WHERE attempt_id = ?", (now, attempt_id)
                    )
                    self._sync_account_daily_usage_db(
                        db, str(attempt["account_id"]), str(attempt["quota_date"])
                    )
            return self._items_db(db, job_id, public=False)

    def items(self, job_id: str, *, public: bool = True) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._items_db(db, job_id, public=public)

    def mark_item_downloading(self, item_id: str, output_path: Path) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                UPDATE job_items SET status = 'downloading', output_path = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?,
                    error_code = NULL, error_message = NULL
                WHERE item_id = ? AND status != 'completed'
                """,
                (str(output_path), now, now, item_id),
            )

    def set_item_expected_pages(self, item_id: str, expected_pages: int) -> None:
        if expected_pages <= 0:
            raise ValueError("expected_pages must be greater than zero")
        with self.connect() as db:
            db.execute(
                "UPDATE job_items SET expected_pages = ?, updated_at = ? WHERE item_id = ?",
                (expected_pages, utc_now(), item_id),
            )

    def complete_item(
        self,
        item_id: str,
        *,
        expected_pages: int,
        output_path: Path,
        sha256: str,
        bytes_count: int,
    ) -> dict[str, Any]:
        now = utc_now()
        artifact_id = f"artifact-{secrets.token_hex(8)}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute(
                "SELECT job_id FROM job_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if not item:
                raise KeyError(item_id)
            db.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, job_id, item_id, path, content_type,
                    sha256, bytes, page_count, valid, created_at
                ) VALUES (?, ?, ?, ?, 'application/pdf', ?, ?, ?, 1, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    path = excluded.path, sha256 = excluded.sha256,
                    bytes = excluded.bytes, page_count = excluded.page_count,
                    valid = 1, created_at = excluded.created_at
                """,
                (
                    artifact_id,
                    item["job_id"],
                    item_id,
                    str(output_path),
                    sha256,
                    bytes_count,
                    expected_pages,
                    now,
                ),
            )
            db.execute(
                """
                UPDATE job_items SET status = 'completed', expected_pages = ?,
                    output_path = ?, error_code = NULL, error_message = NULL,
                    finished_at = ?, updated_at = ? WHERE item_id = ?
                """,
                (expected_pages, str(output_path), now, now, item_id),
            )
            row = db.execute(
                "SELECT * FROM artifacts WHERE item_id = ?", (item_id,)
            ).fetchone()
            self._add_event_db(
                db,
                str(item["job_id"]),
                "artifact_created",
                {"item_id": item_id, "bytes": bytes_count, "page_count": expected_pages},
            )
            return _public_artifact(row)

    def fail_item(self, item_id: str, *, code: str, message: str) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                UPDATE job_items SET status = 'failed', error_code = ?,
                    error_message = ?, finished_at = ?, updated_at = ?
                WHERE item_id = ? AND status != 'completed'
                """,
                (code, redact_text(message)[:1000], now, now, item_id),
            )

    def artifacts(self, *, job_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        where = "WHERE job_id = ?" if job_id else ""
        params: tuple[Any, ...] = (job_id, limit) if job_id else (limit,)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM artifacts {where} ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
            return [_public_artifact(row) for row in rows]

    def artifact_record(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return dict(row) if row else None

    def begin_attempt(
        self,
        *,
        job_id: str,
        account_id: str,
        quota_date: str,
        quota: int,
        run_id: str,
        consume_quota: bool,
    ) -> str | None:
        now = utc_now()
        attempt_id = f"attempt-{secrets.token_hex(8)}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute(
                "SELECT status FROM accounts WHERE run_id = ? AND account_id = ?",
                (run_id, account_id),
            ).fetchone()
            if not account or account["status"] != "available":
                return None
            if consume_quota:
                checkpoint = db.execute(
                    "SELECT result_count FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                prior_search = db.execute(
                    "SELECT 1 FROM job_attempts WHERE job_id = ? AND "
                    "(status = 'search_completed' OR (safety_stop = 'search_outcome_unknown' "
                    "AND (account_id = ? OR NOT EXISTS (SELECT 1 FROM search_retry_clearance WHERE job_id=?))) "
                    "OR (status = 'running' AND quota_consumed = 1))",
                    (job_id, account_id, job_id),
                ).fetchone()
                if db.execute('SELECT 1 FROM leases WHERE lease_name=? AND expires_at>=?',
                              ('browser_operation:'+job_id,utc_now())).fetchone():
                    return None
                if checkpoint is None or checkpoint["result_count"] is not None or prior_search:
                    return None
                usage = self._account_usage_db(db, account_id, quota_date)
                if usage >= quota:
                    return None
            db.execute(
                """
                INSERT INTO job_attempts(
                    attempt_id, job_id, account_id, quota_date,
                    quota_consumed, status, started_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (attempt_id, job_id, account_id, quota_date, 1 if consume_quota else 0, now),
            )
            db.execute(
                "UPDATE jobs SET current_account_id = ?, updated_at = ? WHERE job_id = ?",
                (account_id, now, job_id),
            )
            self._add_event_db(
                db,
                job_id,
                "attempt_started",
                {"quota_reserved": consume_quota},
                account_id=account_id,
            )
            return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        safety_stop: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute(
                """
                SELECT job_id, account_id, quota_date, quota_consumed
                FROM job_attempts WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            release_quota = bool(
                attempt
                and int(attempt["quota_consumed"] or 0)
                and status not in QUOTA_SUCCESS_ATTEMPT_STATUSES
                and safety_stop != 'search_outcome_unknown'
            )
            if attempt and (release_quota or safety_stop == 'search_outcome_unknown') and db.execute(
                "SELECT 1 FROM leases WHERE lease_name=? AND expires_at>=?",
                ("browser_operation:" + attempt["job_id"], utc_now()),
            ).fetchone():
                # A disconnected/timed-out worker cannot cancel an in-flight
                # portal operation owned by another process. Keep its reserved
                # slot until the owner commits acceptance or finishes failure.
                return
            db.execute(
                """
                UPDATE job_attempts SET status = ?, safety_stop = ?,
                    error_message = ?, finished_at = ?,
                    quota_consumed = CASE WHEN ? THEN 0 ELSE quota_consumed END
                WHERE attempt_id = ?
                """,
                (
                    status,
                    safety_stop,
                    redact_text(error)[:1000] if error else None,
                    utc_now(),
                    1 if release_quota else 0,
                    attempt_id,
                ),
            )
            if attempt and int(attempt["quota_consumed"] or 0):
                self._sync_account_daily_usage_db(
                    db,
                    str(attempt["account_id"]),
                    str(attempt["quota_date"]),
                )
            if release_quota and attempt:
                self._add_event_db(
                    db,
                    str(attempt["job_id"]),
                    "attempt_quota_released",
                    {"status": status, "reason": safety_stop or status},
                    account_id=str(attempt["account_id"]),
                )

    @staticmethod
    def _sync_account_daily_usage_db(
        db: sqlite3.Connection,
        account_id: str,
        quota_date: str,
    ) -> None:
        success_placeholders = ", ".join(
            "?" for _ in QUOTA_SUCCESS_ATTEMPT_STATUSES
        )
        success_values = tuple(sorted(QUOTA_SUCCESS_ATTEMPT_STATUSES))
        usage = db.execute(
            f"""
            SELECT COUNT(*) AS used, MAX(started_at) AS last_used_at
            FROM job_attempts
            WHERE account_id = ? AND quota_date = ? AND quota_consumed = 1
              AND status IN ({success_placeholders})
            """,
            (account_id, quota_date, *success_values),
        ).fetchone()
        db.execute(
            """
            INSERT INTO account_daily_usage(account_id, quota_date, used, last_used_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, quota_date) DO UPDATE SET
                used = excluded.used,
                last_used_at = excluded.last_used_at
            """,
            (
                account_id,
                quota_date,
                int(usage["used"] or 0),
                usage["last_used_at"],
            ),
        )

    def reconcile_quota_usage(self, *, quota_date: str | None = None) -> dict[str, int]:
        """Release legacy failed reservations and rebuild the account ledger."""

        date_filter = "AND quota_date = ?" if quota_date else ""
        params: tuple[str, ...] = (quota_date,) if quota_date else ()
        success_placeholders = ", ".join("?" for _ in QUOTA_SUCCESS_ATTEMPT_STATUSES)
        success_values = tuple(sorted(QUOTA_SUCCESS_ATTEMPT_STATUSES))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            released = db.execute(
                f"""
                UPDATE job_attempts
                SET quota_consumed = 0
                WHERE quota_consumed = 1
                  AND status NOT IN ({success_placeholders})
                  {date_filter}
                """,
                (*success_values, *params),
            ).rowcount
            pairs = db.execute(
                f"""
                SELECT account_id, quota_date FROM account_daily_usage
                WHERE 1 = 1 {date_filter}
                UNION
                SELECT account_id, quota_date FROM job_attempts
                WHERE 1 = 1 {date_filter}
                """,
                (*params, *params),
            ).fetchall()
            for row in pairs:
                self._sync_account_daily_usage_db(
                    db,
                    str(row["account_id"]),
                    str(row["quota_date"]),
                )
        return {"released_attempts": int(released), "accounts_updated": len(pairs)}

    def select_account(
        self,
        *,
        run_id: str,
        config: PoolConfig,
        quota_date: str,
        excluded: set[str] | None = None,
        preferred_account_id: str | None = None,
        quota_by_account: Mapping[str, int] | None = None,
        source: str = "production",
        source_quota_by_account: Mapping[str, int] | None = None,
        require_search_capacity: bool = True,
    ) -> PoolAccount | None:
        excluded = excluded or set()
        with self.connect() as db:
            rows = db.execute(
                "SELECT account_id, status FROM accounts WHERE run_id = ?", (run_id,)
            ).fetchall()
            statuses = {str(row["account_id"]): str(row["status"]) for row in rows}
            candidates: dict[str, PoolAccount] = {}
            for index, account in enumerate(config.accounts):
                if (
                    not account.enabled
                    or account.account_id in excluded
                    or statuses.get(account.account_id) != "available"
                ):
                    continue
                used = self._account_usage_db(db, account.account_id, quota_date)
                quota = (
                    int(quota_by_account[account.account_id])
                    if quota_by_account and account.account_id in quota_by_account
                    else config.quota_for(account)
                )
                if require_search_capacity and used >= quota:
                    continue
                if require_search_capacity and source_quota_by_account and account.account_id in source_quota_by_account:
                    from .form_search import account_window_db
                    source_used = account_window_db(db,account.account_id)['source_used'].get(source,0)
                    if source_used >= int(source_quota_by_account[account.account_id]):
                        continue
                candidates[account.account_id] = account
            if not candidates:
                return None
            if preferred_account_id:
                preferred = candidates.get(preferred_account_id)
                if preferred is not None:
                    return preferred
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_index FROM account_rotation WHERE name = 'jobs'"
            ).fetchone()
            start = int(row["next_index"]) if row else 0
            for offset in range(len(config.accounts)):
                index = (start + offset) % len(config.accounts)
                account = config.accounts[index]
                if account.account_id not in candidates:
                    continue
                db.execute(
                    """
                    INSERT INTO account_rotation(name, next_index, updated_at)
                    VALUES ('jobs', ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        next_index = excluded.next_index,
                        updated_at = excluded.updated_at
                    """,
                    ((index + 1) % len(config.accounts), utc_now()),
                )
                return account
            return None

    def usage_by_account(self, quota_date: str) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT account_id, used FROM account_daily_usage WHERE quota_date = ?",
                (quota_date,),
            ).fetchall()
            result = {str(row["account_id"]): int(row["used"]) for row in rows}
            cycle_rows = db.execute(
                """
                SELECT c.account_id, COUNT(*) AS used
                FROM cycles c JOIN runs r ON r.run_id = c.run_id
                WHERE c.quota_date = ? AND r.dry_run = 0
                GROUP BY c.account_id
                """,
                (quota_date,),
            ).fetchall()
            for row in cycle_rows:
                result[str(row["account_id"])] = result.get(str(row["account_id"]), 0) + int(
                    row["used"]
                )
            return result

    def outstanding_job_count(self, *, source: str = "production") -> int:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS count FROM jobs
                WHERE source = ?
                  AND status IN ('queued','running','waiting_capacity','waiting_captcha')
                """,
                (source,),
            ).fetchone()
        return int(row["count"] or 0)

    def set_account_check(
        self,
        account_id: str,
        *,
        session_checked: bool = False,
        proxy_status: str | None = None,
        egress_hash: str | None = None,
    ) -> None:
        today = local_today()
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO account_checks(
                    account_id, session_checked_date, proxy_checked_date, proxy_checked_at,
                    proxy_status, egress_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    session_checked_date = COALESCE(excluded.session_checked_date, session_checked_date),
                    proxy_checked_date = COALESCE(excluded.proxy_checked_date, proxy_checked_date),
                    proxy_checked_at = COALESCE(excluded.proxy_checked_at, proxy_checked_at),
                    proxy_status = COALESCE(excluded.proxy_status, proxy_status),
                    egress_hash = COALESCE(excluded.egress_hash, egress_hash),
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    today if session_checked else None,
                    today if proxy_status is not None else None,
                    now if proxy_status is not None else None,
                    proxy_status,
                    egress_hash,
                    now,
                ),
            )

    def egress_owner(self, egress_hash: str, *, exclude_account: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT account_id FROM account_checks
                WHERE egress_hash = ? AND account_id != ? AND proxy_status = 'passed'
                LIMIT 1
                """,
                (egress_hash, exclude_account),
            ).fetchone()
            return str(row["account_id"]) if row else None

    def account_check(self, account_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM account_checks WHERE account_id = ?", (account_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_waiting(self, job_id: str, status: str, *, reason: str) -> None:
        if status not in {"queued", "waiting_capacity", "waiting_captcha"}:
            raise ValueError(f"invalid waiting status: {status}")
        next_run_at = (
            _utc_after(60)
            if status == "waiting_capacity"
            else _utc_after(30) if status == "waiting_captcha" else None
        )
        with self.connect() as db:
            db.execute(
                """
                UPDATE jobs SET status = ?, next_run_at = ?, error_code = ?,
                    error_message = ?, current_account_id = NULL,
                    worker_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status NOT IN ('completed','partial','failed','cancelled')
                """,
                (status, next_run_at, reason, redact_text(reason), utc_now(), job_id),
            )
            self._add_event_db(db, job_id, f"job_{status}", {"reason": reason}, level="warning")

    def release_waiting_captcha(self) -> int:
        """Make CAPTCHA-blocked jobs immediately claimable after manual authorization."""
        with self.connect() as db:
            changed = db.execute(
                """
                UPDATE jobs SET status = 'queued', next_run_at = NULL,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE status = 'waiting_captcha'
                """,
                (utc_now(),),
            ).rowcount
        return int(changed)

    def set_next_account(self, account_id: str, config: PoolConfig) -> None:
        index = next(
            (
                index
                for index, account in enumerate(config.accounts)
                if account.account_id == account_id
            ),
            None,
        )
        if index is None:
            raise ValueError(f"Unknown pool account: {account_id}")
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO account_rotation(name, next_index, updated_at)
                VALUES ('jobs', ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    next_index = excluded.next_index,
                    updated_at = excluded.updated_at
                """,
                (index, utc_now()),
            )

    def set_account_browser_state(
        self,
        account_id: str,
        *,
        live: bool,
        authenticated: bool,
        headless: bool,
        owner: str,
        engine: str = "native_chrome",
        status: str,
        auth_state: str | None = None,
        auth_error: str | None = None,
        auth_http_status: int | None = None,
    ) -> None:
        browser_owner = self.active_lease("browser_owner")
        if browser_owner and browser_owner["owner"] != owner:
            return  # independent owner is authoritative, even while worker stops
        now = utc_now()
        normalized_auth_state = str(auth_state or CommerceAuthState.UNKNOWN.value)
        authenticated = bool(
            live
            and authenticated
            and normalized_auth_state == CommerceAuthState.AUTHENTICATED_FORM.value
        )
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO account_checks(
                    account_id, browser_live, browser_authenticated, browser_headless,
                    browser_owner, browser_engine, browser_status, browser_started_at,
                    browser_checked_at, browser_auth_state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    browser_started_at = CASE
                        WHEN excluded.browser_live = 1 AND account_checks.browser_live = 0
                        THEN excluded.browser_started_at
                        ELSE account_checks.browser_started_at
                    END,
                    browser_live = excluded.browser_live,
                    browser_authenticated = excluded.browser_authenticated,
                    browser_headless = excluded.browser_headless,
                    browser_owner = excluded.browser_owner,
                    browser_engine = excluded.browser_engine,
                    browser_status = excluded.browser_status,
                    browser_auth_state = excluded.browser_auth_state,
                    browser_checked_at = excluded.browser_checked_at,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    int(live),
                    int(authenticated),
                    int(headless),
                    owner,
                    str(engine or "unknown").strip().lower(),
                    status,
                    now if live else None,
                    now,
                    normalized_auth_state,
                    now,
                ),
            )
            if authenticated:
                db.execute(
                    "UPDATE account_checks SET browser_authenticated_at = ?, "
                    "browser_last_auth_error = NULL, browser_last_auth_http_status = NULL "
                    "WHERE account_id = ?", (now, account_id),
                )
            elif auth_error:
                db.execute(
                    "UPDATE account_checks SET browser_last_auth_error = ?, "
                    "browser_last_auth_http_status = ? WHERE account_id = ?",
                    (auth_error, auth_http_status, account_id),
                )

    def finalize_job(self, job_id: str) -> str:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not job:
                raise KeyError(job_id)
            counts = {
                str(row["status"]): int(row["count"])
                for row in db.execute(
                    "SELECT status, COUNT(*) AS count FROM job_items WHERE job_id = ? GROUP BY status",
                    (job_id,),
                ).fetchall()
            }
            total = sum(counts.values())
            completed = counts.get("completed", 0)
            failed = counts.get("failed", 0)
            if bool(job["cancel_requested"]):
                status = "cancelled"
            else:
                if total == 0:
                    status = "completed"
                elif completed == total:
                    status = "completed"
                elif completed > 0 and completed + failed == total:
                    status = "partial"
                elif failed == total:
                    status = "failed"
                else:
                    return "running"
            db.execute(
                """
                UPDATE jobs SET completed_items = ?, failed_items = ?
                WHERE job_id = ?
                """,
                (completed, failed, job_id),
            )
            db.execute(
                """
                UPDATE jobs SET status = ?, finished_at = ?, updated_at = ?,
                    current_account_id = NULL, worker_owner = NULL,
                    lease_expires_at = NULL, next_run_at = NULL
                WHERE job_id = ?
                """,
                (status, now, now, job_id),
            )
            self._add_event_db(db, job_id, f"job_{status}", {})
            return status

    def fail_job(self, job_id: str, *, code: str, message: str) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                UPDATE jobs SET status = 'failed', error_code = ?, error_message = ?,
                    finished_at = ?, updated_at = ?, current_account_id = NULL,
                    worker_owner = NULL, lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (code, redact_text(message)[:1000], now, now, job_id),
            )
            self._add_event_db(db, job_id, "job_failed", {"code": code}, level="error")

    def add_event(
        self,
        event: str,
        *,
        job_id: str | None = None,
        account_id: str | None = None,
        level: str = "info",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        with self.connect() as db:
            self._add_event_db(
                db,
                job_id,
                event,
                dict(data or {}),
                account_id=account_id,
                level=level,
            )

    def recent_events(self, *, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE job_id = ?" if job_id else ""
        params: tuple[Any, ...] = (job_id, limit) if job_id else (limit,)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM job_events {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def acquire_lease(
        self,
        lease_name: str,
        owner: str,
        *,
        stale_seconds: int = WORKER_STALE_SECONDS,
    ) -> bool:
        now = utc_now()
        expires = _utc_after(stale_seconds)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM leases WHERE lease_name = ?", (lease_name,)
            ).fetchone()
            if row and row["owner"] != owner and str(row["expires_at"]) >= now:
                return False
            db.execute(
                """
                INSERT INTO leases(lease_name, owner, acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner = excluded.owner, acquired_at = excluded.acquired_at,
                    heartbeat_at = excluded.heartbeat_at, expires_at = excluded.expires_at
                """,
                (lease_name, owner, now, now, expires),
            )
            return True

    def heartbeat_lease(
        self, lease_name: str, owner: str, *, stale_seconds: int = WORKER_STALE_SECONDS
    ) -> bool:
        now = utc_now()
        with self.connect() as db:
            changed = db.execute(
                """
                UPDATE leases SET heartbeat_at = ?, expires_at = ?
                WHERE lease_name = ? AND owner = ?
                """,
                (now, _utc_after(stale_seconds), lease_name, owner),
            ).rowcount
            return changed == 1

    def release_lease(self, lease_name: str, owner: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM leases WHERE lease_name = ? AND owner = ?", (lease_name, owner)
            )

    def lease(self, lease_name: str = WORKER_LEASE_NAME) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM leases WHERE lease_name = ?", (lease_name,)
            ).fetchone()
            return dict(row) if row else None

    def ensure_dataimpulse_route(
        self, account_id: str, initial_port: int
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO account_proxy_routes(account_id, active_port, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (account_id, int(initial_port), now),
            )
            row = db.execute(
                "SELECT * FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            assert row is not None
            return dict(row)

    def dataimpulse_route(self, account_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            return dict(row) if row else None

    def dataimpulse_routes(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM account_proxy_routes ORDER BY account_id"
                ).fetchall()
            ]

    def begin_dataimpulse_rotation(
        self,
        account_id: str,
        *,
        initial_port: int,
        reason: str,
        port_min: int,
        port_max: int,
        cooldown_seconds: float,
        max_rotations_per_hour: int,
        randomize: bool = False,
    ) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.replace(microsecond=0).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO account_proxy_routes(account_id, active_port, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (account_id, int(initial_port), now),
            )
            row = db.execute(
                "SELECT * FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            assert row is not None
            state = dict(row)
            cooldown_until = str(state.get("cooldown_until") or "")
            if cooldown_until and cooldown_until > now:
                return {
                    **state,
                    "ok": False,
                    "reason": "rotation_cooldown",
                    "next_eligible_at": cooldown_until,
                }
            window_started = str(state.get("rotation_window_started_at") or "")
            if not window_started or seconds_since(window_started) >= ROTATION_WINDOW_SECONDS:
                window_started = now
                rotation_count = 0
            else:
                rotation_count = int(state.get("rotation_count") or 0)
            if rotation_count >= max_rotations_per_hour:
                # The allowance resets at the window boundary, not one full
                # hour after whichever attempt happened to notice exhaustion.
                next_eligible = (
                    datetime.fromisoformat(window_started.replace("Z", "+00:00"))
                    + timedelta(seconds=ROTATION_WINDOW_SECONDS)
                ).replace(microsecond=0).isoformat()
                db.execute(
                    """
                    UPDATE account_proxy_routes
                    SET status = ?, cooldown_until = ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (RETRY_ALLOWANCE_EXHAUSTED, next_eligible, now, account_id),
                )
                return {
                    **state,
                    "ok": False,
                    "reason": RETRY_ALLOWANCE_EXHAUSTED,
                    "cooldown_until": next_eligible,
                    "next_eligible_at": next_eligible,
                    "rotation_count": rotation_count,
                    "rotation_window_started_at": window_started,
                }
            used_ports = {
                int(value)
                for used in db.execute(
                    "SELECT active_port, pending_port FROM account_proxy_routes"
                ).fetchall()
                for value in (used["active_port"], used["pending_port"])
                if value is not None
            }
            try:
                rejected_ports = {
                    int(value)
                    for value in json.loads(
                        str(state.get("rejected_ports_json") or "[]")
                    )
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                rejected_ports = set()
            used_ports.update(
                port for port in rejected_ports if port_min <= port <= port_max
            )
            if randomize:
                available = [
                    port for port in range(port_min, port_max + 1) if port not in used_ports
                ]
                if not available:
                    raise ValueError("No unused sticky ports available")
                candidate = secrets.choice(available)
            else:
                candidate = next_unused_sticky_port(
                    int(state["active_port"]), used_ports=used_ports,
                    minimum=port_min, maximum=port_max,
                )
            # ``cooldown_seconds`` here is only the minimum spacing before the
            # next candidate. A promotion applies its own, longer cooldown in
            # finish_dataimpulse_rotation; a failed candidate never waits for it.
            cooldown = (now_dt + timedelta(seconds=cooldown_seconds)).replace(
                microsecond=0
            ).isoformat()
            db.execute(
                """
                UPDATE account_proxy_routes
                SET pending_port = ?, status = 'validating',
                    last_rotation_reason = ?, cooldown_until = ?,
                    rotation_window_started_at = ?, rotation_count = ?,
                    updated_at = ? WHERE account_id = ?
                """,
                (
                    candidate,
                    redact_text(reason),
                    cooldown,
                    window_started,
                    rotation_count + 1,
                    now,
                    account_id,
                ),
            )
            return {
                **state,
                "ok": True,
                "reason": "candidate_ready",
                "pending_port": candidate,
                "cooldown_until": cooldown,
                "rotation_count": rotation_count + 1,
                "rotation_window_started_at": window_started,
                "rotation_limit": int(max_rotations_per_hour),
            }

    def finish_dataimpulse_rotation(
        self,
        account_id: str,
        *,
        promoted: bool,
        error_code: str | None = None,
        cooldown_seconds: float | None = None,
        retry_seconds: float | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(account_id)
            if promoted and row["pending_port"] is None:
                raise RuntimeError("DataImpulse rotation has no pending port")
            if promoted:
                cooldown = (
                    (datetime.now(timezone.utc) + timedelta(seconds=float(cooldown_seconds)))
                    .replace(microsecond=0).isoformat()
                    if cooldown_seconds is not None and cooldown_seconds > 0
                    else None
                )
                db.execute(
                    """
                    UPDATE account_proxy_routes
                    SET active_port = pending_port, pending_port = NULL,
                        generation = generation + 1, status = 'active',
                        last_error_code = NULL, last_rotated_at = ?,
                        cooldown_until = COALESCE(?, cooldown_until),
                        temporary_window_started_at = NULL,
                        temporary_failure_count = 0, rejected_ports_json = '[]',
                        updated_at = ?
                    WHERE account_id = ?
                    """,
                    (now, cooldown, now, account_id),
                )
            else:
                try:
                    rejected_ports = [
                        int(value)
                        for value in json.loads(
                            str(row["rejected_ports_json"] or "[]")
                        )
                    ]
                except (TypeError, ValueError, json.JSONDecodeError):
                    rejected_ports = []
                if row["pending_port"] is not None:
                    rejected_ports.append(int(row["pending_port"]))
                rejected_ports = list(dict.fromkeys(rejected_ports))[-100:]
                # A failed candidate only waits the short retry delay (portal
                # rejections) or nothing at all (transport failures).
                retry_until = (
                    (datetime.now(timezone.utc) + timedelta(seconds=float(retry_seconds)))
                    .replace(microsecond=0).isoformat()
                    if retry_seconds is not None and retry_seconds > 0
                    else None
                )
                db.execute(
                    """
                    UPDATE account_proxy_routes
                    SET pending_port = NULL, status = 'candidate_failed',
                        last_error_code = ?, rejected_ports_json = ?,
                        cooldown_until = ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (
                        redact_text(error_code or "candidate_failed"),
                        json.dumps(rejected_ports, separators=(",", ":")),
                        retry_until,
                        now,
                        account_id,
                    ),
                )
            updated = db.execute(
                "SELECT * FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def set_dataimpulse_route_retry(
        self, account_id: str, *, retry_seconds: float
    ) -> dict[str, Any] | None:
        """Stamp the next eligible attempt for a route whose candidate failed."""
        retry_until = (
            datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(retry_seconds)))
        ).replace(microsecond=0).isoformat()
        with self.connect() as db:
            db.execute(
                """
                UPDATE account_proxy_routes
                SET cooldown_until = ?, updated_at = ?
                WHERE account_id = ? AND status = 'candidate_failed'
                """,
                (retry_until, utc_now(), account_id),
            )
            row = db.execute(
                "SELECT * FROM account_proxy_routes WHERE account_id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_dataimpulse_temporary_failure(
        self, account_id: str, *, initial_port: int
    ) -> int:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO account_proxy_routes(account_id, active_port, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (account_id, int(initial_port), now),
            )
            row = db.execute(
                "SELECT temporary_window_started_at, temporary_failure_count "
                "FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            assert row is not None
            started = str(row["temporary_window_started_at"] or "")
            count = int(row["temporary_failure_count"] or 0)
            if not started or seconds_since(started) > 600:
                started = now
                count = 0
            count += 1
            db.execute(
                """
                UPDATE account_proxy_routes
                SET temporary_window_started_at = ?, temporary_failure_count = ?,
                    updated_at = ? WHERE account_id = ?
                """,
                (started, count, now, account_id),
            )
            return count

    def candidate_exit_rejected(self, account_id: str, egress_hash: str) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM proxy_candidate_exits WHERE account_id=? AND egress_hash=? AND failed_at>=?",
                (account_id, egress_hash, cutoff),
            ).fetchone() is not None

    def record_failed_candidate_exit(self, account_id: str, egress_hash: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO proxy_candidate_exits(account_id,egress_hash,failed_at) VALUES (?,?,?) "
                "ON CONFLICT(account_id,egress_hash) DO UPDATE SET failed_at=excluded.failed_at",
                (account_id, egress_hash, utc_now()),
            )

    def record_candidate_attempt(
        self,
        account_id: str,
        *,
        sticky_port: int | None,
        egress_hash: str | None,
        outcome: str,
        http_status: int | None = None,
        response_code: str | None = None,
        reason: str | None = None,
        started_at: str,
    ) -> None:
        """Persist one candidate attempt with its sanitized failure reason.

        ``outcome`` separates portal login rejections from proxy-transport
        failures so the overview can show what actually happened instead of a
        single generic label. Bodies, credentials and raw exits are never stored.
        """
        route_id = (
            f"ip-{hashlib.sha256(str(egress_hash).encode('utf-8')).hexdigest()[:10]}"
            if egress_hash else None
        )
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO proxy_candidate_attempts(
                    account_id, sticky_port, egress_route_id, outcome, http_status,
                    response_code, reason, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    int(sticky_port) if sticky_port is not None else None,
                    route_id,
                    redact_text(outcome)[:80],
                    int(http_status) if http_status is not None else None,
                    redact_text(response_code)[:80] if response_code else None,
                    redact_text(reason)[:160] if reason else None,
                    started_at,
                    utc_now(),
                ),
            )
            db.execute(
                """
                DELETE FROM proxy_candidate_attempts
                WHERE account_id = ? AND id NOT IN (
                    SELECT id FROM proxy_candidate_attempts WHERE account_id = ?
                    ORDER BY id DESC LIMIT 200
                )
                """,
                (account_id, account_id),
            )

    def recent_candidate_attempts(
        self, account_id: str, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT sticky_port, egress_route_id, outcome, http_status,
                       response_code, reason, started_at, finished_at
                FROM proxy_candidate_attempts WHERE account_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (account_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_mobile_login_canary(
        self, *, max_per_hour: int = 6, spacing_seconds: float = 60.0
    ) -> bool:
        """Bounded pool-wide login probes when no proxy account works."""
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM job_control WHERE key = 'mobile_login_canary'").fetchone()
            state = json.loads(row["value"]) if row else {}
            last = state.get("last_attempt")
            if last and seconds_since(last) < float(spacing_seconds):
                return False
            started = state.get("window_started")
            count = int(state.get("count", 0))
            if not started or seconds_since(started) >= ROTATION_WINDOW_SECONDS:
                started, count = now, 0
            if count >= int(max_per_hour):
                return False
            payload = stable_json({"window_started": started, "last_attempt": now, "count": count + 1})
            db.execute(
                "INSERT INTO job_control(key,value,updated_at) VALUES ('mobile_login_canary',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (payload, now),
            )
            return True

    def another_account_succeeded_recently(
        self, account_id: str, *, within_seconds: float = 600,
        allow_authenticated_form: bool = False,
    ) -> bool:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        ).replace(microsecond=0).isoformat()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT 1 FROM job_attempts
                WHERE account_id != ? AND status IN ('search_completed','completed')
                  AND COALESCE(finished_at, started_at) >= ?
                LIMIT 1
                """,
                (account_id, cutoff),
            ).fetchone()
            if row is not None:
                return True
            if not allow_authenticated_form:
                return False
            # Login recovery can use a fresh protected form. Search recovery
            # still requires a successful search/download, since login alone
            # does not establish that the query service is healthy.
            row = db.execute(
                "SELECT 1 FROM account_checks c JOIN leases l "
                "ON c.browser_owner = l.owner "
                "WHERE c.account_id != ? AND c.browser_live = 1 "
                "AND c.browser_authenticated = 1 "
                "AND c.browser_auth_state = 'authenticated_form' "
                "AND c.browser_checked_at >= ? "
                "AND l.lease_name IN (?, 'browser_owner') AND l.expires_at >= ? LIMIT 1",
                (account_id, cutoff, WORKER_LEASE_NAME, utc_now()),
            ).fetchone()
            return row is not None

    def active_lease(self, lease_name: str = WORKER_LEASE_NAME) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM leases WHERE lease_name = ? AND expires_at >= ?",
                (lease_name, utc_now()),
            ).fetchone()
            return dict(row) if row else None

    def clear_expired_lease(self, lease_name: str = WORKER_LEASE_NAME) -> bool:
        """Remove only an expired lease; an active worker is never disturbed."""
        with self.connect() as db:
            changed = db.execute(
                "DELETE FROM leases WHERE lease_name = ? AND expires_at < ?",
                (lease_name, utc_now()),
            ).rowcount
        return bool(changed)

    def set_control(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO job_control(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, redact_text(value), utc_now()),
            )

    def get_control(self, key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT value, updated_at FROM job_control WHERE key = ?", (key,)
            ).fetchone()
            return dict(row) if row else None

    def clear_control(self, key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM job_control WHERE key = ?", (key,))

    def request_dataimpulse_rotation(self, account_id: str, *, reason: str) -> dict[str, Any]:
        """Queue one sanitized rotation request for the active worker owner."""
        request_id = f"rotation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        payload = {
            "request_id": request_id,
            "account_id": str(account_id),
            "reason": redact_text(reason),
            "status": "pending",
            "requested_at": utc_now(),
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT value FROM job_control WHERE key = ?",
                (DATAIMPULSE_ROTATION_REQUEST_KEY,),
            ).fetchone()
            if existing:
                raise RuntimeError("A DataImpulse rotation request is already pending.")
            db.execute(
                "INSERT INTO job_control(key, value, updated_at) VALUES (?, ?, ?)",
                (DATAIMPULSE_ROTATION_REQUEST_KEY, stable_json(payload), utc_now()),
            )
        return payload

    def claim_dataimpulse_rotation_request(self, owner: str) -> dict[str, Any] | None:
        """Claim a pending request, or reclaim one abandoned by a stale worker."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value, updated_at FROM job_control WHERE key = ?",
                (DATAIMPULSE_ROTATION_REQUEST_KEY,),
            ).fetchone()
            if not row:
                return None
            try:
                payload = json.loads(str(row["value"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                db.execute(
                    "DELETE FROM job_control WHERE key = ?",
                    (DATAIMPULSE_ROTATION_REQUEST_KEY,),
                )
                return None
            status = str(payload.get("status") or "pending")
            if status == "running" and seconds_since(str(row["updated_at"])) < WORKER_STALE_SECONDS:
                return None
            if status not in {"pending", "running"}:
                return None
            payload.update({"status": "running", "worker_owner": owner, "started_at": utc_now()})
            db.execute(
                "UPDATE job_control SET value = ?, updated_at = ? WHERE key = ?",
                (stable_json(payload), utc_now(), DATAIMPULSE_ROTATION_REQUEST_KEY),
            )
            return payload

    def finish_dataimpulse_rotation_request(
        self,
        request_id: str,
        *,
        ok: bool,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "request_id": str(request_id),
            "ok": bool(ok),
            "status": "completed" if ok else "failed",
            "completed_at": utc_now(),
            **dict(result),
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO job_control(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (DATAIMPULSE_ROTATION_RESULT_KEY, stable_json(payload), utc_now()),
            )
            row = db.execute(
                "SELECT value FROM job_control WHERE key = ?",
                (DATAIMPULSE_ROTATION_REQUEST_KEY,),
            ).fetchone()
            if row:
                try:
                    current = json.loads(str(row["value"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    current = {}
                if str(current.get("request_id") or "") == str(request_id):
                    db.execute(
                        "DELETE FROM job_control WHERE key = ?",
                        (DATAIMPULSE_ROTATION_REQUEST_KEY,),
                    )
        return payload

    def dataimpulse_rotation_result(self) -> dict[str, Any] | None:
        row = self.get_control(DATAIMPULSE_ROTATION_RESULT_KEY)
        if not row:
            return None
        try:
            return json.loads(str(row["value"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def set_global_cooldown(
        self,
        reason: str,
        seconds: float,
        *,
        max_seconds: float = 300.0,
    ) -> dict[str, str]:
        payload = {
            "reason": redact_text(reason)[:80],
            "resume_at": _utc_after(
                min(max(1.0, float(max_seconds)), max(1.0, float(seconds)))
            ),
        }
        self.set_control("global_safety_cooldown", stable_json(payload))
        return payload

    def advance_external_outage_backoff(self) -> dict[str, object]:
        row = self.get_control(EXTERNAL_OUTAGE_BACKOFF_KEY)
        streak = 0
        if row:
            try:
                streak = max(0, int(json.loads(str(row["value"])).get("streak", 0)))
            except (TypeError, ValueError, json.JSONDecodeError):
                streak = 0
        streak += 1
        seconds = EXTERNAL_OUTAGE_BACKOFF_SECONDS[
            min(streak - 1, len(EXTERNAL_OUTAGE_BACKOFF_SECONDS) - 1)
        ]
        self.set_control(
            EXTERNAL_OUTAGE_BACKOFF_KEY,
            stable_json({"streak": streak, "updated_at": utc_now()}),
        )
        cooldown = self.set_global_cooldown(
            EXTERNAL_OUTAGE_REASON,
            seconds,
            max_seconds=EXTERNAL_OUTAGE_BACKOFF_SECONDS[-1],
        )
        return {**cooldown, "streak": streak, "seconds": seconds}

    def clear_external_outage_backoff(self) -> None:
        self.clear_control(EXTERNAL_OUTAGE_BACKOFF_KEY)

    def global_cooldown(self) -> dict[str, str] | None:
        row = self.get_control("global_safety_cooldown")
        if not row:
            return None
        try:
            payload = json.loads(str(row["value"]))
            reason = str(payload["reason"])
            resume_at = str(payload["resume_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.clear_control("global_safety_cooldown")
            return None
        if resume_at <= utc_now():
            self.clear_control("global_safety_cooldown")
            return None
        return {"reason": reason, "resume_at": resume_at}

    def summary(self) -> dict[str, Any]:
        with self.connect() as db:
            counts = {
                str(row["status"]): int(row["count"])
                for row in db.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                ).fetchall()
            }
            artifact = db.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS bytes FROM artifacts"
            ).fetchone()
            return {
                "counts": counts,
                "queued": sum(counts.get(status, 0) for status in CLAIMABLE_JOB_STATES),
                "running": counts.get("running", 0),
                "artifacts": int(artifact["count"]),
                "artifact_bytes": int(artifact["bytes"]),
                "worker": self.lease(),
                "global_safety_cooldown": self.global_cooldown(),
            }

    def _job_payload(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_input: bool = False,
    ) -> dict[str, Any]:
        account_id = row["current_account_id"]
        if not account_id:
            attempt = db.execute(
                """
                SELECT account_id FROM job_attempts
                WHERE job_id = ?
                ORDER BY
                    CASE WHEN status IN ('search_completed', 'completed') THEN 0 ELSE 1 END,
                    COALESCE(finished_at, started_at) DESC,
                    rowid DESC
                LIMIT 1
                """,
                (row["job_id"],),
            ).fetchone()
            account_id = attempt["account_id"] if attempt else None
        raw_input = json.loads(str(row["input_json"]))
        payload = {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "source": row["source"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "priority": int(row["priority"] or 0),
            "result_count": row["result_count"],
            "completed_items": row["completed_items"],
            "search_status": "completed" if row["result_count"] is not None else "unconfirmed",
            "document_status": (
                "not_started" if row["result_count"] is None else
                "not_required" if row["result_count"] == 0 or (
                    row["source"] == "captcha_validation" and raw_input.get("validation_only")
                ) else
                "completed" if row["completed_items"] == row["result_count"] else
                "failed" if row["failed_items"] else "pending"
            ),
            "failed_items": row["failed_items"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "current_account_id": row["current_account_id"],
            "account_id": account_id,
            "next_run_at": row["next_run_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "attempts": self._attempts_db(db, str(row["job_id"])),
            "items": self._items_db(db, str(row["job_id"]), public=True),
        }
        if include_input:
            payload["input"] = raw_input
        elif row["kind"] == "fna":
            payload["request"] = raw_input
        else:
            payload["request"] = {"text_saved": True}
        return redact(payload)

    @staticmethod
    def _attempts_db(db: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
        rows = db.execute(
            """
            SELECT attempt_id, account_id, status, safety_stop, started_at, finished_at
            FROM job_attempts
            WHERE job_id = ?
            ORDER BY started_at, rowid
            """,
            (job_id,),
        ).fetchall()
        return [
            redact(
                {
                    "account_id": row["account_id"],
                    "status": row["status"],
                    "reason": row["safety_stop"] or row["status"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "error_evidence": [dict(e) for e in db.execute(
                        "SELECT evidence_id, captured_at, capture_status, reason, http_status "
                        "FROM attempt_error_evidence WHERE attempt_id=? ORDER BY captured_at",
                        (row["attempt_id"],)).fetchall()],
                }
            )
            for row in rows
        ]

    @staticmethod
    def _items_db(
        db: sqlite3.Connection, job_id: str, *, public: bool
    ) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT * FROM job_items WHERE job_id = ? ORDER BY sequence", (job_id,)
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(str(item.pop("result_json")))
            if public:
                item.pop("ticket_ref", None)
                item.pop("output_path", None)
            items.append(redact(item) if public else item)
        return items

    @staticmethod
    def _add_event_db(
        db: sqlite3.Connection,
        job_id: str | None,
        event: str,
        data: Mapping[str, Any],
        *,
        account_id: str | None = None,
        level: str = "info",
    ) -> None:
        db.execute(
            """
            INSERT INTO job_events(job_id, account_id, level, event, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                account_id,
                level,
                event,
                json.dumps(redact(dict(data)), ensure_ascii=False),
                utc_now(),
            ),
        )

    @staticmethod
    def _account_usage_db(db: sqlite3.Connection, account_id: str, quota_date: str) -> int:
        from .form_search import account_window_db
        window = account_window_db(db, account_id)
        return window['used'] + window['reserved']


def default_job_store(settings: Settings = SETTINGS) -> JobStore:
    return JobStore(settings.profile_dir.parent / "pool" / "pool.sqlite3")


def normalize_job_input(kind: str, input_data: Mapping[str, Any]) -> dict[str, Any]:
    sample_pages = _normalize_sample_pages(input_data.get("sample_pages"))
    internal = {}
    if input_data.get("validation_only"):
        internal["validation_only"] = True
    target_account_id = str(input_data.get("target_account_id") or "").strip()
    if target_account_id:
        internal["target_account_id"] = target_account_id
    if kind == "text":
        query = str(input_data.get("text") or input_data.get("query") or "").strip()
        if not query:
            raise ValueError("text jobs require a non-empty text value")
        if len(query) > 500:
            raise ValueError("text value must be 500 characters or fewer")
        return {
            "text": query,
            **({"sample_pages": sample_pages} if sample_pages else {}),
            **internal,
        }
    if kind == "fna":
        try:
            foja = int(input_data["foja"])
            numero = int(input_data["numero"])
            year = int(input_data.get("year", input_data.get("ano")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("fna jobs require integer foja, numero, and year values") from exc
        if foja <= 0 or numero <= 0 or year < 1800 or year > 2200:
            raise ValueError("fna values are outside the accepted range")
        return {
            "foja": foja,
            "numero": numero,
            "year": year,
            **({"sample_pages": sample_pages} if sample_pages else {}),
            **internal,
        }
    raise ValueError("job kind must be 'text' or 'fna'")


def _normalize_sample_pages(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        pages = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_pages must be an integer from 1 to 5") from exc
    if pages < 1 or pages > 5:
        raise ValueError("sample_pages must be an integer from 1 to 5")
    return pages


def stable_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_pdf(path: Path, *, expected_pages: int) -> tuple[str, int]:
    if expected_pages <= 0:
        raise ValueError("A PDF artifact must contain at least one expected page.")
    if not path.exists() or not path.is_file():
        raise RuntimeError("PDF artifact was not created.")
    size = path.stat().st_size
    if size <= 5:
        raise RuntimeError("PDF artifact is empty or truncated.")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise RuntimeError("Artifact does not have a valid PDF header.")
    digest = hashlib.sha256()
    actual_pages = 0
    tail = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            combined = tail + chunk
            if len(combined) > 64:
                actual_pages += len(PDF_PAGE_OBJECT_RE.findall(combined[:-64]))
                tail = combined[-64:]
            else:
                tail = combined
    actual_pages += len(PDF_PAGE_OBJECT_RE.findall(tail))
    if b"%%EOF" not in tail:
        raise RuntimeError("PDF artifact does not contain an EOF marker.")
    if actual_pages != expected_pages:
        raise RuntimeError(
            f"PDF artifact page count mismatch: expected {expected_pages}, found {actual_pages}."
        )
    return digest.hexdigest(), size


def download_job_item(
    scraper: Any,
    item: Mapping[str, Any],
    *,
    job_id: str,
    output_root: Path,
    sample_pages: int | None = None,
    on_expected_pages: Callable[[int], None] | None = None,
) -> tuple[Path, int, str, int]:
    ticket = item.get("ticket_ref")
    if not ticket:
        raise RuntimeError("Search result did not include an inscription ticket.")
    sequence = int(item["sequence"])
    result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
    stem = _artifact_stem(sequence, result, sample_pages=sample_pages)
    job_dir = output_root / "jobs" / job_id
    image_dir = job_dir / ".staging" / str(item["item_id"])
    final_path = job_dir / f"{stem}.pdf"
    temp_path = job_dir / f".{stem}.{secrets.token_hex(4)}.tmp"
    job_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    manifest = image_dir / "manifest.json"
    refs = None
    if manifest.exists():
        try:
            cached = json.loads(manifest.read_text(encoding="utf-8"))
            if cached.get("ticket") == str(ticket) and cached.get("sample_pages") == sample_pages:
                refs = cached["refs"]
        except (ValueError, KeyError):
            pass
    if refs is None:
        _ticket_info, refs = scraper.get_image_refs(str(ticket))
        if sample_pages is not None:
            refs = refs[:sample_pages]
        manifest_tmp = manifest.with_suffix('.tmp')
        manifest_tmp.write_text(json.dumps({'ticket': str(ticket), 'sample_pages': sample_pages, 'refs': refs}), encoding='utf-8')
        os.replace(manifest_tmp, manifest)
    if not refs:
        raise RuntimeError("No image references returned for this inscription.")
    if sample_pages is not None:
        refs = refs[:sample_pages]
    if on_expected_pages:
        on_expected_pages(len(refs))
    images: list[Path] = []
    try:
        for ref in refs:
            page = int(ref["pageNumber"])
            data_ref = str(ref["dataRef"])
            image_path = image_dir / f"page_{page:05d}.jpg"
            from PIL import Image
            valid = False
            if image_path.exists():
                try:
                    with Image.open(image_path) as image:
                        image.verify()
                    valid = True
                except (OSError, ValueError):
                    pass
            if not valid:
                pending = image_path.with_suffix('.download.jpg')
                scraper.download_image(data_ref, pending)
                with Image.open(pending) as image:
                    image.verify()
                os.replace(pending, image_path)
            images.append(image_path)
        create_pdf(images, temp_path)
        sha256, size = validate_pdf(temp_path, expected_pages=len(refs))
        os.replace(temp_path, final_path)
        return final_path, len(refs), sha256, size
    finally:
        temp_path.unlink(missing_ok=True)
        # Durable, private cache: retain validated pages across failed assembly,
        # process restarts and later document-only retries. Never expose refs.


def run_job_worker(
    *,
    settings: Settings = SETTINGS,
    config: PoolConfig | None = None,
    store: JobStore | None = None,
    pool_store: AccountPoolStore | None = None,
    headless: bool | None = None,
    once: bool = False,
    max_jobs: int | None = None,
    poll_seconds: float | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    scraper_factory: Callable[..., Any] | None = None,
    preflight_runner: Callable[..., Any] | None = None,
    proxy_health_runner: Callable[..., Any] | None = None,
    endurance_plan: Any | None = None,
) -> WorkerResult:
    from .endurance import EnduranceController, load_endurance_plan
    from .preflight import run_preflight
    from .proxy_health import run_proxy_health
    from .scraper import CBRSScraper

    validate_service_browser(settings)
    config = config or load_account_pool_config(settings)
    store = store or default_job_store(settings)
    pool_store = pool_store or AccountPoolStore(store.path)
    supplied_factory = scraper_factory
    scraper_factory = scraper_factory or CBRSScraper
    preflight_runner = preflight_runner or run_preflight
    proxy_health_runner = proxy_health_runner or run_proxy_health
    endurance_plan = endurance_plan or load_endurance_plan(
        settings.profile_dir.parent / "endurance-plan.json"
    )
    endurance = EnduranceController(store, endurance_plan, config)
    runtime_poll_seconds = (
        config.worker_poll_seconds if poll_seconds is None else float(poll_seconds)
    )
    runtime_headless = settings.headless if headless is None else headless
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"
    owner_mode = os.environ.get("CBRS_BROWSER_OWNER_MODE", "embedded")
    if owner_mode not in {"embedded", "external"}:
        raise ValueError("Invalid browser owner mode")
    if supplied_factory is None and owner_mode == "external":
        from functools import partial
        from .owner_protocol import RemoteScraper, OWNER_LEASE, command_path
        if not store.active_lease(OWNER_LEASE):
            raise RuntimeError("Start the independent browser owner before the worker")
        scraper_factory = partial(RemoteScraper, worker_id=worker_id,
                                  store_path=store.path, commands_path=command_path(settings))
    if not store.acquire_lease(WORKER_LEASE_NAME, worker_id):
        raise RuntimeError("Another CBRS job worker has an active lease.")

    run_id: str | None = None
    processed = 0
    final_status = "stopped"
    exit_code = 0
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    startup_gates_pending = True
    browser_pool = _PersistentAccountBrowsers(
        scraper_factory=scraper_factory,
        headless=runtime_headless,
        store=store,
        worker_id=worker_id,
    )
    from .runtime_updates import RuntimeUpdates
    updates = None
    try:
        updates = RuntimeUpdates(Path(__file__).parent, store.path.parent / "runtime-updates", owner=worker_id)
        store.recover_abandoned_jobs()
        legacy_stop = store.get_control("global_safety_stop")
        if legacy_stop:
            reason = str(legacy_stop.get("value") or StopReason.WAF_CHALLENGE.value)
            try:
                parsed_reason = StopReason(reason)
            except ValueError:
                parsed_reason = StopReason.WAF_CHALLENGE
            store.set_global_cooldown(
                parsed_reason.value,
                SAFETY_COOLDOWN_SECONDS.get(parsed_reason, 300.0),
            )
            store.clear_control("global_safety_stop")
        run_id = (
            f"jobs-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{secrets.token_hex(3)}"
        )
        pool_store.clear_stop_request()
        pool_store.create_run(run_id=run_id, dry_run=False, config=config, dashboard_url=None)
        _wire_browser_auth_recovery(
            settings, config, store, pool_store, run_id, browser_pool,
            preflight_runner, proxy_health_runner,
        )
        pool_store.add_event(run_id, message="job worker started", data={"worker_id": worker_id})
        heartbeat_thread = threading.Thread(
            target=_worker_heartbeat,
            args=(store, pool_store, worker_id, run_id, heartbeat_stop),
            name="cbrs-job-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        while max_jobs is None or processed < max_jobs:
            # Same owner/thread and browser objects; switch only before the next
            # complete operation, never inside a search or PDF job.
            updates.poll()
            pool_store.reset_quota_day(run_id, local_today())
            pool_store.reactivate_expired_cooldowns(run_id)
            browser_pool.reconcile()
            browser_pool.capture_previews()
            # A replacement worker can acquire the global lease moments before
            # the previous job lease expires. Recheck on every scheduler pass
            # so that job is requeued once it becomes stale without requiring
            # another process restart.
            store.recover_abandoned_jobs()
            if pool_store.stop_requested():
                final_status = "stopped"
                break
            cooldown = store.global_cooldown()
            if cooldown:
                final_status = "cooldown"
                pool_store.update_run(
                    run_id,
                    status="waiting",
                    next_cycle_at=cooldown["resume_at"],
                    blocked_reason=cooldown["reason"],
                )
                if once:
                    break
                sleep_fn(max(0.1, runtime_poll_seconds))
                continue
            if startup_gates_pending:
                # A replacement worker must not touch account browsers while a
                # global outage circuit is active. Run the live startup gates
                # only after the circuit expires, then exactly once.
                _run_startup_gates(
                    settings,
                    config,
                    store,
                    pool_store,
                    run_id,
                    preflight_runner,
                    proxy_health_runner,
                    browser_pool,
                )
                startup_gates_pending = False
                if store.global_cooldown():
                    if once:
                        final_status = "cooldown"
                        break
                    continue
            _process_requested_dataimpulse_rotation(
                settings=settings,
                config=config,
                store=store,
                pool_store=pool_store,
                run_id=run_id,
                browser_pool=browser_pool,
                worker_id=worker_id,
                preflight_runner=preflight_runner,
                proxy_health_runner=proxy_health_runner,
            )
            endurance.maybe_enqueue()
            job = store.claim_next(worker_id)
            if job is None:
                if once:
                    final_status = "idle"
                    break
                pool_store.update_run(run_id, status="waiting", next_cycle_at="")
                sleep_fn(max(0.1, runtime_poll_seconds))
                continue
            processed += 1
            pool_store.update_run(run_id, status="running", next_cycle_at="")
            outcome = _process_claimed_job(
                job,
                settings=settings,
                config=config,
                store=store,
                pool_store=pool_store,
                run_id=run_id,
                browser_pool=browser_pool,
                preflight_runner=preflight_runner,
                proxy_health_runner=proxy_health_runner,
                endurance_plan=endurance_plan,
            )
            if outcome == "cooldown":
                final_status = outcome
                if once:
                    break
                sleep_fn(max(0.1, runtime_poll_seconds))
                continue
            if job.source == "captcha_validation" and (once or max_jobs is not None):
                final_status = outcome
                break
            if once:
                final_status = outcome
                break
            if outcome in {"waiting_capacity", "waiting_captcha"}:
                sleep_fn(max(0.1, runtime_poll_seconds))
            elif (
                config.human_like_behavior_enabled
                and config.job_interval_max_seconds > 0
            ):
                sleep_fn(
                    random.uniform(
                        config.job_interval_min_seconds,
                        config.job_interval_max_seconds,
                    )
                )
            elif config.interval_minutes > 0:
                sleep_fn(config.interval_minutes * 60)
        else:
            final_status = "completed"
    except KeyboardInterrupt:
        final_status = "stopped"
    except Exception as exc:
        final_status = "failed"
        exit_code = 1
        try:
            store.add_event("worker_failed", level="error", data={"error": redact_text(str(exc))})
        except Exception:
            pass  # Database failure must not cascade into closing Chrome.
        if once or max_jobs is not None:
            raise
        # A recoverable Python exception must not destroy expensive browser
        # sessions. Retain the owner/heartbeat and wait for explicit service stop.
        while True:
            try:
                if pool_store.stop_requested():
                    break
                pool_store.update_run(run_id, status="waiting",
                                     blocked_reason="worker_failed_sessions_preserved")
                browser_pool.capture_previews()
            except Exception:
                pass
            sleep_fn(5)
        final_status = "stopped"
    finally:
        browser_pool.close_all(service_shutdown=True)
        if updates is not None:
            updates.close()
        heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=5)
        if run_id:
            pool_store.update_run(
                run_id,
                status=final_status,
                blocked_reason="global safety stop" if final_status == "safety_stop" else "",
                finished=True,
            )
        store.release_lease(WORKER_LEASE_NAME, worker_id)
    return WorkerResult(exit_code, worker_id, run_id, final_status, processed)


def _process_claimed_job(*args, **kwargs):
    from .runtime_updates import runtime_module
    return runtime_module("runtime_logic").process_job(*args, **kwargs)


def _wire_browser_auth_recovery(
    settings: Settings,
    config: PoolConfig,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    browser_pool: _PersistentAccountBrowsers,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
) -> None:
    accounts = {account.account_id: account for account in config.accounts if account.enabled}
    for account in accounts.values():
        if _is_dataimpulse_account(account) and account.dataimpulse_port is not None:
            store.ensure_dataimpulse_route(account.account_id, account.dataimpulse_port)

    def allowed(account_id: str) -> bool:
        if account_id not in accounts or pool_store.stop_requested() or store.global_cooldown():
            return False
        states = {row["account_id"]: row for row in pool_store.accounts(run_id)}
        state = states.get(account_id, {})
        if state.get("status") in {"paused", "captcha_pending", "captcha_solving"}:
            return False
        route = store.dataimpulse_route(account_id) or {}
        return not (str(route.get("cooldown_until") or "") > utc_now())

    def failed(account_id: str, exc: Exception) -> None:
        account = accounts[account_id]
        reason = (exc.reason.value if isinstance(exc, SafetyStopException)
                  else "credentials_invalid" if isinstance(exc, CredentialsRejectedError)
                  else "authentication_failed")
        store.add_event(
            "background_auth_failed", account_id=account_id, level="warning",
            data={"reason": reason, "auth_code": _auth_failure_code(exc),
                  "http_status": getattr(exc, "status", None),
                  "response_code": getattr(exc, "response_code", None)},
        )
        if isinstance(exc, SafetyStopException):
            _handle_account_safety_stop(
                exc, job_id=None, account=account, store=store,
                pool_store=pool_store, run_id=run_id, config=config,
                settings=settings, browser_pool=browser_pool,
                preflight_runner=preflight_runner, proxy_health_runner=proxy_health_runner,
            )
        else:
            pool_store.pause_account(
                run_id, account_id, reason=reason,
                cooldown_seconds=None if isinstance(exc, CredentialsRejectedError) else 300,
            )

    def succeeded(account_id: str) -> None:
        pool_store.mark_account_available(run_id, account_id)
        store.set_account_check(account_id, session_checked=True)
        store.add_event("background_auth_recovered", account_id=account_id,
                        data={"evidence": "authenticated_form"})

    browser_pool.can_reauthenticate = allowed
    browser_pool.on_auth_failure = failed
    browser_pool.on_auth_success = succeeded


def _run_startup_gates(
    settings: Settings,
    config: PoolConfig,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
    browser_pool: _PersistentAccountBrowsers,
) -> None:
    for account in config.accounts:
        if not account.enabled:
            continue
        try:
            runtime_settings = _runtime_account_settings(settings, account, store)
        except ValueError as exc:
            pool_store.pause_account(
                run_id, account.account_id, reason="account_configuration_invalid"
            )
            store.add_event(
                "account_configuration_invalid",
                account_id=account.account_id,
                level="error",
                data={"error": str(exc)},
            )
            continue
        gate_ok = _ensure_account_gate(
            account,
            runtime_settings,
            store,
            pool_store,
            run_id,
            preflight_runner,
            proxy_health_runner,
            force=True,
        )
        if not gate_ok:
            continue
        username = ""
        password = ""
        try:
            username, password = account_credentials(account)
            with browser_pool.session(
                account.account_id,
                runtime_settings,
                username,
                password,
            ):
                pass
            store.set_account_check(account.account_id, session_checked=True)
            pool_store.mark_account_available(run_id, account.account_id)
        except CredentialsRejectedError as exc:
            browser_pool.discard(account.account_id, status="credentials_invalid")
            pool_store.pause_account(run_id, account.account_id, reason="credentials_invalid",
                                     cooldown_seconds=None)
            store.add_event(
                "account_credentials_invalid",
                account_id=account.account_id,
                level="error",
                data={
                    "http_status": exc.status,
                    "response_code": exc.response_code,
                },
            )
        except SafetyStopException as exc:
            if browser_pool.on_auth_failure:
                browser_pool.on_auth_failure(account.account_id, exc)
            elif exc.reason in GLOBAL_SAFETY_REASONS:
                store.set_global_cooldown(
                    exc.reason.value,
                    SAFETY_COOLDOWN_SECONDS[exc.reason],
                )
                pool_store.pause_account(
                    run_id,
                    account.account_id,
                    reason=exc.reason.value,
                    cooldown_seconds=SAFETY_COOLDOWN_SECONDS[exc.reason],
                )
            elif exc.reason == StopReason.CAPTCHA_REJECTED:
                pool_store.mark_account_captcha_pending(
                    run_id,
                    account.account_id,
                    reason=exc.reason.value,
                    cooldown_seconds=SAFETY_COOLDOWN_SECONDS[exc.reason],
                )
            else:
                pool_store.pause_account(
                    run_id,
                    account.account_id,
                    reason=exc.reason.value,
                    cooldown_seconds=SAFETY_COOLDOWN_SECONDS.get(exc.reason, 300.0),
                )
            store.add_event(
                "account_startup_auth_stopped",
                account_id=account.account_id,
                level="warning",
                data={"reason": exc.reason.value},
            )
        except Exception as exc:
            if _looks_like_connection_failure(exc):
                browser_pool.discard(account.account_id, status="browser_context_failed")
            pool_store.pause_account(run_id, account.account_id, reason="startup_auth_failed")
            store.add_event(
                "account_startup_auth_failed",
                account_id=account.account_id,
                level="error",
                data={
                    "error": _redact_known_values(str(exc), username, password)
                },
            )


def _runtime_account_settings(
    settings: Settings,
    account: PoolAccount,
    store: JobStore,
    *,
    dataimpulse_port: int | None = None,
) -> Settings:
    if not _is_dataimpulse_account(account):
        return account_settings(settings, account)
    if account.dataimpulse_port is None:
        raise ValueError(
            f"Pool account {account.account_id} requires dataimpulse_port."
        )
    route = store.ensure_dataimpulse_route(
        account.account_id, account.dataimpulse_port
    )
    port = dataimpulse_port or int(route["active_port"])
    runtime = account_settings(settings, account, dataimpulse_port=port)
    generation = int(route.get("generation") or 0)
    if generation > 0:
        # A promoted sticky port represents a new exit peer.  Do not carry the
        # rejected peer's cache, cookies, storage, or service workers into the
        # replacement session.  The generation-specific directory remains
        # stable while that route is healthy, preserving successful logins.
        scoped_profile = (
            runtime.profile_dir.parent
            / f"chrome-profile-route-{generation}-port-{port}"
        ).resolve()
        legacy_profile = (
            runtime.profile_dir.parent
            / f"chrome-profile-route-{generation}"
        ).resolve()
        runtime = replace(
            runtime,
            profile_dir=(
                scoped_profile
                if scoped_profile.exists() or not legacy_profile.exists()
                else legacy_profile
            ),
        )
    return runtime


def _ensure_account_gate(
    account: PoolAccount,
    settings: Settings,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
    *,
    force: bool = False,
) -> bool:
    check = store.account_check(account.account_id) or {}
    proxy_checked_at = str(check.get("proxy_checked_at") or "")
    if (
        not force
        and check.get("proxy_status") == "passed"
        and proxy_checked_at
        and seconds_since(proxy_checked_at) < settings.proxy_recheck_seconds
    ):
        return True
    try:
        preflight = preflight_runner(settings, write_report=True)
        rotate_residential_baseline = False
        if (
            not preflight.ok
            and _is_sticky_residential_account(account)
        ):
            replacement_preflight = preflight_runner(
                settings,
                write_report=True,
                allow_baseline_replacement=True,
            )
            if replacement_preflight.ok:
                replacement_status = _egress_baseline_status(
                    replacement_preflight
                )
                preflight = replacement_preflight
            else:
                replacement_status = "failed"
            if replacement_status == "replacement_pending":
                if _is_dataimpulse_account(account):
                    from .proxy_provider import dataimpulse_configuration_health

                    provider_health = dataimpulse_configuration_health(
                        settings.dataimpulse_proxy_login,
                        settings.dataimpulse_proxy_password,
                        provider=account.proxy_provider,
                    )
                else:
                    from .proxy_provider import two_captcha_proxy_health

                    provider_health = two_captcha_proxy_health(
                        settings.two_captcha_api_key,
                        provider=account.proxy_provider,
                        force=True,
                    )
                if not provider_health.get("ok"):
                    raise SafetyStopException(
                        StopReason.PROXY_HEALTH,
                        "Residential proxy traffic is unavailable.",
                        context="job worker startup",
                    )
                rotate_residential_baseline = True
        if not preflight.ok:
            raise SafetyStopException(
                StopReason.EGRESS_PREFLIGHT,
                "Account preflight failed.",
                context="job worker startup",
            )
        if settings.proxy_url:
            proxy = proxy_health_runner(settings, write_report=True)
            if not proxy.ok:
                raise SafetyStopException(
                    StopReason.PROXY_HEALTH,
                    "Account proxy health gate failed.",
                    context="job worker startup",
                )
        egress_hash = str(preflight.report.get("egress_hash") or "") or None
        if egress_hash:
            owner = store.egress_owner(egress_hash, exclude_account=account.account_id)
            if owner:
                raise SafetyStopException(
                    StopReason.PROXY_HEALTH,
                    "Two enabled accounts resolved to the same fixed egress.",
                    context="job worker startup",
                )
        if rotate_residential_baseline:
            from .preflight import replace_egress_baseline

            replace_egress_baseline(
                settings,
                egress_hash=str(egress_hash or ""),
                egress_country=str(preflight.report.get("egress_country") or ""),
            )
            store.add_event(
                "residential_egress_rotated",
                account_id=account.account_id,
                data={
                    "provider": account.proxy_provider,
                    "country_validated": True,
                    "provider_healthy": True,
                    "portal_reachable": True,
                    "recaptcha_reachable": True,
                    "unique_egress": True,
                    "sanitized_baseline_archived": True,
                },
            )
        store.set_account_check(
            account.account_id,
            proxy_status="passed",
            egress_hash=egress_hash,
        )
        return True
    except Exception as exc:
        reason = exc.reason.value if isinstance(exc, SafetyStopException) else "gate_failed"
        pool_store.pause_account(run_id, account.account_id, reason=reason)
        store.set_account_check(account.account_id, proxy_status="failed")
        store.add_event(
            "account_gate_failed",
            account_id=account.account_id,
            level="error",
            data={"reason": reason},
        )
        return False


def _is_sticky_residential_account(account: PoolAccount) -> bool:
    from .proxy_provider import TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER

    return account.proxy_provider in {
        TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
        *DATAIMPULSE_STICKY_PROVIDERS,
    }


def _is_dataimpulse_account(account: PoolAccount) -> bool:
    return account.proxy_provider in DATAIMPULSE_STICKY_PROVIDERS


def _dataimpulse_failure_kind(exc: Exception) -> str:
    from .dataimpulse import classify_dataimpulse_failure

    status = getattr(exc, "status", None)
    try:
        parsed_status = int(status) if status is not None else None
    except (TypeError, ValueError):
        parsed_status = None
    return classify_dataimpulse_failure(parsed_status, str(exc))


def _egress_baseline_status(preflight: Any) -> str:
    checks = preflight.report.get("checks") if isinstance(preflight.report, Mapping) else []
    for check in checks or []:
        if isinstance(check, Mapping) and check.get("name") == "egress baseline":
            return str(check.get("detail") or "")
    return ""


class _CandidateRejected(Exception):
    """One candidate failed; carries the sanitized outcome for the ledger."""

    def __init__(
        self,
        outcome: str,
        reason: str,
        *,
        http_status: int | None = None,
        response_code: str | None = None,
        terminal: bool = False,
    ) -> None:
        self.outcome = outcome
        self.reason = reason
        self.http_status = http_status
        self.response_code = response_code
        self.terminal = terminal
        super().__init__(reason)


# Outcomes after which trying another port cannot help in this recovery call.
TERMINAL_CANDIDATE_OUTCOMES = frozenset({
    CANDIDATE_PROVIDER_TERMINAL,
    CANDIDATE_CREDENTIALS_REJECTED,
    CANDIDATE_PROVEN_UNPERSISTED,
})
# Portal-side rejections: wait the candidate retry delay before the next port.
# Transport-side failures move to the next port immediately.
PORTAL_CANDIDATE_OUTCOMES = frozenset({
    CANDIDATE_LOGIN_REJECTED,
    CANDIDATE_FORM_UNCONFIRMED,
})

_recovery_sleep = time.sleep


def _rotate_dataimpulse_route(
    account: PoolAccount,
    settings: Settings,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    browser_pool: _PersistentAccountBrowsers,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
    *,
    reason: str,
    _owner_execution: bool = False,
) -> bool:
    """Try new sticky ports for ONE account until one is proven and adopted.

    Up to ``dataimpulse_candidates_per_recovery`` candidates run back to back.
    A transport failure (preflight, proxy health, reused exit) moves straight
    to the next port; a portal login rejection waits
    ``dataimpulse_candidate_retry_seconds`` first. The loop ends at the first
    promotion, at a terminal outcome, when the hourly retry allowance is used
    up, or when a stop request / global cooldown appears. Every attempt is
    written to the candidate ledger with its own failure class.
    """
    if os.environ.get("CBRS_BROWSER_OWNER_MODE") == "external" and not _owner_execution:
        from .owner_protocol import owner_preserves_recovery_contexts
        if not owner_preserves_recovery_contexts(settings, store):
            store.add_event('dataimpulse_rotation_skipped', account_id=account.account_id,
                level='warning', data={'reason': 'owner_preservation_upgrade_required'})
            return False
        entry = browser_pool._entries.get(account.account_id)
        if entry is None or not getattr(getattr(entry.scraper, "browser", None), "is_remote", False):
            return False
        recovered = bool(entry.scraper._call("recover_route", {"reason": reason}))
        if recovered:
            browser_pool.discard(account.account_id, status="remote_route_adopted", service_shutdown=True)
            fresh = _runtime_account_settings(settings, account, store)
            browser_pool._known_accounts[account.account_id] = (fresh, entry.username, entry.password)
        return recovered
    validate_service_browser(settings)
    if not _is_dataimpulse_account(account) or account.dataimpulse_port is None:
        return False
    if (browser_pool.has_protected_session(account.account_id)
            and not browser_pool.can_replace_rejected_login(account.account_id)):
        store.add_event(
            "dataimpulse_rotation_skipped", account_id=account.account_id,
            level="warning",
            data={"reason": "authenticated_browser_preserved_until_service_stop"},
        )
        return False
    attempts = max(1, int(getattr(settings, "dataimpulse_candidates_per_recovery", 1) or 1))
    retry_delay = max(0.0, float(getattr(settings, "dataimpulse_candidate_retry_seconds", 0.0) or 0.0))
    outcome = ""
    for index in range(attempts):
        if index:
            if pool_store.stop_requested() or store.global_cooldown():
                break
        outcome = _try_dataimpulse_candidate(
            account, settings, store, pool_store, run_id, browser_pool,
            preflight_runner, proxy_health_runner,
            reason=reason, attempt_index=index + 1, attempts=attempts,
        )
        if outcome == CANDIDATE_PROMOTED:
            return True
        if outcome in TERMINAL_CANDIDATE_OUTCOMES or outcome.startswith("blocked:"):
            break
        if index + 1 < attempts and outcome in PORTAL_CANDIDATE_OUTCOMES and retry_delay > 0:
            _recovery_sleep(retry_delay)
    if outcome in PORTAL_CANDIDATE_OUTCOMES and retry_delay > 0:
        # Pacing inside this call is the sleep above; the durable stamp is the
        # deadline for the NEXT recovery call (published as next eligible).
        store.set_dataimpulse_route_retry(account.account_id, retry_seconds=retry_delay)
    return False


def _try_dataimpulse_candidate(
    account: PoolAccount,
    settings: Settings,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    browser_pool: _PersistentAccountBrowsers,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
    *,
    reason: str,
    attempt_index: int,
    attempts: int,
) -> str:
    """Validate, prove and promote ONE new sticky port; return its outcome."""
    candidate = store.begin_dataimpulse_rotation(
        account.account_id,
        initial_port=account.dataimpulse_port,
        reason=reason,
        port_min=settings.dataimpulse_port_min,
        port_max=settings.dataimpulse_port_max,
        cooldown_seconds=0.0,
        max_rotations_per_hour=settings.dataimpulse_max_rotations_per_hour,
        randomize=True,
    )
    if not candidate.get("ok"):
        blocked_reason = str(candidate.get("reason") or "proxy_rotation_blocked")
        next_eligible = candidate.get("next_eligible_at")
        if blocked_reason == RETRY_ALLOWANCE_EXHAUSTED and next_eligible:
            # One deadline for the route and the account: the window boundary.
            pool_store.pause_account(
                run_id,
                account.account_id,
                reason=RETRY_ALLOWANCE_EXHAUSTED,
                resume_at=str(next_eligible),
            )
        store.add_event(
            "dataimpulse_rotation_skipped",
            account_id=account.account_id,
            level="warning",
            data={
                "reason": blocked_reason,
                "next_eligible_at": next_eligible,
                "attempts_in_window": candidate.get("rotation_count"),
                "window_limit": settings.dataimpulse_max_rotations_per_hour,
            },
        )
        return f"blocked:{blocked_reason}"
    pending_port = int(candidate["pending_port"])
    started_at = utc_now()
    promoted = False
    candidate_manager = None
    candidate_scraper = None
    proven_entry = None
    adopted = False
    egress_hash = ""
    try:
        candidate_settings = _runtime_account_settings(
            settings,
            account,
            store,
            dataimpulse_port=pending_port,
        )
        candidate_generation = int(candidate.get("generation") or 0) + 1
        candidate_settings = replace(
            candidate_settings,
            profile_dir=(
                candidate_settings.profile_dir.parent
                / (
                    f"chrome-profile-route-{candidate_generation}"
                    f"-port-{pending_port}"
                )
            ).resolve(),
        )
        preflight = preflight_runner(
            candidate_settings,
            write_report=True,
            allow_baseline_replacement=True,
        )
        if not preflight.ok:
            details = " ".join(str(value) for value in preflight.report.get("errors", []))
            terminal = "407" in details or _dataimpulse_failure_kind(RuntimeError(details)) == "provider_terminal"
            raise _CandidateRejected(
                CANDIDATE_PROVIDER_TERMINAL if terminal else CANDIDATE_CONNECTIVITY_FAILED,
                StopReason.EGRESS_PREFLIGHT.value, terminal=terminal,
            )
        proxy = proxy_health_runner(candidate_settings, write_report=True)
        if not proxy.ok:
            details = " ".join(str(value) for value in proxy.report.get("errors", []))
            terminal = "407" in details or _dataimpulse_failure_kind(RuntimeError(details)) == "provider_terminal"
            raise _CandidateRejected(
                CANDIDATE_PROVIDER_TERMINAL if terminal else CANDIDATE_CONNECTIVITY_FAILED,
                StopReason.PROXY_HEALTH.value, terminal=terminal,
            )
        egress_hash = str(preflight.report.get("egress_hash") or "")
        if not egress_hash:
            raise _CandidateRejected(
                CANDIDATE_CONNECTIVITY_FAILED, "no_egress_identity",
            )
        previous_hash = (store.account_check(account.account_id) or {}).get("egress_hash")
        if ((previous_hash and egress_hash == previous_hash)
                or store.candidate_exit_rejected(account.account_id, egress_hash)):
            raise _CandidateRejected(
                CANDIDATE_EXIT_REUSED, "previous_or_rejected_exit",
            )
        owner = store.egress_owner(egress_hash, exclude_account=account.account_id)
        if owner:
            raise _CandidateRejected(
                CANDIDATE_EXIT_REUSED, "exit_assigned_to_other_account",
            )
        # Prove the target application's strongest signal before making the
        # route durable.  This standalone context uses the exact profile that
        # the worker will retain after promotion while the current account
        # context remains untouched until the candidate succeeds.
        username, password = account_credentials(account)
        try:
            candidate_manager = browser_pool.scraper_factory(
                headless=browser_pool.headless,
                settings=candidate_settings,
            )
            candidate_scraper = (
                candidate_manager.__enter__()
                if hasattr(candidate_manager, "__enter__")
                else candidate_manager
            )
        except Exception as exc:
            raise _CandidateRejected(
                CANDIDATE_LAUNCH_FAILED, _redact_known_values(str(exc), username, password)[:160],
            ) from exc
        try:
            candidate_scraper.ensure_authenticated(username, password)
        except CredentialsRejectedError as exc:
            raise _CandidateRejected(
                CANDIDATE_CREDENTIALS_REJECTED, "credentials_invalid",
                http_status=exc.status, response_code=exc.response_code, terminal=True,
            ) from exc
        except SafetyStopException as exc:
            raise _CandidateRejected(
                CANDIDATE_LOGIN_REJECTED, exc.reason.value,
                http_status=exc.status, response_code=exc.response_code,
            ) from exc
        except Exception as exc:
            raise _CandidateRejected(
                CANDIDATE_CONNECTIVITY_FAILED if _looks_like_connection_failure(exc)
                else CANDIDATE_FORM_UNCONFIRMED,
                _redact_known_values(str(exc), username, password)[:160],
            ) from exc
        candidate_browser = getattr(
            candidate_scraper, "browser", candidate_scraper
        )
        raw_auth_state = candidate_browser.wait_for_commerce_auth_state()
        auth_state = (
            raw_auth_state
            if isinstance(raw_auth_state, CommerceAuthState)
            else CommerceAuthState(str(raw_auth_state))
        )
        if auth_state is not CommerceAuthState.AUTHENTICATED_FORM:
            raise _CandidateRejected(
                CANDIDATE_FORM_UNCONFIRMED, StopReason.AUTH_REQUIRED.value,
            )
        proven_entry = _ManagedAccountScraper(
            manager=candidate_manager, scraper=candidate_scraper,
            settings=candidate_settings, username=username, password=password,
            authenticated_once=True, last_restart_at=time.monotonic(),
        )
        preserve = getattr(candidate_browser, "preserve_for_service_lifetime", None)
        if callable(preserve):
            preserve()
        from .preflight import replace_egress_baseline

        replace_egress_baseline(
            candidate_settings,
            egress_hash=egress_hash,
            egress_country=str(preflight.report.get("egress_country") or ""),
        )
        route = store.finish_dataimpulse_rotation(
            account.account_id, promoted=True,
            cooldown_seconds=settings.dataimpulse_rotation_cooldown_seconds,
        )
        promoted = True
        store.set_account_check(
            account.account_id,
            proxy_status="passed",
            egress_hash=egress_hash,
        )
        adopted = True
        browser_pool.adopt_authenticated_candidate(account.account_id, proven_entry)
        pool_store.mark_account_available(run_id, account.account_id)
        store.record_candidate_attempt(
            account.account_id, sticky_port=pending_port, egress_hash=egress_hash,
            outcome=CANDIDATE_PROMOTED, started_at=started_at,
        )
        store.add_event(
            "dataimpulse_route_rotated",
            account_id=account.account_id,
            data={
                "generation": int(route["generation"]),
                "sticky_port": int(route["active_port"]),
                "candidate_attempt": attempt_index,
                "candidate_attempts_allowed": attempts,
                "country_validated": True,
                "portal_reachable": True,
                "recaptcha_reachable": True,
                "unique_egress": True,
                "authenticated_form": True,
            },
        )
        return CANDIDATE_PROMOTED
    except Exception as exc:
        if isinstance(exc, _CandidateRejected):
            outcome, failure_reason = exc.outcome, exc.reason
            http_status, response_code, terminal = exc.http_status, exc.response_code, exc.terminal
        elif proven_entry is not None:
            outcome, failure_reason = CANDIDATE_PROVEN_UNPERSISTED, redact_text(str(exc))[:160]
            http_status, response_code, terminal = None, None, True
        else:
            outcome, failure_reason = (
                (CANDIDATE_CONNECTIVITY_FAILED, redact_text(str(exc))[:160])
                if _looks_like_connection_failure(exc)
                else (CANDIDATE_FORM_UNCONFIRMED, redact_text(str(exc))[:160])
            )
            http_status, response_code, terminal = None, None, False
        if proven_entry is None and egress_hash:
            store.record_failed_candidate_exit(account.account_id, egress_hash)
        if outcome in {CANDIDATE_PROVIDER_TERMINAL, CANDIDATE_CREDENTIALS_REJECTED}:
            pool_store.pause_account(
                run_id, account.account_id,
                reason=(
                    "provider_terminal" if outcome == CANDIDATE_PROVIDER_TERMINAL
                    else "credentials_invalid"
                ),
                cooldown_seconds=None,
            )
        if not promoted:
            store.finish_dataimpulse_rotation(
                account.account_id,
                promoted=False,
                error_code=outcome,
            )
        store.record_candidate_attempt(
            account.account_id, sticky_port=pending_port, egress_hash=egress_hash or None,
            outcome=outcome, http_status=http_status, response_code=response_code,
            reason=failure_reason, started_at=started_at,
        )
        store.add_event(
            (
                "dataimpulse_route_promoted_auth_failed"
                if promoted
                else "dataimpulse_rotation_failed"
            ),
            account_id=account.account_id,
            level="error",
            data={
                "reason": outcome,
                "detail": failure_reason,
                "http_status": http_status,
                "response_code": response_code,
                "sticky_port": pending_port,
                "candidate_attempt": attempt_index,
                "candidate_attempts_allowed": attempts,
                "terminal": terminal,
            },
        )
        return outcome
    finally:
        if proven_entry is not None and not adopted:
            # Even persistence failure cannot authorize closing a proven session.
            browser_pool._retained_entries.append((account.account_id, proven_entry))
        elif proven_entry is None and candidate_manager is not None:
            # Only a new, rejected disposable probe is eligible for cleanup.
            try:
                if hasattr(candidate_manager, "__exit__"):
                    candidate_manager.__exit__(None, None, None)
                elif candidate_scraper is not None and hasattr(candidate_scraper, "close"):
                    candidate_scraper.close()
            except Exception:
                pass


def _process_requested_dataimpulse_rotation(
    *,
    settings: Settings,
    config: PoolConfig,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    browser_pool: _PersistentAccountBrowsers,
    worker_id: str,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
) -> bool:
    """Run an explicitly requested rotation inside the exclusive browser owner."""
    request = store.claim_dataimpulse_rotation_request(worker_id)
    if not request:
        return False
    request_id = str(request.get("request_id") or "")
    account_id = str(request.get("account_id") or "")
    account = next(
        (candidate for candidate in config.accounts if candidate.account_id == account_id),
        None,
    )
    if not account or not account.enabled or not _is_dataimpulse_account(account):
        store.finish_dataimpulse_rotation_request(
            request_id,
            ok=False,
            result={"account_id": account_id, "reason": "invalid_dataimpulse_account"},
        )
        return True
    before = store.ensure_dataimpulse_route(account.account_id, int(account.dataimpulse_port or 0))
    ok = _rotate_dataimpulse_route(
        account,
        settings,
        store,
        pool_store,
        run_id,
        browser_pool,
        preflight_runner,
        proxy_health_runner,
        reason=str(request.get("reason") or "operator_requested_recovery"),
    )
    after = store.dataimpulse_route(account.account_id) or before
    failure_reason = None
    if not ok:
        latest = next(
            (
                event
                for event in store.recent_events(limit=10)
                if event.get("account_id") == account.account_id
                and event.get("event")
                in {"dataimpulse_route_promoted_auth_failed", "dataimpulse_rotation_failed"}
            ),
            None,
        )
        if latest:
            try:
                failure_reason = json.loads(str(latest.get("data_json") or "{}")).get(
                    "reason"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                failure_reason = "rotation_failed"
    store.finish_dataimpulse_rotation_request(
        request_id,
        ok=ok,
        result={
            "account_id": account.account_id,
            "previous_port": int(before["active_port"]),
            "active_port": int(after["active_port"]),
            "generation": int(after.get("generation") or 0),
            "route_status": str(after.get("status") or "unknown"),
            **({"reason": failure_reason or "rotation_failed"} if not ok else {}),
        },
    )
    store.add_event(
        "dataimpulse_rotation_request_completed",
        account_id=account.account_id,
        level="info" if ok else "error",
        data={"request_id": request_id, "ok": ok},
    )
    return True


def _handle_account_safety_stop(
    exc: SafetyStopException,
    *,
    job_id: str | None,
    account: PoolAccount,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    config: PoolConfig,
    settings: Settings,
    browser_pool: _PersistentAccountBrowsers,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
) -> str:
    if exc.reason in GLOBAL_SAFETY_REASONS:
        seconds = SAFETY_COOLDOWN_SECONDS[exc.reason]
        cooldown = store.set_global_cooldown(exc.reason.value, seconds)
        pool_store.pause_account(
            run_id,
            account.account_id,
            reason=exc.reason.value,
            cooldown_seconds=seconds,
        )
        store.add_event(
            "global_safety_cooldown",
            job_id=job_id,
            account_id=account.account_id,
            level="error",
            data={"reason": exc.reason.value, "resume_at": cooldown["resume_at"]},
        )
        return "cooldown"
    if exc.reason == StopReason.CAPTCHA_REJECTED:
        pool_store.mark_account_captcha_pending(
            run_id,
            account.account_id,
            reason=exc.reason.value,
            cooldown_seconds=SAFETY_COOLDOWN_SECONDS[exc.reason],
        )
    else:
        pool_store.pause_account(
            run_id,
            account.account_id,
            reason=exc.reason.value,
            cooldown_seconds=SAFETY_COOLDOWN_SECONDS.get(exc.reason, 300.0),
        )
    if (
        exc.reason == StopReason.TEMPORARY_UNAVAILABLE
        and _is_dataimpulse_account(account)
        and account.dataimpulse_port is not None
    ):
        failures = store.record_dataimpulse_temporary_failure(
            account.account_id,
            initial_port=account.dataimpulse_port,
        )
        login_scope = job_id is None
        # A visibly rejected login recovers after ``login_recovery_threshold``
        # (default: the first one). Query failures keep failing over to another
        # account before any route change.
        threshold = (
            settings.dataimpulse_login_recovery_threshold
            if login_scope
            else settings.dataimpulse_temp_unavailable_threshold
        )
        recent_success = store.another_account_succeeded_recently(
            account.account_id, allow_authenticated_form=login_scope,
        )
        canary = False
        if (not recent_success and login_scope
                and account.proxy_provider == "dataimpulse_mobile_sticky"
                and exc.status == 400 and exc.context == "auth login"
                and failures >= threshold
                and not store.global_cooldown()
                and not browser_pool.has_protected_session(account.account_id)):
            # The operator confirmed direct-IP logins work. Permit a bounded
            # Mobile-only sample even when no current proxy account works;
            # this does not bypass the outage wait or establish its cause.
            canary = store.reserve_mobile_login_canary(
                max_per_hour=settings.dataimpulse_canary_per_hour,
                spacing_seconds=settings.dataimpulse_canary_spacing_seconds,
            )
        store.add_event(
            "dataimpulse_recovery_evaluated", account_id=account.account_id,
            job_id=job_id,
            data={"reason": exc.reason.value,
                  "auth_code": _auth_failure_code(exc),
                  "http_status": exc.status,
                  "response_code": getattr(exc, "response_code", None),
                  "failures": failures,
                  "threshold": threshold,
                  "other_account_succeeded": recent_success,
                  "mobile_canary_reserved": canary,
                  "scope": "login" if login_scope else "query"},
        )
        if failures >= threshold and (recent_success or canary):
            if _rotate_dataimpulse_route(
                account,
                settings,
                store,
                pool_store,
                run_id,
                browser_pool,
                preflight_runner,
                proxy_health_runner,
                reason="mobile_login_canary" if canary else "repeated_account_temporary_unavailable",
            ):
                store.add_event(
                    "dataimpulse_account_recovered",
                    job_id=job_id,
                    account_id=account.account_id,
                    data={"trigger": "temporary_unavailable_threshold"},
                )
                return "retry_account"
            if login_scope:
                _align_login_pause_with_route(
                    account, settings, store, pool_store, run_id,
                    reason=exc.reason.value,
                )
    if (
        exc.reason == StopReason.TEMPORARY_UNAVAILABLE
        and _all_enabled_accounts_temporarily_unavailable(
            pool_store,
            run_id,
            account_ids={
                candidate.account_id for candidate in config.accounts if candidate.enabled
            },
        )
    ):
        backoff = store.advance_external_outage_backoff()
        store.add_event(
            "external_portal_backoff",
            job_id=job_id,
            account_id=account.account_id,
            level="warning",
            data={
                "reason": EXTERNAL_OUTAGE_REASON,
                "resume_at": backoff["resume_at"],
                "streak": backoff["streak"],
                "seconds": backoff["seconds"],
            },
        )
        return "cooldown"
    store.add_event(
        "account_safety_stop",
        job_id=job_id,
        account_id=account.account_id,
        level="warning",
        data={"reason": exc.reason.value},
    )
    return "retry_account"


def _align_login_pause_with_route(
    account: PoolAccount,
    settings: Settings,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    *,
    reason: str,
) -> None:
    """After a failed login recovery, pause only as long as the route requires.

    The generic ``temporary_unavailable`` pause exists to avoid re-submitting
    the same rejected login. Once candidates were actually tried, the next
    eligible attempt is the route's own deadline: the retry-allowance window
    boundary when exhausted, otherwise the short candidate retry delay.
    """
    route = store.dataimpulse_route(account.account_id) or {}
    status = str(route.get("status") or "")
    cooldown_until = str(route.get("cooldown_until") or "")
    now = utc_now()
    if status == RETRY_ALLOWANCE_EXHAUSTED and cooldown_until > now:
        pool_store.pause_account(
            run_id, account.account_id,
            reason=RETRY_ALLOWANCE_EXHAUSTED, resume_at=cooldown_until,
        )
        return
    if status != "candidate_failed":
        return  # nothing was tried (protected session, external owner...)
    retry_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=max(0.0, float(settings.dataimpulse_candidate_retry_seconds)))
    ).replace(microsecond=0).isoformat()
    pool_store.pause_account(
        run_id, account.account_id, reason=reason,
        resume_at=max(retry_at, cooldown_until) if cooldown_until else retry_at,
    )


def _all_enabled_accounts_temporarily_unavailable(
    pool_store: AccountPoolStore,
    run_id: str,
    *,
    account_ids: set[str],
) -> bool:
    if not account_ids:
        return False
    states = {
        str(row["account_id"]): row
        for row in pool_store.accounts(run_id)
        if str(row["account_id"]) in account_ids
    }
    return len(states) == len(account_ids) and all(
        str(states[account_id]["status"]) == "paused"
        and str(states[account_id]["paused_reason"])
        == StopReason.TEMPORARY_UNAVAILABLE.value
        for account_id in account_ids
    )


def _search_job(scraper: Any, job: Job) -> list[dict[str, Any]]:
    context_setter = getattr(scraper, "set_job_context", None)
    if callable(context_setter):
        context_setter(job.job_id)
    if job.kind == "text":
        return scraper.search_by_text(str(job.input["text"]))
    return scraper.search_by_fna(
        int(job.input["foja"]), int(job.input["numero"]), int(job.input["year"])
    )


def _unavailable_job_status(
    pool_store: AccountPoolStore,
    run_id: str,
    config: PoolConfig,
    excluded: set[str],
) -> str:
    enabled = {account.account_id for account in config.accounts if account.enabled}
    rows = [
        row
        for row in pool_store.accounts(run_id)
        if str(row["account_id"]) in enabled and str(row["account_id"]) not in excluded
    ]
    captcha_states = {CAPTCHA_PENDING_STATUS, CAPTCHA_SOLVING_STATUS}
    if rows and all(str(row["status"]) in captcha_states for row in rows):
        return "waiting_captcha"
    all_rows = [
        row for row in pool_store.accounts(run_id) if str(row["account_id"]) in enabled
    ]
    if all_rows and all(str(row["status"]) in captcha_states for row in all_rows):
        return "waiting_captcha"
    return "waiting_capacity"


def _worker_heartbeat(
    store: JobStore,
    pool_store: AccountPoolStore,
    worker_id: str,
    run_id: str,
    stop: threading.Event,
) -> None:
    while not stop.wait(30):
        try:
            if not store.heartbeat_lease(WORKER_LEASE_NAME, worker_id):
                return
            pool_store.update_run(run_id)
            running = next(
                (job for job in store.list_jobs(limit=100) if job["status"] == "running"),
                None,
            )
            if running:
                store.heartbeat_job(str(running["job_id"]), worker_id)
        except Exception:
            return


def _artifact_stem(
    sequence: int,
    result: Mapping[str, Any],
    *,
    sample_pages: int | None = None,
) -> str:
    foja = result.get("foja", "unknown")
    numero = result.get("numero", result.get("num", "unknown"))
    year = result.get("ano", result.get("year", "unknown"))
    stem = f"{sequence:04d}_{_safe_part(foja)}_{_safe_part(numero)}_{_safe_part(year)}"
    if sample_pages is not None:
        stem += f"_test-sample-max{sample_pages}p"
    return stem


def _expected_artifact_path(
    output_root: Path,
    job_id: str,
    item: Mapping[str, Any],
    *,
    sample_pages: int | None = None,
) -> Path:
    result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
    return (
        output_root
        / "jobs"
        / job_id
        / f"{_artifact_stem(int(item['sequence']), result, sample_pages=sample_pages)}.pdf"
    )


def _safe_part(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return safe[:80] or "unknown"


def _redact_known_values(text: str, *values: str) -> str:
    safe = redact_text(text)
    for value in values:
        if value:
            safe = safe.replace(value, "[REDACTED]")
    return safe


def _auth_failure_code(exc: Exception) -> str:
    """Specific, sanitized code for a failed browser login.

    ``temporary_unavailable`` is CBRS's shared retry reason for a rejected
    login submission, a rejected login page, a rejected search and a generic
    error dialog. The overview needs to tell those apart; the recovery policy
    keeps using ``exc.reason``.
    """
    if isinstance(exc, SafetyStopException):
        if exc.reason == StopReason.TEMPORARY_UNAVAILABLE:
            if exc.context == "auth login":
                return AUTH_LOGIN_REJECTED
            if exc.context == "auth navigation":
                return AUTH_LOGIN_PAGE_REJECTED
        return exc.reason.value
    if isinstance(exc, CredentialsRejectedError):
        return "credentials_invalid"
    return "authentication_failed"


def _looks_like_connection_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "connection",
            "net::",
            "proxy",
            "timed out",
            "timeout",
            "name not resolved",
            "network",
            "browser has been closed",
            "target page",
            "browser closed",
            "page crashed",
        )
    )


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("idempotency_key must be a string")
    value = value.strip()
    if not value:
        return None
    if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError(
            "idempotency_key must be 1-200 characters using letters, digits, '.', '_', ':', or '-'"
        )
    return value


def _public_artifact(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "artifact_id": row["artifact_id"],
        "job_id": row["job_id"],
        "item_id": row["item_id"],
        "filename": Path(str(row["path"])).name,
        "content_type": row["content_type"],
        "sha256": row["sha256"],
        "bytes": row["bytes"],
        "page_count": row["page_count"],
        "valid": bool(row["valid"]),
        "created_at": row["created_at"],
    }


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        input=json.loads(str(row["input_json"])),
        status=str(row["status"]),
        idempotency_key=str(row["idempotency_key"]) if row["idempotency_key"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        cancel_requested=bool(row["cancel_requested"]),
        source=str(row["source"]),
    )


def _utc_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(
        microsecond=0
    ).isoformat()
