# Professional Client Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the repository README into a professional Spanish client presentation with accurate, colorful architecture diagrams while preserving every runtime behavior.

**Architecture:** Keep `README.md` as the single public entry point and organize it from executive context to progressively deeper operational detail. Use GitHub-native Mermaid diagrams so the visuals remain versioned, accessible, and editable without adding generated assets or runtime dependencies.

**Tech Stack:** Markdown, GitHub Mermaid, Python 3.14, pytest, Git.

## Global Constraints

- Do not change Python files, tests, dependencies, runtime configuration, commands, routes, limits, defaults, or behavior.
- Do not include credentials, personal identifiers, proxy endpoints, API keys, tokens, or other secrets.
- Keep all diagram labels and client-facing explanations in Spanish.
- Preserve the current operational commands for `doctor`, `preflight`, `init`, `search`, `download`, `validate`, `soak`, and `pool`.
- State clearly that accounts are nominal and authorized, login and CAPTCHA handling are manual, and the system does not rotate identities or retry aggressively.

---

### Task 1: Rewrite the public README for a non-technical client

**Files:**
- Modify: `README.md`
- Reference: `docs/superpowers/specs/2026-07-10-documentacion-profesional-cliente-design.md`
- Reference: `docs/architecture.md`
- Reference: `docs/soak-testing.md`
- Reference: `cbrs/cli.py`

**Interfaces:**
- Consumes: Existing CLI commands, safety guarantees, account-pool behavior, output paths, and technology choices documented by the repository.
- Produces: A GitHub-rendered Spanish README that serves as both a client overview and an operational quick reference.

- [ ] **Step 1: Replace the opening with an executive presentation**

Use the title `# Plataforma de Consulta Documental CBRS` and introduce the solution as a controlled tool for authorized Commerce Registry searches, local PDF generation, audit evidence, and monitored multi-account operation. Add four concise value cards as a Markdown table: controlled automation, operational traceability, data protection, and preventive safety.

- [ ] **Step 2: Add the complete operation diagram**

Create a Mermaid `flowchart LR` showing these exact stages: `Operador autorizado`, `Doctor y preflight`, `Inicio de sesión manual`, `Consulta CBRS`, `Selección de resultados`, `Generación de PDF`, and `Reporte sanitizado`. Use `classDef` declarations with blue for people, violet for controls, cyan for portal operations, green for deliverables, and amber for audit evidence.

- [ ] **Step 3: Add the simplified architecture diagram**

Create a Mermaid `flowchart TB` with four labeled subgraphs: `Experiencia del operador`, `Núcleo de control`, `Acceso autorizado`, and `Resultados locales`. Connect CLI and dashboards to configuration, preflight, scheduler, browser profiles, CBRS, PDFs, SQLite, and sanitized reports. Use the same semantic color palette as the operation diagram.

- [ ] **Step 4: Add the safety-decision diagram**

Create a Mermaid `flowchart TD` beginning with `Solicitud controlada` and `Validaciones previas`. Branch on `¿Entorno seguro?` and `¿Respuesta normal?`; successful operations continue to search/download, while egress drift, invalid session, CAPTCHA, WAF, rate limits, or unexpected responses lead to `Pausa segura`, `Registro de evidencia`, and `Revisión manual`. Color success green, decisions amber, and safety stops red.

- [ ] **Step 5: Add the authorized multi-account model**

Create a Mermaid `flowchart LR` connecting the local scheduler to three authorized executives. Each executive must connect to a separate persistent Chrome profile and a separate fixed/sticky Chilean proxy route, then converge on the CBRS portal. State below the diagram that the model enforces isolated sessions and quotas and is not identity rotation or control evasion.

- [ ] **Step 6: Reorganize the operational reference**

Retain and clarify the existing installation, safe `.env` examples, proxy-health gate, manual initialization, query/download examples, validation, soak testing, account-pool commands, local dashboard URLs, output paths, stack, limitations, and next steps. Place these sections after the client-facing overview so essential technical details remain available without dominating the opening.

- [ ] **Step 7: Inspect the documentation-only diff**

Run:

```powershell
git diff -- README.md
git diff --check
git diff --name-only
```

Expected: the README contains four Spanish Mermaid diagrams, no whitespace errors, and no source-code changes.

### Task 2: Validate and publish the documentation

**Files:**
- Verify: `README.md`
- Verify: `docs/superpowers/specs/2026-07-10-documentacion-profesional-cliente-design.md`
- Verify: `docs/superpowers/plans/2026-07-10-documentacion-profesional-cliente.md`

**Interfaces:**
- Consumes: The completed README from Task 1.
- Produces: Verified Markdown, rendered Mermaid evidence, passing regression tests, a documentation commit, and a pushed GitHub branch.

- [ ] **Step 1: Validate the four Mermaid blocks**

Extract every `mermaid` fenced block into temporary `.mmd` files outside the repository and render each one to SVG using Mermaid CLI. Confirm exactly four blocks render successfully and delete the temporary output afterward.

- [ ] **Step 2: Run regression checks**

Run:

```powershell
python -m compileall -q cbrs tests
python -m pytest -q
```

Expected: compilation exits with code 0 and the full test suite reports zero failures.

- [ ] **Step 3: Confirm the final scope**

Run:

```powershell
git diff --check
git status -sb
git diff --name-only master...HEAD
```

Expected: only Markdown documentation is changed on `agent/professional-spanish-documentation`.

- [ ] **Step 4: Commit the implementation**

Run:

```powershell
git add README.md docs/superpowers/plans/2026-07-10-documentacion-profesional-cliente.md
git commit -m "Improve client-facing project documentation"
```

Expected: Git creates a documentation-only commit.

- [ ] **Step 5: Push the branch**

Run:

```powershell
git push -u origin agent/professional-spanish-documentation
```

Expected: the remote branch contains the design specification, implementation plan, and completed README.
