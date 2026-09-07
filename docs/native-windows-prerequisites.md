# Prerrequisitos de endurance CBRS en Windows

## Equipo

- Windows 11 x64 y una sesión de usuario que permanece iniciada.
- PowerShell nativo elevado para instalar y registrar Scheduled Tasks.
- Python 3.14, Google Chrome, Playwright y restic instalables de forma nativa.
- Sin WSL, Docker, Xvfb, hipervisor ni máquina virtual.
- `G:\CBRS` para estado y PDFs; `E:\CBRS-backup\restic` en un volumen físico
  distinto para respaldo cifrado.

## Autorizaciones y proveedores

- Tres cuentas CBRS nominales autorizadas, cada una con cuota diaria 20.
- Autorización escrita para repetir el fixture `9441 / 4580 / 1980`.
- Tráfico DataImpulse Mobile y tres puertos sticky de Chile de 120 minutos,
  distintos y provisionados para las tres cuentas. Residential queda como
  fallback explícito.
- Credenciales proxy del plan móvil; el email/password del dashboard y las
  credenciales de otros planes no son intercambiables.
- Una cuenta CapSolver o 2Captcha con saldo cuando se habilite el fallback
  reCAPTCHA Enterprise y autorización para el presupuesto configurado.
- Reserva production: cinco slots por cuenta; endurance: máximo 15 por cuenta.

## Secretos y red

- Guardar credenciales, proxies y API key solo en
  `C:\ProgramData\CBRS\cbrs.env` con herencia ACL removida y acceso limitado al
  usuario operador y `SYSTEM`.
- Guardar el password restic en `C:\ProgramData\CBRS\restic-password` con la
  misma política ACL.
- No guardar IPs crudas, proxy URLs, cookies, tokens solución ni solver worker
  IPs en Git, logs, reportes o SQLite.
- El dashboard debe escuchar únicamente en `127.0.0.1`.

## Navegador soportado

- Producción usa Google Chrome normal controlado por Playwright, perfiles
  persistentes aislados y `CBRS_HEADLESS=1` por defecto.
- GoLogin/Orbita, Dolphin y navegadores anti-detect quedan fuera de la solución
  soportada. No se instalan ni se seleccionan para recuperación. La política
  del worker y readiness rechaza configuraciones de esos navegadores. No se
  desinstalan aplicaciones existentes ni se toca ningún perfil abierto.

## Gate de activación

Antes de habilitar endurance deben aprobarse tres baselines estables, únicos y
chilenos. Readiness debe confirmar el solver seleccionado sin crear una tarea,
transporte browser-only, tareas registradas, primer backup exitoso y ausencia de
leases stale. El arranque siempre requiere
`-AcknowledgeAuthorizedLiveTraffic`.
