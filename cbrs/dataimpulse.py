from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from urllib.parse import quote

DATAIMPULSE_RESIDENTIAL_STICKY_PROVIDER = "dataimpulse_residential_sticky"
DEFAULT_DATAIMPULSE_HOST = "gw.dataimpulse.com"
DEFAULT_DATAIMPULSE_COUNTRY = "cl"
DEFAULT_DATAIMPULSE_STICKY_TTL_MINUTES = 120
DEFAULT_DATAIMPULSE_PORT_MIN = 10000
DEFAULT_DATAIMPULSE_PORT_MAX = 20000


@dataclass(frozen=True)
class DataImpulseRoute:
    port: int
    generation: int = 0


def active_dataimpulse_port(
    state_path: Path,
    account_id: str,
    default_port: int,
) -> int:
    """Read the promoted safe port when route state already exists."""
    path = Path(state_path)
    if not path.is_file():
        return int(default_port)
    try:
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT active_port FROM account_proxy_routes WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else int(default_port)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return int(default_port)


def validate_dataimpulse_port(port: int, *, minimum: int, maximum: int) -> int:
    value = int(port)
    if minimum < 1 or maximum > 65535 or minimum > maximum:
        raise ValueError("DataImpulse port range is invalid")
    if not minimum <= value <= maximum:
        raise ValueError(
            f"DataImpulse sticky port must be between {minimum} and {maximum}"
        )
    return value


def build_dataimpulse_proxy_url(
    *,
    login: str | None,
    password: str | None,
    host: str,
    country: str,
    ttl_minutes: int,
    port: int,
    port_min: int = DEFAULT_DATAIMPULSE_PORT_MIN,
    port_max: int = DEFAULT_DATAIMPULSE_PORT_MAX,
) -> str:
    """Compose a DataImpulse route without persisting a credential-bearing URL."""
    username = str(login or "").strip()
    secret = str(password or "")
    proxy_host = str(host or "").strip().lower()
    proxy_country = str(country or "").strip().lower()
    ttl = int(ttl_minutes)
    if not username or not secret:
        raise ValueError("DataImpulse proxy login and password are required")
    if not proxy_host or any(char.isspace() for char in proxy_host):
        raise ValueError("DataImpulse proxy host is invalid")
    if len(proxy_country) != 2 or not proxy_country.isalpha():
        raise ValueError("DataImpulse country must be a two-letter code")
    if not 1 <= ttl <= 120:
        raise ValueError("DataImpulse sticky TTL must be between 1 and 120 minutes")
    sticky_port = validate_dataimpulse_port(port, minimum=port_min, maximum=port_max)
    routed_login = f"{username}__cr.{proxy_country};sessttl.{ttl}"
    return (
        f"http://{quote(routed_login, safe='')}:{quote(secret, safe='')}@"
        f"{proxy_host}:{sticky_port}"
    )


def next_unused_sticky_port(
    current: int,
    *,
    used_ports: set[int],
    minimum: int = DEFAULT_DATAIMPULSE_PORT_MIN,
    maximum: int = DEFAULT_DATAIMPULSE_PORT_MAX,
) -> int:
    """Choose the next free sticky port deterministically, wrapping once."""
    validate_dataimpulse_port(current, minimum=minimum, maximum=maximum)
    capacity = maximum - minimum + 1
    if len({port for port in used_ports if minimum <= port <= maximum}) >= capacity:
        raise RuntimeError("No unused DataImpulse sticky ports are available")
    for offset in range(1, capacity + 1):
        candidate = minimum + ((current - minimum + offset) % capacity)
        if candidate not in used_ports:
            return candidate
    raise RuntimeError("No unused DataImpulse sticky ports are available")


def classify_dataimpulse_failure(status: int | None, detail: str | None) -> str:
    """Classify provider failures without returning provider response details."""
    normalized = str(detail or "").upper()
    terminal_codes = {
        "NO_USER",
        "REQUESTS_EXHAUSTED",
        "TRAFFIC_EXHAUSTED",
        "THREADS_EXHAUSTED",
        "PORT_NOT_ALLOWED",
        "USER_BLOCKED",
    }
    if status == 407 or any(code in normalized for code in terminal_codes):
        return "provider_terminal"
    if status in {500, 502, 503} or any(
        code in normalized
        for code in {"INTERNAL_SERVER_ERROR", "NO_HOST_CONNECTION", "NO_RAY"}
    ):
        return "transient_route"
    if any(
        marker in normalized
        for marker in {"TIMEOUT", "TIMED OUT", "CONNECTION RESET", "ECONNRESET"}
    ):
        return "transient_route"
    return "unknown"
