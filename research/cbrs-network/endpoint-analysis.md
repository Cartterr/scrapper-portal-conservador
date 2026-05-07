# CBRS New Portal Network Flow Notes

Captured on 2026-05-06 from a logged-in Chrome profile against:

`https://nuevo-portal.conservador.cl/consultas-en-linea/indices/indice-del-registro-de-comercio`

All tokens, cookies, account details, tickets, and long opaque IDs were redacted from saved artifacts.

## Step Flow

1. Open logged-in account page.
   - Confirmed login state at `/usuario/mi-cuenta`.
   - Page calls `POST /api/v1/user/me` with `Authorization: Bearer ...`.

2. Open commerce index.
   - Route: `/consultas-en-linea/indices/indice-del-registro-de-comercio`.
   - Visible modes:
     - `Foja, número y año`
     - `Razón social o nombre de socio`

3. Search by text.
   - Query used: `MBX Global`.
   - Search result count: `2`.
   - First result:
     - `id`: `1895596`
     - `foja`: `63244`
     - `num`: `27964`
     - `ano`: `2022`
     - `acto`: `Transformación`
     - `nombreSociedad`: `MBX GLOBAL SpA`
     - response included an opaque `ticket` string.

4. Validate ticket.
   - The first search result ticket was posted to `fnaTicket`.
   - Response normalized the record into FNA shape:
     - `foja`: `63244`
     - `numero`: `"27964"`
     - `ano`: `2022`
     - `isFna`: `true`
     - included a returned ticket.

5. Get image refs.
   - Returned `numberOfPages: 2`.
   - Returned `refs.length: 2`.
   - Each ref includes:
     - `pageNumber`
     - `dataRef`
     - `dataRefThumb`

6. Fetch first image.
   - Endpoint returned `200`.
   - Content-Type observed: `image/png`.
   - First page size observed: `102167` bytes.

7. Extended probes.
   - `POST /api/v1/auth/refresh` returned `200` with a new `token`.
   - FNA search against `foja=63244`, `numero=27964`, `ano=2022` returned the same first record.
   - No-result text search returned `200` with `[]`.
   - Missing reCAPTCHA and bad reCAPTCHA both returned `400` with `code=intente-mas-tarde`.
   - Missing auth returned `403`.
   - Both page images and the first thumbnail downloaded successfully.

## Endpoint Map

| Step | Endpoint | Method | Auth header | reCAPTCHA header | Body token |
|---|---:|---:|---:|---:|---:|
| Current user | `/api/v1/user/me` | `POST` | yes | no | no |
| Startup config | `/api/v1/home/start` | `POST` | no | no | no |
| Extras flag | `/api/v1/extras/1` | `GET` | yes | no | no |
| reCAPTCHA SDK | `https://www.google.com/recaptcha/enterprise.js?render=...` | `GET` | no | no | no |
| reCAPTCHA anchor | `https://www.google.com/recaptcha/enterprise/anchor?...` | `GET` | no | no | no |
| reCAPTCHA token mint | `https://www.google.com/recaptcha/enterprise/reload?k=...` | `POST` | no | no | Google protobuf payload |
| reCAPTCHA cleanup | `https://www.google.com/recaptcha/enterprise/clr?k=...` | `POST` | no | no | Google protobuf payload |
| Commerce text/FNA search | `/api/v1/comercio/indice/texto` | `POST` | yes | yes | `recaptchaToken` also in JSON body |
| Ticket validation | `/api/v1/comercio/indice/fnaTicket` | `POST` | yes | yes | opaque `ticket` |
| Image refs | `/api/v1/comercio/indice/img` | `POST` | yes | no | validated FNA/ticket object |
| Image bytes | `/api/v1/comercio/indice/img/{uuid}` | `GET` | no observed | no | no |
| Refresh JWT | `/api/v1/auth/refresh` | `POST` | no observed | no | `{}` |

## Payload Shapes

Text search request:

```json
{
  "foja": null,
  "numero": null,
  "ano": null,
  "texto": "MBX Global",
  "recaptchaToken": "[TOKEN]",
  "ticket": null,
  "titulosAnteriores": false,
  "comuna": null,
  "anoP": null,
  "origen": "texto"
}
```

FNA search appears to use the same endpoint with:

```json
{
  "foja": 63244,
  "numero": 27964,
  "ano": 2022,
  "texto": null,
  "recaptchaToken": "[TOKEN]",
  "ticket": null,
  "titulosAnteriores": false,
  "comuna": null,
  "anoP": null,
  "origen": "fna"
}
```

Ticket validation request:

```json
{
  "ticket": "[TICKET]"
}
```

Image refs request:

```json
{
  "foja": 63244,
  "numero": "27964",
  "ano": 2022,
  "isFna": true,
  "isTomo": null,
  "statusTomo": null,
  "numberOfPagesTomo": null,
  "messageTomo": null,
  "numberTomo": null,
  "ticket": "[TICKET]"
}
```

## Token and State Observations

- Auth token is stored in `localStorage` under `auth_cbrs_token` as a JSON string value containing the JWT.
- `auth_cbrs_stay_signed_in` is present in `localStorage` and cookie state.
- `auth_cbrs_token` is also present as a browser cookie.
- Search and ticket-validation require both:
  - `Authorization: Bearer <jwt>`
  - `recaptcha-token: <token>`
- Search also duplicates the reCAPTCHA token in body field `recaptchaToken`.
- `fnaTicket` did not include `recaptchaToken` in the JSON body during the direct replay, but did include the `recaptcha-token` header.
- `/api/v1/comercio/indice/img` required `Authorization: Bearer <jwt>` but no reCAPTCHA token.
- The image byte `GET /api/v1/comercio/indice/img/{uuid}` succeeded without an observed `Authorization`, `recaptcha-token`, or `Cookie` header in the Playwright request event. Treat that as observed behavior, not a guarantee.
- `/api/v1/auth/refresh` succeeded from the logged-in browser context with no observed `Authorization`, cookie, or reCAPTCHA request headers in Playwright's request event. It returned a fresh JWT-style `token` and `refreshToken: null`.
- Missing auth on the search endpoint returned `403` even with a valid reCAPTCHA token.
- Missing or invalid reCAPTCHA on the search endpoint returned the same application error:
  - HTTP `400`
  - `code`: `intente-mas-tarde`
  - `msg`: `Se ha detectado un problema, refresque la página e intente nuevamente.`

## Extended Probe Results

Text search:

```json
{
  "status": 200,
  "count": 2,
  "first": {
    "id": "1895596",
    "foja": 63244,
    "num": 27964,
    "ano": 2022,
    "acto": "Transformación",
    "nombreSociedad": "MBX GLOBAL SpA"
  }
}
```

FNA search:

```json
{
  "status": 200,
  "count": 1,
  "first": {
    "id": "1895596",
    "foja": 63244,
    "num": 27964,
    "ano": 2022,
    "acto": "Transformación",
    "nombreSociedad": "MBX GLOBAL SpA"
  }
}
```

No-result search:

```json
{
  "status": 200,
  "data": []
}
```

Image refs:

```json
{
  "status": true,
  "numberOfPages": 2,
  "refsCount": 2,
  "isTomo": false,
  "statusTomo": true
}
```

Image downloads:

```json
[
  { "pageNumber": 1, "status": 200, "contentType": "image/png", "size": 102167 },
  { "pageNumber": 2, "status": 200, "contentType": "image/png", "size": 94128 }
]
```

Thumbnail download:

```json
{
  "status": 200,
  "contentType": "image/png",
  "size": 9043
}
```

## Static Bundle Findings

The current portal bundle exposes additional API paths beyond the commerce-index flow. These were found by scanning downloaded JS assets, not all were live-exercised.

Second pass note: the recursive SPA asset map fetched 154 JS/CSS assets into a temp directory, extracted a sanitized route/endpoint inventory, and deleted the raw bundles afterward. Sanitized output: `spa-surface-map.json`.

Endpoint counts from the recursive map:

- `auth`: 4
- `startup`: 1
- `commerce`: 6
- `property`: 7
- `documents`: 1
- `online-query`: 3
- `fna-verification`: 1
- `electronic-notary`: 1
- `user`: 21
- `other`: 23

Commerce-index relevant paths:

- `/v1/comercio/indice/texto`
- `/v1/comercio/indice/fnaTicket`
- `/v1/comercio/indice/img`
- `/v1/comercio/indice/img/`
- `/v1/comercio/indice/imgTomo`
- `/v1/comercio/validaPlazoSocial`

Related index/document paths visible in the bundle:

- `/v1/propiedad/indice/base`
- `/v1/propiedad/indice/fna`
- `/v1/propiedad/indice/texto`
- `/v1/propiedad/indice/texto-extra-features`
- `/v1/propiedad/indice/img`
- `/v1/documentos/consultar`
- `/v1/fna/verifica/`
- `/v1/consulta-en-linea/verifica-doc/validaCodigo`
- `/v1/consulta-en-linea/verifica-doc/obtenerDocumento`
- `/v1/notarioElectronico/verifica`

Additional mapped paths worth explicit production decisions:

- `/v1/consulta-en-linea/estado`
- `/v1/planos/base-data`
- `/v1/planos/buscar`
- `/v1/planos/verPlano`
- `/v1/planos/descargarResolucion`
- `/v1/user/account/caratulas`
- `/v1/user/account/txs`
- `/v1/user/account/estado-masivo/*`
- `/v1/cart/price`
- `/v1/cart/tx`
- `/v1/pago/tx`
- `/v1/pago/diferencia`
- `/v1/reingreso/iniciar`
- `/v1/reingreso/finalizar`

Mapped SPA routes include:

- `/consultas-en-linea`
- `/consultas-en-linea/indices/indice-del-registro-de-comercio`
- `/consultas-en-linea/indices/indice-del-registro-de-propiedad`
- `/consultas-en-linea/planos/viewer`
- `/usuario/mi-cuenta/indice-propiedad-avanzado`
- `/usuario/mi-cuenta/caratulas`
- `/usuario/mi-cuenta/compras`
- `/usuario/mi-cuenta/estado-masivo`
- `/verificacion-funcionario`

Observed third-party/service hosts during the probe and asset scrape:

- `nuevo-portal.conservador.cl`
- `www.google.com` and `www.gstatic.com` for reCAPTCHA Enterprise
- `fonts.gstatic.com`
- `o90030.ingest.sentry.io` for Sentry telemetry
- `analytics.conservador.cl` / Plausible references in public pages
- `challenges.cloudflare.com` appears in static assets as Turnstile-related support, but it was not used by the commerce-index flow observed here.

## Public and Infrastructure Findings

Public search did not find an official API contract for the new portal endpoints. Public material confirms the CBRS portal exposes Registro de Comercio, index lookup, and document/transaction services, but describes them as portal workflows rather than reusable APIs.

Useful public references:

- `https://conservador.cl/portal/`
- `https://nuevo-portal.conservador.cl/consultas-en-linea/indices/indice-del-registro-de-comercio`
- `https://apec.sitefinity.cloud/apecapi/publication/getfile?publicationId=d2c6a825-3e54-43dc-a2bf-3bf47d6a87a1`

Infrastructure observations:

- DNS for `nuevo-portal.conservador.cl` resolves through an Imperva hostname (`*.impervadns.net`).
- TLS certificate observed for the edge was issued to `imperva.com`.
- The root HTML includes a CSP meta tag allowing scripts from `self`, `analytics.conservador.cl`, `challenges.cloudflare.com`, Google reCAPTCHA hosts, and `blob:`.
- The root HTML also loads an `/_Incapsula_Resource?...` script, consistent with Imperva/Incapsula front-door behavior.
- `curl` `HEAD /` returned `403` with `X-CDN: Imperva`; `curl` `GET /` returned `200`.
- `robots.txt` and `sitemap.xml` returned `404`.
- Direct server GET to the deep SPA route returned `404`; browser-side SPA navigation from the app worked. A production client should load the root app before route navigation instead of assuming server-side fallback for all routes.
- `OPTIONS /api/v1/comercio/indice/texto` returned `200`, but no useful CORS allow headers were observed from the terminal probe.
- Unauthenticated `POST /api/v1/home/start` returned `200`; unauthenticated `POST /api/v1/user/me` returned `403`; unauthenticated `POST /api/v1/auth/refresh` returned `401`.

## Long-Term Scraper Risks

- No public API contract: endpoints, payloads, response fields, ticket format, and path names are private implementation details extracted from the SPA bundle.
- reCAPTCHA Enterprise is on the critical path for search and ticket validation. Browser-side token generation is currently required for reliable operation.
- Imperva sits in front of the portal. Non-browser request shapes can be treated differently from real browser navigation.
- Auth state is browser/session-coupled. Refresh behavior worked only in the logged-in browser context and should not be treated as a stable standalone login API.
- Image byte URLs are opaque and ticket-derived. They worked without observed auth headers, but that may be CDN/backend behavior rather than a durable public contract.
- The commerce index is only one flow. The bundle exposes property index, document verification, notary-electronic verification, and other paths that were not fully exercised in this pass.
- Terms and paid-access boundaries matter. Public references describe free basic lookup mixed with paid/certified document access; production behavior should avoid bypassing payment or access controls.

## Implementation Implications

- The browser is still needed for reliable reCAPTCHA Enterprise token generation unless a future official flow removes it.
- The backend expects the reCAPTCHA action `indice_com_texto` for both search and ticket validation.
- `/home/start` returns useful runtime config:
  - `anoDesdeComercio`
  - `anoDesdePropiedad`
  - `indexMaxRows`
  - `indexMaxRows2`
  - portal banner metadata
- Avoid relying on UI clicks for the result action. The visible `Ver` control is an icon-only nested button with tooltip classes; the API sequence is clearer and more stable once a ticket is available.
- Store only sanitized diagnostics. Raw JWTs, cookies, reCAPTCHA tokens, ticket strings, and account profile data should never be committed.

## Second Pass Live Probe Limits

The active Playwright/codegen browser windows were still running, but they use `remote-debugging-pipe`, which is not attachable from a fresh script. Copying the full temp Chrome profile was blocked by large/locked browser files, so a minimal profile copy was tested instead. The minimal copy did not preserve a usable logged-in session.

Public route checks from the copied profile still confirmed:

- `/usuario/mi-cuenta` redirects to `/login` without auth state.
- `/consultas-en-linea/indices/indice-del-registro-de-comercio` loads as a SPA route.
- `/consultas-en-linea/indices/indice-del-registro-de-propiedad` loads as a SPA route.
- `/usuario/mi-cuenta/indice-propiedad-avanzado` redirects to `/login` without auth state.
- `/verificacion-de-documentos` and `/verificacion_documentos` resolved to the SPA's not-found title in this probe.

Auth-gated direct probes for property, document verification, image expiry, ticket expiry, and JWT refresh timing still need an active attachable logged-in browser session.

## Saved Artifacts

- `direct-api-result-summary.json`
- `static-bundle-endpoints.json`
- `static-api-endpoints.json`
- `spa-surface-map.json`
- `live-probe-summary.json`
- `static-assets/asset-index.json`

Raw browser profiles, full HTML dumps, and request/response event captures were deleted after distillation because they can contain account/session material.
