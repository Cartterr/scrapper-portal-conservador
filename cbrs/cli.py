from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import config
from .browser_runtime import get_browser_status
from .preflight import preflight_validation_metadata, run_preflight
from .safety import SafetyStopException, StopReason, redact_text
from .validation import (
    run_controlled_validation,
)


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
    return settings.egress_mode == "dedicated_static_isp"


def _proxy_route_detail(settings: config.Settings) -> str:
    if settings.cloak_proxy_url:
        return "CBRS_CLOAK_PROXY_URL configured"
    if not settings.proxy_url:
        return "not configured"
    if settings.egress_mode != "dedicated_static_isp":
        return "CBRS_PROXY_URL requires CBRS_EGRESS_MODE=dedicated_static_isp"
    return "configured for dedicated_static_isp"


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


def _pool_account_by_id(pool_config, account_id: str):
    for account in pool_config.accounts:
        if account.account_id == account_id:
            return account
    available = ", ".join(account.account_id for account in pool_config.accounts)
    raise SystemExit(f"Unknown pool account {account_id!r}. Available accounts: {available}")


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
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "init":
            cmd_init(args)
            return 0
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "soak":
            return cmd_soak(args)
        if args.command == "pool":
            return cmd_pool(args)

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
