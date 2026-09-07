import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from cbrs.runtime_updates import (MODULES, CONTRACTS, RuntimeUpdates, publish,
                                  request, runtime_module, read_status)


def sources(tmp_path, version=1):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "stable.py").write_text("ABI = 1\n")
    for name in MODULES:
        (source / f"{name}.py").write_text(
            f"def {CONTRACTS[name]}(*args, **kwargs):\n    return {version}\n")
    with (source / "runtime_observation.py").open("a") as f:
        f.write("\ndef preview_interval(configured):\n    return 5\n")
    return source


def test_activation_is_explicit_between_operations_and_rollback_preserves_owner(tmp_path):
    source = sources(tmp_path)
    root = tmp_path / "runtime"
    owner = SimpleNamespace(browser=object(), cookies=object(), proxy=object())
    original = vars(owner).copy()
    manager = RuntimeUpdates(source, root, owner="one-owner")
    try:
        v1 = publish(source, root)
        assert manager.release == "builtin"  # publication cannot interrupt work
        assert read_status(root)["pending"]
        assert manager.poll()
        first = runtime_module("runtime_logic")
        sources(tmp_path, 2)
        v2 = publish(source, root)
        assert first.process_job() == 1
        assert runtime_module("runtime_logic").process_job() == 1
        assert manager.poll()
        assert runtime_module("runtime_logic").process_job() == 2
        request(root, v1)
        assert manager.poll()
        assert runtime_module("runtime_logic").process_job() == 1
        assert vars(owner) == original
        assert read_status(root)["previous_release"] == v2
        request(root, "builtin")
        assert manager.poll()
        assert runtime_module("runtime_logic").__name__ == "cbrs.runtime_logic"
    finally:
        manager.close()


@pytest.mark.parametrize("failure", ["tamper", "core", "load"])
def test_rejected_release_keeps_last_good_generation(tmp_path, failure):
    source = sources(tmp_path)
    root = tmp_path / "runtime"
    manager = RuntimeUpdates(source, root, owner="one-owner")
    try:
        first = publish(source, root)
        manager.poll()
        sources(tmp_path, 2)
        if failure == "core":
            (source / "stable.py").write_text("ABI = 2\n")
        if failure == "load":
            with (source / "runtime_logic.py").open("a") as f:
                f.write("\nraise RuntimeError('sensitive exception must not be logged')\n")
        second = publish(source, root)
        if failure == "tamper":
            (root / "releases" / second / "runtime_logic.py").write_text("raise RuntimeError()")
        assert not manager.poll()
        assert manager.release == first
        assert runtime_module("runtime_logic").process_job() == 1
        assert read_status(root)["state"] == "rejected"
        assert "sensitive" not in (root / "status.json").read_text()
        assert not manager.poll()  # no failed-import retry loop
    finally:
        manager.close()


def test_syntax_failure_does_not_publish_partial_release(tmp_path):
    source = sources(tmp_path)
    root = tmp_path / "runtime"
    first = publish(source, root)
    (source / "form_search.py").write_text("def broken(")
    with pytest.raises(SyntaxError):
        publish(source, root)
    assert json.loads((root / "desired.json").read_text())["release"] == first


def test_invalid_path_is_not_a_release(tmp_path):
    with pytest.raises(ValueError):
        request(tmp_path, "../credentials")


def test_real_release_loads_all_components_with_shared_safety_identity(tmp_path):
    source = Path(__file__).resolve().parents[1] / "cbrs"
    manager = RuntimeUpdates(source, tmp_path, owner="integration-owner")
    try:
        publish(source, tmp_path)
        assert manager.poll(), read_status(tmp_path)
        from cbrs.safety import SafetyStopException
        assert runtime_module("form_search").SafetyStopException is SafetyStopException
        from cbrs import jobs
        assert runtime_module("runtime_logic").core is jobs
        assert runtime_module("runtime_observation").CommerceAuthState is jobs.CommerceAuthState
    finally:
        manager.close()


def test_real_chrome_context_and_storage_survive_release_and_rollback(tmp_path):
    from playwright.sync_api import sync_playwright
    repo = Path(__file__).resolve().parents[1] / "cbrs"
    source = tmp_path / "source"
    shutil.copytree(repo, source, ignore=shutil.ignore_patterns("__pycache__"))
    policy = source / "runtime_observation.py"
    import re
    policy.write_text(re.sub(r"PREVIEW_INTERVAL_SECONDS = [0-9.]+", "PREVIEW_INTERVAL_SECONDS = 5.0", policy.read_text(encoding="utf-8")), encoding="utf-8")
    root = tmp_path / "releases-state"
    manager = RuntimeUpdates(source, root, owner="native-test-owner")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.route("**/*", lambda route: route.fulfill(body="<h1>Protected test session</h1>", content_type="text/html"))
            page.goto("http://127.0.0.1:19999/protected")
            page.evaluate("localStorage.setItem('session-proof', 'preserved')")
            context.add_cookies([{"name": "test-session", "value": "preserved", "url": page.url}])
            session = browser.new_browser_cdp_session()
            pid = lambda: next(x["id"] for x in session.send("SystemInfo.getProcessInfo")["processInfo"] if x["type"] == "browser")
            original_pid = pid()
            v1 = publish(source, root)
            assert manager.poll()
            observer = source / "runtime_observation.py"
            observer.write_text(observer.read_text(encoding="utf-8").replace("PREVIEW_INTERVAL_SECONDS = 5.0", "PREVIEW_INTERVAL_SECONDS = 2.0"), encoding="utf-8")
            publish(source, root)
            assert runtime_module("runtime_observation").preview_interval(5) == 5
            assert manager.poll()
            assert runtime_module("runtime_observation").preview_interval(5) == 2
            request(root, v1)
            assert manager.poll()
            assert runtime_module("runtime_observation").preview_interval(5) == 5
            assert pid() == original_pid
            assert not page.is_closed()
            assert page.evaluate("localStorage.getItem('session-proof')") == "preserved"
            assert context.cookies()[0]["value"] == "preserved"
            browser.close()  # only our disposable test browser, never production
    finally:
        manager.close()
