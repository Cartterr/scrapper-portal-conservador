from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config
from .browser_runtime import get_browser_status
from .preflight import preflight_validation_metadata, run_preflight
from .safety import SafetyStopException, StopReason, redact_text
from .validation import (
    finish_validation_report,
    new_validation_report,
    write_validation_report,
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


def prompt_selection(results: list[dict]) -> list[int]:
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
            "production proxy disabled",
            settings.cloak_proxy_url is None,
            "not configured" if settings.cloak_proxy_url is None else "CBRS_CLOAK_PROXY_URL configured",
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
    print("Opening CBRS in a persistent Chrome/Edge profile.")
    print("Log in manually in the browser window; no raw cookies will be exported.")
    timeout = args.timeout if args.timeout > 0 else None
    with CBRSScraper(headless=False) as scraper:
        scraper.init_session(timeout_seconds=timeout)
    print("Login detected. Persistent profile is ready.")


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
        indices = list(range(len(results))) if len(results) <= 1 else prompt_selection(results)

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
    from .scraper import CBRSScraper

    headless = _runtime_headless(args)
    preflight_result = run_preflight(config.SETTINGS, write_report=True)
    report = new_validation_report(
        config.SETTINGS,
        search_kind="text" if args.query else "fna",
        download_first=args.download_first,
        preflight_metadata=preflight_validation_metadata(preflight_result),
        headless=headless,
    )

    if not preflight_result.ok:
        finish_validation_report(
            report,
            status="safety_stop",
            safety_stop=StopReason.EGRESS_PREFLIGHT.value,
            error="Fixed-egress preflight failed.",
        )
        report_path = write_validation_report(report, config.SETTINGS)
        if preflight_result.report_path:
            print(f"Preflight failed. Report: {preflight_result.report_path}", file=sys.stderr)
        print(f"Validation report: {report_path}")
        return 2

    print("Running one controlled live validation.")
    print("This uses the persistent browser profile, normal pacing, and no retries.")
    if preflight_result.report_path:
        print(f"Preflight passed. Report: {preflight_result.report_path}")

    try:
        with CBRSScraper(headless=headless) as scraper:
            if args.query:
                results = scraper.search_by_text(args.query)
            else:
                results = scraper.search_by_fna(args.foja, args.numero, args.ano)

            report["result_count"] = len(results)
            print(f"Search completed. Result count: {len(results)}")

            if args.download_first:
                if not results:
                    raise RuntimeError("Cannot download: validation search returned no results.")
                ticket = results[0].get("ticket")
                if not ticket:
                    raise RuntimeError("Cannot download: first result did not include a ticket.")
                pdf_path = scraper.download_all_images(
                    ticket,
                    Path(args.output),
                    keep_images=args.keep_images,
                )
                report["pdf_created"] = True
                report["pdf_path"] = str(pdf_path)
                report["pdf_size_bytes"] = pdf_path.stat().st_size
                print(f"Download completed. PDF: {pdf_path}")

        finish_validation_report(report, status="passed")
        report_path = write_validation_report(report, config.SETTINGS)
        print(f"Validation report: {report_path}")
        return 0
    except SafetyStopException as exc:
        finish_validation_report(
            report,
            status="safety_stop",
            safety_stop=exc.reason.value,
            error=str(exc),
        )
        report_path = write_validation_report(report, config.SETTINGS)
        print(f"Safety stop: {exc}", file=sys.stderr)
        print(f"Validation report: {report_path}")
        return 2
    except Exception as exc:
        finish_validation_report(report, status="failed", error=str(exc))
        report_path = write_validation_report(report, config.SETTINGS)
        print(f"Validation failed: {exc}", file=sys.stderr)
        print(f"Validation report: {report_path}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cbrs",
        description="CBRS Commerce Registry operator tool",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.headless and args.headed:
        parser.error("--headless and --headed cannot be used together")
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
