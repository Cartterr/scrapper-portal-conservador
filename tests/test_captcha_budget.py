from pathlib import Path

import pytest

from cbrs.captcha_budget import CaptchaBudgetStore


def test_diagnostic_attempt_is_visible_without_secrets(tmp_path: Path) -> None:
    store = CaptchaBudgetStore(
        tmp_path / "captcha.sqlite3",
        daily_limit=60,
        circuit_seconds=300,
    )
    attempt_id = store.record_diagnostic_attempt(
        account_id="a1",
        action="browser_api_manual",
        status="succeeded",
        portal_status="indeterminate",
        portal_error_code="temporary_unavailable:http_400:intente-mas-tarde",
    )

    latest = store.recent_activity()[0]
    assert attempt_id.startswith("captcha-diagnostic-")
    assert latest["kind"] == "solve"
    assert latest["account_id"] == "a1"
    assert latest["action"] == "browser_api_manual"
    assert latest["status"] == "succeeded"
    assert latest["portal_status"] == "indeterminate"
    assert latest["portal_error_code"] == (
        "temporary_unavailable:http_400:intente-mas-tarde"
    )


@pytest.mark.parametrize("field", ["status", "portal_status"])
def test_diagnostic_attempt_rejects_unknown_states(tmp_path: Path, field: str) -> None:
    store = CaptchaBudgetStore(
        tmp_path / "captcha.sqlite3",
        daily_limit=60,
        circuit_seconds=300,
    )
    values = {
        "account_id": "a1",
        "action": "probe",
        "status": "succeeded",
        "portal_status": "not_submitted",
    }
    values[field] = "private-invalid-state"

    with pytest.raises(ValueError):
        store.record_diagnostic_attempt(**values)
