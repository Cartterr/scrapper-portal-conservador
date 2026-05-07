# CBRS Portal Facts

## Confirmed From Public CBRS Pages

- CBRS exposes portal workflows for Registro de Comercio, including copies, certificates, vigency certificates, society/power flows, and search/visualization of inscriptions.
- CBRS terms state the user account password is personal and confidential.
- For Comercio copies/certificates, the user is responsible for providing correct foja, numero, and ano. Wrong input can map to the wrong inscription and may not be refundable.
- CBRS login/registration pages publicly state that the site is protected by reCAPTCHA Enterprise.

## Confirmed From Local Repo Research

Local raw evidence lives under `research/cbrs-network/`.

- Primary new portal host: `https://nuevo-portal.conservador.cl`.
- Comercio index route observed:
  - `/consultas-en-linea/indices/indice-del-registro-de-comercio`
- Current production-ready adapter scope:
  - Comercio text search.
  - Comercio foja/numero/ano search.
  - Ticket validation.
  - Image refs.
  - Image downloads and PDF assembly.
- Disabled/unproven adapter scope:
  - Propiedad.
  - Document verification.
  - Planos.
  - Other account flows.
- Observed reCAPTCHA action for Comercio search and ticket validation:
  - `indice_com_texto`
- Observed search endpoint:
  - `POST /api/v1/comercio/indice/texto`
- Observed ticket endpoint:
  - `POST /api/v1/comercio/indice/fnaTicket`
- Observed image refs endpoint:
  - `POST /api/v1/comercio/indice/img`
- Observed image bytes endpoint:
  - `GET /api/v1/comercio/indice/img/{dataRef}`

## Local Probe Conclusions

- Search requires an authenticated session plus reCAPTCHA token.
- Missing auth returned `403`.
- Missing/bad reCAPTCHA returned app error `intente-mas-tarde`.
- `err-limite` exists as an app-level daily/account limit signal.
- The new portal is not documented as a public API. Treat endpoints as private portal implementation details.
- Root HTML and DNS evidence indicate Imperva/Incapsula front-door behavior.

## Security Handling

- Do not commit raw JWTs, cookies, reCAPTCHA tokens, ticket strings, account emails, passwords, HAR files, or browser profiles.
- Keep research summaries sanitized. Raw probe scripts may mention token variable names but must not contain real token values.
