from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    input: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    next_run_at: str | None
    last_error_code: str | None


class JobStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL UNIQUE,
                    email_hash TEXT NOT NULL,
                    display_label TEXT NOT NULL,
                    daily_budget INTEGER NOT NULL,
                    used_today INTEGER NOT NULL DEFAULT 0,
                    budget_date TEXT NOT NULL,
                    exhausted_date TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    profile_path TEXT NOT NULL,
                    last_refresh_status TEXT,
                    token_expires_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_run_at TEXT,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS search_results (
                    id INTEGER PRIMARY KEY,
                    query_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    public_json TEXT NOT NULL,
                    ticket_ref TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    page_count INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );

                CREATE TABLE IF NOT EXISTS live_safety_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL,
                    last_signal TEXT,
                    last_endpoint TEXT,
                    last_status INTEGER,
                    last_reason TEXT,
                    profile_path TEXT,
                    operator_action TEXT,
                    successful_requests INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_safety_events (
                    id INTEGER PRIMARY KEY,
                    event TEXT NOT NULL,
                    endpoint TEXT,
                    status INTEGER,
                    classified_code TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_safety_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    owner TEXT NOT NULL,
                    profile_path TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );
                """
            )
            now = utcnow()
            conn.execute(
                """
                INSERT OR IGNORE INTO live_safety_state(
                    id, state, operator_action, successful_requests, updated_at
                )
                VALUES (1, 'ok', NULL, 0, ?)
                """,
                (now,),
            )

    def upsert_account(self, *, label: str, email_hash: str, display_label: str, daily_budget: int) -> None:
        today = date.today().isoformat()
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts(label, email_hash, display_label, daily_budget, budget_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(label) DO UPDATE SET
                    email_hash=excluded.email_hash,
                    display_label=excluded.display_label,
                    daily_budget=excluded.daily_budget
                """,
                (label, email_hash, display_label, daily_budget, today, now),
            )

    def accounts(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            self._reset_budget_if_needed(conn)
            return list(conn.execute("SELECT * FROM accounts ORDER BY label"))

    def record_account_query(self, label: str) -> None:
        with self.connect() as conn:
            self._reset_budget_if_needed(conn)
            conn.execute(
                """
                UPDATE accounts
                SET used_today = used_today + 1,
                    exhausted_date = CASE
                        WHEN daily_budget > 0 AND used_today + 1 >= daily_budget THEN ?
                        ELSE exhausted_date
                    END
                WHERE label = ?
                """,
                (date.today().isoformat(), label),
            )

    def available_account_labels(self) -> list[str]:
        with self.connect() as conn:
            self._reset_budget_if_needed(conn)
            rows = conn.execute(
                """
                SELECT label FROM accounts
                WHERE (daily_budget <= 0 OR used_today < daily_budget)
                  AND exhausted_date IS NULL
                ORDER BY label
                """
            )
            return [row["label"] for row in rows]

    def update_session(self, *, profile_path: Path, last_refresh_status: str, token_expires_at: str | None) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute(
                """
                INSERT INTO sessions(profile_path, last_refresh_status, token_expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(profile_path), last_refresh_status, token_expires_at, now),
            )

    def create_job(
        self,
        kind: str,
        input_data: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
    ) -> int:
        now = utcnow()
        dedupe_key = dedupe_key or make_dedupe_key(kind, input_data)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    kind, input_json, dedupe_key, status, attempts, max_attempts,
                    next_run_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (kind, stable_json(input_data), dedupe_key, max_attempts, now, now, now),
            )
            row = conn.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            return int(row["id"])

    def claim_next(self) -> Job | None:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'retry')
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY created_at
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status='running', attempts=attempts+1, updated_at=?
                WHERE id=?
                """,
                (now, row["id"]),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return _row_to_job(updated)

    def complete_job(self, job_id: int) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='succeeded', updated_at=?, last_error_code=NULL, last_error_message=NULL WHERE id=?",
                (now, job_id),
            )

    def fail_job(self, job_id: int, *, code: str, message: str, retryable: bool) -> str:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            should_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
            status = "retry" if should_retry else "failed"
            next_run_at = (datetime.now(UTC) + _backoff(int(row["attempts"]))).isoformat() if should_retry else None
            conn.execute(
                """
                UPDATE jobs
                SET status=?, next_run_at=?, last_error_code=?, last_error_message=?, updated_at=?
                WHERE id=?
                """,
                (status, next_run_at, code, message[:1000], now, job_id),
            )
            return status

    def list_jobs(self, *, limit: int = 50) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            return [_row_to_job(row) for row in rows]

    def save_search_result(self, *, query_key: str, source: str, public_data: dict[str, Any], ticket_ref: str | None = None) -> int:
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO search_results(query_key, source, public_json, ticket_ref, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (query_key, source, stable_json(public_data), ticket_ref, now),
            )
            return int(cur.lastrowid)

    def save_artifact(
        self,
        *,
        path: Path,
        content_type: str,
        sha256: str,
        bytes_count: int,
        page_count: int | None,
        job_id: int | None = None,
    ) -> int:
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO artifacts(job_id, path, content_type, sha256, bytes, page_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, str(path), content_type, sha256, bytes_count, page_count, now),
            )
            return int(cur.lastrowid)

    def safety_status(self, *, profile_path: Path) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM live_safety_state WHERE id=1").fetchone()
            lock = conn.execute("SELECT * FROM live_safety_lock WHERE id=1").fetchone()
            if not row:
                self.init()
                row = conn.execute("SELECT * FROM live_safety_state WHERE id=1").fetchone()
            return {
                "state": row["state"],
                "last_signal": row["last_signal"],
                "last_endpoint": row["last_endpoint"],
                "last_status": row["last_status"],
                "last_reason": row["last_reason"],
                "profile_path": row["profile_path"] or str(profile_path),
                "operator_action": row["operator_action"],
                "successful_requests": row["successful_requests"],
                "last_success_at": row["last_success_at"],
                "updated_at": row["updated_at"],
                "lock": _lock_to_dict(lock),
            }

    def set_safety_state(
        self,
        *,
        state: str,
        signal: str,
        endpoint: str,
        status: int,
        reason: str,
        profile_path: Path,
        operator_action: str,
    ) -> None:
        now = utcnow()
        safe_reason = reason[:1000]
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_safety_state
                SET state=?, last_signal=?, last_endpoint=?, last_status=?, last_reason=?,
                    profile_path=?, operator_action=?, updated_at=?
                WHERE id=1
                """,
                (
                    state,
                    signal,
                    endpoint,
                    status,
                    safe_reason,
                    str(profile_path),
                    operator_action,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO live_safety_events(
                    event, endpoint, status, classified_code, message, created_at
                )
                VALUES ('state_changed', ?, ?, ?, ?, ?)
                """,
                (endpoint, status, signal, safe_reason, now),
            )

    def unlock_safety(self, *, reason: str) -> None:
        now = utcnow()
        message = reason[:1000]
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_safety_state
                SET state='ok', last_signal=NULL, last_endpoint=NULL, last_status=NULL,
                    last_reason=NULL, operator_action=NULL, updated_at=?
                WHERE id=1
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO live_safety_events(
                    event, endpoint, status, classified_code, message, created_at
                )
                VALUES ('manual_unlock', NULL, NULL, NULL, ?, ?)
                """,
                (message, now),
            )

    def record_live_success(self, *, endpoint: str, status: int) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_safety_state
                SET successful_requests=successful_requests + 1,
                    last_success_at=?, updated_at=?
                WHERE id=1
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO live_safety_events(
                    event, endpoint, status, classified_code, message, created_at
                )
                VALUES ('success', ?, ?, 'ok', 'successful live request', ?)
                """,
                (endpoint, status, now),
            )

    def record_safety_event(
        self,
        *,
        event: str,
        endpoint: str | None,
        status: int | None,
        classified_code: str | None,
        message: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO live_safety_events(
                    event, endpoint, status, classified_code, message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event, endpoint, status, classified_code, (message or "")[:1000], utcnow()),
            )

    def safety_events(self, *, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM live_safety_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return list(rows)

    def acquire_live_lock(
        self,
        *,
        owner: str,
        profile_path: Path,
        stale_after_seconds: int,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        now_text = now.isoformat()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM live_safety_lock WHERE id=1").fetchone()
            if row:
                heartbeat = datetime.fromisoformat(row["heartbeat_at"])
                stale = now - heartbeat > timedelta(seconds=stale_after_seconds)
                if not stale and row["owner"] != owner:
                    return {"acquired": False, "lock": _lock_to_dict(row)}
                conn.execute("DELETE FROM live_safety_lock WHERE id=1")
            conn.execute(
                """
                INSERT INTO live_safety_lock(id, owner, profile_path, acquired_at, heartbeat_at)
                VALUES (1, ?, ?, ?, ?)
                """,
                (owner, str(profile_path), now_text, now_text),
            )
            return {"acquired": True, "lock": {"owner": owner, "profile_path": str(profile_path)}}

    def release_live_lock(self, *, owner: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM live_safety_lock WHERE id=1 AND owner=?", (owner,))

    def _reset_budget_if_needed(self, conn: sqlite3.Connection) -> None:
        today = date.today().isoformat()
        conn.execute(
            """
            UPDATE accounts
            SET used_today=0, exhausted_date=NULL, budget_date=?
            WHERE budget_date != ?
            """,
            (today, today),
        )


def stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_dedupe_key(kind: str, input_data: dict[str, Any]) -> str:
    digest = hashlib.sha256(f"{kind}:{stable_json(input_data)}".encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _backoff(attempts: int) -> timedelta:
    minutes = min(60, max(1, 2 ** max(0, attempts - 1)))
    return timedelta(minutes=minutes)


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=int(row["id"]),
        kind=row["kind"],
        input=json.loads(row["input_json"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        next_run_at=row["next_run_at"],
        last_error_code=row["last_error_code"],
    )


def _lock_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "owner": row["owner"],
        "profile_path": row["profile_path"],
        "acquired_at": row["acquired_at"],
        "heartbeat_at": row["heartbeat_at"],
    }
