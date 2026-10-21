"""Persistent store for accounts created by the registration pipeline.

Keeps the email, password, RUT and lifecycle state of every account so they
remain usable long term (login, searches, recovery).  Local file only; never
commit ``registered_accounts.db``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_STATUSES = ("created", "activated", "verified", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AccountRecord:
    email: str
    password: str
    rut: str
    nombres: str
    apellido_paterno: str
    apellido_materno: str | None
    direccion: str
    celular: str
    status: str
    failed_stage: str | None
    failure_reason: str | None
    portal_user_id: str | None
    proxy_port: int | None
    created_at: str
    activated_at: str | None
    verified_at: str | None


class AccountStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS registered_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    rut TEXT NOT NULL,
                    nombres TEXT NOT NULL,
                    apellido_paterno TEXT NOT NULL,
                    apellido_materno TEXT,
                    direccion TEXT NOT NULL,
                    celular TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    failed_stage TEXT,
                    failure_reason TEXT,
                    portal_user_id TEXT,
                    proxy_port INTEGER,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    verified_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_registered_status ON registered_accounts(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_registered_rut ON registered_accounts(rut)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def insert_created(
        self,
        *,
        email: str,
        password: str,
        rut: str,
        nombres: str,
        apellido_paterno: str,
        apellido_materno: str | None,
        direccion: str,
        celular: str,
        proxy_port: int | None,
    ) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO registered_accounts
                (email, password, rut, nombres, apellido_paterno, apellido_materno,
                 direccion, celular, status, failed_stage, failure_reason,
                 portal_user_id, proxy_port, created_at, activated_at, verified_at,
                 updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', NULL, NULL, NULL, ?, ?, NULL, NULL, ?)
                """,
                (
                    email,
                    password,
                    rut,
                    nombres,
                    apellido_paterno,
                    apellido_materno,
                    direccion,
                    celular,
                    proxy_port,
                    now,
                    now,
                ),
            )
            conn.commit()

    def mark_activated(self, email: str) -> None:
        self._update(email, {"status": "activated", "activated_at": _now()})

    def set_proxy_port(self, email: str, port: int) -> None:
        """Track the current registration candidate without replacing credentials."""
        self._update(email, {"proxy_port": port})

    def registration_retry_for_rut(self, rut: str) -> AccountRecord | None:
        """Reuse credentials after safe recovery/form failures or explicit CAPTCHA rejection.

        Never reuse/retry a pending, activated, verified, or ambiguously
        submitted record. These records are read internally, never dumped.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT email FROM registered_accounts WHERE rut=? AND status='failed' "
                "AND (failed_stage IN ('route_recovery','form','captcha') OR "
                "(failed_stage='create' AND failure_reason="
                "'Portal stopped create: captcha_rejected (code captcha-rechazado).')) "
                "ORDER BY id DESC LIMIT 1", (rut,)
            ).fetchone()
        return self.get(row[0]) if row else None

    def mark_verified(self, email: str, portal_user_id: Any = None) -> None:
        fields: dict[str, Any] = {"status": "verified", "verified_at": _now()}
        if portal_user_id is not None:
            fields["portal_user_id"] = str(portal_user_id)
        self._update(email, fields)

    def mark_failed(self, email: str, stage: str, reason: str) -> None:
        self._update(
            email,
            {
                "status": "failed",
                "failed_stage": stage,
                "failure_reason": reason[:300],
            },
        )

    def clear_failure(self, email: str, status: str) -> None:
        self._update(email, {"status": status, "failed_stage": None, "failure_reason": None})

    def _update(self, email: str, fields: dict[str, Any]) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE registered_accounts SET {assignments}, updated_at = ? "
                "WHERE email = ?",
                [*fields.values(), _now(), email],
            )
            conn.commit()

    def get(self, email: str) -> AccountRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT email, password, rut, nombres, apellido_paterno, apellido_materno, "
                "direccion, celular, status, failed_stage, failure_reason, portal_user_id, "
                "proxy_port, created_at, activated_at, verified_at "
                "FROM registered_accounts WHERE email = ?",
                (email,),
            ).fetchone()
        return self._to_record(row) if row else None

    def list_by_status(self, status: str) -> list[AccountRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT email, password, rut, nombres, apellido_paterno, apellido_materno, "
                "direccion, celular, status, failed_stage, failure_reason, portal_user_id, "
                "proxy_port, created_at, activated_at, verified_at "
                "FROM registered_accounts WHERE status = ? ORDER BY id ASC",
                (status,),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM registered_accounts GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def export_pool_entries(
        self, *, prefix: str = "registro", start_index: int = 1
    ) -> list[dict[str, Any]]:
        """Verified accounts as worker-pool entries (never inline secrets).

        ``cbrs.account_pool.load_account_pool_config`` rejects pool entries
        containing ``email``/``password``/``rut``; credentials must live in env
        vars referenced by ``username_env``/``password_env``.  Entries start
        disabled so merging the patch cannot disturb the live pool until the
        operator enables the accounts that are ready to use.
        """

        env_prefix = re.sub(r"[^A-Z0-9_]+", "_", prefix.upper()).strip("_") or "REGISTRO"
        entries: list[dict[str, Any]] = []
        for offset, _record in enumerate(self.list_by_status("verified")):
            number = start_index + offset
            entries.append(
                {
                    "id": f"{prefix}_{number}",
                    "label": f"Registro {number}",
                    "enabled": False,
                    "username_env": f"CBRS_{env_prefix}_{number}_USERNAME",
                    "password_env": f"CBRS_{env_prefix}_{number}_PASSWORD",
                }
            )
        return entries

    def export_env_assignments(
        self, *, prefix: str = "registro", start_index: int = 1
    ) -> list[str]:
        """``NAME=value`` env lines matching :meth:`export_pool_entries`."""

        env_prefix = re.sub(r"[^A-Z0-9_]+", "_", prefix.upper()).strip("_") or "REGISTRO"
        lines: list[str] = []
        for offset, record in enumerate(self.list_by_status("verified")):
            number = start_index + offset
            lines.append(f"CBRS_{env_prefix}_{number}_USERNAME={record.email}")
            lines.append(f"CBRS_{env_prefix}_{number}_PASSWORD={record.password}")
        return lines

    @staticmethod
    def _to_record(row: tuple) -> AccountRecord:
        return AccountRecord(
            email=row[0],
            password=row[1],
            rut=row[2],
            nombres=row[3],
            apellido_paterno=row[4],
            apellido_materno=row[5],
            direccion=row[6],
            celular=row[7],
            status=row[8],
            failed_stage=row[9],
            failure_reason=row[10],
            portal_user_id=row[11],
            proxy_port=row[12],
            created_at=row[13],
            activated_at=row[14],
            verified_at=row[15],
        )


def _pool_entry_key(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("email") or "").strip().lower()


def append_to_account_pool(pool_path: str | Path, entries: list[dict[str, Any]]) -> int:
    """Merge pool entries into a pool JSON without duplicates.

    Entries are matched by ``id`` (the worker-pool schema) and, for legacy
    records, by ``email``.  Existing accounts and top-level settings are
    preserved exactly.
    """

    path = Path(pool_path)
    pool = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    accounts = pool.get("accounts", [])
    known = {_pool_entry_key(entry) for entry in accounts}
    added = 0
    for entry in entries:
        key = _pool_entry_key(entry)
        if key and key in known:
            continue
        accounts.append(entry)
        known.add(key)
        added += 1
    pool["accounts"] = accounts
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pool, indent=2, ensure_ascii=False), encoding="utf-8")
    return added
