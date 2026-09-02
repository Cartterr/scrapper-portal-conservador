# Operación CBRS con DataImpulse

Esta es la referencia de onboarding y recuperación para el cliente y para un LLM
que opere el runtime local. El entorno soportado es Windows nativo con Chrome
headless persistente; no usa Docker, WSL, VM ni servicios cloud propios.

## Contrato operativo

- DataImpulse Residential Proxy es el egreso recomendado para CBRS.
- Cada cuenta conserva una pareja exclusiva: identidad CBRS, perfil Chrome y
  puerto sticky DataImpulse.
- Los puertos iniciales son `10000`, `10001` y `10002`; el rango de recuperación
  es `10000–20000` y la sesión solicita `sessttl.120` con país `cl`.
- El worker que posee el lease es el único dueño de los navegadores. Mantiene
  las tres ventanas durante idle, cooldown y errores funcionales del portal.
- 2Captcha y CapSolver son solvers CAPTCHA independientes. Sus claves nunca se
  usan como proxy DataImpulse.

DataImpulse documenta los
[puertos sticky](https://docs.dataimpulse.com/proxies/types-of-connections), el
[intervalo `sessttl`](https://docs.dataimpulse.com/proxies/parameters/session-interval)
y su [taxonomía de errores](https://docs.dataimpulse.com/errors). La modalidad
residencial normal no requiere automatizar el dashboard: seleccionar otro puerto
sticky controla la recuperación. Las credenciales administrativas opcionales
`DATAIMPULSE_EMAIL`/`DATAIMPULSE_PASSWORD` no son credenciales proxy ni una API.

## Configuración y secretos

Usar `.env.example` como inventario y escribir valores reales solamente en
`C:\ProgramData\CBRS\cbrs.env`, cuya ACL debe limitarse al usuario autorizado y
`SYSTEM`. `G:\CBRS\account-pool.json` contiene `dataimpulse_port`, nunca una URL
con usuario o contraseña. La fuente protegida y el `.env` local deben mantenerse
en paridad mediante el script transaccional de migración.

Claves principales:

```dotenv
CBRS_EGRESS_MODE=residential_sticky
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_HEADLESS=1
CBRS_WINDOW_MODE=normal
DATAIMPULSE_PROXY_HOST=gw.dataimpulse.com
DATAIMPULSE_COUNTRY=cl
DATAIMPULSE_STICKY_TTL_MINUTES=120
DATAIMPULSE_STICKY_PORT_MIN=10000
DATAIMPULSE_STICKY_PORT_MAX=20000
```

La URL proxy se compone y codifica solo en memoria. Una cuenta DataImpulse que
también declare `proxy_url_env` es inválida por precedencia ambigua. Nunca
imprimir el archivo protegido, URLs proxy completas, IPs de egreso, cookies,
claves de solver ni credenciales del portal.

## Evidencia de autenticación

El detector es fail-closed y devuelve exactamente uno de estos estados:

| Estado | Evidencia | Decisión |
|---|---|---|
| `authenticated_form` | Sección visible `Búsqueda por foja, número y año`, tres inputs y botones `Buscar`/`Limpiar` visibles | Autenticado |
| `login_gate` | Tarjeta visible completa `Para acceder debe iniciar sesión` | Reautenticar con backoff |
| `unknown` | Ninguna firma completa dentro del timeout | No autenticar; observar y recargar acotadamente |
| `conflict` | Ambas firmas aparecen | No autenticado |

Una respuesta de refresh, una cookie o la ausencia de la tarjeta de login no son
prueba positiva. `has_active_login()` exige recargar la ruta protegida y observar
`authenticated_form`. El reconciliador puede promover o degradar el estado del
dashboard y conserva la evidencia exacta con timestamp.

## Ciclo de vida y recuperación

Cada 30 segundos el worker:

1. verifica los contextos conocidos sin cerrarlos;
2. reautentica en el mismo perfil cuando aparece `login_gate`, con un piso de
   60 segundos por cuenta;
3. recarga una vez tras dos estados `unknown` consecutivos;
4. relanza solo un contexto cerrado o desconectado;
5. cierra ordenadamente todos los contextos al detener el servicio.

El watchdog consulta el lease durable, no solo el estado `Running` de Task
Scheduler. Reinicia un worker nominalmente activo únicamente cuando el heartbeat
del lease está vencido, con gracia de arranque, para evitar dos dueños de los
mismos perfiles.

## Decisión de rotación

No toda falla de CBRS implica cambiar de IP:

- el primer `temporary_unavailable` hace failover a otra cuenta;
- se considera rotación tras dos ocurrencias de la misma cuenta en diez minutos
  solo si otra cuenta tuvo éxito reciente;
- si fallan todas las cuentas se conserva el backoff global `300/900/3600`;
- `500`, `502 NO_HOST_CONNECTION`, `503 NO_RAY`, reset o probe fallido se
  reintentan una vez y luego permiten recuperación de ruta;
- `407 NO_USER`, credenciales inválidas, usuario bloqueado, puerto prohibido o
  tráfico/hilos agotados pausan la cuenta con estado accionable; no rotan.

La recuperación es de dos fases. Se reserva el siguiente puerto no usado, se
prueba conectividad del proveedor, país Chile, CBRS/reCAPTCHA y unicidad del
egreso. Solo al superar todo se promueve el puerto, se archiva y reemplaza el
baseline saneado atómicamente y se relanza/reautentica esa cuenta. Un candidato
fallido conserva el puerto, baseline y Chrome anteriores. El límite es una
promoción por cinco minutos y tres por hora por cuenta; después queda
  `proxy_recovery_exhausted`.

## Dashboard y diagnóstico

La cabecera debe separar `3/3 Chrome live` de `3/3 protected forms
authenticated`. Por cuenta se revisan: evidencia DOM y timestamp, edad del
contexto, lease owner, último health-check, puerto/ruta sanitizada, TTL,
generación, última rotación, cooldown y resultado de recuperación. Nunca se
muestran credenciales ni IPs crudas.

Orden de diagnóstico:

1. confirmar un solo lease vigente y tareas activas;
2. verificar `browser_live` y luego `browser_auth_state`;
3. distinguir `login_gate`, `unknown` y una caída real del contexto;
4. revisar salud de ruta y clasificación del proveedor;
5. rotar solo cuando la política lo permita;
6. tratar CAPTCHA y disponibilidad global como circuitos separados.

## Migración y rollback

Con el worker detenido, ejecutar desde PowerShell elevado:

```powershell
.\deploy\windows\Set-CbrsDataImpulseProxySessions.ps1
.\deploy\windows\Start-CbrsNative.ps1 -AcknowledgeAuthorizedLiveTraffic
```

El migrador valida las tres URLs heredadas sin mostrarlas, crea un respaldo bajo
`C:\ProgramData\CBRS\rollback`, migra la credencial común y los tres puertos, y
escribe env/pool atómicamente. Tras arrancar, readiness debe confirmar tres
rutas Chile distintas, tres perfiles, tres Chrome headless y tres formularios
protegidos. Si falla cualquier gate de ruta, autenticación, aislamiento o
redacción, detener el worker y restaurar los archivos del último respaldo; no
borrar perfiles, SQLite ni baselines durante el rollback.
