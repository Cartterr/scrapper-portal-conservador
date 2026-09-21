# Respuesta técnica al informe de pruebas del 17 de septiembre

Fuente: `INFORME-PRUEBAS-2026-09-17.md` (rama `handoff-review-fixes`, fusionada
en `master` sin conflictos junto con el informe del 16 y la revisión del 10).
Los cuatro commits del mandante quedaron integrados tal cual; los defectos
abiertos se corrigen en este mismo `master`. Todo lo descrito aquí tiene prueba
de regresión sin tráfico al portal (Chrome real sólo renderiza HTML local).

## Cambios del mandante integrados

| Commit | Contenido | Observación |
| --- | --- | --- |
| `6089bf6` | Rotación de salida ante `captcha-rechazado` en login (formulario rechazado visible, código explícito o `CBRS_LOGIN_CAPTCHA_ROTATE_AFTER` fallos en la misma ruta); `embedded_chrome_survives` limpia marcas de Chrome muerto vía `/proc`. | Integrado. El escaneo de `/proc` se reutiliza ahora para el cierre acotado de D9. |
| `168baeb` | Alias `anio` en CSV. | Integrado. |
| `1c5eefc` | Rechazo de credenciales durable (hash SHA-256 de usuario+contraseña, nunca el secreto). | Integrado. El hash vive en la base local del servicio con los permisos del runtime. |
| `a247837` | Ciclo del worker dentro de un bucle de recuperación; descarta sólo contextos cuya página está cerrada. | Integrado. |

## Defectos abiertos del informe y su corrección

| Defecto | Causa encontrada | Cambio |
| --- | --- | --- |
| D17 | El reconocimiento de modales exigía la oración exacta (con punto) y el título `Atención`. Cualquier variante del diálogo de límite diario dejaba el formulario bloqueado y el fallo se clasificaba como «no se pudo enviar», una vez por minuto y por trabajo pendiente. | Reconocimiento por frase (`se han agotado las consultas`, `límite diario`), cualquier título y botón de cierre habitual. Un modal de límite que bloquea el formulario marca la cuenta `held` sin enviar búsqueda; el observador pasivo lo detecta también en páginas inactivas. Un modal desconocido se registra (`search_form_blocked_by_dialog`, con título y primera línea) y se cierra para no repetir el bloqueo. |
| D16 | El reporte sólo mostraba `pending_quota` si todas las cuentas del pool, incluidas las deshabilitadas, tenían retención de cuota. | Las cuentas `disabled` no cuentan. Cuando todas las utilizables están retenidas, el worker deja el trabajo con `portal_quota_exhausted` y `next_run_at` en la liberación más próxima (sin reclamos cada minuto) y el reporte muestra `pending_quota` con `resume_at`. |
| D25 | El modal «No se pudo realizar búsqueda, intente nuevamente por favor» no existía en el clasificador. | Se reconoce (`search_failed_retry`), se cierra, y la búsqueda se trata como no registrada: cuota intacta y reintento inmediato en otra cuenta o en el pase siguiente. |
| D22 | Un resultado desconocido pasaba directo a conciliación manual y el reporte lo mostraba como `pending` genérico. | Tras un resultado desconocido el worker lee el panel «Recientes» de esa cuenta: si la inscripción no figura, el reintento es automático (`search_not_registered`); si figura o el panel no se pudo leer, el trabajo queda en `pending_reconciliation` con el comando exacto en `error`. La lectura ocurre justo después del fallo, cuando la entrada sería la más reciente de las diez que conserva el portal. |
| D26 | `resume_unconfirmed_jobs.py` abría en modo lectura la base de comandos del propietario externo, que no existe sin systemd. | `cbrs jobs reconcile JOB_ID [--apply]` (y el script, que ahora delega en la misma función) consultan esa base sólo si existe. |
| D23 | `interval_minutes` valía 5 por defecto cuando no hay `account-pool.json`. | Valor por defecto 0. `CBRS_REQUEST_DELAY_SECONDS` sigue separando las peticiones. |
| D24 | Un candidato por pase del worker; con la pausa de D23 eso era uno cada cinco minutos. | Con la cola vacía se prueban de forma consecutiva todos los candidatos configurados (`CBRS_DATAIMPULSE_CANDIDATES_PER_RECOVERY`), con la espera de 10 s entre rechazos del portal. Con trabajos pendientes en cuentas sanas se mantiene uno por pase para no bloquearlos detrás de la prueba de candidatos. |
| D9 | SIGTERM mataba el proceso sin cierre (huérfanos de Chrome); el cierre de Playwright podía quedarse esperando a una salida muerta. | SIGTERM se convierte en `KeyboardInterrupt` y sigue el mismo camino que Ctrl+C. El cierre del Chrome embebido tiene un plazo de 20 s; si vence, se terminan los procesos que usan los perfiles del runtime (SIGTERM, luego SIGKILL) y se repasa al final para que no quede ninguno. En modo externo el worker no toca el Chrome del propietario. |
| D12 | El mensaje citaba ambos comandos. | `service_start_hint()` elige según exista la unidad `cbrs-worker.service`: `cbrs service start worker` o `cbrs jobs worker (en otra terminal)`. |

## Hallazgo propio en el servicio productivo

El trabajo `job-20260914T002233Z-dc8da4b62f` (Foja 2198 · N° 1398 · 2013)
acumuló 1.872 intentos entre el 14 y el 20 de septiembre: la búsqueda aceptada
tenía referencias de página en caché y el portal respondía HTTP 404 al
descargarlas; cada intento pausaba la cuenta dos minutos y volvía a encolarse.
Ahora un 404 en la descarga del documento revalida el ticket una vez (descarta
las referencias en caché) y, si persiste, termina el trabajo como `failed` con
`document_unavailable` y la indicación de `--force`, sin pausar la cuenta. En
producción la revalidación bastó: las referencias en caché eran las caducas y el
trabajo terminó `completed` con su PDF de cinco páginas a los cinco minutos del
despliegue.

## Comportamiento nuevo visible para el operador

- Estados del reporte y de `Result.status`: `done`, `not_found`, `pending_quota`,
  `pending_reconciliation`, `pending`, `failed`. `get-batch` no espera por
  `pending_reconciliation`, igual que por `pending_quota`.
- `cbrs jobs reconcile JOB_ID` previsualiza; `--apply` autoriza una única cuenta
  distinta y reencola. Nunca aplicar si «Recientes» muestra la inscripción.
- Eventos nuevos: `search_not_registered`, `search_outcome_unknown_history`,
  `search_form_blocked_by_dialog`, `document_unavailable`.

## Validación reproducible

```bash
python -m pytest -q
```

Windows, Python 3.14.3: 609 pruebas pasaron, 4 omitidas (la terminación por
`/proc` es sólo Linux). `tests/test_informe_2026_09_17.py` cubre cada defecto
de la tabla y el bucle del 404. Los diálogos y el panel «Recientes» se prueban
con Chrome real sobre HTML local que reproduce la estructura observada en las
capturas del servicio (`headlessui-dialog-panel`, `Atención` / `Cerrar`, chips
`Foja X · N° Y · Z` con su atributo `data-firma="fna|foja|numero|ano|"`,
«Se conservan las últimas 10 búsquedas»).

Linux (servicio WSL Ubuntu 24.04, Python 3.14.6, despliegue autorizado del 20 de
septiembre): `tests/test_informe_2026_09_17.py` más las regresiones del mandante,
93 pruebas pasaron, incluida la terminación de procesos por `/proc`. Comprobado
en vivo sobre la página real, con el propietario detenido: el panel «Recientes»
lo sirve `/api/v1/user/recientes/clave/indice_com_recientes` (historial del
servidor, no del navegador) y el lector devolvió sus diez entradas. Los tres
trabajos en conciliación desde el 14 de septiembre no figuraban en el historial de
`ejecutivo_2`; se autorizaron con `cbrs jobs reconcile --apply` y terminaron
`completed` con PDF en otras cuentas en menos de seis minutos. En ese intervalo
`ejecutivo_1` mostró el diálogo de error del portal y la ruta se reemplazó por un
puerto nuevo con formulario autenticado en 50 segundos. Cola vacía al cierre:
81 trabajos completados, ninguno en espera.

## Límites que siguen vigentes

- Si «Recientes» lista la inscripción, la cuota ya se consumió y el resultado
  no puede recuperarse sin otra búsqueda; la decisión sigue siendo del operador.
- Si el panel no se puede leer (DOM distinto), el trabajo queda en
  `pending_reconciliation`, nunca se reintenta a ciegas.
- A11 (reanudación tras cuota) requiere observar una liberación real; el
  trabajo queda con `next_run_at` en esa hora y se reclama solo.
