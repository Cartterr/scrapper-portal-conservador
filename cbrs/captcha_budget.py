from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Santiago")


class CaptchaBudgetError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"CAPTCHA solver unavailable ({code}).")


@dataclass(frozen=True)
class CaptchaReservation:
    attempt_id: str
    quota_date: str


class CaptchaBudgetStore:
    """Persistent, sanitized guard around paid CAPTCHA task creation."""

    def __init__(
        self,
        path: Path,
        *,
        daily_limit: int,
        circuit_seconds: float,
        rejection_cooldown_seconds: float = 300.0,
    ) -> None:
        self.path = Path(path)
        self.daily_limit = int(daily_limit)
        self.circuit_seconds = float(circuit_seconds)
        self.rejection_cooldown_seconds = float(rejection_cooldown_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def reserve(
        self,
        *,
        account_id: str,
        action: str,
        provider: str = "2captcha",
        require_manual_authorization: bool = False,
    ) -> CaptchaReservation:
        now = _utc_now()
        quota_date = datetime.now(LOCAL_TZ).date().isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            circuit = db.execute(
                "SELECT open_until, disabled FROM captcha_solver_state WHERE singleton = 1"
            ).fetchone()
            if circuit and bool(circuit["disabled"]):
                raise CaptchaBudgetError("EXTERNAL_FALLBACK_DISABLED")
            if circuit and circuit["open_until"] and str(circuit["open_until"]) > now:
                raise CaptchaBudgetError("CIRCUIT_OPEN")
            rejected = db.execute(
                """
                SELECT paid_retry_blocked_until FROM captcha_attempts
                WHERE account_id = ? AND portal_status = 'rejected'
                  AND paid_retry_blocked_until > ?
                ORDER BY portal_finished_at DESC
                LIMIT 1
                """,
                (account_id, now),
            ).fetchone()
            if rejected:
                raise CaptchaBudgetError("RECENT_PORTAL_REJECTION")
            if require_manual_authorization:
                authorization = db.execute(
                    """
                    SELECT remaining, expires_at, event_id
                    FROM captcha_manual_authorizations
                    WHERE account_id = ?
                    """,
                    (account_id,),
                ).fetchone()
                if (
                    not authorization
                    or int(authorization["remaining"]) < 1
                    or not authorization["expires_at"]
                    or str(authorization["expires_at"]) <= now
                ):
                    if authorization and authorization["event_id"]:
                        db.execute(
                            """
                            UPDATE captcha_authorization_events
                            SET status = 'expired', reason = 'authorization_expired',
                                finished_at = ?
                            WHERE event_id = ? AND status = 'armed'
                            """,
                            (now, authorization["event_id"]),
                        )
                    db.execute(
                        "DELETE FROM captcha_manual_authorizations WHERE account_id = ?",
                        (account_id,),
                    )
                    raise CaptchaBudgetError("MANUAL_AUTH_REQUIRED")
            used = int(
                db.execute(
                    "SELECT COUNT(*) FROM captcha_attempts WHERE quota_date = ?",
                    (quota_date,),
                ).fetchone()[0]
            )
            if used >= self.daily_limit:
                raise CaptchaBudgetError("DAILY_LIMIT")
            if require_manual_authorization:
                event_id = str(authorization["event_id"] or "")
                db.execute(
                    "DELETE FROM captcha_manual_authorizations WHERE account_id = ?",
                    (account_id,),
                )
                if event_id:
                    db.execute(
                        """
                        UPDATE captcha_authorization_events
                        SET status = 'consumed', reason = 'paid_task_reserved',
                            finished_at = ?
                        WHERE event_id = ? AND status = 'armed'
                        """,
                        (now, event_id),
                    )
            attempt_id = f"captcha-{secrets.token_hex(8)}"
            db.execute(
                """
                INSERT INTO captcha_attempts(
                    attempt_id, quota_date, account_id, action, provider, status, started_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (attempt_id, quota_date, account_id, action[:80], provider[:40], now),
            )
        return CaptchaReservation(attempt_id, quota_date)

    def arm_manual(self, *, account_id: str) -> str:
        now = _utc_now()
        quota_date = datetime.now(LOCAL_TZ).date().isoformat()
        event_id = f"authorization-{secrets.token_hex(8)}"
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=15)
        ).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            circuit = db.execute(
                "SELECT open_until, disabled FROM captcha_solver_state WHERE singleton = 1"
            ).fetchone()
            if circuit and bool(circuit["disabled"]):
                raise CaptchaBudgetError("EXTERNAL_FALLBACK_DISABLED")
            if circuit and circuit["open_until"] and str(circuit["open_until"]) > now:
                raise CaptchaBudgetError("CIRCUIT_OPEN")
            rejected = db.execute(
                """
                SELECT paid_retry_blocked_until FROM captcha_attempts
                WHERE account_id = ? AND portal_status = 'rejected'
                  AND paid_retry_blocked_until > ?
                ORDER BY portal_finished_at DESC
                LIMIT 1
                """,
                (account_id, now),
            ).fetchone()
            if rejected:
                raise CaptchaBudgetError("RECENT_PORTAL_REJECTION")
            used = int(
                db.execute(
                    "SELECT COUNT(*) FROM captcha_attempts WHERE quota_date = ?",
                    (quota_date,),
                ).fetchone()[0]
            )
            if used >= self.daily_limit:
                raise CaptchaBudgetError("DAILY_LIMIT")
            existing = db.execute(
                """
                SELECT event_id FROM captcha_manual_authorizations
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            if existing and existing["event_id"]:
                db.execute(
                    """
                    UPDATE captcha_authorization_events
                    SET status = 'replaced', reason = 'new_authorization_issued',
                        finished_at = ?
                    WHERE event_id = ? AND status = 'armed'
                    """,
                    (now, existing["event_id"]),
                )
            db.execute(
                """
                INSERT INTO captcha_authorization_events(
                    event_id, account_id, status, armed_at, expires_at
                ) VALUES (?, ?, 'armed', ?, ?)
                """,
                (event_id, account_id, now, expires_at),
            )
            db.execute(
                """
                INSERT INTO captcha_manual_authorizations(
                    account_id, remaining, armed_at, expires_at, event_id
                ) VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    remaining = 1,
                    armed_at = excluded.armed_at,
                    expires_at = excluded.expires_at,
                    event_id = excluded.event_id
                """,
                (account_id, now, expires_at, event_id),
            )
        return event_id

    def finish_manual_authorization(
        self,
        *,
        account_id: str,
        status: str,
        reason: str,
    ) -> None:
        if status not in {"not_required", "cancelled", "expired"}:
            raise ValueError("Invalid manual CAPTCHA authorization outcome.")
        now = _utc_now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            authorization = db.execute(
                """
                SELECT armed_at, expires_at, event_id
                FROM captcha_manual_authorizations WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            if not authorization:
                return
            event_id = str(authorization["event_id"] or "")
            if event_id:
                db.execute(
                    """
                    UPDATE captcha_authorization_events
                    SET status = ?, reason = ?, finished_at = ?
                    WHERE event_id = ?
                    """,
                    (status, reason[:80], now, event_id),
                )
            else:
                db.execute(
                    """
                    INSERT INTO captcha_authorization_events(
                        event_id, account_id, status, reason,
                        armed_at, finished_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"authorization-{secrets.token_hex(8)}",
                        account_id,
                        status,
                        reason[:80],
                        authorization["armed_at"],
                        now,
                        authorization["expires_at"],
                    ),
                )
            db.execute(
                "DELETE FROM captcha_manual_authorizations WHERE account_id = ?",
                (account_id,),
            )

    def manual_armed(self, *, account_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT remaining FROM captcha_manual_authorizations
                WHERE account_id = ? AND expires_at > ?
                """,
                (account_id, _utc_now()),
            ).fetchone()
        return bool(row and int(row["remaining"]) > 0)

    def automatic_enabled(self) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT automatic_enabled FROM captcha_preferences WHERE singleton = 1"
            ).fetchone()
        return bool(row and int(row["automatic_enabled"]))

    def set_automatic_enabled(self, enabled: bool) -> bool:
        now = _utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO captcha_preferences(singleton, automatic_enabled, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    automatic_enabled = excluded.automatic_enabled,
                    updated_at = excluded.updated_at
                """,
                (1 if enabled else 0, now),
            )
        return bool(enabled)

    def record_diagnostic_attempt(
        self,
        *,
        account_id: str,
        action: str,
        provider: str = "2captcha",
        status: str,
        error_code: str | None = None,
        cost_usd: float | None = None,
        latency_seconds: float | None = None,
        portal_status: str = "not_submitted",
        portal_error_code: str | None = None,
    ) -> str:
        """Persist a sanitized paid-solver probe run outside the worker flow."""
        if status not in {"succeeded", "failed"}:
            raise ValueError("Invalid diagnostic CAPTCHA attempt status.")
        if portal_status not in {
            "accepted",
            "rejected",
            "indeterminate",
            "not_submitted",
        }:
            raise ValueError("Invalid diagnostic CAPTCHA portal outcome.")
        now = _utc_now()
        quota_date = datetime.now(LOCAL_TZ).date().isoformat()
        attempt_id = f"captcha-diagnostic-{secrets.token_hex(8)}"
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO captcha_attempts(
                    attempt_id, quota_date, account_id, action, provider, status,
                    error_code, cost_usd, latency_seconds, started_at, finished_at,
                    portal_status, portal_error_code, portal_finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    quota_date,
                    account_id[:80],
                    action[:80],
                    provider[:40],
                    status,
                    error_code[:80] if error_code else None,
                    cost_usd,
                    latency_seconds,
                    now,
                    now,
                    portal_status,
                    portal_error_code[:80] if portal_error_code else None,
                    now,
                ),
            )
        return attempt_id

    def finish(
        self,
        reservation: CaptchaReservation,
        *,
        status: str,
        error_code: str | None = None,
        cost_usd: float | None = None,
        latency_seconds: float | None = None,
        open_circuit: bool = False,
        disable_external: bool = False,
    ) -> None:
        now = _utc_now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE captcha_attempts
                SET status = ?, error_code = ?, cost_usd = ?, latency_seconds = ?,
                    finished_at = ?
                WHERE attempt_id = ?
                """,
                (
                    status,
                    error_code[:80] if error_code else None,
                    cost_usd,
                    latency_seconds,
                    now,
                    reservation.attempt_id,
                ),
            )
            if open_circuit or disable_external:
                open_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=self.circuit_seconds)
                ).isoformat(timespec="seconds")
                db.execute(
                    """
                    INSERT INTO captcha_solver_state(
                        singleton, open_until, reason, disabled, updated_at
                    ) VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        open_until = excluded.open_until,
                        reason = excluded.reason,
                        disabled = excluded.disabled,
                        updated_at = excluded.updated_at
                    """,
                    (
                        open_until,
                        error_code or "solver_failure",
                        int(disable_external),
                        now,
                    ),
                )

    def record_portal_outcome(
        self,
        attempt_id: str,
        *,
        status: str,
        error_code: str | None = None,
    ) -> None:
        """Attach the target portal result without persisting provider identifiers."""
        if status not in {"accepted", "rejected", "indeterminate", "not_submitted"}:
            raise ValueError("Invalid CAPTCHA portal outcome.")
        now = _utc_now()
        blocked_until = None
        if status == "rejected":
            blocked_until = (
                datetime.now(timezone.utc)
                + timedelta(seconds=self.rejection_cooldown_seconds)
            ).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute(
                """
                UPDATE captcha_attempts
                SET portal_status = ?, portal_error_code = ?,
                    portal_finished_at = ?, paid_retry_blocked_until = ?
                WHERE attempt_id = ?
                """,
                (
                    status,
                    error_code[:80] if error_code else None,
                    now,
                    blocked_until,
                    attempt_id,
                ),
            )

    def clear_solver_disable(self) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM captcha_solver_state WHERE singleton = 1"
            )

    def status(self) -> dict[str, object]:
        quota_date = datetime.now(LOCAL_TZ).date().isoformat()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS attempts,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS successes,
                       SUM(CASE WHEN portal_status = 'accepted' THEN 1 ELSE 0 END) AS accepted,
                       SUM(CASE WHEN portal_status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd,
                       COALESCE(AVG(latency_seconds), 0) AS avg_latency_seconds
                FROM captcha_attempts WHERE quota_date = ?
                """,
                (quota_date,),
            ).fetchone()
            circuit = db.execute(
                "SELECT open_until, reason, disabled FROM captcha_solver_state WHERE singleton = 1"
            ).fetchone()
            armed = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM captcha_manual_authorizations
                    WHERE remaining > 0 AND expires_at > ?
                    """,
                    (_utc_now(),),
                ).fetchone()[0]
            )
        return {
            "quota_date": quota_date,
            "daily_limit": self.daily_limit,
            "attempts": int(row["attempts"] or 0),
            "remaining": max(0, self.daily_limit - int(row["attempts"] or 0)),
            "successes": int(row["successes"] or 0),
            "accepted": int(row["accepted"] or 0),
            "rejected": int(row["rejected"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0), 6),
            "avg_latency_seconds": round(float(row["avg_latency_seconds"] or 0), 3),
            "circuit_open_until": str(circuit["open_until"]) if circuit else None,
            "circuit_reason": str(circuit["reason"]) if circuit else None,
            "external_fallback_disabled": bool(circuit["disabled"]) if circuit else False,
            "manual_authorizations_armed": armed,
            "automatic_enabled": self.automatic_enabled(),
        }

    def recent_attempts(self, *, limit: int = 50) -> list[dict[str, object]]:
        """Return recent solver activity without tokens, keys, or worker details."""
        limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT account_id, action, provider, status, error_code, cost_usd,
                       latency_seconds, started_at, finished_at,
                       portal_status, portal_error_code, portal_finished_at,
                       paid_retry_blocked_until
                FROM captcha_attempts
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_activity(self, *, limit: int = 50) -> list[dict[str, object]]:
        """Return paid tasks and manual authorizations as one sanitized timeline."""
        limit = max(1, min(int(limit), 100))
        activity = [
            {"kind": "solve", **attempt}
            for attempt in self.recent_attempts(limit=limit)
        ]
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT account_id, status, reason, armed_at, finished_at, expires_at
                FROM captcha_authorization_events
                ORDER BY armed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        for row in rows:
            status = str(row["status"])
            activity.append(
                {
                    "kind": "authorization",
                    "provider": "external",
                    "account_id": row["account_id"],
                    "action": "manual_authorization",
                    "status": status,
                    "error_code": row["reason"],
                    "cost_usd": None,
                    "latency_seconds": None,
                    "started_at": row["armed_at"],
                    "finished_at": row["finished_at"],
                    "portal_status": "not_required" if status == "not_required" else None,
                    "portal_error_code": row["reason"],
                    "portal_finished_at": row["finished_at"],
                    "paid_retry_blocked_until": None,
                    "authorization_expires_at": row["expires_at"],
                }
            )
        activity.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        return activity[:limit]

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS captcha_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    quota_date TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '2captcha',
                    status TEXT NOT NULL,
                    error_code TEXT,
                    cost_usd REAL,
                    latency_seconds REAL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    portal_status TEXT,
                    portal_error_code TEXT,
                    portal_finished_at TEXT,
                    paid_retry_blocked_until TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_captcha_attempts_quota
                ON captcha_attempts(quota_date, status);
                CREATE TABLE IF NOT EXISTS captcha_solver_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    open_until TEXT,
                    reason TEXT,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS captcha_manual_authorizations (
                    account_id TEXT PRIMARY KEY,
                    remaining INTEGER NOT NULL CHECK(remaining BETWEEN 0 AND 1),
                    armed_at TEXT NOT NULL,
                    expires_at TEXT,
                    event_id TEXT
                );
                CREATE TABLE IF NOT EXISTS captcha_authorization_events (
                    event_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    armed_at TEXT NOT NULL,
                    finished_at TEXT,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS captcha_preferences (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    automatic_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(captcha_solver_state)").fetchall()
            }
            if "disabled" not in columns:
                db.execute(
                    "ALTER TABLE captcha_solver_state ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
                )
            authorization_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(captcha_manual_authorizations)"
                ).fetchall()
            }
            if "expires_at" not in authorization_columns:
                db.execute(
                    "ALTER TABLE captcha_manual_authorizations ADD COLUMN expires_at TEXT"
                )
            if "event_id" not in authorization_columns:
                db.execute(
                    "ALTER TABLE captcha_manual_authorizations ADD COLUMN event_id TEXT"
                )
            attempt_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(captcha_attempts)").fetchall()
            }
            for name in (
                "provider",
                "portal_status",
                "portal_error_code",
                "portal_finished_at",
                "paid_retry_blocked_until",
            ):
                if name not in attempt_columns:
                    if name == "provider":
                        db.execute(
                            "ALTER TABLE captcha_attempts ADD COLUMN provider "
                            "TEXT NOT NULL DEFAULT '2captcha'"
                        )
                    else:
                        db.execute(f"ALTER TABLE captcha_attempts ADD COLUMN {name} TEXT")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
