import json
import random
from pathlib import Path
from urllib.request import Request, urlopen

from cbrs.config import load_settings
from cbrs.soak import (
    SoakConfig,
    SoakStore,
    SoakTarget,
    choose_target,
    dashboard_status,
    load_soak_config,
    next_interval_seconds,
    run_soak,
    _heartbeat_while_cycle_runs,
)
from cbrs.soak_dashboard import start_dashboard
from cbrs.validation import ValidationRunResult


def test_soak_config_defaults_to_one_safe_query(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)

    config = load_soak_config(settings)

    assert config.interval_min_minutes == 2.0
    assert config.interval_max_minutes == 4.0
    assert config.dashboard_host == "127.0.0.1"
    assert config.dashboard_port == 8765
    assert config.targets == (
        SoakTarget(label="default_safe_query", kind="text", query="BANCO DE CHILE"),
    )


def test_soak_config_loads_allowlisted_targets(tmp_path: Path) -> None:
    settings = load_settings({"CBRS_PROFILE_DIR": ".cbrs/chrome-profile"}, root=tmp_path)
    config_path = tmp_path / ".cbrs" / "soak-config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "interval_min_minutes": 1,
                "interval_max_minutes": 2,
                "targets": [
                    {"label": "safe_text", "query": "BANCO DE CHILE"},
                    {"label": "safe_fna", "foja": 1, "numero": 2, "ano": 2024},
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_soak_config(settings, path=config_path)

    assert [target.label for target in config.targets] == ["safe_text", "safe_fna"]
    assert {choose_target(config, random.Random(i)).label for i in range(20)} <= {
        "safe_text",
        "safe_fna",
    }


def test_next_interval_stays_within_configured_bounds() -> None:
    config = SoakConfig(
        interval_min_minutes=0.5,
        interval_max_minutes=1.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        targets=(SoakTarget(label="safe", kind="text", query="BANCO"),),
    )

    values = [next_interval_seconds(config, random.Random(i)) for i in range(25)]

    assert all(30 <= value <= 60 for value in values)


def test_dry_run_writes_cycles_and_artifacts(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    config = _fast_config()

    result = run_soak(
        settings=settings,
        config=config,
        store=store,
        dry_run=True,
        max_cycles=2,
        rng=random.Random(7),
    )

    status = dashboard_status(store)
    assert result.exit_code == 0
    assert result.status == "completed"
    assert status["stats"]["total_cycles"] == 2
    assert status["stats"]["downloads"] == 2
    assert len(status["artifacts"]) == 2
    assert len({cycle["validation_report_path"] for cycle in status["cycles"]}) == 2
    for artifact in status["artifacts"]:
        assert Path(artifact["artifact_path"]).is_file()


def test_status_counts_only_latest_run(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    config = _fast_config()

    first = run_soak(settings=settings, config=config, store=store, dry_run=True, max_cycles=2)
    second = run_soak(settings=settings, config=config, store=store, dry_run=True, max_cycles=1)

    status = dashboard_status(store)

    assert first.run_id != second.run_id
    assert status["run"]["run_id"] == second.run_id
    assert status["stats"]["total_cycles"] == 1
    assert len(status["artifacts"]) == 1


def test_success_rate_ignores_current_running_cycle(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    config = _fast_config()
    run_id = "run-success-rate"
    store.create_run(run_id=run_id, dry_run=False, config=config, dashboard_url=None)
    store.start_cycle(
        cycle_id="cycle-1",
        run_id=run_id,
        sequence=1,
        target=config.targets[0],
    )
    store.finish_cycle("cycle-1", status="passed", started_at_monotonic=0, result_count=1)
    store.start_cycle(
        cycle_id="cycle-2",
        run_id=run_id,
        sequence=2,
        target=config.targets[0],
    )

    status = dashboard_status(store)

    assert status["stats"]["total_cycles"] == 2
    assert status["stats"]["success_rate"] == 1.0
    assert status["stats"]["consecutive_successes"] == 1


def test_cycle_heartbeat_updates_run_while_cycle_is_active(tmp_path: Path) -> None:
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    config = _fast_config()
    run_id = "run-heartbeat"
    store.create_run(run_id=run_id, dry_run=False, config=config, dashboard_url=None)
    before = store.latest_run()["heartbeat_at"]
    import time

    time.sleep(1.05)

    with _heartbeat_while_cycle_runs(store, run_id, interval_seconds=0.01):
        time.sleep(0.04)

    after = store.latest_run()["heartbeat_at"]
    assert after > before


def test_safety_stop_blocks_future_cycles_and_redacts_error(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")

    def fake_runner(**_: object) -> ValidationRunResult:
        report_path = tmp_path / ".cbrs" / "logs" / "validation-fake.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return ValidationRunResult(
            exit_code=2,
            status="safety_stop",
            report={},
            report_path=report_path,
            preflight_report_path=None,
            safety_stop="waf_challenge",
            error="blocked from 1.2.3.4 with token=secret",
        )

    result = run_soak(
        settings=settings,
        config=_fast_config(),
        store=store,
        max_cycles=5,
        validation_runner=fake_runner,
    )

    status = dashboard_status(store)
    serialized = json.dumps(status)
    assert result.exit_code == 2
    assert result.status == "blocked"
    assert status["stats"]["total_cycles"] == 1
    assert status["status"] == "blocked"
    assert "1.2.3.4" not in serialized
    assert "secret" not in serialized
    assert "[REDACTED_IP]" in serialized


def test_captcha_safety_stop_pauses_and_exposes_alert(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")

    def fake_runner(**_: object) -> ValidationRunResult:
        report_path = tmp_path / ".cbrs" / "logs" / "validation-captcha.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return ValidationRunResult(
            exit_code=2,
            status="safety_stop",
            report={},
            report_path=report_path,
            preflight_report_path=None,
            safety_stop="captcha_rejected",
            error="captcha challenge detected",
        )

    result = run_soak(
        settings=settings,
        config=_fast_config(),
        store=store,
        max_cycles=5,
        validation_runner=fake_runner,
    )

    status = dashboard_status(store)
    assert result.status == "blocked"
    assert status["status"] == "blocked"
    assert status["stats"]["total_cycles"] == 1
    assert status["alert"]["active"] is True
    assert status["alert"]["reason"] == "captcha_rejected"
    assert "CAPTCHA" in status["alert"]["title"]
    assert "pausada" in status["alert"]["message"]


def test_stop_request_stops_runner_after_current_safe_point(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    config = SoakConfig(
        interval_min_minutes=1,
        interval_max_minutes=1,
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        targets=(SoakTarget(label="safe_text", kind="text", query="BANCO DE CHILE"),),
    )

    def request_stop(_: float) -> None:
        store.request_stop()

    def fake_runner(**kwargs: object) -> ValidationRunResult:
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / "fake.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        report_path = tmp_path / ".cbrs" / "logs" / "validation-stop-test.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return ValidationRunResult(
            exit_code=0,
            status="passed",
            report={},
            report_path=report_path,
            preflight_report_path=None,
            result_count=1,
            pdf_path=pdf_path,
            pdf_size_bytes=pdf_path.stat().st_size,
        )

    result = run_soak(
        settings=settings,
        config=config,
        store=store,
        dry_run=False,
        max_cycles=None,
        validation_runner=fake_runner,
        sleep_fn=request_stop,
    )

    status = dashboard_status(store)
    assert result.status == "stopped"
    assert status["run"]["status"] == "stopped"
    assert status["stats"]["total_cycles"] == 1


def test_dashboard_status_endpoint_and_artifact_link(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    run_soak(
        settings=settings,
        config=_fast_config(),
        store=store,
        dry_run=True,
        max_cycles=1,
    )
    dashboard = start_dashboard(store, settings=settings, port=0)
    try:
        with urlopen(f"{dashboard.url}/api/status", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        artifact = payload["artifacts"][0]
        with urlopen(f"{dashboard.url}/artifact/{artifact['cycle_id']}", timeout=5) as response:
            content = response.read()
    finally:
        dashboard.stop()

    assert payload["stats"]["total_cycles"] == 1
    assert content.startswith(b"%PDF")


def test_dashboard_root_contains_screenshot_evidence_ui(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    dashboard = start_dashboard(store, settings=settings, port=0)
    try:
        with urlopen(f"{dashboard.url}/", timeout=5) as response:
            html = response.read().decode("utf-8")
    finally:
        dashboard.stop()

    assert "Evidencia actual" in html
    assert "iconLibrary" in html
    assert "Resumen de resultados" in html
    assert "Línea de ciclos" in html
    assert "Estado en vivo" in html
    assert "Próximo ciclo en" in html
    assert "liveNextCountdown" in html
    assert "updateStatusBadges" in html
    assert "en espera ·" in html
    assert "waitProgressBar" in html
    assert "safetyAlert" in html
    assert "Parada crítica de seguridad" in html
    assert "donut" in html
    assert "sparkline" in html


def test_dashboard_stop_endpoint_requests_stop(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "CBRS_PROFILE_DIR": ".cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "outputs",
        },
        root=tmp_path,
    )
    store = SoakStore(tmp_path / ".cbrs" / "soak" / "soak.sqlite3")
    dashboard = start_dashboard(store, settings=settings, port=0)
    try:
        request = Request(f"{dashboard.url}/api/stop", method="POST")
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        dashboard.stop()

    assert payload == {"ok": True, "status": "stop_requested"}
    assert store.stop_requested() is True


def _fast_config() -> SoakConfig:
    return SoakConfig(
        interval_min_minutes=0,
        interval_max_minutes=0,
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        targets=(
            SoakTarget(label="safe_text", kind="text", query="BANCO DE CHILE"),
        ),
    )
