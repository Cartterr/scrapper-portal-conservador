from types import SimpleNamespace

import pytest

from cbrs.cli import _runtime_headless, build_parser, cmd_pool, main, missing_fna_fields


def test_fna_requires_numero_and_ano() -> None:
    parser = build_parser()
    args = parser.parse_args(["search", "--foja", "123"])

    assert missing_fna_fields(args) == ["numero", "ano"]


def test_complete_fna_has_no_missing_fields() -> None:
    parser = build_parser()
    args = parser.parse_args(["download", "--foja", "123", "--numero", "456", "--ano", "2024"])

    assert missing_fna_fields(args) == []


def test_headed_overrides_default_headless() -> None:
    parser = build_parser()
    args = parser.parse_args(["--headed", "search", "--query", "BANCO DE CHILE"])

    assert _runtime_headless(args) is False


def test_no_headless_legacy_alias_enables_headed() -> None:
    parser = build_parser()
    args = parser.parse_args(["--no-headless", "search", "--query", "BANCO DE CHILE"])

    assert args.headed is True
    assert _runtime_headless(args) is False


def test_headless_flag_enables_headless() -> None:
    parser = build_parser()
    args = parser.parse_args(["--headless", "search", "--query", "BANCO DE CHILE"])

    assert _runtime_headless(args) is True


def test_headless_and_headed_are_mutually_exclusive(capsys) -> None:
    try:
        main(["--headless", "--headed", "doctor"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected conflicting headless flags to fail")

    assert "--headless and --headed cannot be used together" in capsys.readouterr().err


def test_use_proxy_legacy_flag_fails_with_fixed_trust_message(capsys) -> None:
    try:
        main(["--use-proxy", "doctor"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected legacy proxy flag to fail")

    assert "--use-proxy is not supported" in capsys.readouterr().err


def test_soak_run_parser_supports_dashboard_dry_run() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["soak", "run", "--dashboard", "--dry-run", "--max-cycles", "3"]
    )

    assert args.command == "soak"
    assert args.soak_command == "run"
    assert args.dashboard is True
    assert args.dry_run is True
    assert args.max_cycles == 3


def test_soak_dashboard_parser_is_independent_from_run() -> None:
    parser = build_parser()
    args = parser.parse_args(["soak", "dashboard", "--port", "9876"])

    assert args.command == "soak"
    assert args.soak_command == "dashboard"
    assert args.port == 9876


def test_soak_stop_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["soak", "stop"])

    assert args.command == "soak"
    assert args.soak_command == "stop"


def test_jobs_proxy_rotate_requires_explicit_account_and_acknowledgement() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "jobs",
            "proxy-rotate",
            "--account",
            "ejecutivo_2",
            "--acknowledge-authorized-live-traffic",
        ]
    )

    assert args.jobs_command == "proxy-rotate"
    assert args.account == "ejecutivo_2"
    assert args.acknowledge_authorized_live_traffic is True


def test_pool_init_parser_targets_one_account() -> None:
    parser = build_parser()
    args = parser.parse_args(["pool", "init", "--account", "ejecutivo_1", "--timeout", "600"])

    assert args.command == "pool"
    assert args.pool_command == "init"
    assert args.account == "ejecutivo_1"
    assert args.timeout == 600


def test_pool_run_parser_supports_dashboard_dry_run() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["pool", "run", "--dashboard", "--dry-run", "--max-cycles", "6"]
    )

    assert args.command == "pool"
    assert args.pool_command == "run"
    assert args.dashboard is True
    assert args.dry_run is True
    assert args.max_cycles == 6


def test_pool_dashboard_and_stop_parsers() -> None:
    parser = build_parser()
    dashboard_args = parser.parse_args(["pool", "dashboard", "--port", "9877"])
    stop_args = parser.parse_args(["pool", "stop"])

    assert dashboard_args.command == "pool"
    assert dashboard_args.pool_command == "dashboard"
    assert dashboard_args.port == 9877
    assert stop_args.command == "pool"
    assert stop_args.pool_command == "stop"


def test_captcha_test_parser_supports_native_browser_control() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "captcha-test",
            "--provider",
            "browser",
            "--account",
            "ejecutivo_2",
            "--foja",
            "12597",
            "--numero",
            "6347",
            "--ano",
            "1992",
        ]
    )

    assert args.command == "captcha-test"
    assert args.provider == "browser"
    assert args.account == "ejecutivo_2"


def test_jobs_recover_parser() -> None:
    args = build_parser().parse_args(["jobs", "recover"])

    assert args.command == "jobs"
    assert args.jobs_command == "recover"


def test_pool_proxy_health_parser_supports_guarded_baseline_replacement() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "pool",
            "proxy-health",
            "--account",
            "ejecutivo_1",
            "--replace-egress-baseline",
        ]
    )

    assert args.account == "ejecutivo_1"
    assert args.replace_egress_baseline is True
    assert args.approve_egress_baseline is False


def test_pool_proxy_health_records_successful_live_gate(monkeypatch) -> None:
    accounts = [
        SimpleNamespace(account_id="ejecutivo_1", label="Ejecutivo 1"),
        SimpleNamespace(account_id="ejecutivo_2", label="Ejecutivo 2"),
    ]
    pool_config = SimpleNamespace(accounts=accounts)

    class FakeJobStore:
        def __init__(self) -> None:
            self.checks = {}

        def egress_owner(self, egress_hash, *, exclude_account):
            return next(
                (
                    account_id
                    for account_id, check in self.checks.items()
                    if account_id != exclude_account
                    and check["proxy_status"] == "passed"
                    and check["egress_hash"] == egress_hash
                ),
                None,
            )

        def set_account_check(self, account_id, *, proxy_status, egress_hash=None):
            self.checks[account_id] = {
                "proxy_status": proxy_status,
                "egress_hash": egress_hash,
            }

    job_store = FakeJobStore()
    monkeypatch.setattr(
        "cbrs.account_pool.load_account_pool_config", lambda *args, **kwargs: pool_config
    )
    monkeypatch.setattr("cbrs.account_pool.default_pool_store", lambda *args: object())
    monkeypatch.setattr(
        "cbrs.account_pool.account_settings", lambda settings, account: account.account_id
    )
    monkeypatch.setattr("cbrs.jobs.default_job_store", lambda *args: job_store)
    monkeypatch.setattr(
        "cbrs.cli.run_preflight",
        lambda settings, **kwargs: SimpleNamespace(
            ok=True,
            report={
                "egress_hash": f"hash-{settings}",
                "checks": [],
            },
            report_path=None,
        ),
    )
    monkeypatch.setattr(
        "cbrs.proxy_health.run_proxy_health",
        lambda settings, **kwargs: SimpleNamespace(
            ok=True,
            report={"checks": []},
            report_path=None,
        ),
    )

    result = cmd_pool(
        SimpleNamespace(
            pool_command="proxy-health",
            config=None,
            account=None,
            approve_egress_baseline=False,
        )
    )

    assert result == 0
    assert job_store.checks == {
        "ejecutivo_1": {
            "proxy_status": "passed",
            "egress_hash": "hash-ejecutivo_1",
        },
        "ejecutivo_2": {
            "proxy_status": "passed",
            "egress_hash": "hash-ejecutivo_2",
        },
    }


def test_pool_proxy_health_replaces_baseline_only_after_safe_gates(monkeypatch) -> None:
    account = SimpleNamespace(
        account_id="ejecutivo_1",
        label="Ejecutivo 1",
        proxy_provider="generic_static",
    )
    pool_config = SimpleNamespace(accounts=[account])

    class FakeJobStore:
        def __init__(self) -> None:
            self.checks = {}

        def summary(self):
            return {"worker": None}

        def egress_owner(self, _egress_hash, *, exclude_account):
            assert exclude_account == "ejecutivo_1"
            return None

        def set_account_check(self, account_id, *, proxy_status, egress_hash=None):
            self.checks[account_id] = (proxy_status, egress_hash)

    job_store = FakeJobStore()
    replaced = []
    monkeypatch.setattr(
        "cbrs.account_pool.load_account_pool_config", lambda *args, **kwargs: pool_config
    )
    monkeypatch.setattr("cbrs.account_pool.default_pool_store", lambda *args: object())
    monkeypatch.setattr(
        "cbrs.account_pool.account_settings", lambda settings, selected: selected.account_id
    )
    monkeypatch.setattr("cbrs.jobs.default_job_store", lambda *args: job_store)
    monkeypatch.setattr("cbrs.endurance.load_endurance_plan", lambda *_args: object())
    monkeypatch.setattr(
        "cbrs.endurance.EnduranceController",
        lambda *_args: SimpleNamespace(status=lambda: {"paused": True}),
    )
    monkeypatch.setattr(
        "cbrs.cli.run_preflight",
        lambda settings, **kwargs: SimpleNamespace(
            ok=True,
            report={
                "egress_hash": "new-hash",
                "egress_country": "CL",
                "checks": [{"name": "egress baseline", "ok": True, "detail": "replacement_pending"}],
            },
            report_path=None,
        ),
    )
    monkeypatch.setattr(
        "cbrs.proxy_health.run_proxy_health",
        lambda settings, **kwargs: SimpleNamespace(ok=True, report={"checks": []}, report_path=None),
    )
    monkeypatch.setattr(
        "cbrs.cli.replace_egress_baseline",
        lambda settings, **kwargs: replaced.append((settings, kwargs)) or "archive.json",
    )

    result = cmd_pool(
        SimpleNamespace(
            pool_command="proxy-health",
            config=None,
            account="ejecutivo_1",
            approve_egress_baseline=False,
            replace_egress_baseline=True,
        )
    )

    assert result == 0
    assert replaced == [
        ("ejecutivo_1", {"egress_hash": "new-hash", "egress_country": "CL"})
    ]
    assert job_store.checks["ejecutivo_1"] == ("passed", "new-hash")


@pytest.mark.parametrize(
    ("worker", "paused", "expected"),
    [({"owner": "worker"}, True, "no worker lease"), (None, False, "paused endurance")],
)
def test_pool_proxy_health_replacement_refuses_active_runtime(
    monkeypatch, capsys, worker, paused, expected
) -> None:
    account = SimpleNamespace(
        account_id="ejecutivo_1",
        label="Ejecutivo 1",
        proxy_provider="generic_static",
    )
    pool_config = SimpleNamespace(accounts=[account])
    job_store = SimpleNamespace(summary=lambda: {"worker": worker})
    monkeypatch.setattr(
        "cbrs.account_pool.load_account_pool_config", lambda *args, **kwargs: pool_config
    )
    monkeypatch.setattr("cbrs.account_pool.default_pool_store", lambda *args: object())
    monkeypatch.setattr("cbrs.jobs.default_job_store", lambda *args: job_store)
    monkeypatch.setattr("cbrs.endurance.load_endurance_plan", lambda *_args: object())
    monkeypatch.setattr(
        "cbrs.endurance.EnduranceController",
        lambda *_args: SimpleNamespace(status=lambda: {"paused": paused}),
    )
    monkeypatch.setattr(
        "cbrs.cli.run_preflight",
        lambda *_args, **_kwargs: pytest.fail("preflight must not run"),
    )

    result = cmd_pool(
        SimpleNamespace(
            pool_command="proxy-health",
            config=None,
            account="ejecutivo_1",
            approve_egress_baseline=False,
            replace_egress_baseline=True,
        )
    )

    assert result == 1
    assert expected in capsys.readouterr().err


def test_pool_proxy_health_replacement_refuses_duplicate_egress(monkeypatch) -> None:
    account = SimpleNamespace(
        account_id="ejecutivo_1",
        label="Ejecutivo 1",
        proxy_provider="generic_static",
    )
    pool_config = SimpleNamespace(accounts=[account])

    class FakeJobStore:
        def __init__(self) -> None:
            self.checks = []

        def summary(self):
            return {"worker": None}

        def egress_owner(self, _egress_hash, *, exclude_account):
            assert exclude_account == "ejecutivo_1"
            return "ejecutivo_2"

        def set_account_check(self, account_id, *, proxy_status, egress_hash=None):
            self.checks.append((account_id, proxy_status, egress_hash))

    job_store = FakeJobStore()
    monkeypatch.setattr(
        "cbrs.account_pool.load_account_pool_config", lambda *args, **kwargs: pool_config
    )
    monkeypatch.setattr("cbrs.account_pool.default_pool_store", lambda *args: object())
    monkeypatch.setattr(
        "cbrs.account_pool.account_settings", lambda _settings, selected: selected.account_id
    )
    monkeypatch.setattr("cbrs.jobs.default_job_store", lambda *args: job_store)
    monkeypatch.setattr("cbrs.endurance.load_endurance_plan", lambda *_args: object())
    monkeypatch.setattr(
        "cbrs.endurance.EnduranceController",
        lambda *_args: SimpleNamespace(status=lambda: {"paused": True}),
    )
    monkeypatch.setattr(
        "cbrs.cli.run_preflight",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            report={"egress_hash": "duplicate", "egress_country": "CL", "checks": []},
            report_path=None,
        ),
    )
    monkeypatch.setattr(
        "cbrs.proxy_health.run_proxy_health",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True, report={"checks": []}, report_path=None
        ),
    )
    monkeypatch.setattr(
        "cbrs.cli.replace_egress_baseline",
        lambda *_args, **_kwargs: pytest.fail("duplicate egress must not be installed"),
    )

    result = cmd_pool(
        SimpleNamespace(
            pool_command="proxy-health",
            config=None,
            account="ejecutivo_1",
            approve_egress_baseline=False,
            replace_egress_baseline=True,
        )
    )

    assert result == 1
    assert job_store.checks == [("ejecutivo_1", "failed", None)]


def test_jobs_cli_parses_text_fna_worker_and_dashboard() -> None:
    parser = build_parser()

    text = parser.parse_args(
        ["jobs", "enqueue", "--text", "Company", "--idempotency-key", "req-1"]
    )
    fna = parser.parse_args(
        ["jobs", "enqueue", "--foja", "10", "--numero", "20", "--year", "2020"]
    )
    worker = parser.parse_args(["jobs", "worker", "--once", "--poll-seconds", "1"])
    dashboard = parser.parse_args(["jobs", "dashboard", "--port", "9000"])

    assert text.jobs_command == "enqueue"
    assert text.text == "Company"
    assert text.idempotency_key == "req-1"
    assert (fna.foja, fna.numero, fna.ano) == (10, 20, 2020)
    assert worker.once is True
    assert worker.poll_seconds == 1
    assert dashboard.port == 9000


def test_readiness_cli_parses_offline_wsl_gate() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "readiness",
            "--target",
            "wsl",
            "--distro",
            "Ubuntu-24.04",
            "--probe-wsl-runtime",
            "--json-report",
            ".cbrs/readiness/test.json",
        ]
    )

    assert args.command == "readiness"
    assert args.target == "wsl"
    assert args.distro == "Ubuntu-24.04"
    assert args.probe_wsl_runtime is True


def test_readiness_cli_can_require_active_native_runtime() -> None:
    parser = build_parser()

    args = parser.parse_args(["readiness", "--target", "windows", "--require-active-runtime"])

    assert args.target == "windows"
    assert args.require_active_runtime is True
