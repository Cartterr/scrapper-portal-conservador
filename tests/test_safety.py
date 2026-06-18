from cbrs.safety import StopReason, classify_response, redact


def test_classifies_daily_limit() -> None:
    reason = classify_response(400, {}, {"code": "err-limite"})

    assert reason == StopReason.DAILY_LIMIT


def test_classifies_captcha_rejection() -> None:
    reason = classify_response(400, {}, {"code": "intente-mas-tarde"})

    assert reason == StopReason.CAPTCHA_REJECTED


def test_classifies_temporary_unavailable_message() -> None:
    reason = classify_response(
        400,
        {},
        {
            "type": "error",
            "code": "error",
            "msg": "Problemas obteniendo índice de comercio, intente más tarde.",
        },
    )

    assert reason == StopReason.TEMPORARY_UNAVAILABLE


def test_classifies_waf_and_rate_limit_statuses() -> None:
    assert classify_response(403, {}, "") == StopReason.WAF_CHALLENGE
    assert classify_response(429, {}, "") == StopReason.RATE_LIMIT


def test_classifies_imperva_html() -> None:
    html = "<html><body>Request unsuccessful. Incapsula incident ID</body></html>"

    assert classify_response(200, {}, html) == StopReason.WAF_CHALLENGE


def test_classifies_visible_captcha_html() -> None:
    html = '<html><body><div class="g-recaptcha" data-sitekey="site"></div></body></html>'

    assert classify_response(200, {}, html) == StopReason.CAPTCHA_REJECTED


def test_allows_success_json_from_imperva_cdn() -> None:
    body = {"token": "ok", "refreshToken": None, "user": None}

    assert classify_response(200, {"x-cdn": "Imperva"}, body) is None


def test_classifies_image_endpoint_html() -> None:
    html = b"<html><body>login required</body></html>"

    assert (
        classify_response(200, {"content-type": "text/html"}, html, expected="image")
        == StopReason.UNEXPECTED_HTML
    )


def test_redacts_sensitive_fields_and_text() -> None:
    value = {
        "Authorization": "Bearer eyJaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc",
        "ticket": "secret-ticket",
        "safe": "ok",
    }

    redacted = redact(value)

    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["ticket"] == "[REDACTED]"
    assert redacted["safe"] == "ok"


def test_redacts_fingerprint_arg_from_text() -> None:
    redacted = redact("launch args: --fingerprint=12345")

    assert "--fingerprint=[REDACTED]" in redacted


def test_redacts_proxy_url_field_and_text() -> None:
    value = {
        "cloak_proxy_url": "socks5://user:pass@example.test:1234",
        "message": "proxy=socks5://user:pass@example.test:1234",
    }

    redacted = redact(value)

    assert redacted["cloak_proxy_url"] == "[REDACTED]"
    assert "user:pass" not in redacted["message"]
    assert "socks5://[REDACTED]@example.test:1234" in redacted["message"]


def test_redacts_raw_ip_fields_and_text() -> None:
    value = {
        "raw_ip": "1.2.3.4",
        "message": "egress is 1.2.3.4",
    }

    redacted = redact(value)

    assert redacted["raw_ip"] == "[REDACTED]"
    assert redacted["message"] == "egress is [REDACTED_IP]"


def test_redacts_generic_token_assignments() -> None:
    redacted = redact("blocked with token=secret and refresh_token: abc123")

    assert "secret" not in redacted
    assert "abc123" not in redacted
    assert "token=[REDACTED]" in redacted
    assert "refresh_token: [REDACTED]" in redacted
