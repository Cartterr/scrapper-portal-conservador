from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cbrs.account_pool import (
    account_credentials,
    account_settings,
    load_account_pool_config,
)
from cbrs.config import SETTINGS
from cbrs.jobs import default_job_store
from cbrs.scraper import CBRSScraper


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Keep one authenticated headed Chrome context open per CBRS account."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    args = parser.parse_args()

    config = load_account_pool_config(SETTINGS, path=args.config)
    accounts = [account for account in config.accounts if account.enabled]
    if len(accounts) != 3:
        raise RuntimeError("Exactly three enabled CBRS accounts are required.")

    owner = f"headed-recovery-{uuid.uuid4().hex[:10]}"
    store = default_job_store(SETTINGS)
    managers: list[tuple[str, CBRSScraper]] = []
    failures = 0
    try:
        for account in accounts:
            settings = account_settings(SETTINGS, account)
            username, password = account_credentials(account)
            scraper = CBRSScraper(headless=False, settings=settings)
            scraper.__enter__()
            managers.append((account.account_id, scraper))
            store.set_account_browser_state(
                account.account_id,
                live=True,
                authenticated=False,
                headless=False,
                owner=owner,
                status="authenticating",
            )
            try:
                method = scraper.ensure_authenticated(username, password)
            except Exception as exc:
                failures += 1
                store.set_account_browser_state(
                    account.account_id,
                    live=True,
                    authenticated=False,
                    headless=False,
                    owner=owner,
                    status="authentication_unconfirmed",
                )
                print(
                    json.dumps(
                        {
                            "account": account.account_id,
                            "authenticated": False,
                            "browser_mode": "headed",
                            "error_type": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
                continue
            status = {
                "refreshed": "authenticated_refresh",
                "browser_fetch": "authenticated_login_api",
                "browser_form": "authenticated_login_form",
            }.get(str(method or ""), "authenticated")
            store.set_account_browser_state(
                account.account_id,
                live=True,
                authenticated=True,
                headless=False,
                owner=owner,
                status=status,
            )
            print(
                json.dumps(
                    {
                        "account": account.account_id,
                        "authenticated": True,
                        "browser_mode": "headed",
                        "method": method,
                    }
                ),
                flush=True,
            )

        print(
            json.dumps(
                {
                    "ready": failures == 0,
                    "contexts_open": len(managers),
                    "authenticated": len(managers) - failures,
                    "failures": failures,
                }
            ),
            flush=True,
        )
        while True:
            time.sleep(max(1.0, args.heartbeat_seconds))
            for account_id, scraper in managers:
                try:
                    requires_login = scraper.browser.page_requires_login()
                except Exception:
                    continue
                if requires_login:
                    store.set_account_browser_state(
                        account_id,
                        live=True,
                        authenticated=False,
                        headless=False,
                        owner=owner,
                        status="login_gate_visible",
                    )
    except KeyboardInterrupt:
        return 0
    finally:
        for account_id, scraper in reversed(managers):
            try:
                scraper.close()
            except Exception:
                pass
            finally:
                store.set_account_browser_state(
                    account_id,
                    live=False,
                    authenticated=False,
                    headless=False,
                    owner=owner,
                    status="worker_stopped",
                )


if __name__ == "__main__":
    raise SystemExit(main())
