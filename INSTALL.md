# Instalación y uso en Ubuntu

Destino de entrega: Ubuntu 22.04 o 24.04, arquitectura amd64, Google Chrome
regular y Python 3.11 o superior. La instalación del sistema requiere sudo.
El instalador instala Python 3.11 en 22.04 si el Python del sistema es anterior;
en 24.04 utiliza Python 3.12. El servicio conserva cuentas, cola y documentos
en el repositorio. No requiere claves de resolución de CAPTCHA.

Estado de validación: consulte [el reporte de aceptación](docs/acceptance-status.md).
Correcciones del informe del 16 de septiembre: [alcance y pruebas pendientes](docs/handoff-fixes-2026-09-16.md).
Las pruebas locales no certifican todavía una instalación limpia ni la descarga
real y recuperación completa en el portal.

1. Instalar Git y obtener el código:

   ```bash
   sudo apt-get update
   sudo apt-get install -y git
   sudo git clone --branch master https://github.com/Cartterr/scrapper-portal-conservador.git /opt/scrapper-portal-conservador
   cd /opt/scrapper-portal-conservador
   sudo bash deploy/install-ubuntu.sh
   ```

   La instalación inicial registra `cbrs`. Después puede invocarse también con
   `cbrs service install`. No use el instalador para activar cambios sobre una
   instalación que conserva sesiones: coordine esa actualización por separado.

2. Completar el archivo generado:

   ```bash
   sudoedit /opt/scrapper-portal-conservador/.env
   ```

   Complete `CBRS_EJECUTIVO_1_USERNAME`, `CBRS_EJECUTIVO_1_PASSWORD` y los pares
   de las demás cuentas, más `DATAIMPULSE_PROXY_LOGIN` y
   `DATAIMPULSE_PROXY_PASSWORD`. Quite los pares no usados. No es necesario
   escribir un número de cuentas ni un archivo JSON en una instalación nueva.
   Si ya existe un `account-pool.json` con cuentas explícitas, tiene prioridad;
   agregar pares al `.env` no reemplaza esa configuración anterior.

3. Abrir una sesión de terminal nueva para recibir pertenencia al grupo `cbrs`
   y validar. Hasta entonces puede usar `sudo -u cbrs cbrs config validate`:

   ```bash
   cbrs config validate
   sudo systemctl enable cbrs-display.service cbrs-browser-owner.service cbrs-worker.service
   cbrs service start
   cbrs status
   ```

   `cbrs service start` arranca el worker y sus dependencias de Chrome. La primera
   autenticación puede tardar. `cbrs status` distingue `login_pending`,
   `available`, `held` por evidencia del portal y `estimated_quota_reached`
   por el presupuesto conservador configurado. El cupo mostrado es estimado.
   Una instalación nueva con cuentas descubiertas no bloquea al llegar a la
   estimación de 8: espera la señal real del portal. Un JSON anterior con
   presupuesto explícito conserva su límite; no se migra automáticamente.

   En `mobile_sticky`, la primera línea base se crea automáticamente por cuenta
   después de validar país y proxy configurado. Una línea base existente no se
   sobrescribe por esta inicialización. Para otros modos que exigen aprobación,
   use `cbrs pool proxy-health --approve-egress-baseline`; `cbrs preflight` valida
   el perfil individual, no todos los perfiles del pool.

   `cbrs status` muestra el motivo y la reanudación de cuentas pausadas, además
   de `captcha_pending`. `disabled` con `credentials_invalid` significa que el
   worker excluyó la cuenta por rechazo de autenticación y necesita revisión;
   un HTTP 401 por sí solo no demuestra que el portal haya dado de baja la cuenta.

4. Descargar una inscripción y repetirla para comprobar la caché:

   ```bash
   cbrs get --fojas 30282 --numero 12784 --ano 2024
   cbrs get --fojas 30282 --numero 12784 --ano 2024 --json
   cbrs get --fojas 30282 --numero 12784 --ano 2024 --output "$HOME/mis-pdfs/"
   ```

   Por defecto la copia de entrega queda en `outputs/pdf/` del repositorio.
   El original permanece en `.cbrs/runtime/outputs/jobs/` para recuperación.
   `CBRS_OUTPUT_DIR` en `.env` o el entorno cambia el directorio de entrega de
   la API, sin mover originales ni perfiles del servicio. Tiene prioridad
   `--output`, luego `Client(output_dir=...)`, luego `CBRS_OUTPUT_DIR`.
   En directorios se usa `F30282_N12784_A2024.pdf`; una ruta terminada en `.pdf`
   selecciona un archivo. Un destino con contenido diferente se rechaza.

   `get` espera hasta 300 segundos por defecto; `--timeout 900` lo amplía.
   Si termina antes del PDF, devuelve `pending` y su `job_id`. Consultar
   `cbrs jobs show JOB_ID` o `Client().job(JOB_ID)` no envía otra búsqueda.
   Repetir `get` reutiliza el trabajo. `--force` solicita una búsqueda nueva
   explícita y puede consumir cuota.

5. Descargar un lote y generar un reporte:

   ```bash
   cbrs get-batch /opt/scrapper-portal-conservador/examples/inscripciones.csv --output "$HOME/mis-pdfs/" --report "$HOME/resultados.csv"
   cbrs get-batch /opt/scrapper-portal-conservador/examples/inscripciones.csv --no-wait --report "$HOME/pendientes.csv"
   cbrs jobs list
   ```

   El CSV usa UTF-8 y coma. Se aceptan `foja|fojas`, `numero|número|num` y
   `ano|año|year`. Las columnas adicionales se conservan. Los encabezados del
   reporte (`status`, `pdf_path`, `error`, `account`, `attempts`, `job_id`,
   `finished_at`) están reservados. Una fila inválida produce `failed` y no
   impide encolar las demás. Un encabezado inválido impide encolar el archivo.

   El lote espera por defecto hasta resultados terminales o cuota agotada en
   todas las cuentas. Puede limitar la espera con `--timeout 900`; las filas
   restantes quedan `pending`. Una fila `pending_reconciliation` es una búsqueda
   enviada sin resultado confirmado: el reporte trae el comando exacto
   (`cbrs jobs reconcile JOB_ID --apply`) para autorizar otra cuenta después de
   comprobar en «Recientes» del portal que la inscripción no quedó registrada.
   `--no-wait` devuelve los IDs inmediatamente
   después de encolar. El reporte es una foto del estado al salir: vuelva a
   ejecutar el comando para actualizarlo y copiar los PDFs que terminaron
   después. No se repiten búsquedas aceptadas. No hay un límite de 100 filas
   en esta interfaz.

6. Usar Python desde otro proyecto:

   ```bash
   python3 -m venv "$HOME/cbrs-client-env"
   "$HOME/cbrs-client-env/bin/python" -m pip install /opt/scrapper-portal-conservador
   export CBRS_REPOSITORY=/opt/scrapper-portal-conservador
   "$HOME/cbrs-client-env/bin/python"
   ```

   ```python
   from cbrs import Client

   client = Client(timeout=300)
   result = client.get(fojas=30282, numero=12784, ano=2024)
   print(result.status, result.pdf_path, result.job_id)
   results = client.get_batch([(30282, 12784, 2024)], no_wait=True)
   print(client.status())
   print(client.job(results[0].job_id))
   ```

   El proceso Python necesita acceso al grupo `cbrs`. `CBRS_REPOSITORY` debe
   definirse antes del primer acceso al cliente. `pip install -e .` y wheel
   también son admitidos. El wheel instala el cliente/código Python; la
   instalación del sistema usa los scripts del repositorio.

   El API devuelve `Result` para resultados del portal. `InvalidInscription`
   y `ServiceUnavailable` son excepciones directas. `DownloadFailed` también
   informa errores de copia/reporte. `result.raise_for_status()` permite
   obtener `QuotaExhausted` para `pending_quota` y `DownloadFailed` para otros
   resultados sin PDF. Importar `cbrs` o construir `Client()` no inicia Chrome,
   no carga credenciales y no abre la red.

7. Consultar logs y operar el worker:

   ```bash
   cbrs service logs --follow
   cbrs service status
   cbrs service stop
   cbrs service start
   ```

   Logs: `.cbrs/runtime/logs/services.log`. El worker usa `Restart=always` y
   se habilita al arranque en el paso 3. Una parada explícita no provoca
   reinicio automático. El propietario de Chrome sigue separado y conserva
   la política de protección de sesiones; su reinicio automático completo
   todavía no está certificado. Los comandos de reinicio del propietario no
   forman parte del mantenimiento habitual.

## Ejecución sin systemd

Con Python 3.11+ y Chrome ya instalados: cree `.venv`, instale
`pip install -r requirements-dev.txt` y `pip install --no-deps -e .`, complete
`.env` y ejecute `.venv/bin/cbrs config validate`. En una terminal separada use
`.venv/bin/cbrs jobs worker`; el cliente puede ejecutarse desde otra terminal.
Este modo conserva la propiedad de Chrome dentro del worker y no tiene las
garantías de separación del servicio con propietario independiente. No use
`pkill chrome` para resolver errores ni sobre sesiones productivas. Ctrl+C o
SIGTERM detienen el worker en menos de 30 segundos y cierran el Chrome que
abrió; las sesiones autenticadas persisten en el perfil y se reutilizan al
volver a arrancar. No hay pausa fija entre trabajos: `interval_minutes` es 0
por defecto y sólo `CBRS_REQUEST_DELAY_SECONDS` separa las peticiones.

Los eventos del worker aparecen en stderr y en `.cbrs/runtime/logs/worker.log`
(rotación de 5 MB, tres copias). El log emite nombres de eventos, cuenta y
códigos de motivo; los detalles permanecen en SQLite. En systemd también puede
usar los comandos de logs anteriores.

Los valores actuales por defecto son 10 candidatos por recuperación y 30
rotaciones por hora; con la cola vacía, una ruta comprometida prueba sus
candidatos de forma consecutiva y, con trabajos pendientes en otras cuentas, uno
por pase para no bloquearlos. Si un `.env` anterior fija 1 y 3, esos valores siguen
teniendo prioridad: revise `CBRS_DATAIMPULSE_CANDIDATES_PER_RECOVERY` y
`CBRS_DATAIMPULSE_MAX_ROTATIONS_PER_HOUR`. No se eliminan los presupuestos.
La recuperación de login con candidatos sigue limitada a los IDs explícitos
de `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS` y a evidencia visible de rechazo.

## Cinco errores frecuentes

| Síntoma | Acción |
|---|---|
| Servicio no disponible | El mensaje indica el comando exacto según la instalación: `cbrs service start worker` con systemd, `cbrs jobs worker` en otra terminal sin systemd. |
| Campos faltantes | `cbrs config validate`; completar sólo las claves indicadas en `.env`. |
| CSV inválido | Corregir encabezado, codificación o la línea indicada; las filas inválidas constan en el reporte. |
| `pending_quota` | Consultar `resume_at`; el servicio revisará la cuota sin reenviar el lote. La hora es una estimación. |
| `pending` por login o proxy | Revisar `cbrs status` y logs; conservar las sesiones autenticadas. El servicio reintenta solo. |
| `pending_reconciliation` | Búsqueda enviada sin resultado confirmado. El servicio ya consultó «Recientes» en el portal: si la inscripción hubiera faltado habría reintentado solo. Revise «Recientes» de esa cuenta: si la inscripción no figura, `cbrs jobs reconcile JOB_ID --apply`; si figura, la cuota se consumió y el resultado debe recuperarse a mano. |

CLI: `get` retorna 0 con PDF, 1 sin PDF y 2 por argumentos/servicio. Batch
retorna 0 si todas las filas son `done`/`not_found`, 1 si alguna es pendiente o
fallida y 2 por estructura CSV inválida o servicio ausente.
