from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from cbrs.account_pool import AccountPoolStore, load_account_pool_config
from cbrs.account_pool_dashboard import start_pool_dashboard
from cbrs.browser_preview import (
    browser_preview_path,
    capture_browser_preview,
    read_browser_preview,
)
from cbrs.config import load_settings
from cbrs.jobs import JobStore, WORKER_LEASE_NAME
from cbrs.jobs import _PersistentAccountBrowsers, _ManagedAccountScraper
from cbrs.browser_session import CommerceAuthState


def test_preview_refreshes_auth_evidence_without_recovery(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "pool.sqlite3")
    settings = load_settings({}, root=tmp_path)
    pool = _PersistentAccountBrowsers(
        scraper_factory=None, headless=False, store=store, worker_id="preview-test"
    )
    page = _ScreenshotPage()
    state = [CommerceAuthState.AUTHENTICATED_FORM]
    browser = SimpleNamespace(page=page, detect_commerce_auth_state=lambda: state[0])
    entry = _ManagedAccountScraper(manager=None, scraper=browser, settings=settings,
                                   reauth_required=True)
    pool._entries["a1"] = entry
    pool._known_accounts["a1"] = (settings, "", "")
    pool.can_reauthenticate = lambda _: pytest.fail("preview must not run recovery")
    pool.on_auth_success = lambda _: pytest.fail("preview must not clear cooldowns")
    monkeypatch.setattr("cbrs.jobs.time.monotonic", lambda: 100.0)
    pool.capture_previews()
    assert store.account_check("a1")["browser_auth_state"] == "authenticated_form"
    assert entry.authenticated_once
    assert not entry.reauth_required
    state[0] = CommerceAuthState.LOGIN_GATE
    pool.capture_previews()  # interval remains bounded
    assert len(page.calls) == 1
    pool.capture_previews(force=True)
    assert store.account_check("a1")["browser_auth_state"] == "login_gate"
    assert entry.authenticated_once  # lifecycle protection survives demotion
    state[0] = CommerceAuthState.UNKNOWN
    pool.capture_previews(force=True)
    assert store.account_check("a1")["browser_auth_state"] == "unknown"
    assert entry.authenticated_once


class _ScreenshotPage:
    def __init__(self, payload: bytes = b"\xff\xd8preview\xff\xd9") -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def screenshot(self, **kwargs: object) -> bytes:
        self.calls.append(kwargs)
        return self.payload


def test_browser_preview_is_atomic_low_bandwidth_and_traversal_safe(tmp_path: Path) -> None:
    store_path = tmp_path / "pool" / "pool.sqlite3"
    page = _ScreenshotPage()

    preview = capture_browser_preview(page, store_path, "../account@example.test")

    assert preview.path == browser_preview_path(store_path, "../account@example.test")
    assert preview.path.parent == store_path.parent / "browser-previews"
    assert preview.path.read_bytes() == page.payload
    assert page.calls == [
        {
            "type": "jpeg",
            "quality": 48,
            "full_page": False,
            "animations": "disabled",
            "caret": "hide",
            "timeout": 8_000,
        }
    ]
    assert list(preview.path.parent.glob("*.tmp.jpg")) == []


def test_stale_browser_preview_is_not_available(tmp_path: Path) -> None:
    store_path = tmp_path / "pool.sqlite3"
    path = browser_preview_path(store_path, "account-1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xd8old\xff\xd9")
    old = time.time() - 120
    os.utime(path, (old, old))

    with pytest.raises(FileNotFoundError, match="stale"):
        read_browser_preview(store_path, "account-1", max_age_seconds=20)


@pytest.mark.parametrize("lease_name", [WORKER_LEASE_NAME, "browser_owner"])
def test_dashboard_serves_only_fresh_frames_from_the_active_browser_owner(
    tmp_path: Path, lease_name,
) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
            "CBRS_BROWSER_PREVIEW_INTERVAL_SECONDS": "2",
            "CBRS_BROWSER_PREVIEW_MAX_AGE_SECONDS": "10",
        },
        root=tmp_path,
    )
    config = load_account_pool_config(settings)
    path = tmp_path / ".cbrs" / "pool" / "pool.sqlite3"
    pool_store = AccountPoolStore(path)
    pool_store.create_run(run_id="live", dry_run=False, config=config, dashboard_url=None)
    job_store = JobStore(path)
    account_id = config.accounts[0].account_id
    owner = "preview-worker"
    assert job_store.acquire_lease(lease_name, owner)
    job_store.set_account_browser_state(
        account_id,
        live=True,
        authenticated=False,
        headless=True,
        owner=owner,
        status="authentication_unknown",
    )
    frame = _ScreenshotPage()
    capture_browser_preview(frame, path, account_id)

    dashboard = start_pool_dashboard(
        pool_store,
        settings=settings,
        config=config,
        job_store=job_store,
        port=0,
    )
    try:
        with urlopen(f"{dashboard.url}/api/status", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        account = next(item for item in payload["accounts"] if item["account_id"] == account_id)
        with urlopen(f"{dashboard.url}{account['browser_preview_url']}", timeout=5) as response:
            content = response.read()
            content_type = response.headers["Content-Type"]
            cache_control = response.headers["Cache-Control"]
            captured_at = response.headers["X-CBRS-Captured-At"]

        assert account["browser_preview_available"] is True
        if lease_name == "browser_owner":
            assert not account["worker_active"]
            assert account["browser_owner_active"]
        assert account["browser_preview_age_seconds"] <= 10
        assert content == frame.payload
        assert content_type == "image/jpeg"
        assert cache_control == "private, no-store"
        assert captured_at == account["browser_preview_captured_at"]

        job_store.set_account_browser_state(
            account_id,
            live=False,
            authenticated=False,
            headless=True,
            owner=owner,
            status="worker_stopped",
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(f"{dashboard.url}/api/browser-preview/{account_id}", timeout=5)
        assert rejected.value.code == 404
    finally:
        dashboard.stop()
