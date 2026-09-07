"""Best-effort immutable viewport evidence; never navigate or retry a browser."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from datetime import datetime, timezone


def notify_browser_error(browser, error) -> None:
    try:
        callback = getattr(browser, "error_capture_callback", None)
        if callable(callback):
            callback(error)
    except Exception:
        pass


def evidence_path(store_path: Path, evidence_id: str) -> Path:
    if not re.fullmatch(r"error-[a-f0-9]{32}", evidence_id):
        raise ValueError("Invalid evidence identifier")
    return store_path.parent / "error-screenshots" / f"{evidence_id}.jpg"


def capture_error(store, account_id, browser, error) -> None:
    """Never let evidence collection replace an original error or affect recovery."""
    if getattr(error, "_cbrs_error_captured", False):
        return
    try:
        with store.connect() as db:
            attempt = db.execute(
                "SELECT a.attempt_id FROM job_attempts a JOIN jobs j ON j.job_id=a.job_id "
                "WHERE a.account_id=? AND j.status='running' "
                "ORDER BY a.started_at DESC, a.rowid DESC LIMIT 1", (account_id,)
            ).fetchone()
            if not attempt:
                return
            attempt_id = attempt["attempt_id"]
            if db.execute("SELECT COUNT(*) FROM attempt_error_evidence WHERE attempt_id=?",
                          (attempt_id,)).fetchone()[0] >= 8:
                return  # bounded storage and capture overhead per attempt
        evidence_id = "error-" + secrets.token_hex(16)
        captured_at = datetime.now(timezone.utc).isoformat()
        capture_status = "unavailable"
        target = evidence_path(store.path, evidence_id)
        temporary = target.with_suffix(".tmp")
        try:
            page = browser.page
            # Mask input values without changing DOM, focus, cookies or navigation.
            frame = page.screenshot(type="jpeg", quality=65, full_page=False,
                                    timeout=3000, mask=[page.locator("input, textarea, [contenteditable=true]")])
            if not frame.startswith(b"\xff\xd8") or len(frame) > 4_000_000:
                raise ValueError("Invalid or oversized frame")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(frame)
            os.replace(temporary, target)
            capture_status = "captured"
        except Exception:
            pass
        finally:
            temporary.unlink(missing_ok=True)
        reason = getattr(getattr(error, "reason", None), "value", None) or type(error).__name__
        reason = re.sub(r"[^a-zA-Z0-9_-]", "_", str(reason))[:80]
        http_status = getattr(error, "status", None)
        if not isinstance(http_status, int) or not 100 <= http_status <= 599:
            http_status = None
        with store.connect() as db:
            db.execute("INSERT INTO attempt_error_evidence VALUES(?,?,?,?,?,?)",
                       (evidence_id, attempt_id, captured_at, capture_status, reason, http_status))
        error._cbrs_error_captured = True
    except Exception:
        pass
