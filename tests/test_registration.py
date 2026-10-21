"""Offline registration tests; opt-in browser fixtures use localhost only."""

from __future__ import annotations

import imaplib
import os
import random
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cbrs.registration.config import RegistrationSettings, load_registration_settings
from cbrs.registration.email_inbox import (
    ActivationEmailTimeout,
    ActivationInbox,
    ActivationLink,
    ImapActivationInbox,
    build_activation_address,
    parse_activation_link,
)
from cbrs.registration.identity import (
    build_identity,
    choose_address,
    generate_celular,
    generate_password,
    password_meets_requirements,
)
from cbrs.registration.orchestrator import (
    RegistrationOrchestrator,
    choose_sticky_port,
    human_delay,
)
from cbrs.registration.portal import PortalError, PortalResponse, RegistrationPortal
from cbrs.registration.rut_provider import (
    NoRutsAvailable,
    PersonIdentity,
    RutProvider,
    is_company_name,
    parse_person_name,
)
from cbrs.registration.rut_refill import (
    default_terms,
    extract_pairs,
    insert_new_ruts,
    normalize_rut,
    parse_html_tables,
    validate_rut,
)
from cbrs.registration.store import AccountStore, append_to_account_pool


# ---------------------------------------------------------------------------
# Name / identity helpers
# ---------------------------------------------------------------------------

def test_parse_person_name_three_tokens():
    nombres, paterno, materno = parse_person_name("Sanchez Sepulveda Elsa Nieves")
    assert nombres == "Elsa Nieves"
    assert paterno == "Sanchez"
    assert materno == "Sepulveda"


def test_parse_person_name_two_tokens():
    nombres, paterno, materno = parse_person_name("Sepulveda Miguel")
    assert nombres == "Miguel"
    assert paterno == "Sepulveda"
    assert materno is None


def test_parse_person_name_single_token_raises():
    with pytest.raises(ValueError):
        parse_person_name("Carola")


def test_is_company_name():
    assert is_company_name("COMERCIAL ANDINA SPA")
    assert is_company_name("Inversiones Los Andes Ltda.")
    assert not is_company_name("Sanchez Sepulveda Elsa Nieves")


def test_generate_password_meets_portal_requirements():
    rng = random.Random(7)
    for _ in range(50):
        password = generate_password(rng)
        assert password_meets_requirements(password)
        assert len(password) >= 8


def test_choose_address_is_deterministic_per_rut():
    pool = ("A 1", "B 2", "C 3")
    assert choose_address(pool, "1.000.026-2") == choose_address(pool, "1.000.026-2")


def test_generate_celular_shape():
    rng = random.Random(5)
    for _ in range(50):
        number = generate_celular(rng)
        assert len(number) == 9
        assert number.isdigit()
        assert number.startswith("9")


def _person(rut="1.000.026-2", nombre="Sanchez Sepulveda Elsa Nieves"):
    nombres, paterno, materno = parse_person_name(nombre)
    return PersonIdentity(
        rut=rut, nombres=nombres, apellido_paterno=paterno, apellido_materno=materno
    )


def test_build_identity_makes_up_celular_when_unset():
    identity = build_identity(
        _person(),
        email_address="cbrs-1@example.com",
        address_pool=("Av. Providencia 1234",),
        celular=None,
        rng=random.Random(1),
    )
    assert len(identity.celular) == 9 and identity.celular.startswith("9")


def test_build_identity_keeps_fixed_celular():
    identity = build_identity(
        _person(),
        email_address="cbrs-1@example.com",
        address_pool=("Av. Providencia 1234",),
        celular="986552408",
        rng=random.Random(1),
    )
    assert identity.celular == "986552408"


# ---------------------------------------------------------------------------
# Activation inbox (provider-agnostic IMAP)
# ---------------------------------------------------------------------------

def test_imap_inbox_requires_credentials():
    with pytest.raises(ValueError):
        ImapActivationInbox("imap.example.com", 993, "", "p")


def test_imap_inbox_uses_ssl_by_default(monkeypatch):
    recorded = {}

    class FakeSSL:
        def __init__(self, host, port, **kwargs):
            recorded["ssl"] = (host, port)

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeSSL)
    inbox = ImapActivationInbox("imap.example.com", 993, "u", "p")
    connection = inbox._connect()
    assert isinstance(connection, FakeSSL)
    assert recorded["ssl"] == ("imap.example.com", 993)


def test_imap_inbox_starttls_path(monkeypatch):
    events = []

    class FakePlain:
        def __init__(self, host, port, **kwargs):
            events.append(("plain", host, port))

        def starttls(self):
            events.append("starttls")

    monkeypatch.setattr(imaplib, "IMAP4", FakePlain)
    inbox = ImapActivationInbox(
        "mail.example.com", 143, "u", "p", use_ssl=False, starttls=True
    )
    inbox._connect()
    assert events == [("plain", "mail.example.com", 143), "starttls"]


def test_imap_inbox_accepts_custom_mailbox_candidates():
    inbox = ImapActivationInbox(
        "imap.example.com", 993, "u", "p", mailbox_candidates=("Recepcion",)
    )
    assert inbox.mailbox_candidates == ("Recepcion",)


def test_imap_inbox_list_folders(monkeypatch):
    class FakeConn:
        def login(self, user, password):
            return ("OK", [b""])

        def list(self):
            return (
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b'(\\HasNoChildren) "/" "Recepcion"',
                    b'(\\HasNoChildren) "/" "[Gmail]/All Mail"',
                ],
            )

        def logout(self):
            return ("BYE", [b""])

    inbox = ImapActivationInbox("imap.example.com", 993, "u", "p")
    monkeypatch.setattr(inbox, "_connect", lambda: FakeConn())
    assert inbox.list_folders() == ["INBOX", "Recepcion", "[Gmail]/All Mail"]


def test_activation_inbox_contract_is_structural():
    class Stub:
        def wait_for_activation(
            self, address, *, not_before, timeout_seconds, poll_seconds, sleep
        ):
            return ActivationLink(email=address, token="t", raw_url="u")

    assert isinstance(Stub(), ActivationInbox)
    assert isinstance(ImapActivationInbox("h", 993, "u", "p"), ActivationInbox)


# ---------------------------------------------------------------------------
# Registration config: activation inbox + phone keys
# ---------------------------------------------------------------------------

def _write_env(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_registration_settings_reads_activation_keys(tmp_path, monkeypatch):
    for key in (
        "CBRS_ACTIVATION_DOMAIN",
        "CBRS_ACTIVATION_IMAP_USER",
        "CBRS_ACTIVATION_IMAP_PASSWORD",
        "CBRS_ACTIVATION_IMAP_HOST",
        "CBRS_ACTIVATION_IMAP_PORT",
        "CBRS_ACTIVATION_IMAP_MAILBOX",
        "CBRS_ACTIVATION_IMAP_SSL",
        "CBRS_ACTIVATION_IMAP_STARTTLS",
        "CBRS_REGISTRATION_RANDOMIZE_CELULAR",
    ):
        monkeypatch.delenv(key, raising=False)
    env = _write_env(
        tmp_path / ".env",
        "CBRS_ACTIVATION_DOMAIN=registro.example.cl\n"
        "CBRS_ACTIVATION_IMAP_USER=inbox@example.cl\n"
        "CBRS_ACTIVATION_IMAP_PASSWORD=app-pass\n"
        "DATAIMPULSE_PROXY_LOGIN=u\n"
        "DATAIMPULSE_PROXY_PASSWORD=p\n"
        "CBRS_ACTIVATION_IMAP_MAILBOX=Recepcion\n"
        "CBRS_ACTIVATION_IMAP_SSL=0\n"
        "CBRS_ACTIVATION_IMAP_STARTTLS=1\n"
        "CBRS_REGISTRATION_RANDOMIZE_CELULAR=1\n",
    )
    settings = load_registration_settings(env)
    assert settings.activation_domain == "registro.example.cl"
    assert settings.imap_user == "inbox@example.cl"
    assert settings.imap_mailbox == "Recepcion"
    assert settings.imap_ssl is False
    assert settings.imap_starttls is True
    assert settings.randomize_celular is True


def test_load_registration_settings_defaults(tmp_path, monkeypatch):
    for key in (
        "CBRS_ACTIVATION_IMAP_MAILBOX",
        "CBRS_ACTIVATION_IMAP_SSL",
        "CBRS_ACTIVATION_IMAP_STARTTLS",
        "CBRS_REGISTRATION_RANDOMIZE_CELULAR",
        "CBRS_REGISTRATION_POOL_STATE_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = load_registration_settings(_write_env(tmp_path / ".env", ""))
    assert settings.imap_ssl is True
    assert settings.imap_starttls is False
    assert settings.imap_mailbox is None
    assert settings.randomize_celular is False
    assert settings.celular == "986552408"
    # Must point at the worker's live route DB, not a stale pool path.
    assert settings.pool_state_path == Path(".cbrs") / "runtime" / "pool" / "pool.sqlite3"


def test_load_registration_settings_pool_state_override(tmp_path, monkeypatch):
    override = tmp_path / "state.sqlite3"
    monkeypatch.setenv("CBRS_REGISTRATION_POOL_STATE_DB", str(override))
    settings = load_registration_settings(_write_env(tmp_path / ".env", ""))
    assert settings.pool_state_path == override


# ---------------------------------------------------------------------------
# Activation email helpers
# ---------------------------------------------------------------------------

def test_build_activation_address_format_and_uniqueness():
    rng = random.Random(3)
    first = build_activation_address("Example.COM", "1.000.026-2", rng)
    second = build_activation_address("example.com", "1.000.026-2", rng)
    assert first.endswith("@example.com")
    assert first.startswith("cbrs-10000262-")
    assert first != second


def test_build_activation_address_rejects_bad_domain():
    with pytest.raises(ValueError):
        build_activation_address("user@domain.com", "1-9")


def test_parse_activation_link_plain_text():
    body = (
        "Active su cuenta en "
        "https://nuevo-portal.conservador.cl/activacion-de-cuenta"
        "?email=cbrs-10000262-k7x2@example.com&token=abc123%3D%3D ."
    )
    link = parse_activation_link(body)
    assert link is not None
    assert link.email == "cbrs-10000262-k7x2@example.com"
    assert link.token == "abc123=="


def test_parse_activation_link_html_escaped():
    body = (
        '<a href="https://nuevo-portal.conservador.cl/activacion-de-cuenta'
        '?email=a%40b.cl&amp;token=T0K3N">Activar</a>'
    )
    link = parse_activation_link(body)
    assert link is not None
    assert link.email == "a@b.cl"
    assert link.token == "T0K3N"


def test_parse_activation_link_absent():
    assert parse_activation_link("Bienvenido al portal, sin enlace.") is None


# ---------------------------------------------------------------------------
# RUT provider
# ---------------------------------------------------------------------------

def _make_rut_db(path: Path, rows: list[tuple[str, str]]) -> None:
    import uuid as _uuid

    with sqlite3.connect(path) as conn:
        # Mirrors the real rut_data.db schema (TEXT uuid primary key).
        conn.execute(
            """
            CREATE TABLE ruts (
                id TEXT PRIMARY KEY,
                rut TEXT NOT NULL, name TEXT, company TEXT, registration_date TEXT,
                status TEXT DEFAULT 'pending', raw_html TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        now = "2026-10-12T00:00:00+00:00"
        for rut, name in rows:
            conn.execute(
                "INSERT INTO ruts (id, rut, name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'scraped', ?, ?)",
                (str(_uuid.uuid4()), rut, name, now, now),
            )
        conn.commit()


def test_rut_provider_reserve_release_and_marks(tmp_path):
    db = tmp_path / "rut_data.db"
    _make_rut_db(
        db,
        [
            ("1.000.026-2", "Sanchez Sepulveda Elsa Nieves"),
            ("2.000.026-2", "COMERCIAL ANDINA SPA"),
            ("3.000.026-2", "Elgueda Rozas Luis Urbano"),
        ],
    )
    provider = RutProvider(db)

    first = provider.reserve_next()
    assert first.rut == "1.000.026-2"

    # The company row is skipped, not reserved.
    second = provider.reserve_next()
    assert second.rut == "3.000.026-2"

    with pytest.raises(NoRutsAvailable):
        provider.reserve_next()

    provider.release(second.rut)
    again = provider.reserve_next()
    assert again.rut == "3.000.026-2"

    provider.mark_registered(first.rut)
    provider.mark_failed(second.rut, "create")
    counts = provider.counts()
    assert counts["registered"] == 1
    assert counts["failed:create"] == 1
    assert counts["skipped_company"] == 1


# ---------------------------------------------------------------------------
# Account store
# ---------------------------------------------------------------------------

def test_account_store_lifecycle(tmp_path):
    store = AccountStore(tmp_path / "registered_accounts.db")
    store.insert_created(
        email="cbrs-1-x@example.com",
        password="Secret123",
        rut="1.000.026-2",
        nombres="Elsa Nieves",
        apellido_paterno="Sanchez",
        apellido_materno="Sepulveda",
        direccion="Av. Providencia 1234",
        celular="986552408",
        proxy_port=12345,
    )
    record = store.get("cbrs-1-x@example.com")
    assert record is not None
    assert record.status == "created"
    assert record.proxy_port == 12345

    store.mark_activated("cbrs-1-x@example.com")
    assert store.get("cbrs-1-x@example.com").status == "activated"

    store.mark_verified("cbrs-1-x@example.com", portal_user_id=42)
    record = store.get("cbrs-1-x@example.com")
    assert record.status == "verified"
    assert record.portal_user_id == "42"
    assert store.counts() == {"verified": 1}

    entries = store.export_pool_entries()
    assert entries == [
        {
            "id": "registro_1",
            "label": "Registro 1",
            "enabled": False,
            "username_env": "CBRS_REGISTRO_1_USERNAME",
            "password_env": "CBRS_REGISTRO_1_PASSWORD",
        }
    ]
    # Credentials never go into the pool JSON; they map to the referenced env
    # vars and are kept out of the worker config.
    assert store.export_env_assignments() == [
        "CBRS_REGISTRO_1_USERNAME=cbrs-1-x@example.com",
        "CBRS_REGISTRO_1_PASSWORD=Secret123",
    ]


def test_account_store_mark_failed(tmp_path):
    store = AccountStore(tmp_path / "registered_accounts.db")
    store.insert_created(
        email="a@b.cl",
        password="x",
        rut="1-1",
        nombres="N",
        apellido_paterno="P",
        apellido_materno=None,
        direccion="D",
        celular="986552408",
        proxy_port=None,
    )
    store.mark_failed("a@b.cl", "create", "duplicate")
    record = store.get("a@b.cl")
    assert record.status == "failed"
    assert record.failed_stage == "create"


def test_append_to_account_pool_dedupes(tmp_path):
    pool_path = tmp_path / "account-pool.json"
    pool_path.write_text(
        '{"accounts": [{"id": "ejecutivo_1", "enabled": true}]}', encoding="utf-8"
    )
    entries = [
        {"id": "registro_1", "enabled": False, "username_env": "A", "password_env": "B"},
        {"id": "registro_1", "enabled": False, "username_env": "A", "password_env": "B"},
    ]
    added = append_to_account_pool(pool_path, entries)
    assert added == 1
    import json

    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    assert pool["accounts"][0] == {"id": "ejecutivo_1", "enabled": True}
    assert len(pool["accounts"]) == 2


def test_exported_pool_entries_pass_the_worker_loader(tmp_path):
    from cbrs.account_pool import _parse_accounts

    store = AccountStore(tmp_path / "registered_accounts.db")
    store.insert_created(
        email="a@b.cl",
        password="Passw0rdX",
        rut="12.345.678-5",
        nombres="Ana",
        apellido_paterno="Munoz",
        apellido_materno="Silva",
        direccion="D",
        celular="986552408",
        proxy_port=None,
    )
    store.mark_verified("a@b.cl", portal_user_id=7)

    parsed = list(_parse_accounts(store.export_pool_entries(), profile_root=tmp_path))
    assert [account.account_id for account in parsed] == ["registro_1"]
    assert parsed[0].enabled is False
    assert parsed[0].username_env == "CBRS_REGISTRO_1_USERNAME"
    assert parsed[0].password_env == "CBRS_REGISTRO_1_PASSWORD"


# ---------------------------------------------------------------------------
# Sticky port selection
# ---------------------------------------------------------------------------

def test_human_delay_is_jittered_within_bounds():
    rng = random.Random(9)
    seen = {human_delay(rng) for _ in range(100)}
    assert seen  # non-empty
    assert all(1.5 <= value <= 4.0 for value in seen)
    assert len(seen) > 1  # actually jittered, not a constant


def test_choose_sticky_port_avoids_production_and_past_ports(tmp_path):
    pool_db = tmp_path / "pool.sqlite3"
    with sqlite3.connect(pool_db) as conn:
        conn.execute(
            "CREATE TABLE account_proxy_routes (account_id TEXT, active_port INTEGER)"
        )
        conn.execute(
            "INSERT INTO account_proxy_routes VALUES ('ejecutivo_2', 15000)"
        )
    store = AccountStore(tmp_path / "registered_accounts.db")
    store.insert_created(
        email="used@example.com",
        password="p",
        rut="1-1",
        nombres="N",
        apellido_paterno="P",
        apellido_materno=None,
        direccion="D",
        celular="986552408",
        proxy_port=15001,
    )
    settings = _test_settings(tmp_path, pool_state_path=pool_db)
    rng = random.Random(0)
    ports = {
        choose_sticky_port(settings, store, rng) for _ in range(5)
    }
    assert 15000 not in ports
    assert 15001 not in ports


# ---------------------------------------------------------------------------
# Orchestrator with fakes
# ---------------------------------------------------------------------------

class FakePortal:
    def __init__(
        self,
        *,
        create_error: PortalError | None = None,
        activate_error: PortalError | None = None,
        login_error: PortalError | None = None,
        user_id: int = 99,
    ) -> None:
        self.create_error = create_error
        self.activate_error = activate_error
        self.login_error = login_error
        self.user_id = user_id
        self.calls: list[str] = []
        self.closed = False

    def open(self):
        self.calls.append("open")
        return self

    def close(self):
        self.closed = True

    def check_egress_country(self, expected):
        self.calls.append("egress")
        return expected.upper()

    def bootstrap(self):
        self.calls.append("bootstrap")

    def recaptcha_token(self, action):
        self.calls.append(f"token:{action}")
        return f"token-{action}"

    def prepare_registration_form(self, payload):
        self.calls.append("fill")

    def create_account(self, payload):
        self.calls.append("create")
        self.last_payload = payload
        if self.create_error is not None:
            raise self.create_error
        return {"status": True}

    def activate(self, email, token):
        self.calls.append("activate")
        if self.activate_error is not None:
            raise self.activate_error
        return {}

    def resend_activation(self, email):
        self.calls.append("resend")

    def login(self, email, password):
        self.calls.append("login")
        if self.login_error is not None:
            raise self.login_error
        return "jwt-token"

    def fetch_me(self, jwt):
        self.calls.append("me")
        return {"idUsuario": self.user_id, "email": "x"}


class FakeInbox:
    def __init__(self, link: ActivationLink | None = None, timeout: bool = False):
        self.link = link
        self.timeout = timeout
        self.requested: list[str] = []

    def wait_for_activation(self, address, *, not_before, timeout_seconds, poll_seconds, sleep):
        self.requested.append(address)
        if self.timeout:
            raise ActivationEmailTimeout(f"no email for {address}")
        return self.link or ActivationLink(
            email=address, token="activation-token", raw_url="url"
        )


def _test_settings(
    tmp_path: Path,
    *,
    pool_state_path: Path | None = None,
    headless: bool = False,
) -> RegistrationSettings:
    return RegistrationSettings(
        base_url="https://nuevo-portal.conservador.cl",
        recaptcha_sitekey="test-sitekey",
        dataimpulse_login="user",
        dataimpulse_password="pass",
        dataimpulse_host="gw.dataimpulse.com",
        dataimpulse_country="cl",
        dataimpulse_ttl_minutes=30,
        dataimpulse_scheme="http",
        dataimpulse_port_min=10000,
        dataimpulse_port_max=20000,
        capsolver_api_key=None,
        capsolver_timeout_seconds=5.0,
        capsolver_poll_seconds=1.0,
        capsolver_min_score=0.9,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="inbox@example.com",
        imap_password="app-password",
        activation_domain="example.com",
        activation_timeout_seconds=5.0,
        activation_poll_seconds=1.0,
        celular="986552408",
        address_pool=("Av. Providencia 1234, Providencia, Santiago",),
        rut_db_path=tmp_path / "rut_data.db",
        accounts_db_path=tmp_path / "registered_accounts.db",
        pool_state_path=pool_state_path or (tmp_path / "missing-pool.sqlite3"),
        profile_root=tmp_path / "profiles",
        headless=headless,
        request_delay_seconds=0.0,
    )


def _orchestrator(tmp_path, portal: FakePortal, inbox: FakeInbox | None):
    settings = _test_settings(tmp_path)
    _make_rut_db(
        settings.rut_db_path,
        [
            ("1.000.026-2", "Sanchez Sepulveda Elsa Nieves"),
            ("1.000.131-5", "Elgueda Rozas Luis Urbano"),
        ],
    )
    return RegistrationOrchestrator(
        settings,
        inbox_factory=lambda _s: inbox,
        portal_factory=lambda _s, _proxy, _profile: portal,
        rng=random.Random(11),
        sleep=lambda _seconds: None,
        now=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc),
    )


def test_orchestrator_happy_path(tmp_path):
    portal = FakePortal()
    inbox = FakeInbox()
    orchestrator = _orchestrator(tmp_path, portal, inbox)

    results = orchestrator.register_accounts(count=1)

    assert len(results) == 1
    result = results[0]
    assert result.status == "verified"
    assert result.rut == "1.000.026-2"
    assert result.portal_user_id == "99"
    assert portal.calls == [
        "open", "egress", "bootstrap", "create",
        "activate", "login", "me",
    ]
    assert portal.closed

    record = orchestrator.store.get(result.email)
    assert record is not None
    assert record.status == "verified"
    assert record.rut == "1.000.026-2"
    assert record.nombres == "Elsa Nieves"
    assert record.apellido_paterno == "Sanchez"
    assert record.apellido_materno == "Sepulveda"
    assert password_meets_requirements(record.password)

    payload = portal.last_payload
    assert payload["rutDv"] == "1.000.026-2"
    assert payload["celular"] == "986552408"
    assert payload["tos"] is True
    assert payload["emailConfirmar"] == payload["email"]
    assert payload["passwordConfirmar"] == payload["password"]
    assert payload["recaptchaToken"] == ""

    counts = orchestrator.rut_provider.counts()
    assert counts.get("registered") == 1
    assert counts.get("scraped") == 1


def test_orchestrator_create_retryable_releases_rut(tmp_path):
    portal = FakePortal(
        create_error=PortalError(
            "create", "portal busy", retryable=True
        )
    )
    orchestrator = _orchestrator(tmp_path, portal, FakeInbox())

    results = orchestrator.register_accounts(count=1)

    assert results[0].status == "failed"
    assert results[0].stage_failed == "create"
    counts = orchestrator.rut_provider.counts()
    assert counts.get("scraped") == 2  # released, both rows available again
    record = orchestrator.store.get(results[0].email)
    assert record.status == "failed"


def test_orchestrator_create_terminal_consumes_rut(tmp_path):
    portal = FakePortal(
        create_error=PortalError("create", "RUT ya registrado", retryable=False)
    )
    orchestrator = _orchestrator(tmp_path, portal, FakeInbox())

    results = orchestrator.register_accounts(count=1)

    assert results[0].status == "failed"
    counts = orchestrator.rut_provider.counts()
    assert counts.get("failed:create") == 1
    assert counts.get("scraped") == 1


def test_orchestrator_email_timeout_stays_resumable(tmp_path):
    portal = FakePortal()
    inbox = FakeInbox(timeout=True)
    orchestrator = _orchestrator(tmp_path, portal, inbox)

    results = orchestrator.register_accounts(count=1)

    result = results[0]
    assert result.status == "created"
    assert result.stage_failed == "wait_email"
    assert "activate" not in portal.calls
    record = orchestrator.store.get(result.email)
    assert record.status == "created"
    # The RUT was accepted by the portal, so it stays consumed.
    assert orchestrator.rut_provider.counts().get("registered") == 1


def test_orchestrator_login_failure_keeps_activated(tmp_path):
    portal = FakePortal(login_error=PortalError("login", "rejected", http_status=401))
    orchestrator = _orchestrator(tmp_path, portal, FakeInbox())

    results = orchestrator.register_accounts(count=1)

    assert results[0].status == "activated"
    assert results[0].stage_failed == "login"
    assert orchestrator.store.get(results[0].email).status == "activated"


def test_orchestrator_resume_pending_completes(tmp_path):
    portal = FakePortal()
    inbox = FakeInbox(timeout=True)
    orchestrator = _orchestrator(tmp_path, portal, inbox)
    first = orchestrator.register_accounts(count=1)[0]
    assert first.status == "created"

    # Resume with a working inbox and portal.
    resume_portal = FakePortal()
    resume_inbox = FakeInbox()
    orchestrator._portal_factory = lambda _s, _p, _pr: resume_portal
    orchestrator._inbox_factory = lambda _s: resume_inbox
    results = orchestrator.resume_pending()

    assert len(results) == 1
    assert results[0].status == "verified"
    assert results[0].email == first.email
    assert "resend" in resume_portal.calls
    assert orchestrator.store.get(first.email).status == "verified"


def test_orchestrator_dry_run_releases_rut_and_skips_create(tmp_path):
    portal = FakePortal()
    orchestrator = _orchestrator(tmp_path, portal, FakeInbox())

    results = orchestrator.register_accounts(count=1, dry_run=True)

    assert results[0].status == "dry_run"
    assert "create" not in portal.calls
    assert portal.calls.index("fill") < portal.calls.index("token:signup")
    assert "token:signup" in portal.calls
    assert orchestrator.rut_provider.counts().get("scraped") == 2
    assert orchestrator.store.counts() == {}


def test_orchestrator_batch_count(tmp_path):
    portal = FakePortal()
    orchestrator = _orchestrator(tmp_path, portal, FakeInbox())

    results = orchestrator.register_accounts(count=2)

    assert [item.status for item in results] == ["verified", "verified"]
    assert results[0].rut != results[1].rut
    assert orchestrator.store.counts() == {"verified": 2}


def test_orchestrator_stops_cleanly_when_pool_runs_dry(tmp_path):
    portal = FakePortal()
    orchestrator = _orchestrator(tmp_path, portal, FakeInbox())

    # Two RUTs are available; the third reservation must not abort the run.
    results = orchestrator.register_accounts(count=3)

    assert [item.status for item in results] == ["verified", "verified"]
    assert portal.closed
    assert orchestrator.store.counts() == {"verified": 2}


def test_orchestrator_randomize_celular_makes_up_number(tmp_path):
    portal = FakePortal()
    orchestrator = _orchestrator(tmp_path, portal, FakeInbox())
    orchestrator.settings = replace(orchestrator.settings, randomize_celular=True)

    results = orchestrator.register_accounts(count=1)

    assert results[0].status == "verified"
    celular = portal.last_payload["celular"]
    assert celular != "986552408"
    assert len(celular) == 9 and celular.isdigit() and celular.startswith("9")
    assert orchestrator.store.get(results[0].email).celular == celular


# ---------------------------------------------------------------------------
# RUT refill helpers
# ---------------------------------------------------------------------------

def test_validate_rut_known_values():
    assert validate_rut("1.000.026-2")
    assert validate_rut("1000375-K")
    assert validate_rut("12.345.678-5")
    assert not validate_rut("12.345.678-0")
    assert not validate_rut("999-1")
    assert not validate_rut("")


def test_normalize_rut():
    assert normalize_rut("1000026-2") == "1.000.026-2"
    assert normalize_rut("12345678-K") == "12.345.678-K"
    assert normalize_rut("1.000.026-2") == "1.000.026-2"
    assert normalize_rut("999-1") is None


def test_parse_html_tables_and_extract_pairs():
    html = """
    <table>
      <tr><th>Nombre</th><th>RUT</th><th>Ciudad</th></tr>
      <tr><td>Sanchez Sepulveda Elsa Nieves</td><td>1.000.026-2</td><td>Santiago</td></tr>
      <tr><td>Elgueda Rozas Luis Urbano</td><td>1.000.131-5</td><td>Valparaiso</td></tr>
      <tr><td>sin rut aqui</td><td>----</td></tr>
    </table>
    """
    rows = parse_html_tables(html)
    pairs = extract_pairs(rows)
    assert pairs == [
        ("Sanchez Sepulveda Elsa Nieves", "1.000.026-2"),
        ("Elgueda Rozas Luis Urbano", "1.000.131-5"),
    ]


def test_insert_new_ruts_validates_and_dedupes(tmp_path):
    db = tmp_path / "rut_data.db"
    _make_rut_db(db, [("1.000.026-2", "Sanchez Sepulveda Elsa Nieves")])

    inserted, duplicates, invalid = insert_new_ruts(
        db,
        [
            ("Elgueda Rozas Luis Urbano", "1.000.131-5"),  # new + valid
            ("Sanchez Sepulveda Elsa Nieves", "1.000.026-2"),  # duplicate
            ("Mala Persona", "12.345.678-0"),  # invalid checksum
        ],
    )
    assert (inserted, duplicates, invalid) == (1, 1, 1)

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT rut, status FROM ruts ORDER BY rowid"
        ).fetchall()
    assert rows == [
        ("1.000.026-2", "scraped"),
        ("1.000.131-5", "scraped"),
    ]


def test_insert_new_ruts_stores_canonical_rut(tmp_path):
    db = tmp_path / "rut_data.db"
    _make_rut_db(db, [("1.000.375-K", "Jaramillo Toro Elisa Victoria")])

    inserted, duplicates, invalid = insert_new_ruts(
        db,
        [
            ("Jaramillo Toro Elisa Victoria", "1000375-k"),  # same RUT, other spelling
            ("Gonzalez Perez Maria Jose", "12345678-5"),  # unformatted but valid
            ("Rojas Diaz Juan Carlos", "11.111.111-1"),
        ],
    )
    assert (inserted, duplicates, invalid) == (2, 1, 0)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT rut FROM ruts ORDER BY rowid").fetchall()
    assert [row[0] for row in rows] == [
        "1.000.375-K",
        "12.345.678-5",
        "11.111.111-1",
    ]


def test_default_terms_sequence():
    assert default_terms(12) == [
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"
    ]


class ScreenshotPage:
    def __init__(self, *, fail=False):
        self.shots = []
        self.selector = None
        self.fail = fail

    def locator(self, selector):
        self.selector = selector
        return "masked-locator"

    def screenshot(self, **kwargs):
        self.shots.append(kwargs)
        if self.fail:
            raise RuntimeError("secret diagnostic error must not be logged")

    def evaluate(self, _script, _args):
        return {
            "status": 400,
            "headers": {},
            "bodyText": '{"code":"captcha-rechazado","token":"secret-jwt"}',
        }


def test_registration_screenshots_are_opt_in_and_mask_credentials(tmp_path):
    settings = _test_settings(tmp_path)
    profile = tmp_path / "profile"
    portal = RegistrationPortal(settings, proxy_url="http://unused", profile_dir=profile)
    page = ScreenshotPage()
    portal._page = page
    portal._capture_screenshot("page_loaded")
    assert not page.shots
    assert not profile.exists()

    portal.settings = replace(settings, capture_screenshots=True)
    portal._capture_screenshot("page_loaded")
    assert page.shots[0]["mask"] == ["masked-locator"]
    assert 'input[type="password"]' in page.selector
    assert 'input[name*="token" i]' in page.selector
    assert Path(page.shots[0]["path"]).parent == profile / "screenshots"


def test_screenshot_failure_does_not_prevent_browser_cleanup(tmp_path, caplog):
    class Context:
        closed = False

        def close(self):
            self.closed = True

    settings = replace(_test_settings(tmp_path), capture_screenshots=True)
    portal = RegistrationPortal(settings, proxy_url="http://unused", profile_dir=tmp_path)
    context = Context()
    portal._context = context
    portal._page = ScreenshotPage(fail=True)
    portal.close()
    assert context.closed
    assert portal._page is None
    assert "RuntimeError" in caplog.text
    assert "secret diagnostic error" not in caplog.text


def test_rejected_create_captures_response_without_logging_tokens(tmp_path, caplog):
    import logging

    settings = replace(_test_settings(tmp_path), capture_screenshots=True)
    portal = RegistrationPortal(settings, proxy_url="http://unused", profile_dir=tmp_path)
    portal._page = ScreenshotPage()
    caplog.set_level(logging.INFO, logger="cbrs.registration.portal")
    with pytest.raises(PortalError, match="captcha_rejected"):
        response = portal.fetch_json("/api/v1/account/create", body={
            "recaptchaToken": "secret-captcha", "password": "secret-password"})
        portal._raise_for_stop("create", response)
    paths = [Path(shot["path"]).name for shot in portal._page.shots]
    assert paths == ["001-create_response_400.png"]
    assert "HTTP 400 code=captcha-rechazado" in caplog.text
    assert "secret-jwt" not in caplog.text
    assert "secret-captcha" not in caplog.text
    assert "secret-password" not in caplog.text


def _fallback_portal(tmp_path, monkeypatch, responses):
    settings = replace(
        _test_settings(tmp_path), external_captcha=True,
        capsolver_api_key="test-cap-key", two_captcha_api_key="test-two-key",
    )
    portal = RegistrationPortal(settings, proxy_url="http://temporary-proxy:12345", profile_dir=tmp_path)
    requests, providers = [], []

    def fetch(_path, *, headers, body):
        requests.append((dict(headers), dict(body)))
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def token(_action, provider):
        providers.append(provider)
        portal._captcha_source = provider
        return provider + "-token"

    monkeypatch.setattr(portal, "fetch_json", fetch)
    monkeypatch.setattr(portal, "_external_token", token)

    def prepare(payload):
        portal._registration_payload = dict(payload)

    def submit(token_override):
        wire_token = token_override or "browser-token"
        return fetch("/api/v1/account/create", headers={"recaptcha-token": wire_token},
                     body={**portal._registration_payload, "recaptchaToken": wire_token})

    monkeypatch.setattr(portal, "prepare_registration_form", prepare)
    monkeypatch.setattr(portal, "_submit_registration_form", submit)
    return portal, requests, providers


def test_registration_create_uses_bounded_solver_chain_on_explicit_rejection(tmp_path, monkeypatch):
    rejected = PortalResponse(400, body_text='{"code":"captcha-rechazado"}')
    portal, requests, providers = _fallback_portal(tmp_path, monkeypatch, [
        rejected, rejected, PortalResponse(200, body_text='{"status":true}'),
    ])
    original = {"rutDv": "1.000.026-2", "email": "a@example.cl",
                "password": "private", "recaptchaToken": "browser-token"}
    assert portal.create_account(original) == {"status": True}
    assert providers == ["capsolver", "2captcha"]
    assert len(requests) == 3
    assert [headers["recaptcha-token"] for headers, _ in requests] == [
        "browser-token", "capsolver-token", "2captcha-token",
    ]
    for headers, body in requests:
        assert body["rutDv"] == original["rutDv"]
        assert body["email"] == original["email"]
        assert body["password"] == original["password"]
        assert body["recaptchaToken"] == headers["recaptcha-token"]
    assert original["recaptchaToken"] == "browser-token"


@pytest.mark.parametrize("response", [
    PortalResponse(200, body_text='{"status":true}'),
    PortalResponse(400, body_text='{"code":"intente-mas-tarde"}'),
    PortalResponse(403, body_text='{"code":"captcha-rechazado"}'),
    PortalResponse(429, body_text='{"code":"captcha-rechazado"}'),
    PortalResponse(500, body_text='{"code":"captcha-rechazado"}'),
    PortalResponse(200, body_text='<html>captcha verification failed</html>'),
    TimeoutError("ambiguous submission"),
])
def test_registration_does_not_retry_success_or_ambiguous_submission(tmp_path, monkeypatch, response):
    portal, requests, providers = _fallback_portal(tmp_path, monkeypatch, [response])
    try:
        portal.create_account({"recaptchaToken": "browser-token"})
    except (PortalError, TimeoutError):
        pass
    assert len(requests) == 1
    assert providers == []


def test_registration_stops_after_both_external_tokens_rejected(tmp_path, monkeypatch):
    rejected = PortalResponse(400, body_text='{"code":"captcha-rechazado"}')
    portal, requests, providers = _fallback_portal(tmp_path, monkeypatch, [rejected] * 3)
    with pytest.raises(PortalError, match="captcha_rejected"):
        portal.create_account({"recaptchaToken": "browser-token"})
    assert providers == ["capsolver", "2captcha"]
    assert len(requests) == 3


def test_registration_external_clients_preserve_proxy_contracts_and_audit(tmp_path, monkeypatch):
    from cbrs.capsolver import CapSolverResult
    from cbrs.captcha_solver import TwoCaptchaResult

    tasks = []

    class FakeCapSolver:
        def __init__(self, _key, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise_result(self, **task):
            tasks.append(("capsolver", task))
            return CapSolverResult("private-cap-token")

    class FakeTwoCaptcha:
        def __init__(self, _key, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise_result(self, **task):
            tasks.append(("2captcha", task))
            return TwoCaptchaResult("private-two-token", cost_usd=0.003)

    monkeypatch.setattr("cbrs.registration.portal.CapSolverClient", FakeCapSolver)
    monkeypatch.setattr("cbrs.registration.portal.TwoCaptchaClient", FakeTwoCaptcha)
    settings = replace(_test_settings(tmp_path), external_captcha=True,
                       capsolver_api_key="private-cap-key", two_captcha_api_key="private-two-key")
    portal = RegistrationPortal(settings, proxy_url="http://temporary:12345", profile_dir=tmp_path)

    class Page:
        def evaluate(self, _script):
            return "registration-browser-UA"

    portal._page = Page()
    assert portal._next_external_token("signup", after="browser") == "private-cap-token"
    assert portal._next_external_token("signup", after="capsolver") == "private-two-token"
    assert tasks[0][1]["proxy"] == "http://temporary:12345"
    assert tasks[0][1]["user_agent"] == "registration-browser-UA"
    assert "proxy" not in tasks[1][1]
    assert "user_agent" not in tasks[1][1]
    assert all(task["page_action"] == "signup" for _, task in tasks)
    assert all(task["website_url"].endswith("/crear-cuenta") for _, task in tasks)
    with sqlite3.connect(portal._captcha_budget().path) as conn:
        rows = conn.execute("SELECT provider, status, cost_usd FROM captcha_attempts").fetchall()
        dump = " ".join(conn.iterdump())
    assert rows == [("capsolver", "succeeded", 0.003), ("2captcha", "succeeded", 0.003)]
    assert "private" not in dump
    assert "temporary:12345" not in dump


def test_registration_budget_limit_stops_before_paid_client(tmp_path, monkeypatch):
    from cbrs.capsolver import CapSolverError

    calls = []

    class FailingSolver:
        def __init__(self, _key, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise_result(self, **_task):
            calls.append("capsolver")
            raise CapSolverError("ERROR_ZERO_BALANCE")

    monkeypatch.setattr("cbrs.registration.portal.CapSolverClient", FailingSolver)
    settings = replace(_test_settings(tmp_path), external_captcha=True, max_paid_solves=1,
                       capsolver_api_key="test-key", two_captcha_api_key="test-key")
    portal = RegistrationPortal(settings, proxy_url="http://temporary:12345", profile_dir=tmp_path)

    class Page:
        def evaluate(self, _script):
            return "browser-UA"

    portal._page = Page()
    with pytest.raises(PortalError, match="task limit reached"):
        portal._next_external_token("signup", after="browser")
    assert calls == ["capsolver"]
    assert portal._paid_solves == 1


@pytest.mark.parametrize("capsolver_fails", [False, True])
def test_registration_actual_solver_chain_records_portal_outcomes(tmp_path, monkeypatch, capsolver_fails):
    from cbrs.capsolver import CapSolverResult, CapSolverError
    from cbrs.captcha_solver import TwoCaptchaResult

    calls = []

    class Cap:
        def __init__(self, _key, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise_result(self, **_task):
            calls.append("capsolver")
            if capsolver_fails:
                raise CapSolverError("ERROR_CAPTCHA_UNSOLVABLE")
            return CapSolverResult("cap-token")

    class Two:
        def __init__(self, _key, **_kwargs):
            pass

        def solve_recaptcha_v3_enterprise_result(self, **_task):
            calls.append("2captcha")
            return TwoCaptchaResult("two-token")

    monkeypatch.setattr("cbrs.registration.portal.CapSolverClient", Cap)
    monkeypatch.setattr("cbrs.registration.portal.TwoCaptchaClient", Two)
    rejected = PortalResponse(400, body_text='{"code":"captcha-rechazado"}')
    responses = [rejected] * (1 if capsolver_fails else 2)
    responses.append(PortalResponse(200, body_text='{"status":true}'))
    portal, requests, _ = _fallback_portal(tmp_path, monkeypatch, responses)
    monkeypatch.setattr(portal, "_external_token", RegistrationPortal._external_token.__get__(portal))

    class Page:
        def evaluate(self, _script):
            return "UA"

    portal._page = Page()
    assert portal.create_account({"recaptchaToken": "browser-token"}) == {"status": True}
    assert calls == ["capsolver", "2captcha"]
    assert len(requests) == (2 if capsolver_fails else 3)
    with sqlite3.connect(portal._captcha_budget().path) as conn:
        rows = conn.execute("SELECT provider,status,portal_status FROM captcha_attempts").fetchall()
    expected_cap = ("capsolver", "failed", "not_submitted") if capsolver_fails else (
        "capsolver", "succeeded", "rejected")
    assert rows[0] == expected_cap
    assert rows[1] == ("2captcha", "succeeded", "accepted")


def test_registration_settings_load_secondary_key_without_exposing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CBRS_2CAPTCHA_API_KEY", "private-two-key")
    monkeypatch.setenv("CBRS_CAPSOLVER_API_KEY", "private-cap-key")
    settings = load_registration_settings(_write_env(tmp_path / ".env", ""))
    assert settings.two_captcha_api_key == "private-two-key"
    assert settings.external_captcha is False
    assert "private-two-key" not in repr(settings)
    assert "private-cap-key" not in repr(settings)


@pytest.mark.skipif(os.environ.get("CBRS_RUN_BROWSER_TESTS") != "1", reason="opt-in local Chrome fixture")
@pytest.mark.parametrize("mode", ["native", "external", "mismatch", "no_request"])
def test_registration_visible_form_in_local_chrome(tmp_path, monkeypatch, mode):
    """Real Playwright against localhost only; never creates a portal account."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from playwright.sync_api import sync_playwright
    from cbrs.browser_runtime import detect_browser, validate_native_chrome_executable

    received = []
    ids = ("email", "emailConfirmar", "password", "passwordConfirmar", "rut", "nombres",
           "apellidoPaterno", "apellidoMaterno", "direccion", "celular")
    controls = "".join(f'<input id="{name}" type="{"password" if "password" in name else "text"}">' for name in ids)
    html = controls + '''<input type="checkbox" id="tos"><button>Crear cuenta</button>
      <script>document.querySelector('button').onclick = async () => {
        const body = Object.fromEntries(Array.from(document.querySelectorAll('input'))
          .map(el => [el.id, el.type === 'checkbox' ? el.checked : el.value]));
        body.rutDv = body.rut;
        body.celular = body.celular.slice(0,4)+' '+body.celular.slice(4);
        body.recaptchaToken = 'native-test-token';
        await fetch('/api/v1/account/create', {method:'POST',
          headers: {'Content-Type':'application/json','recaptcha-token':body.recaptchaToken},
          body:JSON.stringify(body)});
      };</script>'''
    if mode == "no_request":
        html += '''<script>document.querySelector('button').onclick=()=>{
          const p=document.createElement('p');
          p.textContent='Ha ocurrido un problema. Por favor, intente nuevamente.';
          document.body.appendChild(p);
        };</script>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((body, self.headers.get("recaptcha-token")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":true}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    executable = detect_browser()
    validate_native_chrome_executable(executable.path)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable.path), headless=True)
            try:
                page = browser.new_page()
                settings = replace(_test_settings(tmp_path), base_url=f"http://127.0.0.1:{server.server_port}")
                portal = RegistrationPortal(settings, proxy_url="http://unused", profile_dir=tmp_path)
                portal._page = page
                page.goto(settings.base_url + "/crear-cuenta")
                expect_response = page.expect_response
                monkeypatch.setattr(page, "expect_response", lambda predicate, **_kwargs: expect_response(predicate, timeout=3000))
                identity = build_identity(PersonIdentity("1.000.026-2", "Elsa Nieves", "Sanchez", "Sepulveda"),
                                          email_address="fixture@example.test", address_pool=settings.address_pool,
                                          celular="986552408", rng=random.Random(4))
                payload = identity.create_payload("")
                portal.prepare_registration_form(payload)
                assert not received  # Dry-run preparation must never submit.
                assert page.locator("#celular").input_value() == "86552408"
                assert page.locator("#tos").is_checked()
                if mode == "no_request":
                    with pytest.raises(PortalError) as error:
                        portal._submit_registration_form(None)
                    assert error.value.route_retry_allowed
                    assert not error.value.submission_unknown
                    assert not received
                elif mode == "mismatch":
                    page.locator("#email").fill("wrong@example.test")
                    with pytest.raises(PortalError, match="request blocked"):
                        portal._submit_registration_form(None)
                    assert not received
                else:
                    token = "external-test-token" if mode == "external" else None
                    if mode == "native":
                        assert portal.create_account(payload) == {"status": True}
                        assert portal._registration_succeeded
                    else:
                        response = portal._submit_registration_form(token)
                        assert response.status == 200
                    assert len(received) == 1
                    body, header = received[0]
                    assert body["email"] == identity.email
                    assert body["password"] == identity.password
                    assert body["celular"] == "8655 2408"
                    assert body["tos"] is True
                    assert body["recaptchaToken"] == header == (token or "native-test-token")
                    if mode == "native":
                        # The next identity starts a new visible form after the
                        # SPA replaced the accepted form with its success panel.
                        monkeypatch.setattr(portal, "_ensure_recaptcha_ready", lambda: None)
                        portal.prepare_registration_form(payload)
                        assert not portal._registration_succeeded
                        assert len(received) == 1  # No replay of accepted create.
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


def _recovery_orchestrator(tmp_path, portals, *, hourly=30, daily=10):
    orchestrator = _orchestrator(tmp_path, portals[0], FakeInbox())
    production = tmp_path / "production.sqlite3"
    with sqlite3.connect(production) as db:
        db.execute("CREATE TABLE account_proxy_routes(active_port INTEGER,pending_port INTEGER)")
        db.execute("INSERT INTO account_proxy_routes VALUES (10002,10403)")
        db.execute("CREATE TABLE account_checks(egress_hash TEXT)")
        db.execute("INSERT INTO account_checks VALUES ('production-exit')")
    orchestrator.settings = replace(orchestrator.settings, pool_state_path=production,
                                    route_candidates=len(portals), route_retry_seconds=0,
                                    route_hourly_limit=hourly, route_daily_submit_limit=daily)
    candidates = iter(portals)
    calls = []

    def factory(settings, proxy, profile):
        from urllib.parse import urlparse
        portal = next(candidates)
        calls.append((urlparse(proxy).port, profile, settings.max_paid_solves))
        return portal

    orchestrator._portal_factory = factory
    return orchestrator, calls


def _candidate(*, egress="new-exit", error=None):
    portal = FakePortal(create_error=error)
    portal.egress_hash = egress
    return portal


def _route_rejection():
    return PortalError("create", "Known temporary signup rejection", retryable=True,
                       http_status=400, response_code="error", route_retry_allowed=True)


def test_registration_recovery_keeps_identity_and_stops_at_first_success(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    first = _candidate(egress="exit-a", error=_route_rejection())
    first._paid_solves = 3
    second = _candidate(egress="exit-b")
    unused = _candidate(egress="exit-c")
    orchestrator, calls = _recovery_orchestrator(tmp_path, [first, second, unused])
    production_before = orchestrator.settings.pool_state_path.read_bytes()
    result = register_with_route_recovery(orchestrator)
    assert result[0].status == "verified"
    assert len(calls) == 2
    assert calls[0][0] != calls[1][0]
    assert not {port for port, _, _ in calls} & {10002,10403}
    assert calls[1][2] == 1  # Paid allowance is shared across candidates.
    assert first.last_payload == second.last_payload
    assert first.closed and second.closed
    assert not unused.calls
    assert orchestrator.store.counts() == {"verified": 1}
    assert orchestrator.rut_provider.counts() == {"registered": 1, "scraped": 1}
    assert orchestrator.settings.pool_state_path.read_bytes() == production_before


def test_registration_recovery_skips_production_and_repeated_exits(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    production = _candidate(egress="production-exit")
    first = _candidate(egress="same-exit", error=_route_rejection())
    duplicate = _candidate(egress="same-exit")
    good = _candidate(egress="good-exit")
    orchestrator, calls = _recovery_orchestrator(tmp_path, [production,first,duplicate,good])
    assert register_with_route_recovery(orchestrator)[0].status == "verified"
    assert "open" not in production.calls
    assert "open" not in duplicate.calls
    assert len(calls) == 4


def test_registration_recovery_unknown_create_holds_identity_and_never_rotates(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    first = _candidate(error=PortalError("create", "response unknown", submission_unknown=True))
    second = _candidate(egress="unused")
    orchestrator, calls = _recovery_orchestrator(tmp_path, [first,second])
    result = register_with_route_recovery(orchestrator)[0]
    assert result.status == "created"
    assert len(calls) == 1
    assert orchestrator.store.counts() == {"created": 1}
    assert orchestrator.rut_provider.counts() == {"reserved": 1, "scraped": 1}


def test_registration_recovery_accepted_create_email_timeout_never_rotates(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    orchestrator, calls = _recovery_orchestrator(tmp_path, [_candidate(), _candidate(egress="unused")])
    orchestrator._inbox_factory = lambda _settings: FakeInbox(timeout=True)
    assert register_with_route_recovery(orchestrator)[0].status == "created"
    assert len(calls) == 1
    assert orchestrator.rut_provider.counts() == {"registered": 1, "scraped": 1}


@pytest.mark.parametrize("hourly,daily", [(1,10),(30,1)])
def test_registration_recovery_respects_persistent_allowances(tmp_path, hourly, daily):
    from cbrs.registration.recovery import register_with_route_recovery

    first = _candidate(error=_route_rejection())
    orchestrator, calls = _recovery_orchestrator(tmp_path, [first,_candidate(egress="unused")], hourly=hourly,daily=daily)
    result = register_with_route_recovery(orchestrator)[0]
    assert result.status == "failed"
    assert "allowance exhausted" in result.reason
    assert len(calls) == 1
    assert orchestrator.rut_provider.counts() == {"scraped": 2}
    assert orchestrator.store.counts() == {"failed": 1}


def test_registration_recovery_dry_run_does_not_submit(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    first = _candidate()
    orchestrator, calls = _recovery_orchestrator(tmp_path, [first,_candidate(egress="unused")])
    assert register_with_route_recovery(orchestrator,dry_run=True)[0].status == "dry_run"
    assert len(calls) == 1
    assert "fill" in first.calls and "create" not in first.calls
    assert orchestrator.store.counts() == {}
    assert orchestrator.rut_provider.counts() == {"scraped": 2}


def test_registration_recovery_fails_closed_without_production_exclusions(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery
    from cbrs.registration.routes import RouteRecoveryStop

    orchestrator = _orchestrator(tmp_path, _candidate(), FakeInbox())
    with pytest.raises(RouteRecoveryStop,match="Production route exclusions unavailable"):
        register_with_route_recovery(orchestrator)
    assert orchestrator.rut_provider.counts() == {"scraped": 2}


def test_registration_recovery_continues_same_credentials_across_passes(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    first = _candidate(egress="first-exit",error=_route_rejection())
    second = _candidate(egress="second-exit")
    orchestrator, calls = _recovery_orchestrator(tmp_path,[first,second])
    orchestrator.settings = replace(orchestrator.settings,route_candidates=1)
    assert register_with_route_recovery(orchestrator)[0].status == "failed"
    assert register_with_route_recovery(orchestrator)[0].status == "verified"
    assert first.last_payload == second.last_payload
    assert len(calls) == 2
    assert orchestrator.store.counts() == {"verified":1}


def test_registration_recovery_stops_on_proxy_auth_failure(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    first = _candidate()

    def rejected(_country):
        raise PortalError("egress","Proxy authentication rejected",http_status=407)

    first.check_egress_country = rejected
    orchestrator, calls = _recovery_orchestrator(tmp_path,[first,_candidate(egress="unused")])
    assert register_with_route_recovery(orchestrator)[0].status == "failed"
    assert len(calls) == 1
    assert "open" not in first.calls
    assert orchestrator.rut_provider.counts() == {"scraped":2}


def test_registration_recovery_captcha_rejection_is_candidate_not_identity_failure(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery
    from cbrs.safety import StopReason

    first = _candidate(egress="captcha-exit",error=PortalError(
        "create","CAPTCHA rejected",stop_reason=StopReason.CAPTCHA_REJECTED,
        http_status=400,response_code="captcha-rechazado"))
    second = _candidate(egress="working-exit")
    orchestrator, calls = _recovery_orchestrator(tmp_path,[first,second])
    assert register_with_route_recovery(orchestrator)[0].status == "verified"
    assert len(calls) == 2
    assert first.last_payload == second.last_payload
    assert orchestrator.rut_provider.counts() == {"registered":1,"scraped":1}


def test_registration_recovery_solver_stop_preserves_credentials_for_next_pass(tmp_path):
    from cbrs.registration.recovery import register_with_route_recovery

    first = _candidate(egress="solver-stopped-exit",error=PortalError(
        "captcha","Solver cooldown",retryable=True))
    second = _candidate(egress="working-exit")
    orchestrator, calls = _recovery_orchestrator(tmp_path,[first,second])
    assert register_with_route_recovery(orchestrator)[0].stage_failed == "captcha"
    assert register_with_route_recovery(orchestrator)[0].status == "verified"
    assert first.last_payload == second.last_payload
    assert len(calls) == 2
    assert orchestrator.store.registration_retry_for_rut("1.000.026-2") is None


@pytest.mark.parametrize("status,message,expected", [
    (400,"No se pudo registrar el usuario, intente más tarde.",True),
    (429,"No se pudo registrar el usuario, intente más tarde.",False),
    (403,"No se pudo registrar el usuario, intente más tarde.",False),
    (500,"No se pudo registrar el usuario, intente más tarde.",False),
    (400,"Some unrelated backend error",False),
])
def test_registration_only_known_rejection_allows_new_route(tmp_path,status,message,expected):
    import json

    portal = RegistrationPortal(_test_settings(tmp_path),proxy_url="http://unused",profile_dir=tmp_path)
    response = PortalResponse(status,body_text=json.dumps({"code":"error","msg":message}))
    with pytest.raises(PortalError) as error:
        portal._raise_for_stop("create",response)
    assert error.value.route_retry_allowed is expected
    assert error.value.submission_unknown is (status >= 500)
