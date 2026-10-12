"""End-to-end CBRS portal account registration.

Deterministic pipeline: RUT supply (``rut_data.db``) -> identity ->
``/api/v1/account/create`` (reCAPTCHA Enterprise action ``signup``) ->
activation email via catch-all domain + IMAP -> ``/api/v1/account/activation``
-> ``/api/v1/auth/login`` (action ``login``) -> ``/api/v1/user/me`` ->
credentials stored in ``registered_accounts.db``.

Public entry points::

    from cbrs.registration import register_new_accounts
    results = register_new_accounts(count=1)

CLI::

    python -m cbrs.registration register --count 1
"""

from .config import RegistrationSettings, load_registration_settings
from .email_inbox import (
    ActivationEmailTimeout,
    ActivationLink,
    ImapActivationInbox,
    build_activation_address,
    parse_activation_link,
)
from .identity import (
    RegistrationIdentity,
    build_identity,
    generate_password,
    password_meets_requirements,
)
from .orchestrator import (
    RegistrationOrchestrator,
    RegistrationResult,
    choose_sticky_port,
)
from .portal import PortalError, RegistrationPortal
from .rut_provider import (
    NoRutsAvailable,
    PersonIdentity,
    RutProvider,
    RutRecord,
    is_company_name,
    parse_person_name,
)
from .store import AccountRecord, AccountStore, append_to_account_pool


def register_new_accounts(
    count: int = 1,
    *,
    settings: RegistrationSettings | None = None,
    dry_run: bool = False,
) -> list[RegistrationResult]:
    """Convenience wrapper: register ``count`` accounts end to end."""

    orchestrator = RegistrationOrchestrator(settings)
    return orchestrator.register_accounts(count=count, dry_run=dry_run)


__all__ = [
    "AccountRecord",
    "AccountStore",
    "ActivationEmailTimeout",
    "ActivationLink",
    "ImapActivationInbox",
    "NoRutsAvailable",
    "PersonIdentity",
    "PortalError",
    "RegistrationIdentity",
    "RegistrationOrchestrator",
    "RegistrationPortal",
    "RegistrationResult",
    "RegistrationSettings",
    "RutProvider",
    "RutRecord",
    "append_to_account_pool",
    "build_activation_address",
    "build_identity",
    "choose_sticky_port",
    "generate_password",
    "is_company_name",
    "load_registration_settings",
    "parse_activation_link",
    "parse_person_name",
    "password_meets_requirements",
    "register_new_accounts",
]
