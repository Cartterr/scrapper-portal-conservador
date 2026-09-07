# Operación CBRS con DataImpulse

> **Preservación obligatoria (06-09-2026):** conservar todo contexto Chrome de
> producción existente hasta reboot/apagado del PC o detención explícita de TODO
> el servicio. No reiniciar workers para cargar fixes. No rotar/promover una ruta
> que cierre un contexto existente. Una cuenta que nunca se autenticó puede probar
> candidatos separados: adoptar el candidato autenticado sin cerrarlo y conservar
> el contexto anterior abierto/inactivo. Una cuenta con sesión comprobada no rota.
> Los procedimientos históricos
> de rotación/reinicio de este documento solo aplican sin contexto existente o
> después de esa detención explícita. [AGENTS.md](../AGENTS.md) tiene prioridad.

Esta es la referencia de onboarding y recuperación para el cliente y para un LLM
que opere el runtime local. El entorno soportado es Windows nativo con Chrome
headless persistente; no usa Docker, WSL, VM ni servicios cloud propios.

## Contrato operativo

- DataImpulse Mobile Proxy es el egreso primario recomendado para CBRS. El plan
  residencial permanece soportado como fallback explícito.
- Cada cuenta conserva una pareja exclusiva: identidad CBRS, perfil Chrome y
  puerto sticky DataImpulse.
- Los puertos iniciales son `10000`, `10001` y `10002`; el rango de recuperación
  es `10000–20000` y la sesión solicita `sessttl.120` con país `cl`.
- Cada plan tiene credenciales proxy propias. Producción usa las credenciales
  del plan **Mobile Proxy**; las residenciales y las del dashboard no son
  intercambiables.
- El worker que posee el lease es el único dueño de los navegadores. Mantiene
  las tres ventanas durante idle, cooldown y errores funcionales del portal.
- 2Captcha y CapSolver son solvers CAPTCHA independientes. Sus claves nunca se
  usan como proxy DataImpulse.

DataImpulse documenta los
[puertos sticky](https://docs.dataimpulse.com/proxies/types-of-connections), el
[intervalo `sessttl`](https://docs.dataimpulse.com/proxies/parameters/session-interval)
y su [taxonomía de errores](https://docs.dataimpulse.com/errors). La modalidad
normal no requiere automatizar el dashboard: seleccionar otro puerto sticky
controla la recuperación. `gw.dataimpulse.com` permanece visible al rotar: el
puerto identifica la sesión peer y otro puerto obtiene otra salida. Las credenciales administrativas opcionales
`DATAIMPULSE_EMAIL`/`DATAIMPULSE_PASSWORD` no son credenciales proxy ni una API.

## Configuración y secretos

Usar `.env.example` como inventario y escribir valores reales solamente en
`C:\ProgramData\CBRS\cbrs.env`, cuya ACL debe limitarse al usuario autorizado y
`SYSTEM`. `G:\CBRS\account-pool.json` contiene `dataimpulse_port`, nunca una URL
con usuario o contraseña. La fuente protegida y el `.env` local deben mantenerse
en paridad mediante el script transaccional de migración.

Claves principales:

```dotenv
CBRS_EGRESS_MODE=mobile_sticky
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_HEADLESS=1
CBRS_WINDOW_MODE=normal
DATAIMPULSE_PROXY_HOST=gw.dataimpulse.com
DATAIMPULSE_PROXY_SCHEME=http
DATAIMPULSE_COUNTRY=cl
DATAIMPULSE_STICKY_TTL_MINUTES=120
DATAIMPULSE_STICKY_PORT_MIN=10000
DATAIMPULSE_STICKY_PORT_MAX=20000
# DATAIMPULSE_ASN=REPLACE_WITH_VALIDATED_CHILE_MOBILE_ASN
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

Cuando no hay sesión, el flujo vuelve a la ruta protegida y pulsa el enlace
visible `/login/%2Fconsultas-en-linea%2Findices%2Findice-del-registro-de-comercio`.
No carga ese URL codificado como navegación cruda: el portal puede devolver una
página Spring **Whitelabel 400**. El formulario real es el método primario; el
POST browser-origin queda como fallback acotado solo si la UI no renderiza.

## Ciclo de vida y recuperación

Cada 30 segundos el worker:

1. verifica los contextos conocidos sin cerrarlos;
2. reautentica en el mismo perfil cuando aparece `login_gate`, con un piso de
   60 segundos por cuenta;
3. recarga una vez tras dos estados `unknown` consecutivos;
4. relanza solo un contexto cerrado o desconectado;
5. cierra ordenadamente todos los contextos al detener el servicio.

El watchdog consulta el lease durable. Si el heartbeat de un worker existente
está vencido, emite una advertencia y conserva el proceso y Chrome; no los mata.
Solo inicia un worker ausente cuando no hay lease vigente ni procesos anteriores.
Las tareas deshabilitadas por el operador permanecen deshabilitadas.

## Decisión de rotación

### Recuperación móvil sin perder el navegador aceptado

- Reservar un puerto aleatorio libre de `10000–20000`, excluyendo puertos activos,
  pendientes y rechazados. El proveedor asigna la IP; elegir un puerto no garantiza
  por sí solo una IP diferente.
- Comprobar país, reachability y hash de egreso. Rechazar el hash de la ruta
  anterior, el de otra cuenta y salidas rechazadas en las últimas dos horas,
  incluso si el puerto es diferente.
- Crear un perfil aislado para el candidato, entrar por el formulario normal y
  exigir `authenticated_form`. No modificar los perfiles/Chrome existentes.
- Si funciona, adoptar **ese mismo objeto/contexto vivo**. No cerrarlo para
  reabrirlo, no repetir login después de probarlo. El contexto anterior queda
  conservado e inactivo hasta detener TODO el servicio.
- Si el candidato nunca se autenticó, cerrar solo ese candidato descartable.
  Si autenticó pero falló la persistencia, conservarlo abierto para inspección y
  bloquear nuevos candidatos para esa cuenta.
- Una cuenta que ya mostró un formulario protegido queda excluida de rotación,
  aunque después cambie a `unknown`.

Cuando ninguna cuenta proxy ha tenido éxito reciente, dos rechazos de **login**
HTTP 400 permiten una prueba Mobile acotada después de respetar el cooldown
global: máximo tres candidatos por hora para todo el pool y cinco minutos entre
ellos. Este presupuesto es durable y adicional al límite por cuenta. No aplica
a CAPTCHA/WAF explícito, credenciales inválidas ni errores terminales del proveedor.
Un resultado fallido conserva el backoff global; no demuestra un ban ni lo elude
mediante un bucle de IPs. El evento `dataimpulse_recovery_evaluated` indica si
se reservó esta prueba mediante `mobile_canary_reserved`.

Los errores de autenticación del arranque y del reconciliador se envían a la
misma política que los jobs: se contabilizan, se publican como
`background_auth_failed` y `dataimpulse_recovery_evaluated`, y respetan pausas
por cuenta, CAPTCHA y circuitos globales antes del próximo intento. Para una
recuperación de **login**, un `authenticated_form` reciente de otra cuenta viva
y perteneciente al lease vigente también es evidencia de disponibilidad. Para
una recuperación de **consulta**, sigue siendo necesaria una búsqueda o descarga
exitosa reciente. Una imagen del login o un proxy saludable no cumplen ese gate.

El dashboard separa la última comprobación DOM de la última autenticación
confirmada y conserva el último motivo/HTTP del login rechazado hasta observar
el formulario protegido. `unknown` describe únicamente el DOM, no borra el error.

El soporte corrigió específicamente el uso de credenciales residenciales en las
primeras pruebas llamadas móviles. Perfiles limpios y ASN alternativos no habían
resuelto ese caso. La recuperación automática conserva el proveedor Mobile
configurado; no cambia a GoLogin, Dolphin, SOCKS5 ni un ASN sin validación propia.

No toda falla de CBRS implica cambiar de IP:

- el primer `temporary_unavailable` hace failover a otra cuenta;
- se considera rotación tras dos ocurrencias de la misma cuenta en diez minutos
  solo si otra cuenta tuvo éxito reciente;
- si fallan todas las cuentas se conserva el backoff global `300/900/3600`;
- `500`, `502 NO_HOST_CONNECTION`, `503 NO_RAY`, reset o probe fallido se
  reintentan una vez y luego permiten recuperación de ruta;
- `407 NO_USER`, credenciales inválidas, usuario bloqueado, puerto prohibido o
  tráfico/hilos agotados pausan la cuenta con estado accionable; no rotan.

El JSON `400` con código `intente-mas-tarde` y el alert “Se ha detectado un
problema…” vienen de CBRS después de Imperva y reCAPTCHA Enterprise; no prueban
una falla del túnel. Son un rechazo temporal de ruta: failover primero y
rotación solo con la evidencia y límites anteriores. La página Whitelabel 400
durante la entrada al login recibe la misma clasificación, no “clave inválida”.

La recuperación es de dos fases. Se reserva el siguiente puerto no usado, se
prueba conectividad del proveedor, país Chile, CBRS/reCAPTCHA, unicidad del
egreso y una autenticación real que renderice `authenticated_form`. La prueba
usa un perfil limpio `chrome-profile-route-<generación>-port-<puerto>`; un puerto rechazado
queda registrado para que el siguiente intento no vuelva al mismo peer. Solo
entonces se promueve el puerto, se archiva y reemplaza el baseline saneado y se
selecciona el candidato vivo sin cerrar el contexto anterior. Los otros dos contextos permanecen vivos. El límite es una
promoción por cinco minutos y tres por hora por cuenta; después queda
  `proxy_recovery_exhausted`.

Para una prueba controlada o recuperación operativa, la solicitud debe enviarse
al worker que ya posee el lease y los tres contextos. El comando no contiene ni
imprime credenciales y solo reinicia el contexto de la cuenta indicada:

```powershell
.\.venv\Scripts\python.exe deploy\run_with_env.py C:\ProgramData\CBRS\cbrs.env -- `
  .\.venv\Scripts\python.exe -m cbrs jobs proxy-rotate `
  --account ejecutivo_2 --reason controlled_e2e_recovery `
  --acknowledge-authorized-live-traffic
```

La respuesta exitosa debe mostrar un puerto nuevo, una generación incrementada
y estado `active`. Después se confirma `authenticated_form` y que los procesos
padre de las otras dos cuentas no cambiaron.

Un candidato que responde `temporary_unavailable` durante la autenticación no se
promueve. La ruta y el Chrome anteriores permanecen activos hasta que un nuevo
peer supera todos los gates.

## Protocolos, ASN y navegadores de diagnóstico

- La ruta productiva probada usa HTTP y un puerto sticky `10000–20000`.
- `74.81.81.81`, HTTPS/823 y SOCKS5/824 sirven para aislar problemas de gateway
  o protocolo; no sustituyen la rotación del puerto sticky.
- `DATAIMPULSE_ASN` agrega `asn.<n>` a `__cr.cl;...`. Consume el doble de tráfico
  y queda vacío hasta seleccionar y validar un ASN móvil chileno concreto.
- GoLogin/Orbita y Dolphin Anty quedan retirados del flujo soportado, incluyendo
  onboarding y recuperación. La validación real del 03-09-2026 mostró que Chrome normal
  con la ruta móvil logró el formulario protegido y un PDF válido; por ello el
  servicio soportado conserva Chrome/Playwright.
- La configuración del backend se valida antes de adquirir el lease del worker
  y antes de abrir una sesión o reservar un candidato de recuperación. Chrome
  no puede sustituirse silenciosamente por Edge u otro ejecutable. Esta política
  no reinicia procesos existentes ni convierte ventanas headed a headless.

## Evidencia de aceptación 03-09-2026

La aceptación real exige, dentro de una misma sesión proxy:

1. salida chilena única;
2. entrada al login desde la tarjeta protegida;
3. POST de login terminado en `authenticated_form`;
4. búsqueda FNA autorizada con el resultado esperado;
5. artefacto `%PDF` con páginas y tamaño válidos;
6. segunda comprobación del perfil que vuelve a mostrar `authenticated_form`.

El 03-09-2026 esa cadena se completó con Chrome headed, DataImpulse Mobile y el
puerto sticky `10002`, produciendo un PDF válido de tres páginas. Headed fue el
modo de observación; el servicio persiste `CBRS_HEADLESS=1`.

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
.\deploy\windows\Set-CbrsDataImpulseNetwork.ps1 -Network mobile
.\deploy\windows\Start-CbrsNative.ps1 -AcknowledgeAuthorizedLiveTraffic
```

El normalizador valida las credenciales protegidas y los tres puertos sin
mostrarlos, crea un respaldo bajo `C:\ProgramData\CBRS\rollback` y cambia en
paridad env/pool a proveedor móvil. El importador de URLs heredadas sigue
disponible como `Set-CbrsDataImpulseProxySessions.ps1 -Network mobile`. Tras arrancar, readiness debe confirmar tres
rutas Chile distintas, tres perfiles, tres Chrome headless y tres formularios
protegidos. Si falla cualquier gate de ruta, autenticación, aislamiento o
redacción, detener el worker y restaurar los archivos del último respaldo; no
borrar perfiles, SQLite ni baselines durante el rollback.
