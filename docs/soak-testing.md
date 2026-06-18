# CBRS Long-Running Soak Test

The soak runner proves the normal production flow over time without load testing
the portal. It keeps one local process alive, spaces live cycles at a low
operator-like cadence, and stops portal actions on any safety signal.

## Commands

Open the dashboard without starting any portal traffic:

```powershell
python -m cbrs soak dashboard
```

Dry-run the runner and dashboard without portal traffic:

```powershell
python -m cbrs soak run --dry-run --max-cycles 3 --dashboard
```

Start the live long-running soak loop:

```powershell
python -m cbrs soak run --dashboard
```

Start the live runner while an already-open dashboard watches it:

```powershell
python -m cbrs soak run
```

Request a graceful stop from another terminal:

```powershell
python -m cbrs soak stop
```

Check the latest state:

```powershell
python -m cbrs soak status
```

Export a sanitized evidence bundle:

```powershell
python -m cbrs soak export --output .cbrs/soak/export.json
```

## Local Configuration

Optional local config lives at `.cbrs/soak-config.json` and is ignored by git.
The dashboard stores only target labels, not raw query values.

```json
{
  "interval_min_minutes": 2,
  "interval_max_minutes": 4,
  "dashboard_host": "127.0.0.1",
  "dashboard_port": 8765,
  "targets": [
    {
      "label": "default_safe_query",
      "query": "BANCO DE CHILE"
    }
  ]
}
```

## Runtime Behavior

- The first cycle runs immediately.
- Later live cycles wait a randomized test-only interval between 2 and 4
  minutes by default, averaging about 20 full-flow consults per hour.
- Each live cycle uses the regular production path: fixed-egress preflight,
  persistent Chrome/Edge profile, safe search, and one first-result download.
- PDFs are written under `outputs/soak/<run_id>/<cycle_id>/`.
- The dashboard is read-only at `http://127.0.0.1:8765`.
- `python -m cbrs soak dashboard` opens the dashboard without creating a run.
- The dashboard Stop button and `python -m cbrs soak stop` request the runner
  to stop after the current safe point; they do not kill the browser mid-cycle.
- The dashboard serves artifact links only from `outputs/soak`.

## Safety Stops

The soak process records the stop and keeps the dashboard available, but it does
not continue live portal actions after:

- egress preflight failure or egress drift
- authentication failure
- `403`, `429`, WAF/challenge HTML, or unexpected HTML
- `err-limite`, `intente-mas-tarde`, or temporary-unavailable portal responses

Resume only after reviewing the dashboard, exported evidence, and current
egress/profile state.
