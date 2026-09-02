from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import config
from .browser_runtime import get_browser_status
from .preflight import (
    preflight_validation_metadata,
    replace_egress_baseline,
    run_preflight,
)
from .safety import SafetyStopException, StopReason, redact_text
from .validation import (
    run_controlled_validation,
)

if TYPE_CHECKING:
    from .scraper import CBRSScraper


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def format_result(i: int, result: dict) -> str:
    parts = [f"  [{i}] {result.get('nombreSociedad', 'N/A')}"]
    parts.append(
        f"      Foja: {result.get('foja')} | Numero: {result.get('num')} | Ano: {result.get('ano')}"
    )
    if result.get("acto"):
        parts.append(f"      Acto: {result['acto']}")
    if result.get("personas"):
        parts.append(f"      {result['personas']}")
    return "\n".join(parts)


def display_results(results: list[dict]) -> None:
    if not results:
        print("No results found.")
        return
    print(f"\nFound {len(results)} result(s):\n")
    for index, result in enumerate(results, 1):
        print(format_result(index, result))
    print()


def prompt_selection(results: list[dict], *, show_results: bool = True) -> list[int]:
    if show_results:
        display_results(results)
    if not results:
        return []
    if len(results) == 1:
        print("Only one result, selecting it automatically.")
        return [0]

    while True:
        choice = input("Select results to download (e.g. 1,3 or 'all'): ").strip()
        if choice.lower() == "all":
            return list(range(len(results)))
        try:
            indices = []
            for part in choice.split(","):
                selected = int(part.strip())
                if 1 <= selected <= len(results):
                    indices.append(selected - 1)
                else:
                    print(f"  Invalid number: {selected}")
            if indices:
                return indices
        except ValueError:
            pass
        print("  Please enter comma-separated numbers or 'all'.")


def missing_fna_fields(args: argparse.Namespace) -> list[str]:
    if getattr(args, "foja", None) is None:
        return []
    return [
        field
        for field in ("numero", "ano")
        if getattr(args, field, None) is None
    ]


def validate_fna_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    missing = missing_fna_fields(args)
    if missing:
        parser.error("--foja requires --numero and --ano")


def cmd_doctor() -> int:
    settings = config.SETTINGS
    browser_status = get_browser_status(settings)
    checks = [
        ("profile dir", True, str(settings.profile_dir)),
        ("default output dir", True, str(settings.output_dir)),
        (
            "request delay",
            settings.request_delay_seconds >= config.MIN_SAFE_DELAY_SECONDS,
            f"{settings.request_delay_seconds:.1f}s fixed",
        ),
        (
            "browser backend",
            settings.browser_backend == "chrome",
            settings.browser_backend,
        ),
        (
            "browser executable",
            browser_status.available,
            _browser_status_detail(browser_status),
        ),
        (
            "automated browser mode",
            True,
            "headless" if settings.headless else f"headed/{settings.window_mode}",
        ),
        (
            "browser display",
            _browser_display_allowed(settings),
            _browser_display_detail(settings),
        ),
        (
            "expected egress country",
            settings.expected_egress_country == "CL",
            settings.expected_egress_country,
        ),
        (
            "egress mode",
            _egress_mode_allowed(settings),
            _egress_mode_detail(settings),
        ),
        (
            "browser proxy route",
            _proxy_route_allowed(settings),
            _proxy_route_detail(settings),
        ),
        (
            "image transport",
            True,
            "curl_cffi compatibility" if settings.use_curl_cffi_for_images else "browser-origin",
        ),
        (
            "captcha solver",
            settings.captcha_solver_mode == "browser"
            or bool(
                settings.capsolver_api_key
                if settings.external_captcha_provider == "capsolver"
                else settings.two_captcha_api_key
            ),
            settings.captcha_solver_mode,
        ),
    ]

    gitignore = Path(".gitignore")
    required_ignores = [
        ".cbrs/",
        ".env",
        ".env.local",
        "outputs/",
        "*.cookie",
        "*.session.json",
        "*.storage_state.json",
    ]
    ignored = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing_ignores = [item for item in required_ignores if item not in ignored]
    checks.append(
        (
            ".gitignore safety",
            not missing_ignores,
            "ok" if not missing_ignores else f"missing: {', '.join(missing_ignores)}",
        )
    )

    failed = False
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        print(f"{status:4} {name}: {detail}")
        failed = failed or not ok
    return 1 if failed else 0


def _browser_status_detail(status) -> str:
    if status.error:
        return status.error
    if not status.available:
        return "missing"
    return f"{status.family} ({status.source})"


def _browser_display_allowed(settings: config.Settings) -> bool:
    if not sys.platform.startswith("linux") or settings.headless:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _browser_display_detail(settings: config.Settings) -> str:
    if not sys.platform.startswith("linux"):
        return "not required on this platform"
    if settings.headless:
        return "not required in headless mode"
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    return display or "headed Linux requires DISPLAY or WAYLAND_DISPLAY"


def _egress_mode_allowed(settings: config.Settings) -> bool:
    if settings.egress_mode in config.ALLOWED_EGRESS_MODES:
        return True
    return (
        settings.egress_mode == config.PERSONAL_DIRECT_EGRESS_MODE
        and settings.allow_personal_egress
    )


def _egress_mode_detail(settings: config.Settings) -> str:
    if not settings.egress_mode:
        return "not configured"
    if settings.egress_mode == config.PERSONAL_DIRECT_EGRESS_MODE:
        return (
            "personal_direct acknowledged"
            if settings.allow_personal_egress
            else "personal_direct requires CBRS_ALLOW_PERSONAL_EGRESS=1"
        )
    return settings.egress_mode


def _proxy_route_allowed(settings: config.Settings) -> bool:
    if settings.cloak_proxy_url:
        return False
    if not settings.proxy_url:
        return True
    return settings.egress_mode in {"dedicated_static_isp", "residential_sticky"}


def _proxy_route_detail(settings: config.Settings) -> str:
    if settings.cloak_proxy_url:
        return "CBRS_CLOAK_PROXY_URL configured"
    if not settings.proxy_url:
        return "not configured"
    if settings.egress_mode not in {"dedicated_static_isp", "residential_sticky"}:
        return (
            "CBRS_PROXY_URL requires CBRS_EGRESS_MODE="
            "dedicated_static_isp or residential_sticky"
        )
    return f"configured for {settings.egress_mode}"


def _runtime_headless(args: argparse.Namespace) -> bool:
    if getattr(args, "headed", False):
        return False
    if getattr(args, "headless", False):
        return True
    return config.SETTINGS.headless


def cmd_preflight(args: argparse.Namespace) -> int:
    result = run_preflight(
        config.SETTINGS,
        write_report=True,
        approve_baseline=args.approve_egress_baseline,
    )
    for check in result.report.get("checks", []):
        status = "OK" if check.get("ok") else "FAIL"
        print(f"{status:4} {check.get('name')}: {check.get('detail')}")
    if result.report_path:
        print(f"Preflight report: {result.report_path}")
    return 0 if result.ok else 1


def cmd_captcha_health() -> int:
    from .capsolver import CapSolverClient, CapSolverError
    from .captcha_budget import CaptchaBudgetStore
    from .captcha_solver import TwoCaptchaClient, TwoCaptchaError

    settings = config.SETTINGS
    provider = settings.external_captcha_provider
    if provider == "capsolver":
        api_key = settings.capsolver_api_key
        if not api_key:
            print("FAIL CapSolver API key: CBRS_CAPSOLVER_API_KEY is not configured", file=sys.stderr)
            return 1
        client = CapSolverClient(
            api_key,
            timeout_seconds=settings.capsolver_timeout_seconds,
            poll_seconds=settings.capsolver_poll_seconds,
        )
        error_type = CapSolverError
        label = "CapSolver"
    else:
        api_key = settings.two_captcha_api_key
        if not api_key:
            print("FAIL 2Captcha API key: CBRS_2CAPTCHA_API_KEY is not configured", file=sys.stderr)
            return 1
        client = TwoCaptchaClient(
            api_key,
            timeout_seconds=settings.two_captcha_timeout_seconds,
            poll_seconds=settings.two_captcha_poll_seconds,
        )
        error_type = TwoCaptchaError
        label = "2Captcha"
    try:
        balance = client.get_balance()
    except error_type as exc:
        print(f"FAIL {label} API: {exc.code}", file=sys.stderr)
        return 1
    if balance <= 0:
        print(f"FAIL {label} balance: zero balance", file=sys.stderr)
        return 1
    CaptchaBudgetStore(
        settings.captcha_state_path,
        daily_limit=settings.two_captcha_daily_limit,
        circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
        rejection_cooldown_seconds=settings.two_captcha_rejection_cooldown_seconds,
    ).clear_solver_disable()
    print(f"OK   {label} API: authenticated")
    print(f"OK   {label} balance: {balance:.4f}")
    return 0


def _require_preflight() -> dict[str, object]:
    result = run_preflight(config.SETTINGS, write_report=True)
    if result.ok:
        if result.report_path:
            print(f"Preflight passed. Report: {result.report_path}")
        return preflight_validation_metadata(result)

    if result.report_path:
        print(f"Preflight failed. Report: {result.report_path}", file=sys.stderr)
    raise SafetyStopException(
        StopReason.EGRESS_PREFLIGHT,
        "Fixed-egress preflight failed. Run `python -m cbrs preflight` for details.",
        context="preflight",
    )


def cmd_init(args: argparse.Namespace) -> None:
    from .scraper import CBRSScraper

    _require_preflight()
    print("Opening CBRS login page...")
    print("Please log in manually in the browser window.")
    print("The persistent profile will be reused automatically after login.")
    print("No raw cookies or session JSON will be exported.\n")
    print("Waiting for login...")
    timeout = args.timeout if args.timeout > 0 else None
    with CBRSScraper(headless=False) as scraper:
        scraper.init_session(timeout_seconds=timeout)
    print("\nSession ready! You can now run 'python -m cbrs search' or 'python -m cbrs download'.")


def cmd_search(args: argparse.Namespace, scraper: CBRSScraper) -> None:
    if args.query:
        results = scraper.search_by_text(args.query)
    else:
        results = scraper.search_by_fna(args.foja, args.numero, args.ano)
    display_results(results)


def cmd_download(args: argparse.Namespace, scraper: CBRSScraper) -> None:
    output_dir = Path(args.output)

    if args.query:
        results = scraper.search_by_text(args.query)
        indices = prompt_selection(results)
    else:
        results = scraper.search_by_fna(args.foja, args.numero, args.ano)
        display_results(results)
        indices = (
            prompt_selection(results, show_results=False)
            if len(results) > 1
            else list(range(len(results)))
        )

    if not results or not indices:
        print("Nothing to download.")
        return

    for index in indices:
        result = results[index]
        ticket = result.get("ticket")
        name = result.get("nombreSociedad", "unknown")
        print(f"\nDownloading images for: {name}")
        if not ticket:
            print(f"  No ticket found for result {index + 1}, skipping.")
            continue
        pdf_path = scraper.download_all_images(
            ticket,
            output_dir,
            keep_images=args.keep_images,
        )
        print(f"  PDF: {pdf_path}")

    print(f"\nDone. Files saved to {output_dir}/")


def cmd_validate(args: argparse.Namespace) -> int:
    headless = _runtime_headless(args)
    print("Running one controlled live validation.")
    print("This uses the persistent browser profile, normal pacing, and no retries.")

    result = run_controlled_validation(
        settings=config.SETTINGS,
        search_kind="text" if args.query else "fna",
        query=args.query,
        foja=args.foja,
        numero=args.numero,
        ano=args.ano,
        download_first=args.download_first,
        output_dir=Path(args.output),
        keep_images=args.keep_images,
        headless=headless,
    )

    if result.preflight_report_path:
        preflight_status = "passed" if result.report.get("preflight_status") == "passed" else "failed"
        stream = sys.stdout if preflight_status == "passed" else sys.stderr
        print(f"Preflight {preflight_status}. Report: {result.preflight_report_path}", file=stream)
    if result.result_count is not None:
        print(f"Search completed. Result count: {result.result_count}")
    if result.pdf_path:
        print(f"Download completed. PDF: {result.pdf_path}")
    if result.exit_code == 2:
        print(f"Safety stop: {result.error}", file=sys.stderr)
    elif result.exit_code == 1:
        print(f"Validation failed: {result.error}", file=sys.stderr)
    print(f"Validation report: {result.report_path}")
    return result.exit_code


def cmd_captcha_test(args: argparse.Namespace) -> int:
    """Run one headed, search-only CAPTCHA acceptance/control test."""
    from .account_pool import account_settings, load_account_pool_config

    provider = args.provider
    if provider == "capsolver" and not config.SETTINGS.capsolver_api_key:
        print("CBRS_CAPSOLVER_API_KEY is not configured.", file=sys.stderr)
        return 2
    pool_config = load_account_pool_config(
        config.SETTINGS,
        path=Path(args.config) if args.config else None,
    )
    account = _pool_account_by_id(pool_config, args.account)
    runtime_settings = replace(
        account_settings(config.SETTINGS, account),
        captcha_solver_mode=provider,
    )
    output_dir = (
        runtime_settings.output_dir
        / "captcha-tests"
        / provider
        / time.strftime("%Y%m%d-%H%M%S")
    )
    print(
        f"Running one headed {provider} CAPTCHA test with the account's "
        "existing proxy; no PDF will be downloaded."
    )
    result = run_controlled_validation(
        settings=runtime_settings,
        search_kind="fna",
        foja=args.foja,
        numero=args.numero,
        ano=args.ano,
        download_first=False,
        output_dir=output_dir,
        keep_images=False,
        headless=False,
    )
    payload = {
        "provider": provider,
        "account_id": account.account_id,
        "status": result.status,
        "result_count": result.result_count,
        "safety_stop": result.safety_stop,
        "error": result.error,
        "validation_report": str(result.report_path) if result.report_path else None,
        "preflight_report": (
            str(result.preflight_report_path) if result.preflight_report_path else None
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return result.exit_code


def cmd_soak(args: argparse.Namespace) -> int:
    from .soak import default_soak_store, dashboard_status, load_soak_config, run_soak

    store = default_soak_store(config.SETTINGS)
    if args.soak_command == "status":
        print(json.dumps(dashboard_status(store), ensure_ascii=False, indent=2))
        return 0
    if args.soak_command == "export":
        payload = store.export_snapshot()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
            print(f"Soak export: {output}")
        else:
            print(text)
        return 0
    if args.soak_command == "stop":
        store.request_stop()
        print("Stop requested. The soak runner will stop after the current safe point.")
        return 0

    soak_config = load_soak_config(
        config.SETTINGS,
        path=Path(args.config) if args.config else None,
    )
    if args.soak_command == "dashboard":
        from .soak_dashboard import start_dashboard

        host = args.host or soak_config.dashboard_host
        port = args.port if args.port is not None else soak_config.dashboard_port
        dashboard = start_dashboard(store, settings=config.SETTINGS, host=host, port=port)
        print(f"Soak dashboard: {dashboard.url}")
        print("Dashboard is running without starting the soak flow. Press Ctrl+C to stop it.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            dashboard.stop()
            print("\nDashboard stopped.")
        return 0

    result = run_soak(
        settings=config.SETTINGS,
        config=soak_config,
        store=store,
        dry_run=args.dry_run,
        max_cycles=args.max_cycles,
        dashboard=args.dashboard,
        headless=_runtime_headless(args),
        on_dashboard_start=lambda url: print(f"Soak dashboard: {url}"),
    )
    print(f"Soak run {result.status}: {result.run_id}")
    return result.exit_code


def cmd_pool(args: argparse.Namespace) -> int:
    from .account_pool import (
        account_settings,
        dashboard_status,
        default_pool_store,
        load_account_pool_config,
        run_account_pool,
    )

    pool_config = load_account_pool_config(
        config.SETTINGS,
        path=Path(args.config) if getattr(args, "config", None) else None,
    )
    store = default_pool_store(config.SETTINGS)

    if args.pool_command == "status":
        print(json.dumps(dashboard_status(store, config=pool_config), ensure_ascii=False, indent=2))
        return 0
    if args.pool_command == "proxy-health":
        from .endurance import EnduranceController, load_endurance_plan
        from .jobs import default_job_store
        from .proxy_health import run_proxy_health
        from .proxy_provider import (
            TWO_CAPTCHA_DEDICATED_ISP_PROVIDER,
            TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
            two_captcha_proxy_health,
        )

        job_store = default_job_store(config.SETTINGS)
        replace_baseline = bool(getattr(args, "replace_egress_baseline", False))
        if replace_baseline:
            if not args.account:
                print(
                    "FAIL --replace-egress-baseline requires exactly one --account.",
                    file=sys.stderr,
                )
                return 1
            if job_store.summary().get("worker"):
                print(
                    "FAIL egress baseline replacement requires no worker lease.",
                    file=sys.stderr,
                )
                return 1
            endurance = EnduranceController(
                job_store,
                load_endurance_plan(
                    config.SETTINGS.profile_dir.parent / "endurance-plan.json"
                ),
                pool_config,
            ).status()
            if not endurance.get("paused"):
                print(
                    "FAIL egress baseline replacement requires paused endurance.",
                    file=sys.stderr,
                )
                return 1
        accounts = (
            [_pool_account_by_id(pool_config, args.account)]
            if args.account
            else list(pool_config.accounts)
        )
        exit_code = 0
        for account in accounts:
            settings = account_settings(config.SETTINGS, account)
            print(f"{account.label} ({account.account_id})")
            account_provider = getattr(account, "proxy_provider", "generic_static")
            if account_provider in {
                TWO_CAPTCHA_DEDICATED_ISP_PROVIDER,
                TWO_CAPTCHA_RESIDENTIAL_STICKY_PROVIDER,
            }:
                provider = two_captcha_proxy_health(
                    settings.two_captcha_api_key,
                    provider=account_provider,
                    force=True,
                )
                provider_status = str(provider.get("status") or "unavailable")
                print(
                    f"{'OK' if provider.get('ok') else 'FAIL':4} "
                    f"2Captcha proxy account: {provider_status}"
                )
                if not provider.get("ok"):
                    job_store.set_account_check(account.account_id, proxy_status="failed")
                    exit_code = 1
                    continue
            preflight = run_preflight(
                settings,
                write_report=True,
                approve_baseline=args.approve_egress_baseline,
                allow_baseline_replacement=replace_baseline,
            )
            for check in preflight.report.get("checks", []):
                status = "OK" if check.get("ok") else "FAIL"
                print(f"{status:4} {check.get('name')}: {check.get('detail')}")
            if preflight.report_path:
                print(f"Preflight report: {preflight.report_path}")
            if not preflight.ok:
                job_store.set_account_check(account.account_id, proxy_status="failed")
                exit_code = 1
                continue
            result = run_proxy_health(settings, write_report=True)
            _print_proxy_health(result)
            if result.report_path:
                print(f"Proxy health report: {result.report_path}")
            if not result.ok:
                job_store.set_account_check(account.account_id, proxy_status="failed")
                exit_code = 1
                continue
            egress_hash = str(preflight.report.get("egress_hash") or "") or None
            if egress_hash:
                owner = job_store.egress_owner(
                    egress_hash,
                    exclude_account=account.account_id,
                )
                if owner:
                    print(
                        "FAIL shared egress: another enabled account uses the same fixed egress",
                        file=sys.stderr,
                    )
                    job_store.set_account_check(account.account_id, proxy_status="failed")
                    exit_code = 1
                    continue
            if replace_baseline:
                archive = replace_egress_baseline(
                    settings,
                    egress_hash=egress_hash or "",
                    egress_country=str(preflight.report.get("egress_country") or ""),
                )
                print(f"OK   egress baseline replaced; sanitized archive: {archive}")
            job_store.set_account_check(
                account.account_id,
                proxy_status="passed",
                egress_hash=egress_hash,
            )
        return exit_code
    if args.pool_command == "stop":
        store.request_stop()
        print("Stop requested. The account pool runner will stop after the current safe point.")
        return 0
    if args.pool_command == "dashboard":
        from .account_pool_dashboard import start_pool_dashboard

        host = args.host or pool_config.dashboard_host
        port = args.port if args.port is not None else pool_config.dashboard_port
        dashboard = start_pool_dashboard(
            store,
            settings=config.SETTINGS,
            config=pool_config,
            host=host,
            port=port,
        )
        print(f"Account pool dashboard: {dashboard.url}")
        print("Dashboard is running without starting the pool flow. Press Ctrl+C to stop it.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            dashboard.stop()
            print("\nDashboard stopped.")
        return 0
    if args.pool_command == "init":
        from .scraper import CBRSScraper

        account = _pool_account_by_id(pool_config, args.account)
        settings = account_settings(config.SETTINGS, account)
        result = run_preflight(settings, write_report=True, approve_baseline=True)
        for check in result.report.get("checks", []):
            status = "OK" if check.get("ok") else "FAIL"
            print(f"{status:4} {check.get('name')}: {check.get('detail')}")
        if result.report_path:
            print(f"Preflight report: {result.report_path}")
        if not result.ok:
            print("Pool account init stopped because preflight failed.", file=sys.stderr)
            return 1
        if not _run_and_print_proxy_health(settings):
            print("Pool account init stopped because proxy health failed.", file=sys.stderr)
            return 1

        print(f"Opening CBRS login page for {account.label}...")
        print("Please log in manually in the browser window.")
        print("This account has its own persistent Chrome profile.")
        print("No credentials, raw cookies, or session JSON will be exported.\n")
        print("Waiting for login...")
        timeout = args.timeout if args.timeout > 0 else None
        with CBRSScraper(headless=False, settings=settings) as scraper:
            scraper.init_session(timeout_seconds=timeout)
        print(f"\nSession ready for {account.label}.")
        return 0
    if args.pool_command == "login-debug":
        from .login_debug import run_login_debug

        account = _pool_account_by_id(pool_config, args.account)
        settings = account_settings(config.SETTINGS, account)
        result = run_preflight(settings, write_report=True, approve_baseline=True)
        for check in result.report.get("checks", []):
            status = "OK" if check.get("ok") else "FAIL"
            print(f"{status:4} {check.get('name')}: {check.get('detail')}")
        if result.report_path:
            print(f"Preflight report: {result.report_path}")
        if not result.ok:
            print("Pool login debug stopped because preflight failed.", file=sys.stderr)
            return 1
        if not _run_and_print_proxy_health(settings):
            print("Pool login debug stopped because proxy health failed.", file=sys.stderr)
            return 1

        print(f"Opening diagnostic CBRS login page for {account.label}...")
        print("Try the manual login once in that browser window.")
        print("The debug log stores only sanitized URLs, statuses, console errors, and redacted snippets.\n")
        timeout = args.timeout if args.timeout > 0 else None
        try:
            log_path = run_login_debug(settings, timeout_seconds=timeout, label=account.label)
        except SafetyStopException as exc:
            print(f"Login debug stopped: {exc}", file=sys.stderr)
            return 1
        print(f"\nLogin debug complete: {log_path}")
        return 0

    result = run_account_pool(
        settings=config.SETTINGS,
        config=pool_config,
        store=store,
        dry_run=args.dry_run,
        max_cycles=args.max_cycles,
        dashboard=args.dashboard,
        headless=_runtime_headless(args),
        on_dashboard_start=lambda url: print(f"Account pool dashboard: {url}"),
    )
    print(f"Account pool run {result.status}: {result.run_id}")
    return result.exit_code


def cmd_jobs(args: argparse.Namespace) -> int:
    from .account_pool import AccountPoolStore, load_account_pool_config
    from .endurance import EnduranceController, load_endurance_plan
    from .jobs import IdempotencyConflictError, default_job_store, run_job_worker

    store = default_job_store(config.SETTINGS)
    if args.jobs_command == "backup":
        from .backup import run_backup

        result = run_backup(settings=config.SETTINGS, database_path=store.path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.jobs_command == "backup-verify":
        from .backup import verify_backup_restore

        result = verify_backup_restore(
            settings=config.SETTINGS,
            require_pdf=args.require_pdf,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.jobs_command == "recover":
        payload = {
            "expired_worker_lease_cleared": store.clear_expired_lease(),
            "abandoned_jobs_requeued": store.recover_abandoned_jobs(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    pool_config = load_account_pool_config(
        config.SETTINGS,
        path=Path(args.config) if getattr(args, "config", None) else None,
    )
    endurance_plan = load_endurance_plan(
        Path(getattr(args, "endurance_plan", None))
        if getattr(args, "endurance_plan", None)
        else config.SETTINGS.profile_dir.parent / "endurance-plan.json"
    )
    endurance = EnduranceController(store, endurance_plan, pool_config)

    if args.jobs_command == "proxy-rotate":
        if not args.acknowledge_authorized_live_traffic:
            print(
                "Refusing live proxy rotation without --acknowledge-authorized-live-traffic.",
                file=sys.stderr,
            )
            return 2
        account = _pool_account_by_id(pool_config, args.account)
        if account.proxy_provider != "dataimpulse_residential_sticky":
            print("The selected account is not a DataImpulse sticky route.", file=sys.stderr)
            return 2
        if not store.active_lease():
            print("The worker lease must be active so it owns browser recovery.", file=sys.stderr)
            return 2
        try:
            request = store.request_dataimpulse_rotation(
                account.account_id,
                reason=args.reason,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        deadline = time.monotonic() + max(1.0, float(args.wait_seconds))
        while time.monotonic() < deadline:
            result = store.dataimpulse_rotation_result()
            if result and result.get("request_id") == request["request_id"]:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result.get("ok") else 1
            time.sleep(1)
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "pending",
                    "request_id": request["request_id"],
                    "account_id": account.account_id,
                },
                indent=2,
            )
        )
        return 1

    if args.jobs_command == "enqueue":
        kind = "text" if args.text is not None else "fna"
        input_data = (
            {"text": args.text}
            if kind == "text"
            else {"foja": args.foja, "numero": args.numero, "year": args.ano}
        )
        try:
            job, created = store.create_job(
                kind=kind,
                input_data=input_data,
                idempotency_key=args.idempotency_key,
            )
        except IdempotencyConflictError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "created": created,
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "status_url": f"/api/jobs/{job['job_id']}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.jobs_command == "list":
        print(json.dumps({"jobs": store.list_jobs(limit=args.limit)}, ensure_ascii=False, indent=2))
        return 0
    if args.jobs_command == "show":
        job = store.get_job(args.job_id)
        if not job:
            print(f"Unknown job: {args.job_id}", file=sys.stderr)
            return 1
        job["artifacts"] = store.artifacts(job_id=args.job_id)
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0
    if args.jobs_command == "cancel":
        job = store.request_cancel(args.job_id)
        if not job:
            print(f"Unknown job: {args.job_id}", file=sys.stderr)
            return 1
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0
    if args.jobs_command == "status":
        from .captcha_budget import CaptchaBudgetStore

        payload = store.summary()
        payload["endurance"] = endurance.status()
        payload["captcha"] = CaptchaBudgetStore(
            config.SETTINGS.captcha_state_path,
            daily_limit=config.SETTINGS.two_captcha_daily_limit,
            circuit_seconds=config.SETTINGS.two_captcha_circuit_breaker_seconds,
            rejection_cooldown_seconds=(
                config.SETTINGS.two_captcha_rejection_cooldown_seconds
            ),
        ).status()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.jobs_command == "captcha":
        from .captcha_budget import CaptchaBudgetError, CaptchaBudgetStore

        budget = CaptchaBudgetStore(
            config.SETTINGS.captcha_state_path,
            daily_limit=config.SETTINGS.two_captcha_daily_limit,
            circuit_seconds=config.SETTINGS.two_captcha_circuit_breaker_seconds,
            rejection_cooldown_seconds=(
                config.SETTINGS.two_captcha_rejection_cooldown_seconds
            ),
        )
        if args.captcha_command == "status":
            print(json.dumps(budget.status(), ensure_ascii=False, indent=2))
            return 0
        if config.SETTINGS.captcha_solver_mode not in {
            "2captcha_manual",
            "capsolver_manual",
        }:
            print(
                "CBRS_CAPTCHA_SOLVER_MODE must be 2captcha_manual or capsolver_manual.",
                file=sys.stderr,
            )
            return 2
        provider_key = (
            config.SETTINGS.capsolver_api_key
            if config.SETTINGS.external_captcha_provider == "capsolver"
            else config.SETTINGS.two_captcha_api_key
        )
        if not provider_key:
            print("The configured external CAPTCHA provider key is missing.", file=sys.stderr)
            return 2
        account = _pool_account_by_id(pool_config, args.account)
        account_store = AccountPoolStore(store.path)
        run = account_store.latest_run(dry_run=False)
        state = next(
            (
                row
                for row in account_store.accounts(str(run["run_id"]))
                if row["account_id"] == account.account_id
            ),
            None,
        ) if run else None
        if not state or state["status"] != "captcha_pending":
            print("The account must be captcha_pending before arming one solve.", file=sys.stderr)
            return 2
        try:
            budget.arm_manual(account_id=account.account_id)
        except CaptchaBudgetError as exc:
            print(f"2Captcha manual authorization refused: {exc.code}", file=sys.stderr)
            return 2
        account_store.mark_account_available(str(run["run_id"]), account.account_id)
        store.set_next_account(account.account_id, pool_config)
        released = store.release_waiting_captcha()
        account_store.add_event(
            str(run["run_id"]),
            account_id=account.account_id,
            message="one manual 2Captcha solve authorized",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "one_solve_armed",
                    "account_id": account.account_id,
                    "released_jobs": released,
                },
                indent=2,
            )
        )
        return 0
    if args.jobs_command == "endurance":
        if args.endurance_command == "pause":
            endurance.set_paused(True)
        elif args.endurance_command == "resume":
            endurance.set_paused(False)
        elif args.endurance_command == "run-once":
            job = endurance.maybe_enqueue(force=True)
            print(json.dumps({"created": bool(job), "job": job}, ensure_ascii=False, indent=2))
            return 0 if job else 1
        print(json.dumps(endurance.status(), ensure_ascii=False, indent=2))
        return 0
    if args.jobs_command == "safety-clear":
        store.clear_control("global_safety_stop")
        store.clear_control("global_safety_cooldown")
        store.clear_external_outage_backoff()
        store.add_event(
            "global_safety_stop_cleared",
            data={"operator_reason": args.reason},
        )
        print(json.dumps({"ok": True, "status": "safety_stop_cleared"}, indent=2))
        return 0
    if args.jobs_command == "dashboard":
        from .account_pool_dashboard import start_pool_dashboard

        account_store = AccountPoolStore(store.path)
        host = args.host or pool_config.dashboard_host
        port = args.port if args.port is not None else pool_config.dashboard_port
        dashboard = start_pool_dashboard(
            account_store,
            settings=config.SETTINGS,
            config=pool_config,
            host=host,
            port=port,
            job_store=store,
            allow_private_bind=args.allow_private_bind,
        )
        print(f"CBRS jobs dashboard and API: {dashboard.url}")
        scope = "private-network" if args.allow_private_bind else "loopback-only"
        print(f"The listener is {scope}. Press Ctrl+C to stop it.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            dashboard.stop()
            print("\nDashboard stopped.")
        return 0
    result = run_job_worker(
        settings=config.SETTINGS,
        config=pool_config,
        store=store,
        pool_store=AccountPoolStore(store.path),
        headless=_runtime_headless(args),
        once=args.once,
        max_jobs=args.max_jobs,
        poll_seconds=args.poll_seconds,
        endurance_plan=endurance_plan,
    )
    print(
        f"Job worker {result.status}: {result.processed_jobs} job(s) processed "
        f"(worker_id={result.worker_id})"
    )
    return result.exit_code


def _pool_account_by_id(pool_config, account_id: str):
    for account in pool_config.accounts:
        if account.account_id == account_id:
            return account
    available = ", ".join(account.account_id for account in pool_config.accounts)
    raise SystemExit(f"Unknown pool account {account_id!r}. Available accounts: {available}")


def _run_and_print_proxy_health(settings: config.Settings) -> bool:
    from .proxy_health import run_proxy_health

    result = run_proxy_health(settings, write_report=True)
    _print_proxy_health(result)
    if result.report_path:
        print(f"Proxy health report: {result.report_path}")
    return result.ok


def _print_proxy_health(result) -> None:
    for check in result.report.get("checks", []):
        status = "OK" if check.get("ok") else "FAIL"
        print(f"{status:4} {check.get('name')}: {check.get('detail')}")


def cmd_readiness(args: argparse.Namespace) -> int:
    from .readiness import (
        build_readiness_report,
        format_readiness_report,
        write_readiness_report,
    )

    repo_root = Path.cwd().resolve()
    env_file = Path(args.env_file) if args.env_file else None
    pool_config_path = Path(args.config)
    report = build_readiness_report(
        repo_root=repo_root,
        env_file=env_file,
        pool_config_path=pool_config_path,
        target=args.target,
        distro=args.distro,
        probe_wsl_runtime=args.probe_wsl_runtime,
        minimum_free_gib=args.minimum_free_gib,
        require_active_runtime=args.require_active_runtime,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_readiness_report(report))
    if args.json_report:
        output = write_readiness_report(report, Path(args.json_report))
        print(f"Readiness report: {output}", file=sys.stderr if args.json else sys.stdout)
    return 0 if report["summary"]["live_test_ready"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cbrs",
        description="CBRS Commerce Registry Scraper",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless after a persistent profile has been initialized",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Temporarily show the browser for automated commands",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        dest="headed",
        help="Legacy alias from the original scripts; same as --headed",
    )
    parser.add_argument(
        "--use-proxy",
        action="store_true",
        help="Legacy original-scripts flag; unsupported by the production fixed-trust runtime",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Open browser for manual login")
    init_parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Seconds to wait for login; 0 waits indefinitely",
    )

    subparsers.add_parser("doctor", help="Run local safety/configuration checks")
    subparsers.add_parser(
        "captcha-health",
        help="Validate the configured external CAPTCHA key and balance without creating a task",
    )
    readiness_parser = subparsers.add_parser(
        "readiness",
        help="Run the readiness gate for the native Windows endurance runtime",
    )
    readiness_parser.add_argument(
        "--target",
        choices=("current", "windows", "wsl", "ubuntu"),
        default="windows" if os.name == "nt" else "ubuntu",
        help="Runtime to inspect; defaults to native Windows on Windows",
    )
    readiness_parser.add_argument(
        "--distro",
        default="Ubuntu-24.04",
        help="Expected WSL distribution name",
    )
    readiness_parser.add_argument(
        "--probe-wsl-runtime",
        action="store_true",
        help="Start the installed WSL distro only long enough for read-only package checks",
    )
    readiness_parser.add_argument(
        "--config",
        default=str(config.SETTINGS.profile_dir.parent / "account-pool.json"),
        help="Account-pool JSON to validate without using it",
    )
    readiness_parser.add_argument(
        "--env-file",
        default=".env",
        help="Environment file to inspect without exposing its values",
    )
    readiness_parser.add_argument(
        "--minimum-free-gib",
        type=float,
        default=20.0,
        help="Warn when the current workspace volume has less free space",
    )
    readiness_parser.add_argument(
        "--require-active-runtime",
        action="store_true",
        help="Also require enabled/running Windows tasks, worker heartbeat, and dashboard health",
    )
    readiness_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the sanitized report as JSON",
    )
    readiness_parser.add_argument(
        "--json-report",
        default=None,
        help="Atomically write the sanitized report to this path",
    )
    preflight_parser = subparsers.add_parser("preflight", help="Run fixed-egress safety checks")
    preflight_parser.add_argument(
        "--approve-egress-baseline",
        action="store_true",
        help="Approve the current egress hash as the fixed baseline",
    )

    search_parser = subparsers.add_parser("search", help="Search commerce inscriptions")
    search_group = search_parser.add_mutually_exclusive_group(required=True)
    search_group.add_argument("--query", "-q", type=str, help="Search by razon social")
    search_group.add_argument("--foja", type=int, help="Foja number")
    search_parser.add_argument("--numero", type=int, help="Numero")
    search_parser.add_argument("--ano", type=int, help="Ano")

    download_parser = subparsers.add_parser("download", help="Search and download images")
    download_group = download_parser.add_mutually_exclusive_group(required=True)
    download_group.add_argument("--query", "-q", type=str, help="Search by razon social")
    download_group.add_argument("--foja", type=int, help="Foja number")
    download_parser.add_argument("--numero", type=int, help="Numero")
    download_parser.add_argument("--ano", type=int, help="Ano")
    download_parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=str(config.SETTINGS.output_dir),
        help="Output directory",
    )
    download_parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep individual JPEG files alongside the PDF",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="Run one controlled live validation and write a sanitized report",
    )
    validate_group = validate_parser.add_mutually_exclusive_group(required=True)
    validate_group.add_argument("--query", "-q", type=str, help="Search by razon social")
    validate_group.add_argument("--foja", type=int, help="Foja number")
    validate_parser.add_argument("--numero", type=int, help="Numero")
    validate_parser.add_argument("--ano", type=int, help="Ano")
    validate_parser.add_argument(
        "--download-first",
        action="store_true",
        help="After the validation search, download only the first result",
    )
    validate_parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=str(config.SETTINGS.output_dir),
        help="Output directory",
    )
    validate_parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep individual JPEG files alongside the PDF",
    )

    captcha_test_parser = subparsers.add_parser(
        "captcha-test",
        help="Run one headed, search-only CAPTCHA test through an account proxy",
    )
    captcha_test_parser.add_argument(
        "--provider",
        choices=["capsolver", "browser"],
        default="capsolver",
        help="Use CapSolver or a browser-native Google token",
    )
    captcha_test_parser.add_argument("--account", required=True)
    captcha_test_parser.add_argument("--foja", required=True, type=int)
    captcha_test_parser.add_argument("--numero", required=True, type=int)
    captcha_test_parser.add_argument("--ano", required=True, type=int)
    captcha_test_parser.add_argument("--config", default=None)

    soak_parser = subparsers.add_parser("soak", help="Run long-running CBRS soak checks")
    soak_subparsers = soak_parser.add_subparsers(dest="soak_command", required=True)
    soak_run_parser = soak_subparsers.add_parser(
        "run",
        help="Run the controlled long-running soak loop",
    )
    soak_run_parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Start the read-only local dashboard",
    )
    soak_run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exercise soak storage and dashboard without portal traffic",
    )
    soak_run_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Stop after this many cycles; omitted runs until stopped",
    )
    soak_run_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to .cbrs/soak-config.json override",
    )
    soak_subparsers.add_parser("status", help="Print the latest soak status JSON")
    soak_dashboard_parser = soak_subparsers.add_parser(
        "dashboard",
        help="Start the local dashboard without starting the soak loop",
    )
    soak_dashboard_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to .cbrs/soak-config.json override",
    )
    soak_dashboard_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Dashboard host; defaults to soak config",
    )
    soak_dashboard_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port; defaults to soak config",
    )
    soak_subparsers.add_parser(
        "stop",
        help="Request the active soak runner to stop after the current safe point",
    )
    soak_export_parser = soak_subparsers.add_parser(
        "export",
        help="Export latest soak history as sanitized JSON",
    )
    soak_export_parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Write export JSON to a file instead of stdout",
    )

    pool_parser = subparsers.add_parser("pool", help="Run the authorized account query pool")
    pool_subparsers = pool_parser.add_subparsers(dest="pool_command", required=True)

    pool_proxy_health_parser = pool_subparsers.add_parser(
        "proxy-health",
        help="Check fixed proxy egress, Google reCAPTCHA, and CBRS login prerequisites",
    )
    pool_proxy_health_parser.add_argument(
        "--account",
        default=None,
        help="Optional pool account id; omitted checks every configured account",
    )
    pool_proxy_health_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to .cbrs/account-pool.json override",
    )
    pool_baseline_group = pool_proxy_health_parser.add_mutually_exclusive_group()
    pool_baseline_group.add_argument(
        "--approve-egress-baseline",
        action="store_true",
        help="Approve each account's current fixed Chilean egress hash",
    )
    pool_baseline_group.add_argument(
        "--replace-egress-baseline",
        action="store_true",
        help=(
            "Replace one account's existing baseline after provider, country, "
            "portal, uniqueness, worker, and endurance gates pass"
        ),
    )

    pool_init_parser = pool_subparsers.add_parser(
        "init",
        help="Open browser for manual login into one pool account profile",
    )
    pool_init_parser.add_argument(
        "--account",
        required=True,
        help="Pool account id, e.g. ejecutivo_1",
    )
    pool_init_parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Seconds to wait for login; 0 waits indefinitely",
    )
    pool_init_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to .cbrs/account-pool.json override",
    )
    pool_login_debug_parser = pool_subparsers.add_parser(
        "login-debug",
        help="Open one pool account profile with sanitized login/network diagnostics",
    )
    pool_login_debug_parser.add_argument(
        "--account",
        required=True,
        help="Pool account id, e.g. ejecutivo_1",
    )
    pool_login_debug_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for login; 0 waits indefinitely",
    )
    pool_login_debug_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to .cbrs/account-pool.json override",
    )

    pool_run_parser = pool_subparsers.add_parser(
        "run",
        help="Run the controlled multi-account pool loop",
    )
    pool_run_parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Start the read-only local pool dashboard",
    )
    pool_run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exercise pool storage and dashboard without portal traffic",
    )
    pool_run_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Stop after this many cycles; omitted runs until stopped",
    )
    pool_run_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to .cbrs/account-pool.json override",
    )

    pool_dashboard_parser = pool_subparsers.add_parser(
        "dashboard",
        help="Start the local account pool dashboard without running the pool",
    )
    pool_dashboard_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to .cbrs/account-pool.json override",
    )
    pool_dashboard_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Dashboard host; defaults to pool config",
    )
    pool_dashboard_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port; defaults to pool config",
    )
    pool_subparsers.add_parser("status", help="Print the latest pool status JSON")
    pool_subparsers.add_parser(
        "stop",
        help="Request the active pool runner to stop after the current safe point",
    )

    jobs_parser = subparsers.add_parser("jobs", help="Manage the durable production job queue")
    jobs_subparsers = jobs_parser.add_subparsers(dest="jobs_command", required=True)
    jobs_enqueue = jobs_subparsers.add_parser("enqueue", help="Queue an authorized CBRS request")
    jobs_enqueue_group = jobs_enqueue.add_mutually_exclusive_group(required=True)
    jobs_enqueue_group.add_argument("--text", type=str, help="Search by razon social")
    jobs_enqueue_group.add_argument("--foja", type=int, help="Foja number")
    jobs_enqueue.add_argument("--numero", type=int, help="Numero")
    jobs_enqueue.add_argument("--year", "--ano", dest="ano", type=int, help="Inscription year")
    jobs_enqueue.add_argument(
        "--idempotency-key",
        default=None,
        help="Caller-provided key used to return the same job for repeated submissions",
    )
    jobs_enqueue.add_argument("--config", default=None, help="Account-pool JSON override")

    jobs_list = jobs_subparsers.add_parser("list", help="List recent jobs")
    jobs_list.add_argument("--limit", type=int, default=100)
    jobs_list.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_show = jobs_subparsers.add_parser("show", help="Show one job and its artifacts")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_cancel = jobs_subparsers.add_parser("cancel", help="Cancel a queued or active job")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_status = jobs_subparsers.add_parser("status", help="Show queue and worker status")
    jobs_status.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_recover = jobs_subparsers.add_parser(
        "recover",
        help="Clear only expired worker state and requeue abandoned jobs",
    )
    jobs_recover.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_proxy_rotate = jobs_subparsers.add_parser(
        "proxy-rotate",
        help="Request one worker-owned DataImpulse route rotation",
    )
    jobs_proxy_rotate.add_argument("--account", required=True)
    jobs_proxy_rotate.add_argument(
        "--reason",
        default="controlled_e2e_recovery",
        help="Sanitized operational reason",
    )
    jobs_proxy_rotate.add_argument("--wait-seconds", type=float, default=240.0)
    jobs_proxy_rotate.add_argument(
        "--acknowledge-authorized-live-traffic",
        action="store_true",
    )
    jobs_proxy_rotate.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_captcha = jobs_subparsers.add_parser(
        "captcha", help="Inspect or manually authorize one paid CAPTCHA solve"
    )
    jobs_captcha_sub = jobs_captcha.add_subparsers(dest="captcha_command", required=True)
    jobs_captcha_status = jobs_captcha_sub.add_parser("status")
    jobs_captcha_status.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_captcha_arm = jobs_captcha_sub.add_parser("arm")
    jobs_captcha_arm.add_argument("--account", required=True)
    jobs_captcha_arm.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_endurance = jobs_subparsers.add_parser(
        "endurance", help="Control the one-job endurance scheduler"
    )
    jobs_endurance_sub = jobs_endurance.add_subparsers(
        dest="endurance_command", required=True
    )
    for command in ("status", "pause", "resume", "run-once"):
        endurance_command = jobs_endurance_sub.add_parser(command)
        endurance_command.add_argument("--config", default=None, help=argparse.SUPPRESS)
        endurance_command.add_argument("--endurance-plan", default=None)
    jobs_safety_clear = jobs_subparsers.add_parser(
        "safety-clear",
        help="Clear a reviewed global safety stop before restarting the worker",
    )
    jobs_safety_clear.add_argument("--reason", required=True, help="Sanitized operator reason")
    jobs_safety_clear.add_argument("--config", default=None, help=argparse.SUPPRESS)

    jobs_worker = jobs_subparsers.add_parser("worker", help="Run the sequential production worker")
    jobs_worker.add_argument("--once", action="store_true", help="Process at most one claim and exit")
    jobs_worker.add_argument("--max-jobs", type=int, default=None)
    jobs_worker.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="Override the configured worker polling interval",
    )
    jobs_worker.add_argument("--config", default=None, help="Account-pool JSON override")
    jobs_worker.add_argument("--endurance-plan", default=None)

    jobs_dashboard = jobs_subparsers.add_parser(
        "dashboard", help="Serve the loopback jobs API and operational dashboard"
    )
    jobs_dashboard.add_argument("--config", default=None, help="Account-pool JSON override")
    jobs_dashboard.add_argument("--host", default=None, help="Listener address")
    jobs_dashboard.add_argument(
        "--allow-private-bind",
        action="store_true",
        help="Explicitly allow a non-loopback bind (WSL/private network only)",
    )
    jobs_dashboard.add_argument("--port", type=int, default=None)
    jobs_backup = jobs_subparsers.add_parser("backup", help="Back up SQLite and permanent PDFs")
    jobs_backup.add_argument("--config", default=None, help=argparse.SUPPRESS)
    jobs_backup_verify = jobs_subparsers.add_parser(
        "backup-verify",
        help="Restore the latest encrypted snapshot into a temporary directory and validate it",
    )
    jobs_backup_verify.add_argument(
        "--require-pdf",
        action="store_true",
        help="Also require at least one valid restored PDF artifact",
    )
    jobs_backup_verify.add_argument("--config", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.headless and args.headed:
        parser.error("--headless and --headed cannot be used together")
    if args.use_proxy:
        parser.error(
            "--use-proxy is not supported by this production runtime; "
            "configure an approved fixed egress path with CBRS_EGRESS_MODE instead"
        )
    configure_logging(args.verbose)
    validate_fna_args(args, parser)

    try:
        if args.command == "doctor":
            return cmd_doctor()
        if args.command == "captcha-health":
            return cmd_captcha_health()
        if args.command == "readiness":
            return cmd_readiness(args)
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "init":
            cmd_init(args)
            return 0
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "captcha-test":
            return cmd_captcha_test(args)
        if args.command == "soak":
            return cmd_soak(args)
        if args.command == "pool":
            return cmd_pool(args)
        if args.command == "jobs":
            return cmd_jobs(args)

        from .scraper import CBRSScraper

        _require_preflight()
        with CBRSScraper(headless=_runtime_headless(args)) as scraper:
            if args.command == "search":
                cmd_search(args, scraper)
            elif args.command == "download":
                cmd_download(args, scraper)
        return 0
    except SafetyStopException as exc:
        print(f"Safety stop: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
