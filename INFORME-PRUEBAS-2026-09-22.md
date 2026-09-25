# Informe de pruebas locales — 2026-09-22/24

Continúa a [INFORME-PRUEBAS-2026-09-17.md](INFORME-PRUEBAS-2026-09-17.md) y
verifica la respuesta [docs/handoff-fixes-2026-09-17.md](docs/handoff-fixes-2026-09-17.md).
Base probada: `master` en `5e9183d`, sin cambios locales de código. Máquina:
Ubuntu nativo, sin systemd, `cbrs jobs worker` en primer plano, Chrome visible.

**Resultado global: las correcciones del 17 funcionan en vivo.** D9, D16, D17,
D22, D23 y D24 se observaron con tráfico real y se comportaron como se
describe. A11 también pasa: el trabajo retenido por cuota terminó solo al día
siguiente. Quedan dos defectos menores (D27, D28).

**Headless no sirve con el portal (sección 6):** desde la misma red y con la
misma cuenta, headful busca y headless recibe `captcha-rechazado`. Ese rechazo
dispara la recuperación de ruta comprometida, que borra perfiles sin necesidad
(D29, D30). Para correr sin ventanas en Linux: headful sobre Xvfb.

**El portal dio de baja `ejecutivo_3` el 24 (sección 6.4).** Queda una sola
cuenta activa. Además, SIGINT cuelga el worker durante una recuperación de ruta
(D31).

---

## 1. Configuración

- `.env`: se quitó `ejecutivo_1` (cuenta dada de baja, ver D7/D20); 2 cuentas.
  También se quitó de `.cbrs/runtime/account-pool.json`, que conserva los
  puertos fijados de cada cuenta.
- Límites de recuperación de rutas: los valores de `master` (10 candidatos, 30
  rotaciones por hora, 10 s entre rechazos, 60 s de cooldown). Se comentaron los
  valores antiguos 1/3/300/300 del `.env`.
- `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS=ejecutivo_2,ejecutivo_3`,
  `CBRS_CAPTCHA_SOLVER_MODE=browser`.
- Lote: 34 filas. 32 trabajos de la prueba de estrés del 11 de septiembre (13 ya
  completados, 19 sin buscar), reutilizados por `get-batch` sin duplicar, más
  `29929/13757/2022` (la búsqueda ambigua de D22) y `19428/16043/1993`
  (inexistente). En total, 21 búsquedas nuevas para 20 cupos reales.

## 2. Resultados por defecto

| # | Resultado | Evidencia |
|---|---|---|
| A17 pytest | Pasa | 612 pasaron, 1 omitida (Linux, Python 3.14.7). |
| D12 | Pasa | Con el worker detenido, el mensaje dice `cbrs jobs worker (en otra terminal)`. |
| D15 (regresión) | Pasa | Al arrancar, `ejecutivo_2` recibió `captcha_rejected` en el login y la ruta rotó sola (22:34 → 22:35). |
| D23 | Pasa | Aproximadamente un minuto entre trabajos, sin la pausa de 5 minutos. |
| Caché / A8 | Pasa | Las 13 filas ya descargadas volvieron sin búsqueda. Repetir el lote: 0 s, 0 búsquedas nuevas, mismo reporte. |
| D17 | Pasa | `ejecutivo_2` (23:01:14) y `ejecutivo_3` (23:07:19) recibieron `daily_limit`: cuota liberada, `portal_quota_exhausted`, cuenta `held` en menos de 1 s y el trabajo pasó a la otra cuenta. Ningún bucle: sin líneas nuevas en el log entre 23:07 y 22:36 del día siguiente. El diálogo «Se han agotado las consultas disponibles por hoy» quedó visible en ambas ventanas, sin efecto. |
| D16 | Pasa, con D27 | La última fila queda `pending_quota` con «Todas las cuentas tienen cuota del portal agotada». `get-batch` terminó con código 1 e imprimió `resume_at=2026-09-24T01:36:51+00:00`. El trabajo espera con `next_run_at` sin reclamos cada minuto. |
| D24 | Pasa | Cuatro reemplazos de ruta comprometida: 78 s, ~3 min, ~5 min y 1 min 44 s. Los candidatos se prueban con 1 o 2 minutos de separación, no 5. |
| D22 | Pasa | 23-09 22:39:40, `ejecutivo_3`: `search_not_submitted` → lectura de «Recientes» → `search_not_registered`, cuota liberada y reintento automático, sin conciliación manual. |
| D9 | Pasa | SIGINT: el worker salió en 1,0 s. SIGTERM: salió en 1,0 s. En los dos casos no quedó ningún proceso de Chrome con perfiles de `.cbrs/runtime`. Al reiniciar, las cuentas conservaron `held`. |
| D25, D26 | No observadas | El portal no mostró «No se pudo realizar búsqueda» ni hubo filas `pending_reconciliation`. |
| A11 | Pasa, con D28 | Ver sección 5. |

## 3. Datos de la jornada

| Métrica | Valor |
|---|---|
| Búsquedas aceptadas | 20 (10 por cuenta, la cuota real confirmada otra vez) |
| Resultado | 18 PDFs y 2 `not_found` (`23972/9310/2026` del lote de estrés y `19428/16043/1993`) |
| Reporte al cierre del 22 | 31 `done`, 2 `not_found`, 1 `pending_quota` |
| Reporte final (23, 23:40) | 32 `done`, 2 `not_found` |
| CAPTCHA de búsqueda rechazado | 3 veces el 22, 1 el 23. Siempre con la cuota liberada y el trabajo en la otra cuenta. |
| Diálogo de error del portal | 4 veces el 22, 1 el 23. Todos terminaron en ruta nueva con formulario autenticado. |
| Duración | 30 minutos para las 21 búsquedas, con dos recuperaciones de ruta incluidas |

## 4. Defectos nuevos

| # | Defecto | Criterio | Estado |
|---|---|---|---|
| D27 | El CSV de `get-batch` no tiene columna `resume_at`. La hora sólo sale por la terminal (`cbrs/download_cli.py:76`); `REPORT_FIELDS` en `cbrs/api.py:98` no la incluye. La respuesta del 17 dice «el reporte muestra `pending_quota` con `resume_at`». Quien sólo recibe el CSV no sabe cuándo volver. | R2 | Abierto |
| D28 | La comprobación de cuota vencida se gasta aunque el portal nunca responda sobre la cuota. `admit_quota_check` (`cbrs/form_search.py:104`) adelanta `next_check_at` una hora al admitir la prueba, y sólo la devuelve si el fallo es `AUTH_REQUIRED` (`form_search.py:352-363`). El 23 a las 22:36 vencieron ambas retenciones: la prueba de `ejecutivo_2` terminó en `captcha_rejected` y la de `ejecutivo_3` en `search_not_submitted`, las dos con la cuota liberada y sin respuesta de cuota. Aun así las dos cuentas quedaron `held` hasta las 23:37/23:39. Una prueba que no llega al formulario de resultados (CAPTCHA rechazado, búsqueda no enviada, ruta comprometida) debería devolver la admisión, igual que el caso de autenticación. | R5 | Abierto |

Observación sin número: rechazar un candidato de ruta en el login puede tardar
3,5 minutos (23:01:31 → 23:04:56). No bloquea nada porque ocurre con las demás
cuentas ya retenidas.

## 5. A11: reanudación tras cuota

Pasa, con una hora de retraso por D28.

- 23-09 22:36:56: venció la retención. El worker reclamó solo el trabajo
  `29929/13757/2022`, sin reenviar el lote.
- Los dos intentos fallaron por causas ajenas a la cuota (ver D28), y el
  trabajo esperó la siguiente comprobación.
- 23:37:02: segunda comprobación. `ejecutivo_2` buscó y el trabajo terminó
  `completed` a las 23:38:19, con `F29929_N13757_A2022.pdf` (6 intentos en
  total, ninguno con cuota consumida sin resultado). Las dos cuentas quedaron
  `available`.
- Repetir `get-batch` sin reenviar nada: 32 `done`, 2 `not_found`, 0
  pendientes, código 0.

## 6. Pruebas del 24: headless

Objetivo: saber si el worker puede correr sin ventanas. Todo en la misma
máquina y con el mismo `master`.

### 6.1 Worker sobre DataImpulse

| Hora | Modo | Resultado |
|---|---|---|
| 11:07–11:15 | `cbrs --headless jobs worker` | Primeras dos búsquedas: `temporary_unavailable` (`ejecutivo_3`) y `captcha_rejected` (`ejecutivo_2`). Diálogo de error, se descartó el perfil de `ejecutivo_2` y 3 candidatos seguidos fueron rechazados en el login. Se detuvo el worker. |
| 16:06–16:08 | headful (`CBRS_WINDOW_MODE=offscreen`) | Candidato de `ejecutivo_2` rechazado en el login. `ejecutivo_3`, con su sesión de ayer, tuvo el CAPTCHA de búsqueda rechazado; apareció el diálogo de error y se descartó su perfil. Se detuvo el worker. |

Ese día las rutas de DataImpulse fallaban también en headful, así que estas
corridas no separan el modo de la red. Ninguna consumió cuota. Las dos cuentas
perdieron su perfil y necesitan login nuevo.

`CBRS_WINDOW_MODE=offscreen` no oculta la ventana en Linux/X11: el gestor de
ventanas reubica `--window-position=-32000,-32000` dentro de la pantalla.

### 6.2 Control sin proxy desde la red local (Gtd, CL)

`ejecutivo_2`, script aislado con `CBRSScraper`, `CBRS_EGRESS_MODE=personal_direct`
y un perfil nuevo en `.cbrs/tmp/`, una búsqueda por modo:

| Navegador | `navigator.userAgent` | Login | Búsqueda |
|---|---|---|---|
| Firefox manual | – | ✅ | ✅ |
| Chrome automatizado, headful | `Chrome/152`, `webdriver: true` | ✅ 25 s | ✅ 6 s (`30282/12784/2024`) |
| Chrome automatizado, headless | `HeadlessChrome/152`, `webdriver: true` | ✅ 2 s | ❌ HTTP 400 `captcha-rechazado` (`47092/32694/2010`) |
| Chrome automatizado, headful (repetición, después del rechazo headless) | `Chrome/152` | ✅ 2 s | ✅ 6 s (`47092/32694/2010`, la misma inscripción) |

Después, headful sobre DataImpulse, con puertos nuevos y perfil aparte:

| Puerto | Salida | Login | Búsqueda |
|---|---|---|---|
| 16099 | CL, Telefónica (AS16629) | ❌ `captcha_rejected` en 36 s | – |
| 12024 | CL, WOM (AS52341) | ✅ 36 s | ✅ 7 s (`59492/22933/2025`) |

Conclusiones:

- Con la red sana, el reCAPTCHA de búsqueda rechaza headless y acepta headful,
  incluso con la misma inscripción y la misma cuenta, antes y después del
  rechazo. `navigator.webdriver: true` por sí solo no provoca el rechazo.
- Los fallos del 24 sobre DataImpulse en headful vinieron de las rutas: la
  calidad cambia por IP (la de Telefónica fue rechazada y la de WOM pasó), y la
  misma cuenta funcionó sin proxy.
- La respuesta al rechazo headless trae el mensaje «Se ha detectado un
  problema, refresque la página e intente nuevamente.», el mismo que el servicio
  trata como prueba de ruta comprometida (`docs/portal-error-dialog.md`).

### 6.3 Defectos

| # | Defecto | Criterio | Estado |
|---|---|---|---|
| D29 | El valor por defecto es headless: `DEFAULT_HEADLESS = True` (`cbrs/config.py:28`) y `.env.example` sugiere `CBRS_HEADLESS=1`. El instalador de Ubuntu sí fija `CBRS_HEADLESS=0` sobre Xvfb (`deploy/cbrs-display.service`), pero el uso como biblioteca o CLI sin instalador queda en un modo que el portal rechaza. | R1 | Abierto |
| D30 | Con un navegador que el portal rechaza (headless), cada búsqueda produce el diálogo de error y el servicio lo trata como ruta comprometida: descarta el Chrome, borra el perfil y rota la ruta. El cambio no arregla nada y gasta rutas y perfiles; el 24 se perdieron dos perfiles así. La regla de `AGENTS.md` asume que la causa es siempre la ruta. | R5 | Abierto |

### 6.4 Reintento headful del worker y baja de `ejecutivo_3`

19:34, `cbrs jobs worker` headful normal, con las 10 inscripciones en cola y
las dos cuentas sin perfil:

| Hora | Cuenta | Evento |
|---|---|---|
| 19:35:04 | ejecutivo_2 | Ruta nueva al primer candidato. |
| 19:35:26 | ejecutivo_3 | Diálogo de error en la ruta vieja (18325). |
| 19:35:33 | ejecutivo_2 | Primera búsqueda: `captcha_rejected`. Diálogo de error, se descartó el Chrome. |
| 19:36:10 | ejecutivo_3 | Candidato 1 (10491): `captcha-rechazado` en el login. |
| 19:37:00 | ejecutivo_3 | Candidato 2 (14435): HTTP 401 `auth-exception`, `terminal: true`. Cuenta `disabled` / `credentials_invalid`. |
| 19:37:06 | ejecutivo_2 | Diálogo de error en su segunda ruta de la noche, se descartó el Chrome. |

Ningún PDF y ninguna cuota consumida. Al comprobarlo a mano en Firefox, el
portal muestra «Usuario dado de baja» para `ejecutivo_3`: el 401 era real y el
servicio lo clasificó bien. A las 16:08 esa cuenta todavía tenía sesión. Se
quitó de `.env` y de `account-pool.json`; queda sólo `ejecutivo_2`.

Actividad de la cuenta el 24, antes de la baja, como contexto y sin atribuir
causa: sesión headless detectada, unos seis logins desde IP distintas de
DataImpulse entre rutas y candidatos, y varios CAPTCHA rechazados.
`ejecutivo_2` tuvo más: más de diez rutas o candidatos, más cuatro búsquedas
manuales de control. Es la segunda cuenta dada de baja de tres (`ejecutivo_1`,
D7).

### 6.5 Defecto

| # | Defecto | Criterio | Estado |
|---|---|---|---|
| D31 | SIGINT durante una recuperación de ruta cuelga el worker. A las 19:37, con `ejecutivo_2` probando un candidato (`chrome-profile-route-5-port-10771`), el worker recibió SIGINT y no salió en más de 6 minutos: 100 % de CPU, sin líneas nuevas en el log salvo dos `TargetClosedError` de Playwright (`Future exception was never retrieved`) a las 19:41. SIGTERM lo terminó en 1 s, sin procesos de Chrome restantes. Con el worker inactivo, SIGINT salía en 1 s (sección 2). | R4 | Abierto |

## 7. Lo que se pide al desarrollador

1. D28: devolver la admisión de la prueba de cuota cuando el intento termina
   antes de una respuesta del portal sobre la cuota.
2. D27: agregar `resume_at` al CSV del reporte, al menos en filas `pending_quota`.
3. D29: headful por defecto (`DEFAULT_HEADLESS = False`) y documentar Xvfb
   como la forma soportada de correr sin ventanas en Linux; quitar la
   sugerencia `CBRS_HEADLESS=1` de `.env.example`.
4. D30: no tomar el diálogo de error como prueba de ruta comprometida cuando el
   navegador corre headless, o bloquear el arranque headless con un mensaje
   claro. Evaluar si conviene confirmar la ruta (por ejemplo, con otro
   candidato o un chequeo de salida) antes de borrar un perfil autenticado.
5. D31: SIGINT debe salir igual que SIGTERM aunque haya un candidato de ruta a
   medio login.
6. Bajas de cuentas: dos de tres cuentas fueron dadas de baja por el portal
   (`ejecutivo_1` antes del 17 y `ejecutivo_3` el 24). Revisar si la
   recuperación de rutas, que hace logins repetidos de la misma cuenta desde
   muchas IP distintas en poco tiempo, puede provocarlas, y fijar un tope de
   logins por cuenta y por día. Hasta tener una respuesta, conviene limitar la
   rotación automática en la única cuenta activa.
