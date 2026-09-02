from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from cbrs.capsolver import (
    CREATE_TASK_URL,
    ENTERPRISE_V3_COST_USD,
    GET_BALANCE_URL,
    GET_TASK_RESULT_URL,
    CapSolverClient,
    CapSolverError,
    CapSolverResult,
)


def test_capsolver_builds_proxy_bound_enterprise_v3_task() -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []
    responses = iter(
        [
            {"errorId": 0, "taskId": "task-123"},
            {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "gRecaptchaResponse": "solver-token",
                    "userAgent": "browser-agent",
                    "secChUa": '"Chromium";v="140"',
                },
            },
        ]
    )

    def request_json(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append((url, payload))
        return next(responses)

    client = CapSolverClient(
        "private-key",
        poll_seconds=0,
        request_json=request_json,
        sleep_fn=lambda _seconds: None,
    )
    result = client.solve_recaptcha_v3_enterprise_result(
        website_url="https://portal.example.test/commerce",
        website_key="site-key",
        page_action="indice_com_texto",
        min_score=0.9,
        proxy="http://user:pass@proxy.example.test:8080",
        user_agent="browser-agent",
    )

    assert result.token == "solver-token"
    assert result.task_id == "task-123"
    assert result.cost_usd == ENTERPRISE_V3_COST_USD
    assert result.user_agent == "browser-agent"
    assert result.sec_ch_ua == '"Chromium";v="140"'
    assert calls[0][0] == CREATE_TASK_URL
    assert calls[1] == (
        GET_TASK_RESULT_URL,
        {"clientKey": "private-key", "taskId": "task-123"},
    )
    assert calls[0][1]["task"] == {
        "type": "ReCaptchaV3EnterpriseTask",
        "websiteURL": "https://portal.example.test/commerce",
        "websiteKey": "site-key",
        "pageAction": "indice_com_texto",
        "minScore": 0.9,
        "proxy": "http://user:pass@proxy.example.test:8080",
        "userAgent": "browser-agent",
        "apiDomain": "www.google.com",
    }


def test_capsolver_preserves_returned_user_agent_for_browser_alignment() -> None:
    responses = iter(
        [
            {"errorId": 0, "taskId": "task-123"},
            {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "gRecaptchaResponse": "solver-token",
                    "userAgent": "different-agent",
                },
            },
        ]
    )
    client = CapSolverClient(
        "private-key",
        poll_seconds=0,
        request_json=lambda _url, _payload: next(responses),
        sleep_fn=lambda _seconds: None,
    )

    result = client.solve_recaptcha_v3_enterprise_result(
        website_url="https://portal.example.test",
        website_key="site-key",
        page_action="login",
        min_score=0.9,
        proxy="http://user:pass@proxy.example.test:8080",
        user_agent="browser-agent",
    )

    assert result.token == "solver-token"
    assert result.user_agent == "different-agent"


def test_capsolver_error_and_result_repr_are_sanitized() -> None:
    def request_json(_url: str, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "errorId": 1,
            "errorCode": "ERROR_ZERO_BALANCE",
            "errorDescription": "private-key and proxy-password",
        }

    client = CapSolverClient("private-key", request_json=request_json)
    with pytest.raises(CapSolverError) as error:
        client.get_balance()

    assert error.value.code == "ERROR_ZERO_BALANCE"
    assert "private-key" not in str(error.value)
    assert "proxy-password" not in str(error.value)
    result = CapSolverResult("solution-secret", task_id="private-task")
    assert "solution-secret" not in repr(result)
    assert "private-task" not in repr(result)


def test_capsolver_balance_check_does_not_create_task() -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []

    def request_json(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append((url, payload))
        return {"errorId": 0, "balance": 6.0}

    client = CapSolverClient("private-key", request_json=request_json)

    assert client.get_balance() == 6.0
    assert calls == [(GET_BALANCE_URL, {"clientKey": "private-key"})]
