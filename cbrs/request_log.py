"""Per-account record of every portal request (2026-09-25).

The client lost two accounts and nobody could say which requests preceded the
deactivation: the service only kept events. Each Chrome context now appends one
line per portal request to ``<log_dir>/requests/<account>/<UTC date>.jsonl``:
time, method, path, resource type and status. Query strings, headers, cookies
and bodies are never recorded; long opaque path segments (tickets, tokens) are
masked. Plain files, not the job database: request volume must never contend
with the worker's SQLite writes.
"""
from __future__ import annotations

import json
import hashlib
import re
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from urllib.parse import urlsplit

PORTAL_HOST_SUFFIX = "conservador.cl"
RECAPTCHA_HOST_SUFFIXES = ("google.com", "gstatic.com", "recaptcha.net")
# Page loads and API calls; static assets would only add noise. reCAPTCHA also
# keeps its scripts and frames: they are what the portal's bot scoring sees.
RECORDED_TYPES = frozenset({"document", "xhr", "fetch", "other", "health"})
RECAPTCHA_TYPES = RECORDED_TYPES | {"script"}
_OPAQUE_SEGMENT = re.compile(r"^[A-Za-z0-9_\-.=+%]{24,}$")
_egress_observations: OrderedDict[tuple[str, str], dict] = OrderedDict()


def _route_key(settings: Any) -> tuple[str, str]:
    # Bind evidence to the exact profile and proxy configuration. Never write
    # the proxy URL (which contains credentials) into evidence files.
    return (str(Path(settings.profile_dir).absolute()),
            hashlib.sha256(str(getattr(settings, "proxy_url", None)).encode()).hexdigest())


def observe_egress(settings: Any, egress_hash: str, *, source: str,
                   observed_at: datetime | None = None) -> None:
    """Remember an existing egress probe; this function makes no network call.

    This is a point-in-time observation, never proof of a request's exact exit.
    """
    if not re.fullmatch(r"[a-f0-9]{12}", egress_hash):
        return
    key = _route_key(settings)
    _egress_observations[key] = {
        "egress_observed_hash": egress_hash,
        "egress_observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(timespec="milliseconds"),
        "egress_observation_source": source,
    }
    _egress_observations.move_to_end(key)
    while len(_egress_observations) > 256:
        _egress_observations.popitem(last=False)


def redact_path(path: str) -> str:
    return "/".join(":id" if _OPAQUE_SEGMENT.match(part) else part for part in path.split("/"))


def request_log_dir(settings: Any) -> Path:
    return Path(settings.log_dir) / "requests" / (getattr(settings, "account_id", None) or "default")


# ponytail: files are never pruned (roughly 1 MB per account per month); add a
# retention sweep if the log directory ever matters for disk space.
def _recorded(host: str, path: str, resource_type: str) -> bool:
    if host.endswith(PORTAL_HOST_SUFFIX):
        return resource_type in RECORDED_TYPES
    return "/recaptcha/" in path and host.endswith(RECAPTCHA_HOST_SUFFIXES) and resource_type in RECAPTCHA_TYPES


def session_of(settings: Any, *, headless: bool | None, client: str = "chrome") -> dict:
    """Which browser/route made the request: profile, proxy port and mode."""
    proxy = getattr(settings, "proxy_url", None)
    try:
        port = urlsplit(proxy).port if proxy else None
    except ValueError:
        port = None
    return {"client": client, "profile": Path(settings.profile_dir).name, "port": port, "headless": headless,
            **_egress_observations.get(_route_key(settings), {})}


def record(directory: Path, request: Any, status: int | str, *, now: datetime | None = None,
           session: dict | None = None) -> None:
    parts = urlsplit(request.url)
    if not _recorded(parts.hostname or "", parts.path, request.resource_type):
        return
    now = now or datetime.now(timezone.utc)
    line = json.dumps({"at": now.isoformat(timespec="milliseconds"), "method": request.method,
                       "host": parts.hostname, "path": redact_path(parts.path),
                       "type": request.resource_type, "status": status, **(session or {})})
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass  # Evidence must never break a portal operation.


def attach(context: Any, settings: Any, *, headless: bool | None = None) -> None:
    """Record every portal and reCAPTCHA request of this context (responses and failures)."""
    directory = request_log_dir(settings)
    context.on("response", lambda response: record(directory, response.request, response.status,
               session=session_of(settings, headless=headless)))
    context.on("requestfailed", lambda request: record(directory, request, "failed",
               session=session_of(settings, headless=headless)))


def record_outside_browser(settings: Any, url: str, method: str, status: int | None, *, client: str) -> None:
    """Portal calls made without Chrome (proxy health) through the account's exit."""
    record(request_log_dir(settings), SimpleNamespace(url=url, method=method, resource_type="health"),
           status if status is not None else "failed", session=session_of(settings, headless=None, client=client))


def read(settings: Any, account_id: str, *, since: str | None = None, until: str | None = None) -> Iterable[dict]:
    directory = Path(settings.log_dir) / "requests" / account_id
    for path in sorted(directory.glob("*.jsonl")):
        if (since and path.stem < since[:10]) or (until and path.stem > until[:10]):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if (since and entry["at"] < since) or (until and entry["at"] > until):
                continue
            yield entry


def summary(entries: Iterable[dict]) -> list[tuple[str, str, str, int]]:
    """(method, path, status, count), most frequent first."""
    counts = Counter((e["method"], e["path"], str(e["status"])) for e in entries)
    return [(*key, count) for key, count in counts.most_common()]
