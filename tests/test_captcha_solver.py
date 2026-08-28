from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from cbrs.captcha_solver import (
    CREATE_TASK_URL,
    GET_BALANCE_URL,
    GET_TASK_RESULT_URL,
    TwoCaptchaClient,
    TwoCaptchaError,
    TwoCaptchaResult,
)


def test_two_captcha_builds_proxyless_enterprise_v3_task() -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []
    responses = iter(
        [
            {"errorId": 0, "taskId": 123},
            {
                "errorId": 0,
                "status": "ready",
                "solution": {"gRecaptchaResponse": "solver-token"},
            },
        ]
    )

    def request_json(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append((url, payload))
        return next(responses)

    client = TwoCaptchaClient(
        "private-key",
        poll_seconds=0,
        request_json=request_json,
        sleep_fn=lambda _seconds: None,
    )
    token = client.solve_recaptcha_v3_enterprise(
        website_url="https://portal.example.test/commerce",
        website_key="site-key",
        page_action="indice_com_texto",
        min_score=0.7,
    )

    assert token == "solver-token"
    assert calls[0][0] == CREATE_TASK_URL
    assert calls[1][0] == GET_TASK_RESULT_URL
    task = calls[0][1]["task"]
    assert task == {
        "type": "RecaptchaV3TaskProxyless",
        "websiteURL": "https://portal.example.test/commerce",
        "websiteKey": "site-key",
        "minScore": 0.7,
        "pageAction": "indice_com_texto",
        "isEnterprise": True,
        "apiDomain": "google.com",
    }
    assert not any(str(key).lower().startswith("proxy") for key in task)


def test_two_captcha_error_exposes_only_the_error_code() -> None:
    def request_json(_url: str, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "errorId": 10,
            "errorCode": "ERROR_ZERO_BALANCE",
            "errorDescription": "description containing private-key",
        }

    client = TwoCaptchaClient("private-key", request_json=request_json)
    with pytest.raises(TwoCaptchaError) as error:
        client.solve_recaptcha_v3_enterprise(
            website_url="https://portal.example.test",
            website_key="site-key",
            page_action="login",
            min_score=0.7,
        )

    assert error.value.code == "ERROR_ZERO_BALANCE"
    assert "private-key" not in str(error.value)
    assert "description" not in str(error.value)


def test_two_captcha_balance_check_does_not_create_a_task() -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []

    def request_json(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append((url, payload))
        return {"errorId": 0, "balance": 12.3456}

    client = TwoCaptchaClient("private-key", request_json=request_json)

    assert client.get_balance() == 12.3456
    assert calls == [(GET_BALANCE_URL, {"clientKey": "private-key"})]


def test_solver_result_repr_does_not_expose_solution_token() -> None:
    result = TwoCaptchaResult("solution-secret", cost_usd=0.002)
    assert "solution-secret" not in repr(result)


def test_solver_result_retains_task_id_only_in_memory() -> None:
    responses = iter(
        [
            {"errorId": 0, "taskId": 456},
            {
                "errorId": 0,
                "status": "ready",
                "solution": {"gRecaptchaResponse": "solver-token"},
                "cost": "0.00299",
            },
        ]
    )
    client = TwoCaptchaClient(
        "private-key",
        poll_seconds=0,
        request_json=lambda _url, _payload: next(responses),
        sleep_fn=lambda _seconds: None,
    )

    result = client.solve_recaptcha_v3_enterprise_result(
        website_url="https://portal.example.test/commerce",
        website_key="site-key",
        page_action="indice_com_texto",
        min_score=0.9,
    )

    assert result.token == "solver-token"
    assert result.task_id == 456
    assert result.cost_usd == 0.00299
    assert "456" not in repr(result)
