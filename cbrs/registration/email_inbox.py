"""Activation-inbox support: address generation and IMAP retrieval.

The registration pipeline uses a catch-all domain (Cloudflare Email Routing)
that forwards every address to one real Gmail mailbox.  Each registration gets
a unique address; this module waits for the portal's activation email to that
address and extracts the ``/activacion-de-cuenta?email=...&token=...`` link.
"""

from __future__ import annotations

import email
import email.utils
import html
import imaplib
import random
import re
import string
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from urllib.parse import parse_qs, unquote, urlparse

ACTIVATION_PATH = "/activacion-de-cuenta"
_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+activacion-de-cuenta\?[^\s\"'<>\\]+", re.IGNORECASE
)


class ActivationEmailTimeout(RuntimeError):
    """No activation email arrived within the configured window."""


@dataclass(frozen=True)
class ActivationLink:
    email: str
    token: str
    raw_url: str


def build_activation_address(domain: str, rut: str, rng: random.Random | None = None) -> str:
    """Create a unique catch-all address tied to a RUT.

    Example: ``cbrs-10000262-k7x2@example.com`` for RUT ``1.000.026-2``.
    """

    rng = rng or random.Random()
    compact = re.sub(r"[^0-9a-zA-Z]", "", rut or "").lower()
    if not compact:
        raise ValueError("Cannot build an activation address without a RUT")
    suffix = "".join(rng.choices(string.ascii_lowercase + string.digits, k=4))
    local = f"cbrs-{compact}-{suffix}"
    clean_domain = (domain or "").strip().lstrip("@").lower()
    if not clean_domain or "@" in clean_domain:
        raise ValueError("CBRS_ACTIVATION_DOMAIN must be a bare domain")
    return f"{local}@{clean_domain}"


def parse_activation_link(body: str) -> ActivationLink | None:
    """Extract the activation email+token from a message body (HTML or text)."""

    if not body:
        return None
    unescaped = html.unescape(body)
    match = _URL_RE.search(unescaped)
    if not match:
        return None
    raw_url = match.group(0)
    # Trailing punctuation from text mails (periods, parentheses) is not part
    # of the link.
    raw_url = raw_url.rstrip(".,;:!?)]}'\"")
    parsed = urlparse(html.unescape(raw_url))
    query = parse_qs(parsed.query)
    email_values = query.get("email")
    token_values = query.get("token")
    if not email_values or not token_values:
        return None
    return ActivationLink(
        email=unquote(email_values[0]),
        token=unquote(token_values[0]),
        raw_url=raw_url,
    )


def _message_body(msg: Message) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in {"text/plain", "text/html"}:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(parts)


def _recipients(msg: Message) -> set[str]:
    values: set[str] = set()
    for header in ("To", "Delivered-To", "X-Original-To", "Resent-To"):
        raw = msg.get_all(header) or []
        for value in raw:
            for address in re.findall(r"[\w.+-]+@[\w.-]+", str(value)):
                values.add(address.lower())
    return values


def _message_date(msg: Message) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ImapActivationInbox:
    """Reads activation emails from the forwarding mailbox over IMAP."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        *,
        mailbox_candidates: tuple[str, ...] = ("[Gmail]/All Mail", "INBOX"),
    ) -> None:
        if not user or not password:
            raise ValueError("IMAP user and app password are required")
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.mailbox_candidates = mailbox_candidates

    def wait_for_activation(
        self,
        address: str,
        *,
        not_before: datetime,
        timeout_seconds: float,
        poll_seconds: float,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> ActivationLink:
        """Poll the inbox until the activation mail for ``address`` arrives."""

        target = address.strip().lower()
        deadline = monotonic() + max(1.0, timeout_seconds)
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                link = self._search_once(target, not_before)
                if link is not None:
                    return link
            except (imaplib.IMAP4.error, OSError, email.errors.MessageError) as exc:
                last_error = exc
            sleep(max(1.0, poll_seconds))
        detail = f" Last IMAP error: {type(last_error).__name__}" if last_error else ""
        raise ActivationEmailTimeout(
            f"No activation email for {target} within {timeout_seconds:.0f}s.{detail}"
        )

    def _search_once(self, target: str, not_before: datetime) -> ActivationLink | None:
        connection = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            connection.login(self.user, self.password)
            mailbox = self._select_mailbox(connection)
            since = (not_before - timedelta(minutes=5)).strftime("%d-%b-%Y")
            ids = self._search_ids(connection, mailbox, target, since)
            for message_id in ids:
                link = self._inspect_message(connection, mailbox, message_id, target, not_before)
                if link is not None:
                    return link
            return None
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    def _select_mailbox(self, connection: imaplib.IMAP4_SSL) -> str:
        listed = connection.list()
        available = set()
        for line in listed[1] or []:
            decoded = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
            match = re.search(r'"([^"]+)"$|(\S+)$', decoded)
            if match:
                available.add((match.group(1) or match.group(2) or "").strip())
        for candidate in self.mailbox_candidates:
            if candidate in available or candidate == "INBOX":
                status, _ = connection.select(candidate, readonly=True)
                if status == "OK":
                    return candidate
        status, _ = connection.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("IMAP could not select any mailbox")
        return "INBOX"

    def _search_ids(
        self, connection: imaplib.IMAP4_SSL, mailbox: str, target: str, since: str
    ) -> list[bytes]:
        ids: list[bytes] = []
        for criteria in (
            ("TO", f'"{target}"'),
            ("SINCE", since),
        ):
            try:
                status, data = connection.search(None, *criteria)
            except imaplib.IMAP4.error:
                continue
            if status == "OK" and data and data[0]:
                ids.extend(data[0].split())
            if ids:
                break
        # Newest first: activation mails arrive right after registration.
        seen: set[bytes] = set()
        unique = [item for item in ids if not (item in seen or seen.add(item))]
        unique.sort(key=lambda raw: int(raw) if raw.isdigit() else 0, reverse=True)
        return unique[:50]

    def _inspect_message(
        self,
        connection: imaplib.IMAP4_SSL,
        mailbox: str,
        message_id: bytes,
        target: str,
        not_before: datetime,
    ) -> ActivationLink | None:
        status, data = connection.fetch(message_id, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return None
        raw = data[0][1] if isinstance(data[0], tuple) else None
        if not raw:
            return None
        msg = email.message_from_bytes(raw)
        if target not in _recipients(msg):
            return None
        when = _message_date(msg)
        if when is not None and when < not_before - timedelta(minutes=5):
            return None
        body = _message_body(msg)
        if ACTIVATION_PATH not in body:
            return None
        return parse_activation_link(body)
