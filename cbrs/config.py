from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from dotenv import dotenv_values

DEFAULT_BASE_URL = "https://nuevo-portal.conservador.cl"
COMMERCE_ROUTE = "/consultas-en-linea/indices/indice-del-registro-de-comercio"
DEFAULT_RECAPTCHA_SITEKEY = "6Le-eiksAAAAANU-0ITcjxvGfFoHsz40juvUVI_-"

MIN_SAFE_DELAY_SECONDS = 3.5
DEFAULT_REQUEST_DELAY_SECONDS = 5.0
DEFAULT_BROWSER_BACKEND = "chrome"
DEFAULT_HEADLESS = False
DEFAULT_WINDOW_MODE = "normal"
DEFAULT_EXPECTED_EGRESS_COUNTRY = "CL"
DEFAULT_CAPTCHA_SOLVER_MODE = "browser"
CAPTCHA_SOLVER_MODES = frozenset(
    {"browser", "2captcha_manual", "2captcha_fallback", "2captcha"}
)
DEFAULT_2CAPTCHA_MIN_SCORE = 0.9
DEFAULT_2CAPTCHA_TIMEOUT_SECONDS = 120.0
DEFAULT_2CAPTCHA_POLL_SECONDS = 5.0
DEFAULT_2CAPTCHA_DAILY_LIMIT = 10
DEFAULT_2CAPTCHA_CIRCUIT_BREAKER_SECONDS = 900.0
DEFAULT_2CAPTCHA_REJECTION_COOLDOWN_SECONDS = 21_600.0
DEFAULT_PROXY_RECHECK_SECONDS = 300.0
ALLOWED_EGRESS_MODES = frozenset(
    {
        "client_vpn",
        "client_office",
        "dedicated_static_isp",
    }
)
PERSONAL_DIRECT_EGRESS_MODE = "personal_direct"
CLOAK_REQUIRED_VERSION = "0.3.31"


@dataclass(frozen=True)
class Settings:
    base_url: str
    commerce_route: str
    recaptcha_sitekey: str
    browser_backend: str
    browser_executable_path: Path | None
    headless: bool
    window_mode: str
    egress_mode: str
    allow_personal_egress: bool
    expected_egress_country: str
    profile_dir: Path
    cloak_cache_dir: Path
    cloak_fingerprint_seed: str | None
    cloak_proxy_url: str | None
    proxy_url: str | None
    captcha_solver_mode: str
    two_captcha_api_key: str | None = field(repr=False)
    two_captcha_min_score: float
    two_captcha_timeout_seconds: float
    two_captcha_poll_seconds: float
    two_captcha_daily_limit: int
    two_captcha_circuit_breaker_seconds: float
    two_captcha_rejection_cooldown_seconds: float
    captcha_state_path: Path
    account_id: str | None
    proxy_recheck_seconds: float
    allow_cloak_auto_update: bool
    output_dir: Path
    log_dir: Path
    request_delay_seconds: float
    use_curl_cffi_for_images: bool
    curl_cffi_impersonate: str

    @property
    def commerce_url(self) -> str:
        return f"{self.base_url}{self.commerce_route}"

    def delay_seconds(self) -> float:
        return self.request_delay_seconds


def _merged_env(dotenv_path: str | Path = ".env") -> dict[str, str]:
    file_values = {
        key: value
        for key, value in dotenv_values(dotenv_path).items()
        if value is not None
    }
    return {**os.environ, **file_values}


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or value.lower() in {"none", "null", "false"}:
        return None
    return value


def _bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(value: str | None, *, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric setting: {value!r}") from exc


def _int(value: str | None, *, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer setting: {value!r}") from exc


def _path(value: str | None, *, default: str, root: Path) -> Path:
    raw = value or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _request_delay_seconds(env: Mapping[str, str]) -> float:
    fixed_delay = env.get("CBRS_REQUEST_DELAY_SECONDS")
    if fixed_delay not in (None, ""):
        return max(
            _float(fixed_delay, default=DEFAULT_REQUEST_DELAY_SECONDS),
            MIN_SAFE_DELAY_SECONDS,
        )

    # Backward-compatible deterministic handling for old range settings:
    # use the slower configured value inside the range.
    legacy_values = [
        _float(value, default=DEFAULT_REQUEST_DELAY_SECONDS)
        for value in (
            env.get("CBRS_REQUEST_DELAY_MIN_SECONDS"),
            env.get("CBRS_REQUEST_DELAY_MAX_SECONDS"),
        )
        if value not in (None, "")
    ]
    return max(
        legacy_values or [DEFAULT_REQUEST_DELAY_SECONDS],
        key=float,
    )


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
) -> Settings:
    root = (root or Path.cwd()).resolve()
    env = dict(_merged_env(root / ".env") if env is None else env)
    request_delay = max(_request_delay_seconds(env), MIN_SAFE_DELAY_SECONDS)
    browser_backend = env.get("CBRS_BROWSER_BACKEND", DEFAULT_BROWSER_BACKEND).strip().lower()
    default_profile_dir = (
        ".cbrs/cloak-profile"
        if browser_backend == "cloak"
        else ".cbrs/chrome-profile"
    )

    captcha_solver_mode = env.get(
        "CBRS_CAPTCHA_SOLVER_MODE", DEFAULT_CAPTCHA_SOLVER_MODE
    ).strip().lower()
    if captcha_solver_mode not in CAPTCHA_SOLVER_MODES:
        raise ValueError(
            "CBRS_CAPTCHA_SOLVER_MODE must be browser, 2captcha_manual, "
            "2captcha_fallback, or 2captcha"
        )
    two_captcha_api_key = _empty_to_none(env.get("CBRS_2CAPTCHA_API_KEY"))
    if captcha_solver_mode in {"2captcha", "2captcha_fallback"} and not two_captcha_api_key:
        raise ValueError(
            "CBRS_2CAPTCHA_API_KEY is required when CBRS_CAPTCHA_SOLVER_MODE uses 2Captcha"
        )
    two_captcha_min_score = _float(
        env.get("CBRS_2CAPTCHA_MIN_SCORE"), default=DEFAULT_2CAPTCHA_MIN_SCORE
    )
    if two_captcha_min_score not in {0.3, 0.7, 0.9}:
        raise ValueError("CBRS_2CAPTCHA_MIN_SCORE must be 0.3, 0.7, or 0.9")
    two_captcha_timeout_seconds = _float(
        env.get("CBRS_2CAPTCHA_TIMEOUT_SECONDS"),
        default=DEFAULT_2CAPTCHA_TIMEOUT_SECONDS,
    )
    if two_captcha_timeout_seconds <= 0:
        raise ValueError("CBRS_2CAPTCHA_TIMEOUT_SECONDS must be greater than zero")
    two_captcha_poll_seconds = _float(
        env.get("CBRS_2CAPTCHA_POLL_SECONDS"),
        default=DEFAULT_2CAPTCHA_POLL_SECONDS,
    )
    if two_captcha_poll_seconds < 5:
        raise ValueError("CBRS_2CAPTCHA_POLL_SECONDS must be at least 5 seconds")
    two_captcha_daily_limit = _int(
        env.get("CBRS_2CAPTCHA_DAILY_LIMIT"), default=DEFAULT_2CAPTCHA_DAILY_LIMIT
    )
    if two_captcha_daily_limit <= 0:
        raise ValueError("CBRS_2CAPTCHA_DAILY_LIMIT must be greater than zero")
    two_captcha_circuit_breaker_seconds = _float(
        env.get("CBRS_2CAPTCHA_CIRCUIT_BREAKER_SECONDS"),
        default=DEFAULT_2CAPTCHA_CIRCUIT_BREAKER_SECONDS,
    )
    if two_captcha_circuit_breaker_seconds < 60:
        raise ValueError("CBRS_2CAPTCHA_CIRCUIT_BREAKER_SECONDS must be at least 60 seconds")
    two_captcha_rejection_cooldown_seconds = _float(
        env.get("CBRS_2CAPTCHA_REJECTION_COOLDOWN_SECONDS"),
        default=DEFAULT_2CAPTCHA_REJECTION_COOLDOWN_SECONDS,
    )
    if two_captcha_rejection_cooldown_seconds < 3600:
        raise ValueError(
            "CBRS_2CAPTCHA_REJECTION_COOLDOWN_SECONDS must be at least 3600 seconds"
        )
    proxy_recheck_seconds = _float(
        env.get("CBRS_PROXY_RECHECK_SECONDS"), default=DEFAULT_PROXY_RECHECK_SECONDS
    )
    if proxy_recheck_seconds < 60:
        raise ValueError("CBRS_PROXY_RECHECK_SECONDS must be at least 60 seconds")

    return Settings(
        base_url=env.get("CBRS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        commerce_route=env.get("CBRS_COMMERCE_ROUTE", COMMERCE_ROUTE),
        recaptcha_sitekey=env.get("CBRS_RECAPTCHA_SITEKEY", DEFAULT_RECAPTCHA_SITEKEY),
        browser_backend=browser_backend,
        browser_executable_path=(
            _path(env.get("CBRS_BROWSER_EXECUTABLE_PATH"), default="", root=root)
            if _empty_to_none(env.get("CBRS_BROWSER_EXECUTABLE_PATH"))
            else None
        ),
        headless=_bool(env.get("CBRS_HEADLESS"), default=DEFAULT_HEADLESS),
        window_mode=env.get("CBRS_WINDOW_MODE", DEFAULT_WINDOW_MODE).strip().lower(),
        egress_mode=env.get("CBRS_EGRESS_MODE", "").strip().lower(),
        allow_personal_egress=_bool(env.get("CBRS_ALLOW_PERSONAL_EGRESS")),
        expected_egress_country=env.get(
            "CBRS_EXPECTED_EGRESS_COUNTRY",
            DEFAULT_EXPECTED_EGRESS_COUNTRY,
        ).strip().upper(),
        profile_dir=_path(
            (
                env.get("CBRS_CLOAK_PROFILE_DIR")
                if browser_backend == "cloak"
                else env.get("CBRS_PROFILE_DIR")
            ),
            default=default_profile_dir,
            root=root,
        ),
        cloak_cache_dir=_path(
            env.get("CBRS_CLOAK_CACHE_DIR"),
            default=".cbrs/cloak-cache",
            root=root,
        ),
        cloak_fingerprint_seed=_empty_to_none(env.get("CBRS_CLOAK_FINGERPRINT_SEED")),
        cloak_proxy_url=_empty_to_none(env.get("CBRS_CLOAK_PROXY_URL")),
        proxy_url=_empty_to_none(env.get("CBRS_PROXY_URL")),
        captcha_solver_mode=captcha_solver_mode,
        two_captcha_api_key=two_captcha_api_key,
        two_captcha_min_score=two_captcha_min_score,
        two_captcha_timeout_seconds=two_captcha_timeout_seconds,
        two_captcha_poll_seconds=two_captcha_poll_seconds,
        two_captcha_daily_limit=two_captcha_daily_limit,
        two_captcha_circuit_breaker_seconds=two_captcha_circuit_breaker_seconds,
        two_captcha_rejection_cooldown_seconds=two_captcha_rejection_cooldown_seconds,
        captcha_state_path=_path(
            env.get("CBRS_CAPTCHA_STATE_PATH"),
            default=".cbrs/pool/pool.sqlite3",
            root=root,
        ),
        account_id=None,
        proxy_recheck_seconds=proxy_recheck_seconds,
        allow_cloak_auto_update=_bool(env.get("CBRS_ALLOW_CLOAK_AUTO_UPDATE")),
        output_dir=_path(env.get("CBRS_OUTPUT_DIR"), default="outputs", root=root),
        log_dir=_path(env.get("CBRS_LOG_DIR"), default=".cbrs/logs", root=root),
        request_delay_seconds=request_delay,
        use_curl_cffi_for_images=_bool(env.get("CBRS_USE_CURL_CFFI_FOR_IMAGES")),
        curl_cffi_impersonate=env.get("CBRS_CURL_CFFI_IMPERSONATE", "chrome120"),
    )


SETTINGS = load_settings()


def proxy_metadata(settings: Settings = SETTINGS) -> dict[str, object]:
    if not settings.proxy_url:
        return {
            "proxy_configured": False,
            "proxy_scheme": None,
            "proxy_host_hash": None,
            "proxy_port": None,
        }
    parsed = urlparse(settings.proxy_url)
    host = parsed.hostname or ""
    return {
        "proxy_configured": True,
        "proxy_scheme": parsed.scheme.lower(),
        "proxy_host_hash": _hash_text(host) if host else None,
        "proxy_port": parsed.port,
    }


def _hash_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

# Backwards-compatible constants for small scripts/importers.
BASE_URL = SETTINGS.base_url
RECAPTCHA_SITEKEY = SETTINGS.recaptcha_sitekey
PROFILE_DIR = SETTINGS.profile_dir
OUTPUT_DIR = SETTINGS.output_dir
