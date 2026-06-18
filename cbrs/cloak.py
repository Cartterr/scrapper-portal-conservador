from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import CLOAK_REQUIRED_VERSION, SETTINGS, Settings

SEED_RE = re.compile(r"^[0-9]{4,18}$")
ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5"}


@dataclass(frozen=True)
class CloakStatus:
    package_available: bool
    package_version: str | None
    package_version_ok: bool
    binary_installed: bool | None
    binary_version: str | None
    binary_path: str | None
    binary_error: str | None
    auto_update_disabled: bool
    cache_dir: Path
    profile_dir: Path
    seed_hash: str
    seed_source: str


def apply_cloak_environment(settings: Settings = SETTINGS) -> None:
    settings.cloak_cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CLOAKBROWSER_CACHE_DIR"] = str(settings.cloak_cache_dir)
    if not settings.allow_cloak_auto_update:
        os.environ["CLOAKBROWSER_AUTO_UPDATE"] = "false"


def cloak_launch_args(settings: Settings = SETTINGS) -> list[str]:
    return [f"--fingerprint={get_cloak_fingerprint_seed(settings)}"]


def cloak_proxy(settings: Settings = SETTINGS) -> str | None:
    if not settings.cloak_proxy_url:
        return None

    parsed = urlparse(settings.cloak_proxy_url)
    if parsed.scheme.lower() not in ALLOWED_PROXY_SCHEMES:
        raise ValueError(
            "CBRS_CLOAK_PROXY_URL must start with http://, https://, or socks5://."
        )
    if not parsed.hostname or not parsed.port:
        raise ValueError("CBRS_CLOAK_PROXY_URL must include a proxy host and port.")
    return settings.cloak_proxy_url


def cloak_proxy_metadata(settings: Settings = SETTINGS) -> dict[str, Any]:
    if not settings.cloak_proxy_url:
        return {
            "cloak_proxy_configured": False,
            "cloak_proxy_scheme": None,
            "cloak_proxy_host_hash": None,
            "cloak_proxy_port": None,
        }

    parsed = urlparse(settings.cloak_proxy_url)
    host = parsed.hostname or ""
    return {
        "cloak_proxy_configured": True,
        "cloak_proxy_scheme": parsed.scheme.lower(),
        "cloak_proxy_host_hash": hashlib.sha256(host.encode("utf-8")).hexdigest()[:12],
        "cloak_proxy_port": parsed.port,
    }


def get_cloak_fingerprint_seed(settings: Settings = SETTINGS) -> str:
    if settings.cloak_fingerprint_seed:
        return _validate_seed(settings.cloak_fingerprint_seed)

    seed_file = cloak_seed_file(settings)
    if seed_file.exists():
        return _validate_seed(seed_file.read_text(encoding="utf-8").strip())

    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed = str(10000 + secrets.randbelow(90000))
    seed_file.write_text(seed, encoding="utf-8")
    return seed


def cloak_seed_file(settings: Settings = SETTINGS) -> Path:
    return settings.profile_dir.parent / "cloak" / "fingerprint-seed.txt"


def cloak_seed_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def cloak_runtime_metadata(settings: Settings = SETTINGS) -> dict[str, Any]:
    seed = get_cloak_fingerprint_seed(settings)
    status = get_cloak_status(settings)
    return {
        "browser_backend": "cloakbrowser",
        "cloak_package_version": status.package_version,
        "cloak_binary_version": status.binary_version,
        "cloak_binary_installed": status.binary_installed,
        "cloak_seed_hash": cloak_seed_hash(seed),
        **cloak_proxy_metadata(settings),
    }


def get_cloak_status(settings: Settings = SETTINGS) -> CloakStatus:
    apply_cloak_environment(settings)
    seed = get_cloak_fingerprint_seed(settings)
    package_version = _package_version()
    binary_installed: bool | None = None
    binary_version: str | None = None
    binary_path: str | None = os.getenv("CLOAKBROWSER_BINARY_PATH") or None
    binary_error: str | None = None
    override_exists = bool(binary_path and Path(binary_path).exists())

    if package_version is not None:
        try:
            import cloakbrowser

            info_func = getattr(cloakbrowser, "binary_info", None)
            if callable(info_func):
                info = info_func()
                binary_installed = bool(info.get("installed"))
                binary_version = _none_if_empty(info.get("version"))
                binary_path = (
                    _none_if_empty(info.get("binary_path"))
                    or _none_if_empty(info.get("path"))
                    or binary_path
                )
        except Exception as exc:
            binary_error = str(exc)

    if override_exists:
        binary_installed = True
        binary_path = os.getenv("CLOAKBROWSER_BINARY_PATH") or binary_path

    return CloakStatus(
        package_available=package_version is not None,
        package_version=package_version,
        package_version_ok=package_version == CLOAK_REQUIRED_VERSION,
        binary_installed=binary_installed,
        binary_version=binary_version,
        binary_path=binary_path,
        binary_error=binary_error,
        auto_update_disabled=os.getenv("CLOAKBROWSER_AUTO_UPDATE", "").lower() == "false",
        cache_dir=settings.cloak_cache_dir,
        profile_dir=settings.profile_dir,
        seed_hash=cloak_seed_hash(seed),
        seed_source="env" if settings.cloak_fingerprint_seed else "local_file",
    )


def _package_version() -> str | None:
    try:
        return importlib.metadata.version("cloakbrowser")
    except importlib.metadata.PackageNotFoundError:
        return None


def _validate_seed(seed: str) -> str:
    seed = seed.strip()
    if not SEED_RE.fullmatch(seed):
        raise ValueError(
            "CBRS_CLOAK_FINGERPRINT_SEED must be 4-18 digits; "
            "do not include spaces, flags, or shell syntax."
        )
    return seed


def _none_if_empty(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None
