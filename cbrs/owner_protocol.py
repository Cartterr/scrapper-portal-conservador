"""Versioned local durable commands. No sockets, cookies, credentials or eval RPC.

The SQLite file is a trusted-local-user boundary, not a remote API. Its directory
must have the same restricted ACL as the protected runtime configuration.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

OWNER_LEASE = "browser_owner"
OPERATIONS = {"ensure", "search_fna", "search_text", "image_refs", "download_image", "recover_route"}


def owner_preserves_recovery_contexts(settings, store):
    """Fail closed for older owners; never trust a previous owner's marker."""
    try:
        lease = store.active_lease(OWNER_LEASE)
        marker = json.loads((command_path(settings).parent / 'capabilities.json').read_text())
        return bool(lease and marker.get('owner') == lease['owner']
                    and marker.get('retain_production_on_recovery') is True)
    except (OSError, ValueError, TypeError, KeyError):
        return False


class OwnerCommands:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS owner_commands (
              id TEXT PRIMARY KEY, version INTEGER NOT NULL, worker TEXT NOT NULL,
              account TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
              payload_hash TEXT NOT NULL, job_id TEXT, state TEXT NOT NULL,
              result TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS owner_commands_pending ON owner_commands(state,created);
            CREATE TABLE IF NOT EXISTS owner_control (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, worker, account, operation, payload, *, command_id=None, job_id=None):
        if operation not in OPERATIONS:
            raise ValueError("Unsupported browser-owner operation")
        raw = json.dumps(payload, sort_keys=True)
        if len(raw) > 65536:
            raise ValueError("Browser command too large")
        digest = hashlib.sha256(raw.encode()).hexdigest()
        identity = command_id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM owner_commands WHERE id=?", (identity,)).fetchone()
            if previous:
                if (previous["account"], previous["operation"], previous["payload_hash"], previous["job_id"]) != (account, operation, digest, job_id):
                    raise ValueError("Idempotency conflict")
                return identity
            now = time.time()
            db.execute("INSERT INTO owner_commands VALUES(?,1,?,?,?,?,?,?, 'queued',NULL,NULL,?,?)",
                       (identity, worker, account, operation, raw, digest, job_id, now, now))
        return identity

    def claim(self, active_worker):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A dead/replaced worker cannot leave a queued browser action behind.
            db.execute("UPDATE owner_commands SET state='cancelled',error='worker_replaced',updated=? WHERE state='queued' AND worker<>?",
                       (time.time(), active_worker or ""))
            if not active_worker:
                return None
            row = db.execute("SELECT * FROM owner_commands WHERE state='queued' AND worker=? ORDER BY created LIMIT 1", (active_worker,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE owner_commands SET state='running',updated=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def finish(self, identity, *, result=None, error=None, uncertain=False):
        state = "uncertain" if uncertain else "failed" if error else "succeeded"
        with self.connect() as db:
            db.execute("UPDATE owner_commands SET state=?, result=?,error=?,updated=? WHERE id=? AND state='running'",
                       (state, json.dumps(result), json.dumps(error) if error else None, time.time(), identity))

    def read(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM owner_commands WHERE id=?", (identity,)).fetchone()
            return dict(row) if row else None

    def recover_owner_crash(self):
        # Never replay operations whose target-side outcome is unknowable.
        with self.connect() as db:
            db.execute("UPDATE owner_commands SET state='uncertain',error='owner_interrupted',updated=? WHERE state='running'", (time.time(),))

    def cancel_queued(self, identity):
        with self.connect() as db:
            db.execute("UPDATE owner_commands SET state='cancelled',error='caller_timeout',updated=? WHERE id=? AND state='queued'", (time.time(), identity))

    def stop(self):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO owner_control VALUES('stop','1')")

    def clear_stop(self):
        with self.connect() as db:
            db.execute("DELETE FROM owner_control WHERE key='stop'")

    def stopping(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM owner_control WHERE key='stop'").fetchone())


def command_path(settings):
    return settings.profile_dir.parent / "browser-owner" / "commands.sqlite3"


class RemoteBrowser:
    is_remote = True
    page = None  # never expose a remote page/evaluate/close capability

    def __init__(self, scraper):
        self.scraper = scraper
        self.settings = scraper.settings

    def detect_commerce_auth_state(self):
        from .browser_session import CommerceAuthState
        state = self.scraper.store.account_check(self.settings.account_id) or {}
        return CommerceAuthState(state.get("browser_auth_state") or "unknown")

    def wait_for_commerce_auth_state(self):
        return self.detect_commerce_auth_state()

    def has_visible_rejected_login(self):
        from .jobs import seconds_since
        state = self.scraper.store.account_check(self.settings.account_id) or {}
        return (state.get("browser_status") == "login_rejected_visible" and
                seconds_since(state.get("browser_checked_at") or "") < 15)

    def preserve_for_service_lifetime(self):
        pass

    def set_preview_callback(self, callback):
        pass  # the independent owner already publishes previews

    def shutdown_service_context(self):
        pass  # worker shutdown means detach, NEVER owner shutdown

    def close(self, **kwargs):
        pass

    def reload_current_page(self):
        # Deliberately not a protocol operation: recovery uses bounded ensure.
        return None


class RemoteScraper:
    def __init__(self, *, settings, headless=None, worker_id, store_path, commands_path, timeout=360):
        from .jobs import JobStore
        self.settings, self.worker_id, self.timeout = settings, worker_id, timeout
        self.store = JobStore(store_path)
        self.commands = OwnerCommands(commands_path)
        self.headless = settings.headless if headless is None else headless
        self.browser = RemoteBrowser(self)
        self.job_id = None

    def __enter__(self):
        if not self.store.active_lease(OWNER_LEASE):
            raise RuntimeError("Independent browser owner is not available")
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        pass

    def set_job_context(self, job_id):
        self.job_id = job_id

    def _call(self, operation, payload):
        from .safety import SafetyStopException, StopReason
        from .browser_session import CredentialsRejectedError
        if not self.store.active_lease(OWNER_LEASE):
            raise RuntimeError("Independent browser owner is not available")
        identity = self.commands.submit(self.worker_id, self.settings.account_id, operation, payload,
                                        job_id=None if operation in {"ensure", "recover_route"} else self.job_id)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            row = self.commands.read(identity)
            if row["state"] == "succeeded":
                return json.loads(row["result"])
            if row["state"] in {"failed", "uncertain", "cancelled"}:
                try:
                    error = json.loads(row["error"] or "{}")
                except ValueError:
                    error = {}
                if isinstance(error, dict) and error.get("reason") in {reason.value for reason in StopReason}:
                    raise SafetyStopException(StopReason(error["reason"]), "Browser owner reported a portal failure", status=error.get("status"), context=error.get("context") or "browser owner")
                if isinstance(error, dict) and error.get("reason") == "credentials_invalid":
                    raise CredentialsRejectedError()
                raise RuntimeError("Browser operation incomplete; inspect durable owner receipt, do not replay")
            time.sleep(.2)
        self.commands.cancel_queued(identity)
        raise RuntimeError("Browser operation outcome pending; do not replay")

    def ensure_authenticated(self, username=None, password=None, *, force=False):
        return self._call("ensure", {"force": bool(force), "binding": binding_fingerprint(self.settings, self.headless)})

    def search_by_fna(self, foja, numero, ano):
        return self._call("search_fna", {"foja": foja, "numero": numero, "ano": ano})

    def search_by_text(self, texto):
        return self._call("search_text", {"texto": texto})

    def get_image_refs(self, ticket):
        return self._call("image_refs", {"ticket": ticket})

    def download_image(self, data_ref, output_path):
        self._call("download_image", {"data_ref": data_ref, "output_path": str(output_path)})
        return Path(output_path)


def binding_fingerprint(settings, headless):
    # Compare immutable launch identity without sending credentials over IPC.
    fields = (str(settings.profile_dir.resolve()), settings.proxy_url,
              bool(headless), settings.browser_backend,
              str(getattr(settings, "browser_executable_path", "")))
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()
