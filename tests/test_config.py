from pathlib import Path

from cbrs.config import MIN_SAFE_DELAY_SECONDS, load_settings


def test_fixed_delay_is_clamped_to_safe_minimum(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_REQUEST_DELAY_SECONDS": "1"}, root=tmp_path)

    assert settings.request_delay_seconds == MIN_SAFE_DELAY_SECONDS


def test_legacy_delay_range_uses_slowest_value(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_REQUEST_DELAY_MIN_SECONDS": "3.5",
            "CBRS_REQUEST_DELAY_MAX_SECONDS": "7",
        },
        root=tmp_path,
    )

    assert settings.request_delay_seconds == 7.0
    assert settings.delay_seconds() == 7.0


def test_relative_paths_resolve_under_repo_root(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".local/profile",
            "CBRS_CLOAK_CACHE_DIR": ".local/cache",
            "CBRS_OUTPUT_DIR": "downloads",
        },
        root=tmp_path,
    )

    assert settings.profile_dir == tmp_path / ".local" / "profile"
    assert settings.cloak_cache_dir == tmp_path / ".local" / "cache"
    assert settings.output_dir == tmp_path / "downloads"


def test_settings_parse_production_defaults(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    assert settings.browser_backend == "chrome"
    assert settings.browser_executable_path is None
    assert settings.headless is False
    assert settings.egress_mode == ""
    assert settings.allow_personal_egress is False
    assert settings.expected_egress_country == "CL"
    assert settings.profile_dir == tmp_path / ".cbrs" / "chrome-profile"
    assert settings.cloak_cache_dir == tmp_path / ".cbrs" / "cloak-cache"
    assert settings.output_dir == tmp_path / "outputs"
    assert settings.allow_cloak_auto_update is False


def test_legacy_cloak_profile_default_is_only_for_cloak_backend(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_BROWSER_BACKEND": "cloak"}, root=tmp_path)

    assert settings.browser_backend == "cloak"
    assert settings.profile_dir == tmp_path / ".cbrs" / "cloak-profile"


def test_browser_executable_path_is_loaded(tmp_path: Path) -> None:
    browser = tmp_path / "chrome.exe"
    settings = load_settings({"CBRS_BROWSER_EXECUTABLE_PATH": str(browser)}, root=tmp_path)

    assert settings.browser_executable_path == browser


def test_headless_setting_can_be_enabled(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_HEADLESS": "1"}, root=tmp_path)

    assert settings.headless is True


def test_window_mode_defaults_to_normal(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    assert settings.window_mode == "normal"


def test_window_mode_is_loaded(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_WINDOW_MODE": "OffScreen"}, root=tmp_path)

    assert settings.window_mode == "offscreen"


def test_egress_mode_is_loaded(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_EGRESS_MODE": "Client_VPN"}, root=tmp_path)

    assert settings.egress_mode == "client_vpn"


def test_personal_egress_ack_is_loaded(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_ALLOW_PERSONAL_EGRESS": "1"}, root=tmp_path)

    assert settings.allow_personal_egress is True


def test_cloak_seed_override_is_loaded(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_CLOAK_FINGERPRINT_SEED": "12345"}, root=tmp_path)

    assert settings.cloak_fingerprint_seed == "12345"


def test_cloak_proxy_url_is_loaded(tmp_path: Path) -> None:
    settings = load_settings(
        {"CBRS_CLOAK_PROXY_URL": "socks5://user:pass@example.test:1234"},
        root=tmp_path,
    )

    assert settings.cloak_proxy_url == "socks5://user:pass@example.test:1234"


def test_no_multi_account_rotation_config_exists() -> None:
    import cbrs.config as config

    assert not hasattr(config, "ACCOUNTS")
