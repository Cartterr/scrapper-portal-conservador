"""Deterministic end-to-end registration orchestration.

Pipeline per account (all steps observable, resumable, and idempotent in the
local stores)::

    reserve RUT -> build identity -> create (reCAPTCHA 'signup')
      -> wait activation email (IMAP) -> activate -> login ('login')
      -> verify (user/me) -> store credentials

The orchestrator owns *new* resources only: a fresh DataImpulse sticky port
(never a production route), a fresh temporary Chrome profile (never a
production profile), and its own headful browser process.  It never signals,
restarts, or inspects the live worker or its browsers.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ..dataimpulse import build_dataimpulse_proxy_url, next_unused_sticky_port
from .config import RegistrationSettings, load_registration_settings
from .email_inbox import (
    ActivationEmailTimeout,
    ActivationInbox,
    ActivationLink,
    ImapActivationInbox,
    build_activation_address,
)
from .identity import build_identity
from .portal import PortalError, RegistrationPortal
from .rut_provider import (
    NoRutsAvailable,
    PersonIdentity,
    RutProvider,
    RutRecord,
    parse_person_name,
)
from .store import AccountStore

logger = logging.getLogger(__name__)

# Human-pacing jitter: a short, randomized pause between page load, bootstrap
# and the reCAPTCHA token so the run does not look like a script.  reCAPTCHA and
# WAF scoring reward realistic cadence; a fixed, too-quick sequence can hurt
# reputation.  Uniform in [base, base + spread] seconds.
HUMAN_PAUSE_BASE = 1.5
HUMAN_PAUSE_SPREAD = 2.5


def human_delay(rng: random.Random) -> float:
    """Return a jittered human-like delay in seconds (uniform)."""

    return HUMAN_PAUSE_BASE + rng.random() * HUMAN_PAUSE_SPREAD


@dataclass(frozen=True)
class RegistrationResult:
    rut: str
    email: str
    status: str  # verified | created | activated | failed | dry_run
    stage_failed: str | None = None
    reason: str | None = None
    portal_user_id: str | None = None


class _PortalProtocol(Protocol):
    def open(self) -> Any: ...
    def close(self) -> None: ...
    def check_egress_country(self, expected: str) -> str: ...
    def bootstrap(self) -> None: ...
    def prepare_registration_form(self, payload: dict[str, Any]) -> None: ...
    def recaptcha_token(self, action: str) -> str: ...
    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def activate(self, email: str, token: str) -> dict[str, Any]: ...
    def resend_activation(self, email: str) -> None: ...
    def login(self, email: str, password: str) -> str: ...
    def fetch_me(self, jwt: str) -> dict[str, Any]: ...


class _InboxProtocol(ActivationInbox, Protocol):
    """Backwards-compatible alias for the public inbox contract."""


def choose_sticky_port(
    settings: RegistrationSettings,
    store: AccountStore,
    rng: random.Random | None = None,
    *, excluded_ports: set[int] | None = None,
) -> int:
    """Pick a DataImpulse sticky port that no production or past run uses."""

    rng = rng or random.Random()
    used: set[int] = set(excluded_ports or ())
    if settings.pool_state_path.is_file():
        try:
            with sqlite3.connect(settings.pool_state_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    "SELECT active_port FROM account_proxy_routes "
                    "WHERE active_port IS NOT NULL"
                ).fetchall()
            used.update(int(row[0]) for row in rows if row[0] is not None)
        except sqlite3.Error:
            logger.warning(
                "Could not read production proxy routes from %s; continuing "
                "without collision avoidance.",
                settings.pool_state_path,
            )
    try:
        with sqlite3.connect(store.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT proxy_port FROM registered_accounts "
                "WHERE proxy_port IS NOT NULL"
            ).fetchall()
        used.update(int(row[0]) for row in rows if row[0] is not None)
    except sqlite3.Error:
        pass
    start = rng.randint(settings.dataimpulse_port_min, settings.dataimpulse_port_max)
    return next_unused_sticky_port(
        start,
        used_ports=used,
        minimum=settings.dataimpulse_port_min,
        maximum=settings.dataimpulse_port_max,
    )


def _default_portal_factory(
    settings: RegistrationSettings, proxy_url: str, profile_dir: Path
) -> _PortalProtocol:
    return RegistrationPortal(settings, proxy_url=proxy_url, profile_dir=profile_dir)


def _default_inbox_factory(settings: RegistrationSettings) -> _InboxProtocol:
    assert settings.imap_user and settings.imap_password
    candidates = (
        (settings.imap_mailbox, "INBOX")
        if settings.imap_mailbox
        else ("[Gmail]/All Mail", "INBOX")
    )
    return ImapActivationInbox(
        settings.imap_host,
        settings.imap_port,
        settings.imap_user,
        settings.imap_password,
        mailbox_candidates=candidates,
        use_ssl=settings.imap_ssl,
        starttls=settings.imap_starttls,
    )


class RegistrationOrchestrator:
    def __init__(
        self,
        settings: RegistrationSettings | None = None,
        *,
        rut_provider: RutProvider | None = None,
        store: AccountStore | None = None,
        inbox_factory: Callable[[RegistrationSettings], _InboxProtocol] | None = None,
        portal_factory: Callable[
            [RegistrationSettings, str, Path], _PortalProtocol
        ] | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings or load_registration_settings()
        self.rut_provider = rut_provider or RutProvider(self.settings.rut_db_path)
        self.store = store or AccountStore(self.settings.accounts_db_path)
        self._inbox_factory = inbox_factory or _default_inbox_factory
        self._portal_factory = portal_factory or _default_portal_factory
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._now = now

    # -- public API ----------------------------------------------------------

    def register_accounts(self, count: int = 1, *, dry_run: bool = False) -> list[RegistrationResult]:
        """Register up to ``count`` new portal accounts end to end."""

        if count < 1:
            raise ValueError("count must be >= 1")
        self.settings.require_proxy()
        if not dry_run:
            self.settings.require_activation_inbox()

        port = choose_sticky_port(self.settings, self.store, self._rng)
        proxy_url = build_dataimpulse_proxy_url(
            login=self.settings.dataimpulse_login,
            password=self.settings.dataimpulse_password,
            host=self.settings.dataimpulse_host,
            country=self.settings.dataimpulse_country,
            ttl_minutes=self.settings.dataimpulse_ttl_minutes,
            port=port,
            scheme=self.settings.dataimpulse_scheme,
            port_min=self.settings.dataimpulse_port_min,
            port_max=self.settings.dataimpulse_port_max,
        )
        profile_dir = (
            self.settings.profile_root
            / datetime.now(timezone.utc).strftime("reg-%Y%m%d-%H%M%S")
        )
        logger.info(
            "Registration run: sticky port %d, profile %s, dry_run=%s",
            port,
            profile_dir,
            dry_run,
        )
        portal = self._portal_factory(self.settings, proxy_url, profile_dir)
        inbox = None if dry_run else self._inbox_factory(self.settings)
        results: list[RegistrationResult] = []
        try:
            portal.open()
            portal.check_egress_country(self.settings.dataimpulse_country)
            portal.bootstrap()
            self._human_pause()
            for index in range(count):
                if index:
                    self._sleep(self.settings.request_delay_seconds)
                try:
                    if dry_run:
                        results.append(self._dry_run_one(portal, port))
                    else:
                        assert inbox is not None
                        results.append(self._register_one(portal, inbox, port))
                except NoRutsAvailable:
                    # Fewer usable RUTs than requested: stop cleanly instead of
                    # aborting after a partially created account.  Results for
                    # the accounts already attempted stay available to the CLI.
                    logger.warning(
                        "RUT pool ran dry after %d/%d accounts; stopping this run.",
                        len(results),
                        count,
                    )
                    if not results:
                        raise
                    break
        finally:
            portal.close()
        return results

    def resume_pending(self) -> list[RegistrationResult]:
        """Finish accounts stuck at 'created' or 'activated' (no new RUTs)."""

        self.settings.require_proxy()
        self.settings.require_activation_inbox()
        pending = self.store.list_by_status("created") + self.store.list_by_status("activated")
        if not pending:
            return []
        port = choose_sticky_port(self.settings, self.store, self._rng)
        proxy_url = build_dataimpulse_proxy_url(
            login=self.settings.dataimpulse_login,
            password=self.settings.dataimpulse_password,
            host=self.settings.dataimpulse_host,
            country=self.settings.dataimpulse_country,
            ttl_minutes=self.settings.dataimpulse_ttl_minutes,
            port=port,
            scheme=self.settings.dataimpulse_scheme,
            port_min=self.settings.dataimpulse_port_min,
            port_max=self.settings.dataimpulse_port_max,
        )
        profile_dir = (
            self.settings.profile_root
            / datetime.now(timezone.utc).strftime("resume-%Y%m%d-%H%M%S")
        )
        portal = self._portal_factory(self.settings, proxy_url, profile_dir)
        inbox = self._inbox_factory(self.settings)
        results: list[RegistrationResult] = []
        try:
            portal.open()
            portal.check_egress_country(self.settings.dataimpulse_country)
            portal.bootstrap()
            for record in pending:
                results.append(self._resume_one(portal, inbox, record))
                self._sleep(self.settings.request_delay_seconds)
        finally:
            portal.close()
        return results

    # -- internals -----------------------------------------------------------

    def _dry_run_one(self, portal: _PortalProtocol, port: int) -> RegistrationResult:
        record = self.rut_provider.reserve_next()
        try:
            person = self._person(record)
            address = build_activation_address(
                self.settings.activation_domain or "dry-run.invalid",
                record.rut,
                self._rng,
            )
            identity = build_identity(
                person,
                email_address=address,
                address_pool=self.settings.address_pool,
                celular=self._celular(),
                rng=self._rng,
            )
            portal.prepare_registration_form(identity.create_payload(""))
            self._human_pause()
            token = portal.recaptcha_token("signup")
            payload = identity.create_payload(token)
            masked = {
                **payload,
                "recaptchaToken": f"<{len(token)} chars>",
                "password": "<masked>",
                "passwordConfirmar": "<masked>",
            }
            logger.info("Dry-run payload for %s: %s", record.rut, masked)
            return RegistrationResult(
                rut=record.rut, email=address, status="dry_run"
            )
        finally:
            # Nothing was submitted; the RUT goes back to the pool.
            self.rut_provider.release(record.rut)

    def _register_one(
        self, portal: _PortalProtocol, inbox: _InboxProtocol, port: int
    ) -> RegistrationResult:
        record = self.rut_provider.reserve_next()
        person = self._person(record)
        address = build_activation_address(
            self.settings.activation_domain or "", record.rut, self._rng
        )
        identity = build_identity(
            person,
            email_address=address,
            address_pool=self.settings.address_pool,
            celular=self._celular(),
            rng=self._rng,
        )
        self.store.insert_created(
            email=identity.email,
            password=identity.password,
            rut=record.rut,
            nombres=person.nombres,
            apellido_paterno=person.apellido_paterno,
            apellido_materno=person.apellido_materno,
            direccion=identity.direccion,
            celular=identity.celular,
            proxy_port=port,
        )
        started = self._now()

        # Stage: create
        try:
            self._human_pause()
            # The visible SPA form generates its browser token when clicked.
            portal.create_account(identity.create_payload(""))
        except PortalError as exc:
            return self._fail(record, identity, exc, consumed=not exc.retryable)

        # The portal accepted the RUT/email: the RUT is consumed from now on.
        self.rut_provider.mark_registered(record.rut)

        return self._finish_created_registration(portal, inbox, record, identity, started)

    def _finish_created_registration(self, portal, inbox, record, identity, started):
        """Complete an accepted signup on its exact live candidate, without rotating."""

        # Stage: wait for the activation email
        try:
            link = inbox.wait_for_activation(
                identity.email,
                not_before=started,
                timeout_seconds=self.settings.activation_timeout_seconds,
                poll_seconds=self.settings.activation_poll_seconds,
                sleep=self._sleep,
            )
        except ActivationEmailTimeout as exc:
            logger.warning(
                "Activation email not received for %s; run resume-pending later",
                identity.email,
            )
            return RegistrationResult(
                rut=record.rut,
                email=identity.email,
                status="created",
                stage_failed="wait_email",
                reason=str(exc)[:300],
            )

        # Stages: activate -> login -> verify
        return self._activate_login_verify(portal, record, identity, link)

    def _activate_login_verify(
        self,
        portal: _PortalProtocol,
        record: RutRecord,
        identity: Any,
        link: ActivationLink,
    ) -> RegistrationResult:
        try:
            portal.activate(link.email or identity.email, link.token)
        except PortalError as exc:
            return RegistrationResult(
                rut=record.rut,
                email=identity.email,
                status="created",
                stage_failed=exc.stage,
                reason=str(exc)[:300],
            )
        self.store.mark_activated(identity.email)
        self.rut_provider.mark_registered(record.rut)

        try:
            jwt = portal.login(identity.email, identity.password)
            me = portal.fetch_me(jwt)
        except PortalError as exc:
            return RegistrationResult(
                rut=record.rut,
                email=identity.email,
                status="activated",
                stage_failed=exc.stage,
                reason=str(exc)[:300],
            )
        user_id = me.get("idUsuario") if isinstance(me, dict) else None
        self.store.mark_verified(identity.email, portal_user_id=user_id)
        logger.info(
            "Account verified: %s (RUT %s, user id %s)",
            identity.email,
            record.rut,
            user_id,
        )
        return RegistrationResult(
            rut=record.rut,
            email=identity.email,
            status="verified",
            portal_user_id=str(user_id) if user_id is not None else None,
        )

    def _resume_one(
        self, portal: _PortalProtocol, inbox: _InboxProtocol, record: Any
    ) -> RegistrationResult:
        rut_record = RutRecord(id=0, rut=record.rut, name="")
        started = self._now()
        if record.status == "created":
            try:
                portal.resend_activation(record.email)
                link = inbox.wait_for_activation(
                    record.email,
                    not_before=started,
                    timeout_seconds=self.settings.activation_timeout_seconds,
                    poll_seconds=self.settings.activation_poll_seconds,
                    sleep=self._sleep,
                )
            except (PortalError, ActivationEmailTimeout) as exc:
                stage = exc.stage if isinstance(exc, PortalError) else "wait_email"
                return RegistrationResult(
                    rut=record.rut,
                    email=record.email,
                    status="created",
                    stage_failed=stage,
                    reason=str(exc)[:300],
                )
            identity = _ResumeIdentity(record)
            return self._activate_login_verify(portal, rut_record, identity, link)

        # status == 'activated': only login + verify remain
        try:
            jwt = portal.login(record.email, record.password)
            me = portal.fetch_me(jwt)
        except PortalError as exc:
            return RegistrationResult(
                rut=record.rut,
                email=record.email,
                status="activated",
                stage_failed=exc.stage,
                reason=str(exc)[:300],
            )
        user_id = me.get("idUsuario") if isinstance(me, dict) else None
        self.store.mark_verified(record.email, portal_user_id=user_id)
        return RegistrationResult(
            rut=record.rut,
            email=record.email,
            status="verified",
            portal_user_id=str(user_id) if user_id is not None else None,
        )

    def _fail(
        self,
        record: RutRecord,
        identity: Any,
        exc: PortalError,
        *,
        consumed: bool,
    ) -> RegistrationResult:
        if consumed:
            self.rut_provider.mark_failed(record.rut, exc.stage)
        else:
            self.rut_provider.release(record.rut)
        self.store.mark_failed(identity.email, exc.stage, str(exc))
        logger.warning(
            "Registration failed at %s for %s: %s", exc.stage, record.rut, exc
        )
        return RegistrationResult(
            rut=record.rut,
            email=identity.email,
            status="failed",
            stage_failed=exc.stage,
            reason=str(exc)[:300],
        )

    def _celular(self) -> str | None:
        """Fixed phone, or ``None`` so the identity makes one up per account."""

        return None if self.settings.randomize_celular else self.settings.celular

    def _human_pause(self) -> None:
        """Sleep a short jittered interval to pace the run like a human."""

        self._sleep(human_delay(self._rng))

    @staticmethod
    def _person(record: RutRecord) -> PersonIdentity:
        nombres, paterno, materno = parse_person_name(record.name)
        return PersonIdentity(
            rut=record.rut,
            nombres=nombres,
            apellido_paterno=paterno,
            apellido_materno=materno,
        )


class _ResumeIdentity:
    """Adapts a stored AccountRecord to the identity fields used on resume."""

    def __init__(self, record: Any) -> None:
        self.email = record.email
        self.password = record.password
