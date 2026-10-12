"""Offline tests for the cbrs.registration pipeline (no network, no browser)."""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cbrs.registration.config import RegistrationSettings
from cbrs.registration.email_inbox import (
    ActivationEmailTimeout,
    ActivationLink,
    build_activation_address,
    parse_activation_link,
)
from cbrs.registration.identity import (
    build_identity,
    choose_address,
    generate_password,
    password_meets_requirements,
)
from cbrs.registration.orchestrator import (
    RegistrationOrchestrator,
    choose_sticky_port,
)
from cbrs.registration.portal import PortalError
from cbrs.registration.rut_provider import (
    NoRutsAvailable,
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
            "email": "cbrs-1-x@example.com",
            "password": "Secret123",
            "provider": "cbrs-registration",
            "rut": "1.000.026-2",
        }
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
        {"email": "new@example.com", "password": "p", "provider": "cbrs-registration", "rut": "1-1"},
        {"email": "new@example.com", "password": "p", "provider": "cbrs-registration", "rut": "1-1"},
    ]
    added = append_to_account_pool(pool_path, entries)
    assert added == 1
    import json

    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    assert pool["accounts"][0] == {"id": "ejecutivo_1", "enabled": True}
    assert len(pool["accounts"]) == 2


# ---------------------------------------------------------------------------
# Sticky port selection
# ---------------------------------------------------------------------------

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
        "open", "egress", "bootstrap", "token:signup", "create",
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
    assert payload["recaptchaToken"] == "token-signup"

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


def test_default_terms_sequence():
    assert default_terms(12) == [
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"
    ]
