# Plan de validación del runtime productivo CBRS

> La ruta activa es Windows nativo. Toda referencia Ubuntu/WSL2 debajo es
> histórica; los criterios vigentes están en
> [`native-windows-endurance.md`](native-windows-endurance.md).

## 1. Gate offline

Ejecutar en Ubuntu y en Ubuntu/WSL2:

```bash
python -m compileall -q cbrs tests
python -m pytest -q
bash -n deploy/install-ubuntu.sh deploy/install-wsl.sh
python -m cbrs jobs --help
```

La suite debe cubrir:

- migración aditiva sobre la base actual del pool;
- WAL, foreign keys, `busy_timeout`, leases y recuperación de jobs abandonados;
- idempotencia y conflicto de claves;
- reserva atómica de cupos y cambio de fecha `America/Santiago`;
- descarga de todos los resultados, PDFs válidos, SHA-256 y estados parciales;
- failover por cuenta y hard stop global para rate limit/WAF;
- sesión persistente, login automático y redacción de secretos;
- API loopback, cancelación y confinamiento de artefactos;
- snapshot SQLite, restic, backup atrasado y espacio en disco.

## 2. Gate de infraestructura

Con el usuario y variables de los servicios:

```bash
python -m cbrs doctor
python -m cbrs pool proxy-health \
  --config /var/lib/cbrs/account-pool.json \
  --approve-egress-baseline
python -m cbrs jobs status
```

Aceptación:

- Google Chrome estable, Xvfb y `DISPLAY=:99` están disponibles;
- cada cuenta tiene un perfil y referencia de proxy diferentes;
- cada proxy resuelve a Chile, mantiene su hash aprobado y no comparte egress;
- reCAPTCHA Enterprise y `/api/v1/home/start` son accesibles;
- dashboard/API y noVNC escuchan exclusivamente en loopback;
- `/var/lib/cbrs`, `/var/log/cbrs` y el repositorio restic tienen permisos del
  usuario de servicio.

## 3. Gate funcional vivo autorizado

1. Partir con perfiles vacíos y confirmar login automático para tres cuentas.
2. Encolar una búsqueda textual conocida con múltiples resultados.
3. Verificar un `job_item` y PDF por inscripción, cabecera `%PDF-`, número de
   páginas, tamaño y hash.
4. Repetir la clave idempotente y confirmar que no se crea otra búsqueda.
5. Reiniciar el worker durante una descarga y confirmar recuperación sin
   duplicar artefactos completos.
6. Expirar una sesión y confirmar refresh o un único relogin automático.
7. Llevar una cuenta a CAPTCHA y confirmar failover a otra.
8. Llevar todas a CAPTCHA: no debe haber tráfico hasta **Resolver CAPTCHA** y
   **Validar y reactivar** mediante la vista noVNC/WSLg.
9. Cambiar el egress de una cuenta y confirmar pausa antes de una búsqueda.
10. Completar 60 solicitudes distribuidas 20/20/20; la 61 debe quedar en
    `waiting_capacity` hasta la siguiente fecha chilena.
11. Confirmar que un `429` o WAF crea un safety stop global y que un segundo
    worker no puede adquirir el lease.
12. Ejecutar el backup, restaurar SQLite y un PDF en un directorio temporal y
    comparar sus hashes.

## 4. Soak de aceptación

Ejecutar siete días con `cbrs-worker.service`, `cbrs-dashboard.service` y
`cbrs-backup.timer` activos. Revisar diariamente sin preparar sesiones:

- heartbeat y reinicios de systemd;
- distribución y reset de cupos;
- refresh/login de sesiones;
- CAPTCHA y tiempos de recuperación;
- jobs `partial` o `failed` y motivos sanitizados;
- edad del último backup y espacio libre;
- ausencia de credenciales, JWT, cookies, IPs o proxy URLs en SQLite, dashboard,
  journald y `/var/log/cbrs`.

La aceptación E2E termina únicamente después de este gate vivo. La suite offline
no puede demostrar credenciales, reputación de proxies, cuota real ni respuesta
actual del portal.
