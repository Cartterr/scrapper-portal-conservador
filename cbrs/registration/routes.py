"""Bounded registration-only candidate ledger; production state is read-only."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class RouteRecoveryStop(RuntimeError):
    pass


def production_exclusions(path: Path) -> tuple[set[int], set[str]]:
    """Fail closed if live routes cannot be read; never initialize/migrate them."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(account_proxy_routes)")}
            port_columns = "active_port, pending_port" if "pending_port" in columns else "active_port"
            ports = {int(value) for row in db.execute(f"SELECT {port_columns} FROM account_proxy_routes")
                     for value in row if value is not None}
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            exits = set()
            if "account_checks" in tables:
                exits.update(r[0] for r in db.execute("SELECT egress_hash FROM account_checks WHERE egress_hash IS NOT NULL"))
            for table, date in (("account_egress_history", "observed_at"), ("proxy_candidate_exits", "failed_at")):
                if table in tables:
                    exits.update(r[0] for r in db.execute(f"SELECT egress_hash FROM {table} WHERE {date} >= ?", (cutoff,)))
            return ports, exits
    except (sqlite3.Error, OSError, ValueError):
        raise RouteRecoveryStop("Production route exclusions unavailable; registration recovery stopped.") from None


class RegistrationRouteStore:
    def __init__(self, path: Path, *, hourly_limit: int, daily_submit_limit: int):
        self.path = path
        self.hourly_limit = hourly_limit
        self.daily_submit_limit = daily_submit_limit
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS registration_routes (
                id INTEGER PRIMARY KEY, identity_key TEXT NOT NULL,
                port INTEGER, egress_hash TEXT, outcome TEXT NOT NULL DEFAULT 'reserved',
                submitted INTEGER NOT NULL DEFAULT 0, http_status INTEGER, response_code TEXT,
                started_at TEXT NOT NULL, finished_at TEXT)""")

    def exclusions(self) -> tuple[set[int], set[str]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT port,egress_hash FROM registration_routes WHERE started_at >= ?", (cutoff,)).fetchall()
        return {int(p) for p, _ in rows if p is not None}, {e for _, e in rows if e}

    def reserve(self, identity_key: str) -> int:
        now = datetime.now(timezone.utc)
        hour = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        day = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("SELECT COUNT(*) FROM registration_routes WHERE started_at >= ?", (hour,)).fetchone()[0]
            submits = db.execute("SELECT COUNT(*) FROM registration_routes WHERE identity_key=? AND submitted=1 AND started_at>=?", (identity_key, day)).fetchone()[0]
            if used >= self.hourly_limit:
                raise RouteRecoveryStop("Registration hourly candidate allowance exhausted.")
            if submits >= self.daily_submit_limit:
                raise RouteRecoveryStop("Registration daily identity submission allowance exhausted.")
            return db.execute("INSERT INTO registration_routes(identity_key,started_at) VALUES (?,?)",
                              (identity_key, now.isoformat(timespec="seconds"))).lastrowid

    def assign(self, attempt: int, *, port: int, egress_hash: str | None = None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE registration_routes SET port=?,egress_hash=? WHERE id=?", (port, egress_hash, attempt))

    def mark_submitted(self, attempt: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute("SELECT identity_key FROM registration_routes WHERE id=?", (attempt,)).fetchone()[0]
            day = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
            used = db.execute("SELECT COUNT(*) FROM registration_routes WHERE identity_key=? AND submitted=1 AND started_at>=?", (identity, day)).fetchone()[0]
            if used >= self.daily_submit_limit:
                raise RouteRecoveryStop("Registration daily identity submission allowance exhausted.")
            db.execute("UPDATE registration_routes SET submitted=1 WHERE id=?", (attempt,))

    def finish(self, attempt: int, outcome: str, *, http_status: int | None = None, response_code: str | None = None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE registration_routes SET outcome=?,http_status=?,response_code=?,finished_at=? WHERE id=?",
                       (outcome, http_status, response_code, datetime.now(timezone.utc).isoformat(timespec="seconds"), attempt))
