# Informe de pruebas locales — 2026-09-30

Continúa a [INFORME-PRUEBAS-2026-09-22.md](INFORME-PRUEBAS-2026-09-22.md) y
verifica las respuestas [docs/handoff-fixes-2026-09-22.md](docs/handoff-fixes-2026-09-22.md)
y [docs/handoff-fixes-2026-09-24.md](docs/handoff-fixes-2026-09-24.md).
Base probada: `master` en `881480b`, sin cambios locales de código. Máquina:
Ubuntu nativo, sin systemd, `cbrs jobs worker` en primer plano, **headful sobre
Xvfb**. Horas locales (UTC−3) salvo indicación.

**Resultado global: el servicio funciona con cuentas nuevas y sin ventanas.**
Tres cuentas nuevas hicieron 30 búsquedas aceptadas (10 cada una, la cuota real
otra vez) y 30 PDF en unos 40 minutos. El tope diario de logins y la retención
por cuota se comportaron como se describe.

**Defecto principal (D32):** un CAPTCHA de búsqueda rechazado deja abierto en la
página el mismo diálogo «Atención / Se ha detectado un problema…». Un minuto
después, la observación pasiva lo lee como ruta comprometida y borra el perfil
autenticado, sin ninguna petición nueva al portal. Pasó tres veces en el día y
cada vez terminó en un login desde otra IP.

---

## 1. Configuración

- Cuentas: `ejecutivo_2` (la que quedaba) y **tres usuarios nuevos del
  mandante con ID nuevos**: `ejecutivo_4`, `ejecutivo_5`, `ejecutivo_6`
  (puertos 10003, 10004 y 10005). Los ID `ejecutivo_1` y `ejecutivo_3` se
  retiraron sin reutilizarlos, para no mezclar el estado de las cuentas dadas de
  baja (ruta comprometida, puertos rechazados, perfiles con cookies del usuario
  anterior) con los usuarios nuevos. Ver la pregunta 1 de la sección 7.
- `CBRS_DATAIMPULSE_MAX_CANDIDATE_LOGINS_PER_DAY=3`, siguiendo la recomendación
  de la respuesta del 22.
- `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS=ejecutivo_2,ejecutivo_4,ejecutivo_5,ejecutivo_6`.
- `CBRS_HEADLESS=0`; el worker corre sobre Xvfb con la misma pantalla que
  `deploy/cbrs-display.service`:
  `xvfb-run -n 99 -s "-screen 0 1366x900x24 -nolisten tcp" .venv/bin/cbrs jobs worker`.
  Sin `-s`, `xvfb-run` crea una pantalla de 640x480 a 8 bits; conviene
  documentarlo junto a la recomendación de Xvfb.
- Antes de empezar se cancelaron los 10 trabajos que quedaban en cola desde el 24.
- Lote: 100 inscripciones nuevas del mandante más 8 de esa cola cancelada
  (108 filas; 25 ya descargadas en pruebas anteriores). Primero un humo de 4
  filas, luego el lote completo con `get-batch`.
- Verificación previa, sin cupo: `cbrs config validate` correcto y
  `cbrs pool proxy-health --account` correcto para las tres cuentas nuevas
  (CL y tres salidas distintas). La de `ejecutivo_2` falló con HTTP 429 de
  `ipinfo.io` en dos intentos seguidos.

## 2. Resultados sobre las correcciones del 22 y el 24

| # | Resultado | Evidencia |
|---|---|---|
| A17 pytest | Pasa | 629 pasaron, 1 omitida (Linux, Python 3.14.7). |
| D29/D30 | Pasa (por diseño) | Headful por defecto; se corrió sobre Xvfb sin ventanas. No se intentó arrancar headless en vivo. |
| Punto 6 (tope de logins) | Pasa | `ejecutivo_2` hizo 3 logins de candidato (uno promovido y dos `captcha-rechazado`) y a las 15:23:28 quedó `retry_allowance_exhausted` hasta `2026-10-01T18:02:45Z`, con `dataimpulse_rotation_skipped`. Sin más logins. |
| D17 / cuota | Pasa | `daily_limit` en la búsqueda 11 de cada cuenta nueva (15:53:58, 15:54:50, 15:57:24): cupo liberado, `portal_quota_exhausted` con `browser_preserved: true`, cuenta `held` en menos de 1 s, reanudación en primera búsqueda + 24 h. |
| D25 | Pasa | 15:48:53, `ejecutivo_5`: «No se pudo realizar búsqueda», `search_not_registered` (`portal_history_listed: false`), cupo liberado, cuenta `login_pending`; volvió sin login nuevo. |
| Cambio de IP en el mismo puerto | Pasa | 15:51:18, `ejecutivo_5`: `residential_egress_rotated`; se aceptó la nueva línea de base sin login. |
| Registro de peticiones | Pasa | `.cbrs/runtime/logs/requests/<cuenta>/2026-09-30.jsonl` permitió reconstruir D32 al segundo (sección 5). |
| D27 (`resume_at` en el CSV) | No observado | Ver D34: las filas quedaron `pending` y no `pending_quota`. |
| D28, A11 | Pendiente | Las retenciones vencen el 01-10 entre las 15:17 y las 15:20. |
| D31 | No observado | No se interrumpió el worker durante una recuperación; terminó con SIGTERM a las 17:02 y cerró limpio (sin Chrome ni Xvfb restantes). |

## 3. Datos de la jornada

| Métrica | Valor |
|---|---|
| Búsquedas aceptadas | 30 (10 por cuenta nueva); `ejecutivo_2`, ninguna |
| PDF nuevos | 30; ningún `not_found` |
| Duración | Humo 15:16–15:21; lote 15:22–15:57 |
| Reporte al cierre | 55 `done`, 43 `pending`, 10 `failed` (D33) |
| CAPTCHA de búsqueda rechazado | 3 (15:20 `ejecutivo_2`; 15:28 y 15:42 `ejecutivo_5`), siempre con el cupo liberado y el trabajo en otra cuenta |
| Perfiles autenticados borrados | 3, los tres por D32 |
| Logins al portal (`/api/v1/auth/login`) | 10: `ejecutivo_2` 3 (1 aceptado), `ejecutivo_4` 3 (1 aceptado), `ejecutivo_5` 3 (3 aceptados), `ejecutivo_6` 1. Los 5 rechazos fueron `captcha-rechazado` en rutas candidatas. |

## 4. Cronología

| Hora | Cuenta | Evento |
|---|---|---|
| 15:02:45 | – | Arranca el worker. |
| 15:05:07 | ejecutivo_2 | Ruta del 24 comprometida: primer candidato (18990) promovido con formulario autenticado. |
| 15:05:25–29 | ejecutivo_4 | Preflight: la salida del puerto 10003 cambió desde la línea de base de las 14:09. Proxy-health falla por HTTP 503 de `ipinfo.io` (D35) → `account_gate_failed`. |
| 15:06–15:12 | ejecutivo_4 | Candidatos 11399 y 14089: `captcha-rechazado` en el login. 14428 promovido. Tres logins de un tope de tres antes de la primera búsqueda. |
| 15:16–15:21 | 4, 5, 6 | Humo: tres búsquedas y tres PDF, una por cuenta. |
| 15:20:41 | ejecutivo_2 | Búsqueda: `captcha_rejected`. El trabajo pasa a `ejecutivo_4` y termina bien. |
| 15:22:00 | ejecutivo_2 | D32: diálogo pasivo → perfil borrado. Candidatos 19634 y 19536 rechazados; tope alcanzado a las 15:23:28. |
| 15:28:14 | ejecutivo_5 | Búsqueda: `captcha_rejected`. |
| 15:29:17 | ejecutivo_5 | D32 → perfil borrado; 16842 sin conectividad (no cuenta); 17714 promovido a las 15:31. |
| 15:42:09 | ejecutivo_5 | Búsqueda: `captcha_rejected` en la ruta nueva. |
| 15:43:10 | ejecutivo_5 | D32 otra vez → perfil borrado; 15941 promovido a las 15:43:55. |
| 15:48:53 | ejecutivo_5 | D25: búsqueda no enviada, sin cupo. |
| 15:53–15:57 | 4, 6, 5 | `daily_limit` en la búsqueda 11 de cada una. |
| 15:57–17:02 | – | D34: 739 reclamos de trabajos en 65 minutos, sin búsquedas. |
| 17:02 | – | Worker detenido con SIGTERM; cierre limpio. |

## 5. D32 en detalle

Las tres veces siguió la misma secuencia; `ejecutivo_5` a las 15:28:

1. 15:28:13.819 — `POST /api/v1/comercio/indice/texto` → HTTP 400
   (`captcha_rejected`). La captura de error de las 15:28:14 ya muestra el
   diálogo exacto «Atención / Se ha detectado un problema, refresque la página e
   intente nuevamente. / Cerrar», con la sesión iniciada y «Recientes» cargado.
2. Se libera el cupo, `account_safety_stop` y el trabajo pasa a otra cuenta.
   El diálogo queda abierto.
3. Entre 15:28:14 y 15:29:17 el registro de peticiones de `ejecutivo_5` no tiene
   **ninguna** petición al portal.
4. 15:29:17 — `runtime_observation.sample_auth` (desde
   `_PersistentAccountBrowsers.reconcile`, cada `CBRS_BROWSER_HEALTHCHECK_SECONDS`)
   lee el mismo diálogo con `portal_dialog_reason` → `portal_error_dialog_detected`
   con `"passive": true, "after_submission": false, "verdict": "proxy_compromised"`.
5. 15:29:18 — `proxy_compromised_browser_discarded`
   (`contexts_closed: 1, profiles_removed: 1`) y rotación con login nuevo.

El informe del 22 (sección 6.2) ya mostró que ese mensaje viene en la respuesta
al `captcha-rechazado`, incluso desde una red sana. La «evidencia pasiva» de
la ruta es, entonces, el rastro del rechazo del CAPTCHA que el propio flujo de
búsqueda ya había clasificado. Como la cuenta ya quedó detenida por seguridad
(`captcha_rejected`), el diálogo no aporta información nueva. Aun así, cada
CAPTCHA rechazado termina en un perfil borrado y un login desde otra IP.

No sabemos si la ruta era mala: un reCAPTCHA de búsqueda rechazado puede deberse
a la reputación de la IP. Pero en `ejecutivo_5` las dos rutas rechazadas habían
aceptado 3 y 4 búsquedas antes del rechazo, y la regla de `AGENTS.md` pide la
firma completa como prueba de la ruta, no un rastro de un error ya manejado.

## 6. Defectos nuevos

| # | Defecto | Criterio | Estado |
|---|---|---|---|
| D32 | La observación pasiva toma como ruta comprometida el diálogo que dejó abierto un CAPTCHA de búsqueda rechazado (sección 5). `cbrs/runtime_observation.py:68` marca `entry.portal_error_dialog` ante cualquier diálogo visible; `cbrs/jobs.py:322-333` lo reporta como `portal_error_dialog_stop(..., evidence={"passive": True})`. Ni la rama `CAPTCHA_REJECTED` (`cbrs/jobs.py:5087`) cierra el diálogo ni se descarta su detección. | R5 | Abierto |
| D33 | Un trabajo cancelado bloquea su fila en `get-batch` para siempre. `Client.submit` (`cbrs/api.py:197-201`) excluye los cancelados al buscar uno reutilizable, pero luego `create_job` con la clave `get:F:N:A` (`cbrs/api.py:214`) devuelve el trabajo existente con esa clave (`cbrs/jobs.py:1131-1138`), que es el cancelado. `_result` lo informa como `failed`. Afectó a las 10 filas cuyos trabajos del 24 se cancelaron. Paliativo usado: `cbrs jobs enqueue` sin clave. | R2 | Abierto |
| D34 | Si una cuenta queda retenida por el tope de logins (`paused` / `retry_allowance_exhausted`) y las demás por cuota, los trabajos no pasan a `pending_quota`. El worker los deja en `waiting_capacity` / `waiting_authenticated_alternate` y reclama un trabajo cada ~5 s: 739 reclamos entre 15:57 y 17:02. El reporte los marca `pending` sin `resume_at`, porque `_result` (`cbrs/api.py:323-326`) sólo considera `held` o `portal_quota`, no esta retención. Quien lee el CSV no sabe cuándo volver (el objetivo de D27). | R2, R4 | Abierto |
| D35 | Un fallo del servicio de consulta de IP (`https://ipinfo.io/json`, `cbrs/preflight.py:24`) cuenta como proxy caído. A las 15:05:29, un HTTP 503 de `ipinfo.io` hizo fallar el gate de `ejecutivo_4` y disparó la rotación: tres logins de candidato, dos rechazados, que agotaron su tope del día antes de la primera búsqueda. `ipinfo.io` también respondió 429 dos veces a `ejecutivo_2` en la verificación previa. El portal y el script de reCAPTCHA respondían bien por esa misma salida. | R5 | Abierto |

## 7. Lo que se pide al desarrollador

1. **D32:** que el diálogo visible tras un `captcha_rejected` ya clasificado no
   cuente como evidencia pasiva de ruta comprometida. Por ejemplo, cerrarlo o
   recordar que pertenece a ese rechazo, o exigir la firma en una
   carga nueva de la página. Si el mandante quiere mantener la política del
   13-09 también en este caso, que sea una decisión explícita.
2. **D33:** que la clave de idempotencia de `get` no reutilice un trabajo
   cancelado, o que `get-batch` lo informe con el comando para volver a pedirlo.
3. **D34:** tratar la retención por tope de logins como una cuenta no
   disponible hasta `next_eligible_at`: `pending_quota` o un estado propio con
   `resume_at`, y sin bucle de reclamos.
4. **D35:** que un error del servicio de IP (429/503) no se cuente como fallo
   del proxy, o que se reintente o use una segunda fuente antes de rotar.
5. **Alta de cuentas:** documentar en `INSTALL.md` cómo reemplazar una cuenta
   dada de baja. Hoy sólo se explica agregar pares al `.env`. Proponemos: número
   nuevo nunca usado, entrada nueva en `account-pool.json` con puerto 9999+N,
   agregar el ID a `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS` si corresponde y no
   reutilizar el ID retirado, porque rutas, retenciones, rechazos, historial de
   salidas y perfiles van por ID. ¿Es el procedimiento correcto?
6. **`ejecutivo_2`:** rechazó su primera búsqueda del día y luego dos logins
   con `captcha-rechazado`. Mientras las tres cuentas nuevas buscan bien con el
   mismo código, esta cuenta muestra más rechazos. ¿Hay algo en su historial
   (rotaciones, sesión headless del 24, uso compartido) que el portal pueda
   estar penalizando?

## 8. Continuación

El 01-10 desde las ~15:20 se retoma el mismo lote, sin reenviar nada, para
verificar A11 y D28 y descargar las 53 filas pendientes (10 de ellas reencoladas
por D33).
