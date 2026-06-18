from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import SETTINGS, Settings
from .safety import redact
from .soak import SoakStore, dashboard_status


@dataclass(frozen=True)
class DashboardHandle:
    url: str
    server: ThreadingHTTPServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_dashboard(
    store: SoakStore,
    *,
    settings: Settings = SETTINGS,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> DashboardHandle:
    handler = _handler_factory(store, settings)
    server = ThreadingHTTPServer((host, port), handler)
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return DashboardHandle(
        url=f"http://{actual_host}:{actual_port}",
        server=server,
        thread=thread,
    )


def _handler_factory(store: SoakStore, settings: Settings):
    class SoakDashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(_dashboard_html())
                return
            if parsed.path == "/api/health":
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/status":
                payload = dashboard_status(store)
                payload["runtime"] = _runtime_summary(settings)
                self._send_json(payload)
                return
            if parsed.path == "/api/cycles":
                limit = _limit(parsed.query)
                run_id = _latest_run_id(store)
                self._send_json(
                    {"cycles": store.recent_cycles(run_id=run_id, limit=limit)}
                )
                return
            if parsed.path == "/api/artifacts":
                run_id = _latest_run_id(store)
                self._send_json(
                    {"artifacts": _with_artifact_urls(store.artifacts(run_id=run_id))}
                )
                return
            if parsed.path == "/api/events":
                limit = _limit(parsed.query)
                run_id = _latest_run_id(store)
                self._send_json(
                    {"events": store.recent_events(run_id=run_id, limit=limit)}
                )
                return
            if parsed.path.startswith("/artifact/"):
                cycle_id = parsed.path.rsplit("/", 1)[-1]
                self._send_artifact(cycle_id)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/stop":
                store.request_stop()
                self._send_json({"ok": True, "status": "stop_requested"})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def _send_artifact(self, cycle_id: str) -> None:
            match = None
            for artifact in store.artifacts(limit=1000):
                if artifact.get("cycle_id") == cycle_id:
                    match = artifact
                    break
            if not match or not match.get("artifact_path"):
                self.send_error(HTTPStatus.NOT_FOUND, "artifact not found")
                return
            path = Path(str(match["artifact_path"])).resolve()
            output_root = (settings.output_dir / "soak").resolve()
            if not path.exists() or not path.is_file() or not path.is_relative_to(output_root):
                self.send_error(HTTPStatus.NOT_FOUND, "artifact not available")
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            content = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.end_headers()
            self.wfile.write(content)

        def _send_html(self, html: str) -> None:
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(redact(payload), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return SoakDashboardHandler


def _with_artifact_urls(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for artifact in artifacts:
        item = dict(artifact)
        item["artifact_url"] = f"/artifact/{item['cycle_id']}"
        enriched.append(item)
    return enriched


def _latest_run_id(store: SoakStore) -> str | None:
    run = store.latest_run()
    return str(run["run_id"]) if run else None


def _limit(query: str) -> int:
    raw = parse_qs(query).get("limit", ["100"])[0]
    try:
        return max(1, min(int(raw), 1000))
    except ValueError:
        return 100


def _runtime_summary(settings: Settings) -> dict[str, Any]:
    return {
        "browser_backend": settings.browser_backend,
        "browser_headless": settings.headless,
        "browser_window_mode": settings.window_mode,
        "expected_egress_country": settings.expected_egress_country,
        "request_delay_seconds": settings.request_delay_seconds,
    }


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CBRS Soak Monitor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef3f8;
      --ink: #111827;
      --muted: #667085;
      --line: #d7dee8;
      --panel: #ffffff;
      --panel-2: #f8fafc;
      --ok: #11845b;
      --ok-soft: #e7f7ef;
      --warn: #b76e00;
      --warn-soft: #fff4de;
      --bad: #b42318;
      --bad-soft: #fff1f0;
      --accent: #1d4ed8;
      --accent-soft: #e8efff;
      --cyan: #0891b2;
      --shadow: 0 18px 50px rgba(31, 41, 55, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      padding: 16px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .actions { display: flex; gap: 10px; align-items: center; }
    button {
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      cursor: pointer;
      font: inherit;
      font-weight: 700;
      padding: 8px 12px;
    }
    button.primary { background: var(--bad); border-color: var(--bad); color: #fff; }
    button:disabled { cursor: not-allowed; opacity: .45; }
    h1 { margin: 0; font-size: 21px; letter-spacing: 0; }
    main { padding: 20px 24px 32px; max-width: 1320px; margin: 0 auto; }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      border-radius: 6px;
      background: #edf2ff;
      color: var(--accent);
      font-weight: 700;
      text-transform: uppercase;
      font-size: 12px;
    }
    .status.running, .status.completed { background: var(--ok-soft); color: var(--ok); }
    .status.blocked, .status.stale { background: var(--bad-soft); color: var(--bad); }
    .status.waiting { background: var(--warn-soft); color: var(--warn); }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(340px, .85fr);
      gap: 16px;
      margin-bottom: 16px;
    }
    .snapshot {
      background:
        radial-gradient(circle at 92% 8%, rgba(8, 145, 178, .16), transparent 28%),
        linear-gradient(135deg, #ffffff 0%, #f7fbff 54%, #eef6ff 100%);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 20px;
      min-height: 230px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      overflow: hidden;
    }
    .snapshot-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
    }
    .snapshot-title { color: var(--muted); font-size: 12px; font-weight: 750; text-transform: uppercase; }
    .snapshot h2 {
      margin: 10px 0 8px;
      font-size: clamp(30px, 4vw, 48px);
      line-height: 1.02;
      letter-spacing: 0;
      max-width: 850px;
    }
    .snapshot-subtitle { color: var(--muted); max-width: 720px; font-size: 15px; line-height: 1.45; }
    .proof-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 18px;
    }
    .proof-item {
      background: rgba(255,255,255,.72);
      border: 1px solid rgba(215,222,232,.9);
      border-radius: 8px;
      padding: 10px;
      min-width: 0;
    }
    .proof-item .label, .metric .label { color: var(--muted); font-size: 12px; font-weight: 650; }
    .proof-item .value { font-size: 14px; font-weight: 760; margin-top: 4px; overflow-wrap: anywhere; }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric, section, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      box-shadow: 0 8px 28px rgba(31, 41, 55, .04);
    }
    .metric {
      display: grid;
      gap: 10px;
      min-height: 128px;
    }
    .metric-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .metric .value { font-size: 27px; font-weight: 800; margin-top: 5px; letter-spacing: 0; }
    .metric-note { color: var(--muted); font-size: 12px; min-height: 18px; }
    .icon {
      width: 34px;
      height: 34px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: var(--accent);
      background: var(--accent-soft);
      flex: 0 0 auto;
    }
    .icon.ok { color: var(--ok); background: var(--ok-soft); }
    .icon.warn { color: var(--warn); background: var(--warn-soft); }
    .icon.bad { color: var(--bad); background: var(--bad-soft); }
    .icon svg { width: 19px; height: 19px; stroke-width: 2.15; }
    .visual-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, .85fr);
      gap: 12px;
      margin-bottom: 18px;
    }
    .chart-shell {
      min-height: 260px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .chart-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 2px;
    }
    .chart-title h2 { margin: 0; }
    .chart-caption { color: var(--muted); font-size: 12px; }
    .sparkline {
      width: 100%;
      height: 168px;
      background: linear-gradient(180deg, #f8fbff 0%, #ffffff 100%);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .sparkline text { fill: var(--muted); font-size: 11px; }
    .outcome-bars { display: grid; gap: 10px; }
    .bar-row {
      display: grid;
      grid-template-columns: 80px minmax(0, 1fr) 48px;
      gap: 9px;
      align-items: center;
      font-size: 13px;
    }
    .bar-track {
      height: 10px;
      background: #eef2f7;
      border-radius: 99px;
      overflow: hidden;
    }
    .bar-fill { height: 100%; min-width: 2px; border-radius: 99px; }
    .bar-fill.ok { background: var(--ok); }
    .bar-fill.warn { background: var(--warn); }
    .bar-fill.bad { background: var(--bad); }
    .donut-wrap {
      display: grid;
      grid-template-columns: 136px minmax(0, 1fr);
      gap: 14px;
      align-items: center;
    }
    .donut {
      width: 132px;
      height: 132px;
      border-radius: 50%;
      background: conic-gradient(var(--ok) 0deg, var(--ok) var(--ok-deg), var(--bad) var(--ok-deg), var(--bad) var(--bad-deg), var(--warn) var(--bad-deg), var(--warn) 360deg);
      position: relative;
      box-shadow: inset 0 0 0 1px rgba(0,0,0,.04);
    }
    .donut::after {
      content: "";
      position: absolute;
      inset: 18px;
      border-radius: 50%;
      background: var(--panel);
      border: 1px solid var(--line);
    }
    .donut-center {
      position: absolute;
      inset: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      z-index: 1;
      font-weight: 800;
    }
    .legend { display: grid; gap: 8px; font-size: 13px; }
    .legend-item { display: flex; justify-content: space-between; gap: 10px; }
    .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-right: 7px; }
    .dot.ok { background: var(--ok); }
    .dot.warn { background: var(--warn); }
    .dot.bad { background: var(--bad); }
    section { margin-bottom: 18px; }
    h2 { margin: 0 0 10px; font-size: 16px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 650; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .muted { color: var(--muted); }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 999px;
      font-weight: 750;
      font-size: 12px;
      background: var(--accent-soft);
      color: var(--accent);
    }
    .badge.passed { background: var(--ok-soft); color: var(--ok); }
    .badge.blocked, .badge.failed { background: var(--bad-soft); color: var(--bad); }
    .badge.running, .badge.waiting { background: var(--warn-soft); color: var(--warn); }
    .empty {
      min-height: 110px;
      display: grid;
      place-items: center;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--panel-2);
      text-align: center;
      padding: 16px;
    }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .hero, .visual-grid { grid-template-columns: 1fr; }
      .kpi-grid, .proof-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      main { padding: 14px; }
      .kpi-grid, .proof-strip { grid-template-columns: 1fr; }
      .donut-wrap { grid-template-columns: 1fr; }
      table { display: block; overflow-x: auto; white-space: nowrap; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>CBRS Soak Monitor</h1>
      <div class="muted">Local read-only runtime dashboard</div>
    </div>
    <div class="actions">
      <button id="stopButton" class="primary" disabled>Stop</button>
      <div id="status" class="status">loading</div>
    </div>
  </header>
  <main>
    <div class="hero">
      <section class="snapshot">
        <div class="snapshot-top">
          <div>
            <div class="snapshot-title">Evidence Snapshot</div>
            <h2 id="headline">Loading current test evidence</h2>
            <div id="headlineSub" class="snapshot-subtitle">The dashboard is collecting the latest run state.</div>
          </div>
          <span id="heroStatus" class="status">loading</span>
        </div>
        <div class="proof-strip">
          <div class="proof-item"><div class="label">Run id</div><div id="runId" class="value">-</div></div>
          <div class="proof-item"><div class="label">Started</div><div id="startedAt" class="value">-</div></div>
          <div class="proof-item"><div class="label">Last cycle</div><div id="lastCycle" class="value">-</div></div>
          <div class="proof-item"><div class="label">Next cycle</div><div id="nextCycleProof" class="value">-</div></div>
        </div>
      </section>

      <section class="panel chart-shell">
        <div class="chart-title">
          <h2>Outcome Mix</h2>
          <span id="cycleSample" class="chart-caption">-</span>
        </div>
        <div class="donut-wrap">
          <div id="donut" class="donut" style="--ok-deg: 0deg; --bad-deg: 0deg;">
            <div class="donut-center"><span id="donutValue">-</span><span class="muted">pass</span></div>
          </div>
          <div id="legend" class="legend"></div>
        </div>
        <div id="outcomeBars" class="outcome-bars"></div>
      </section>
    </div>

    <div class="kpi-grid">
      <div class="metric">
        <div class="metric-head"><div><div class="label">Alive</div><div id="alive" class="value">-</div></div><span id="aliveIcon" class="icon"></span></div>
        <div id="aliveNote" class="metric-note">Runtime heartbeat</div>
      </div>
      <div class="metric">
        <div class="metric-head"><div><div class="label">Success Rate</div><div id="success" class="value">-</div></div><span id="successIcon" class="icon ok"></span></div>
        <div id="successNote" class="metric-note">Passed cycles over latest run</div>
      </div>
      <div class="metric">
        <div class="metric-head"><div><div class="label">PDF Outputs</div><div id="downloads" class="value">-</div></div><span id="downloadIcon" class="icon"></span></div>
        <div id="downloadsNote" class="metric-note">Generated files available below</div>
      </div>
      <div class="metric">
        <div class="metric-head"><div><div class="label">Safety Stops</div><div id="safetyStops" class="value">-</div></div><span id="shieldIcon" class="icon ok"></span></div>
        <div id="safetyNote" class="metric-note">Hard stops remain visible</div>
      </div>
    </div>

    <div class="visual-grid">
      <section class="chart-shell">
        <div class="chart-title">
          <h2>Cycle Timeline</h2>
          <span class="chart-caption">latest run, oldest to newest</span>
        </div>
        <div id="sparkline" class="sparkline"></div>
      </section>

      <section>
        <h2>Runtime Proof</h2>
        <div id="runtime" class="muted">Loading...</div>
      </section>
    </div>

    <section>
      <h2>Cycles</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>Status</th><th>Target</th><th>Results</th><th>PDF</th><th>Report</th><th>Stop</th><th>Finished</th>
          </tr>
        </thead>
        <tbody id="cycles"></tbody>
      </table>
    </section>

    <section>
      <h2>Events</h2>
      <table>
        <thead><tr><th>Time</th><th>Level</th><th>Message</th></tr></thead>
        <tbody id="events"></tbody>
      </table>
    </section>
  </main>
  <script>
    const iconLibrary = {
      activity: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>',
      check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M20 6 9 17l-5-5"/></svg>',
      clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
      download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>',
      shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3 5 6v5c0 5 3 8 7 10 4-2 7-5 7-10V6l-7-3Z"/><path d="m9 12 2 2 4-5"/></svg>',
      alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 4.6 2.7 18a2 2 0 0 0 1.7 3h15.2a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0Z"/></svg>',
      file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6"/></svg>',
      graph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 19V5"/><path d="M4 19h16"/><path d="m7 15 4-4 3 3 5-7"/></svg>'
    };
    const fmtSeconds = (value) => {
      if (value === null || value === undefined) return "-";
      const seconds = Math.max(0, Math.floor(value));
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = seconds % 60;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s}s`;
      return `${s}s`;
    };
    const localTime = (value) => value ? new Date(value).toLocaleString() : "-";
    const fileName = (path) => path ? path.split(/[\\\\/]/).pop() : "";
    const statusBadge = (status) => `<span class="badge ${status || ""}">${status || "unknown"}</span>`;
    const pct = (value) => value === null || value === undefined ? "-" : `${Math.round(value * 100)}%`;
    const latestCycle = (cycles) => (cycles || [])[0] || null;
    const statusCounts = (cycles) => {
      const counts = { passed: 0, failed: 0, blocked: 0, running: 0, waiting: 0 };
      for (const cycle of cycles || []) counts[cycle.status] = (counts[cycle.status] || 0) + 1;
      return counts;
    };
    const icon = (name) => iconLibrary[name] || "";
    const healthTone = (data) => {
      if (data.status === "blocked" || data.status === "stale") return "bad";
      if (data.status === "waiting") return "warn";
      return "ok";
    };
    const screenshotHeadline = (data) => {
      const stats = data.stats || {};
      const cycles = stats.total_cycles || 0;
      if (!data.run) return "Ready for the first long-term run";
      if (data.status === "blocked") return "Run blocked by a safety stop";
      if (data.status === "stale") return "Dashboard has not seen a fresh heartbeat";
      if (cycles === 0) return "Ready for the first cycle";
      if ((stats.safety_stops || 0) === 0 && ["running", "waiting"].includes(data.status)) return `Behaving normally for ${fmtSeconds(stats.uptime_seconds)}`;
      if ((stats.safety_stops || 0) === 0 && data.status === "completed") return "Last run completed cleanly";
      return `Completed ${cycles} cycles with visible safety evidence`;
    };
    const renderDonut = (stats, cycles) => {
      const counts = statusCounts(cycles);
      const total = Math.max(1, (cycles || []).length);
      const passDeg = Math.round((counts.passed / total) * 360);
      const badDeg = passDeg + Math.round(((counts.failed + counts.blocked) / total) * 360);
      const donut = document.getElementById("donut");
      donut.style.setProperty("--ok-deg", `${passDeg}deg`);
      donut.style.setProperty("--bad-deg", `${badDeg}deg`);
      document.getElementById("donutValue").textContent = pct(stats.success_rate);
      document.getElementById("legend").innerHTML = [
        `<div class="legend-item"><span><span class="dot ok"></span>Passed</span><strong>${counts.passed}</strong></div>`,
        `<div class="legend-item"><span><span class="dot bad"></span>Failed/blocked</span><strong>${counts.failed + counts.blocked}</strong></div>`,
        `<div class="legend-item"><span><span class="dot warn"></span>Running/waiting</span><strong>${counts.running + counts.waiting}</strong></div>`
      ].join("");
      document.getElementById("cycleSample").textContent = `${(cycles || []).length} cycle sample`;
      const max = Math.max(1, ...Object.values(counts));
      const bars = [
        ["Passed", counts.passed, "ok"],
        ["Failed", counts.failed, "bad"],
        ["Blocked", counts.blocked, "bad"],
        ["In flight", counts.running + counts.waiting, "warn"]
      ];
      document.getElementById("outcomeBars").innerHTML = bars.map(([label, count, tone]) => `
        <div class="bar-row"><span>${label}</span><div class="bar-track"><div class="bar-fill ${tone}" style="width:${Math.max(4, count / max * 100)}%"></div></div><strong>${count}</strong></div>
      `).join("");
    };
    const renderSparkline = (cycles) => {
      const shell = document.getElementById("sparkline");
      const ordered = [...(cycles || [])].reverse().slice(-32);
      if (!ordered.length) {
        shell.innerHTML = '<div class="empty">No cycle history yet. Start the soak run to populate this chart.</div>';
        return;
      }
      const width = 720, height = 168, pad = 24;
      const step = ordered.length > 1 ? (width - pad * 2) / (ordered.length - 1) : 0;
      const yFor = (status) => status === "passed" ? 56 : status === "running" || status === "waiting" ? 92 : 124;
      const points = ordered.map((cycle, index) => [pad + step * index, yFor(cycle.status), cycle]);
      const path = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
      shell.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="100%" role="img" aria-label="Cycle outcome trend">
          <path d="M${pad} 56 H${width-pad}" stroke="#dbe4ef" stroke-width="1"/>
          <path d="M${pad} 92 H${width-pad}" stroke="#dbe4ef" stroke-width="1"/>
          <path d="M${pad} 124 H${width-pad}" stroke="#dbe4ef" stroke-width="1"/>
          <text x="${pad}" y="45">passed</text>
          <text x="${pad}" y="84">active</text>
          <text x="${pad}" y="116">stopped</text>
          <path d="${path}" fill="none" stroke="#1d4ed8" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          ${points.map(([x, y, cycle]) => {
            const color = cycle.status === "passed" ? "#11845b" : cycle.status === "running" || cycle.status === "waiting" ? "#b76e00" : "#b42318";
            return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5" fill="${color}" stroke="#fff" stroke-width="2"><title>Cycle ${cycle.sequence}: ${cycle.status}</title></circle>`;
          }).join("")}
        </svg>`;
    };
    async function requestStop() {
      if (!confirm("Request the soak runner to stop after the current safe point?")) return;
      await fetch("/api/stop", { method: "POST" });
      await refresh();
    }
    async function refresh() {
      const response = await fetch("/api/status", { cache: "no-store" });
      const data = await response.json();
      const status = document.getElementById("status");
      status.textContent = data.status || "unknown";
      status.className = `status ${data.status || ""}`;
      const heroStatus = document.getElementById("heroStatus");
      heroStatus.textContent = data.status || "unknown";
      heroStatus.className = `status ${data.status || ""}`;
      const stats = data.stats || {};
      const canStop = ["running", "waiting", "stale"].includes(data.status);
      const stopButton = document.getElementById("stopButton");
      stopButton.disabled = !canStop;
      document.getElementById("alive").textContent = fmtSeconds(stats.uptime_seconds);
      document.getElementById("success").textContent = pct(stats.success_rate);
      document.getElementById("downloads").textContent = stats.downloads ?? "-";
      document.getElementById("safetyStops").textContent = stats.safety_stops ?? "-";
      document.getElementById("headline").textContent = screenshotHeadline(data);
      document.getElementById("headlineSub").textContent = data.run
        ? `Latest run has ${stats.total_cycles || 0} cycle(s), ${stats.downloads || 0} PDF output(s), and ${stats.safety_stops || 0} safety stop(s).`
        : "Open the dashboard now, then start the soak flow when you are ready.";
      document.getElementById("runId").textContent = data.run ? data.run.run_id : "-";
      document.getElementById("startedAt").textContent = data.run ? localTime(data.run.started_at) : "-";
      const last = latestCycle(data.cycles);
      document.getElementById("lastCycle").innerHTML = last ? `${statusBadge(last.status)} <span class="muted">#${last.sequence}</span>` : "-";
      document.getElementById("nextCycleProof").textContent = data.run && data.run.next_cycle_at ? localTime(data.run.next_cycle_at) : "-";
      const tone = healthTone(data);
      document.getElementById("aliveIcon").className = `icon ${tone}`;
      document.getElementById("aliveIcon").innerHTML = icon("clock");
      document.getElementById("successIcon").innerHTML = icon("check");
      document.getElementById("downloadIcon").innerHTML = icon("download");
      document.getElementById("shieldIcon").className = `icon ${(stats.safety_stops || 0) ? "bad" : "ok"}`;
      document.getElementById("shieldIcon").innerHTML = (stats.safety_stops || 0) ? icon("alert") : icon("shield");
      document.getElementById("aliveNote").textContent = `Heartbeat age ${fmtSeconds(stats.heartbeat_age_seconds)}`;
      document.getElementById("successNote").textContent = `${stats.passed_cycles || 0} passed / ${stats.total_cycles || 0} total`;
      document.getElementById("downloadsNote").textContent = `${stats.downloads || 0} artifact link(s) in latest run`;
      document.getElementById("safetyNote").textContent = (stats.safety_stops || 0) ? "Review stop reason before continuing" : "No hard stops in latest run";
      renderDonut(stats, data.cycles || []);
      renderSparkline(data.cycles || []);
      const runtime = data.runtime || {};
      const run = data.run || {};
      document.getElementById("runtime").innerHTML = [
        `backend=<code>${runtime.browser_backend || "-"}</code>`,
        `window=<code>${runtime.browser_window_mode || "-"}</code>`,
        `headless=<code>${runtime.browser_headless}</code>`,
        `delay=<code>${runtime.request_delay_seconds || "-"}s</code>`,
        `expected country=<code>${runtime.expected_egress_country || "-"}</code>`,
        `next cycle=<code>${run.next_cycle_at ? localTime(run.next_cycle_at) : "-"}</code>`,
        `blocked reason=<code>${run.blocked_reason || "-"}</code>`
      ].map((item) => `<div style="margin-bottom:8px">${item}</div>`).join("");
      document.getElementById("cycles").innerHTML = (data.cycles || []).map((cycle) => {
        const artifact = cycle.artifact_path ? `<a href="/artifact/${cycle.cycle_id}" target="_blank">${fileName(cycle.artifact_path)}</a>` : "";
        const report = cycle.validation_report_path ? `<code>${fileName(cycle.validation_report_path)}</code>` : "";
        return `<tr>
          <td>${cycle.sequence}</td>
          <td>${statusBadge(cycle.status)}</td>
          <td>${cycle.target_label}</td>
          <td>${cycle.result_count ?? ""}</td>
          <td>${artifact}</td>
          <td>${report}</td>
          <td>${cycle.safety_stop || cycle.error || ""}</td>
          <td>${localTime(cycle.finished_at)}</td>
        </tr>`;
      }).join("");
      document.getElementById("events").innerHTML = (data.events || []).slice(0, 25).map((event) => `<tr>
        <td>${localTime(event.created_at)}</td>
        <td>${event.level}</td>
        <td>${event.message}</td>
      </tr>`).join("");
    }
    document.getElementById("stopButton").addEventListener("click", requestStop);
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""
