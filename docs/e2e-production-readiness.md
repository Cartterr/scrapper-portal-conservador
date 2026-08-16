# Operación E2E autónoma en Ubuntu

## Arquitectura soportada

El entorno productivo soportado es Ubuntu. En estaciones Windows se desarrolla
en Ubuntu sobre WSL2; WSLg muestra el mismo Google Chrome Linux usado por el
runtime. No se mezclan procesos Python de WSL con Chrome o librerías nativas de
Windows.

```text
Poderalia -> API 127.0.0.1 -> SQLite/WAL -> worker secuencial
                                      -> cuenta autorizada + perfil + proxy CL
                                      -> Chrome headed en Xvfb -> CBRS
                                      -> todos los resultados -> PDFs permanentes
                                      -> restic -> volumen/NAS secundario
```

Se mantienen los componentes locales del proyecto: Python, Playwright, SQLite,
Pillow y `ThreadingHTTPServer`. No existe un frontend Node, un servicio FastAPI,
una base externa ni un solver de CAPTCHA.

## Instalación Ubuntu

Desde un checkout confiable:

```bash
sudo bash deploy/install-ubuntu.sh
sudoedit /etc/cbrs/cbrs.env
sudoedit /var/lib/cbrs/account-pool.json
sudo install -o root -g cbrs -m 0640 /dev/null /etc/cbrs/restic-password
sudoedit /etc/cbrs/restic-password
```

El instalador crea el usuario `cbrs`, el virtualenv Python 3.14, Google Chrome,
Xvfb, noVNC, restic y las unidades `systemd`. Es idempotente y no habilita el
tráfico automáticamente.

La configuración de cuentas contiene únicamente nombres de variables:

```json
{
  "accounts": [
    {
      "id": "ejecutivo_1",
      "username_env": "CBRS_ACCOUNT_1_USERNAME",
      "password_env": "CBRS_ACCOUNT_1_PASSWORD",
      "proxy_url_env": "CBRS_ACCOUNT_1_PROXY_URL",
      "profile_dir": "/var/lib/cbrs/accounts/ejecutivo_1/chrome-profile",
      "daily_quota": 20
    }
  ]
}
```

Las variables reales se mantienen en `/etc/cbrs/cbrs.env`, con permisos
`0640 root:cbrs`. Cada cuenta habilitada debe referenciar un proxy chileno
dedicado diferente. Las contraseñas, URLs completas, IPs crudas, cookies y JWT
no se escriben en SQLite ni en reportes.

Antes del primer arranque, inicializar el repositorio restic sin política de
pruning:

```bash
sudo -u cbrs bash -lc 'set -a; source /etc/cbrs/cbrs.env; restic init'
```

## WSL2 y depuración visual

Dentro de Ubuntu/WSL2:

```bash
bash deploy/install-wsl.sh
export CBRS_HEADLESS=0
.venv/bin/python -m cbrs doctor
```

Chrome Linux aparece mediante WSLg. Este camino reproduce el entorno Ubuntu;
el runtime Windows nativo queda fuera del soporte productivo.

## Preparación única

Ejecutar con el mismo usuario, configuración y display del servicio:

```bash
sudo systemctl start cbrs-display.service cbrs-x11vnc.service cbrs-novnc.service
sudo -u cbrs bash -lc '
  set -a
  source /etc/cbrs/cbrs.env
  cd /opt/cbrs
  .venv/bin/python -m cbrs doctor
  .venv/bin/python -m cbrs pool proxy-health \
    --config /var/lib/cbrs/account-pool.json \
    --approve-egress-baseline
'
```

El comando aprueba un baseline separado por perfil de cuenta y prueba país CL,
reCAPTCHA Enterprise y el endpoint inicial. El worker repite estos gates al
arrancar y antes del primer uso diario.

La autenticación normal es automática: primero intenta refresh del perfil y,
si expiró, usa las credenciales en memoria para un login browser-origin con un
token generado por la propia página. `pool init` se conserva como herramienta
manual de diagnóstico, no como preparación diaria.

## Cola y API de Poderalia

CLI:

```bash
python -m cbrs jobs enqueue --text "EMPRESA AUTORIZADA" --idempotency-key req-123
python -m cbrs jobs enqueue --foja 9441 --numero 4580 --year 1980
python -m cbrs jobs list
python -m cbrs jobs show JOB_ID
python -m cbrs jobs cancel JOB_ID
python -m cbrs jobs safety-clear --reason "revisión aprobada"
```

API local:

```http
POST /api/jobs
GET  /api/jobs/{job_id}
GET  /api/jobs/{job_id}/artifacts
POST /api/jobs/{job_id}/cancel
GET  /api/artifacts/{artifact_id}
```

Ejemplo de alta:

```json
{
  "kind": "text",
  "text": "EMPRESA AUTORIZADA",
  "idempotency_key": "demo-001"
}
```

La API devuelve `202` con `job_id` y `status_url`. Repetir la misma clave y
payload devuelve el mismo job; reutilizarla con otro payload devuelve `409`.
El valor de una búsqueda textual no aparece en respuestas de estado.

El listener rechaza direcciones no loopback. Si Poderalia no comparte host,
debe alcanzarlo mediante la red privada o un proxy autenticado administrado por
el cliente; no se debe cambiar a `0.0.0.0`.

## Ejecución y acceso operacional

```bash
sudo systemctl enable --now \
  cbrs-display.service cbrs-x11vnc.service cbrs-novnc.service \
  cbrs-dashboard.service cbrs-worker.service cbrs-backup.timer
sudo systemctl status cbrs-worker.service cbrs-dashboard.service
```

Desde la estación del operador:

```bash
ssh -L 8765:127.0.0.1:8765 -L 6080:127.0.0.1:6080 usuario@servidor
```

Abrir `http://127.0.0.1:8765`. Si todas las cuentas quedan en
`captcha_pending`, el worker deja de emitir tráfico. **Resolver CAPTCHA** abre
Chrome sobre el display privado; la vista llega por
`http://127.0.0.1:6080/vnc.html` a través del mismo túnel. No existe resolución
externa ni automática. Después de resolverlo, **Validar y reactivar** cierra la
sesión visual, ejecuta una validación segura y solo entonces devuelve la cuenta
al scheduler.

## Persistencia, cupos y recuperación

- SQLite vive en `/var/lib/cbrs/pool/pool.sqlite3`, con WAL, foreign keys y
  `busy_timeout`.
- Un lease vigente impide dos workers. Al expirar, los jobs `running` vuelven a
  cola y los items incompletos se reanudan; artefactos completos no se repiten.
- Cada búsqueda que alcanza el portal reserva cupo atómicamente. Con tres
  cuentas de 20, la solicitud 61 queda en `waiting_capacity` hasta la siguiente
  fecha de `America/Santiago`.
- Una búsqueda descarga todas las inscripciones. Cada resultado mantiene estado
  y PDF independiente; una mezcla de éxitos y fallos termina como `partial`.
- Los PDFs se publican con rename atómico, cabecera `%PDF-`, página esperada,
  tamaño y SHA-256. Permanecen en `/var/lib/cbrs/outputs/jobs/<job_id>/`.
- `cbrs-backup.timer` crea un snapshot online de SQLite y ejecuta `restic backup`
  diariamente. No ejecuta `forget` ni `prune`.
- El dashboard marca backup fallido/atrasado y espacio libre inferior al 10 %.

## Verificación

Prueba offline:

```bash
python -m pytest -q
```

Gate vivo autorizado:

1. Perfiles nuevos autentican automáticamente las tres cuentas.
2. Una consulta multirresultado crea un PDF válido por inscripción.
3. Reiniciar el worker durante una descarga no duplica trabajos terminados.
4. CAPTCHA en una cuenta produce failover; CAPTCHA en todas pausa la cola y
   permite recuperación visual.
5. Un cambio de egress pausa la cuenta antes de buscar.
6. Las primeras 60 solicitudes respetan la distribución y la 61 espera.
7. Reiniciar Ubuntu recupera worker, dashboard, cola y timers.
8. Un soak autorizado de siete días funciona sin preparación diaria.

Las pruebas automatizadas no sustituyen este gate vivo: credenciales, proxies,
cuota real y respuestas del portal solo pueden confirmarse en la infraestructura
autorizada del cliente.
