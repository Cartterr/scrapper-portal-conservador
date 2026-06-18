from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import SETTINGS, Settings

WINDOWS_BROWSER_PATHS: tuple[tuple[str, str], ...] = (
    ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ("edge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)


@dataclass(frozen=True)
class BrowserExecutable:
    family: str
    path: Path
    source: str


@dataclass(frozen=True)
class BrowserStatus:
    available: bool
    family: str | None
    path: Path | None
    source: str | None
    error: str | None = None


def detect_browser(
    settings: Settings = SETTINGS,
    *,
    candidates: Iterable[tuple[str, str]] = WINDOWS_BROWSER_PATHS,
) -> BrowserExecutable:
    if settings.browser_executable_path is not None:
        path = settings.browser_executable_path
        if not path.exists():
            raise RuntimeError(f"Configured browser executable does not exist: {path}")
        family = _browser_family(path)
        return BrowserExecutable(family=family, path=path, source="env")

    for family, raw_path in candidates:
        path = Path(raw_path)
        if path.exists():
            return BrowserExecutable(family=family, path=path, source="auto")

    raise RuntimeError(
        "No Chrome or Edge executable found. Set CBRS_BROWSER_EXECUTABLE_PATH."
    )


def get_browser_status(settings: Settings = SETTINGS) -> BrowserStatus:
    try:
        executable = detect_browser(settings)
    except Exception as exc:
        return BrowserStatus(
            available=False,
            family=None,
            path=None,
            source=None,
            error=str(exc),
        )
    return BrowserStatus(
        available=True,
        family=executable.family,
        path=executable.path,
        source=executable.source,
    )


def browser_runtime_metadata(settings: Settings = SETTINGS) -> dict[str, object]:
    status = get_browser_status(settings)
    return {
        "browser_backend": settings.browser_backend,
        "browser_family": status.family,
        "browser_executable_source": status.source,
        "browser_executable_hash": _hash_text(str(status.path)) if status.path else None,
        "profile_hash": profile_hash(settings),
    }


def profile_hash(settings: Settings = SETTINGS) -> str:
    return _hash_text(str(settings.profile_dir))


def _browser_family(path: Path) -> str:
    name = path.name.lower()
    if "msedge" in name or "edge" in str(path).lower():
        return "edge"
    return "chrome"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
