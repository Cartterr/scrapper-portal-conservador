from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .browser_session import BrowserSession
from .config import Settings
from .safety import SafetyStopException, StopReason

SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(password|clave|token|recaptcha|authorization|cookie)=([^&\s]+)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(set-cookie|cookie|authorization)(\s*[:=]\s*)([^\s,;}]+)"),
]
INTERESTING_URL_PARTS = (
    "api/",
    "auth",
    "login",
    "usuario",
    "recaptcha",
    "captcha",
    "google.com",
    "gstatic.com",
    "error",
)


def run_login_debug(
    settings: Settings,
    *,
    timeout_seconds: int | None,
    label: str,
) -> Path:
    log_dir = settings.profile_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"login-debug-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    pending: dict[int, dict[str, Any]] = {}

    def write(event: dict[str, Any]) -> None:
        event.setdefault("ts", _utc_now())
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        if event.get("kind") in {
            "request",
            "response",
            "requestfailed",
            "pending_requests",
            "navigation",
            "console",
            "pageerror",
            "login_cookie",
            "timeout",
        }:
            printable = {key: value for key, value in event.items() if key != "body"}
            print(json.dumps(printable, ensure_ascii=False), flush=True)

    with BrowserSession(settings, headless=False) as browser:
        page = browser.page

        def on_request(request: Any) -> None:
            url = _safe_url(request.url)
            if _interesting_request(request.method, url):
                pending[id(request)] = {
                    "started": time.time(),
                    "method": request.method,
                    "url": url,
                }
                write(
                    {
                        "kind": "request",
                        "method": request.method,
                        "url": url,
                        "resource_type": request.resource_type,
                    }
                )

        def on_response(response: Any) -> None:
            request_id = id(response.request)
            pending.pop(request_id, None)
            url = _safe_url(response.url)
            if _interesting_url(url) or response.status >= 400:
                body = None
                if response.status >= 400 or "auth" in url.lower() or "login" in url.lower():
                    try:
                        body = _redact(response.text())
                    except Exception as exc:  # pragma: no cover - browser dependent
                        body = f"[body unavailable: {type(exc).__name__}]"
                write(
                    {
                        "kind": "response",
                        "status": response.status,
                        "url": url,
                        "body": body,
                    }
                )

        def on_request_failed(request: Any) -> None:
            pending.pop(id(request), None)
            write(
                {
                    "kind": "requestfailed",
                    "method": request.method,
                    "url": _safe_url(request.url),
                    "failure": str(request.failure or "unknown"),
                }
            )

        def on_console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                write({"kind": "console", "type": message.type, "text": _redact(message.text)})

        def on_page_error(exc: Exception) -> None:
            write({"kind": "pageerror", "error": _redact(str(exc))})

        def on_frame_nav(frame: Any) -> None:
            if frame == page.main_frame:
                write({"kind": "navigation", "url": _safe_url(frame.url)})

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("framenavigated", on_frame_nav)

        write({"kind": "start", "account_label": label, "log_path": str(log_path)})
        page.expose_function("__cbrsDebugEvent", write)
        page.add_init_script(_browser_instrumentation_script())
        page.goto(settings.commerce_url, wait_until="domcontentloaded", timeout=60000)

        started = time.time()
        timeout = None if timeout_seconds is None else max(timeout_seconds, 1)
        last_pending_log = 0.0
        while True:
            if browser.has_login_cookie():
                write({"kind": "login_cookie", "status": "detected"})
                return log_path
            elapsed = time.time() - started
            if timeout is not None and elapsed >= timeout:
                write({"kind": "timeout", "seconds": timeout, "current_url": _safe_url(page.url)})
                raise SafetyStopException(
                    StopReason.AUTH_REQUIRED,
                    "Timed out waiting for manual login during login debug.",
                    context="login-debug",
                )
            if time.time() - last_pending_log >= 15:
                _write_pending_requests(write, pending)
                last_pending_log = time.time()
            page.wait_for_timeout(1000)


def _write_pending_requests(write: Any, pending: dict[int, dict[str, Any]]) -> None:
    now = time.time()
    active = []
    for item in pending.values():
        age = round(now - float(item["started"]), 1)
        if age >= 10:
            active.append(
                {
                    "method": item["method"],
                    "url": item["url"],
                    "age_seconds": age,
                }
            )
    if active:
        write({"kind": "pending_requests", "requests": active[-10:]})


def _interesting_request(method: str, url: str) -> bool:
    return method.upper() == "POST" or _interesting_url(url)


def _interesting_url(url: str) -> bool:
    lower = url.lower()
    return any(part in lower for part in INTERESTING_URL_PARTS)


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def _redact(text: str | None) -> str | None:
    if text is None:
        return None
    output = text[:1200]
    for pattern in SENSITIVE_PATTERNS:
        output = pattern.sub(lambda match: f"{match.group(1)}{match.group(2) if match.lastindex and match.lastindex > 2 else ''}[REDACTED]", output)
    return re.sub(r"[A-Za-z0-9_-]{80,}", "[REDACTED_LONG_VALUE]", output)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _browser_instrumentation_script() -> str:
    return r"""
(() => {
  const emit = (event) => {
    try {
      window.__cbrsDebugEvent(Object.assign({ kind: "browser_probe" }, event));
    } catch (_error) {
      // Debug hook is best-effort only.
    }
  };
  const safeUrl = (value) => {
    try {
      const url = new URL(String(value), window.location.href);
      return `${url.protocol}//${url.host}${url.pathname}`;
    } catch (_error) {
      return String(value).slice(0, 180);
    }
  };

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async (...args) => {
      const input = args[0];
      const init = args[1] || {};
      const method = init.method || (input && input.method) || "GET";
      const url = safeUrl(input && input.url ? input.url : input);
      emit({ probe: "fetch_start", method, url });
      try {
        const response = await originalFetch(...args);
        emit({ probe: "fetch_done", method, url, status: response.status });
        return response;
      } catch (error) {
        emit({ probe: "fetch_error", method, url, error: String(error && error.message || error) });
        throw error;
      }
    };
  }

  const OriginalXHR = window.XMLHttpRequest;
  if (typeof OriginalXHR === "function") {
    window.XMLHttpRequest = function XMLHttpRequestProbe() {
      const xhr = new OriginalXHR();
      let method = "GET";
      let url = "";
      const originalOpen = xhr.open;
      xhr.open = function openProbe(nextMethod, nextUrl, ...rest) {
        method = nextMethod || "GET";
        url = safeUrl(nextUrl);
        emit({ probe: "xhr_open", method, url });
        return originalOpen.call(xhr, nextMethod, nextUrl, ...rest);
      };
      xhr.addEventListener("loadend", () => {
        emit({ probe: "xhr_done", method, url, status: xhr.status });
      });
      xhr.addEventListener("error", () => {
        emit({ probe: "xhr_error", method, url, status: xhr.status });
      });
      return xhr;
    };
  }

  const wrapRecaptcha = () => {
    const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
    if (!enterprise || typeof enterprise.execute !== "function" || enterprise.__cbrsWrapped) {
      return false;
    }
    const originalExecute = enterprise.execute.bind(enterprise);
    enterprise.execute = async (...args) => {
      const action = args[1] && args[1].action;
      emit({ probe: "recaptcha_execute_start", action: action || null });
      try {
        const token = await originalExecute(...args);
        emit({ probe: "recaptcha_execute_done", action: action || null, token_present: Boolean(token) });
        return token;
      } catch (error) {
        emit({
          probe: "recaptcha_execute_error",
          action: action || null,
          error: String(error && error.message || error),
        });
        throw error;
      }
    };
    enterprise.__cbrsWrapped = true;
    emit({ probe: "recaptcha_wrapped" });
    return true;
  };
  const recaptchaInterval = setInterval(() => {
    if (wrapRecaptcha()) {
      clearInterval(recaptchaInterval);
    }
  }, 250);
  setTimeout(() => clearInterval(recaptchaInterval), 30000);

  window.addEventListener("click", (event) => {
    const target = event.target;
    const button = target && target.closest ? target.closest("button") : null;
    if (button) {
      emit({
        probe: "button_click",
        text: (button.innerText || button.textContent || "").trim().slice(0, 80),
        disabled: Boolean(button.disabled),
        type: button.getAttribute("type") || null,
      });
    }
  }, true);
  window.addEventListener("submit", (event) => {
    emit({ probe: "form_submit", action: safeUrl(event.target && event.target.action || window.location.href) });
  }, true);
})();
"""
