from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency in offline tests
    load_dotenv = None


DEFAULT_BASE_URL = "https://nuevo-portal.conservador.cl"
DEFAULT_SITEKEY = "6Le-eiksAAAAANU-0ITcjxvGfFoHsz40juvUVI_-"
SECRET_KEYWORDS = ("PASSWORD", "TOKEN", "COOKIE", "SECRET", "KEY", "AUTH", "JWT", "TICKET")


@dataclass(frozen=True)
class AccountConfig:
    label: str
    email: str
    password: str

    @property
    def email_hash(self) -> str:
        return hashlib.sha256(self.email.lower().encode("utf-8")).hexdigest()[:16]

    @property
    def display_label(self) -> str:
        local, _, domain = self.email.partition("@")
        if not domain:
            return self.label
        prefix = local[:2] + "***" if local else "***"
        return f"{self.label}:{prefix}@{domain}"


@dataclass(frozen=True)
class Settings:
    base_url: str = DEFAULT_BASE_URL
    recaptcha_sitekey: str = DEFAULT_SITEKEY
    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/cbrs.sqlite3"
    browser_profile_dir: Path = Path(".local/browser-profile")
    headless: bool = False
    min_request_delay_ms: int = 30000
    request_jitter_percent: int = 20
    transient_backoff_ms: tuple[int, ...] = (120000, 300000, 600000)
    daily_query_budget_per_account: int = 0
    max_job_attempts: int = 3
    max_live_requests_per_run: int = 12
    max_waf_failures_per_run: int = 0
    max_captcha_failures_per_run: int = 0
    max_auth_failures_per_run: int = 1
    max_transient_failures_per_run: int = 2
    curl_impersonate: str = "chrome120"
    accounts: list[AccountConfig] = field(default_factory=list)

    @classmethod
    def from_env(cls, *, env_file: str | Path | None = None) -> "Settings":
        if load_dotenv:
            load_dotenv(env_file or ".env", override=False)

        data_dir = Path(_env("CBRS_DATA_DIR", "data"))
        database_url = _env("CBRS_DATABASE_URL", f"sqlite:///{(data_dir / 'cbrs.sqlite3').as_posix()}")
        browser_profile_dir = Path(_env("CBRS_BROWSER_PROFILE_DIR", ".local/browser-profile"))

        return cls(
            base_url=_env("CBRS_PORTAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            recaptcha_sitekey=_env("CBRS_RECAPTCHA_SITEKEY", DEFAULT_SITEKEY),
            data_dir=data_dir,
            database_url=database_url,
            browser_profile_dir=browser_profile_dir,
            headless=_bool(_env("CBRS_HEADLESS", "false")),
            min_request_delay_ms=_int(_env("CBRS_MIN_REQUEST_DELAY_MS", "30000"), 30000),
            request_jitter_percent=_int(_env("CBRS_REQUEST_JITTER_PERCENT", "20"), 20),
            transient_backoff_ms=_int_tuple(
                _env("CBRS_TRANSIENT_BACKOFF_MS", "120000,300000,600000"),
                (120000, 300000, 600000),
            ),
            daily_query_budget_per_account=_int(
                _env("CBRS_DAILY_QUERY_BUDGET_PER_ACCOUNT", "0"), 0
            ),
            max_job_attempts=_int(_env("CBRS_MAX_JOB_ATTEMPTS", "3"), 3),
            max_live_requests_per_run=_int(_env("CBRS_MAX_LIVE_REQUESTS_PER_RUN", "12"), 12),
            max_waf_failures_per_run=_int(_env("CBRS_MAX_WAF_FAILURES_PER_RUN", "0"), 0),
            max_captcha_failures_per_run=_int(
                _env("CBRS_MAX_CAPTCHA_FAILURES_PER_RUN", "0"), 0
            ),
            max_auth_failures_per_run=_int(_env("CBRS_MAX_AUTH_FAILURES_PER_RUN", "1"), 1),
            max_transient_failures_per_run=_int(
                _env("CBRS_MAX_TRANSIENT_FAILURES_PER_RUN", "2"), 2
            ),
            curl_impersonate=_env("CBRS_CURL_IMPERSONATE", "chrome120"),
            accounts=_load_accounts(),
        )

    @property
    def sqlite_path(self) -> Path:
        parsed = urlparse(self.database_url)
        if parsed.scheme != "sqlite":
            raise ValueError(f"Only sqlite database URLs are supported, got {parsed.scheme!r}")
        if parsed.netloc and parsed.netloc != "":
            raise ValueError("SQLite URL must use sqlite:///absolute-or-relative-path")
        raw_path = parsed.path
        if raw_path.startswith("/") and len(raw_path) > 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        return Path(raw_path)

    def ensure_local_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)

    def redacted_dict(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "recaptcha_sitekey": redact(self.recaptcha_sitekey),
            "data_dir": str(self.data_dir),
            "database_url": self.database_url,
            "browser_profile_dir": str(self.browser_profile_dir),
            "headless": self.headless,
            "min_request_delay_ms": self.min_request_delay_ms,
            "request_jitter_percent": self.request_jitter_percent,
            "transient_backoff_ms": list(self.transient_backoff_ms),
            "daily_query_budget_per_account": self.daily_query_budget_per_account,
            "daily_query_budget_note": "deprecated: 0 means unlimited; not a live request cap",
            "max_job_attempts": self.max_job_attempts,
            "deprecated_live_caps": "ignored by signal-based live safety",
            "curl_impersonate": self.curl_impersonate,
            "accounts": [a.display_label for a in self.accounts],
        }


def redact(value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(keyword in upper for keyword in SECRET_KEYWORDS)


def redact_mapping(mapping: dict[str, object]) -> dict[str, object]:
    return {key: redact(value) if is_secret_name(key) else value for key, value in mapping.items()}


def find_chrome_path() -> str | None:
    for name in ("chrome", "chrome.exe", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return None


def _env(name: str, default: str) -> str:
    value = os.getenv(name) or _windows_user_env(name)
    return default if value is None or value == "" else value


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _int_tuple(value: str, default: tuple[int, ...]) -> tuple[int, ...]:
    try:
        parsed = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except (TypeError, ValueError):
        return default
    return parsed or default


def _load_accounts() -> list[AccountConfig]:
    accounts: list[AccountConfig] = []
    for index in range(1, 100):
        email = (
            os.getenv(f"CBRS_USER_{index}")
            or _windows_user_env(f"CBRS_USER_{index}")
            or os.getenv(f"USER_{index}")
            or _windows_user_env(f"USER_{index}")
        )
        password = (
            os.getenv(f"CBRS_PASSWORD_{index}")
            or _windows_user_env(f"CBRS_PASSWORD_{index}")
            or os.getenv(f"PASSWORD_{index}")
            or _windows_user_env(f"PASSWORD_{index}")
        )
        if not email and not password:
            break
        if email and password:
            accounts.append(AccountConfig(label=f"account-{index}", email=email, password=password))
    return accounts


def _windows_user_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return str(value)
    except Exception:
        return None
