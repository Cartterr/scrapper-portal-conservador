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
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

PORTAL_HOST_SUFFIX = "conservador.cl"
# Page loads and API calls; static assets would only add noise.
RECORDED_TYPES = frozenset({"document", "xhr", "fetch", "other"})
_OPAQUE_SEGMENT = re.compile(r"^[A-Za-z0-9_\-.=+%]{24,}$")


def redact_path(path: str) -> str:
    return "/".join(":id" if _OPAQUE_SEGMENT.match(part) else part for part in path.split("/"))


def request_log_dir(settings: Any) -> Path:
    return Path(settings.log_dir) / "requests" / (getattr(settings, "account_id", None) or "default")


# ponytail: files are never pruned (roughly 1 MB per account per month); add a
# retention sweep if the log directory ever matters for disk space.
def record(directory: Path, request: Any, status: int | str, *, now: datetime | None = None) -> None:
    if request.resource_type not in RECORDED_TYPES:
        return
    parts = urlsplit(request.url)
    if not (parts.hostname or "").endswith(PORTAL_HOST_SUFFIX):
        return
    now = now or datetime.now(timezone.utc)
    line = json.dumps({"at": now.isoformat(timespec="milliseconds"), "method": request.method,
                       "host": parts.hostname, "path": redact_path(parts.path),
                       "type": request.resource_type, "status": status})
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass  # Evidence must never break a portal operation.


def attach(context: Any, settings: Any) -> None:
    """Record every portal request of this context (responses and failures)."""
    directory = request_log_dir(settings)
    context.on("response", lambda response: record(directory, response.request, response.status))
    context.on("requestfailed", lambda request: record(directory, request, "failed"))


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
