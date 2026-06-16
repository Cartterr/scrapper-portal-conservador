import argparse
import logging
import shutil
import sys
from pathlib import Path

from .scraper import CBRSScraper


def format_result(i: int, r: dict) -> str:
    """Format a single search result for display."""
    parts = [f"  [{i}] {r.get('nombreSociedad', 'N/A')}"]
    parts.append(
        f"      Foja: {r.get('foja')} | Número: {r.get('num')} | Año: {r.get('ano')}"
    )
    if r.get("acto"):
        parts.append(f"      Acto: {r['acto']}")
    if r.get("personas"):
        parts.append(f"      {r['personas']}")
    return "\n".join(parts)


def display_results(results: list[dict]) -> None:
    """Display search results as a numbered list."""
    if not results:
        print("No results found.")
        return
    print(f"\nFound {len(results)} result(s):\n")
    for i, r in enumerate(results, 1):
        print(format_result(i, r))
    print()


def prompt_selection(results: list[dict]) -> list[int]:
    """Prompt user to select results to download. Returns 0-based indices."""
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
                n = int(part.strip())
                if 1 <= n <= len(results):
                    indices.append(n - 1)
                else:
                    print(f"  Invalid number: {n}")
                    continue
            if indices:
                return indices
        except ValueError:
            pass
        print("  Please enter comma-separated numbers or 'all'.")


def cmd_init(args) -> None:
    """Handle the 'init' subcommand: manual login to save session."""
    from playwright.sync_api import sync_playwright
    from . import config
    from .session import save_session

    try:
        from playwright_stealth import Stealth
        stealth = Stealth()
    except ImportError:
        stealth = None

    # Load proxy if requested
    proxy = None
    if getattr(args, "use_proxy", False):
        proxy = config.get_proxy_config()
        if not proxy:
            print("Error: --use-proxy requires PROXY_2CAPTCHA_HOST in .env")
            sys.exit(1)

    pw = sync_playwright().start()
    launch_kwargs = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy:
        launch_kwargs["proxy"] = proxy
        print(f"Using proxy: {proxy['server']}")
    if shutil.which("google-chrome"):
        launch_kwargs["channel"] = "chrome"

    browser = pw.chromium.launch(**launch_kwargs)
    ctx = browser.new_context(bypass_csp=True)
    if stealth:
        stealth.apply_stealth_sync(ctx)
    page = ctx.new_page()

    print("Opening CBRS login page...")
    print("Please log in manually in the browser window.")
    print("The session will be saved automatically once you're logged in.\n")

    page.goto(
        f"{config.BASE_URL}"
        "/consultas-en-linea/indices/indice-del-registro-de-comercio",
        wait_until="networkidle",
        timeout=60000,
    )

    # Wait for the user to log in (detected by the refresh token cookie appearing)
    print("Waiting for login...")
    while True:
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        if "cbrs_refresh_token" in cookies:
            break
        page.wait_for_timeout(1000)

    save_session(cookies)
    print(f"\nSession saved! You can now run 'cbrs search' or 'cbrs download'.")

    browser.close()
    pw.stop()


def cmd_search(args, scraper: CBRSScraper) -> None:
    """Handle the 'search' subcommand."""
    if args.query:
        results = scraper.search_by_text(args.query)
    else:
        results = scraper.search_by_fna(args.foja, args.numero, args.ano)
    display_results(results)


def cmd_download(args, scraper: CBRSScraper) -> None:
    """Handle the 'download' subcommand."""
    output_dir = Path(args.output)

    if args.query:
        results = scraper.search_by_text(args.query)
        indices = prompt_selection(results)
    elif args.foja and args.numero and args.ano:
        results = scraper.search_by_fna(args.foja, args.numero, args.ano)
        display_results(results)
        if len(results) <= 1:
            indices = list(range(len(results)))
        else:
            indices = prompt_selection(results)
    else:
        print("Error: provide --query or --foja/--numero/--ano")
        sys.exit(1)

    if not results or not indices:
        print("Nothing to download.")
        return

    for idx in indices:
        r = results[idx]
        ticket = r.get("ticket")
        name = r.get("nombreSociedad", "unknown")
        print(f"\nDownloading images for: {name}")

        if not ticket:
            print(f"  No ticket found for result {idx + 1}, skipping.")
            continue

        pdf_path = scraper.download_all_images(
            ticket, output_dir, keep_images=args.keep_images
        )
        print(f"  PDF: {pdf_path}")

    print(f"\nDone. Files saved to {output_dir}/")


def main():
    parser = argparse.ArgumentParser(
        prog="cbrs",
        description="CBRS Commerce Registry Scraper",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--no-headless", action="store_true", help="Show browser window"
    )
    parser.add_argument(
        "--use-proxy", action="store_true",
        help="Route browser through 2captcha proxy for login (requires PROXY_2CAPTCHA_* in .env)"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Init subcommand — manual login to save session
    subparsers.add_parser(
        "init", help="Open browser for manual login (saves session for future runs)"
    )

    # Search subcommand
    search_parser = subparsers.add_parser("search", help="Search commerce inscriptions")
    search_group = search_parser.add_mutually_exclusive_group(required=True)
    search_group.add_argument("--query", "-q", type=str, help="Search by razón social")
    search_group.add_argument("--foja", type=int, help="Foja number (use with --numero and --ano)")
    search_parser.add_argument("--numero", type=int, help="Número")
    search_parser.add_argument("--ano", type=int, help="Año")

    # Download subcommand
    dl_parser = subparsers.add_parser("download", help="Search and download images")
    dl_group = dl_parser.add_mutually_exclusive_group(required=True)
    dl_group.add_argument("--query", "-q", type=str, help="Search by razón social")
    dl_group.add_argument("--foja", type=int, help="Foja number (use with --numero and --ano)")
    dl_parser.add_argument("--numero", type=int, help="Número")
    dl_parser.add_argument("--ano", type=int, help="Año")
    dl_parser.add_argument(
        "--output", "-o", type=str, default="./output", help="Output directory (default: ./output)"
    )
    dl_parser.add_argument(
        "--keep-images", action="store_true", help="Keep individual JPEG files alongside the PDF"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.command == "init":
        cmd_init(args)
        return

    with CBRSScraper(headless=not args.no_headless, use_proxy=args.use_proxy) as scraper:
        if args.command == "search":
            cmd_search(args, scraper)
        elif args.command == "download":
            cmd_download(args, scraper)


if __name__ == "__main__":
    main()
