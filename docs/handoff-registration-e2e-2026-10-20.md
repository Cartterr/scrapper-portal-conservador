# Handoff: CBRS registration E2E debugging (2026-10-20)

> Versioned delivery copy: operational mailbox values and coworker identity
> examples are anonymized. The original handoff is retained locally under
> the gitignored registration directory. Sections 1-14 are chronological;
> their statements about uncommitted work describe their respective times.
> For the client-facing result and limitations, see
> [the delivery report](client-registration-changes-2026-10-20.md).

Audience: the next engineer/LLM picking this up. Written after debugging and
partially fixing the account-registration runbook against the Windows checkout
of this repository. Nothing was committed, nothing was deployed, no production
browser, profile, route, or worker was touched.

---

## 1. One-paragraph summary

The registration runbook (`rut_data.db` → `insert_new_ruts` → `check-inbox` →
`register --dry-run` → `register --count 3` → `export-pool`) was executed as far
as was safe. Steps 3 and 4 work live. Five real code defects were found and
fixed, with regression tests (suite went from 42 to 46 passing). Two problems are
in the *runbook itself*, not the code: the sample RUTs are placeholders whose
check digits fail mod-11 validation, and step 1 parks the whole existing pool
under a status (`unused`) that the provider never reads. The account-creation step
(`register --count 3`) was deliberately **not** run: it needs the user's real
`(name, RUT-DV)` pairs, and it creates real portal accounts and sends activation
email.

---

## 2. Environment and repository state

| Item | Value |
| --- | --- |
| Machine | Windows 11, PowerShell 7 (`pwsh`) |
| Checkout | `C:\V\scrapper\scrapper-portal-conservador` |
| Branch | `master` at `f4ca36c` |
| Working tree | dirty, **uncommitted** (see §8) |
| Active production repo per `AGENTS.md` | `/opt/scrapper-portal-conservador` in Ubuntu-24.04 WSL2 (systemd services). The Windows checkout is a rollback copy. |
| Registration pipeline | CLI-only (`python -m cbrs.registration ...`), **not** part of the worker/service, so no service restart is needed to use it. These changes are **not** active in the WSL checkout until ported there. |
| Browser used by registration | regular Chrome, **new** persistent profile under `.cbrs/registration-profiles/`, fresh DataImpulse sticky port. Never a production context. |

Local state observed (all gitignored):

- `rut_data.db` — 30 rows, all `status='scraped'`, all checksum-valid, all
  parseable as `ApellidoPaterno ApellidoMaterno Nombres`, created
  `2026-10-11T03:25:59` (looks like a scrape of `nombrerutyfirma`; RUTs run
  `<coworker-rut-1>` … `<coworker-rut-4>`).
- `registered_accounts.db` — created empty by the CLI (`AccountStore.__init__`
  creates it). 0 accounts as of this handoff. Gitignored.
- `.cbrs/runtime/pool/pool.sqlite3` — the **live** worker state DB; table
  `account_proxy_routes` holds `ejecutivo_1=10002`, `ejecutivo_2=10403`,
  `ejecutivo_3=12264`.
- `.cbrs/pool/pool.sqlite3` — a **different, stale** DB with no
  `account_proxy_routes` table (tables: `runs`, `accounts`, `cycles`, `events`,
  `jobs`, …). This distinction caused defect D-3.
- `.env` — contains `DATAIMPULSE_PROXY_*`, `CBRS_ACTIVATION_*`,
  `CBRS_EJECUTIVO_*`, CapSolver/2Captcha keys. Do not print or copy values.
  Activation config resolves to: domain `<redacted:CBRS_ACTIVATION_DOMAIN>`, IMAP
  `<redacted:CBRS_ACTIVATION_IMAP_HOST>:993` SSL, user `example-1@example.invalid`.

---

## 3. The runbook that was being debugged (as received)

1. Park scraped identities and reuse the pool:
   `python -c "import sqlite3; c=sqlite3.connect('rut_data.db'); c.execute(\"UPDATE ruts SET status='unused' WHERE status='scraped'\"); c.commit(); ..."`
2. Load each coworker's own RUT + name, format
   `"ApellidoPaterno ApellidoMaterno Nombres"` + real check digit:
   `insert_new_ruts('rut_data.db', pairs)` → prints `(inserted, duplicates, invalid)`.
3. `python -m cbrs.registration check-inbox`
4. `python -m cbrs.registration register --count 1 --dry-run`
5. `python -m cbrs.registration register --count 3`
6. `python -m cbrs.registration export-pool`

The user states the RUTs of their coworkers live in `rut_data.db`. If that means
the 30 existing rows *are* the coworkers, **step 1 must not be run** (it hides
exactly those rows). Confirm before step 1 — see §6.

---

## 4. What was actually executed (evidence)

```
python -m cbrs.registration status
  RUT pool (rut_data.db):
    scraped: 30
  Registered accounts (registered_accounts.db):
    (none)
  Activation inbox: configured

python -m cbrs.registration check-inbox        # exit 0
  Connecting to <redacted:CBRS_ACTIVATION_IMAP_HOST>:993 (SSL) as example-1@example.invalid...
  IMAP OK. Folders: Archive, Drafts, INBOX, Junk, Sent, Trash
  Read folder: INBOX
  Note: the activation address must be on <redacted:CBRS_ACTIVATION_DOMAIN> with a catch-all ...

python -m cbrs.registration register --count 1 --dry-run     # exit 0, run twice
  Registration run: sticky port 15773 (first run) / 11723 (after D-3 fix)
  Dry-run payload for <coworker-rut-1>:
    nombres='Example GivenNames', apellidoPaterno='ExamplePaternal', apellidoMaterno='ExampleMaternal',
    rutDv='<coworker-rut-1>', email='example-3@example.invalid',
    direccion='<example-address>', celular='986552408',
    recaptchaToken='<2489 chars>', tos=True
  [ DRY_RUN] <coworker-rut-1> -> example-3@example.invalid
  Dry run complete: nothing was submitted; RUTs released.

python -m cbrs.registration status              # after both dry runs
  scraped: 30        # RUT correctly released, pool unchanged
```

The first dry run logged
`WARNING ... Could not read production proxy routes from .cbrs\pool\pool.sqlite3; continuing`.
That was defect D-3; the second run (after the fix) has no warning and used port
11723, avoiding 10002/10403/12264.

**Not executed:** step 1 (would mutate the live pool), step 2 (no real pairs
supplied), step 5 (creates real accounts + sends email).

---

## 5. Defects found and fixed

### D-1 — `insert_new_ruts` stored non-canonical RUTs and missed duplicates

- File: `cbrs/registration/rut_refill.py`, `insert_new_ruts` (now line 165).
- Cause: the dedupe set was built from `normalize_rut(...)`, but the incoming
  value was validated, compared and inserted **raw**.
- Evidence (temp copy of the real DB):

  ```
  insert_new_ruts(db, [("Jaramillo Toro Elisa Victoria", "1000375-k")])
    -> (1, 0, 0)                       # should be a duplicate
  rows for that person:
    ('<coworker-rut-3>', 'unused'), ('1000375-k', 'scraped')
  ```

  Two consequences: duplicate identities in `ruts`, and a malformed `rutDv`
  (`1000375-k`) sent to `/api/v1/account/create` (the payload sends the stored
  string verbatim in `rutDv`).
- Fix: `normalized = normalize_rut(rut)`; reject if `None` or invalid; dedupe and
  insert the normalized value; collapse whitespace in the name. Also documented
  why. Regression test: `test_insert_new_ruts_stores_canonical_rut`
  (`tests/test_registration.py:915`) asserts `(2, 1, 0)` and stored values
  `["<coworker-rut-3>", "12.345.678-5", "11.111.111-1"]`.

### D-2 — `register --count N` aborted mid-run when the pool ran dry

- File: `cbrs/registration/orchestrator.py`, loop in `register_accounts`
  (around line 219-240).
- Cause: `RutProvider.reserve_next()` raises `NoRutsAvailable`; the loop did not
  catch it, so it propagated out of `register_accounts`, hit the CLI's
  `except NoRutsAvailable` (which prints "No RUTs available" and exits 2) and
  `finally: portal.close()` ran — the account already created earlier in the
  same run was never reported to the operator.
- Why it matters for this runbook: the three sample RUTs contain only **one**
  valid check digit (see §6), so `--count 3` would hit exactly this path after
  creating one real account.
- Fix: catch `NoRutsAvailable` inside the loop, log
  `RUT pool ran dry after N/M accounts; stopping this run.`, keep the collected
  results, and re-raise only when nothing at all was attempted. The CLI now also
  prints `RUT pool ran dry: only X of Y accounts attempted; add RUTs ...`.
  Regression test: `test_orchestrator_stops_cleanly_when_pool_runs_dry`
  (`tests/test_registration.py:828`) → 2 RUTs, `count=3`, expects
  `["verified", "verified"]`, `portal.closed`, store counts `{"verified": 2}`.

### D-3 — sticky-port collision guard read a stale database (real operational risk)

- Files: `cbrs/registration/config.py:225`, `cbrs/registration/orchestrator.py:90`.
- Cause: `RegistrationSettings.pool_state_path` was hardcoded to
  `.cbrs/pool/pool.sqlite3`. That file exists but has **no**
  `account_proxy_routes` table, so `choose_sticky_port` swallowed the
  `sqlite3.Error` and continued with an empty "used ports" set. Live production
  routes (10002, 10403, 12264 in `.cbrs/runtime/pool/pool.sqlite3`) were
  therefore invisible to registration, and a run could have been given the same
  sticky port/exit IP as a production account.
- Fix: default is now
  `.cbrs/runtime/pool/pool.sqlite3` (same DB the worker writes), with the
  override `CBRS_REGISTRATION_POOL_STATE_DB`; the warning text now says
  "continuing without collision avoidance". Regression tests:
  `test_load_registration_settings_defaults` (asserts the path) and
  `test_load_registration_settings_pool_state_override`.
  Live check: 200 sampled ports avoided `{10002, 10403, 12264}` and stayed in
  `[dataimpulse_port_min, dataimpulse_port_max]`.

### D-4 — `export-pool` produced a pool file the worker loader rejects

- Files: `cbrs/registration/store.py` (`export_pool_entries`,
  `append_to_account_pool`), `cbrs/registration/__main__.py`
  (`_cmd_export_pool`).
- Cause: entries were `{"email", "password", "provider": "cbrs-registration",
  "rut"}` written to `.cbrs/account-pool.json`. `cbrs.account_pool._parse_accounts`
  rejects credential fields outright:

  ```
  ValueError: account pool config must not contain credentials, emails, RUTs,
  or secret fields: email, password, rut
  ```

  It also requires an `id` per account, and the worker reads
  `.cbrs/runtime/account-pool.json` (env-ref based:
  `id`, `label`, `enabled`, `username_env`, `password_env`, `proxy_provider`,
  `dataimpulse_port`), not `.cbrs/account-pool.json`.
  Note: `.cbrs/account-pool.json` already contains 8 legacy credential-bearing
  entries, so the old exporter kept growing an already-invalid file. That legacy
  file was not modified.
- Fix:
  - `export_pool_entries()` now emits loader-compatible, secret-free entries:
    `{"id": "registro_N", "label": "Registro N", "enabled": false,
    "username_env": "CBRS_REGISTRO_N_USERNAME",
    "password_env": "CBRS_REGISTRO_N_PASSWORD"}`.
  - New `export_env_assignments()` returns the matching `NAME=value` lines.
  - `append_to_account_pool()` dedupes by `id` (falls back to `email` for legacy
    rows) and creates parent dirs.
  - `export-pool` defaults to
    `.cbrs/registration/worker-pool.patch.json` +
    `.cbrs/registration/credentials.env`, accepts `--pool` / `--env-file`, and
    prints the three manual steps (add env lines to `.env`, merge the patch into
    `.cbrs/runtime/account-pool.json`, flip `"enabled": true`).
    Entries are deliberately `enabled: false` so merging cannot disturb the live
    worker without an explicit decision.
  - Regression tests: `test_account_store_lifecycle` (new shape),
    `test_append_to_account_pool_dedupes` (id-based),
    `test_exported_pool_entries_pass_the_worker_loader`
    (`tests/test_registration.py:479`, calls the real
    `cbrs.account_pool._parse_accounts`).
- **Not done on purpose:** the exporter still does not assign
  `proxy_provider` / `dataimpulse_port`, and does not write to
  `.cbrs/runtime/account-pool.json`. Choosing sticky ports for these new
  accounts (and their exit-IP mapping) is an operator decision.

### D-5 — portal/egress failures surfaced as tracebacks

- File: `cbrs/registration/__main__.py:47`.
- Cause: `_cmd_register` only caught `NoRutsAvailable`; `PortalError` from
  `open()`, `check_egress_country()` or `bootstrap()` escaped as a stack trace.
- Fix: `except PortalError as exc: print(f"Registration stopped at {exc.stage}: {exc}")`
  and return 1.

---

## 6. Runbook problems that are not code defects (act on these)

1. **The sample pairs are placeholders and two are invalid.** Checked with the
   code's own mod-11 validator:
   - `12.345.678-9` → **invalid** (correct DV for body 12345678 is `5`).
   - `11.111.111-1` → valid.
   - `10.222.333-4` → **invalid** (correct DV for body 10222333 is `0`).
   `insert_new_ruts` therefore returns `(1, 0, 2)` for the runbook's exact input.
   Real RUTs pass; the operator must supply the real pairs.
2. **Step 1's `status='unused'` is a parking label only.** `RutProvider` selects
   only `status='scraped'` (`ORDER BY rowid ASC`, up to 25 rows per call), so the
   parked rows are never consumed again. There is no CLI to un-park them; the
   manual reverse is `UPDATE ruts SET status='scraped' WHERE status='unused'`.
   - Intended use: park scraped identities, then insert the real ones in step 2
     so `register` consumes those oldest-first.
   - **Wrong** use: if the 30 existing rows are the coworkers' real RUTs, step 1
     hides exactly what should be used. Confirm with the operator first.
   - Verified behaviour on a temp copy of the real DB: after step 1 the pool is
     `unused: 30` and `reserve_next()` raises `NoRutsAvailable`; after inserting
     3 valid pairs, `reserve_next()` returned them in insertion order
     (`12.345.678-5`, `11.111.111-1`, `10.222.333-0`) and the 4th call raised
     `NoRutsAvailable`.
3. **Step 6's description is obsolete.** Nothing in `export-pool` "wires" an
   account into the worker automatically any more (see D-4). The worker's pool
   is env-ref based and needs the credentials present in the environment.
4. **One run = one exit + one Chrome profile for all accounts.** `--count 3`
   registers three accounts through the same sticky port and the same persistent
   profile/cookies (see `register_accounts`). This is the documented design
   ("a fresh sticky exit per registration run"), but it is a real rejection/link
   risk for three accounts. Three separate `--count 1` runs are safer. Do not
   change this without an explicit decision; it changes cost and side effects.

---

## 7. Files changed (uncommitted)

| File | Change |
| --- | --- |
| `cbrs/registration/rut_refill.py` | D-1: canonicalize before validate/dedupe/insert; normalize name whitespace. |
| `cbrs/registration/orchestrator.py` | D-2: catch `NoRutsAvailable` per account, keep partial results; D-3 warning text. |
| `cbrs/registration/config.py` | D-3: `pool_state_path` → `.cbrs/runtime/pool/pool.sqlite3` + `CBRS_REGISTRATION_POOL_STATE_DB`. |
| `cbrs/registration/store.py` | D-4: secret-free `export_pool_entries`, new `export_env_assignments`, id-based `append_to_account_pool`. |
| `cbrs/registration/__main__.py` | D-2 partial-run message, D-4 new `export-pool` output/args, D-5 clean `PortalError` handling. |
| `tests/test_registration.py` | 4 new tests + 2 updated assertions (suite 42 → 46). |

Already modified **before** this session (pre-existing work, untouched except
where listed above): `.env.example`, `cbrs/registration/__init__.py`,
`cbrs/registration/email_inbox.py`, `cbrs/registration/identity.py`. Preserve
them; do not `git checkout` those files.

Nothing was staged or committed. `registered_accounts.db`, `rut_data.db` and
`.cbrs/` are gitignored; do not add them.

---

## 8. Tests

```
python -m pytest tests/test_registration.py -q      -> 46 passed
python -m pytest tests/test_dataimpulse.py -q       -> 12 passed
python -m pytest tests/test_account_pool.py -q      -> 31 passed
python -m compileall -q cbrs/registration           -> exit 0
```

New/updated tests and what they pin down:

- `test_insert_new_ruts_stores_canonical_rut` (line 915): alternate spellings
  (`1000375-k`, `12345678-5`) are canonicalized and deduped.
- `test_orchestrator_stops_cleanly_when_pool_runs_dry` (line 828): `count=3`
  with 2 RUTs returns 2 results, closes the portal, no exception.
- `test_exported_pool_entries_pass_the_worker_loader` (line 479): exported
  entries survive the real `cbrs.account_pool._parse_accounts`.
- `test_load_registration_settings_defaults` / `..._pool_state_override`
  (line 278): route DB path and its env override.
- Updated: `test_account_store_lifecycle`, `test_append_to_account_pool_dedupes`.

---

## 9. What the next agent should do

1. **Ask the operator two questions** (blocks step 5):
   - Are the 30 rows already in `rut_data.db` the coworkers' RUTs, or scraped
     identities to be replaced? (Decides whether step 1 runs at all.)
   - Supply the real `(ApellidoPaterno ApellidoMaterno Nombres, RUT-DV)` pairs.
2. If replacing: run step 1, then step 2 with the real pairs, then
   `python -m cbrs.registration status` and confirm
   `scraped: <number of real pairs>`.
3. Run one `register --count 1 --dry-run` again as a smoke test (it reserves and
   releases a RUT, opens headful Chrome, spends a sticky-port session, submits
   nothing). Confirm no "collision avoidance" warning and CL egress.
4. Only with explicit operator approval: `register --count 1` (recommended) or
   `--count 3`. This creates real portal accounts and triggers activation email
   to `<redacted:CBRS_ACTIVATION_DOMAIN>`; activation is read from `example-1@example.invalid` via IMAP.
5. After a verified account exists, run `export-pool`; then, if the operator wants
   it in the pool, add the `CBRS_REGISTRO_*` lines to `.env`, merge
   `.cbrs/registration/worker-pool.patch.json` into
   `.cbrs/runtime/account-pool.json`, choose `proxy_provider`/`dataimpulse_port`
   (unused ports in 10000-20000; production currently holds 10002, 10403, 12264),
   and flip `enabled`.

Useful commands:

```powershell
python -m cbrs.registration status
python -m cbrs.registration check-inbox
python -m cbrs.registration register --count 1 --dry-run
python -m cbrs.registration register --count 1
python -m cbrs.registration export-pool [--pool PATH] [--env-file PATH]
python -m pytest tests/test_registration.py -q
```

---

## 10. Hard safety constraints (from `AGENTS.md`, still in force)

- Never stop/restart/kill/close the production worker, browser owner, Chrome
  instances, contexts, profiles, cookies, or proxy routes for registration work.
  Registration opens its own temporary browser/profile/exit and closes only that.
- Never clear cookies/profiles, rotate a proven proxy, or replay an accepted
  search while debugging registration.
- Do not commit secrets; do not print `.env` values, passwords, or
  `registered_accounts.db` contents into logs or chat.
- Do not restart `cbrs-worker` to load these changes: the registration pipeline
  is a standalone CLI. If the changes must be active in the WSL checkout, port
  them to `/opt/scrapper-portal-conservador` — that is a separate, deliberate
  step.
- `register` without `--dry-run` is outward-facing (account creation + email).
  Get explicit approval first.

---

## 11. Operator confirmation and first real attempt (2026-10-20, America/Santiago)

The operator confirmed that all 30 existing identities are coworkers' real
identities and are already correctly loaded. **Do not park or reinsert them.**
The operator also identified an unresolved name-parsing caveat at row 15,
`ExampleSurname GivenNameOne GivenNameTwo`; leave that row unchanged pending their confirmation.

Revalidation before any real attempt:

- `python -m pytest tests/test_registration.py -q`: 46 passed.
- `status`: 30 scraped RUTs, no registered-account records.
- `check-inbox`: IMAP connection succeeded.
- `register --count 1 --dry-run` at 22:08:21: exit 0, port 17783, no
  collision-avoidance warning, CL country check passed, token length 2446.
  The first RUT returned to `scraped`; all 30 remained available.

The operator then explicitly approved proceeding. Exactly **one** real attempt
was made with `python -m cbrs.registration register --count 1`, at
22:10:18–22:10:34, using temporary port 13833 and profile
`.cbrs/registration-profiles/reg-20261021-011018`. The portal rejected the
create request:

```text
Portal stopped create: captcha_rejected (code captcha-rechazado).
0/1 accounts fully verified.
```

The command exited 1. No create acceptance, activation, login, or verification
was observed. The original run did not log its HTTP response status. Do not
infer that status from the new offline test's simulated HTTP 400.

Current state, checked after the diagnostic dry run below:

- `rut_data.db`: `failed:create: 1`, `scraped: 29`.
- First RUT `<coworker-rut-1>`: `failed:create`, as set by the existing non-retryable
  CAPTCHA-stop handling. It was **not** manually released or retried.
- Second and third RUTs remain `scraped`; row 15's name is unchanged.
- Account-store aggregate counts: `failed: 1`, no verified accounts.
- No export was run; no `.env`, runtime pool, or production route was changed.

One invocation shares its sticky exit and Chrome profile across every account
in that invocation. This invocation requested only one account.

### Screenshot diagnostics added after the failed attempt

The operator requested visual checks at specific times. A desktop capture raced
the CLI's normal browser cleanup: the temporary Chrome window disappeared
before capture. **No screenshot of the failed real request was obtained.**

Added an opt-in `register --screenshots` flag, backed by
`RegistrationSettings.capture_screenshots` and `RegistrationPortal` capture
hooks. It saves numbered PNGs under the run's own gitignored profile at page
load, bootstrap response, token readiness, before create, API responses, and
before browser close. Captures mask password/token inputs, have bounded
timeouts, and cannot prevent browser cleanup if capture fails. Response logs
include endpoint, HTTP status, and sanitized portal code; they omit bodies,
credentials, and JWTs. CL egress success is now explicitly logged.

The CLI builds and sends JSON via browser `fetch`; it does **not** populate the
visible form or click its submit button. Screenshots show rendered UI, not the
API payload, and cannot establish server-side CAPTCHA acceptance. Do not assume
the empty fields caused the rejection; that remains unproven.

Validation without another create request:

```text
python -m cbrs.registration register --count 1 --dry-run --screenshots
  22:12:01–22:12:36, port 19744, exit 0
  CL egress explicitly verified; bootstrap HTTP 200
  reCAPTCHA token: 2382 characters
  second RUT released back to scraped; nothing submitted
```

Five screenshots were saved under
`.cbrs/registration-profiles/reg-20261021-011201/screenshots/`:

1. `001-page_loaded.png`
2. `002-start_response_200.png`
3. `003-bootstrap_ok.png`
4. `004-signup_token_ready.png`
5. `005-before_close.png`

Screenshots 3 and 4 were visually inspected: the normal create-account page
renders, visible fields remain empty, and password fields are masked.
The live diagnostic proved capture works at dry-run stages. The rejected-create
capture path is covered offline; captures around real creation, activation, and
login have not been exercised live.

Tests after these changes: **49 registration + 12 DataImpulse + 31 account-pool
tests passed**. Three new tests cover opt-in capture and masking, cleanup after
capture failure, and capturing a rejected create response without logging
tokens/passwords. `git diff --check` passed for the changed code and tests.

No further real registration was attempted. Backend CAPTCHA rejection remains
unresolved; do not treat the successful dry run as proof that signup will be
accepted. Preserve the failed first RUT and the production environment while
deciding the next diagnostic attempt. Nothing was committed or deployed.

---

## 12. Both CAPTCHA providers connected to registration (2026-10-20)

The operator reminded us to use the repository's two existing providers. Review
of `browser_session.py` and `client.py` found a registration-specific gap: the
worker already supports browser → CapSolver → 2Captcha on explicit rejection,
but registration only called CapSolver when the browser could not generate a
token. A generated-but-rejected browser token stopped registration immediately;
registration did not load the 2Captcha key at all.

Added `register --external-captcha` for a deliberate bounded standalone run:

- A browser token remains first. Explicit JSON CAPTCHA rejection (HTTP 200/400)
  can advance to configured CapSolver, then configured 2Captcha, once each.
- Provider-side solve failure before submission can advance to the next provider.
- Success, ambiguous transport errors, timeouts, WAF/rate-limit HTTP statuses,
  and generic portal errors never advance or replay a submission.
- Retries preserve the same identity payload, temporary Chrome, and sticky exit.
- CapSolver receives the current browser proxy and user agent. Returned solver
  browser identity is aligned within the temporary context if provided.
- The existing 2Captcha Enterprise-v3 client remains proxyless; no browser proxy
  is added to its solver task.
- At most four paid tasks can be created per invocation. The existing
  `CaptchaBudgetStore` implementation keeps a separate, sanitized registration
  ledger at `.cbrs/registration/captcha-budget.sqlite3`, with the configured
  daily limit and rejection cooldown. Worker solver preferences and state are
  not modified. This ledger's daily limit is separate from the worker ledger.
- Without the explicit CLI flag, registration now stops on browser-token failure
  instead of silently buying the old CapSolver fallback.

Read-only provider health checks confirmed both configured keys authenticate and
both balances are positive. These checks created no paid tasks and printed no keys.

One live registration invocation then ran at 22:20:32–22:21:36 Santiago time:

```text
python -m cbrs.registration register --count 1 --screenshots --external-captcha
  temporary port 11508
  profile .cbrs/registration-profiles/reg-20261021-012032
  CL egress verified; bootstrap HTTP 200
  browser-token create: HTTP 400, code=captcha-rechazado
  CapSolver generated one signup token
  CapSolver-token create: HTTP 400, code=error, temporary_unavailable
  0/1 accounts fully verified; exit 1
```

The first failed identity was left untouched. This invocation used the second
identity, `<coworker-rut-2>`. Its RUT was released back to `scraped` because the generic
temporary portal error is retryable under the existing orchestrator rules.
**No 2Captcha task was created:** the second response was not an explicit
CAPTCHA rejection, so escalating to another token would replay a generic failed
request. A changed response code does not prove that CapSolver's token passed
server-side validation. No create acceptance, activation, or login was observed.

The sanitized solver ledger contains one CapSolver signup attempt:
`status=succeeded` (token generation), `portal_status=indeterminate`,
`portal_error_code=error`. Do not report that as a successful registration.

Nine masked screenshots were saved under this run's `screenshots/` directory;
`006-create_response_400.png` was visually inspected. The form still renders
empty, as expected for the API-based implementation. `008-create_response_400.png`
captures the page after the generic-error response; server outcome comes from
the response metadata, not the unchanged form.

Current aggregate state:

- RUT pool: `scraped: 29`, `failed:create: 1` (only the original first RUT).
- Account store: `failed: 2`, no verified accounts. No contents/passwords dumped.
- No export, `.env` edit, production pool edit, worker restart, or production
  browser/route change. Nothing committed or deployed.

Validation: **63 registration + 68 solver/client/browser + 43
DataImpulse/account-pool tests = 174 passed**; changed code/tests passed
`git diff --check`. Coverage includes bounded provider ordering, preserving the
identity, stopping on success/ambiguous errors, both providers rejected,
provider-generation failure, proxy contracts, task limits, sanitized audit
outcomes, and secret-free representations of both solver keys.

Signup remains unverified because of the generic portal error. No further live
attempt was made after that stop.

---

## 13. Visible Playwright form implementation (2026-10-20)

The operator requested filling real controls and submitting the visible form.
Registration now does that instead of constructing its initial create request
with browser `fetch`.

Read-only inspection used new registration-owned Chrome profiles. It confirmed
the live input IDs, labels, and public application code:

- Inputs: `email`, `emailConfirmar`, `password`, `passwordConfirmar`, `rut`,
  `nombres`, `apellidoPaterno`, `apellidoMaterno`, `direccion`, `celular`, `tos`.
- The SPA validates its model, runs Enterprise action `signup`, and posts its
  own model when the **Crear cuenta** button is clicked.
- The mobile input supplies prefix `+56 9` and applies mask `#### ####` to the
  remaining eight digits. The model contains that masked string, e.g.
  `8655 2408` (nine characters including a space), rather than the old manually
  supplied national-number string. Do not edit the protected pre-existing
  `identity.py`; conversion belongs at the UI boundary.
- Registration displays request errors inline. No modal dismissal is needed.

Implementation in `portal.py` and `orchestrator.py`:

1. `prepare_registration_form` fills ten real inputs with Playwright `fill`,
   blurs them, checks terms, reads every value back, verifies the checkbox, and
   saves `form_filled` screenshots with password fields masked.
2. Dry runs now exercise that filling step before generating a diagnostic
   signup token, then release the RUT without clicking submit.
3. Real registration clicks the SPA submit button and observes its own POST
   response. It does not generate a redundant browser token beforehand.
4. A route guard compares the outgoing native model with the intended identity
   before allowing transmission. It accepts the actual eight-digit mobile mask
   and refuses mismatched identity data. No payload is logged.
5. Only explicit CAPTCHA rejection permits the existing bounded solver chain.
   An external token replaces only the token/header in the next native button
   submission; the SPA still builds the rest of the body. Generic errors,
   unknown responses, and accepted requests are never replayed.
6. After accepted signup, the next batch identity navigates to a new visible
   form within the same temporary browser/profile/exit; the SPA success panel
   is not mistaken for an editable form. Activation and verification still use
   their existing API paths. No production lifecycle behavior changes.

Live dry run at 22:28:13–22:28:35 Santiago time:

```text
python -m cbrs.registration register --count 1 --dry-run --screenshots
  port 18231; profile reg-20261021-012813
  CL egress; bootstrap HTTP 200; all ten fields plus terms checked
  token length 2468; exit 0; no create request
```

Visually inspected `screenshots/004-form_filled.png`: names, RUT, matching
emails, address, masked passwords with all four requirements green, formatted
mobile number, and checked terms are present. The second RUT was released.

One controlled native-form registration at 22:31:35–22:31:58:

```text
python -m cbrs.registration register --count 1 --screenshots --external-captcha
  port 16025; profile reg-20261021-013135
  CL egress; bootstrap HTTP 200
  visible form populated and verified
  native button POST: HTTP 400, code=error, temporary_unavailable
  exit 1; 0/1 accounts verified
```

The screenshot after this request shows the filled form and the exact visible
error: **No se pudo registrar el usuario, intente más tarde.** See
`.cbrs/registration-profiles/reg-20261021-013135/screenshots/006-create_response_400.png`.
It was visually inspected. There was exactly one native create submission, no
generic-error replay, and no new paid solver task. The generic portal error
persists after genuine form filling; missing visible fields are no longer an
explanation for this latest failure. Its server-side cause remains unknown.

Checked state after the native attempt: `scraped: 29`, `failed:create: 1` in the
RUT pool; account-store aggregate `failed: 3`, no verified accounts. The first
failed identity remains untouched; the second is `scraped` again. Row 15's name
is unchanged. The registration CAPTCHA ledger still has only the earlier one
CapSolver task. No export or production wiring was performed.

Validation: **66 registration tests**, including three opt-in real-Chrome
fixtures against localhost only, plus **111 solver/client/browser/DataImpulse/
account-pool tests**. Local browser fixtures prove dry-run preparation sends no
request, the native button constructs the request, token replacement works,
mismatched identity data is blocked before transmission, and preparing a next
form does not replay an accepted create. Run them with
`CBRS_RUN_BROWSER_TESTS=1`; otherwise the three browser fixtures are skipped.

Existing unrelated edits, production processes/profiles/routes, `.env`, and the
runtime account-pool JSON remain untouched. Nothing committed or deployed.

---

## 14. Bounded registration IP recovery and live sweep (2026-10-20)

The operator explicitly requested applying the repository's IP-candidate
recovery approach until a registration route works. We reviewed
`jobs.py::_rotate_dataimpulse_route`, `_try_dataimpulse_candidate`, the candidate
allowance code, and `docs/dataimpulse-cbrs-operations.md`. We did **not** invoke
production recovery, migrate its database, quarantine any production exit, or
touch an owner/browser/service.

New opt-in standalone command:

```text
python -m cbrs.registration register --count 1 --recover-routes --screenshots [--external-captcha]
```

`--recover-routes` requires `--count 1`. Ordinary registration retains its
existing one-exit-per-run behavior. New modules:

- `cbrs/registration/routes.py`: read-only production exclusions plus a separate
  sanitized registration ledger at `.cbrs/registration/routes.sqlite3`.
- `cbrs/registration/recovery.py`: one stable identity across finite candidates
  and repeat passes, stopping immediately on accepted or unknown creation.

Recovery behavior:

1. Read active/pending production ports and current/recent production egress
   hashes using SQLite `mode=ro`. Missing/unreadable exclusions stop recovery
   before reserving a RUT. The ordinary sticky-port guard also now opens the
   production DB read-only.
2. Reserve one RUT and construct one identity. Its RUT, email, password,
   address, and mobile stay fixed across candidates. Subsequent passes reuse
   the saved credentials only after safe failed states; pending/activated/
   verified/unknown submissions are not retried.
3. Reserve candidate allowance durably, choose an unused port, and prove CL
   egress before opening its temporary Chrome. Hash the exit with the same
   12-character SHA-256 format as production preflight. Raw IPs, proxy URLs,
   credentials, and tokens are never stored in the route ledger or printed.
4. Skip production-used or previously attempted recovery exit hashes. Use a
   new registration-owned profile per candidate, then bootstrap, fill/check
   the native form, and submit through its own button.
5. An explicitly classified HTTP-400 signup rejection, an explicit JSON
   CAPTCHA rejection after the selected solver path, or the known generic
   client error with **no observed POST** and no invalid input can advance to
   a new exit. Data mismatches, unknown submission outcomes, WAF/rate-limit
   statuses, and terminal provider/authentication failures stop the run.
6. Close only a candidate created by registration. On accepted create, keep
   that exact candidate through activation/login/verification; never rotate
   or recreate the account after acceptance. Unknown create outcomes hold
   the RUT reserved and preserve a pending record for reconciliation.
7. Honor the existing configured candidate/pass, retry-delay, hourly allowance,
   and rolling-24-hour identity allowance values. Defaults are 10/pass,
   10-second portal delay, 30/hour, and 10 daily candidate submission
   reservations; this checkout actually resolves to **3/pass, 10 seconds,
   and 10 daily reservations**. No configuration value was changed.
8. `CaptchaBudgetStore` remains separate from production. At most four external
   solver reservations are allowed within each recovery invocation across all
   its candidates. Existing solver rejection cooldowns remain enforced;
   native-token-only passes can continue without buying another solver task.

### Live outcome

The sweep ran from **22:42:37 to 22:57:10 America/Santiago**. Five invocations
used the same second identity and email throughout, with **10 distinct ports
and 10 distinct CL exit hashes**, none matching the protected production exits.

- Eight candidates reached the create endpoint. Across native/solver retries,
  **11 create responses were captured, all HTTP 400**: eight explicit
  `captcha-rechazado` responses and three generic `code=error` signup rejections.
- Two candidates showed `Ha ocurrido un problema. Por favor, intente nuevamente.`
  before any registration POST was observed. Their filled controls had no
  invalid-input state; the screenshot evidence was inspected.
- Both providers were used. Recovery reserved four external solver attempts:
  one CapSolver token and two 2Captcha tokens were returned and rejected by
  the portal; another CapSolver attempt failed at the provider with `HTTP_400`
  before submission. The older pre-recovery CapSolver record is separate.
- The final pass stopped with **Registration daily identity submission
  allowance exhausted.** This is the existing rolling-24-hour candidate
  allowance, not a provider traffic limit or proof that every IP is bad.
- A `submitted=1` route-ledger flag reserves one candidate's potential
  submission before clicking. It conservatively counts a pre-POST client
  failure too; it does **not** mean exactly ten HTTP create requests occurred.
- No candidate created an accepted/verified account. No activation, export,
  production pool merge, `.env` edit, or service/browser-owner change occurred.

The route ledger's historical outcomes are:
`candidate_captcha_rejected: 4`, `candidate_create_rejected: 3`,
`candidate_no_submission: 1`, `terminal_stop: 2`. The two historical terminal
rows came from the initial conservative form/CAPTCHA handling, described below;
they were not rewritten after fixing recovery classification.

Run directories (under `.cbrs/registration-profiles/`):

- `recovery-20261021-014237-candidate-*` (two candidates)
- `recovery-20261021-014645-candidate-*` (one candidate; both solvers tried)
- `recovery-20261021-015033-candidate-*` (three native-token candidates)
- `recovery-20261021-015247-candidate-*` (three candidates; solvers on the second)
- `recovery-20261021-015519-candidate-01` (final no-POST client failure)

The final screenshot is
`recovery-20261021-015519-candidate-01/screenshots/006-create_ui_stopped.png`.
All screenshots and ledgers remain gitignored.

### Integration corrections during the sweep

The initial candidate loop stopped on a known pre-POST client error; it was
extended to advance only when that exact visible error is present, no input is
invalid, no guard mismatch occurred, and no request was sent.

The old single-route terminal CAPTCHA handling also incorrectly consumed the
second RUT after both providers were rejected. The recovery handler was fixed
to classify an explicit CAPTCHA rejection as a failed candidate. After checking
that the latest matching route-ledger row was `terminal_stop` with
`captcha-rechazado` and the saved record was the failed create, one guarded
transaction returned **only `<coworker-rut-2>` from `failed:create` to `scraped`**.
The credentials were retained. The original first RUT `<coworker-rut-1>` was left
untouched; no identities were parked or reinserted and no names were changed.

Final checked state: `scraped: 29`, `failed:create: 1` in the RUT pool;
account-store aggregate `failed: 4`, no verified accounts. The second and third
RUTs are `scraped`, row 15 is unchanged, and the protected production ports
10002/10403/12264 are still present. The one failed account record used by
recovery was reused across passes rather than creating a new record per IP.

Validation: **195 tests passed**, including 84 registration tests with four
opt-in real-Chrome localhost fixtures and 111 existing solver/client/browser/
DataImpulse/account-pool tests. Recovery coverage pins identity continuity,
cross-pass credential reuse, production exclusions/read-only access, duplicate
exits, budgets, accepted-create and unknown-create stops, CAPTCHA candidate
classification, proxy authentication stops, and no-POST client failure handling.
`git diff --check` and whitespace checks on the new modules passed.

**Unresolved:** changing among ten distinct DataImpulse exits did not establish
a working registration route. The IP-only explanation is unproven; a whole-pool
restriction or a portal/identity-specific cause cannot be distinguished by this
generic response alone. Do not bypass the allowance or repeat signup blindly.
Portal-side error details or an explicitly chosen alternative diagnostic are
needed before claiming the cause or a successful route. Nothing committed or
deployed; all production lifecycle boundaries remain in force.


## 15. Source-control and client delivery (2026-10-20)

The operator requested committing and pushing the accumulated registration
work to `master`, with a client-facing Markdown comparison against `f4ca36c`.
The delivery includes the five initial fixes, the reviewed pre-existing
registration inbox/identity changes without modifying them, subsequent
form/screenshot/provider/recovery changes, their tests, and documentation.

The client report is `docs/client-registration-changes-2026-10-20.md`.
It distinguishes the 195 passing tests from the unsuccessful live signup
results. Committing or pushing does not deploy to WSL, change runtime
configuration, restart a service, export accounts, or prove registration
acceptance. Private databases, profiles, screenshots, the original handoff,
and environment credentials remain outside Git.
