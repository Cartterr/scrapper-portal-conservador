"""Activation-inbox support: address generation and IMAP retrieval.

The registration pipeline uses a catch-all domain (e.g. Cloudflare Email
Routing, or any registrar/provider catch-all) that forwards every address on
that domain to one real mailbox.  Each registration gets a unique address; this
module waits for the portal's activation email to that address and extracts the
``/activacion-de-cuenta?email=...&token=...`` link.

The forwarding mailbox is *any* provider reachable over IMAP -- Gmail, Outlook /
Microsoft 365, Proton Bridge, a self-hosted server, ... -- so no specific email
brand is required.  :class:`ActivationInbox` is the public contract;
:class:`ImapActivationInbox` is the bundled implementation.
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
from typing import Callable, Protocol, runtime_checkable
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


@runtime_checkable
class ActivationInbox(Protocol):
    """Contract for reading the portal's activation email.

    Any mailbox that can receive mail for the generated address satisfies it:
    the only requirement is that :meth:`wait_for_activation` returns the
    activation link (or raises :class:`ActivationEmailTimeout`) within the
    timeout.  :class:`ImapActivationInbox` is the bundled implementation; a new
    provider is any class with the same method.
    """

    def wait_for_activation(
        self,
        address: str,
        *,
        not_before: datetime,
        timeout_seconds: float,
        poll_seconds: float,
        sleep: Callable[[float], None] = ...,
    ) -> ActivationLink: ...


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


def _folder_names(lines) -> list[str]:
    """Parse IMAP LIST responses into folder names (handles quoted names)."""

    names: list[str] = []
    for line in lines or []:
        decoded = (
            line.decode("utf-8", errors="replace")
            if isinstance(line, bytes)
            else str(line)
        )
        match = re.search(r'"([^"]+)"$|(\S+)$', decoded)
        if match:
            name = (match.group(1) or match.group(2) or "").strip()
            if name:
                names.append(name)
    return names


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
    """Reads activation emails from any IMAP mailbox over SSL or STARTTLS.

    Provider-agnostic: pass the IMAP host/port and credentials of whatever
    mailbox receives the catch-all forwards.  Defaults match the common
    mailbox; override ``mailbox_candidates`` (or ``use_ssl``/``starttls``) for
    a provider with a different folder layout or transport.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        *,
        mailbox_candidates: tuple[str, ...] = ("[Gmail]/All Mail", "INBOX"),
        use_ssl: bool = True,
        starttls: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        if not user or not password:
            raise ValueError("IMAP user and app password are required")
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.mailbox_candidates = mailbox_candidates
        self.use_ssl = use_ssl
        self.starttls = starttls
        self._timeout = timeout_seconds

    def _connect(self) -> imaplib.IMAP4:
        kwargs: dict[str, object] = {}
        if self._timeout is not None:
            kwargs["timeout"] = self._timeout
        if self.use_ssl:
            return imaplib.IMAP4_SSL(self.host, self.port, **kwargs)
        connection = imaplib.IMAP4(self.host, self.port, **kwargs)
        if self.starttls:
            connection.starttls()
        return connection

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
        connection = self._connect()
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

    def list_folders(self) -> list[str]:
        """Return the mailbox folder names (read-only; no message is read)."""

        connection = self._connect()
        try:
            connection.login(self.user, self.password)
            status, lines = connection.list()
            return _folder_names(lines) if status == "OK" else []
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    def _select_mailbox(self, connection: imaplib.IMAP4) -> str:
        listed = connection.list()
        available = set(_folder_names(listed[1]))
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
        self, connection: imaplib.IMAP4, mailbox: str, target: str, since: str
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
        connection: imaplib.IMAP4,
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
