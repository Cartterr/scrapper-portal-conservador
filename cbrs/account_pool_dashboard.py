from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .account_pool import AccountPoolStore, PoolConfig, dashboard_status, resolve_account_captcha
from .config import SETTINGS, Settings
from .safety import redact


@dataclass(frozen=True)
class PoolDashboardHandle:
    url: str
    server: ThreadingHTTPServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_pool_dashboard(
    store: AccountPoolStore,
    *,
    settings: Settings = SETTINGS,
    config: PoolConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    captcha_resolver: Callable[..., dict[str, Any]] | None = None,
) -> PoolDashboardHandle:
    handler = _handler_factory(store, settings, config, captcha_resolver=captcha_resolver)
    server = ThreadingHTTPServer((host, port), handler)
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return PoolDashboardHandle(
        url=f"http://{actual_host}:{actual_port}",
        server=server,
        thread=thread,
    )


def _handler_factory(
    store: AccountPoolStore,
    settings: Settings,
    config: PoolConfig,
    *,
    captcha_resolver: Callable[..., dict[str, Any]] | None = None,
):
    resolver = captcha_resolver or resolve_account_captcha
    captcha_threads: dict[str, threading.Thread] = {}

    class AccountPoolDashboardHandler(BaseHTTPRequestHandler):
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
                payload = dashboard_status(store, config=config)
                payload["runtime"] = _runtime_summary(settings)
                self._send_json(_with_artifact_urls(payload))
                return
            if parsed.path == "/api/cycles":
                limit = _limit(parsed.query)
                run_id = _latest_run_id(store)
                self._send_json({"cycles": store.recent_cycles(run_id=run_id, limit=limit)})
                return
            if parsed.path == "/api/artifacts":
                run_id = _latest_run_id(store)
                artifacts = _with_artifact_urls({"artifacts": store.artifacts(run_id=run_id)})
                self._send_json(artifacts)
                return
            if parsed.path == "/api/events":
                limit = _limit(parsed.query)
                run_id = _latest_run_id(store)
                self._send_json({"events": store.recent_events(run_id=run_id, limit=limit)})
                return
            if parsed.path.startswith("/artifact/"):
                self._send_artifact(parsed.path.rsplit("/", 1)[-1])
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/stop":
                store.request_stop()
                self._send_json({"ok": True, "status": "stop_requested"})
                return
            if parsed.path.startswith("/api/captcha/") and parsed.path.endswith("/trigger"):
                account_id = parsed.path.split("/")[3]
                self._trigger_captcha(account_id)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def _trigger_captcha(self, account_id: str) -> None:
            known_ids = {account.account_id for account in config.accounts}
            if account_id not in known_ids:
                self.send_error(HTTPStatus.NOT_FOUND, "unknown account")
                return
            existing = captcha_threads.get(account_id)
            if existing and existing.is_alive():
                self._send_json({"ok": True, "status": "already_running", "account_id": account_id})
                return

            def run_recovery() -> None:
                try:
                    resolver(
                        settings=settings,
                        config=config,
                        store=store,
                        account_id=account_id,
                    )
                except Exception as exc:
                    run = store.latest_run()
                    if run:
                        store.add_event(
                            str(run["run_id"]),
                            account_id=account_id,
                            level="error",
                            message="pool captcha recovery failed",
                            data={"error": str(exc)},
                        )

            thread = threading.Thread(
                target=run_recovery,
                name=f"cbrs-captcha-{account_id}",
                daemon=True,
            )
            captcha_threads[account_id] = thread
            thread.start()
            self._send_json({"ok": True, "status": "started", "account_id": account_id})

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
            output_root = (settings.output_dir / "pool").resolve()
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

    return AccountPoolDashboardHandler


def _with_artifact_urls(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    artifacts = []
    for artifact in enriched.get("artifacts", []):
        item = dict(artifact)
        item["artifact_url"] = f"/artifact/{item['cycle_id']}"
        artifacts.append(item)
    enriched["artifacts"] = artifacts
    return enriched


def _latest_run_id(store: AccountPoolStore) -> str | None:
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
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pool de Consultas CBRS</title>
  <style>
    :root {
      --bg: #edf3f8;
      --ink: #111827;
      --muted: #667085;
      --panel: #fff;
      --line: #d8e0ea;
      --ok: #11845b;
      --ok-soft: #e7f7ef;
      --warn: #b76e00;
      --warn-soft: #fff4de;
      --bad: #b42318;
      --bad-soft: #fff1f0;
      --accent: #1d4ed8;
      --accent-soft: #e8efff;
      --shadow: 0 18px 50px rgba(31, 41, 55, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    main { max-width: 1240px; margin: 0 auto; padding: 22px 24px 36px; }
    button {
      border: 1px solid var(--bad);
      border-radius: 6px;
      color: #fff;
      background: var(--bad);
      padding: 8px 12px;
      font-weight: 800;
      cursor: pointer;
    }
    .muted { color: var(--muted); }
    .status {
      display: inline-flex;
      padding: 7px 10px;
      border-radius: 6px;
      background: var(--accent-soft);
      color: var(--accent);
      font-weight: 800;
      text-transform: uppercase;
      font-size: 12px;
    }
    .status.completed, .status.running, .status.waiting { background: var(--ok-soft); color: var(--ok); }
    .status.waiting_capacity { background: var(--warn-soft); color: var(--warn); }
    .status.stale, .status.blocked, .status.captcha_pending { background: var(--bad-soft); color: var(--bad); }
    .status.captcha_solving { background: var(--warn-soft); color: var(--warn); }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.6fr) minmax(320px, .8fr);
      gap: 16px;
      margin-bottom: 16px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 18px;
    }
    .headline {
      min-height: 240px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      background: linear-gradient(135deg, #fff 0%, #eef8ff 100%);
    }
    .headline h2 { font-size: 42px; line-height: 1.05; margin: 0; max-width: 760px; }
    .capacity {
      margin-top: 18px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,.8);
    }
    .capacity-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-weight: 800;
      margin-bottom: 10px;
    }
    .bar { height: 18px; border-radius: 999px; overflow: hidden; background: #e8eef5; display: flex; }
    .bar span { display: block; min-width: 0; transition: width .2s ease; }
    .bar .used { background: var(--ok); }
    .bar .remaining { background: #dfe7f0; }
    .accounts {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0;
    }
    .account {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .account.paused, .account.captcha_pending { border-color: var(--bad); background: var(--bad-soft); }
    .account.captcha_solving { border-color: var(--warn); background: var(--warn-soft); }
    .account.quota_reached { border-color: var(--warn); background: var(--warn-soft); }
    .account-top { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
    .account-name { font-weight: 850; }
    .account-count { font-size: 28px; font-weight: 900; margin: 8px 0; }
    .mini-bar { height: 10px; border-radius: 999px; overflow: hidden; background: #e8eef5; }
    .mini-bar span { display: block; height: 100%; background: var(--accent); }
    .account-action {
      margin-top: 12px;
      border-color: var(--bad);
      background: var(--bad);
      width: 100%;
    }
    .account-action[disabled] {
      cursor: not-allowed;
      opacity: .65;
    }
    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .kpi .value { font-size: 28px; font-weight: 900; margin-top: 6px; }
    .alert {
      display: none;
      margin-bottom: 16px;
      border: 2px solid var(--bad);
      background: var(--bad-soft);
      border-radius: 8px;
      padding: 16px;
    }
    .alert.show { display: block; }
    .alert h2 { color: var(--bad); font-size: 22px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; }
    th { color: var(--muted); }
    a { color: var(--accent); text-decoration: none; font-weight: 700; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    @media (max-width: 900px) {
      .hero, .accounts, .kpis { grid-template-columns: 1fr; }
      .headline h2 { font-size: 34px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Pool de Consultas CBRS</h1>
      <div class="muted">3 cuentas autorizadas · 60 consultas teóricas por día</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <button id="stopButton">Detener</button>
      <div id="status" class="status">cargando</div>
    </div>
  </header>
  <main>
    <section id="alert" class="alert">
      <h2 id="alertTitle">Advertencia</h2>
      <div id="alertMessage">-</div>
    </section>

    <div class="hero">
      <section class="panel headline">
        <div>
          <div class="muted">Evidencia actual</div>
          <h2 id="headline">Cargando pool de consultas</h2>
          <p id="headlineSub" class="muted">El panel está leyendo el estado local.</p>
        </div>
        <div class="capacity">
          <div class="capacity-row">
            <span>Consultas disponibles hoy</span>
            <span id="capacityNumber">- / 60</span>
          </div>
          <div class="bar">
            <span id="usedBar" class="used"></span>
            <span id="remainingBar" class="remaining"></span>
          </div>
        </div>
      </section>

      <section class="panel">
        <h2>Estado del pool</h2>
        <table>
          <tbody id="poolFacts"></tbody>
        </table>
      </section>
    </div>

    <div id="accounts" class="accounts"></div>
    <div class="muted" style="margin:-8px 0 16px">Cuentas: Ejecutivo 1 · Ejecutivo 2 · Ejecutivo 3</div>

    <div class="kpis">
      <div class="kpi"><div class="muted">Usadas hoy</div><div id="usedToday" class="value">-</div></div>
      <div class="kpi"><div class="muted">Restantes hoy</div><div id="remainingToday" class="value">-</div></div>
      <div class="kpi"><div class="muted">PDFs generados</div><div id="downloads" class="value">-</div></div>
      <div class="kpi"><div class="muted">Captchas pendientes</div><div id="captchaPending" class="value">-</div></div>
    </div>

    <section class="panel">
      <h2>Ciclos recientes</h2>
      <table>
        <thead>
          <tr><th>#</th><th>Cuenta</th><th>Estado</th><th>Resultados</th><th>PDF</th><th>Parada</th><th>Finalizado</th></tr>
        </thead>
        <tbody id="cycles"></tbody>
      </table>
    </section>
  </main>
  <script>
    const statusLabels = {
      available: "disponible",
      blocked: "bloqueado",
      captcha_pending: "captcha pendiente",
      captcha_solving: "resolviendo captcha",
      completed: "completado",
      disabled: "deshabilitada",
      failed: "fallido",
      not_started: "no iniciado",
      passed: "correcto",
      paused: "pausada",
      quota_reached: "cupo usado",
      running: "ejecutando",
      stale: "sin latido",
      stopped: "detenido",
      waiting: "en espera",
      waiting_capacity: "sin capacidad"
    };
    const label = (value) => statusLabels[value] || value || "-";
    const localTime = (value) => value ? new Date(value).toLocaleString() : "-";
    const secondsUntil = (value) => {
      if (!value) return null;
      return Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 1000));
    };
    const fmtSeconds = (value) => {
      if (value === null || value === undefined) return "-";
      const h = Math.floor(value / 3600);
      const m = Math.floor((value % 3600) / 60);
      const s = Math.floor(value % 60);
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s}s`;
      return `${s}s`;
    };
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }[char]));
    let current = null;
    let lastCaptchaNoticeKey = "";
    async function refresh() {
      const response = await fetch("/api/status");
      current = await response.json();
      render();
    }
    function render() {
      if (!current) return;
      const pool = current.pool || {};
      const stats = current.stats || {};
      const run = current.run || {};
      const remaining = pool.remaining_today || 0;
      const quota = pool.daily_quota || 60;
      const used = pool.used_today || 0;
      const usedPct = quota ? Math.min(100, (used / quota) * 100) : 0;
      const remainingPct = Math.max(0, 100 - usedPct);
      const nextSeconds = secondsUntil(run.next_cycle_at);
      const status = document.getElementById("status");
      status.textContent = label(current.status);
      status.className = `status ${current.status || ""}`;
      document.getElementById("headline").textContent = headline(current, nextSeconds);
      document.getElementById("headlineSub").textContent = subline(current, nextSeconds);
      document.getElementById("capacityNumber").textContent = `${remaining} / ${quota}`;
      document.getElementById("usedBar").style.width = `${usedPct}%`;
      document.getElementById("remainingBar").style.width = `${remainingPct}%`;
      document.getElementById("usedToday").textContent = used;
      document.getElementById("remainingToday").textContent = remaining;
      document.getElementById("downloads").textContent = stats.downloads ?? 0;
      document.getElementById("captchaPending").textContent = pool.captcha_pending_accounts ?? 0;
      renderAlert(current.alert);
      renderPoolFacts(pool, run, nextSeconds);
      renderAccounts(current.accounts || []);
      renderCycles(current.cycles || [], current.artifacts || []);
    }
    function headline(data, nextSeconds) {
      const pool = data.pool || {};
      if (!data.run) return "Listo para iniciar el pool";
      if (data.status === "waiting_capacity") return "Pool sin capacidad disponible";
      if (data.status === "waiting" && nextSeconds !== null) return `Próxima consulta en ${fmtSeconds(nextSeconds)}`;
      if (data.status === "running") return `Ejecutando con ${pool.next_account_label || "cuenta disponible"}`;
      return `${pool.remaining_today ?? 0} consultas disponibles hoy`;
    }
    function subline(data, nextSeconds) {
      const pool = data.pool || {};
      if (data.status === "waiting" && nextSeconds !== null) {
        return `Siguiente cuenta: ${pool.next_account_label || "-"} · ${localTime(data.run.next_cycle_at)}`;
      }
      if (data.status === "waiting_capacity") {
        return "El runner queda vivo, pero no hará más tráfico hasta tener cuentas con cupo.";
      }
      return `${pool.used_today || 0} usadas de ${pool.daily_quota || 60} consultas teóricas diarias.`;
    }
    function renderAlert(alert) {
      const box = document.getElementById("alert");
      if (!alert || !alert.active) {
        box.classList.remove("show");
        return;
      }
      box.classList.add("show");
      document.getElementById("alertTitle").textContent = alert.title || "Advertencia";
      document.getElementById("alertMessage").textContent = alert.message || "-";
      maybeNotifyCaptcha(alert);
    }
    function renderPoolFacts(pool, run, nextSeconds) {
      const rows = [
        ["Cupo diario", `${pool.daily_quota || 60}`],
        ["Disponible", `${pool.available_accounts ?? 0} cuenta(s)`],
        ["Siguiente cuenta", pool.next_account_label || "-"],
        ["Próximo ciclo", nextSeconds === null ? "-" : fmtSeconds(nextSeconds)],
        ["Fecha de cupo", pool.quota_date || "-"],
        ["ID de ejecución", run.run_id || "-"]
      ];
      document.getElementById("poolFacts").innerHTML = rows
        .map(([key, value]) => `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(value)}</td></tr>`)
        .join("");
    }
    function renderAccounts(accounts) {
      document.getElementById("accounts").innerHTML = accounts.map((account) => {
        const pct = account.daily_quota ? Math.min(100, (account.used_today / account.daily_quota) * 100) : 0;
        const note = account.paused_reason ? `Motivo: ${account.paused_reason}` : `${account.remaining_today} restantes hoy`;
        const action = account.status === "captcha_pending"
          ? `<button class="account-action" data-captcha-account="${escapeHtml(account.account_id)}">Resolver captcha</button>`
          : "";
        return `<section class="account ${escapeHtml(account.status)}">
          <div class="account-top">
            <div class="account-name">${escapeHtml(account.label)}</div>
            <div class="status ${escapeHtml(account.status)}">${escapeHtml(label(account.status))}</div>
          </div>
          <div class="account-count">${account.used_today}/${account.daily_quota}</div>
          <div class="mini-bar"><span style="width:${pct}%"></span></div>
          <div class="muted" style="margin-top:8px">${escapeHtml(note)}</div>
          ${action}
        </section>`;
      }).join("");
    }
    async function triggerCaptcha(accountId, button) {
      if (!accountId) return;
      if (button) {
        button.disabled = true;
        button.textContent = "Abriendo Chrome...";
      }
      await fetch(`/api/captcha/${encodeURIComponent(accountId)}/trigger`, { method: "POST" });
      await refresh();
    }
    function maybeNotifyCaptcha(alert) {
      if (!("Notification" in window)) return;
      if (alert.reason !== "captcha_rejected") return;
      const key = `${alert.account_id || alert.account_label || ""}:${alert.reason || ""}`;
      if (!key || key === lastCaptchaNoticeKey) return;
      lastCaptchaNoticeKey = key;
      const send = () => new Notification("CBRS: captcha pendiente", {
        body: `${alert.account_label || "Una cuenta"} necesita resolución manual.`,
      });
      if (Notification.permission === "granted") send();
      else if (Notification.permission !== "denied") {
        Notification.requestPermission().then((permission) => {
          if (permission === "granted") send();
        });
      }
    }
    function renderCycles(cycles, artifacts) {
      const artifactMap = new Map((artifacts || []).map((item) => [item.cycle_id, item]));
      document.getElementById("cycles").innerHTML = (cycles || []).map((cycle) => {
        const artifact = artifactMap.get(cycle.cycle_id);
        const pdf = artifact ? `<a href="${artifact.artifact_url}" target="_blank">PDF</a>` : "-";
        return `<tr>
          <td>${cycle.sequence}</td>
          <td>${escapeHtml(cycle.account_label || cycle.account_id)}</td>
          <td>${escapeHtml(label(cycle.status))}</td>
          <td>${cycle.result_count ?? "-"}</td>
          <td>${pdf}</td>
          <td><code>${escapeHtml(cycle.safety_stop || "-")}</code></td>
          <td>${localTime(cycle.finished_at)}</td>
        </tr>`;
      }).join("");
    }
    document.getElementById("stopButton").addEventListener("click", async () => {
      await fetch("/api/stop", { method: "POST" });
      await refresh();
    });
    document.getElementById("accounts").addEventListener("click", (event) => {
      const button = event.target.closest("[data-captcha-account]");
      if (!button) return;
      triggerCaptcha(button.dataset.captchaAccount, button).catch(console.error);
    });
    refresh().catch(console.error);
    setInterval(() => refresh().catch(console.error), 2000);
  </script>
</body>
</html>"""
