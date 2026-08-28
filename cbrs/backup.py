from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import SETTINGS, Settings
from .safety import redact, redact_text

BACKUP_MAX_AGE_HOURS = 36
RESTORE_MAX_AGE_HOURS = 30 * 24


def run_backup(
    *,
    settings: Settings = SETTINGS,
    database_path: Path,
    env: Mapping[str, str] | None = None,
    command_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Create a consistent SQLite snapshot and append it to encrypted restic storage."""
    runtime_env = dict(os.environ if env is None else env)
    repository_configured = bool(runtime_env.get("RESTIC_REPOSITORY"))
    state_dir = settings.profile_dir.parent / "backup"
    snapshot_dir = state_dir / "snapshot"
    status_path = state_dir / "status.json"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = _now()
    payload: dict[str, Any] = {
        "schema": "cbrs-backup-v1",
        "ok": False,
        "started_at": started_at,
        "finished_at": None,
        "repository_configured": repository_configured,
        "database_snapshot": None,
        "error": None,
    }
    try:
        if not database_path.exists():
            raise RuntimeError("CBRS SQLite database does not exist.")
        snapshot = snapshot_dir / "cbrs.sqlite3"
        temporary = snapshot.with_suffix(".sqlite3.tmp")
        temporary.unlink(missing_ok=True)
        with closing(sqlite3.connect(database_path)) as source, closing(
            sqlite3.connect(temporary)
        ) as target:
            source.backup(target)
            target.commit()
        os.replace(temporary, snapshot)
        payload["database_snapshot"] = str(snapshot)

        if not repository_configured:
            raise RuntimeError("RESTIC_REPOSITORY is not configured.")
        configured_restic = runtime_env.get("CBRS_RESTIC_EXECUTABLE_PATH", "").strip()
        restic = configured_restic or shutil.which("restic")
        if not restic or (configured_restic and not Path(restic).is_file()):
            raise RuntimeError("restic is not installed or is not on PATH.")

        targets = [str(snapshot), str(settings.output_dir)]
        pool_config = settings.profile_dir.parent / "account-pool.json"
        if pool_config.exists():
            targets.append(str(pool_config))
        result = command_runner(
            [restic, "backup", "--tag", "cbrs", *targets],
            env=runtime_env,
            capture_output=True,
            text=True,
            timeout=24 * 60 * 60,
            check=False,
        )
        if int(result.returncode) != 0:
            detail = redact_text((result.stderr or result.stdout or "restic failed").strip())
            raise RuntimeError(detail[:1000])
        payload["ok"] = True
    except Exception as exc:
        payload["error"] = redact_text(str(exc))[:1000]
    finally:
        payload["finished_at"] = _now()
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return redact(payload)


def backup_health(settings: Settings = SETTINGS) -> dict[str, Any]:
    status_path = settings.profile_dir.parent / "backup" / "status.json"
    disk_probe = settings.output_dir
    while not disk_probe.exists() and disk_probe != disk_probe.parent:
        disk_probe = disk_probe.parent
    disk = shutil.disk_usage(disk_probe)
    payload: dict[str, Any] = {
        "status": "not_configured",
        "last_backup_at": None,
        "age_hours": None,
        "free_bytes": disk.free,
        "total_bytes": disk.total,
        "free_percent": round((disk.free / disk.total) * 100, 2) if disk.total else 0,
        "repository_free_bytes": None,
        "repository_free_percent": None,
        "error": None,
        "restore_status": "not_verified",
        "last_restore_verified_at": None,
        "restored_pdf_count": None,
    }
    repository = os.environ.get("RESTIC_REPOSITORY", "")
    repository_path = Path(repository).expanduser() if repository else None
    if repository_path and repository_path.is_absolute() and repository_path.exists():
        repository_disk = shutil.disk_usage(repository_path)
        payload["repository_free_bytes"] = repository_disk.free
        payload["repository_free_percent"] = (
            round((repository_disk.free / repository_disk.total) * 100, 2)
            if repository_disk.total
            else 0
        )
    if status_path.exists():
        try:
            stored = json.loads(status_path.read_text(encoding="utf-8"))
            finished_at = stored.get("finished_at")
            payload["last_backup_at"] = finished_at
            payload["error"] = stored.get("error")
            if finished_at:
                finished = datetime.fromisoformat(str(finished_at))
                age = (datetime.now(timezone.utc) - finished).total_seconds() / 3600
                payload["age_hours"] = round(max(0, age), 2)
            if not stored.get("ok"):
                payload["status"] = "failed"
            elif payload["age_hours"] is not None and payload["age_hours"] > BACKUP_MAX_AGE_HOURS:
                payload["status"] = "stale"
            else:
                payload["status"] = "healthy"
        except Exception as exc:
            payload["status"] = "invalid"
            payload["error"] = redact_text(str(exc))[:500]
    low_repository = (
        payload["repository_free_percent"] is not None
        and payload["repository_free_percent"] < 10
    )
    if (payload["free_percent"] < 10 or low_repository) and payload["status"] == "healthy":
        payload["status"] = "low_disk"
    restore_status_path = settings.profile_dir.parent / "backup" / "restore-status.json"
    if restore_status_path.is_file():
        try:
            restored = json.loads(restore_status_path.read_text(encoding="utf-8"))
            restored_at = restored.get("finished_at")
            payload["last_restore_verified_at"] = restored_at
            payload["restored_pdf_count"] = int(restored.get("pdf_count") or 0)
            if restored.get("ok") and restored_at:
                age = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(str(restored_at))
                ).total_seconds() / 3600
                payload["restore_status"] = (
                    "verified" if age <= RESTORE_MAX_AGE_HOURS else "stale"
                )
            else:
                payload["restore_status"] = "failed"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload["restore_status"] = "invalid"
    return redact(payload)


def verify_backup_restore(
    *,
    settings: Settings = SETTINGS,
    require_pdf: bool = False,
    env: Mapping[str, str] | None = None,
    command_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Restore the latest encrypted snapshot into a temporary directory and validate it."""
    runtime_env = dict(os.environ if env is None else env)
    state_dir = settings.profile_dir.parent / "backup"
    status_path = state_dir / "restore-status.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema": "cbrs-restore-verification-v1",
        "ok": False,
        "started_at": _now(),
        "finished_at": None,
        "database_quick_check": False,
        "pdf_count": 0,
        "require_pdf": bool(require_pdf),
        "error": None,
    }
    temporary_root: Path | None = None
    try:
        if not runtime_env.get("RESTIC_REPOSITORY"):
            raise RuntimeError("RESTIC_REPOSITORY is not configured.")
        configured_restic = runtime_env.get("CBRS_RESTIC_EXECUTABLE_PATH", "").strip()
        restic = configured_restic or shutil.which("restic")
        if not restic or (configured_restic and not Path(restic).is_file()):
            raise RuntimeError("restic is not installed or is not on PATH.")
        temporary_root = Path(tempfile.mkdtemp(prefix="restore-verify-", dir=state_dir))
        result = command_runner(
            [
                restic,
                "restore",
                "latest",
                "--tag",
                "cbrs",
                "--target",
                str(temporary_root),
                "--verify",
            ],
            env=runtime_env,
            capture_output=True,
            text=True,
            timeout=24 * 60 * 60,
            check=False,
        )
        if int(result.returncode) != 0:
            detail = redact_text((result.stderr or result.stdout or "restic restore failed").strip())
            raise RuntimeError(detail[:1000])
        databases = list(temporary_root.rglob("cbrs.sqlite3"))
        if not databases:
            raise RuntimeError("The restored snapshot does not contain cbrs.sqlite3.")
        with closing(sqlite3.connect(f"file:{databases[0].as_posix()}?mode=ro", uri=True)) as db:
            quick_check = [str(row[0]) for row in db.execute("PRAGMA quick_check").fetchall()]
        if quick_check != ["ok"]:
            raise RuntimeError("The restored SQLite database failed PRAGMA quick_check.")
        payload["database_quick_check"] = True
        pdfs = list(temporary_root.rglob("*.pdf"))
        valid_pdfs = 0
        for pdf in pdfs:
            try:
                with pdf.open("rb") as handle:
                    valid_pdfs += int(handle.read(5) == b"%PDF-")
            except OSError:
                continue
        payload["pdf_count"] = valid_pdfs
        if require_pdf and valid_pdfs < 1:
            raise RuntimeError("The restored snapshot does not contain a valid PDF artifact.")
        payload["ok"] = True
    except Exception as exc:
        payload["error"] = redact_text(str(exc))[:1000]
    finally:
        if temporary_root is not None:
            try:
                _remove_temporary_restore(temporary_root)
            except OSError:
                payload["ok"] = False
                payload["error"] = "Temporary restore verification data could not be removed."
        payload["finished_at"] = _now()
        temporary = status_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(redact(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, status_path)
    return redact(payload)


def _remove_temporary_restore(path: Path) -> None:
    for attempt in range(4):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            for item in sorted(path.rglob("*"), reverse=True):
                try:
                    os.chmod(item, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
                except OSError:
                    pass
            if attempt == 3:
                raise
            time.sleep(0.1)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
