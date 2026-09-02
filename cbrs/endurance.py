from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .account_pool import PoolConfig, utc_now

ACTIVE_STATES = ("queued", "running", "waiting_capacity", "waiting_captcha")


@dataclass(frozen=True)
class EnduranceFixture:
    kind: str
    input: dict[str, Any]
    label: str


@dataclass(frozen=True)
class EndurancePlan:
    enabled: bool
    fixtures: tuple[EnduranceFixture, ...]
    cooldown_seconds: float = 300.0
    max_outstanding_jobs: int = 1
    jobs_per_account_per_day: int = 20
    production_reserve_per_account: int = 0
    no_catch_up: bool = True
    quota_exhaustion_test_mode: bool = False

    def source_quota(self, config: PoolConfig) -> dict[str, int]:
        return {
            account.account_id: (
                config.quota_for(account)
                if self.quota_exhaustion_test_mode
                else min(self.jobs_per_account_per_day, config.quota_for(account))
            )
            for account in config.accounts
        }


def load_endurance_plan(path: Path) -> EndurancePlan:
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    fixtures = tuple(
        EnduranceFixture(
            kind=str(item.get("kind", "fna")),
            input=dict(item.get("input") or {}),
            label=str(item.get("label") or f"fixture_{index + 1}"),
        )
        for index, item in enumerate(raw.get("fixtures") or [])
        if isinstance(item, dict)
    )
    plan = EndurancePlan(
        enabled=bool(raw.get("enabled", False)),
        fixtures=fixtures,
        cooldown_seconds=float(raw.get("cooldown_seconds", 300)),
        max_outstanding_jobs=int(raw.get("max_outstanding_jobs", 1)),
        jobs_per_account_per_day=int(raw.get("jobs_per_account_per_day", 20)),
        production_reserve_per_account=int(raw.get("production_reserve_per_account", 0)),
        no_catch_up=bool(raw.get("no_catch_up", True)),
        quota_exhaustion_test_mode=bool(raw.get("quota_exhaustion_test_mode", False)),
    )
    if plan.max_outstanding_jobs != 1:
        raise ValueError("endurance max_outstanding_jobs must be exactly 1")
    if not 60 <= plan.cooldown_seconds <= 300:
        raise ValueError("endurance cooldown_seconds must be between 60 and 300 seconds")
    if plan.jobs_per_account_per_day <= 0 or plan.production_reserve_per_account < 0:
        raise ValueError("endurance quotas must be positive and reserve cannot be negative")
    if not plan.no_catch_up:
        raise ValueError("endurance no_catch_up must remain true")
    if plan.enabled and not fixtures:
        raise ValueError("enabled endurance plan requires at least one fixture")
    return plan


class EnduranceController:
    def __init__(self, store: Any, plan: EndurancePlan, config: PoolConfig) -> None:
        self.store = store
        self.plan = plan
        self.config = config

    def set_paused(self, paused: bool) -> None:
        with self.store.connect() as db:
            db.execute(
                """
                INSERT INTO endurance_state(name, paused, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    paused = excluded.paused, updated_at = excluded.updated_at
                """,
                (int(paused), utc_now()),
            )

    def status(self) -> dict[str, Any]:
        with self.store.connect() as db:
            state = db.execute(
                "SELECT * FROM endurance_state WHERE name = 'default'"
            ).fetchone()
            active = db.execute(
                """
                SELECT job_id, status, created_at FROM jobs
                WHERE source = 'endurance'
                  AND status IN ('queued','running','waiting_capacity','waiting_captcha')
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            last = db.execute(
                """
                SELECT job_id, status, finished_at FROM jobs
                WHERE source = 'endurance' AND status IN ('completed','partial','failed','cancelled')
                ORDER BY finished_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
        return {
            "enabled": self.plan.enabled,
            "paused": bool(state["paused"]) if state else False,
            "active_job": dict(active) if active else None,
            "last_terminal_job": dict(last) if last else None,
            "cooldown_seconds": self.plan.cooldown_seconds,
            "max_outstanding_jobs": 1,
            "jobs_per_account_per_day": self.plan.jobs_per_account_per_day,
            "production_reserve_per_account": self.plan.production_reserve_per_account,
            "quota_exhaustion_test_mode": self.plan.quota_exhaustion_test_mode,
        }

    def maybe_enqueue(self, *, force: bool = False) -> dict[str, Any] | None:
        if not self.plan.fixtures or (not self.plan.enabled and not force):
            return None
        with self.store.connect() as db:
            state = db.execute(
                "SELECT * FROM endurance_state WHERE name = 'default'"
            ).fetchone()
            if state and bool(state["paused"]) and not force:
                return None
            if db.execute(
                """
                SELECT 1 FROM jobs WHERE source = 'endurance'
                  AND status IN ('queued','running','waiting_capacity','waiting_captcha')
                LIMIT 1
                """
            ).fetchone():
                return None
            last = db.execute(
                """
                SELECT finished_at FROM jobs WHERE source = 'endurance'
                  AND status IN ('completed','partial','failed','cancelled')
                ORDER BY finished_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
            if last and last["finished_at"] and not force:
                due = datetime.fromisoformat(str(last["finished_at"])) + timedelta(
                    seconds=self.plan.cooldown_seconds
                )
                if datetime.now(timezone.utc) < due:
                    return None
            sequence = int(state["sequence"]) if state else 0
            fixture_index = int(state["fixture_index"]) if state else 0
        fixture = self.plan.fixtures[fixture_index % len(self.plan.fixtures)]
        try:
            job, created = self.store.create_job(
                kind=fixture.kind,
                input_data=fixture.input,
                idempotency_key=f"endurance:default:{sequence + 1}",
                priority=-10,
                source="endurance",
            )
        except sqlite3.IntegrityError:
            return None
        if not created:
            return None
        with self.store.connect() as db:
            db.execute(
                """
                INSERT INTO endurance_state(name, paused, sequence, fixture_index, updated_at)
                VALUES ('default', 0, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    sequence = excluded.sequence,
                    fixture_index = excluded.fixture_index,
                    updated_at = excluded.updated_at
                """,
                (sequence + 1, (fixture_index + 1) % len(self.plan.fixtures), utc_now()),
            )
        return job
