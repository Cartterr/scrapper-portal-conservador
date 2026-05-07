from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

from .adapters.commerce import CommerceAdapter, public_result
from .browser_session import BrowserSession
from .config import Settings, find_chrome_path
from .errors import PortalCallError
from .jobs import Job, JobStore
from .logging_utils import configure_logging, dumps_safe
from .portal_client import PortalClient
from .safety import SafetyStop


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(args, "verbose", False))
    settings = Settings.from_env()
    settings.ensure_local_dirs()
    store = JobStore(settings.sqlite_path)
    _sync_configured_accounts(settings, store)

    try:
        return dispatch(args, settings, store)
    except SafetyStop as exc:
        print(f"Safety stop: {exc.reason}", file=sys.stderr)
        return 3
    except PortalCallError as exc:
        print(f"Portal error: {exc.classified.code} ({exc.status}) {exc.classified.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cbrs", description="CBRS portal worker")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check local setup")
    doctor.add_argument("--live", action="store_true", help="Probe the live portal")

    init = sub.add_parser("init", help="Open browser for manual login")
    init.add_argument("--timeout", type=int, default=600, help="Seconds to wait for login")

    search = sub.add_parser("search", help="Run a live Comercio search")
    add_search_args(search)
    search.add_argument("--json", action="store_true", help="Print JSON")

    download = sub.add_parser("download", help="Run a live Comercio search and download first/all selected PDFs")
    add_search_args(download)
    download.add_argument("--output", "-o", default="output", help="Output directory")
    download.add_argument("--all", action="store_true", help="Download every result instead of only the first")
    download.add_argument("--keep-images", action="store_true", help="Keep page images beside the PDF")

    enqueue = sub.add_parser("enqueue", help="Add a job to the local queue")
    enqueue_sub = enqueue.add_subparsers(dest="job_kind", required=True)
    q_text = enqueue_sub.add_parser("search-text", help="Queue text search")
    q_text.add_argument("--query", "-q", required=True)
    q_fna = enqueue_sub.add_parser("search-fna", help="Queue FNA search")
    add_fna_args(q_fna)
    q_dl = enqueue_sub.add_parser("download-fna", help="Queue FNA download")
    add_fna_args(q_dl)
    q_dl.add_argument("--output", "-o", default="output")

    worker = sub.add_parser("worker", help="Process queued jobs")
    worker.add_argument("--limit", type=int, default=1, help="Maximum jobs to process")

    jobs = sub.add_parser("jobs", help="List jobs")
    jobs.add_argument("--limit", type=int, default=50)

    sub.add_parser("accounts", help="List configured/local account budget state")
    safety = sub.add_parser("safety", help="Inspect or clear live safety state")
    safety_sub = safety.add_subparsers(dest="safety_command", required=True)
    safety_sub.add_parser("status", help="Show current live safety state")
    unlock = safety_sub.add_parser("unlock", help="Clear manual-required safety state")
    unlock.add_argument("--reason", required=True, help="Operator recovery reason")
    events = safety_sub.add_parser("events", help="Show recent live safety decisions")
    events.add_argument("--limit", type=int, default=50)
    return parser


def add_search_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--query", "-q", help="Search by razon social/text")
    group.add_argument("--foja", type=int, help="Foja")
    parser.add_argument("--numero", type=int, help="Numero")
    parser.add_argument("--ano", type=int, help="Ano")


def add_fna_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--foja", type=int, required=True)
    parser.add_argument("--numero", type=int, required=True)
    parser.add_argument("--ano", type=int, required=True)


def dispatch(args, settings: Settings, store: JobStore) -> int:
    match args.command:
        case "doctor":
            return cmd_doctor(args, settings, store)
        case "init":
            return cmd_init(args, settings, store)
        case "search":
            return cmd_search(args, settings, store)
        case "download":
            return cmd_download(args, settings, store)
        case "enqueue":
            return cmd_enqueue(args, settings, store)
        case "worker":
            return cmd_worker(args, settings, store)
        case "jobs":
            return cmd_jobs(args, store)
        case "accounts":
            return cmd_accounts(settings, store)
        case "safety":
            return cmd_safety(args, settings, store)
    raise AssertionError(args.command)


def cmd_doctor(args, settings: Settings, store: JobStore) -> int:
    checks = {
        "python": sys.version.split()[0],
        "chrome": bool(find_chrome_path()),
        "data_dir": settings.data_dir.exists(),
        "database": settings.sqlite_path.exists(),
        "browser_profile_dir": settings.browser_profile_dir.exists(),
        "configured_accounts": len(settings.accounts),
    }
    print(dumps_safe({"settings": settings.redacted_dict(), "checks": checks}))
    if args.live:
        with BrowserSession(settings) as browser:
            with PortalClient(settings, browser, store) as client:
                with urllib.request.urlopen(f"{settings.base_url}/", timeout=30) as response:
                    print(
                        dumps_safe(
                            {
                                "portal_root": {
                                    "status": response.status,
                                    "content_type": response.headers.get("content-type"),
                                }
                            }
                        )
                    )
                state = browser.login_state()
                print(dumps_safe({"browser_login": state.__dict__}))
                home = client.home_start()
                print(dumps_safe({"home_start": {"status": home.status, "keys": sorted(home.data.keys()) if isinstance(home.data, dict) else []}}))
    return 0


def cmd_init(args, settings: Settings, store: JobStore) -> int:
    with BrowserSession(settings) as browser:
        state = browser.manual_login(timeout_seconds=args.timeout)
        store.update_session(
            profile_path=settings.browser_profile_dir,
            last_refresh_status="manual_login_ok",
            token_expires_at=state.token_expires_at,
        )
    print("Login profile ready.")
    return 0


def cmd_search(args, settings: Settings, store: JobStore) -> int:
    results = _run_search(args, settings, store)
    public = [public_result(item) for item in results]
    query_key = _query_key(args)
    for item in public:
        store.save_search_result(query_key=query_key, source="commerce", public_data=item)
    if args.json:
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        _print_results(public)
    return 0


def cmd_download(args, settings: Settings, store: JobStore) -> int:
    output = Path(args.output)
    with BrowserSession(settings) as browser, PortalClient(settings, browser, store) as client:
        _ensure_client_auth(settings, client)
        adapter = CommerceAdapter(client)
        results = _search_with_adapter(args, adapter)
        if not results:
            print("No results found.")
            return 0
        selected = results if args.all else results[:1]
        for result in selected:
            ticket = result.get("ticket")
            if not ticket:
                print(f"Skipping result without ticket: {public_result(result)}")
                continue
            record = adapter.download_all_images(ticket, output, keep_images=args.keep_images)
            store.save_artifact(
                path=record.path,
                content_type=record.content_type,
                sha256=record.sha256,
                bytes_count=record.bytes,
                page_count=record.page_count,
            )
            print(f"PDF: {record.path}")
    return 0


def cmd_enqueue(args, settings: Settings, store: JobStore) -> int:
    if args.job_kind == "search-text":
        job_id = store.create_job("commerce.search_text", {"query": args.query}, max_attempts=settings.max_job_attempts)
    elif args.job_kind == "search-fna":
        job_id = store.create_job(
            "commerce.search_fna",
            {"foja": args.foja, "numero": args.numero, "ano": args.ano},
            max_attempts=settings.max_job_attempts,
        )
    elif args.job_kind == "download-fna":
        job_id = store.create_job(
            "commerce.download_fna",
            {"foja": args.foja, "numero": args.numero, "ano": args.ano, "output": args.output},
            max_attempts=settings.max_job_attempts,
        )
    else:
        raise AssertionError(args.job_kind)
    print(f"Queued job {job_id}")
    return 0


def cmd_worker(args, settings: Settings, store: JobStore) -> int:
    processed = 0
    with BrowserSession(settings) as browser, PortalClient(settings, browser, store) as client:
        _ensure_client_auth(settings, client)
        adapter = CommerceAdapter(client)
        for _ in range(args.limit):
            job = store.claim_next()
            if not job:
                break
            try:
                _process_job(job, adapter, store)
                store.complete_job(job.id)
            except SafetyStop as exc:
                store.fail_job(job.id, code="safety_stop", message=exc.reason, retryable=False)
                raise
            except PortalCallError as exc:
                store.fail_job(
                    job.id,
                    code=str(exc.classified.code),
                    message=exc.classified.message,
                    retryable=exc.classified.retryable,
                )
            except Exception as exc:
                store.fail_job(job.id, code="local_error", message=str(exc), retryable=True)
            processed += 1
    print(f"Processed {processed} job(s)")
    return 0


def cmd_jobs(args, store: JobStore) -> int:
    rows = [
        {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "attempts": job.attempts,
            "next_run_at": job.next_run_at,
            "last_error_code": job.last_error_code,
        }
        for job in store.list_jobs(limit=args.limit)
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_accounts(settings: Settings, store: JobStore) -> int:
    rows = [
        {
            "label": row["label"],
            "display_label": row["display_label"],
            "used_today": row["used_today"],
            "daily_budget": row["daily_budget"],
            "exhausted_date": row["exhausted_date"],
        }
        for row in store.accounts()
    ]
    print(dumps_safe({"configured": len(settings.accounts), "accounts": rows}))
    return 0


def cmd_safety(args, settings: Settings, store: JobStore) -> int:
    if args.safety_command == "status":
        print(dumps_safe(store.safety_status(profile_path=settings.browser_profile_dir)))
        return 0
    if args.safety_command == "unlock":
        store.unlock_safety(reason=args.reason)
        print(dumps_safe({"state": "ok", "unlocked": True}))
        return 0
    if args.safety_command == "events":
        rows = [
            {
                "id": row["id"],
                "event": row["event"],
                "endpoint": row["endpoint"],
                "status": row["status"],
                "classified_code": row["classified_code"],
                "message": row["message"],
                "created_at": row["created_at"],
            }
            for row in store.safety_events(limit=args.limit)
        ]
        print(dumps_safe({"events": rows}))
        return 0
    raise AssertionError(args.safety_command)


def _run_search(args, settings: Settings, store: JobStore) -> list[dict[str, Any]]:
    _validate_search_args(args)
    with BrowserSession(settings) as browser, PortalClient(settings, browser, store) as client:
        _ensure_client_auth(settings, client)
        adapter = CommerceAdapter(client)
        return _search_with_adapter(args, adapter)


def _search_with_adapter(args, adapter: CommerceAdapter) -> list[dict[str, Any]]:
    if args.query:
        return adapter.search_text(args.query)
    return adapter.search_fna(args.foja, args.numero, args.ano)


def _process_job(job: Job, adapter: CommerceAdapter, store: JobStore) -> None:
    if job.kind == "commerce.search_text":
        results = adapter.search_text(job.input["query"])
        for result in results:
            store.save_search_result(
                query_key=f"text:{job.input['query']}",
                source="commerce",
                public_data=public_result(result),
            )
        return
    if job.kind == "commerce.search_fna":
        results = adapter.search_fna(job.input["foja"], job.input["numero"], job.input["ano"])
        for result in results:
            store.save_search_result(
                query_key=f"fna:{job.input['foja']}:{job.input['numero']}:{job.input['ano']}",
                source="commerce",
                public_data=public_result(result),
            )
        return
    if job.kind == "commerce.download_fna":
        results = adapter.search_fna(job.input["foja"], job.input["numero"], job.input["ano"])
        if not results:
            return
        ticket = results[0].get("ticket")
        if not ticket:
            raise RuntimeError("First FNA result did not include ticket")
        record = adapter.download_all_images(ticket, Path(job.input.get("output", "output")))
        store.save_artifact(
            job_id=job.id,
            path=record.path,
            content_type=record.content_type,
            sha256=record.sha256,
            bytes_count=record.bytes,
            page_count=record.page_count,
        )
        return
    raise RuntimeError(f"Unsupported job kind: {job.kind}")


def _ensure_client_auth(settings: Settings, client: PortalClient) -> None:
    account = settings.accounts[0] if settings.accounts else None
    client.ensure_auth(account)


def _sync_configured_accounts(settings: Settings, store: JobStore) -> None:
    for account in settings.accounts:
        store.upsert_account(
            label=account.label,
            email_hash=account.email_hash,
            display_label=account.display_label,
            daily_budget=settings.daily_query_budget_per_account,
        )


def _validate_search_args(args) -> None:
    if args.foja is not None and (args.numero is None or args.ano is None):
        raise SystemExit("--foja requires --numero and --ano")


def _query_key(args) -> str:
    if args.query:
        return f"text:{args.query}"
    return f"fna:{args.foja}:{args.numero}:{args.ano}"


def _print_results(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No results found.")
        return
    for index, result in enumerate(results, 1):
        print(
            f"[{index}] {result.get('nombreSociedad', 'N/A')} | "
            f"Foja {result.get('foja')} | Numero {result.get('num') or result.get('numero')} | "
            f"Ano {result.get('ano')} | {result.get('acto', '')}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
