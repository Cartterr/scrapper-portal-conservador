import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location("acceptance_monitor", Path(__file__).resolve().parents[1] / "deploy/acceptance_monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def samples():
    return [{"at": i * 60, "rss_bytes": 100 * 1024**2, "chrome_orphans": 0, "boot_id": "fixture",
             "services": {"owner": {"pid": 1, "active": "active"}, "worker": {"pid": 2, "active": "active"}}}
            for i in range(1441)]


def test_new_boot_archives_continuity_without_losing_jobs(tmp_path):
    from types import SimpleNamespace
    import pytest
    boot = Path('/proc/sys/kernel/random/boot_id')
    if not boot.exists():
        pytest.skip('Linux boot evidence')
    monitor.atomic_json(tmp_path / 'run.json', {'started_at': 1, 'boot_id': 'previous', 'target_hours': 24})
    monitor.atomic_json(tmp_path / 'evidence.json', {'primary_job': {'id': 'preserved'}})
    monitor.atomic_json(tmp_path / 'samples.jsonl', {'at': 1})
    runner = monitor.Runner(SimpleNamespace(state=tmp_path))
    assert runner.run['boot_id'] == boot.read_text().strip()
    assert runner.run['started_at'] > 1
    assert runner.evidence['primary_job']['id'] == 'preserved'
    assert runner.samples == []
    assert len(list(tmp_path.glob('samples.before-boot-*'))) == 1
    assert len(list(tmp_path.glob('run.before-boot-*'))) == 1
    again = monitor.Runner(SimpleNamespace(state=tmp_path))
    assert again.run['started_at'] == runner.run['started_at']


def test_memory_needs_full_day_and_continuous_evidence():
    data = samples()
    assert monitor.memory_verdict(data[:100], 0, 24)[0] == "running"
    assert monitor.memory_verdict(data, 0, 24)[0] == "passed"
    assert monitor.memory_verdict(data[:50] + data[60:], 0, 24)[0] == "inconclusive"


def test_memory_growth_orphans_and_process_change_never_pass():
    data = samples()
    for sample in data[-60:]:
        sample["rss_bytes"] = 1024**3
    assert monitor.memory_verdict(data, 0, 24)[0] == "failed"
    data = samples()
    data[-1]["chrome_orphans"] = 1
    assert monitor.memory_verdict(data, 0, 24)[0] == "failed"
    data = samples()
    data[-1]["boot_id"] = "reboot"
    assert monitor.memory_verdict(data, 0, 24)[0] == "inconclusive"


def test_elapsed_time_cannot_approve_functional_or_destructive_tests():
    result = monitor.evaluate({"started_at": 0, "target_hours": 24}, samples(), {})
    assert result["A16"]["status"] == "inconclusive"
    for key in ("A1", "A13", "A15"):
        assert result[key]["status"] == "deferred"
    for key in ("A6", "A12", "A14", "A19", "A20"):
        assert result[key]["status"] == "supported"
    assert result["A3"]["status"] == "pending"


def test_recorded_proofs_and_load_are_required():
    result = monitor.evaluate({"started_at": 0, "target_hours": 24}, samples(), {
        "new_completed_jobs": 1, "idle_observed": True, "A3": {"passed": True},
        "A2": {"binary_valid": True}, "A20": {"passed": True},
    })
    assert result["A16"]["status"] == result["A3"]["status"] == "passed"
    assert result["A2"]["status"] == result["A20"]["status"] == "partial"


def test_reviewed_and_reboot_proofs_are_visible_without_overclaiming():
    evidence = {
        "A15": {"passed": True, "boot_id_changed": True},
        "A17": {"passed": True},
        "A18": {"passed": True, "matches": 0},
    }
    result = monitor.evaluate({"started_at": 0, "target_hours": 24}, samples()[:2], evidence)
    assert result["A15"]["status"] == "passed"
    assert result["A17"]["status"] == "passed"
    assert result["A18"]["status"] == "passed"
    assert result["A12"]["status"] == "supported"
    assert result["A13"]["status"] == "deferred"


def test_dashboard_translates_evidence_statuses():
    assert "RESPALDADO" in monitor.HTML
    assert "APROBADO" in monitor.HTML
    assert "Conclusión acumulada" in monitor.HTML or "aceptación contractual" in monitor.HTML
