# CLI CBRS para Linux y WSL

Esta es la referencia completa del comando global `cbrs`. La CLI opera el mismo
worker, la misma cola SQLite, las mismas cuentas y los mismos controles que el
servicio. No reemplaza ni modifica el overview web.

## Instalación y primera apertura

Desde Ubuntu/WSL, en el checkout activo:

```bash
bash deploy/install-wsl.sh
sudo bash deploy/install-ubuntu.sh
exec newgrp cbrs
cbrs config paths
cbrs config validate
cbrs health
```

Los dos instaladores crean `/usr/local/bin/cbrs`, apuntando al entorno `.venv`
del checkout. `install-ubuntu.sh` también instala autocompletado Bash. Después de
la primera instalación, abre una terminal WSL nueva para aplicar la membresía al
grupo `cbrs`.

## Panel de terminal

```bash
cbrs overview
cbrs overview --watch
cbrs overview --watch --interval 5
cbrs overview --json
```

El panel ocupa pocas líneas y muestra servicios, cola, PDFs, cupo diario,
cuentas y trabajos recientes. `--watch` refresca la misma pantalla hasta
presionar `Ctrl+C`. Usa `--no-color` antes del comando para texto sin ANSI.

## Mapa rápido

```bash
cbrs commands
cbrs --help
cbrs jobs --help
cbrs service --help
```

## Servicios Linux

Componentes válidos: `owner`, `worker`, `dashboard`, `display`, `novnc`,
`backup`, `watchdog` y `all`.

```bash
cbrs service status all
cbrs service status worker --json
cbrs service logs worker
cbrs service logs worker --lines 200
cbrs service logs worker --follow
cbrs service start worker
cbrs service stop worker
cbrs service restart worker
cbrs service restart dashboard
```

Las acciones usan `sudo` cuando corresponde. La CLI bloquea acciones contra
`owner`, `display`, `novnc` o `all` porque pueden cerrar sesiones autenticadas.
El flag oculto de reconocimiento solo se usa cuando el propietario del servicio
autoriza explícitamente detener o reiniciar el servicio completo. Para
mantenimiento ordinario se reinicia únicamente `worker`; el owner y sus Chrome
deben conservar PID y sesión.

## Configuración

```bash
cbrs config paths
cbrs config show
cbrs config show --json
cbrs config validate
cbrs config set CBRS_REQUEST_DELAY_SECONDS 12
cbrs config unset CBRS_VARIABLE_OPCIONAL
```

`show` presenta la configuración efectiva y oculta contraseñas, tokens, claves,
logins y URLs de proxy. `set` y `unset` aceptan únicamente claves `CBRS_*`,
actualizan `.env` de forma atómica y dejan permisos `0640`. Para secretos es
preferible editar `.env` sin poner el valor en el historial del shell:

```bash
sudoedit /opt/scrapper-portal-conservador/.env
cbrs config validate
```

Una edición no cambia el proceso ya cargado. Reinicia solo el worker si esa
variable pertenece al worker. Cambios del owner, navegador, dependencias o
esquema requieren una migración autorizada.

## Cuentas y rutas

```bash
cbrs accounts
cbrs accounts --json
cbrs pool status
cbrs pool proxy-health
cbrs pool proxy-health --account ejecutivo_1
cbrs pool init --account ejecutivo_1 --timeout 600
cbrs pool login-debug --account ejecutivo_1
cbrs pool stop
```

Comandos históricos de ejecución manual, normalmente gestionados por systemd:

```bash
cbrs pool run [--dry-run] [--max-cycles N] [--dashboard]
cbrs pool dashboard [--host HOST] [--port PORT]
cbrs pool proxy-health --approve-egress-baseline
```

El reemplazo de un baseline o una ruta tiene gates adicionales. Consulta
`cbrs pool proxy-health --help` antes de una intervención.

## Cola de producción

```bash
cbrs jobs status
cbrs jobs list --limit 25
cbrs jobs show JOB_ID
cbrs jobs enqueue --text "RAZÓN SOCIAL"
cbrs jobs enqueue --foja 123 --numero 456 --year 2024
cbrs jobs enqueue --text "RAZÓN SOCIAL" --idempotency-key PEDIDO-001
cbrs jobs cancel JOB_ID
cbrs jobs recover
```

La cola es el camino recomendado para consultas. `cancel` solicita cancelación
en un punto seguro. `recover` limpia solo leases vencidos y reencola trabajos
abandonados; nunca cierra Chrome.

Comandos avanzados de la cola:

```bash
cbrs jobs captcha status
cbrs jobs captcha arm --account ejecutivo_1
cbrs jobs endurance status
cbrs jobs endurance pause
cbrs jobs endurance resume
cbrs jobs endurance run-once
cbrs jobs safety-clear --reason "motivo revisado"
cbrs jobs proxy-rotate --account ejecutivo_2 --acknowledge-authorized-live-traffic
cbrs jobs backup
cbrs jobs backup-verify --require-pdf
```

Procesos que systemd ejecuta normalmente y que no deben duplicarse en otra
terminal:

```bash
cbrs jobs worker [--once] [--max-jobs N] [--poll-seconds S]
cbrs jobs dashboard [--host HOST] [--port PORT]
```

## Diagnóstico, búsqueda directa y validación

```bash
cbrs health
cbrs doctor
cbrs preflight
cbrs captcha-health
cbrs readiness --target ubuntu
cbrs search --query "RAZÓN SOCIAL"
cbrs search --foja 123 --numero 456 --ano 2024
cbrs download --query "RAZÓN SOCIAL" --output ./salida
cbrs validate --foja 123 --numero 456 --ano 2024
cbrs init --timeout 600
```

La búsqueda directa conserva compatibilidad con `original-scripts`, pero para
el servicio regular se prefiere `jobs enqueue`: aporta idempotencia, selección
de cuenta, cupos, recibos, recuperación y trazabilidad.

## Soak histórico

```bash
cbrs soak status
cbrs soak run [--dry-run] [--max-cycles N] [--dashboard]
cbrs soak dashboard [--host HOST] [--port PORT]
cbrs soak export [--output ARCHIVO]
cbrs soak stop
```

## Salidas y códigos

- `0`: operación completada o estado saludable.
- `1`: diagnóstico no saludable, operación fallida o estado incompleto.
- `2`: argumento inválido o acción rechazada por un gate de seguridad.

Los comandos de automatización existentes conservan JSON. Los nuevos paneles
aceptan `--json` donde corresponde. Logs: `cbrs service logs COMPONENTE`.
Datos y PDFs: rutas exactas en `cbrs config paths`.

## Reglas operacionales

- No ejecutes un segundo worker ni un segundo dashboard cuando systemd está activo.
- No reinicies `owner`, `display` o `novnc` durante mantenimiento ordinario.
- No borres perfiles, cookies, SQLite, cuotas, recibos ni estados de control.
- Un error HTTP, CAPTCHA o DOM desconocido no autoriza reemplazar un navegador.
- Valida configuración antes de reiniciar y revisa `cbrs overview --watch` después.
