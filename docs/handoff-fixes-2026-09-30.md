# Respuesta técnica al informe del 30 de septiembre y a la llamada del 6 de octubre

Fuente: [`INFORME-PRUEBAS-2026-09-30.md`](../INFORME-PRUEBAS-2026-09-30.md)
(`master` en `5bb203b`) y los acuerdos de la llamada del 2026-10-06. Continúa
[handoff-fixes-2026-09-24.md](handoff-fixes-2026-09-24.md). Todo cambio de
código tiene prueba de regresión sin tráfico al portal
(`tests/test_informe_2026_09_30.py`).

## Defectos del informe y su corrección

| Defecto | Causa encontrada | Cambio |
| --- | --- | --- |
| D32 | El flujo de búsqueda clasifica el HTTP 400 `captcha-rechazado` y detiene la cuenta, pero deja abierto el diálogo «Atención / Se ha detectado un problema…» que el portal muestra junto con ese rechazo (informe del 22, 6.2). Un minuto después el muestreo pasivo lo lee sin contexto y lo toma como la firma de ruta comprometida. | El flujo que observa el rechazo se hace cargo del diálogo: lo cierra con su botón «Cerrar» (sin recarga, sin reenvío, sin logout) y, si el cierre falla, lo recuerda en ese navegador. El muestreo pasivo y la comprobación previa a la siguiente búsqueda ignoran ese diálogo recordado; la comprobación previa vuelve a intentar cerrarlo. Un diálogo distinto (otro panel) o uno que aparece sin un rechazo clasificado conserva la política del 13-09. Evento nuevo `captcha_rejected_dialog_attributed` con `dismissed` y `panel_id`. La cuenta sigue pausada 120 s por `captcha_rejected`; su perfil autenticado no se borra. |
| D33 | `create_job` devolvía el trabajo existente con la clave `get:F:N:A` aunque estuviera `cancelled`. | Un trabajo cancelado libera su clave de idempotencia al llegar la siguiente solicitud con la misma clave, que crea un trabajo nuevo. El trabajo cancelado conserva su fila, intentos y evidencia. Vale para `get`, `get-batch` y `cbrs jobs enqueue --idempotency-key`. |
| D34 | Sólo las retenciones por cuota contaban como «ninguna cuenta disponible»; una pausa con hora de reanudación (`retry_allowance_exhausted`) dejaba el trabajo en `waiting_capacity` con reclamos cada pocos segundos, y `_result` no la consideraba para `pending_quota`. | Una pausa con `resume_at` futuro cuenta como retención. Cuando todas las cuentas utilizables están retenidas, el trabajo espera hasta la reanudación más temprana (`next_run_at`), con motivo `portal_quota_exhausted` si sólo hay cuota de por medio y `accounts_unavailable` en otro caso; sin bucle de reclamos. El reporte muestra `pending_quota` con `resume_at` y un `error` que distingue cuántas cuentas están por cuota y cuántas pausadas y por qué. El overview etiqueta el motivo nuevo. |
| D35 | `fetch_public_egress` hacía una sola petición a `ipinfo.io`; un 429/503 del servicio fallaba el gate como si el proxy no respondiera. | La consulta viaja por un túnel HTTPS, así que un estado HTTP sólo puede venir del servicio: el proxy ya transportó la petición. Ante un estado HTTP se reintenta una vez (2 s) y luego se consulta `ipapi.co/json/` (mismos campos `ip`, `country`). Si los tres fallan, el error se llama `EgressServiceUnavailable` y nombra cada servicio y código. Un fallo de transporte (túnel, DNS, tiempo de espera) sigue siendo inmediato, porque ése sí es el proxy. |

## La cuenta «bloqueada 24 horas» (llamada del 6-10)

Fue `ejecutivo_2` el 30-09 y fue el servicio, no el portal. La cadena: una
búsqueda con `captcha_rejected` a las 15:20; D32 borró el perfil autenticado a
las 15:22; dos logins de candidato rechazados con `captcha-rechazado`; tope de
tres logins del día alcanzado a las 15:23; cuenta pausada hasta el 01-10 a las
18:02 UTC. Con D32 corregido la cadena se rompe en el primer paso: un reCAPTCHA
de búsqueda rechazado deja la cuenta 120 s en `captcha_pending` y vuelve con la
misma sesión, sin login nuevo ni candidatos.

El tope diario de logins se mantiene como protección: un login rechazado por el
portal llegó al portal, y el mandante perdió `ejecutivo_1` y `ejecutivo_3` por
decisiones del portal sobre la cuenta. Con tres cuentas nuevas, el valor 3 es
una pasada de recuperación por cuenta y día. Con D34, cuando ese tope deja la
última cuenta fuera, el reporte lo dice con `resume_at` en vez de aparentar una
cola viva. Si el mandante prefiere que los candidatos rechazados en el login no
cuenten contra el tope, es un cambio de política explícito, no de código.

## Sesiones que se cierran solas: cómo vive una sesión y cómo se mantiene (llamada del 6-10)

Lo medimos en nuestros perfiles y registros del 25 al 29 de septiembre, sin
tráfico al portal (detalle en [session-lifetime.md](session-lifetime.md)):

- El token de acceso (`auth_cbrs_token`) dura **30 minutos**. El de refresco
  (`cbrs_refresh_token`, cookie httpOnly) dura 7 días y **se renueva en cada
  refresco**: nuestras sesiones vivieron 14 y 15 días así, y una sobrevivió
  27 horas de inactividad total y un cambio de IP de salida.
- El portal sólo refresca cuando lo necesita: al cargar la página, justo antes
  de una petición con token vencido, o tras un 401. Si ese refresco falla,
  borra la sesión local y redirige a `/login/sesion-expirada`. Eso es
  exactamente «se recarga la página y te saca»: no es el token de acceso
  vencido (eso se renueva solo), es un refresco rechazado.
- Dos de nuestras tres sesiones fueron rechazadas por el portal el 28-09 entre
  las 12:56 y las 15:58 UTC sin ninguna petición nuestra de por medio (sin
  logout, sin login en otro perfil, sin reinicio de Chrome); la tercera, que
  no había refrescado a las 12:56, siguió viva al día siguiente. Esa
  revocación es del lado del portal y no se puede evitar desde aquí, sólo
  detectar a tiempo.
- Cargar otras rutas del portal no ayuda: las rutas de la SPA son del
  navegador, el servidor sólo ve las llamadas a la API y la única que extiende
  la sesión es el refresco. Más cargas de página sólo suman superficie ante el
  WAF y reCAPTCHA.

Por eso la renovación quedó **activada por defecto y guiada por el vencimiento**,
no por un reloj: `CBRS_SESSION_KEEPALIVE_SECONDS` (300 por defecto; 0 la
apaga) es el margen antes del vencimiento del token de acceso en que cada
navegador autenticado llama al refresco desde su propia página y guarda el
token nuevo en la cookie y en `localStorage`, igual que el portal. Es la misma
petición que haría la SPA, con la misma cookie: una cada ~25 minutos por
cuenta. No navega, no recarga, no inicia sesión; si la página ya necesita
login, no hace nada. Cada intento es un evento `session_keepalive` con
`http_status`, `refresh_cookie_present`, `refresh_cookie_rotated` y
`remaining_seconds`:

- `ok: true` con `refresh_cookie_rotated: true` de forma sostenida: la sesión
  se extiende indefinidamente.
- `http_status: 401`: el portal rechazó el refresco; la búsqueda siguiente
  habría caído en `/login/sesion-expirada`. El login ocurre por el camino de
  siempre al tomar el siguiente trabajo, y la hora del evento queda como
  evidencia de cuándo el portal mata sus tokens.

En su servidor basta con dejar el valor por defecto y mirar
`session_keepalive` junto a los logins (`cbrs requests CUENTA --json`) tras
un día completo.

## Telemetría (llamada del 6-10)

Confirmado: el registro de peticiones del 30-09 reconstruyó D32 al segundo,
incluida la ausencia de peticiones entre el rechazo y el borrado. Para un
bloqueo temporal hoy existen tres fuentes por cuenta: los eventos (`cbrs status`,
overview), el libro de candidatos (`proxy_candidate_attempts`) y
`.cbrs/runtime/logs/requests/<cuenta>/<fecha>.jsonl`. Esta entrega añade los
eventos `captcha_rejected_dialog_attributed` y `session_keepalive`.

## Alta de cuentas (pregunta 5)

El procedimiento propuesto es el correcto y quedó en `INSTALL.md`
(«Reemplazar una cuenta dada de baja»): número nuevo nunca usado, par
`CBRS_EJECUTIVO_N_*` (puerto `9999 + N` automático con cuentas descubiertas, o
entrada explícita en `account-pool.json`), ID retirado sin reutilizar porque
rutas, retenciones, rechazos, historial de salidas, registro de peticiones y
perfil de Chrome van por ID, `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS` sólo con
IDs explícitos, y reinicio del worker porque las cuentas se leen al arrancar.
Para retirar una cuenta: quitar su par del `.env` o `"enabled": false` en el
JSON, sin borrar su perfil ni su historial.

## `ejecutivo_2` (pregunta 6)

No tenemos sus datos; sólo podemos decir qué la distingue de las tres nuevas.
Es el único ID con historia anterior al 24-09: la sesión headless del 24 con
búsquedas rechazadas (D29), la ruta comprometida del 24 y más logins de
candidato. reCAPTCHA puntúa navegador, IP y comportamiento; si además pondera
la cuenta, no lo sabemos. Para decidir con sus datos: en
`cbrs requests ejecutivo_2 --json`, comparar con `ejecutivo_4..6` el número de
`/api/v1/auth/login` y de HTTP 400 por día, ver si los rechazos se concentran
en salidas concretas (`egress_observed_hash`) y si esas salidas aparecen antes
en otras cuentas (la unicidad sólo mira siete días). Si los rechazos siguen
mientras las cuentas nuevas limpias buscan bien, retirar el ID según la
pregunta 5 cuesta menos que seguir gastando logins en él.

## Headless (mensajes del 30-09)

La negativa de `cbrs jobs worker` a arrancar headless es deliberada (D29/D30):
el portal rechaza el reCAPTCHA de búsqueda de Chrome headless. El mensaje ahora
dice exactamente eso y da la forma soportada, incluida la pantalla de Xvfb:
`xvfb-run -n 99 -s "-screen 0 1366x900x24 -nolisten tcp" cbrs jobs worker`
(sin `-s`, 640x480 a 8 bits), también en `INSTALL.md`, `README.md` y
`.env.example`. `cbrs get --headless` sigue disponible para diagnósticos.

## Servicio y API (llamada del 6-10)

La «pseudocola en una tabla» ya existe: la tabla `jobs` en
`.cbrs/runtime/pool/pool.sqlite3`, que el worker reclama cada pocos segundos.
Hay dos entradas estables sobre ella, sin tocar el worker:

- Python: `from cbrs import Client`; `Client(timeout=0).get(fojas, numero, ano)`
  encola y devuelve `job_id`; `Client().job(job_id)` consulta sin encolar;
  `get_batch` procesa un CSV.
- CLI: `cbrs get-batch entrada.csv --output reporte.csv`, con columnas
  `status, pdf_path, error, account, attempts, job_id, finished_at, resume_at`.

Campos mínimos de una solicitud: `foja`, `numero`, `ano`; de una respuesta:
`status` (`done`, `not_found`, `failed`, `pending`, `pending_quota`,
`pending_reconciliation`), `pdf_path`, `error`, `resume_at`, `job_id`. Una API
HTTP sobre su servidor sería una capa delgada que llame a esas dos funciones:
`POST` encola con `timeout=0` y `GET /{job_id}` consulta; o bien su aplicación
escribe en su propia tabla y un puente periódico llama a `get-batch`. La
modularización queda para después del acceso SSH, como se acordó.

La autoregistración de cuentas no forma parte de esta entrega.

## Búsqueda por nombre y elección entre coincidencias

Quedó implementada de punta a punta con la misma disciplina que la búsqueda
FNA. La búsqueda por «razón social o nombre de socio» va por el formulario del
portal (el conmutador «Tipo de búsqueda», el campo de 50 caracteres y un solo
clic en «Buscar»), acepta sólo el POST con `origen: texto`, y al terminar deja
visible otra vez el formulario FNA. Cada búsqueda por nombre consume una
consulta del portal, igual que una FNA; las filas devueltas (razón social,
foja, número, año, acto) se guardan todas con su ticket, y cuáles se descargan
lo decide una política explícita: sin ella, ninguna.

- `Client.find(nombre)` / `cbrs find "NOMBRE"`: una búsqueda, ningún PDF; el
  resultado es `needs_selection` con la lista `matches` (`foja`, `numero`,
  `ano`, `nombre`, `acto`, `tipo`, `personas`, `folio`, `id`, `selected`). Quien
  llama elige y pide la inscripción con `get` (segunda consulta).
- `Client.find(nombre, ano=2021, exact=True, download=True)` /
  `cbrs find "NOMBRE" --ano 2021 --exact --download`: filtra por año y por
  razón social idéntica (sin acentos ni mayúsculas) y descarga **sólo si queda
  exactamente una** coincidencia, en la misma consulta; si quedan cero o varias,
  devuelve `needs_selection` con todas, sin gastar más.
- `not_found` cuando el portal no devuelve filas. Las demás salidas
  (`pending`, `pending_quota`, `failed`) son las de `get`.
- `raise_for_status()` lanza `SelectionRequired` con `.matches`. El trabajo
  guarda las filas no elegidas como `skipped`: no son fallos, no se reintentan y
  no cuentan como documentos pendientes.
- La misma búsqueda (texto y política) reutiliza su trabajo; `--force` repite.
  La cola `cbrs jobs enqueue --text` conserva su comportamiento histórico
  (descargar todas las filas).

Decidir «cuál es la correcta cuando hay dos sociedades con el mismo nombre»
sigue siendo lógica del mandante: el servicio le entrega las coincidencias
completas y descarga sólo cuando la regla que él fije deja una sola.

## Validación y activación

`tests/test_informe_2026_09_30.py`: 14 pruebas nuevas (Chrome real sobre HTML
local, sin portal). Suite completa en Windows, Python 3.14: 640 aprobadas y 4
omitidas. Activación:
cambian `jobs.py`, `config.py`, `preflight.py` y `browser_session.py`, así que
es un reinicio completo del worker (autorización permanente del 27-09 para
releases CBRS; en su instalación, Ctrl+C y volver a arrancar). Los perfiles,
rutas y recibos no cambian. En nuestra producción WSL el servicio está
detenido desde el 29-09; estos cambios se activan al próximo arranque.
