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
from dataclasses import dataclass
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
from .config import SETTINGS, Settings
from .dataimpulse import (
    DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER,
    next_unused_sticky_port,
)
from .pdf import create_pdf
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
        self._known_accounts: dict[str, tuple[Settings, str, str]] = {}
        self._last_reconcile_at = 0.0

    def refresh_page_auth_states(self) -> None:
        """Publish fail-closed DOM evidence without performing authentication."""
        for account_id, entry in tuple(self._entries.items()):
            browser = getattr(entry.scraper, "browser", entry.scraper)
            detector = getattr(browser, "detect_commerce_auth_state", None)
            if not callable(detector):
                legacy_detector = getattr(browser, "page_requires_login", None)
                if not callable(legacy_detector):
                    continue
                detector = lambda: (
                    CommerceAuthState.LOGIN_GATE
                    if legacy_detector()
                    else CommerceAuthState.UNKNOWN
                )
            try:
                raw_state = detector()
                state = (
                    raw_state
                    if isinstance(raw_state, CommerceAuthState)
                    else CommerceAuthState(str(raw_state))
                )
            except Exception:
                state = CommerceAuthState.UNKNOWN
            entry.unknown_checks = (
                entry.unknown_checks + 1
                if state is CommerceAuthState.UNKNOWN
                else 0
            )
            self.store.set_account_browser_state(
                account_id,
                live=True,
                authenticated=state is CommerceAuthState.AUTHENTICATED_FORM,
                headless=self.headless,
                owner=self.worker_id,
                status={
                    CommerceAuthState.AUTHENTICATED_FORM: "authenticated_form_visible",
                    CommerceAuthState.LOGIN_GATE: "login_gate_visible",
                    CommerceAuthState.CONFLICT: "authentication_dom_conflict",
                    CommerceAuthState.UNKNOWN: "authentication_unknown",
                }[state],
                auth_state=state.value,
            )

    def reconcile(self) -> None:
        """Keep successful per-account contexts alive without cross-account resets."""
        now = time.monotonic()
        intervals = [
            settings.browser_healthcheck_seconds
            for settings, _username, _password in self._known_accounts.values()
        ]
        interval = min(intervals, default=30.0)
        if now - self._last_reconcile_at < interval:
            return
        self._last_reconcile_at = now
        self.refresh_page_auth_states()
        for account_id, credentials in tuple(self._known_accounts.items()):
            settings, username, password = credentials
            entry = self._entries.get(account_id)
            if entry is None:
                try:
                    with self.session(account_id, settings, username, password):
                        pass
                except Exception:
                    continue
                continue
            try:
                browser = getattr(entry.scraper, "browser", entry.scraper)
                page = browser.page
                if callable(getattr(page, "is_closed", None)) and page.is_closed():
                    self.discard(account_id, status="browser_context_closed")
                    with self.session(account_id, settings, username, password):
                        pass
                    continue
                raw_state = browser.detect_commerce_auth_state()
                state = (
                    raw_state
                    if isinstance(raw_state, CommerceAuthState)
                    else CommerceAuthState(str(raw_state))
                )
                if state is CommerceAuthState.UNKNOWN and entry.unknown_checks >= 2:
                    browser.reload_current_page()
                    state = browser.wait_for_commerce_auth_state()
                    entry.unknown_checks = 0
                should_reauthenticate = state is CommerceAuthState.LOGIN_GATE or (
                    state is CommerceAuthState.UNKNOWN and entry.reauth_required
                )
                if should_reauthenticate:
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
            except Exception as exc:
                if _looks_like_connection_failure(exc):
                    self.discard(account_id, status="browser_context_failed")

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
                status="authenticating",
            )

        try:
            auth_method = entry.scraper.ensure_authenticated(
                username, password, force=force
            )
        except Exception as exc:
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
                    status="authentication_unconfirmed",
                    auth_state=CommerceAuthState.UNKNOWN.value,
                )
            raise

        entry.reauth_required = False
        self.store.set_account_browser_state(
            account_id,
            live=True,
            authenticated=True,
            headless=self.headless,
            owner=self.worker_id,
            status={
                "refreshed": "authenticated_refresh",
                "browser_fetch": "authenticated_login_api",
                "browser_form": "authenticated_login_form",
            }.get(str(auth_method or ""), "authenticated"),
            auth_state=CommerceAuthState.AUTHENTICATED_FORM.value,
        )
        yield entry.scraper

    def discard(self, account_id: str, *, status: str) -> None:
        entry = self._entries.pop(account_id, None)
        if entry is not None:
            try:
                if hasattr(entry.manager, "__exit__"):
                    entry.manager.__exit__(None, None, None)
                elif hasattr(entry.scraper, "close"):
                    entry.scraper.close()
            except Exception:
                pass
        self.store.set_account_browser_state(
            account_id,
            live=False,
            authenticated=False,
            headless=self.headless,
            owner=self.worker_id,
            status=status,
        )

    def close_all(self, *, status: str = "worker_stopped") -> None:
        for account_id in tuple(self._entries):
            self.discard(account_id, status=status)
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
                    updated_at TEXT NOT NULL
                );

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
                VALUES ('jobs', 7, ?)
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
                "browser_status": "TEXT",
                "browser_auth_state": "TEXT",
                "browser_started_at": "TEXT",
                "browser_checked_at": "TEXT",
            }
            for name, definition in browser_columns.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE account_checks ADD COLUMN {name} {definition}")
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
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
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
                """,
                (now,),
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

    def add_results(self, job_id: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT COUNT(*) AS count FROM job_items WHERE job_id = ?", (job_id,)
            ).fetchone()
            if int(existing["count"] or 0) == 0:
                for sequence, result in enumerate(results, 1):
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
            )
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
                if used >= quota:
                    continue
                if source_quota_by_account and account.account_id in source_quota_by_account:
                    source_used = int(
                        db.execute(
                            """
                            SELECT COUNT(*) FROM job_attempts a
                            JOIN jobs j ON j.job_id = a.job_id
                            WHERE a.account_id = ? AND a.quota_date = ?
                              AND a.quota_consumed = 1 AND j.source = ?
                            """,
                            (account.account_id, quota_date, source),
                        ).fetchone()[0]
                    )
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
        status: str,
        auth_state: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO account_checks(
                    account_id, browser_live, browser_authenticated, browser_headless,
                    browser_owner, browser_status, browser_started_at,
                    browser_checked_at, browser_auth_state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    status,
                    now if live else None,
                    now,
                    auth_state,
                    now,
                ),
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
                return {**state, "ok": False, "reason": "rotation_cooldown"}
            window_started = str(state.get("rotation_window_started_at") or "")
            if not window_started or seconds_since(window_started) >= 3600:
                window_started = now
                rotation_count = 0
            else:
                rotation_count = int(state.get("rotation_count") or 0)
            if rotation_count >= max_rotations_per_hour:
                cooldown = (now_dt + timedelta(seconds=3600)).replace(
                    microsecond=0
                ).isoformat()
                db.execute(
                    """
                    UPDATE account_proxy_routes
                    SET status = 'proxy_recovery_exhausted', cooldown_until = ?,
                        updated_at = ? WHERE account_id = ?
                    """,
                    (cooldown, now, account_id),
                )
                return {
                    **state,
                    "ok": False,
                    "reason": "proxy_recovery_exhausted",
                    "cooldown_until": cooldown,
                }
            used_ports = {
                int(value)
                for used in db.execute(
                    "SELECT active_port, pending_port FROM account_proxy_routes"
                ).fetchall()
                for value in (used["active_port"], used["pending_port"])
                if value is not None
            }
            candidate = next_unused_sticky_port(
                int(state["active_port"]),
                used_ports=used_ports,
                minimum=port_min,
                maximum=port_max,
            )
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
            }

    def finish_dataimpulse_rotation(
        self,
        account_id: str,
        *,
        promoted: bool,
        error_code: str | None = None,
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
                db.execute(
                    """
                    UPDATE account_proxy_routes
                    SET active_port = pending_port, pending_port = NULL,
                        generation = generation + 1, status = 'active',
                        last_error_code = NULL, last_rotated_at = ?,
                        temporary_window_started_at = NULL,
                        temporary_failure_count = 0, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (now, now, account_id),
                )
            else:
                db.execute(
                    """
                    UPDATE account_proxy_routes
                    SET pending_port = NULL, status = 'candidate_failed',
                        last_error_code = ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (redact_text(error_code or "candidate_failed"), now, account_id),
                )
            updated = db.execute(
                "SELECT * FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            assert updated is not None
            return dict(updated)

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

    def another_account_succeeded_recently(
        self, account_id: str, *, within_seconds: float = 600
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
        payload = {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "source": row["source"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "priority": int(row["priority"] or 0),
            "result_count": row["result_count"],
            "completed_items": row["completed_items"],
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
        raw_input = json.loads(str(row["input_json"]))
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
            SELECT account_id, status, safety_stop, started_at, finished_at
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
        row = db.execute(
            """
            SELECT COALESCE((
                SELECT used FROM account_daily_usage
                WHERE account_id = ? AND quota_date = ?
            ), 0) + COALESCE((
                SELECT COUNT(*) FROM cycles c
                JOIN runs r ON r.run_id = c.run_id
                WHERE c.account_id = ? AND c.quota_date = ? AND r.dry_run = 0
            ), 0) + COALESCE((
                SELECT COUNT(*) FROM job_attempts
                WHERE account_id = ? AND quota_date = ?
                  AND quota_consumed = 1 AND status = 'running'
            ), 0) AS used
            """,
            (
                account_id,
                quota_date,
                account_id,
                quota_date,
                account_id,
                quota_date,
            ),
        ).fetchone()
        return int(row["used"] or 0)


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

    _ticket_info, refs = scraper.get_image_refs(str(ticket))
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
            images.append(scraper.download_image(data_ref, image_path))
        create_pdf(images, temp_path)
        sha256, size = validate_pdf(temp_path, expected_pages=len(refs))
        os.replace(temp_path, final_path)
        return final_path, len(refs), sha256, size
    finally:
        temp_path.unlink(missing_ok=True)
        for image in images:
            image.unlink(missing_ok=True)
        if image_dir.exists():
            for leftover in image_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink(missing_ok=True)
        try:
            image_dir.rmdir()
            image_dir.parent.rmdir()
        except OSError:
            pass


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

    config = config or load_account_pool_config(settings)
    store = store or default_job_store(settings)
    pool_store = pool_store or AccountPoolStore(store.path)
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
    try:
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
        pool_store.add_event(run_id, message="job worker started", data={"worker_id": worker_id})
        heartbeat_thread = threading.Thread(
            target=_worker_heartbeat,
            args=(store, pool_store, worker_id, run_id, heartbeat_stop),
            name="cbrs-job-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        while max_jobs is None or processed < max_jobs:
            pool_store.reset_quota_day(run_id, local_today())
            pool_store.reactivate_expired_cooldowns(run_id)
            browser_pool.reconcile()
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
            if job.source == "captcha_validation":
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
        store.add_event("worker_failed", level="error", data={"error": str(exc)})
        raise
    finally:
        browser_pool.close_all()
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


def _process_claimed_job(
    job: Job,
    *,
    settings: Settings,
    config: PoolConfig,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
    browser_pool: _PersistentAccountBrowsers,
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
    endurance_plan: Any | None = None,
) -> str:
    target_account_id = (
        str(job.input.get("target_account_id") or "")
        if job.source == "captcha_validation"
        else ""
    )
    excluded: set[str] = (
        {
            account.account_id
            for account in config.accounts
            if account.account_id != target_account_id
        }
        if target_account_id
        else set()
    )
    quota_date = local_today()
    while True:
        if store.cancel_requested(job.job_id):
            return store.finalize_job(job.job_id)
        account = store.select_account(
            run_id=run_id,
            config=config,
            quota_date=quota_date,
            excluded=excluded,
            source=job.source,
            source_quota_by_account=(
                endurance_plan.source_quota(config)
                if job.source == "endurance" and endurance_plan is not None
                else None
            ),
        )
        if account is None:
            if target_account_id:
                target_state = next(
                    (
                        str(row["status"])
                        for row in pool_store.accounts(run_id)
                        if str(row["account_id"]) == target_account_id
                    ),
                    "paused",
                )
                status = (
                    "waiting_captcha"
                    if target_state == CAPTCHA_PENDING_STATUS
                    else "waiting_capacity"
                )
            else:
                status = _unavailable_job_status(pool_store, run_id, config, excluded)
            store.set_waiting(job.job_id, status, reason=status)
            pool_store.update_run(
                run_id,
                status=status,
                next_cycle_at=next_quota_reset_at() if status == "waiting_capacity" else "",
                blocked_reason=status,
            )
            return status
        excluded.add(account.account_id)
        try:
            runtime_settings = _runtime_account_settings(settings, account, store)
        except ValueError as exc:
            pool_store.pause_account(
                run_id, account.account_id, reason="account_configuration_invalid"
            )
            store.add_event(
                "account_configuration_invalid",
                job_id=job.job_id,
                account_id=account.account_id,
                level="error",
                data={"error": str(exc)},
            )
            continue
        if not _ensure_account_gate(
            account,
            runtime_settings,
            store,
            pool_store,
            run_id,
            preflight_runner,
            proxy_health_runner,
        ):
            continue
        try:
            username, password = account_credentials(account)
        except ValueError as exc:
            pool_store.pause_account(run_id, account.account_id, reason="credentials_missing")
            store.add_event(
                "account_credentials_missing",
                job_id=job.job_id,
                account_id=account.account_id,
                level="error",
                data={"error": str(exc)},
            )
            continue

        attempt_id: str | None = None
        try:
            with browser_pool.session(
                account.account_id,
                runtime_settings,
                username,
                password,
            ) as scraper:
                store.set_account_check(account.account_id, session_checked=True)

                items = store.items(job.job_id, public=False)
                if not items:
                    attempt_id = store.begin_attempt(
                        job_id=job.job_id,
                        account_id=account.account_id,
                        quota_date=quota_date,
                        quota=config.quota_for(account),
                        run_id=run_id,
                        consume_quota=True,
                    )
                    if not attempt_id:
                        continue
                    try:
                        results = _search_job(scraper, job)
                    except SafetyStopException as exc:
                        if exc.reason == StopReason.AUTH_REQUIRED:
                            store.finish_attempt(
                                attempt_id,
                                status="auth_expired",
                                safety_stop=exc.reason.value,
                                error=str(exc),
                            )
                            with browser_pool.session(
                                account.account_id,
                                runtime_settings,
                                username,
                                password,
                                force=True,
                            ) as scraper:
                                pass
                            attempt_id = store.begin_attempt(
                                job_id=job.job_id,
                                account_id=account.account_id,
                                quota_date=quota_date,
                                quota=config.quota_for(account),
                                run_id=run_id,
                                consume_quota=True,
                            )
                            if not attempt_id:
                                continue
                            results = _search_job(scraper, job)
                        else:
                            raise
                    store.clear_external_outage_backoff()
                    store.finish_attempt(attempt_id, status="search_completed")
                    attempt_id = None
                    if job.source == "captcha_validation" and job.input.get(
                        "validation_only"
                    ):
                        from .captcha_budget import CaptchaBudgetStore

                        CaptchaBudgetStore(
                            settings.captcha_state_path,
                            daily_limit=settings.two_captcha_daily_limit,
                            circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
                            rejection_cooldown_seconds=(
                                settings.two_captcha_rejection_cooldown_seconds
                            ),
                        ).finish_manual_authorization(
                            account_id=account.account_id,
                            status="not_required",
                            reason="browser_token_accepted",
                        )
                        store.add_event(
                            "captcha_validation_completed",
                            job_id=job.job_id,
                            account_id=account.account_id,
                            data={"result_count": len(results)},
                        )
                        return store.finalize_job(job.job_id)
                    items = store.add_results(job.job_id, results)
                else:
                    attempt_id = store.begin_attempt(
                        job_id=job.job_id,
                        account_id=account.account_id,
                        quota_date=quota_date,
                        quota=config.quota_for(account),
                        run_id=run_id,
                        consume_quota=False,
                    )

                for item in items:
                    if item["status"] == "completed":
                        continue
                    if store.cancel_requested(job.job_id):
                        if attempt_id:
                            store.finish_attempt(attempt_id, status="cancelled")
                        return store.finalize_job(job.job_id)
                    sample_pages = (
                        (int(job.input.get("sample_pages") or 0) or None)
                        if job.source == "endurance"
                        else None
                    )
                    final_path = _expected_artifact_path(
                        settings.output_dir,
                        job.job_id,
                        item,
                        sample_pages=sample_pages,
                    )
                    store.mark_item_downloading(str(item["item_id"]), final_path)
                    try:
                        if final_path.exists():
                            expected_pages = int(item.get("expected_pages") or 1)
                            sha256, size = validate_pdf(
                                final_path, expected_pages=expected_pages
                            )
                            page_count = expected_pages
                        else:
                            try:
                                final_path, page_count, sha256, size = download_job_item(
                                    scraper,
                                    item,
                                    job_id=job.job_id,
                                    output_root=settings.output_dir,
                                    sample_pages=sample_pages,
                                    on_expected_pages=lambda count: store.set_item_expected_pages(
                                        str(item["item_id"]), count
                                    ),
                                )
                            except SafetyStopException as exc:
                                if exc.reason != StopReason.AUTH_REQUIRED:
                                    raise
                                with browser_pool.session(
                                    account.account_id,
                                    runtime_settings,
                                    username,
                                    password,
                                    force=True,
                                ) as scraper:
                                    pass
                                final_path, page_count, sha256, size = download_job_item(
                                    scraper,
                                    item,
                                    job_id=job.job_id,
                                    output_root=settings.output_dir,
                                    sample_pages=sample_pages,
                                    on_expected_pages=lambda count: store.set_item_expected_pages(
                                        str(item["item_id"]), count
                                    ),
                                )
                        store.complete_item(
                            str(item["item_id"]),
                            expected_pages=page_count,
                            output_path=final_path,
                            sha256=sha256,
                            bytes_count=size,
                        )
                    except SafetyStopException:
                        raise
                    except Exception as exc:
                        if _looks_like_connection_failure(exc):
                            raise
                        store.fail_item(
                            str(item["item_id"]), code="download_failed", message=str(exc)
                        )
                if attempt_id:
                    store.finish_attempt(attempt_id, status="completed")
                return store.finalize_job(job.job_id)
        except CredentialsRejectedError as exc:
            browser_pool.discard(account.account_id, status="credentials_invalid")
            if attempt_id:
                store.finish_attempt(attempt_id, status="credentials_invalid")
            pool_store.pause_account(run_id, account.account_id, reason="credentials_invalid")
            store.add_event(
                "account_credentials_invalid",
                job_id=job.job_id,
                account_id=account.account_id,
                level="error",
                data={
                    "http_status": exc.status,
                    "response_code": exc.response_code,
                },
            )
        except SafetyStopException as exc:
            if attempt_id:
                store.finish_attempt(
                    attempt_id,
                    status="safety_stop",
                    safety_stop=exc.reason.value,
                    error=str(exc),
                )
            outcome = _handle_account_safety_stop(
                exc,
                job_id=job.job_id,
                account=account,
                store=store,
                pool_store=pool_store,
                run_id=run_id,
                config=config,
                settings=settings,
                browser_pool=browser_pool,
                preflight_runner=preflight_runner,
                proxy_health_runner=proxy_health_runner,
            )
            if outcome in {"safety_stop", "cooldown"}:
                store.set_waiting(job.job_id, "queued", reason=exc.reason.value)
                return outcome
        except Exception as exc:
            safe_error = _redact_known_values(str(exc), username, password)
            if attempt_id:
                store.finish_attempt(attempt_id, status="failed", error=safe_error)
            connection_failure = _looks_like_connection_failure(exc)
            dataimpulse_failure = (
                _dataimpulse_failure_kind(exc)
                if _is_dataimpulse_account(account)
                else "unknown"
            )
            recovered_route = False
            if (
                _is_dataimpulse_account(account)
                and dataimpulse_failure != "provider_terminal"
                and (connection_failure or dataimpulse_failure == "transient_route")
            ):
                recovered_route = _rotate_dataimpulse_route(
                    account,
                    settings,
                    store,
                    pool_store,
                    run_id,
                    browser_pool,
                    preflight_runner,
                    proxy_health_runner,
                    reason="confirmed_connection_failure",
                )
            if dataimpulse_failure == "provider_terminal":
                pool_store.pause_account(
                    run_id,
                    account.account_id,
                    reason="dataimpulse_provider_terminal",
                    cooldown_seconds=None,
                )
                store.add_event(
                    "dataimpulse_provider_terminal",
                    job_id=job.job_id,
                    account_id=account.account_id,
                    level="error",
                    data={"action": "operator_required"},
                )
                continue
            if connection_failure and not recovered_route:
                browser_pool.discard(account.account_id, status="browser_context_failed")
            if recovered_route:
                store.add_event(
                    "account_route_recovered_after_failure",
                    job_id=job.job_id,
                    account_id=account.account_id,
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
            if gate_ok:
                pool_store.pause_account(
                    run_id,
                    account.account_id,
                    reason=(
                        "browser_context_failed"
                        if _looks_like_connection_failure(exc)
                        else "unexpected_worker_failure"
                    ),
                )
            store.add_event(
                "account_paused_after_failure",
                job_id=job.job_id,
                account_id=account.account_id,
                level="error",
                data={"error": safe_error},
            )


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
            pool_store.pause_account(run_id, account.account_id, reason="credentials_invalid")
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
            if exc.reason in GLOBAL_SAFETY_REASONS:
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
    return account_settings(settings, account, dataimpulse_port=port)


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
            if (
                replacement_preflight.ok
                and _egress_baseline_status(replacement_preflight) == "replacement_pending"
            ):
                if _is_dataimpulse_account(account):
                    from .proxy_provider import dataimpulse_configuration_health

                    provider_health = dataimpulse_configuration_health(
                        settings.dataimpulse_proxy_login,
                        settings.dataimpulse_proxy_password,
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
                preflight = replacement_preflight
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
        DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER,
    }


def _is_dataimpulse_account(account: PoolAccount) -> bool:
    return account.proxy_provider == DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER


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
) -> bool:
    """Validate then promote one new sticky port for one account only."""
    if not _is_dataimpulse_account(account) or account.dataimpulse_port is None:
        return False
    candidate = store.begin_dataimpulse_rotation(
        account.account_id,
        initial_port=account.dataimpulse_port,
        reason=reason,
        port_min=settings.dataimpulse_port_min,
        port_max=settings.dataimpulse_port_max,
        cooldown_seconds=settings.dataimpulse_rotation_cooldown_seconds,
        max_rotations_per_hour=settings.dataimpulse_max_rotations_per_hour,
    )
    if not candidate.get("ok"):
        blocked_reason = str(candidate.get("reason") or "proxy_rotation_blocked")
        if blocked_reason == "proxy_recovery_exhausted":
            pool_store.pause_account(
                run_id,
                account.account_id,
                reason=blocked_reason,
                cooldown_seconds=3600,
            )
        store.add_event(
            "dataimpulse_rotation_skipped",
            account_id=account.account_id,
            level="warning",
            data={"reason": blocked_reason},
        )
        return False
    pending_port = int(candidate["pending_port"])
    promoted = False
    try:
        candidate_settings = _runtime_account_settings(
            settings,
            account,
            store,
            dataimpulse_port=pending_port,
        )
        preflight = preflight_runner(
            candidate_settings,
            write_report=True,
            allow_baseline_replacement=True,
        )
        if not preflight.ok:
            raise SafetyStopException(
                StopReason.EGRESS_PREFLIGHT,
                "Candidate DataImpulse route failed preflight.",
                context="dataimpulse rotation",
            )
        proxy = proxy_health_runner(candidate_settings, write_report=True)
        if not proxy.ok:
            raise SafetyStopException(
                StopReason.PROXY_HEALTH,
                "Candidate DataImpulse route failed proxy health.",
                context="dataimpulse rotation",
            )
        egress_hash = str(preflight.report.get("egress_hash") or "")
        if not egress_hash:
            raise SafetyStopException(
                StopReason.EGRESS_PREFLIGHT,
                "Candidate DataImpulse route did not produce an egress identity.",
                context="dataimpulse rotation",
            )
        owner = store.egress_owner(egress_hash, exclude_account=account.account_id)
        if owner:
            raise SafetyStopException(
                StopReason.PROXY_HEALTH,
                "Candidate DataImpulse route is already assigned to another account.",
                context="dataimpulse rotation",
            )
        from .preflight import replace_egress_baseline

        replace_egress_baseline(
            candidate_settings,
            egress_hash=egress_hash,
            egress_country=str(preflight.report.get("egress_country") or ""),
        )
        route = store.finish_dataimpulse_rotation(account.account_id, promoted=True)
        promoted = True
        store.set_account_check(
            account.account_id,
            proxy_status="passed",
            egress_hash=egress_hash,
        )
        browser_pool.discard(account.account_id, status="proxy_route_rotated")
        username, password = account_credentials(account)
        promoted_settings = _runtime_account_settings(settings, account, store)
        with browser_pool.session(
            account.account_id,
            promoted_settings,
            username,
            password,
        ):
            pass
        pool_store.mark_account_available(run_id, account.account_id)
        store.add_event(
            "dataimpulse_route_rotated",
            account_id=account.account_id,
            data={
                "generation": int(route["generation"]),
                "sticky_port": int(route["active_port"]),
                "country_validated": True,
                "portal_reachable": True,
                "recaptcha_reachable": True,
                "unique_egress": True,
                "authenticated_form": True,
            },
        )
        return True
    except Exception as exc:
        if not promoted:
            store.finish_dataimpulse_rotation(
                account.account_id,
                promoted=False,
                error_code=(
                    exc.reason.value
                    if isinstance(exc, SafetyStopException)
                    else "candidate_failed"
                ),
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
                "reason": (
                    exc.reason.value
                    if isinstance(exc, SafetyStopException)
                    else "candidate_failed"
                )
            },
        )
        return False


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
    job_id: str,
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
        if (
            failures >= settings.dataimpulse_temp_unavailable_threshold
            and store.another_account_succeeded_recently(account.account_id)
        ):
            if _rotate_dataimpulse_route(
                account,
                settings,
                store,
                pool_store,
                run_id,
                browser_pool,
                preflight_runner,
                proxy_health_runner,
                reason="repeated_account_temporary_unavailable",
            ):
                store.add_event(
                    "dataimpulse_account_recovered",
                    job_id=job_id,
                    account_id=account.account_id,
                    data={"trigger": "temporary_unavailable_threshold"},
                )
                return "retry_account"
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
