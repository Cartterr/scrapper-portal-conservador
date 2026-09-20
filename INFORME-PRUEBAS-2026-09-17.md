# Informe de pruebas locales — 2026-09-17

Continúa a [INFORME-PRUEBAS-2026-09-16.md](INFORME-PRUEBAS-2026-09-16.md).
Base probada: `origin/master` en `d3e2604` (respuesta técnica al informe del
16), más cinco commits propios en esta rama que se describen en la sección 6 y
que el contratista debe revisar. Máquina: Ubuntu nativo, sin systemd, sin
sudo, `cbrs jobs worker` en primer plano. Criterios: `CRITERIOS-DE-ACEPTACION.md`.

**Resultado global: el sistema descarga PDFs reales de forma automática.**
20 PDFs obtenidos en el día, con login automático, rotación de proxy y
failover entre cuentas funcionando. La cuota real del portal es **10
búsquedas por cuenta y día**. Quedan defectos de robustez y de reporte que se
listan en la sección 5, y la prueba A11 (reanudación tras cuota) se observa
mañana.

---

## 1. Qué cambió respecto al 16 de septiembre

El commit `d3e2604` del contratista corrigió D1–D6, D8, D10, D12 y D13 del
informe anterior. Verificado en vivo:

- Login automático: con los cinco defaults de Playwright omitidos, la cuenta 2
  se autenticó sola a los 2 minutos de arrancar. Primer login automático de
  toda la evaluación.
- `captcha-rechazado` ya se clasifica como tal: cooldown de 2 minutos, no
  pausa indefinida. `cbrs status` muestra motivo y hora.
- El fallback a 2Captcha se invoca tras un rechazo del formulario.
- Logs a stderr y a `.cbrs/runtime/logs/worker.log`.
- El arranque tras una caída ya no exige aprobar la línea base a mano.

## 2. Pruebas de aceptación ejecutadas

| Prueba | Resultado | Evidencia |
|---|---|---|
| A0 login automático (propuesta) | Parcial | Cuenta 2 sola; cuenta 3 solo tras la rotación agregada en `6089bf6`; cuenta 1 dada de baja, ver D7. |
| A2 PDF válido | Pasa | `F30282_N12784_A2024.pdf`, 3 páginas, contenido verificado visualmente. |
| A3 caché | Pasa | Segunda llamada en 0 s, misma ruta, cuota intacta. |
| A5 argumentos inválidos | Pasa | `--ano abc` y `--fojas 0` devuelven 2 con mensaje. |
| A6 servicio caído | Pasa | Código 2. El mensaje aún cita `cbrs service start worker`. |
| A7 lote mixto | Pasa | 3 `done` (1 caché, 2 nuevas en cuentas distintas), 1 `failed` con línea, código 1, columna extra conservada. |
| A8 repetir lote | Pasa | 0 s, sin descargas nuevas. |
| A9 API Python | Pasa | `Client().get(...)` y `status()`. |
| A10 lote mayor que la cuota | Pasa con reservas | 30 inscripciones: 15 `done`, 1 `not_found`, 14 pendientes al agotar las dos cuentas. Ver D16 y D17. |
| A11 reanudación tras cuota | Pendiente | Cuentas `held` hasta 2026-09-18 03:31 y 05:33 UTC. Se observa mañana. |
| A12 proxy bloqueado | Pasa | Rotaciones automáticas por diálogo de error del portal y por captcha rechazado en login (con `6089bf6`). |
| A13 matar Chrome | No ejecutada | Un contexto cerrado provocó el D18 en vez de recuperarse. |
| A14 Ctrl+C | Falla | SIGINT no detiene el worker con Chrome abierto en 45–90 s. SIGTERM sale en 1 s y deja Chrome huérfanos. |
| A17 pytest | Pasa | 585 tests, 1 omitido, con los cambios de esta rama. |
| A19 quitar cuenta | Pasa | Se eliminó la cuenta 1 del `.env`; el worker arrancó con 2. |
| A20 sin claves captcha | Pasa | Todo el lote de 30 corrió con `CBRS_CAPTCHA_SOLVER_MODE=browser`. |

## 3. Datos del portal confirmados

- **Cuota**: 10 búsquedas aceptadas por cuenta y día. La cuenta 2 recibió
  `daily_limit` (HTTP 400) en la búsqueda 11 a las 14:46 UTC; la cuenta 3
  igual a las 15:57 UTC. La ventana de 24 horas desde el primer éxito que
  implementa el servicio coincidió con el comportamiento observado.
- Además del diálogo "Se ha detectado un problema, refresque la página",
  el portal muestra "No se pudo realizar búsqueda, intente nuevamente por
  favor" cuando la petición no llega a responderse. Captura en
  `.cbrs/runtime/pool/error-screenshots/error-d6b96a78f276cb4a058158a39cb40607.jpg`.
- Una inscripción inexistente (fojas 19428, nº 16043, 1993) devuelve lista
  vacía y el portal muestra el diálogo con el texto literal `null`. Se reporta
  `not_found` sin reintento.
- Los diálogos de error del portal ("Se ha detectado un problema...") en
  búsquedas fueron frecuentes: la salida se rota y otra cuenta toma el trabajo.
- Firefox con la misma cuenta y el mismo puerto de DataImpulse entra sin
  problema. El rechazo de reCAPTCHA depende del navegador automatizado, no del
  proxy ni de las credenciales.

## 4. Rendimiento observado

| Métrica | Valor |
|---|---|
| PDFs del día | 20 (4 sueltos + 16 del lote) |
| Tiempo por inscripción | 25 s a 2 min de trabajo real |
| Pausa entre trabajos | 5 min fijos (`interval_minutes` por defecto) |
| Rendimiento efectivo | 10 a 12 PDFs por hora, con cuentas disponibles |
| Rotaciones de proxy | 8 en el día, 6 exitosas |
| Tokens 2Captcha | 28 consumidos sin ningún éxito; luego desactivado |

## 5. Defectos nuevos (continúa la numeración del 16)

| # | Defecto | Criterio | Estado |
|---|---|---|---|
| D15 | Rotación por captcha rechazado en login nunca se disparaba: exigía una alerta visible que el login no produce. Las cuentas quedaban en bucle de 2 minutos por el mismo puerto. | R5 | Corregido en `6089bf6` |
| D16 | El reporte de lote marca `pending` con "esperando cuenta alternativa" cuando ambas cuentas están `held` por cuota. Debe ser `pending_quota` con la hora de reanudación. | R2 | Abierto |
| D17 | Tras la décima búsqueda, el diálogo de límite diario bloquea el formulario y el sistema lo clasifica como "no se pudo enviar; cuenta alternativa". Con la otra cuenta agotada entra en un bucle de intentos cada 5 s (más de 350 en 36 min). Solo detecta el límite si vuelve a enviar una búsqueda, lo que ocurrió tras reiniciar el worker. | R5 | Abierto |
| D18 | Ante una excepción del ciclo principal (contexto de Chrome cerrado), el worker queda en espera indefinida con heartbeat y lease activos; `cbrs status` dice "arriba". Ocurrió el 16 (12 horas) y el 17 (16:47). | R4 | Corregido en `a247837` |
| D19 | Al arrancar tras una caída, la base de datos decía que Chrome seguía vivo sin ningún proceso; el worker exigía un "handoff" que no existe. | R4 | Corregido en `6089bf6` |
| D20 | Cuenta dada de baja: el `disabled` no persistía entre reinicios; cada run volvía a intentarla (60 logins en un día) con un Chrome permanente. | R5 | Corregido en `1c5eefc` |
| D21 | Cabecera `anio` rechazada en CSV. | R2 | Corregido en `168baeb` |
| D22 | Una búsqueda con resultado ambiguo (fojas 29929, nº 13757, 2022) queda en `search_reconciliation_required` y exige un script manual. El reporte la muestra como `pending` genérico. | R5 | Abierto, decisión pendiente |
| D23 | Pausa fija de 5 minutos entre trabajos en instalaciones configuradas solo por `.env`. El JSON de ejemplo trae 0. | R1/R2 | Abierto |
| D24 | La recuperación de rutas comprometidas prueba un candidato cada 5 minutos aunque haya 10 permitidos. La cuenta 3 estuvo 40 minutos fuera por tres candidatos rechazados. | R5 | Abierto |
| D9 | Ctrl+C sigue sin detener el worker en menos de 30 s. | R4 | Abierto |
| D25 | El diálogo del portal "Atención: No se pudo realizar búsqueda, intente nuevamente por favor" no se reconoce. Tras el clic, la búsqueda quedó con resultado desconocido y el trabajo exige conciliación manual. En la misma captura, el panel "Recientes" del portal no lista la inscripción, prueba de que no se registró ni consumió cuota: esa comprobación puede automatizarse. | R5 | Abierto |
| D26 | `deploy/resume_unconfirmed_jobs.py`, la única vía documentada para conciliar, falla en modo embebido ("unable to open database file"): busca la base de comandos del propietario externo, que solo existe con systemd. Sin systemd no hay forma de destrabar el D22. | R5 | Abierto |

## 6. Cambios de código en esta rama, para revisión del contratista

Todos con tests de regresión y la suite completa en verde. Mantienen los
gates de preservación existentes (alcance explícito, sesión protegida,
presupuestos). El contratista decide si los integra tal cual o los rehace.

1. **`6089bf6` Rotar salida ante captcha rechazado en login.** Nueva función
   `_login_captcha_rotation_due` en `cbrs/jobs.py`: acepta como evidencia el
   formulario rechazado visible (regla original), la respuesta explícita
   `captcha-rechazado` con HTTP 400, o `CBRS_LOGIN_CAPTCHA_ROTATE_AFTER`
   (3) fallos acumulados en la misma ruta. Un fallo del solver externo cuenta
   igual y rota. En `cbrs/worker_lock.py`, `embedded_chrome_survives` revisa
   `/proc` buscando procesos con un perfil de cuenta; si no hay ninguno,
   limpia las marcas `browser_live` y toma el lease.
2. **`168baeb` Alias `anio`** en `cbrs/api.py`.
3. **`1c5eefc` Credenciales rechazadas durables.** Tabla
   `account_credential_rejections` con hash SHA-256 de usuario y contraseña
   (nunca el secreto). El gate de arranque pausa la cuenta como
   `credentials_invalid` antes del preflight y sin abrir Chrome. Un par
   distinto en `.env` descarta el registro.
4. **`a247837` Recuperación del ciclo del worker.** El ciclo principal queda
   dentro de un bucle de recuperación: ante una excepción registra
   `worker_loop_failed`, descarta solo contextos cuya página está cerrada
   (`discard_closed_contexts`), marca el run `worker_failed_recovering` y
   reintenta tras `CBRS_WORKER_RECOVER_SECONDS` (30). Las sesiones vivas no se
   tocan. Sustituye la espera indefinida hasta detención manual.

## 7. Lo que se pide al contratista

En orden de prioridad:

1. Revisar e integrar (o rehacer) los cuatro cambios de la sección 6.
2. D17: reconocer el diálogo de límite diario cuando bloquea el formulario y
   marcar la cuenta `held` sin necesidad de otra búsqueda. Sin esto, con una
   cuenta agotada el sistema puede quedar en bucle.
3. D16: `pending_quota` y `resume_at` en el reporte cuando la espera es por
   cuota del portal.
4. D22, D25 y D26: reconocer el diálogo "No se pudo realizar búsqueda" como
   fallo previo al envío; ante un resultado desconocido, comprobar en el panel
   "Recientes" del portal si la inscripción quedó registrada y decidir solo el
   reintento; y hacer que el script de conciliación funcione sin propietario
   externo mientras exista. Si la conciliación manual se mantiene, el reporte
   debe decirlo con un estado propio y el operador debe recibir el comando
   exacto.
5. D23 y D24: valores por defecto de ritmo. Sin pausa de 5 minutos entre
   trabajos cuando hay cuentas disponibles, y candidatos consecutivos sin
   esperar 5 minutos en la recuperación de rutas.
6. D9: salida limpia con Ctrl+C.
7. Mensaje de servicio caído acorde al modo de ejecución (D12 parcial).

## 8. Estado de la máquina al cierre (17:00 UTC)

- Worker corriendo con el código de esta rama. Cuentas 2 y 3 `held` hasta
  mañana; 13 trabajos pendientes por cuota y 1 por conciliación.
- `.env`: cuenta 1 eliminada, `CBRS_CAPTCHA_SOLVER_MODE=browser`,
  `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS=ejecutivo_2,ejecutivo_3`,
  10 candidatos y 30 rotaciones por hora.
- PDFs: `outputs/pdf/` (2) y `~/cbrs-pdfs/` (18); originales en
  `.cbrs/runtime/outputs/jobs/`.
