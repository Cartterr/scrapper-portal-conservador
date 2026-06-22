from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from zoneinfo import ZoneInfo

from .config import SETTINGS, Settings
from .safety import redact, redact_text
from .validation import (
    ValidationRunResult,
    finish_validation_report,
    new_validation_report,
    run_controlled_validation,
    write_validation_report,
)

DEFAULT_ACCOUNT_POOL_CONFIG = ".cbrs/account-pool.json"
DEFAULT_DAILY_QUOTA_PER_ACCOUNT = 20
DEFAULT_INTERVAL_MINUTES = 5.0
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8765
DEFAULT_TARGET_LABEL = "default_safe_query"
DEFAULT_TARGET_QUERY = "BANCO DE CHILE"
LOCAL_TZ = ZoneInfo("America/Santiago")
SECRET_ACCOUNT_KEYS = {
    "email",
    "password",
    "pass",
    "username",
    "rut",
    "token",
    "cookie",
    "credentials",
}


@dataclass(frozen=True)
class PoolAccount:
    account_id: str
    label: str
    enabled: bool = True


@dataclass(frozen=True)
class PoolTarget:
    label: str
    kind: str
    query: str | None = None
    foja: int | None = None
    numero: int | None = None
    ano: int | None = None


@dataclass(frozen=True)
class PoolConfig:
    accounts: tuple[PoolAccount, ...]
    daily_quota_per_account: int
    interval_minutes: float
    dashboard_host: str
    dashboard_port: int
    targets: tuple[PoolTarget, ...]

    @property
    def pool_daily_quota(self) -> int:
        return self.daily_quota_per_account * len([a for a in self.accounts if a.enabled])


@dataclass(frozen=True)
class PoolRunResult:
    exit_code: int
    run_id: str
    status: str
    dashboard_url: str | None = None


DEFAULT_ACCOUNTS = (
    PoolAccount("ejecutivo_1", "Ejecutivo 1"),
    PoolAccount("ejecutivo_2", "Ejecutivo 2"),
    PoolAccount("ejecutivo_3", "Ejecutivo 3"),
)


class AccountPoolStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    next_cycle_at TEXT,
                    blocked_reason TEXT,
                    dry_run INTEGER NOT NULL,
                    dashboard_url TEXT,
                    interval_minutes REAL NOT NULL,
                    daily_quota_per_account INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    paused_reason TEXT,
                    paused_at TEXT,
                    quota_date TEXT NOT NULL,
                    daily_quota INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, account_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    account_label TEXT NOT NULL,
                    quota_date TEXT NOT NULL,
                    target_label TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_seconds REAL,
                    result_count INTEGER,
                    download_count INTEGER NOT NULL DEFAULT 0,
                    validation_report_path TEXT,
                    preflight_report_path TEXT,
                    artifact_path TEXT,
                    artifact_size_bytes INTEGER,
                    safety_stop TEXT,
                    error TEXT,
                    next_cycle_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    cycle_id TEXT,
                    account_id TEXT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT
                );

                CREATE TABLE IF NOT EXISTS control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create_run(
        self,
        *,
        run_id: str,
        dry_run: bool,
        config: PoolConfig,
        dashboard_url: str | None,
    ) -> None:
        now = utc_now()
        quota_date = local_today()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO runs (
                    run_id, started_at, status, heartbeat_at, dry_run,
                    dashboard_url, interval_minutes, daily_quota_per_account
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now,
                    "running",
                    now,
                    1 if dry_run else 0,
                    dashboard_url,
                    config.interval_minutes,
                    config.daily_quota_per_account,
                ),
            )
            db.executemany(
                """
                INSERT INTO accounts (
                    run_id, account_id, label, status, quota_date,
                    daily_quota, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        account.account_id,
                        account.label,
                        "available" if account.enabled else "disabled",
                        quota_date,
                        config.daily_quota_per_account,
                        now,
                    )
                    for account in config.accounts
                ],
            )

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        next_cycle_at: str | None = None,
        blocked_reason: str | None = None,
        finished: bool = False,
    ) -> None:
        fields = ["heartbeat_at = ?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if next_cycle_at is not None:
            fields.append("next_cycle_at = ?")
            values.append(next_cycle_at)
        if blocked_reason is not None:
            fields.append("blocked_reason = ?")
            values.append(redact_text(blocked_reason))
        if finished:
            fields.append("finished_at = ?")
            values.append(utc_now())
        values.append(run_id)
        with self._connect() as db:
            db.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id = ?", values)

    def add_event(
        self,
        run_id: str,
        *,
        message: str,
        level: str = "info",
        cycle_id: str | None = None,
        account_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO events (
                    run_id, cycle_id, account_id, created_at, level, message, data_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    cycle_id,
                    account_id,
                    utc_now(),
                    level,
                    message,
                    json.dumps(redact(data or {}), ensure_ascii=False),
                ),
            )

    def start_cycle(
        self,
        *,
        cycle_id: str,
        run_id: str,
        sequence: int,
        account: PoolAccount,
        target: PoolTarget,
        quota_date: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO cycles (
                    cycle_id, run_id, sequence, account_id, account_label,
                    quota_date, target_label, target_kind, action, status, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    run_id,
                    sequence,
                    account.account_id,
                    account.label,
                    quota_date,
                    target.label,
                    target.kind,
                    "full_flow_download_first",
                    "running",
                    utc_now(),
                ),
            )

    def finish_cycle(
        self,
        cycle_id: str,
        *,
        status: str,
        started_at_monotonic: float,
        result_count: int | None = None,
        download_count: int = 0,
        validation_report_path: Path | None = None,
        preflight_report_path: Path | None = None,
        artifact_path: Path | None = None,
        artifact_size_bytes: int | None = None,
        safety_stop: str | None = None,
        error: str | None = None,
        next_cycle_at: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE cycles
                SET status = ?,
                    finished_at = ?,
                    duration_seconds = ?,
                    result_count = ?,
                    download_count = ?,
                    validation_report_path = ?,
                    preflight_report_path = ?,
                    artifact_path = ?,
                    artifact_size_bytes = ?,
                    safety_stop = ?,
                    error = ?,
                    next_cycle_at = ?
                WHERE cycle_id = ?
                """,
                (
                    status,
                    utc_now(),
                    round(time.monotonic() - started_at_monotonic, 3),
                    result_count,
                    download_count,
                    str(validation_report_path) if validation_report_path else None,
                    str(preflight_report_path) if preflight_report_path else None,
                    str(artifact_path) if artifact_path else None,
                    artifact_size_bytes,
                    safety_stop,
                    redact_text(error) if error else None,
                    next_cycle_at,
                    cycle_id,
                ),
            )

    def pause_account(
        self,
        run_id: str,
        account_id: str,
        *,
        reason: str,
    ) -> None:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                UPDATE accounts
                SET status = ?, paused_reason = ?, paused_at = ?, updated_at = ?
                WHERE run_id = ? AND account_id = ?
                """,
                ("paused", redact_text(reason), now, now, run_id, account_id),
            )

    def latest_run(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def accounts(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM accounts
                WHERE run_id = ?
                ORDER BY rowid
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_by_account(self, run_id: str, quota_date: str) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT account_id, COUNT(*) AS count
                FROM cycles
                WHERE quota_date = ? AND status = 'passed'
                GROUP BY account_id
                """,
                (quota_date,),
            ).fetchall()
        return {str(row["account_id"]): int(row["count"]) for row in rows}

    def recent_cycles(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE run_id = ?" if run_id else ""
        params: tuple[Any, ...] = (run_id, limit) if run_id else (limit,)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT * FROM cycles
                {where}
                ORDER BY started_at DESC, sequence DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_events(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE run_id = ?" if run_id else ""
        params: tuple[Any, ...] = (run_id, limit) if run_id else (limit,)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT * FROM events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def artifacts(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "AND run_id = ?" if run_id else ""
        params: tuple[Any, ...] = (run_id, limit) if run_id else (limit,)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT cycle_id, run_id, sequence, account_id, account_label,
                       target_label, artifact_path, artifact_size_bytes, finished_at
                FROM cycles
                WHERE artifact_path IS NOT NULL
                {where}
                ORDER BY finished_at DESC, sequence DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def request_stop(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO control (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                ("stop_requested_at", utc_now(), utc_now()),
            )

    def clear_stop_request(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM control WHERE key = ?", ("stop_requested_at",))

    def stop_requested(self) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM control WHERE key = ?",
                ("stop_requested_at",),
            ).fetchone()
        return row is not None

    def control_state(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT key, value, updated_at FROM control").fetchall()
        return {
            str(row["key"]): {"value": row["value"], "updated_at": row["updated_at"]}
            for row in rows
        }

    def export_snapshot(self, config: PoolConfig | None = None) -> dict[str, Any]:
        return redact(
            {
                "schema": "cbrs-account-pool-export-v1",
                "exported_at": utc_now(),
                "latest_run": self.latest_run(),
                "status": dashboard_status(self, config=config),
            }
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()


def load_account_pool_config(
    settings: Settings = SETTINGS,
    *,
    path: Path | None = None,
) -> PoolConfig:
    path = path or settings.profile_dir.parent / "account-pool.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    accounts = tuple(_parse_accounts(raw.get("accounts")))
    targets = tuple(_parse_targets(raw.get("targets")))
    config = PoolConfig(
        accounts=accounts or DEFAULT_ACCOUNTS,
        daily_quota_per_account=int(
            raw.get("daily_quota_per_account", DEFAULT_DAILY_QUOTA_PER_ACCOUNT)
        ),
        interval_minutes=float(raw.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)),
        dashboard_host=str(raw.get("dashboard_host", DEFAULT_DASHBOARD_HOST)),
        dashboard_port=int(raw.get("dashboard_port", DEFAULT_DASHBOARD_PORT)),
        targets=targets
        or (
            PoolTarget(label=DEFAULT_TARGET_LABEL, kind="text", query=DEFAULT_TARGET_QUERY),
        ),
    )
    if config.daily_quota_per_account <= 0:
        raise ValueError("daily_quota_per_account must be greater than zero")
    if config.interval_minutes < 0:
        raise ValueError("interval_minutes must be zero or greater")
    if not any(account.enabled for account in config.accounts):
        raise ValueError("account pool requires at least one enabled account")
    return config


def account_settings(settings: Settings, account: PoolAccount) -> Settings:
    profile_root = settings.profile_dir.parent / "accounts" / account.account_id
    return replace(settings, profile_dir=(profile_root / "chrome-profile").resolve())


def default_pool_store(settings: Settings = SETTINGS) -> AccountPoolStore:
    return AccountPoolStore(settings.profile_dir.parent / "pool" / "pool.sqlite3")


def select_next_account(
    store: AccountPoolStore,
    run_id: str,
    config: PoolConfig,
    *,
    quota_date: str | None = None,
) -> PoolAccount | None:
    quota_date = quota_date or local_today()
    account_rows = {row["account_id"]: row for row in store.accounts(run_id)}
    usage = store.usage_by_account(run_id, quota_date)
    candidates: list[tuple[int, int, PoolAccount]] = []
    for index, account in enumerate(config.accounts):
        row = account_rows.get(account.account_id)
        if not account.enabled or not row or row.get("status") != "available":
            continue
        used = usage.get(account.account_id, 0)
        if used >= int(row.get("daily_quota") or config.daily_quota_per_account):
            continue
        candidates.append((used, index, account))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]))[0][2]


def choose_target(config: PoolConfig, sequence: int) -> PoolTarget:
    return config.targets[(sequence - 1) % len(config.targets)]


def run_account_pool(
    *,
    settings: Settings = SETTINGS,
    config: PoolConfig | None = None,
    store: AccountPoolStore | None = None,
    dry_run: bool = False,
    max_cycles: int | None = None,
    dashboard: bool = False,
    headless: bool | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    validation_runner: Callable[..., ValidationRunResult] = run_controlled_validation,
    on_dashboard_start: Callable[[str], None] | None = None,
) -> PoolRunResult:
    from .account_pool_dashboard import start_pool_dashboard

    config = config or load_account_pool_config(settings)
    store = store or default_pool_store(settings)
    run_id = (
        f"pool-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{secrets.token_hex(3)}"
    )
    dashboard_server = None
    dashboard_url = None
    if dashboard:
        dashboard_server = start_pool_dashboard(
            store,
            settings=settings,
            config=config,
            host=config.dashboard_host,
            port=config.dashboard_port,
        )
        dashboard_url = dashboard_server.url
        if on_dashboard_start:
            on_dashboard_start(dashboard_url)

    store.clear_stop_request()
    store.create_run(run_id=run_id, dry_run=dry_run, config=config, dashboard_url=dashboard_url)
    store.add_event(
        run_id,
        message="pool run started",
        data={"dry_run": dry_run, "dashboard_url": dashboard_url},
    )

    sequence = 0
    final_status = "completed"
    exit_code = 0
    try:
        while max_cycles is None or sequence < max_cycles:
            if store.stop_requested():
                final_status = "stopped"
                store.add_event(run_id, message="pool run stop requested")
                break

            quota_date = local_today()
            account = select_next_account(store, run_id, config, quota_date=quota_date)
            if account is None:
                final_status = "waiting_capacity"
                store.update_run(
                    run_id,
                    status="waiting_capacity",
                    next_cycle_at=next_quota_reset_at(),
                    blocked_reason="no available account capacity",
                )
                store.add_event(
                    run_id,
                    level="warning",
                    message="pool waiting capacity",
                    data={"quota_date": quota_date},
                )
                if max_cycles is not None:
                    break
                if _sleep_with_heartbeat(store, run_id, 60.0, sleep_fn=sleep_fn):
                    final_status = "stopped"
                    break
                continue

            sequence += 1
            target = choose_target(config, sequence)
            cycle_id = f"{run_id}-cycle-{sequence:04d}"
            cycle_output_dir = (
                settings.output_dir
                / "pool"
                / run_id
                / account.account_id
                / f"cycle-{sequence:04d}"
            )
            runtime_settings = account_settings(settings, account)
            started = time.monotonic()
            store.update_run(run_id, status="running", next_cycle_at="")
            store.start_cycle(
                cycle_id=cycle_id,
                run_id=run_id,
                sequence=sequence,
                account=account,
                target=target,
                quota_date=quota_date,
            )
            store.add_event(
                run_id,
                cycle_id=cycle_id,
                account_id=account.account_id,
                message="pool cycle started",
                data={"target_label": target.label, "target_kind": target.kind},
            )

            with _heartbeat_while_cycle_runs(store, run_id):
                result = (
                    _dry_run_validation(runtime_settings, target, cycle_output_dir)
                    if dry_run
                    else validation_runner(
                        settings=runtime_settings,
                        search_kind=target.kind,
                        query=target.query,
                        foja=target.foja,
                        numero=target.numero,
                        ano=target.ano,
                        download_first=True,
                        output_dir=cycle_output_dir,
                        keep_images=False,
                        headless=headless,
                    )
                )

            cycle_status = _cycle_status(result)
            store.finish_cycle(
                cycle_id,
                status=cycle_status,
                started_at_monotonic=started,
                result_count=result.result_count,
                download_count=1 if result.pdf_path else 0,
                validation_report_path=result.report_path,
                preflight_report_path=result.preflight_report_path,
                artifact_path=result.pdf_path,
                artifact_size_bytes=result.pdf_size_bytes,
                safety_stop=result.safety_stop,
                error=result.error,
            )
            store.add_event(
                run_id,
                cycle_id=cycle_id,
                account_id=account.account_id,
                level="error" if result.exit_code else "info",
                message=f"pool cycle {cycle_status}",
                data={
                    "status": result.status,
                    "result_count": result.result_count,
                    "safety_stop": result.safety_stop,
                    "error": result.error,
                },
            )

            if result.exit_code == 2:
                reason = result.safety_stop or result.error or "safety_stop"
                store.pause_account(run_id, account.account_id, reason=reason)
                store.add_event(
                    run_id,
                    account_id=account.account_id,
                    level="warning",
                    message="pool account paused",
                    data={"reason": reason},
                )

            if store.stop_requested():
                final_status = "stopped"
                store.add_event(run_id, message="pool run stop requested")
                break

            if max_cycles is not None and sequence >= max_cycles:
                break

            interval = 0.0 if dry_run else config.interval_minutes * 60
            next_cycle_at = utc_from_epoch(time.time() + interval)
            store.finish_cycle(
                cycle_id,
                status=cycle_status,
                started_at_monotonic=started,
                result_count=result.result_count,
                download_count=1 if result.pdf_path else 0,
                validation_report_path=result.report_path,
                preflight_report_path=result.preflight_report_path,
                artifact_path=result.pdf_path,
                artifact_size_bytes=result.pdf_size_bytes,
                safety_stop=result.safety_stop,
                error=result.error,
                next_cycle_at=next_cycle_at,
            )
            store.update_run(run_id, status="waiting", next_cycle_at=next_cycle_at)
            if _sleep_with_heartbeat(store, run_id, interval, sleep_fn=sleep_fn):
                final_status = "stopped"
                store.add_event(run_id, message="pool run stop requested")
                break
    except KeyboardInterrupt:
        final_status = "stopped"
        exit_code = 0
        store.add_event(run_id, message="pool run stopped by operator")
    finally:
        if final_status == "waiting_capacity" and max_cycles is None:
            store.update_run(run_id, status=final_status)
        else:
            store.update_run(run_id, status=final_status, finished=True)
        if dashboard_server and final_status == "waiting_capacity" and max_cycles is None:
            _hold_dashboard(store, run_id, status="waiting_capacity", sleep_fn=sleep_fn)
            dashboard_server.stop()
        elif dashboard_server:
            dashboard_server.stop()

    return PoolRunResult(
        exit_code=exit_code,
        run_id=run_id,
        status=final_status,
        dashboard_url=dashboard_url,
    )


def dashboard_status(
    store: AccountPoolStore,
    *,
    config: PoolConfig | None = None,
) -> dict[str, Any]:
    run = store.latest_run()
    if not run:
        quota = (config.pool_daily_quota if config else DEFAULT_DAILY_QUOTA_PER_ACCOUNT * 3)
        return {
            "schema": "cbrs-account-pool-status-v1",
            "status": "not_started",
            "run": None,
            "pool": {
                "daily_quota": quota,
                "used_today": 0,
                "remaining_today": quota,
                "paused_accounts": 0,
                "available_accounts": len(config.accounts) if config else 3,
            },
            "accounts": _default_account_status(config),
            "stats": {},
            "cycles": [],
            "events": [],
            "artifacts": [],
            "alert": None,
        }

    run_id = str(run["run_id"])
    quota_date = local_today()
    cycles = store.recent_cycles(run_id=run_id, limit=100)
    events = store.recent_events(run_id=run_id, limit=100)
    artifacts = store.artifacts(run_id=run_id, limit=100)
    account_rows = store.accounts(run_id)
    usage = store.usage_by_account(run_id, quota_date)
    account_payload = []
    for row in account_rows:
        used = usage.get(str(row["account_id"]), 0)
        quota = int(row["daily_quota"])
        status = str(row["status"])
        if status == "available" and used >= quota:
            status = "quota_reached"
        account_payload.append(
            {
                "account_id": row["account_id"],
                "label": row["label"],
                "status": status,
                "used_today": used,
                "daily_quota": quota,
                "remaining_today": max(0, quota - used),
                "paused_reason": row["paused_reason"],
                "paused_at": row["paused_at"],
            }
        )

    pool_daily_quota = sum(int(row["daily_quota"]) for row in account_rows)
    used_today = sum(account["used_today"] for account in account_payload)
    remaining_today = max(0, pool_daily_quota - used_today)
    available_accounts = len(
        [
            account
            for account in account_payload
            if account["status"] == "available" and account["remaining_today"] > 0
        ]
    )
    paused_accounts = len([account for account in account_payload if account["status"] == "paused"])
    status = str(run["status"])
    heartbeat_age = seconds_since(str(run["heartbeat_at"]))
    if status in {"running", "waiting", "waiting_capacity"} and heartbeat_age > 120:
        status = "stale"
    passed = sum(1 for cycle in cycles if cycle["status"] == "passed")
    failed = sum(1 for cycle in cycles if cycle["status"] == "failed")
    blocked = sum(1 for cycle in cycles if cycle["status"] == "blocked")
    completed_cycles = [
        cycle for cycle in cycles if cycle["status"] in {"passed", "failed", "blocked"}
    ]
    completed_total = len(completed_cycles)
    next_account = (
        select_next_account(store, run_id, config, quota_date=quota_date) if config else None
    )

    return redact(
        {
            "schema": "cbrs-account-pool-status-v1",
            "status": status,
            "run": dict(run),
            "pool": {
                "daily_quota": pool_daily_quota,
                "used_today": used_today,
                "remaining_today": remaining_today,
                "paused_accounts": paused_accounts,
                "available_accounts": available_accounts,
                "quota_date": quota_date,
                "next_account_id": next_account.account_id if next_account else None,
                "next_account_label": next_account.label if next_account else None,
            },
            "accounts": account_payload,
            "alert": _active_alert(
                status=status,
                remaining_today=remaining_today,
                available_accounts=available_accounts,
                paused_accounts=paused_accounts,
                accounts=account_payload,
            ),
            "stats": {
                "total_cycles": len(cycles),
                "passed_cycles": passed,
                "failed_cycles": failed,
                "safety_stops": blocked,
                "success_rate": round(passed / completed_total, 4) if completed_total else None,
                "downloads": sum(int(cycle["download_count"] or 0) for cycle in cycles),
                "heartbeat_age_seconds": heartbeat_age,
                "uptime_seconds": seconds_between(str(run["started_at"]), utc_now()),
            },
            "cycles": cycles,
            "events": events,
            "artifacts": artifacts,
            "control": store.control_state(),
        }
    )


def _active_alert(
    *,
    status: str,
    remaining_today: int,
    available_accounts: int,
    paused_accounts: int,
    accounts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if status == "waiting_capacity" and remaining_today <= 0:
        return {
            "active": True,
            "severity": "warning",
            "title": "Pool diario agotado",
            "message": "Las 60 consultas teóricas del día ya fueron consumidas o reservadas por cuenta. No se generará más tráfico hasta el próximo día.",
            "reason": "daily_pool_exhausted",
        }
    if status == "waiting_capacity" and available_accounts == 0 and paused_accounts:
        return {
            "active": True,
            "severity": "critical",
            "title": "Todas las cuentas están pausadas",
            "message": "El runner sigue vivo, pero no ejecutará nuevas consultas hasta revisar las cuentas pausadas.",
            "reason": "all_accounts_paused",
        }
    paused = [account for account in accounts if account["status"] == "paused"]
    if paused:
        return {
            "active": True,
            "severity": "warning",
            "title": "Cuenta pausada por seguridad",
            "message": "Una cuenta recibió una señal de seguridad y quedó fuera del pool. Las demás cuentas pueden seguir operando.",
            "reason": paused[0].get("paused_reason") or "account_paused",
            "account_label": paused[0].get("label"),
        }
    return None


def _parse_accounts(raw_accounts: Any) -> Iterable[PoolAccount]:
    if not raw_accounts:
        return []
    accounts = []
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            raise ValueError("each pool account must be an object")
        forbidden = SECRET_ACCOUNT_KEYS.intersection({str(key).lower() for key in raw})
        if forbidden:
            raise ValueError(
                "account pool config must not contain credentials, emails, RUTs, "
                f"or secret fields: {', '.join(sorted(forbidden))}"
            )
        account_id = _safe_account_id(str(raw.get("id") or raw.get("account_id") or ""))
        if not account_id:
            raise ValueError("each pool account requires an id")
        label = str(raw.get("label") or account_id).strip()
        enabled = bool(raw.get("enabled", True))
        accounts.append(PoolAccount(account_id=account_id, label=label, enabled=enabled))
    return accounts


def _parse_targets(raw_targets: Any) -> Iterable[PoolTarget]:
    if not raw_targets:
        return []
    targets = []
    for raw in raw_targets:
        label = str(raw.get("label") or "").strip()
        if not label:
            raise ValueError("each pool target requires a label")
        if raw.get("query"):
            targets.append(PoolTarget(label=label, kind="text", query=str(raw["query"])))
        else:
            targets.append(
                PoolTarget(
                    label=label,
                    kind="fna",
                    foja=int(raw["foja"]),
                    numero=int(raw["numero"]),
                    ano=int(raw["ano"]),
                )
            )
    return targets


def _safe_account_id(value: str) -> str:
    value = value.strip().lower().replace("-", "_")
    if not re.fullmatch(r"[a-z0-9_]+", value):
        return ""
    return value


def _default_account_status(config: PoolConfig | None) -> list[dict[str, Any]]:
    accounts = config.accounts if config else DEFAULT_ACCOUNTS
    return [
        {
            "account_id": account.account_id,
            "label": account.label,
            "status": "available" if account.enabled else "disabled",
            "used_today": 0,
            "daily_quota": config.daily_quota_per_account if config else DEFAULT_DAILY_QUOTA_PER_ACCOUNT,
            "remaining_today": config.daily_quota_per_account if config else DEFAULT_DAILY_QUOTA_PER_ACCOUNT,
            "paused_reason": None,
            "paused_at": None,
        }
        for account in accounts
    ]


def _dry_run_validation(
    settings: Settings,
    target: PoolTarget,
    output_dir: Path,
) -> ValidationRunResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "dry-run-placeholder.pdf"
    pdf_path.write_bytes(_dry_run_pdf_bytes())
    report = new_validation_report(
        settings,
        search_kind=target.kind,
        download_first=True,
        preflight_metadata={
            "expected_egress_country": settings.expected_egress_country,
            "egress_mode": settings.egress_mode or None,
            "egress_country": "DRY_RUN",
            "egress_hash": "dry-run",
            "profile_hash": "dry-run",
            "preflight_status": "dry_run",
        },
        headless=settings.headless,
    )
    report["result_count"] = 1
    report["pdf_created"] = True
    report["pdf_path"] = str(pdf_path)
    report["pdf_size_bytes"] = pdf_path.stat().st_size
    finish_validation_report(report, status="passed")
    report_path = write_validation_report(report, settings)
    return ValidationRunResult(
        exit_code=0,
        status="passed",
        report=report,
        report_path=report_path,
        preflight_report_path=None,
        result_count=1,
        pdf_path=pdf_path,
        pdf_size_bytes=pdf_path.stat().st_size,
    )


def _cycle_status(result: ValidationRunResult) -> str:
    if result.exit_code == 0:
        return "passed"
    if result.exit_code == 2:
        return "blocked"
    return "failed"


@contextmanager
def _heartbeat_while_cycle_runs(
    store: AccountPoolStore,
    run_id: str,
    *,
    interval_seconds: float = 30.0,
):
    stop_event = threading.Event()

    def heartbeat() -> None:
        while not stop_event.wait(interval_seconds):
            store.update_run(run_id, status="running", next_cycle_at="")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=2.0)


def _sleep_with_heartbeat(
    store: AccountPoolStore,
    run_id: str,
    seconds: float,
    *,
    sleep_fn: Callable[[float], None],
) -> bool:
    deadline = time.time() + seconds
    while True:
        if store.stop_requested():
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        store.update_run(run_id)
        sleep_fn(min(30.0, remaining))


def _hold_dashboard(
    store: AccountPoolStore,
    run_id: str,
    *,
    status: str,
    sleep_fn: Callable[[float], None],
) -> None:
    try:
        while True:
            store.update_run(run_id, status=status)
            sleep_fn(30.0)
    except KeyboardInterrupt:
        store.update_run(run_id, status="stopped", finished=True)


def local_today() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def next_quota_reset_at() -> str:
    now = datetime.now(LOCAL_TZ)
    next_day = now.date() + timedelta(days=1)
    reset = datetime.combine(next_day, datetime_time.min, tzinfo=LOCAL_TZ)
    return reset.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(microsecond=0).isoformat()


def seconds_since(value: str) -> float:
    return max(0.0, seconds_between(value, utc_now()))


def seconds_between(start: str, end: str) -> float:
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return max(0.0, round((end_dt - start_dt).total_seconds(), 3))


def _dry_run_pdf_bytes() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )
