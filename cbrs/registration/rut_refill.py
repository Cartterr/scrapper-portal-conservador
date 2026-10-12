"""RUT replenishment for the registration pipeline.

Proven method (HOW_TO_SCRAPING_RUTS.md): open a Chilean lookup site, search a
*simple* numeric term ("1", "21", ...), and harvest (name, RUT) pairs from the
results table.  Every harvested RUT is checksum-validated (mod 11) and
deduplicated against ``rut_data.db`` before insertion with status ``scraped``.

Browser work uses the same headful regular Chrome as the registration portal,
optionally through a fresh DataImpulse sticky exit (lookup sites 403 some
datacenter/foreign IPs).  DB and parsing logic are pure and unit-tested.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

RUT_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{3})*-[\dkK]$")
RUT_SEARCH_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*-[\dkK]")


@dataclass(frozen=True)
class RefillSource:
    name: str
    search_url: str
    input_selector: str | None = None
    button_selector: str | None = None


# Documented working flow (HOW_TO_SCRAPING_RUTS.md).  Others can be passed
# with --source; the refiller falls back to generic selectors.
DEFAULT_SOURCE = RefillSource(
    name="nombrerutyfirma",
    search_url="https://www.nombrerutyfirma.com/buscarRut",
    input_selector='input[name="rut"]',
    button_selector='button[aria-label="Buscar"]',
)

GENERIC_INPUT_SELECTORS = (
    'input[name="rut"]',
    'input[type="search"]',
    'input[type="text"]',
    "input",
)
GENERIC_BUTTON_SELECTORS = (
    'button[aria-label="Buscar"]',
    'button[type="submit"]',
    "button",
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def normalize_rut(raw: str) -> str | None:
    """Canonicalize a RUT to ``1.000.026-2`` form; None when malformed."""

    clean = re.sub(r"[^0-9kK]", "", raw or "").upper()
    if len(clean) < 8 or len(clean) > 9:
        return None
    body, dv = clean[:-1], clean[-1]
    if not body.isdigit():
        return None
    body = body.lstrip("0") or "0"
    reversed_body = body[::-1]
    groups = [reversed_body[i : i + 3] for i in range(0, len(reversed_body), 3)]
    formatted = ".".join(groups)[::-1]
    return f"{formatted}-{dv}"


def validate_rut(rut: str) -> bool:
    """Chilean mod-11 check-digit validation."""

    clean = re.sub(r"[^0-9kK]", "", rut or "").upper()
    if len(clean) < 8 or len(clean) > 9:
        return False
    body, dv = clean[:-1], clean[-1]
    if not body.isdigit():
        return False
    total = 0
    factor = 2
    for digit in reversed(body):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    remainder = 11 - (total % 11)
    expected = "0" if remainder == 11 else ("K" if remainder == 10 else str(remainder))
    return dv == expected


class _TableTextParser(HTMLParser):
    """Collects table rows as lists of cell text (no external deps)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_html_tables(html: str) -> list[list[str]]:
    parser = _TableTextParser()
    parser.feed(html or "")
    return parser.rows


def extract_pairs(rows: Iterable[Sequence[str]]) -> list[tuple[str, str]]:
    """Turn result-table rows into (name, normalized RUT) pairs."""

    pairs: list[tuple[str, str]] = []
    for cells in rows:
        texts = [str(cell).strip() for cell in cells if str(cell).strip()]
        if len(texts) < 2:
            continue
        rut_cell = next(
            (text for text in texts if RUT_PATTERN.match(text)), None
        )
        if rut_cell is None:
            continue
        normalized = normalize_rut(rut_cell)
        if normalized is None:
            continue
        name_candidates = [
            text
            for text in texts
            if text != rut_cell
            and len(text.split()) >= 2
            and sum(char.isalpha() or char.isspace() for char in text) >= len(text) * 0.8
        ]
        if not name_candidates:
            continue
        name = max(name_candidates, key=len)
        pairs.append((name, normalized))
    return pairs


def insert_new_ruts(
    db_path: str | Path, pairs: Iterable[tuple[str, str]]
) -> tuple[int, int, int]:
    """Validate + dedupe + insert.  Returns (inserted, duplicates, invalid)."""

    path = Path(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = duplicates = invalid = 0
    with sqlite3.connect(path, timeout=30) as conn:
        existing = {
            normalize_rut(str(row[0]))
            for row in conn.execute("SELECT rut FROM ruts").fetchall()
        }
        existing.discard(None)
        for name, rut in pairs:
            if not validate_rut(rut):
                invalid += 1
                continue
            if rut in existing:
                duplicates += 1
                continue
            conn.execute(
                "INSERT INTO ruts (id, rut, name, company, registration_date, "
                "status, raw_html, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, 'unknown', 'scraped', NULL, ?, ?)",
                (str(uuid.uuid4()), rut, name, now, now),
            )
            existing.add(rut)
            inserted += 1
        conn.commit()
    return inserted, duplicates, invalid


def default_terms(count: int) -> list[str]:
    """Simple numeric search terms, per the proven scraping method."""

    terms: list[str] = []
    single = [str(digit) for digit in range(1, 10)]
    terms.extend(single)
    candidate = 10
    while len(terms) < max(1, count):
        terms.append(str(candidate))
        candidate += 1
    return terms[: max(1, count)]


# ---------------------------------------------------------------------------
# Browser-driven refiller
# ---------------------------------------------------------------------------

@dataclass
class RefillReport:
    inserted: int = 0
    duplicates: int = 0
    invalid: int = 0
    terms_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class RutRefiller:
    def __init__(
        self,
        db_path: str | Path,
        *,
        proxy_url: str | None = None,
        headless: bool = False,
        source: RefillSource = DEFAULT_SOURCE,
        request_delay_seconds: float = 2.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.proxy_url = proxy_url
        self.headless = headless
        self.source = source
        self.request_delay_seconds = request_delay_seconds
        self._playwright = None
        self._browser = None
        self._page = None

    def open(self) -> "RutRefiller":
        from playwright.sync_api import sync_playwright

        from ..browser_runtime import detect_browser, validate_native_chrome_executable
        from ..browser_session import CHROME_IGNORED_DEFAULT_ARGS
        from .portal import _playwright_proxy

        executable = detect_browser()
        validate_native_chrome_executable(executable.path)
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(
                executable_path=str(executable.path),
                headless=self.headless,
                chromium_sandbox=True,
                proxy=_playwright_proxy(self.proxy_url) if self.proxy_url else None,
                args=[],
                ignore_default_args=CHROME_IGNORED_DEFAULT_ARGS,
                handle_sigint=False,
            )
        except Exception:
            self._stop_playwright()
            raise
        self._page = self._browser.new_page()
        return self

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            logger.debug("Refill browser close failed", exc_info=True)
        finally:
            self._browser = None
            self._page = None
            self._stop_playwright()

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("Playwright stop failed", exc_info=True)
            self._playwright = None

    def __enter__(self) -> "RutRefiller":
        return self.open()

    def __exit__(self, *args: object) -> None:
        self.close()

    def search_term(self, term: str) -> list[tuple[str, str]]:
        assert self._page is not None
        page = self._page
        page.goto(self.source.search_url, wait_until="domcontentloaded", timeout=60000)
        input_locator = self._first_visible(page, self.source.input_selector, GENERIC_INPUT_SELECTORS)
        if input_locator is None:
            raise RuntimeError(f"No search input found at {self.source.search_url}")
        input_locator.fill(term)
        button = self._first_visible(page, self.source.button_selector, GENERIC_BUTTON_SELECTORS)
        if button is not None:
            button.click()
        else:
            input_locator.press("Enter")
        try:
            page.wait_for_selector("table", timeout=15000)
        except Exception:
            logger.debug("No table appeared for term %s; parsing page anyway", term)
        rows = parse_html_tables(page.content())
        return extract_pairs(rows)

    @staticmethod
    def _first_visible(page, preferred: str | None, fallbacks: tuple[str, ...]):
        selectors = ((preferred,) if preferred else ()) + fallbacks
        for selector in selectors:
            if not selector:
                continue
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=2000):
                    return locator
            except Exception:
                continue
        return None

    def refill(self, count: int, terms: Sequence[str] | None = None) -> RefillReport:
        report = RefillReport()
        pending = list(terms) if terms else default_terms(count * 3)
        for term in pending:
            if report.inserted >= count:
                break
            try:
                pairs = self.search_term(term)
            except Exception as exc:
                message = f"term {term!r}: {type(exc).__name__}"
                logger.warning("Refill search failed for %s", message)
                report.errors.append(message)
                continue
            report.terms_used.append(term)
            inserted, duplicates, invalid = insert_new_ruts(self.db_path, pairs)
            report.inserted += inserted
            report.duplicates += duplicates
            report.invalid += invalid
            logger.info(
                "Term %s: +%d new (%d dup, %d invalid), total %d/%d",
                term, inserted, duplicates, invalid, report.inserted, count,
            )
            if self.request_delay_seconds > 0:
                import time

                time.sleep(self.request_delay_seconds)
        return report
