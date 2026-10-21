"""Configuration for the CBRS account-registration pipeline.

Reads the repository ``.env`` (same precedence as the worker: explicit process
environment wins) and adds registration-specific keys.  Nothing here touches
the live worker, its browsers, or its proxy routes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import DEFAULT_RECAPTCHA_SITEKEY, _merged_env
from ..dataimpulse import (
    DEFAULT_DATAIMPULSE_COUNTRY,
    DEFAULT_DATAIMPULSE_HOST,
    DEFAULT_DATAIMPULSE_PORT_MAX,
    DEFAULT_DATAIMPULSE_PORT_MIN,
    DEFAULT_DATAIMPULSE_STICKY_TTL_MINUTES,
)

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_CELULAR = "986552408"
DEFAULT_ACTIVATION_TIMEOUT_SECONDS = 600.0
DEFAULT_ACTIVATION_POLL_SECONDS = 15.0
DEFAULT_REQUEST_DELAY_SECONDS = 2.0

# Plausible Santiago addresses used for the required "Direccion" field
# (contact/billing data).  One is chosen deterministically per RUT.
DEFAULT_ADDRESS_POOL: tuple[str, ...] = (
    "Av. Providencia 1234, Of. 56, Providencia, Santiago",
    "Av. Apoquindo 4501, Depto 1201, Las Condes, Santiago",
    "Calle Nueva York 53, Of. 42, Santiago Centro, Santiago",
    "Av. Vitacura 2939, Of. 901, Vitacura, Santiago",
    "Calle Merced 320, Depto 1502, Santiago Centro, Santiago",
    "Av. Los Leones 946, Of. 305, Providencia, Santiago",
)


def _bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None, *, default: float) -> float:
    try:
        return float(str(value).strip()) if value is not None else default
    except ValueError:
        return default


def _int(value: str | None, *, default: int) -> int:
    try:
        return int(str(value).strip()) if value is not None else default
    except ValueError:
        return default


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().strip('"').strip("'")
    return stripped or None


@dataclass(frozen=True)
class RegistrationSettings:
    base_url: str
    recaptcha_sitekey: str

    # DataImpulse (fresh sticky exit per registration run)
    dataimpulse_login: str | None
    dataimpulse_password: str | None
    dataimpulse_host: str
    dataimpulse_country: str
    dataimpulse_ttl_minutes: int
    dataimpulse_scheme: str
    dataimpulse_port_min: int
    dataimpulse_port_max: int

    # CapSolver fallback for reCAPTCHA Enterprise tokens
    capsolver_api_key: str | None = field(repr=False)
    capsolver_timeout_seconds: float
    capsolver_poll_seconds: float
    capsolver_min_score: float

    # Activation inbox (catch-all domain forwarded to one Gmail, read via IMAP)
    imap_host: str
    imap_port: int
    imap_user: str | None
    imap_password: str | None
    activation_domain: str | None
    activation_timeout_seconds: float
    activation_poll_seconds: float

    # Identity inputs
    celular: str
    address_pool: tuple[str, ...]

    # Storage
    rut_db_path: Path
    accounts_db_path: Path
    pool_state_path: Path
    profile_root: Path

    # Browser: the portal rejects headless Chrome reCAPTCHA (D29/D30), so the
    # registration browser is headful regular Chrome unless explicitly forced.
    headless: bool
    request_delay_seconds: float

    # Activation inbox transport (any IMAP provider; see email_inbox).
    imap_ssl: bool = True
    imap_starttls: bool = False
    imap_mailbox: str | None = None

    # Phone: when true each account gets a fresh made-up number instead of the
    # single fixed CBRS_REGISTRATION_CELULAR value.
    randomize_celular: bool = False

    # Opt-in visual diagnostics, saved only under this run's temporary profile.
    capture_screenshots: bool = False

    # A deliberate standalone CLI run may enable bounded paid fallback; this
    # never changes the worker's manual/automatic solver preference.
    external_captcha: bool = False
    two_captcha_api_key: str | None = field(default=None, repr=False)
    two_captcha_timeout_seconds: float = 120.0
    two_captcha_poll_seconds: float = 5.0
    captcha_daily_limit: int = 60
    max_paid_solves: int = 4
    route_candidates: int = 10
    route_retry_seconds: float = 10.0
    route_hourly_limit: int = 30
    route_daily_submit_limit: int = 10

    def require_activation_inbox(self) -> None:
        missing = [
            name
            for name, value in (
                ("CBRS_ACTIVATION_DOMAIN", self.activation_domain),
                ("CBRS_ACTIVATION_IMAP_USER", self.imap_user),
                ("CBRS_ACTIVATION_IMAP_PASSWORD", self.imap_password),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Registration needs a readable activation inbox. Set "
                + ", ".join(missing)
                + " in .env (a catch-all domain forwarding to any IMAP mailbox)."
            )

    def require_proxy(self) -> None:
        if not self.dataimpulse_login or not self.dataimpulse_password:
            raise RuntimeError(
                "DATAIMPULSE_PROXY_LOGIN and DATAIMPULSE_PROXY_PASSWORD are "
                "required to build a fresh Chile sticky exit."
            )


def load_registration_settings(dotenv_path: str | Path = ".env") -> RegistrationSettings:
    env = _merged_env(dotenv_path)

    celular = _empty_to_none(env.get("CBRS_REGISTRATION_CELULAR")) or DEFAULT_CELULAR
    if len(celular) != 9 or not celular.isdigit() or not celular.startswith("9"):
        raise ValueError(
            "CBRS_REGISTRATION_CELULAR must be the 9-digit national number "
            "starting with 9 (e.g. 986552408)."
        )

    address_override = _empty_to_none(env.get("CBRS_REGISTRATION_DIRECCION"))
    address_pool = (address_override,) if address_override else DEFAULT_ADDRESS_POOL

    min_score = _float(env.get("CBRS_2CAPTCHA_MIN_SCORE"), default=0.9)
    if min_score not in {0.3, 0.7, 0.9}:
        min_score = 0.9

    return RegistrationSettings(
        base_url=(
            _empty_to_none(env.get("CBRS_BASE_URL")) or "https://nuevo-portal.conservador.cl"
        ).rstrip("/"),
        recaptcha_sitekey=(
            _empty_to_none(env.get("CBRS_RECAPTCHA_SITEKEY")) or DEFAULT_RECAPTCHA_SITEKEY
        ),
        dataimpulse_login=_empty_to_none(env.get("DATAIMPULSE_PROXY_LOGIN")),
        dataimpulse_password=_empty_to_none(env.get("DATAIMPULSE_PROXY_PASSWORD")),
        dataimpulse_host=(
            _empty_to_none(env.get("DATAIMPULSE_PROXY_HOST")) or DEFAULT_DATAIMPULSE_HOST
        ),
        dataimpulse_country=(
            _empty_to_none(env.get("DATAIMPULSE_COUNTRY")) or DEFAULT_DATAIMPULSE_COUNTRY
        ).lower(),
        dataimpulse_ttl_minutes=_int(
            env.get("DATAIMPULSE_STICKY_TTL_MINUTES"),
            default=DEFAULT_DATAIMPULSE_STICKY_TTL_MINUTES,
        ),
        dataimpulse_scheme=(
            _empty_to_none(env.get("DATAIMPULSE_PROXY_SCHEME")) or "http"
        ).lower(),
        dataimpulse_port_min=_int(
            env.get("DATAIMPULSE_STICKY_PORT_MIN"), default=DEFAULT_DATAIMPULSE_PORT_MIN
        ),
        dataimpulse_port_max=_int(
            env.get("DATAIMPULSE_STICKY_PORT_MAX"), default=DEFAULT_DATAIMPULSE_PORT_MAX
        ),
        capsolver_api_key=_empty_to_none(env.get("CBRS_CAPSOLVER_API_KEY")),
        capsolver_timeout_seconds=_float(
            env.get("CBRS_CAPSOLVER_TIMEOUT_SECONDS"), default=120.0
        ),
        capsolver_poll_seconds=_float(env.get("CBRS_CAPSOLVER_POLL_SECONDS"), default=3.0),
        capsolver_min_score=min_score,
        imap_host=(
            _empty_to_none(env.get("CBRS_ACTIVATION_IMAP_HOST")) or DEFAULT_IMAP_HOST
        ),
        imap_port=_int(env.get("CBRS_ACTIVATION_IMAP_PORT"), default=DEFAULT_IMAP_PORT),
        imap_user=_empty_to_none(env.get("CBRS_ACTIVATION_IMAP_USER")),
        imap_password=_empty_to_none(env.get("CBRS_ACTIVATION_IMAP_PASSWORD")),
        activation_domain=_empty_to_none(env.get("CBRS_ACTIVATION_DOMAIN")),
        activation_timeout_seconds=_float(
            env.get("CBRS_REGISTRATION_ACTIVATION_TIMEOUT_SECONDS"),
            default=DEFAULT_ACTIVATION_TIMEOUT_SECONDS,
        ),
        activation_poll_seconds=_float(
            env.get("CBRS_REGISTRATION_ACTIVATION_POLL_SECONDS"),
            default=DEFAULT_ACTIVATION_POLL_SECONDS,
        ),
        celular=celular,
        address_pool=address_pool,
        rut_db_path=Path(
            _empty_to_none(env.get("CBRS_REGISTRATION_RUT_DB")) or "rut_data.db"
        ),
        accounts_db_path=Path(
            _empty_to_none(env.get("CBRS_REGISTRATION_ACCOUNTS_DB"))
            or "registered_accounts.db"
        ),
        # Same state DB the worker writes its live sticky routes to.  Pointing
        # at a stale path silently disabled the port-collision guard, so a
        # registration could have reused a production exit IP.
        pool_state_path=Path(
            _empty_to_none(env.get("CBRS_REGISTRATION_POOL_STATE_DB"))
            or str(Path(".cbrs") / "runtime" / "pool" / "pool.sqlite3")
        ),
        profile_root=Path(
            _empty_to_none(env.get("CBRS_REGISTRATION_PROFILE_ROOT"))
            or str(Path(".cbrs") / "registration-profiles")
        ),
        headless=_bool(env.get("CBRS_REGISTRATION_HEADLESS"), default=False),
        request_delay_seconds=_float(
            env.get("CBRS_REGISTRATION_REQUEST_DELAY_SECONDS"),
            default=DEFAULT_REQUEST_DELAY_SECONDS,
        ),
        imap_ssl=_bool(env.get("CBRS_ACTIVATION_IMAP_SSL"), default=True),
        imap_starttls=_bool(env.get("CBRS_ACTIVATION_IMAP_STARTTLS"), default=False),
        imap_mailbox=_empty_to_none(env.get("CBRS_ACTIVATION_IMAP_MAILBOX")),
        randomize_celular=_bool(
            env.get("CBRS_REGISTRATION_RANDOMIZE_CELULAR"), default=False
        ),
        two_captcha_api_key=_empty_to_none(env.get("CBRS_2CAPTCHA_API_KEY")),
        two_captcha_timeout_seconds=_float(
            env.get("CBRS_2CAPTCHA_TIMEOUT_SECONDS"), default=120.0
        ),
        two_captcha_poll_seconds=max(5.0, _float(
            env.get("CBRS_2CAPTCHA_POLL_SECONDS"), default=5.0
        )),
        captcha_daily_limit=_int(env.get("CBRS_2CAPTCHA_DAILY_LIMIT"), default=60),
        route_candidates=max(1, _int(env.get("CBRS_DATAIMPULSE_CANDIDATES_PER_RECOVERY"), default=10)),
        route_retry_seconds=max(0.0, _float(env.get("CBRS_DATAIMPULSE_CANDIDATE_RETRY_SECONDS"), default=10.0)),
        route_hourly_limit=max(1, _int(env.get("CBRS_DATAIMPULSE_MAX_ROTATIONS_PER_HOUR"), default=30)),
        route_daily_submit_limit=max(1, _int(env.get("CBRS_DATAIMPULSE_MAX_CANDIDATE_LOGINS_PER_DAY"), default=10)),
    )
