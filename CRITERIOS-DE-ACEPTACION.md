# Criterios de aceptación — Descarga de PDFs del CBRS por CLI y Python

Versión: 1.0 · Fecha: 2026-09-12 · Plataforma objetivo: **Ubuntu nativo** (22.04 o 24.04)

Este documento define qué significa "terminado" para la aplicación. El trabajo se
considera entregado cuando **todos** los criterios marcados como obligatorios se
cumplen y las pruebas de aceptación de la sección 6 pasan en una máquina Ubuntu
limpia, siguiendo únicamente las instrucciones entregadas.

---

## 1. Objetivo

Desde una terminal Ubuntu, sin intervención manual, poder obtener el PDF de una
inscripción del Registro de Comercio del Conservador de Bienes Raíces de Santiago
(CBRS) identificada por **fojas, número y año**, tanto de a una como en lote (CSV),
por línea de comando y por `import` en Python.

## 2. Definiciones

| Término | Significado |
|---|---|
| Inscripción | Tupla `(fojas, numero, ano)`. Los tres son enteros positivos. |
| PDF | Archivo PDF válido descargado del portal que corresponde exactamente a la inscripción pedida. |
| Servicio | Proceso de larga duración que mantiene Chrome, sesiones, proxies y la cola. |
| Cuenta | Credencial de usuario de `conservador.cl`. |
| Proxy | Salida de red de DataImpulse (sesión sticky móvil chilena). |
| Cuota | Límite diario de búsquedas que el portal impone por cuenta (valor exacto desconocido, se estima 20 o menos). |
| Pendiente | Inscripción solicitada que aún no tiene PDF pero que no ha fallado de forma definitiva. |

## 3. Requisitos obligatorios

### R1. Descarga unitaria por CLI

Debe existir un comando único que reciba fojas, número y año y devuelva el PDF.

```bash
cbrs get --fojas 1234 --numero 567 --ano 2019
cbrs get --fojas 1234 --numero 567 --ano 2019 --output /ruta/destino/
cbrs get --fojas 1234 --numero 567 --ano 2019 --output /ruta/destino/mi_nombre.pdf
```

Criterios:

- Sin `--output`, el PDF se guarda en un directorio por defecto documentado y el
  comando imprime la **ruta absoluta** del PDF en `stdout` (última línea, sola).
- Con `--output` a un directorio, guarda allí con nombre determinístico
  `F{fojas}_N{numero}_A{ano}.pdf`. Con `--output` a un archivo, usa ese nombre.
- `--json` imprime un objeto con al menos: `fojas`, `numero`, `ano`, `status`,
  `pdf_path`, `error`, `account`, `attempts`, `started_at`, `finished_at`.
- Códigos de salida: `0` PDF obtenido; `1` no obtenido (error, cuota, etc.);
  `2` argumentos inválidos o servicio no disponible. El mensaje de error va a
  `stderr` en una línea comprensible para un humano.
- Si la inscripción ya fue descargada antes y el archivo existe, se devuelve la
  ruta existente **sin consumir cuota** (a menos que se pase `--force`).
- Si la inscripción **no existe** en el portal (resultado vacío confirmado), el
  comando termina con `status: not_found` y código `1`, y no se reintenta.
- Tiempo máximo de espera configurable (`--timeout`, por defecto razonable y
  documentado). Al expirar, el trabajo queda encolado como pendiente y el comando
  informa el `job_id` para consultarlo después.

### R2. Descarga en lote por CSV

```bash
cbrs get-batch entrada.csv --output /ruta/destino/ --report resultados.csv
```

Formato de entrada (CSV con encabezado, separador coma, UTF-8):

```csv
fojas,numero,ano
1234,567,2019
88,12,2001
```

Se aceptan como sinónimos de encabezado: `foja|fojas`, `numero|número|num`,
`ano|año|year`. Columnas adicionales se ignoran y se copian al reporte.

Criterios:

- Cada fila se convierte en un trabajo idempotente: repetir el mismo CSV no
  duplica trabajos ni vuelve a descargar PDFs ya existentes.
- El comando bloquea hasta que **cada fila** llegue a un estado terminal o a
  `pending_quota` (ver R5), salvo que se pase `--no-wait`, en cuyo caso encola y
  retorna de inmediato con los `job_id`.
- Al terminar escribe `resultados.csv` con las columnas de entrada más:
  `status`, `pdf_path`, `error`, `account`, `attempts`, `job_id`, `finished_at`.
  Valores de `status`: `done`, `not_found`, `pending_quota`, `pending`, `failed`.
- Imprime en `stdout` un resumen: total, `done`, `not_found`, `pending_quota`,
  `failed`, y la ruta del reporte.
- Códigos de salida: `0` todas `done` o `not_found`; `1` alguna `pending_*` o
  `failed`; `2` CSV inválido (indica número de línea y motivo) o servicio caído.
- Un CSV de 100 filas debe procesarse sin límite artificial de tamaño. Las filas
  inválidas se reportan como `failed` con el motivo y no detienen el lote.
- Volver a ejecutar el mismo comando con el mismo CSV retoma **sólo** las filas
  que no están `done` ni `not_found`.

### R3. API Python

Instalable en un entorno virtual (`pip install -e .` o wheel). Uso mínimo:

```python
from cbrs import Client

client = Client()  # lee .env / config por defecto; acepta timeout, output_dir

result = client.get(fojas=1234, numero=567, ano=2019)
result.pdf_path      # pathlib.Path o None
result.status        # "done" | "not_found" | "pending_quota" | "pending" | "failed"
result.error         # str | None

results = client.get_batch("entrada.csv", output_dir="/ruta/destino/")
results = client.get_batch([(1234, 567, 2019), (88, 12, 2001)])
for r in results:
    print(r.fojas, r.numero, r.ano, r.status, r.pdf_path)

client.status()      # salud del servicio, cuentas disponibles, cupos estimados
client.job(job_id)   # consultar un trabajo pendiente
```

Criterios:

- La API y el CLI usan **el mismo código**; el CLI es una capa fina sobre la API.
- Errores como excepciones tipadas y documentadas: `ServiceUnavailable`,
  `InvalidInscription`, `QuotaExhausted`, `DownloadFailed`.
- Sin efectos secundarios en `import` (no lanza Chrome, no abre red).
- Type hints y docstrings en la superficie pública.

### R4. Servicio permanente y auto-mantenido

Es aceptable, y esperado, que exista un servicio de fondo que deba estar
levantado antes de usar el CLI o la API.

Criterios:

- Se instala como unidad `systemd` (`systemctl --user` o de sistema, documentado)
  con `Restart=always` y habilitado al arranque. Comandos: `cbrs service
  install|start|stop|restart|status|logs`.
- Se **auto-recupera** sin intervención humana ante: caída de Chrome, pérdida de
  sesión del portal, proxy caído o bloqueado, reinicio de la máquina, caída del
  propio worker. Recuperación verificada en pruebas de la sección 6.
- Si el servicio no está corriendo, CLI y API fallan **inmediatamente** con
  código `2` y un mensaje que dice exactamente qué comando ejecutar.
- `cbrs status` (o equivalente) muestra en una pantalla: servicio arriba/abajo,
  cuentas con su estado (disponible, cuota agotada hasta HH:MM, login pendiente,
  bloqueada), proxy activo por cuenta, trabajos pendientes y últimos errores.
- Los logs se pueden ver con un solo comando y contienen suficiente contexto
  para diagnosticar (cuenta, proxy, job_id, paso, causa).
- El servicio no consume recursos de forma creciente: tras 24 h corriendo, la
  memoria total (servicio + Chrome) debe mantenerse estable.

### R5. Cuotas, rotación de proxies y cuentas — todo automático

Es aceptable que una cuenta deje de producir PDFs cuando el portal indique que
llegó a su límite. **No** es aceptable que queden inscripciones pendientes
mientras exista alguna cuenta con cuota disponible.

Criterios:

- **Detección de cuota**: cuando el portal muestra el mensaje de límite diario,
  la cuenta se marca `held` con hora estimada de liberación. La cuenta no se
  vuelve a usar hasta entonces. Los trabajos asignados a ella se **reasignan**
  a otra cuenta disponible de inmediato.
- **Detección de proxy malo**: se considera que un proxy no sirve cuando ocurre
  cualquiera de: login rechazado o bucle de login, captcha irresoluble tras N
  intentos, respuesta bloqueada (403/429/página de bloqueo), salida no chilena,
  timeout de red reiterado, descarga de PDF que falla o devuelve contenido
  inválido. En ese caso el sistema **rota el proxy automáticamente** (nueva
  sesión sticky de DataImpulse) y reintenta, sin que el operador haga nada.
- **Orden de rotación**: primero proxy (hasta un máximo configurable, por
  defecto 3 por intento), luego cuenta. Los límites de rotación son
  configurables con valores por defecto sensatos y documentados.
- **Nunca se pierde un trabajo**: una inscripción sin PDF queda `pending` o
  `pending_quota`, nunca desaparece. Un trabajo pasa a `failed` sólo por causas
  definitivas (inscripción inválida, `not_found`), nunca por fallos de proxy,
  login, captcha o red.
- **Regla central de detención**: la **única** razón aceptable para que una
  inscripción válida quede sin descargar es que **todas** las cuentas
  configuradas hayan alcanzado el límite diario del CBRS. Mientras exista al
  menos una cuenta con cuota disponible, el sistema sigue intentando (rotando
  proxies y cuentas) hasta lograr el PDF o hasta que esa cuenta también llegue
  al límite. No hay tope de reintentos que produzca un `failed` por
  agotamiento.
- **Reanudación**: cuando una cuenta `held` se libera, el servicio retoma los
  trabajos `pending_quota` **solo**, sin que el usuario relance el comando. El
  usuario puede consultar el estado con `cbrs jobs list` o `client.job()`.
- **Sin cuota disponible en ninguna cuenta**: el comando batch termina, reporta
  cuántas filas quedaron `pending_quota` y la hora estimada en que se retomarán.
- **No consume cuota inútilmente**: no se hacen búsquedas repetidas para la
  misma inscripción; un reintento por fallo de proxy reutiliza el resultado de
  búsqueda ya aceptado si el portal lo permite.
- Todo lo anterior queda registrado en logs y visible en `cbrs status`.

### R6. Configuración mínima e instrucciones

Criterios:

- **Configuración**: para operar, el usuario sólo debe completar en un archivo
  `.env` (a partir de `.env.example`):
  - `CBRS_EJECUTIVO_1_USERNAME` / `_PASSWORD` (y 2, 3, … según cuentas)
  - `DATAIMPULSE_PROXY_LOGIN` / `DATAIMPULSE_PROXY_PASSWORD`

  Todo lo demás debe tener valores por defecto que funcionen.
- **Cuentas variables**: el sistema detecta automáticamente las cuentas
  `CBRS_EJECUTIVO_1..N` que tengan usuario y contraseña, sin variable de conteo.
  Debe funcionar con 1, 2, 3, 4 o más cuentas; 3 es el caso por defecto.
- **Captcha opcional**: las claves de 2Captcha/CapSolver son **opcionales**. Si
  están presentes se usan; si no, el sistema debe operar igual. `.env.example`
  las muestra comentadas y documentadas como opcionales.
- `cbrs config validate` verifica el `.env` y explica cada campo faltante.
- **Instrucciones** (`INSTALL.md` o `README.md`): una secuencia numerada de
  comandos, copiables tal cual, que lleva de Ubuntu limpio a un PDF descargado.
  Debe incluir: prerrequisitos del sistema, instalación, `.env`, instalación y
  arranque del servicio, verificación (`cbrs status`), primer `cbrs get`,
  primer `cbrs get-batch`, uso desde Python, dónde quedan los PDFs y logs, y
  qué hacer ante los 5 errores más comunes.
- Las instrucciones se validan ejecutándolas en una VM o contenedor Ubuntu
  **limpio** por alguien distinto de quien las escribió. El tiempo desde cero
  hasta el primer PDF no debe superar 30 minutos, sin contar tiempo de espera del
  portal.
- Documentación en español, sin referencias a Windows, WSL, PowerShell ni
  ninguna otra plataforma como ruta activa.

## 4. Requisitos de calidad

- Suite de tests automatizados (`pytest`) que corre sin red ni credenciales y
  cubre: parseo de CSV, nombres de archivo, idempotencia, máquina de estados de
  trabajos, lógica de rotación y detección de cuota. Debe pasar en verde.
- Un test de integración documentado (con credenciales reales, ejecutado a mano)
  para cada escenario de la sección 6.
- Ningún secreto en el repositorio ni en los logs (las contraseñas y logins de
  proxy se enmascaran).
- El código de la API pública no cambia de firma sin nota en un `CHANGELOG`.

## 5. Entregables

1. Repositorio con el código, instalable en Ubuntu.
2. `INSTALL.md` (o sección en `README.md`) con las instrucciones de R6.
3. `.env.example` con **sólo** los campos obligatorios sin valor y el resto
   comentado con su valor por defecto.
4. Unidad `systemd` e instalador (`cbrs service install` o script).
5. Reporte de ejecución de las pruebas de aceptación de la sección 6: fecha,
   comandos usados, salida, y PDFs resultantes (o sus hashes).
6. Un CSV de ejemplo y su `resultados.csv` real.

## 6. Pruebas de aceptación

Se ejecutan en una VM/contenedor Ubuntu limpio, con 3 cuentas reales (caso por defecto) y
credenciales DataImpulse reales, siguiendo sólo la documentación entregada.
Cada prueba indica el resultado esperado. Todas deben pasar.

| # | Prueba | Resultado esperado |
|---|---|---|
| A1 | Instalación siguiendo `INSTALL.md` desde cero | Termina sin pasos improvisados; `cbrs status` muestra servicio arriba y las 3 cuentas disponibles. |
| A2 | `cbrs get` con una inscripción válida | Código `0`, imprime ruta, el PDF abre y corresponde a la inscripción. |
| A3 | Repetir A2 | Código `0`, misma ruta, no se consume cuota (verificable en `cbrs status`). |
| A4 | `cbrs get` con inscripción inexistente | Código `1`, `status: not_found`, sin reintentos. |
| A5 | `cbrs get` con argumentos inválidos (`--ano abc`) | Código `2`, mensaje claro. |
| A6 | `cbrs get` con el servicio detenido | Código `2`, mensaje indica el comando para arrancarlo. |
| A7 | `cbrs get-batch` con 10 filas válidas, 1 inexistente, 1 mal formada | `resultados.csv` con 10 `done`, 1 `not_found`, 1 `failed`; código `1`; los 10 PDFs existen. |
| A8 | Repetir A7 con el mismo CSV | Ningún PDF se vuelve a descargar; termina en segundos. |
| A9 | Python: `Client().get(...)` y `get_batch(...)` | Mismos resultados que A2 y A7. |
| A10 | Lote grande: CSV con más filas que la cuota conjunta de las 3 cuentas (p. ej. 70) | Se descargan PDFs hasta que las 3 cuentas quedan `held`; el resto queda `pending_quota`; el resumen indica hora de reanudación; **ninguna** fila se pierde. |
| A11 | Continuación de A10 tras liberarse la cuota | Sin relanzar nada, el servicio descarga las pendientes; `cbrs jobs list` las muestra `done`. |
| A12 | Proxy bloqueado: forzar un proxy que no permita login (p. ej. país incorrecto o puerto sticky quemado) durante un `get` | El sistema rota proxy solo, el `get` termina en `done`; los logs muestran la rotación. |
| A13 | Matar Chrome (`kill -9`) durante un lote | El servicio lo relanza, recupera sesión y el lote termina sin intervención. |
| A14 | Matar el proceso del servicio durante un lote | `systemd` lo reinicia; los trabajos en curso se retoman; el lote termina. |
| A15 | Reiniciar la máquina con trabajos pendientes | Al volver, el servicio arranca solo y continúa los pendientes. |
| A16 | Servicio corriendo 24 h en reposo y con carga intermitente | Memoria estable, sin procesos Chrome huérfanos acumulados. |
| A17 | `pytest` sin credenciales ni red | Todo verde. |
| A18 | `grep` de contraseñas y logins en logs y repo | Sin coincidencias. |
| A19 | Quitar una cuenta del `.env` (dejar 2) y reiniciar el servicio | `cbrs status` muestra 2 cuentas; `get` y `get-batch` funcionan igual. |
| A20 | Instalación sin claves de captcha en `.env` | `cbrs config validate` pasa y A2 funciona. |

Métrica global: en A10 + A11, el **100 %** de las inscripciones válidas
termina `done` sin ninguna acción manual. Durante la prueba, en ningún momento
puede haber una fila `pending` inactiva mientras alguna cuenta tenga cuota
disponible; la única espera admitida es `pending_quota` con todas las cuentas
`held`.

## 7. Fuera de alcance

- Interfaz web u overview gráfico (puede existir, no se evalúa).
- Soporte Windows, WSL o macOS.
- Búsqueda por razón social u otros criterios distintos de fojas/número/año.
- Superar o eludir el límite diario del portal por cuenta.
- Alta disponibilidad multi-máquina.

## 8. Supuestos y preguntas abiertas

Los puntos marcados como decididos fueron confirmados por el mandante el
2026-09-12. Los demás se asumen así salvo indicación en contrario:

1. **Captcha**: las claves de 2Captcha/CapSolver son opcionales (decidido).
   Con o sin ellas, se exigen los mismos criterios de R5.
2. **Nombre del comando**: se mantiene `cbrs` (ya registrado en
   `/usr/local/bin/cbrs`), agregando `get` y `get-batch`. Los comandos existentes
   (`jobs enqueue`, `search`, `download`) pueden conservarse.
3. **Cantidad de cuentas**: N, detectadas por numeración `CBRS_EJECUTIVO_1..N`
   (decidido). 3 es el valor por defecto de las pruebas; agregar o quitar una
   cuenta sólo requiere editar `.env` y reiniciar el servicio.
4. **Directorio por defecto de PDFs**: `./outputs/pdf/` relativo al repositorio,
   o el que indique `CBRS_OUTPUT_DIR`.
5. **Ventana de cuota**: se trata como ventana móvil de 24 h desde el primer
   éxito de la cuenta (política ya implementada), no como reset a medianoche.
6. **Modo del comando batch**: bloqueante por defecto, con `--no-wait` para
   encolar y salir (decidido).
7. **Inscripción inexistente**: se reporta `not_found` sin reintentos y se asume
   que consumió cuota (decidido).
8. **Métrica de éxito**: 100 % de inscripciones válidas descargadas; la única
   detención admitida es el límite diario del CBRS en todas las cuentas
   (decidido).
