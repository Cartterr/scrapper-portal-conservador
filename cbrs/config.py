from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    allow_cloak_auto_update: bool
    output_dir: Path
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
        allow_cloak_auto_update=_bool(env.get("CBRS_ALLOW_CLOAK_AUTO_UPDATE")),
        output_dir=_path(env.get("CBRS_OUTPUT_DIR"), default="outputs", root=root),
        request_delay_seconds=request_delay,
        use_curl_cffi_for_images=_bool(env.get("CBRS_USE_CURL_CFFI_FOR_IMAGES")),
        curl_cffi_impersonate=env.get("CBRS_CURL_CFFI_IMPERSONATE", "chrome120"),
    )


SETTINGS = load_settings()

# Backwards-compatible constants for small scripts/importers.
BASE_URL = SETTINGS.base_url
RECAPTCHA_SITEKEY = SETTINGS.recaptcha_sitekey
PROFILE_DIR = SETTINGS.profile_dir
OUTPUT_DIR = SETTINGS.output_dir
