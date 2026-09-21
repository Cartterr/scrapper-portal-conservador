"""Worker exclusion survives missed heartbeats; recovery requires process proof."""
from __future__ import annotations

import os
import re
import signal
import socket
import time
from functools import wraps
from pathlib import Path

from .owner_lock import OwnerLock


def local_owner_is_dead(owner: str) -> bool:
    match = re.fullmatch(re.escape(socket.gethostname()) + r"-(\d+)-[0-9a-f]{6}", owner)
    if not match:
        return False
    pid = int(match[1])
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
        if handle:
            try:
                return kernel.WaitForSingleObject(handle, 0) == 0
            finally:
                kernel.CloseHandle(handle)
        return ctypes.get_last_error() == 87  # invalid PID; access denied is unknown
    try:
        os.kill(pid, 0)  # POSIX-only existence probe, never terminate a process
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def chrome_pids_using_profiles(accounts_dir) -> list[int] | None:
    """PIDs whose command line opens a Chrome profile under ``accounts_dir``.

    Linux-only proof via /proc command lines; ``None`` on any other platform or
    scan failure so callers preserve browser ownership by default.
    """
    try:
        accounts = str(Path(accounts_dir).resolve())
    except Exception:
        return None
    proc = "/proc"
    if not os.path.isdir(proc):
        return None
    try:
        names = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return None
    pids: list[int] = []
    for name in names:
        pid = int(name)
        if pid == os.getpid():
            continue
        try:
            with open(os.path.join(proc, name, "cmdline"), "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "--user-data-dir=" in cmdline and accounts in cmdline:
            pids.append(pid)
    return pids


def embedded_chrome_survives(settings) -> bool:
    """True when some live process still uses one of this runtime's account profiles."""
    try:
        accounts_dir = settings.profile_dir.parent / "accounts"
    except Exception:
        return True
    pids = chrome_pids_using_profiles(accounts_dir)
    return pids is None or bool(pids)


def terminate_profile_chrome(accounts_dir, *, grace: float = 5.0) -> int:
    """Stop every Chrome process using these profiles: SIGTERM, then SIGKILL (D9).

    Used only at embedded-worker shutdown, where the worker owns those Chrome
    processes; an independent owner's browsers live under a different mode and
    are never touched. Returns how many processes were signalled.
    """
    pids = chrome_pids_using_profiles(accounts_dir) or []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + max(0.0, grace)
    while pids and time.monotonic() < deadline and chrome_pids_using_profiles(accounts_dir):
        time.sleep(0.2)
    for pid in chrome_pids_using_profiles(accounts_dir) or []:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (ProcessLookupError, PermissionError):
            pass
    return len(pids)


def exclusive_worker(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        from .config import SETTINGS
        from .browser_runtime import validate_service_browser
        from .jobs import default_job_store, WORKER_LEASE_NAME, utc_now
        settings = kwargs.get("settings", SETTINGS)
        validate_service_browser(settings)
        store = kwargs.get("store") or default_job_store(settings)
        kwargs["store"] = store
        with OwnerLock(store.path.parent / "worker.lock", label="job worker"):
            lease = store.lease(WORKER_LEASE_NAME)
            if lease:
                dead = local_owner_is_dead(str(lease["owner"]))
                with store.connect() as db:
                    browser_alive = bool(db.execute(
                        "SELECT 1 FROM account_checks WHERE browser_live=1 LIMIT 1"
                    ).fetchone())
                independent = (settings.env_value("CBRS_BROWSER_OWNER_MODE", "embedded") == "external"
                               and bool(store.active_lease("browser_owner")))
                if dead and (independent or not browser_alive):
                    # Retire only the proven-dead worker. Never touch browser
                    # ownership, account quotas, accepted receipts or profiles.
                    with store.connect() as db:
                        removed = db.execute(
                            "DELETE FROM leases WHERE lease_name=? AND owner=?",
                            (WORKER_LEASE_NAME, lease["owner"]),
                        ).rowcount
                        if removed:
                            db.execute(
                                "UPDATE runs SET status='stale', finished_at=?, "
                                "blocked_reason='worker process no longer exists' "
                                "WHERE dry_run=0 AND finished_at IS NULL AND run_id LIKE 'jobs-%'",
                                (utc_now(),),
                            )
                elif dead and browser_alive and not embedded_chrome_survives(settings):
                    # The dead worker's Chrome is gone too (no process uses any
                    # account profile). Its browser marks are stale bookkeeping,
                    # not live ownership: clear them and take the lease over.
                    with store.connect() as db:
                        # No process uses any account profile, so every live
                        # mark is stale regardless of which owner wrote it.
                        db.execute(
                            "UPDATE account_checks SET browser_live=0, browser_authenticated=0, "
                            "browser_status='browser_closed' WHERE browser_live=1"
                        )
                        db.execute(
                            "DELETE FROM leases WHERE lease_name=? AND owner=?",
                            (WORKER_LEASE_NAME, lease["owner"]),
                        )
                        db.execute(
                            "UPDATE runs SET status='stale', finished_at=?, "
                            "blocked_reason='worker and its browsers no longer exist' "
                            "WHERE dry_run=0 AND finished_at IS NULL AND run_id LIKE 'jobs-%'",
                            (utc_now(),),
                        )
                elif dead and browser_alive:
                    raise RuntimeError(
                        "Previous embedded worker is absent but browser ownership survives; "
                        "preserve existing Chrome and arrange an explicit owner handoff."
                    )
                elif str(lease["expires_at"]) < utc_now() and re.fullmatch(
                    re.escape(socket.gethostname()) + r"-\d+-[0-9a-f]{6}", str(lease["owner"])
                ):
                    # Legacy workers may not hold our new OS lock. An expired
                    # heartbeat is insufficient proof that their process died.
                    raise RuntimeError("Previous worker process is alive or cannot be verified; takeover refused.")
            return function(*args, **kwargs)
    return guarded
