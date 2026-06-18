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
  <title>Monitor de Prueba Continua CBRS</title>
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
    .hidden { display: none !important; }
    .alert-banner {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 14px;
      align-items: flex-start;
      margin-bottom: 16px;
      padding: 18px;
      border: 2px solid var(--bad);
      border-radius: 8px;
      background:
        linear-gradient(135deg, rgba(180, 35, 24, .08), rgba(255, 241, 240, .96)),
        var(--panel);
      box-shadow: 0 18px 50px rgba(180, 35, 24, .16);
    }
    .alert-icon {
      width: 46px;
      height: 46px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--bad);
      color: #fff;
      flex: 0 0 auto;
    }
    .alert-icon svg { width: 25px; height: 25px; stroke-width: 2.25; }
    .alert-kicker {
      color: var(--bad);
      font-size: 12px;
      font-weight: 850;
      letter-spacing: .02em;
      text-transform: uppercase;
    }
    .alert-title {
      margin-top: 4px;
      font-size: clamp(24px, 3vw, 34px);
      line-height: 1.06;
      font-weight: 850;
    }
    .alert-message { margin-top: 7px; color: #681d17; font-size: 15px; line-height: 1.45; }
    .alert-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      margin-top: 12px;
      color: #7a271a;
      font-size: 13px;
    }
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
      <h1>Monitor de Prueba Continua CBRS</h1>
      <div class="muted">Panel local de solo lectura</div>
    </div>
    <div class="actions">
      <button id="stopButton" class="primary" disabled>Detener</button>
      <div id="status" class="status">cargando</div>
    </div>
  </header>
  <main>
    <section id="safetyAlert" class="alert-banner hidden" role="alert" aria-live="polite">
      <div id="safetyAlertIcon" class="alert-icon"></div>
      <div>
        <div class="alert-kicker">Parada crítica de seguridad</div>
        <div id="safetyAlertTitle" class="alert-title">Acción del portal pausada</div>
        <div id="safetyAlertMessage" class="alert-message">La prueba continua se detuvo antes de generar más tráfico al portal.</div>
        <div class="alert-meta">
          <span>Motivo <code id="safetyAlertReason">-</code></span>
          <span>Ciclo <code id="safetyAlertCycle">-</code></span>
        </div>
      </div>
    </section>

    <div class="hero">
      <section class="snapshot">
        <div class="snapshot-top">
          <div>
            <div class="snapshot-title">Evidencia actual</div>
            <h2 id="headline">Cargando evidencia de la prueba</h2>
            <div id="headlineSub" class="snapshot-subtitle">El panel está cargando el estado más reciente.</div>
          </div>
          <span id="heroStatus" class="status">cargando</span>
        </div>
        <div class="proof-strip">
          <div class="proof-item"><div class="label">ID de ejecución</div><div id="runId" class="value">-</div></div>
          <div class="proof-item"><div class="label">Inicio</div><div id="startedAt" class="value">-</div></div>
          <div class="proof-item"><div class="label">Último ciclo</div><div id="lastCycle" class="value">-</div></div>
          <div class="proof-item"><div class="label">Próximo ciclo</div><div id="nextCycleProof" class="value">-</div></div>
        </div>
      </section>

      <section class="panel chart-shell">
        <div class="chart-title">
          <h2>Resumen de resultados</h2>
          <span id="cycleSample" class="chart-caption">-</span>
        </div>
        <div class="donut-wrap">
          <div id="donut" class="donut" style="--ok-deg: 0deg; --bad-deg: 0deg;">
            <div class="donut-center"><span id="donutValue">-</span><span class="muted">éxito</span></div>
          </div>
          <div id="legend" class="legend"></div>
        </div>
        <div id="outcomeBars" class="outcome-bars"></div>
      </section>
    </div>

    <div class="kpi-grid">
      <div class="metric">
        <div class="metric-head"><div><div class="label">Activo</div><div id="alive" class="value">-</div></div><span id="aliveIcon" class="icon"></span></div>
        <div id="aliveNote" class="metric-note">Latido de ejecución</div>
      </div>
      <div class="metric">
        <div class="metric-head"><div><div class="label">Tasa de éxito</div><div id="success" class="value">-</div></div><span id="successIcon" class="icon ok"></span></div>
        <div id="successNote" class="metric-note">Ciclos correctos en la última ejecución</div>
      </div>
      <div class="metric">
        <div class="metric-head"><div><div class="label">PDFs generados</div><div id="downloads" class="value">-</div></div><span id="downloadIcon" class="icon"></span></div>
        <div id="downloadsNote" class="metric-note">Archivos disponibles abajo</div>
      </div>
      <div class="metric">
        <div class="metric-head"><div><div class="label">Paradas de seguridad</div><div id="safetyStops" class="value">-</div></div><span id="shieldIcon" class="icon ok"></span></div>
        <div id="safetyNote" class="metric-note">Las paradas críticas quedan visibles</div>
      </div>
    </div>

    <div class="visual-grid">
      <section class="chart-shell">
        <div class="chart-title">
          <h2>Línea de ciclos</h2>
          <span class="chart-caption">última ejecución, de antiguo a reciente</span>
        </div>
        <div id="sparkline" class="sparkline"></div>
      </section>

      <section>
        <h2>Prueba de ejecución</h2>
        <div id="runtime" class="muted">Cargando...</div>
      </section>
    </div>

    <section>
      <h2>Ciclos</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>Estado</th><th>Objetivo</th><th>Resultados</th><th>PDF</th><th>Reporte</th><th>Parada</th><th>Finalizado</th>
          </tr>
        </thead>
        <tbody id="cycles"></tbody>
      </table>
    </section>

    <section>
      <h2>Eventos</h2>
      <table>
        <thead><tr><th>Hora</th><th>Nivel</th><th>Mensaje</th></tr></thead>
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
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    }[char]));
    const statusLabels = {
      blocked: "bloqueado",
      completed: "completado",
      failed: "fallido",
      not_started: "no iniciado",
      passed: "correcto",
      running: "ejecutando",
      safety_stop: "parada de seguridad",
      stale: "sin latido",
      stopped: "detenido",
      waiting: "en espera"
    };
    const eventMessages = {
      "cycle blocked": "ciclo bloqueado",
      "cycle failed": "ciclo fallido",
      "cycle passed": "ciclo correcto",
      "cycle running": "ciclo en ejecución",
      "cycle started": "ciclo iniciado",
      "soak run started": "prueba continua iniciada",
      "soak run stop requested": "detención solicitada",
      "soak run stopped by operator": "prueba continua detenida por el operador"
    };
    const levelLabels = { error: "error", info: "info", warning: "advertencia" };
    const statusLabel = (status) => statusLabels[status] || status || "desconocido";
    const eventMessageLabel = (message) => eventMessages[message] || message || "-";
    const levelLabel = (level) => levelLabels[level] || level || "-";
    const targetLabel = (label) => ({
      default_safe_query: "consulta segura predeterminada",
      safe_text: "consulta segura",
      safe_fna: "foja/número/año seguro"
    }[label] || label || "-");
    const booleanLabel = (value) => value === true ? "sí" : value === false ? "no" : "-";
    const windowModeLabel = (value) => ({
      normal: "normal",
      offscreen: "fuera de pantalla"
    }[value] || value || "-");
    const statusBadge = (status) => `<span class="badge ${status || ""}">${escapeHtml(statusLabel(status))}</span>`;
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
      if (!data.run) return "Listo para la primera prueba continua";
      if (data.status === "blocked") return (data.alert && data.alert.title) || "Ejecución bloqueada por seguridad";
      if (data.status === "stale") return "El panel no recibe latidos recientes";
      if (cycles === 0) return "Listo para el primer ciclo";
      if ((stats.safety_stops || 0) === 0 && ["running", "waiting"].includes(data.status)) return `Funcionando normalmente por ${fmtSeconds(stats.uptime_seconds)}`;
      if ((stats.safety_stops || 0) === 0 && data.status === "completed") return "Última ejecución completada correctamente";
      return `${cycles} ciclos completados con evidencia visible`;
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
        `<div class="legend-item"><span><span class="dot ok"></span>Correctos</span><strong>${counts.passed}</strong></div>`,
        `<div class="legend-item"><span><span class="dot bad"></span>Fallidos/bloqueados</span><strong>${counts.failed + counts.blocked}</strong></div>`,
        `<div class="legend-item"><span><span class="dot warn"></span>En ejecución/en espera</span><strong>${counts.running + counts.waiting}</strong></div>`
      ].join("");
      document.getElementById("cycleSample").textContent = `${(cycles || []).length} ciclo(s)`;
      const max = Math.max(1, ...Object.values(counts));
      const bars = [
        ["Correctos", counts.passed, "ok"],
        ["Fallidos", counts.failed, "bad"],
        ["Bloqueados", counts.blocked, "bad"],
        ["En curso", counts.running + counts.waiting, "warn"]
      ];
      document.getElementById("outcomeBars").innerHTML = bars.map(([label, count, tone]) => `
        <div class="bar-row"><span>${label}</span><div class="bar-track"><div class="bar-fill ${tone}" style="width:${Math.max(4, count / max * 100)}%"></div></div><strong>${count}</strong></div>
      `).join("");
    };
    const renderSparkline = (cycles) => {
      const shell = document.getElementById("sparkline");
      const ordered = [...(cycles || [])].reverse().slice(-32);
      if (!ordered.length) {
        shell.innerHTML = '<div class="empty">Todavía no hay historial de ciclos. Inicia la prueba continua para llenar este gráfico.</div>';
        return;
      }
      const width = 720, height = 168, pad = 24;
      const step = ordered.length > 1 ? (width - pad * 2) / (ordered.length - 1) : 0;
      const yFor = (status) => status === "passed" ? 56 : status === "running" || status === "waiting" ? 92 : 124;
      const points = ordered.map((cycle, index) => [pad + step * index, yFor(cycle.status), cycle]);
      const path = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
      shell.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="100%" role="img" aria-label="Tendencia de resultados por ciclo">
          <path d="M${pad} 56 H${width-pad}" stroke="#dbe4ef" stroke-width="1"/>
          <path d="M${pad} 92 H${width-pad}" stroke="#dbe4ef" stroke-width="1"/>
          <path d="M${pad} 124 H${width-pad}" stroke="#dbe4ef" stroke-width="1"/>
          <text x="${pad}" y="45">correcto</text>
          <text x="${pad}" y="84">activo</text>
          <text x="${pad}" y="116">detenido</text>
          <path d="${path}" fill="none" stroke="#1d4ed8" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          ${points.map(([x, y, cycle]) => {
            const color = cycle.status === "passed" ? "#11845b" : cycle.status === "running" || cycle.status === "waiting" ? "#b76e00" : "#b42318";
            return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5" fill="${color}" stroke="#fff" stroke-width="2"><title>Ciclo ${cycle.sequence}: ${statusLabel(cycle.status)}</title></circle>`;
          }).join("")}
        </svg>`;
    };
    const renderAlert = (data) => {
      const alert = data.alert;
      const shell = document.getElementById("safetyAlert");
      if (!alert || !alert.active) {
        shell.classList.add("hidden");
        return;
      }
      shell.classList.remove("hidden");
      document.getElementById("safetyAlertIcon").innerHTML = icon("alert");
      document.getElementById("safetyAlertTitle").textContent = alert.title || "Acción del portal pausada";
      document.getElementById("safetyAlertMessage").textContent = alert.message || "La prueba continua se detuvo antes de generar más tráfico al portal.";
      document.getElementById("safetyAlertReason").textContent = alert.reason || "-";
      document.getElementById("safetyAlertCycle").textContent = alert.cycle_sequence || "-";
    };
    async function requestStop() {
      if (!confirm("¿Solicitar que la prueba continua se detenga en el próximo punto seguro?")) return;
      await fetch("/api/stop", { method: "POST" });
      await refresh();
    }
    async function refresh() {
      const response = await fetch("/api/status", { cache: "no-store" });
      const data = await response.json();
      const status = document.getElementById("status");
      status.textContent = statusLabel(data.status);
      status.className = `status ${data.status || ""}`;
      const heroStatus = document.getElementById("heroStatus");
      heroStatus.textContent = statusLabel(data.status);
      heroStatus.className = `status ${data.status || ""}`;
      const stats = data.stats || {};
      const canStop = ["running", "waiting", "stale"].includes(data.status);
      const stopButton = document.getElementById("stopButton");
      stopButton.disabled = !canStop;
      renderAlert(data);
      document.getElementById("alive").textContent = fmtSeconds(stats.uptime_seconds);
      document.getElementById("success").textContent = pct(stats.success_rate);
      document.getElementById("downloads").textContent = stats.downloads ?? "-";
      document.getElementById("safetyStops").textContent = stats.safety_stops ?? "-";
      document.getElementById("headline").textContent = screenshotHeadline(data);
      document.getElementById("headlineSub").textContent = data.run
        ? `La última ejecución tiene ${stats.total_cycles || 0} ciclo(s), ${stats.downloads || 0} PDF(s) generado(s) y ${stats.safety_stops || 0} parada(s) de seguridad.`
        : "Abre el panel ahora e inicia la prueba continua cuando estés listo.";
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
      document.getElementById("aliveNote").textContent = `Edad del latido ${fmtSeconds(stats.heartbeat_age_seconds)}`;
      document.getElementById("successNote").textContent = `${stats.passed_cycles || 0} correctos / ${stats.total_cycles || 0} total`;
      document.getElementById("downloadsNote").textContent = `${stats.downloads || 0} enlace(s) de archivo en la última ejecución`;
      document.getElementById("safetyNote").textContent = (stats.safety_stops || 0) ? "Revisar el motivo antes de continuar" : "Sin paradas críticas en la última ejecución";
      renderDonut(stats, data.cycles || []);
      renderSparkline(data.cycles || []);
      const runtime = data.runtime || {};
      const run = data.run || {};
      document.getElementById("runtime").innerHTML = [
        `motor=<code>${runtime.browser_backend || "-"}</code>`,
        `ventana=<code>${windowModeLabel(runtime.browser_window_mode)}</code>`,
        `sin interfaz=<code>${booleanLabel(runtime.browser_headless)}</code>`,
        `demora=<code>${runtime.request_delay_seconds || "-"}s</code>`,
        `país esperado=<code>${runtime.expected_egress_country || "-"}</code>`,
        `próximo ciclo=<code>${run.next_cycle_at ? localTime(run.next_cycle_at) : "-"}</code>`,
        `motivo de bloqueo=<code>${run.blocked_reason || "-"}</code>`
      ].map((item) => `<div style="margin-bottom:8px">${item}</div>`).join("");
      document.getElementById("cycles").innerHTML = (data.cycles || []).map((cycle) => {
        const artifact = cycle.artifact_path ? `<a href="/artifact/${cycle.cycle_id}" target="_blank">${fileName(cycle.artifact_path)}</a>` : "";
        const report = cycle.validation_report_path ? `<code>${fileName(cycle.validation_report_path)}</code>` : "";
        return `<tr>
          <td>${cycle.sequence}</td>
          <td>${statusBadge(cycle.status)}</td>
          <td>${escapeHtml(targetLabel(cycle.target_label))}</td>
          <td>${cycle.result_count ?? ""}</td>
          <td>${artifact}</td>
          <td>${report}</td>
          <td>${escapeHtml(cycle.safety_stop || cycle.error || "")}</td>
          <td>${localTime(cycle.finished_at)}</td>
        </tr>`;
      }).join("");
      document.getElementById("events").innerHTML = (data.events || []).slice(0, 25).map((event) => `<tr>
        <td>${localTime(event.created_at)}</td>
        <td>${escapeHtml(levelLabel(event.level))}</td>
        <td>${escapeHtml(eventMessageLabel(event.message))}</td>
      </tr>`).join("");
    }
    document.getElementById("stopButton").addEventListener("click", requestStop);
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""
