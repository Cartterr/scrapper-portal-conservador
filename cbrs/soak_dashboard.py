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
      --bg: #f5f7fb;
      --ink: #172033;
      --muted: #647089;
      --line: #d8deea;
      --panel: #ffffff;
      --ok: #147d4f;
      --warn: #a96900;
      --bad: #b42318;
      --accent: #2454d6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
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
    h1 { margin: 0; font-size: 22px; }
    main { padding: 20px 24px 32px; max-width: 1280px; margin: 0 auto; }
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
    .status.blocked, .status.stale { background: #fff1f0; color: var(--bad); }
    .status.waiting { background: #fff8e6; color: var(--warn); }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { font-size: 24px; font-weight: 750; margin-top: 5px; }
    section { margin-bottom: 18px; }
    h2 { margin: 0 0 10px; font-size: 16px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 650; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .muted { color: var(--muted); }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      main { padding: 14px; }
      .grid { grid-template-columns: 1fr; }
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
    <div class="grid">
      <div class="metric"><div class="label">Alive</div><div id="alive" class="value">-</div></div>
      <div class="metric"><div class="label">Next cycle</div><div id="next" class="value">-</div></div>
      <div class="metric"><div class="label">Success rate</div><div id="success" class="value">-</div></div>
      <div class="metric"><div class="label">Downloads</div><div id="downloads" class="value">-</div></div>
    </div>

    <section>
      <h2>Runtime</h2>
      <div id="runtime" class="muted">Loading...</div>
    </section>

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
      const stats = data.stats || {};
      const canStop = ["running", "waiting", "stale"].includes(data.status);
      const stopButton = document.getElementById("stopButton");
      stopButton.disabled = !canStop;
      document.getElementById("alive").textContent = fmtSeconds(stats.uptime_seconds);
      document.getElementById("next").textContent = data.run && data.run.next_cycle_at ? localTime(data.run.next_cycle_at) : "-";
      document.getElementById("success").textContent = stats.success_rate === null || stats.success_rate === undefined ? "-" : `${Math.round(stats.success_rate * 100)}%`;
      document.getElementById("downloads").textContent = stats.downloads ?? "-";
      const runtime = data.runtime || {};
      const run = data.run || {};
      document.getElementById("runtime").innerHTML = [
        `backend=<code>${runtime.browser_backend || "-"}</code>`,
        `window=<code>${runtime.browser_window_mode || "-"}</code>`,
        `headless=<code>${runtime.browser_headless}</code>`,
        `delay=<code>${runtime.request_delay_seconds || "-"}s</code>`,
        `egress=<code>${run.blocked_reason || runtime.expected_egress_country || "-"}</code>`,
        `heartbeat age=<code>${fmtSeconds(stats.heartbeat_age_seconds)}</code>`
      ].join(" &nbsp; ");
      document.getElementById("cycles").innerHTML = (data.cycles || []).map((cycle) => {
        const artifact = cycle.artifact_path ? `<a href="/artifact/${cycle.cycle_id}" target="_blank">${fileName(cycle.artifact_path)}</a>` : "";
        const report = cycle.validation_report_path ? `<code>${fileName(cycle.validation_report_path)}</code>` : "";
        return `<tr>
          <td>${cycle.sequence}</td>
          <td>${cycle.status}</td>
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
