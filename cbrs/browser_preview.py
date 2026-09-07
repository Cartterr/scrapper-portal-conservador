from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BrowserPreview:
    path: Path
    captured_at: str
    age_seconds: float


def browser_preview_path(store_path: Path, account_id: str) -> Path:
    """Return a stable, traversal-safe preview path for one configured account."""
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]
    return Path(store_path).parent / "browser-previews" / f"{digest}.jpg"


def capture_browser_preview(page: Any, store_path: Path, account_id: str) -> BrowserPreview:
    """Capture one low-bandwidth viewport frame and publish it atomically."""
    target = browser_preview_path(store_path, account_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}-{os.getpid()}.tmp.jpg")
    try:
        frame = page.screenshot(
            type="jpeg",
            quality=48,
            full_page=False,
            animations="disabled",
            caret="hide",
            timeout=8_000,
        )
        temporary.write_bytes(frame)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return read_browser_preview(store_path, account_id, max_age_seconds=float("inf"))


def read_browser_preview(
    store_path: Path,
    account_id: str,
    *,
    max_age_seconds: float,
) -> BrowserPreview:
    path = browser_preview_path(store_path, account_id)
    stat = path.stat()
    age_seconds = max(0.0, time.time() - stat.st_mtime)
    if age_seconds > max_age_seconds:
        raise FileNotFoundError("browser preview is stale")
    captured_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    return BrowserPreview(path=path, captured_at=captured_at, age_seconds=age_seconds)


def remove_browser_preview(store_path: Path, account_id: str) -> None:
    """Remove an ephemeral frame when its owning browser is discarded."""
    browser_preview_path(store_path, account_id).unlink(missing_ok=True)
