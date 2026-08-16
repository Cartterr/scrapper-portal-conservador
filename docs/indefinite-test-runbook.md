# Runbook de prueba E2E indefinida

Este runbook separa deliberadamente la preparación sin tráfico de la puesta en
marcha real. La ruta de prueba es la cola durable (`jobs worker`), no el runner
legacy de `soak` ni el pool legacy. El worker puede quedar activo
indefinidamente y solo consulta CBRS cuando existe un job autorizado.

## Estado preparado en este PC

La auditoría local del 16 de agosto de 2026 confirmó:

- WSL2 está habilitado y su versión predeterminada es 2;
- todavía no hay una distribución Ubuntu instalada;
- Windows tiene Python 3.14.3, Playwright 1.60.0, Pillow 12.1.1, pytest 9.0.3,
  `curl_cffi` 0.15.0 y Google Chrome;
- `restic` no está instalado en Windows, lo cual es correcto porque se instalará
  dentro de Ubuntu;
- el volumen `V:` tiene aproximadamente 6.3 GiB libres. No se debe usar para la
  retención indefinida de PDFs. El almacenamiento primario debe quedar dentro de
  `/var/lib/cbrs/outputs` y el repositorio restic en otro volumen o NAS;
- la configuración local existente conserva tres cuentas, pero todavía no tiene
  referencias de usuario/contraseña para autologin. No fue modificada ni se
  copiaron secretos.

El reporte local reproducible se genera con:

```powershell
.\deploy\windows\Test-CbrsReadiness.ps1
```

Este comando no instala WSL, no inicia Ubuntu, no abre Chrome y no hace llamadas
de red. Solo con `-ProbeWslRuntime` puede arrancar brevemente una distribución ya
instalada para inspeccionar comandos locales; tampoco contacta CBRS.

## Controles ya preparados

| Archivo | Función | ¿Puede generar tráfico CBRS? |
|---|---|---:|
| `deploy/windows/Test-CbrsReadiness.ps1` | Gate offline y reporte sanitizado | No |
| `deploy/windows/Install-CbrsWsl.ps1` | Instala Ubuntu WSL2 solo con `-Apply` | No |
| `deploy/windows/Initialize-CbrsRuntime.ps1` | Instala runtime/unidades solo con `-Apply` | No |
| `deploy/windows/Start-CbrsIndefiniteTest.ps1` | Gate final y arranque de servicios | Sí |
| `deploy/windows/Get-CbrsIndefiniteStatus.ps1` | Estado de systemd, cola y heartbeat | No |
| `deploy/windows/Stop-CbrsIndefiniteTest.ps1` | Parada graceful; preserva estado | No |

El comando de arranque rechaza la ejecución si falta
`-AcknowledgeAuthorizedLiveTraffic`. Antes de iniciar cualquier unidad vuelve a
ejecutar el gate dentro de Ubuntu. El gate exige configuración, referencias a
secretos, perfiles separados, baselines aprobados, runtime Linux y respaldos;
no intenta corregirlos automáticamente.

## Etapa 0 — ahora: solo preparación offline

Ejecutar desde la raíz del repositorio:

```powershell
python -m pytest -q
python -m cbrs readiness `
  --target wsl `
  --distro Ubuntu-24.04 `
  --env-file .env `
  --config .cbrs/account-pool.json `
  --json-report .cbrs/readiness/indefinite-test.json
```

Un exit code `1` es esperado mientras Ubuntu, secretos y baselines permanezcan
deliberadamente pendientes. El JSON no contiene contraseñas, URLs completas de
proxy, IPs ni consultas.

No ejecutar todavía ninguno de los pasos siguientes.

## Etapa 1 — setup diferido de Ubuntu/WSL2

En una ventana PowerShell elevada y dentro de la ventana aprobada:

```powershell
.\deploy\windows\Install-CbrsWsl.ps1 -Apply -DistroName Ubuntu-24.04
```

Si Windows solicita reinicio, completarlo. Abrir Ubuntu una sola vez para crear
su usuario inicial y verificar que `wsl -d Ubuntu-24.04` funciona. Después:

```powershell
.\deploy\windows\Initialize-CbrsRuntime.ps1 -Apply -DistroName Ubuntu-24.04
```

El inicializador instala `/opt/cbrs`, Python 3.14, Chrome Linux, Xvfb, noVNC,
restic y las unidades systemd. No habilita ni inicia los servicios.

Si `systemctl` informa que WSL no arrancó con systemd, agregar dentro de Ubuntu:

```ini
[boot]
systemd=true
```

a `/etc/wsl.conf`, ejecutar `wsl --shutdown` desde PowerShell y volver a abrir la
distribución. Esto pertenece a la etapa de setup, no a la preparación actual.

## Etapa 2 — configuración protegida

Dentro de Ubuntu, completar sin pegar secretos en terminales compartidas ni en
argumentos de proceso:

```text
/etc/cbrs/cbrs.env                 0640 root:cbrs
/var/lib/cbrs/account-pool.json    0640 cbrs:cbrs
/etc/cbrs/restic-password          0640 root:cbrs
```

Usar `deploy/cbrs.env.example` y `deploy/account-pool.json.example`. Cada cuenta
debe tener:

- una referencia `username_env` distinta;
- una referencia `password_env`;
- una referencia `proxy_url_env` distinta;
- un `profile_dir` único bajo `/var/lib/cbrs/accounts`;
- cuota diaria aprobada (20 inicialmente).

Montar el segundo volumen o NAS antes de inicializar restic. La ruta de ejemplo
`/srv/cbrs-backup/restic` solo es válida si `/srv/cbrs-backup` realmente reside
en ese almacenamiento secundario. No configurar `forget` ni `prune`.

## Etapa 3 — gates sin búsquedas

Inicializar la base local y el repositorio de respaldo. Estos pasos no buscan en
CBRS, aunque restic puede contactar el NAS o backend configurado:

```bash
sudo -u cbrs bash -lc '
  set -a; source /etc/cbrs/cbrs.env; set +a
  cd /opt/cbrs
  .venv/bin/python -m cbrs jobs status --config /var/lib/cbrs/account-pool.json
  restic init
  .venv/bin/python -m cbrs jobs backup --config /var/lib/cbrs/account-pool.json
'
```

Después ejecutar el gate desde Windows con el probe local de Ubuntu:

```powershell
.\deploy\windows\Test-CbrsReadiness.ps1 -ProbeWslRuntime
```

## Etapa 4 — gates de red autorizados

Esta es la primera etapa que contacta servicios externos. Verificar una cuenta a
la vez y aprobar su egress únicamente si corresponde al proxy chileno dedicado:

```bash
sudo systemctl start cbrs-display.service cbrs-x11vnc.service cbrs-novnc.service
sudo -u cbrs bash -lc '
  set -a; source /etc/cbrs/cbrs.env; set +a
  cd /opt/cbrs
  .venv/bin/python -m cbrs pool proxy-health \
    --config /var/lib/cbrs/account-pool.json \
    --approve-egress-baseline
'
```

Confirmar que existe un baseline por cuenta y que ningún proxy se reutiliza.
Luego realizar primero un login automático con perfil vacío y una sola consulta
controlada. No saltar directamente a la prueba indefinida.

## Etapa 5 — escalamiento controlado antes de dejarlo indefinido

Orden obligatorio:

1. Un job con una sola cuenta y un resultado conocido.
2. Un job textual con múltiples resultados; validar un PDF por inscripción.
3. Reinicio del worker durante una descarga; comprobar recuperación sin
   duplicado ni PDF parcial publicado.
4. Tres cuentas, failover de sesión y proxy.
5. CAPTCHA en una cuenta; luego CAPTCHA en todas y recuperación visual.
6. Cuota controlada hasta 60 y verificación de que la solicitud 61 queda en
   `waiting_capacity`.
7. Reinicio completo de Ubuntu y verificación de servicios, cola y backup.

Cada alta debe usar una `idempotency_key` única y estable. Para una consulta
textual:

```bash
python -m cbrs jobs enqueue \
  --text "CONSULTA AUTORIZADA" \
  --idempotency-key "acceptance-YYYYMMDD-sequence"
```

No colocar la consulta en tickets, capturas ni reportes operativos.

## Etapa 6 — inicio de la prueba indefinida

Solo después de que el gate diga `LIVE READY`:

```powershell
.\deploy\windows\Start-CbrsIndefiniteTest.ps1 `
  -DistroName Ubuntu-24.04 `
  -AcknowledgeAuthorizedLiveTraffic
```

Esto inicia worker, dashboard, display privado, recuperación visual y timer de
backup. No crea búsquedas sintéticas: procesa únicamente jobs explícitos de
Poderalia o del plan de aceptación. Esa propiedad evita que un simple reinicio
genere tráfico no solicitado.

Estado y parada:

```powershell
.\deploy\windows\Get-CbrsIndefiniteStatus.ps1
.\deploy\windows\Stop-CbrsIndefiniteTest.ps1
```

La parada conserva SQLite, jobs pendientes, perfiles y PDFs. Para mantener el
dashboard visible mientras el worker está detenido, usar `-KeepDashboard`.

## Observación durante siete días

Revisar al menos diariamente:

- heartbeat del worker menor a 120 segundos;
- ausencia de un segundo lease activo;
- jobs por estado y progreso por inscripción;
- cuota usada/restante y próxima reposición en `America/Santiago`;
- cuentas `captcha_pending`, `credentials_invalid` o pausadas por proxy;
- safety stop global; nunca limpiarlo sin revisar la causa;
- último backup menor a 36 horas;
- espacio libre sobre 10 % tanto en outputs como en el repositorio;
- SHA-256, páginas y tamaño de cada PDF final.

Al terminar, detener graceful, ejecutar un backup final y exportar el estado
sanitizado. Los PDFs y snapshots no se eliminan.
