from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cbrs import account_pool as pool
from cbrs.config import load_settings


@pytest.mark.parametrize("preboot", [False, True])
def test_fresh_run_only_replaced_with_boot_evidence(tmp_path, monkeypatch, preboot):
    config = pool.load_account_pool_config(load_settings({}, root=tmp_path))
    store = pool.AccountPoolStore(tmp_path / "pool.sqlite3")
    store.create_run(run_id="old", dry_run=False, config=config, dashboard_url=None)
    store.mark_account_captcha_pending("old", "ejecutivo_1")
    monkeypatch.setattr(pool, "_heartbeat_predates_boot", lambda _: preboot)
    if not preboot:
        with pytest.raises(RuntimeError, match="already active"):
            store.create_run(run_id="new", dry_run=False, config=config, dashboard_url=None)
        return
    store.create_run(run_id="new", dry_run=False, config=config, dashboard_url=None)
    with store._connect() as db:
        old = db.execute("SELECT * FROM runs WHERE run_id='old'").fetchone()
        assert old["status"] == "stale"
        assert old["finished_at"]
        assert old["blocked_reason"] == "runner heartbeat predates current boot"
    account = next(a for a in store.accounts("new") if a["account_id"] == "ejecutivo_1")
    assert account["paused_reason"] == "captcha_rejected"


def test_boot_evidence_is_conservative(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: f"btime {int(now.timestamp())}\n")
    assert pool._heartbeat_predates_boot((now - timedelta(seconds=5)).isoformat())
    assert not pool._heartbeat_predates_boot((now + timedelta(seconds=5)).isoformat())
    assert not pool._heartbeat_predates_boot("invalid")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: "unavailable")
    assert not pool._heartbeat_predates_boot((now - timedelta(seconds=5)).isoformat())
