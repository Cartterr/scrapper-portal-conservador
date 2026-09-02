from pathlib import Path

import pytest

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
            "CBRS_LOG_DIR": "runtime-logs",
        },
        root=tmp_path,
    )

    assert settings.profile_dir == tmp_path / ".local" / "profile"
    assert settings.cloak_cache_dir == tmp_path / ".local" / "cache"
    assert settings.output_dir == tmp_path / "downloads"
    assert settings.log_dir == tmp_path / "runtime-logs"


def test_two_captcha_fallback_requires_api_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CBRS_2CAPTCHA_API_KEY"):
        load_settings(
            {"CBRS_CAPTCHA_SOLVER_MODE": "2captcha_fallback"},
            root=tmp_path,
        )


def test_two_captcha_manual_mode_loads_with_explicit_key(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_CAPTCHA_SOLVER_MODE": "2captcha_manual",
            "CBRS_2CAPTCHA_API_KEY": "private-key",
        },
        root=tmp_path,
    )
    assert settings.captcha_solver_mode == "2captcha_manual"
    assert "private-key" not in repr(settings)


def test_two_captcha_manual_mode_can_be_preconfigured_without_a_key(tmp_path: Path) -> None:
    settings = load_settings(
        {"CBRS_CAPTCHA_SOLVER_MODE": "2captcha_manual"}, root=tmp_path
    )
    assert settings.captcha_solver_mode == "2captcha_manual"
    assert settings.two_captcha_api_key is None


def test_two_captcha_fallback_settings_are_loaded_without_exposing_key(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_CAPTCHA_SOLVER_MODE": "2captcha_fallback",
            "CBRS_2CAPTCHA_API_KEY": "private-key",
            "CBRS_2CAPTCHA_MIN_SCORE": "0.9",
            "CBRS_PROXY_RECHECK_SECONDS": "600",
        },
        root=tmp_path,
    )

    assert settings.captcha_solver_mode == "2captcha_fallback"
    assert settings.two_captcha_api_key == "private-key"
    assert settings.two_captcha_min_score == 0.9
    assert settings.proxy_recheck_seconds == 600
    assert "private-key" not in repr(settings)


def test_capsolver_fallback_requires_api_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CBRS_CAPSOLVER_API_KEY"):
        load_settings(
            {"CBRS_CAPTCHA_SOLVER_MODE": "capsolver_fallback"},
            root=tmp_path,
        )


def test_capsolver_manual_settings_are_secret_safe(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_CAPTCHA_SOLVER_MODE": "capsolver_manual",
            "CBRS_CAPSOLVER_API_KEY": "CAP-private-key",
            "CBRS_CAPSOLVER_TIMEOUT_SECONDS": "90",
            "CBRS_CAPSOLVER_POLL_SECONDS": "3",
        },
        root=tmp_path,
    )

    assert settings.external_captcha_provider == "capsolver"
    assert settings.capsolver_api_key == "CAP-private-key"
    assert settings.capsolver_timeout_seconds == 90
    assert settings.capsolver_poll_seconds == 3
    assert "CAP-private-key" not in repr(settings)


def test_settings_parse_production_defaults(tmp_path: Path) -> None:
    settings = load_settings({}, root=tmp_path)

    assert settings.browser_backend == "chrome"
    assert settings.browser_executable_path is None
    assert settings.headless is True
    assert settings.egress_mode == ""
    assert settings.allow_personal_egress is False
    assert settings.expected_egress_country == "CL"
    assert settings.profile_dir == tmp_path / ".cbrs" / "chrome-profile"
    assert settings.cloak_cache_dir == tmp_path / ".cbrs" / "cloak-cache"
    assert settings.output_dir == tmp_path / "outputs"
    assert settings.log_dir == tmp_path / ".cbrs" / "logs"
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


def test_dataimpulse_runtime_settings_are_loaded_and_secret_repr_is_safe(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        {
            "CBRS_EGRESS_MODE": "residential_sticky",
            "DATAIMPULSE_PROXY_LOGIN": "private-login",
            "DATAIMPULSE_PROXY_PASSWORD": "private-password",
            "DATAIMPULSE_STICKY_TTL_MINUTES": "120",
            "CBRS_BROWSER_HEALTHCHECK_SECONDS": "30",
            "CBRS_BROWSER_REAUTH_BACKOFF_SECONDS": "60",
        },
        root=tmp_path,
    )

    assert settings.egress_mode == "residential_sticky"
    assert settings.dataimpulse_sticky_ttl_minutes == 120
    assert settings.browser_healthcheck_seconds == 30
    assert "private-login" not in repr(settings)
    assert "private-password" not in repr(settings)


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


def test_browser_proxy_url_is_loaded(tmp_path: Path) -> None:
    settings = load_settings(
        {"CBRS_PROXY_URL": "http://user:pass@example.test:33335"},
        root=tmp_path,
    )

    assert settings.proxy_url == "http://user:pass@example.test:33335"


def test_no_multi_account_rotation_config_exists() -> None:
    import cbrs.config as config

    assert not hasattr(config, "ACCOUNTS")
