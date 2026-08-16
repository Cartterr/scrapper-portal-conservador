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
    utc_now,
)
from .browser_session import CredentialsRejectedError
from .config import SETTINGS, Settings
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
GLOBAL_SAFETY_REASONS = frozenset({StopReason.RATE_LIMIT, StopReason.WAF_CHALLENGE})
WORKER_LEASE_NAME = "portal_worker"
WORKER_STALE_SECONDS = 120
JOB_LEASE_SECONDS = 180
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


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    worker_id: str
    run_id: str | None
    status: str
    processed_jobs: int


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
                    proxy_status TEXT,
                    egress_hash TEXT,
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
                """
            )
            db.execute(
                """
                INSERT INTO schema_versions(component, version, applied_at)
                VALUES ('jobs', 2, ?)
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

    def create_job(
        self,
        *,
        kind: str,
        input_data: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        normalized = normalize_job_input(kind, input_data)
        key = _normalize_idempotency_key(idempotency_key)
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
                    job_id, kind, input_json, idempotency_key, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (job_id, kind, stable_json(normalized), key, now, now),
            )
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            self._add_event_db(db, job_id, "job_enqueued", {"kind": kind})
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
                ORDER BY created_at, rowid
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
                    INSERT INTO account_daily_usage(account_id, quota_date, used, last_used_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(account_id, quota_date) DO UPDATE SET
                        used = used + 1, last_used_at = excluded.last_used_at
                    """,
                    (account_id, quota_date, now),
                )
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
                {"quota_consumed": consume_quota},
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
            db.execute(
                """
                UPDATE job_attempts SET status = ?, safety_stop = ?,
                    error_message = ?, finished_at = ? WHERE attempt_id = ?
                """,
                (
                    status,
                    safety_stop,
                    redact_text(error)[:1000] if error else None,
                    utc_now(),
                    attempt_id,
                ),
            )

    def select_account(
        self,
        *,
        run_id: str,
        config: PoolConfig,
        quota_date: str,
        excluded: set[str] | None = None,
        preferred_account_id: str | None = None,
    ) -> PoolAccount | None:
        excluded = excluded or set()
        with self.connect() as db:
            rows = db.execute(
                "SELECT account_id, status FROM accounts WHERE run_id = ?", (run_id,)
            ).fetchall()
            statuses = {str(row["account_id"]): str(row["status"]) for row in rows}
            candidates: list[tuple[int, str, int, PoolAccount]] = []
            for index, account in enumerate(config.accounts):
                if (
                    not account.enabled
                    or account.account_id in excluded
                    or statuses.get(account.account_id) != "available"
                ):
                    continue
                used = self._account_usage_db(db, account.account_id, quota_date)
                if used >= config.quota_for(account):
                    continue
                last = db.execute(
                    """
                    SELECT last_used_at FROM account_daily_usage
                    WHERE account_id = ? AND quota_date = ?
                    """,
                    (account.account_id, quota_date),
                ).fetchone()
                candidates.append((used, str(last["last_used_at"] or "") if last else "", index, account))
            if not candidates:
                return None
            if preferred_account_id:
                preferred = next(
                    (value[3] for value in candidates if value[3].account_id == preferred_account_id),
                    None,
                )
                if preferred is not None:
                    return preferred
            return min(candidates, key=lambda value: (value[0], value[1], value[2]))[3]

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
                    account_id, session_checked_date, proxy_checked_date,
                    proxy_status, egress_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    session_checked_date = COALESCE(excluded.session_checked_date, session_checked_date),
                    proxy_checked_date = COALESCE(excluded.proxy_checked_date, proxy_checked_date),
                    proxy_status = COALESCE(excluded.proxy_status, proxy_status),
                    egress_hash = COALESCE(excluded.egress_hash, egress_hash),
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    today if session_checked else None,
                    today if proxy_status is not None else None,
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
                "global_safety_stop": self.get_control("global_safety_stop"),
            }

    def _job_payload(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_input: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "result_count": row["result_count"],
            "completed_items": row["completed_items"],
            "failed_items": row["failed_items"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "current_account_id": row["current_account_id"],
            "next_run_at": row["next_run_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
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
            ), 0) AS used
            """,
            (account_id, quota_date, account_id, quota_date),
        ).fetchone()
        return int(row["used"] or 0)


def default_job_store(settings: Settings = SETTINGS) -> JobStore:
    return JobStore(settings.profile_dir.parent / "pool" / "pool.sqlite3")


def normalize_job_input(kind: str, input_data: Mapping[str, Any]) -> dict[str, Any]:
    if kind == "text":
        query = str(input_data.get("text") or input_data.get("query") or "").strip()
        if not query:
            raise ValueError("text jobs require a non-empty text value")
        if len(query) > 500:
            raise ValueError("text value must be 500 characters or fewer")
        return {"text": query}
    if kind == "fna":
        try:
            foja = int(input_data["foja"])
            numero = int(input_data["numero"])
            year = int(input_data.get("year", input_data.get("ano")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("fna jobs require integer foja, numero, and year values") from exc
        if foja <= 0 or numero <= 0 or year < 1800 or year > 2200:
            raise ValueError("fna values are outside the accepted range")
        return {"foja": foja, "numero": numero, "year": year}
    raise ValueError("job kind must be 'text' or 'fna'")


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
    on_expected_pages: Callable[[int], None] | None = None,
) -> tuple[Path, int, str, int]:
    ticket = item.get("ticket_ref")
    if not ticket:
        raise RuntimeError("Search result did not include an inscription ticket.")
    sequence = int(item["sequence"])
    result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
    stem = _artifact_stem(sequence, result)
    job_dir = output_root / "jobs" / job_id
    image_dir = job_dir / ".staging" / str(item["item_id"])
    final_path = job_dir / f"{stem}.pdf"
    temp_path = job_dir / f".{stem}.{secrets.token_hex(4)}.tmp"
    job_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    _ticket_info, refs = scraper.get_image_refs(str(ticket))
    if not refs:
        raise RuntimeError("No image references returned for this inscription.")
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
    poll_seconds: float = 5.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    scraper_factory: Callable[..., Any] | None = None,
    preflight_runner: Callable[..., Any] | None = None,
    proxy_health_runner: Callable[..., Any] | None = None,
) -> WorkerResult:
    from .preflight import run_preflight
    from .proxy_health import run_proxy_health
    from .scraper import CBRSScraper

    config = config or load_account_pool_config(settings)
    store = store or default_job_store(settings)
    pool_store = pool_store or AccountPoolStore(store.path)
    scraper_factory = scraper_factory or CBRSScraper
    preflight_runner = preflight_runner or run_preflight
    proxy_health_runner = proxy_health_runner or run_proxy_health
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
    try:
        store.recover_abandoned_jobs()
        if store.get_control("global_safety_stop"):
            return WorkerResult(2, worker_id, None, "safety_stop", 0)
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
        _run_startup_gates(
            settings,
            config,
            store,
            pool_store,
            run_id,
            preflight_runner,
            proxy_health_runner,
            scraper_factory,
            runtime_headless,
        )

        while max_jobs is None or processed < max_jobs:
            pool_store.reset_quota_day(run_id, local_today())
            # A replacement worker can acquire the global lease moments before
            # the previous job lease expires. Recheck on every scheduler pass
            # so that job is requeued once it becomes stale without requiring
            # another process restart.
            store.recover_abandoned_jobs()
            if pool_store.stop_requested():
                final_status = "stopped"
                break
            if store.get_control("global_safety_stop"):
                final_status = "safety_stop"
                exit_code = 2
                break
            job = store.claim_next(worker_id)
            if job is None:
                if once:
                    final_status = "idle"
                    break
                pool_store.update_run(run_id, status="waiting", next_cycle_at="")
                sleep_fn(max(0.1, poll_seconds))
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
                headless=runtime_headless,
                scraper_factory=scraper_factory,
                preflight_runner=preflight_runner,
                proxy_health_runner=proxy_health_runner,
            )
            if outcome == "safety_stop":
                final_status = outcome
                exit_code = 2
                break
            if once:
                final_status = outcome
                break
            if outcome in {"waiting_capacity", "waiting_captcha"}:
                sleep_fn(max(0.1, poll_seconds))
            elif config.job_interval_max_seconds > 0:
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
    headless: bool,
    scraper_factory: Callable[..., Any],
    preflight_runner: Callable[..., Any],
    proxy_health_runner: Callable[..., Any],
) -> str:
    excluded: set[str] = set()
    restarted_accounts: set[str] = set()
    preferred_account_id: str | None = None
    quota_date = local_today()
    while True:
        if store.cancel_requested(job.job_id):
            return store.finalize_job(job.job_id)
        account = store.select_account(
            run_id=run_id,
            config=config,
            quota_date=quota_date,
            excluded=excluded,
            preferred_account_id=preferred_account_id,
        )
        preferred_account_id = None
        if account is None:
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
        runtime_settings = account_settings(settings, account)
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
            with scraper_factory(headless=headless, settings=runtime_settings) as scraper:
                try:
                    scraper.ensure_authenticated(username, password)
                except CredentialsRejectedError:
                    pool_store.pause_account(
                        run_id, account.account_id, reason="credentials_invalid"
                    )
                    store.add_event(
                        "account_credentials_invalid",
                        job_id=job.job_id,
                        account_id=account.account_id,
                        level="error",
                    )
                    continue
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
                            scraper.ensure_authenticated(username, password, force=True)
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
                    store.finish_attempt(attempt_id, status="search_completed")
                    attempt_id = None
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
                    final_path = _expected_artifact_path(
                        settings.output_dir, job.job_id, item
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
                                    on_expected_pages=lambda count: store.set_item_expected_pages(
                                        str(item["item_id"]), count
                                    ),
                                )
                            except SafetyStopException as exc:
                                if exc.reason != StopReason.AUTH_REQUIRED:
                                    raise
                                scraper.ensure_authenticated(username, password, force=True)
                                final_path, page_count, sha256, size = download_job_item(
                                    scraper,
                                    item,
                                    job_id=job.job_id,
                                    output_root=settings.output_dir,
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
        except CredentialsRejectedError:
            if attempt_id:
                store.finish_attempt(attempt_id, status="credentials_invalid")
            pool_store.pause_account(run_id, account.account_id, reason="credentials_invalid")
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
            )
            if outcome == "safety_stop":
                store.set_waiting(job.job_id, "queued", reason=exc.reason.value)
                return outcome
        except Exception as exc:
            safe_error = _redact_known_values(str(exc), username, password)
            if attempt_id:
                store.finish_attempt(attempt_id, status="failed", error=safe_error)
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
            transient_browser_failure = _looks_like_connection_failure(exc)
            if (
                gate_ok
                and transient_browser_failure
                and account.account_id not in restarted_accounts
            ):
                restarted_accounts.add(account.account_id)
                excluded.discard(account.account_id)
                preferred_account_id = account.account_id
                store.add_event(
                    "account_browser_context_restarting",
                    job_id=job.job_id,
                    account_id=account.account_id,
                    level="warning",
                )
                continue
            if gate_ok:
                pool_store.pause_account(
                    run_id, account.account_id, reason="unexpected_worker_failure"
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
    scraper_factory: Callable[..., Any],
    headless: bool,
) -> None:
    states = {
        str(row["account_id"]): str(row["status"])
        for row in pool_store.accounts(run_id)
    }
    for account in config.accounts:
        if not account.enabled:
            continue
        if states.get(account.account_id) == CAPTCHA_PENDING_STATUS:
            continue
        runtime_settings = account_settings(settings, account)
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
            with scraper_factory(headless=headless, settings=runtime_settings) as scraper:
                scraper.ensure_authenticated(username, password)
            store.set_account_check(account.account_id, session_checked=True)
            pool_store.mark_account_available(run_id, account.account_id)
        except CredentialsRejectedError:
            pool_store.pause_account(run_id, account.account_id, reason="credentials_invalid")
            store.add_event(
                "account_credentials_invalid",
                account_id=account.account_id,
                level="error",
            )
        except SafetyStopException as exc:
            if exc.reason in GLOBAL_SAFETY_REASONS:
                store.set_control("global_safety_stop", exc.reason.value)
            elif exc.reason == StopReason.CAPTCHA_REJECTED:
                pool_store.mark_account_captcha_pending(
                    run_id, account.account_id, reason=exc.reason.value
                )
            else:
                pool_store.pause_account(run_id, account.account_id, reason=exc.reason.value)
            store.add_event(
                "account_startup_auth_stopped",
                account_id=account.account_id,
                level="warning",
                data={"reason": exc.reason.value},
            )
        except Exception as exc:
            pool_store.pause_account(run_id, account.account_id, reason="startup_auth_failed")
            store.add_event(
                "account_startup_auth_failed",
                account_id=account.account_id,
                level="error",
                data={
                    "error": _redact_known_values(str(exc), username, password)
                },
            )


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
    if not force and check.get("proxy_checked_date") == local_today() and check.get(
        "proxy_status"
    ) == "passed":
        return True
    try:
        preflight = preflight_runner(settings, write_report=True)
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


def _handle_account_safety_stop(
    exc: SafetyStopException,
    *,
    job_id: str,
    account: PoolAccount,
    store: JobStore,
    pool_store: AccountPoolStore,
    run_id: str,
) -> str:
    if exc.reason in GLOBAL_SAFETY_REASONS:
        store.set_control("global_safety_stop", exc.reason.value)
        store.add_event(
            "global_safety_stop",
            job_id=job_id,
            account_id=account.account_id,
            level="error",
            data={"reason": exc.reason.value},
        )
        return "safety_stop"
    if exc.reason == StopReason.CAPTCHA_REJECTED:
        pool_store.mark_account_captcha_pending(
            run_id, account.account_id, reason=exc.reason.value
        )
    else:
        pool_store.pause_account(run_id, account.account_id, reason=exc.reason.value)
    store.add_event(
        "account_safety_stop",
        job_id=job_id,
        account_id=account.account_id,
        level="warning",
        data={"reason": exc.reason.value},
    )
    return "retry_account"


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


def _artifact_stem(sequence: int, result: Mapping[str, Any]) -> str:
    foja = result.get("foja", "unknown")
    numero = result.get("numero", result.get("num", "unknown"))
    year = result.get("ano", result.get("year", "unknown"))
    return f"{sequence:04d}_{_safe_part(foja)}_{_safe_part(numero)}_{_safe_part(year)}"


def _expected_artifact_path(output_root: Path, job_id: str, item: Mapping[str, Any]) -> Path:
    result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
    return output_root / "jobs" / job_id / f"{_artifact_stem(int(item['sequence']), result)}.pdf"


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
    )


def _utc_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(
        microsecond=0
    ).isoformat()
