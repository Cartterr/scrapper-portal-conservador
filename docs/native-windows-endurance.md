# Endurance E2E nativo en Windows

Esta es la ruta operativa vigente. Usa la cola SQLite durable, Chrome instalado,
Python y restic nativos. No usa WSL, Docker ni máquinas virtuales.

## Límites y distribución

- Cuentas CBRS: tres, cada una con su perfil y un proxy Proxy-Cheap Chile static
  residential IPv4 distinto.
- Selección: round-robin durable compartido por jobs production y endurance.
- CAPTCHA: token Enterprise v3 del navegador primero. Un rechazo pausa la cuenta;
  solo el operador puede autorizar un solve pagado `RecaptchaV3TaskProxyless` de
  un solo uso para el siguiente intento.
- Fallback pagado: máximo global 10 intentos/día; circuito 15 minutos ante
  red, timeout, capacidad, autenticación o saldo cero.
- Endurance: una sola solicitud outstanding, prioridad baja, 10 minutos después
  de terminar; jamás recupera intervalos perdidos.
- Cupo: 15 jobs endurance por cuenta/día y reserva de cinco de las 20 consultas
  para production. `quota_exhaustion_test_mode` solo se habilita manualmente en
  una aceptación aislada.

El fixture inicial es `foja=9441`, `numero=4580`, `year=1980`. El plan se instala
deshabilitado porque primero deben provisionarse y aprobarse los tres egresos.

## Instalación

Desde PowerShell elevado en la raíz del repositorio:

```powershell
.\deploy\windows\Install-CbrsNative.ps1
```

Para una estación que también ejecutará la suite de aceptación, agregar
`-InstallDevelopmentRequirements`. El instalador es idempotente: completa las
claves faltantes del template sin reemplazar secretos, crea el password Restic
con ACL restringida, inicializa el repositorio cifrado y deja las tareas
registradas pero deshabilitadas.

Si `E:\CBRS-backup\restic` ya contiene un repositorio creado con otra clave,
el instalador se detiene sin modificarlo. Se debe proporcionar su password
original o elegir explícitamente otra ruta con `-BackupRepository`.

Luego completar, sin copiar secretos al repositorio:

```text
C:\ProgramData\CBRS\cbrs.env
C:\ProgramData\CBRS\restic-password
```

Provisionar manualmente tres endpoints Proxy-Cheap. Después de configurar las
cuentas y proxies, ejecutar un primer backup desde el entorno protegido.

Ingresar las tres credenciales CBRS directamente en el prompt local. Las
contraseñas no se muestran ni se incluyen en la línea de comandos:

```powershell
.\deploy\windows\Set-CbrsNativeAccountCredentials.ps1
```

```powershell
.\deploy\windows\Invoke-CbrsNativeTask.ps1 -Role backup
```

## Gates antes de tráfico

```powershell
.\.venv\Scripts\python.exe -m cbrs readiness `
  --target windows `
  --env-file C:\ProgramData\CBRS\cbrs.env `
  --config G:\CBRS\account-pool.json
```

Readiness exige Chrome/restic/tareas, layout `G:` + `E:`, ACL restringida,
transporte browser-only, tres URLs distintas, tres hashes de egreso aprobados,
país `CL`, saldo 2Captcha positivo, backup exitoso y ausencia de lease stale.
La consulta de saldo no crea una tarea CAPTCHA.

Tras aprobar los baselines, habilitar `enabled` en
`G:\CBRS\endurance-plan.json` y arrancar:

```powershell
.\deploy\windows\Start-CbrsNative.ps1 -AcknowledgeAuthorizedLiveTraffic
```

El arranque conserva el estado previo de las tareas, espera dashboard y worker,
ejecuta un segundo gate con `--require-active-runtime` y revierte las tareas que
él mismo inició si el heartbeat, el dashboard o Task Scheduler no quedan sanos.
El reporte post-arranque se guarda en
`G:\CBRS\readiness\operational.json`.

Para auditar el runtime ya iniciado sin cambiar estado:

```powershell
.\.venv\Scripts\python.exe deploy\run_with_env.py C:\ProgramData\CBRS\cbrs.env -- `
  .\.venv\Scripts\python.exe -m cbrs readiness `
  --target windows --require-active-runtime `
  --env-file C:\ProgramData\CBRS\cbrs.env `
  --config G:\CBRS\account-pool.json
```

## Prueba de restauración

El backup no se considera recuperable sólo porque Restic aceptó un snapshot.
Después del primer PDF real y luego de cambios relevantes, restaurar el último
snapshot en un directorio temporal, validar SQLite y exigir un PDF válido:

```powershell
.\.venv\Scripts\python.exe deploy\run_with_env.py C:\ProgramData\CBRS\cbrs.env -- `
  .\.venv\Scripts\python.exe -m cbrs jobs backup-verify --require-pdf
```

La verificación nunca sobrescribe `G:\CBRS`: elimina su directorio temporal al
terminar y registra sólo estado saneado en
`G:\CBRS\backup\restore-status.json`.

## Operación

```powershell
.\deploy\windows\Get-CbrsNativeStatus.ps1
.\.venv\Scripts\python.exe -m cbrs jobs endurance status
.\.venv\Scripts\python.exe -m cbrs jobs endurance pause
.\.venv\Scripts\python.exe -m cbrs jobs endurance resume
.\.venv\Scripts\python.exe -m cbrs jobs endurance run-once
.\.venv\Scripts\python.exe -m cbrs jobs captcha status
.\.venv\Scripts\python.exe -m cbrs jobs captcha arm --account ejecutivo_1
.\deploy\windows\Stop-CbrsNative.ps1
```

El dashboard escucha solo en `http://127.0.0.1:8765` y expone los mismos
controles en `/api/endurance`.

El botón **Configuración** abre el panel de operación. Permite ajustar sin
exponer secretos el cupo diario, comportamiento humano, jitter, frecuencia de
polling del worker, límite de cola de producción, trabajos inmediatos,
cooldown y asignación endurance. El guardado se rechaza mientras el worker está
activo. Round-robin estricto, prioridad de producción, un único job endurance,
no catch-up, transporte de PDF por navegador y bind loopback permanecen
bloqueados como protecciones del sistema.

Para recuperación manual: pausar endurance, detener el worker, esperar que el
perfil quede liberado y abrir solamente la cuenta afectada en modo headed. El
egreso nuevo nunca se autoaprueba.

```powershell
.\deploy\windows\Open-CbrsNativeRecovery.ps1 `
  -Account ejecutivo_1 `
  -AcknowledgeAuthorizedLiveTraffic
```

## Aceptación escalonada

1. Aprobar los tres baselines únicos y estables de Chile.
2. Validar autenticación/saldo 2Captcha sin tarea.
3. Con la cuenta en `captcha_pending`, autorizar manualmente un único solve
   pagado y confirmar que la autorización se consumió. Queda ligada a esa
   cuenta y vence a los 15 minutos.
4. Comprobar `1 → 2 → 3 → 1` y failover por cada estado de cuenta.
5. Observar 24 horas y luego siete días: ningún egreso compartido, backlog,
   PDF inválido, fuga de secreto, pérdida de prioridad production ni exceso del
   límite CAPTCHA.
