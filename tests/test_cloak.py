import os
import sys
from pathlib import Path
from types import SimpleNamespace

from cbrs.browser_session import BrowserSession
from cbrs.cloak import (
    apply_cloak_environment,
    cloak_proxy,
    cloak_proxy_metadata,
    cloak_seed_file,
    cloak_seed_hash,
    get_cloak_fingerprint_seed,
)
from cbrs.config import load_settings


def test_local_cloak_seed_is_created_once_and_reused(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_BROWSER_BACKEND": "cloak"}, root=tmp_path)

    first = get_cloak_fingerprint_seed(settings)
    second = get_cloak_fingerprint_seed(settings)

    assert first == second
    assert first.isdigit()
    assert cloak_seed_file(settings).read_text(encoding="utf-8") == first
    assert cloak_seed_hash(first) != first


def test_cloak_environment_disables_auto_update(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings({"CBRS_BROWSER_BACKEND": "cloak"}, root=tmp_path)
    monkeypatch.setenv("CLOAKBROWSER_AUTO_UPDATE", "true")

    apply_cloak_environment(settings)

    assert os.environ["CLOAKBROWSER_AUTO_UPDATE"] == "false"
    assert os.environ["CLOAKBROWSER_CACHE_DIR"] == str(settings.cloak_cache_dir)


def test_browser_session_launches_cloak_persistent_context(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(
        {
            "CBRS_BROWSER_BACKEND": "cloak",
            "CBRS_CLOAK_FINGERPRINT_SEED": "12345",
            "CBRS_CLOAK_PROXY_URL": "socks5://user:pass@example.test:1234",
        },
        root=tmp_path,
    )
    captured = {}

    class FakeContext:
        pages = []

        def close(self):
            captured["closed"] = True

    def fake_launch_persistent_context(user_data_dir, **kwargs):
        captured["user_data_dir"] = user_data_dir
        captured["kwargs"] = kwargs
        return FakeContext()

    monkeypatch.setitem(
        sys.modules,
        "cloakbrowser",
        SimpleNamespace(launch_persistent_context=fake_launch_persistent_context),
    )

    session = BrowserSession(settings, headless=False)
    session.open()
    session.close()

    assert captured["user_data_dir"] == str(settings.profile_dir)
    assert captured["kwargs"]["proxy"] == "socks5://user:pass@example.test:1234"
    assert captured["kwargs"]["args"] == ["--fingerprint=12345"]
    assert captured["kwargs"]["humanize"] is True
    assert captured["kwargs"]["human_preset"] == "careful"
    assert captured["kwargs"]["headless"] is False
    assert os.environ["CLOAKBROWSER_AUTO_UPDATE"] == "false"
    assert captured["closed"] is True


def test_cloak_proxy_metadata_is_sanitized(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_BROWSER_BACKEND": "cloak",
            "CBRS_CLOAK_PROXY_URL": "socks5://user:pass@example.test:1234",
        },
        root=tmp_path,
    )

    metadata = cloak_proxy_metadata(settings)

    assert cloak_proxy(settings) == "socks5://user:pass@example.test:1234"
    assert metadata["cloak_proxy_configured"] is True
    assert metadata["cloak_proxy_scheme"] == "socks5"
    assert metadata["cloak_proxy_port"] == 1234
    assert metadata["cloak_proxy_host_hash"]
    assert "example.test" not in str(metadata)
    assert "user" not in str(metadata)
    assert "pass" not in str(metadata)


def test_cloak_proxy_rejects_missing_scheme(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_BROWSER_BACKEND": "cloak",
            "CBRS_CLOAK_PROXY_URL": "user:pass@example.test:1234",
        },
        root=tmp_path,
    )

    try:
        cloak_proxy(settings)
    except ValueError as exc:
        assert "CBRS_CLOAK_PROXY_URL" in str(exc)
    else:
        raise AssertionError("Expected invalid proxy URL to fail")
