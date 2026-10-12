"""CLI for the CBRS account-registration pipeline.

Usage::

    python -m cbrs.registration register --count 1 [--dry-run]
    python -m cbrs.registration resume-pending
    python -m cbrs.registration verify --email account@domain
    python -m cbrs.registration status
    python -m cbrs.registration export-pool
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_registration_settings
from .orchestrator import RegistrationOrchestrator, RegistrationResult
from .portal import RegistrationPortal, PortalError
from .rut_provider import NoRutsAvailable
from .store import AccountStore, append_to_account_pool


def _print_results(results: list[RegistrationResult]) -> None:
    for result in results:
        line = f"[{result.status.upper():>8}] {result.rut} -> {result.email}"
        if result.stage_failed:
            line += f" (failed at {result.stage_failed}: {result.reason})"
        if result.portal_user_id:
            line += f" (portal user id {result.portal_user_id})"
        print(line)


def _cmd_register(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    orchestrator = RegistrationOrchestrator(settings)
    try:
        results = orchestrator.register_accounts(count=args.count, dry_run=args.dry_run)
    except NoRutsAvailable as exc:
        print(f"No RUTs available: {exc}", file=sys.stderr)
        return 2
    _print_results(results)
    if args.dry_run:
        ok = all(item.status == "dry_run" for item in results)
        print("\nDry run complete: nothing was submitted; RUTs released." if ok
              else "\nDry run failed.", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    verified = sum(1 for item in results if item.status == "verified")
    print(f"\n{verified}/{len(results)} accounts fully verified.")
    return 0 if verified == len(results) else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    orchestrator = RegistrationOrchestrator(settings)
    results = orchestrator.resume_pending()
    if not results:
        print("Nothing pending: no accounts in status created/activated.")
        return 0
    _print_results(results)
    return 0 if all(item.status == "verified" for item in results) else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    settings.require_proxy()
    store = AccountStore(settings.accounts_db_path)
    record = store.get(args.email)
    if record is None:
        print(f"Unknown account: {args.email}", file=sys.stderr)
        return 2
    from .orchestrator import choose_sticky_port
    from ..dataimpulse import build_dataimpulse_proxy_url
    import random
    from pathlib import Path
    from datetime import datetime, timezone

    port = choose_sticky_port(settings, store, random.Random())
    proxy_url = build_dataimpulse_proxy_url(
        login=settings.dataimpulse_login,
        password=settings.dataimpulse_password,
        host=settings.dataimpulse_host,
        country=settings.dataimpulse_country,
        ttl_minutes=settings.dataimpulse_ttl_minutes,
        port=port,
        scheme=settings.dataimpulse_scheme,
        port_min=settings.dataimpulse_port_min,
        port_max=settings.dataimpulse_port_max,
    )
    profile_dir = settings.profile_root / (
        "verify-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    with RegistrationPortal(settings, proxy_url=proxy_url, profile_dir=profile_dir) as portal:
        try:
            portal.check_egress_country(settings.dataimpulse_country)
            portal.bootstrap()
            jwt = portal.login(record.email, record.password)
            me = portal.fetch_me(jwt)
        except PortalError as exc:
            print(f"Verification failed at {exc.stage}: {exc}", file=sys.stderr)
            return 1
    user_id = me.get("idUsuario") if isinstance(me, dict) else None
    store.mark_verified(record.email, portal_user_id=user_id)
    print(f"Login OK for {record.email} (portal user id {user_id}).")
    return 0


def _cmd_refill(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    from .rut_refill import DEFAULT_SOURCE, RefillSource, RutRefiller

    source = DEFAULT_SOURCE
    if args.source:
        source = RefillSource(name="custom", search_url=args.source)

    proxy_url = None
    if not args.direct:
        settings.require_proxy()
        import random

        from ..dataimpulse import build_dataimpulse_proxy_url
        from .orchestrator import choose_sticky_port

        store = AccountStore(settings.accounts_db_path)
        port = choose_sticky_port(settings, store, random.Random())
        proxy_url = build_dataimpulse_proxy_url(
            login=settings.dataimpulse_login,
            password=settings.dataimpulse_password,
            host=settings.dataimpulse_host,
            country=settings.dataimpulse_country,
            ttl_minutes=settings.dataimpulse_ttl_minutes,
            port=port,
            scheme=settings.dataimpulse_scheme,
            port_min=settings.dataimpulse_port_min,
            port_max=settings.dataimpulse_port_max,
        )
        print(f"Refilling through fresh DataImpulse sticky port {port}...")
    else:
        print("Refilling through the direct local connection...")

    terms = [term.strip() for term in args.terms.split(",") if term.strip()] if args.terms else None
    with RutRefiller(
        settings.rut_db_path,
        proxy_url=proxy_url,
        headless=False,
        source=source,
        request_delay_seconds=settings.request_delay_seconds,
    ) as refiller:
        report = refiller.refill(args.count, terms=terms)
    print(
        f"Inserted {report.inserted} new RUTs "
        f"({report.duplicates} duplicates, {report.invalid} invalid) "
        f"using terms {report.terms_used or '(none)'}."
    )
    for error in report.errors:
        print(f"  error: {error}", file=sys.stderr)
    return 0 if report.inserted > 0 or not report.errors else 1


def _cmd_status(_args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    store = AccountStore(settings.accounts_db_path)
    print("RUT pool (rut_data.db):")
    try:
        from .rut_provider import RutProvider

        for status, count in sorted(RutProvider(settings.rut_db_path).counts().items()):
            print(f"  {status}: {count}")
    except RuntimeError as exc:
        print(f"  unavailable: {exc}")
    print("Registered accounts (registered_accounts.db):")
    counts = store.counts()
    if not counts:
        print("  (none)")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    inbox_missing = not (
        settings.activation_domain and settings.imap_user and settings.imap_password
    )
    print(
        "Activation inbox: "
        + ("NOT configured (set CBRS_ACTIVATION_* in .env)" if inbox_missing else "configured")
    )
    return 0


def _cmd_export_pool(_args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    store = AccountStore(settings.accounts_db_path)
    entries = store.export_pool_entries()
    if not entries:
        print("No verified accounts to export.")
        return 0
    added = append_to_account_pool(".cbrs/account-pool.json", entries)
    print(f"Exported {added} verified accounts to .cbrs/account-pool.json "
          f"({len(entries) - added} already present).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cbrs.registration",
        description="CBRS portal account registration pipeline",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="register new accounts end to end")
    register.add_argument("--count", type=int, default=1)
    register.add_argument(
        "--dry-run",
        action="store_true",
        help="everything except submitting: reserve RUT, open browser, "
        "prove egress+bootstrap+signup captcha, then release the RUT",
    )
    register.set_defaults(func=_cmd_register)

    resume = sub.add_parser(
        "resume-pending", help="finish accounts stuck at created/activated"
    )
    resume.set_defaults(func=_cmd_resume)

    refill = sub.add_parser("refill", help="scrape new RUTs into rut_data.db")
    refill.add_argument("--count", type=int, default=50)
    refill.add_argument(
        "--terms",
        help="comma-separated search terms (default: simple numeric sequence)",
    )
    refill.add_argument("--source", help="alternate search page URL")
    refill.add_argument(
        "--direct", action="store_true", help="no proxy (sites may 403)"
    )
    refill.set_defaults(func=_cmd_refill)

    verify = sub.add_parser("verify", help="login-test one stored account")
    verify.add_argument("--email", required=True)
    verify.set_defaults(func=_cmd_verify)

    status = sub.add_parser("status", help="show RUT pool and account counts")
    status.set_defaults(func=_cmd_status)

    export = sub.add_parser(
        "export-pool", help="append verified accounts to .cbrs/account-pool.json"
    )
    export.set_defaults(func=_cmd_export_pool)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
