# Recuperación E2E de sesiones persistentes por cuenta

Este documento describe el procedimiento comprobado el **31 de agosto de 2026**
para recuperar las tres sesiones CBRS sin mezclar cuentas, perfiles ni rutas de
salida. Es la referencia operativa para el cliente y para cualquier LLM que deba
diagnosticar el runtime.

> Alcance exacto: se recuperó y verificó la autenticación persistente de las tres
> cuentas. Esto **no** demuestra que CapSolver o 2Captcha hayan sido aceptados por
> CBRS. Un token externo solo cuenta como éxito cuando el mismo intento termina
> con una aceptación positiva y explícita de CBRS.

## Resultado verificado

La recuperación se realizó con Windows, Python y Google Chrome nativos. No se
usó WSL, Docker, una VM ni un perfil temporal como reemplazo de producción.

Para cada una de las tres cuentas:

1. se abrió su perfil Chrome durable de producción;
2. se aplicó exclusivamente la ruta proxy asignada a esa misma cuenta;
3. `BrowserSession.ensure_authenticated(...)` recargó la ruta protegida y el
   detector confirmó el formulario completo `authenticated_form`;
4. no fue necesario enviar nuevamente el formulario de login;
5. se conservó abierto el contexto Chrome después de validar la sesión;
6. el dashboard informó `browser_live=true`,
   `browser_authenticated=true`, `browser_mode=headless` y
   `browser_status=authenticated_form_visible`.

La prueba no ejecutó búsquedas, no descargó PDFs, no consumió cupo y no creó un
CAPTCHA. Las tareas programadas y el worker normal permanecieron detenidos, y la
cola existente no fue modificada.

## Qué estaba mal

Había tres problemas diferentes y era importante no confundirlos:

### 1. Una prueba manual cruzó identidad y ruta

Una ventana temporal se abrió con el perfil y proxy asignados a
`ejecutivo_3`, pero dentro de esa ventana se intentó autenticar la identidad
asignada a `ejecutivo_2`. Ese resultado no era una prueba válida de la salud de
ninguna de las dos rutas.

CBRS puede correlacionar sesión, cookies, perfil, IP de salida y señales del
navegador. Por eso una cuenta nunca debe probarse desde el perfil o proxy de
otra, aunque ambas rutas salgan por Chile.

### 2. El ciclo anterior abría y cerraba Chrome por trabajo

Cerrar Chrome no equivale necesariamente a cerrar la sesión en CBRS: las
cookies permanecen en el perfil persistente. Sin embargo, abrir, refrescar o
autenticar y cerrar el navegador para cada PDF produce churn innecesario y hace
mucho más difícil distinguir una sesión expirada de un problema temporal del
portal.

La arquitectura corregida conserva un contexto de navegador por cuenta durante
toda la vida del worker. La implementación está en
`cbrs/jobs.py::_PersistentAccountBrowsers`; el contexto se crea una vez cuando
la cuenta se usa por primera vez y se reutiliza en trabajos posteriores.

### 3. “Disponible” no significaba “sesión viva”

`status=available` solo significa que la cuenta es elegible según cupo,
cooldown y estado del scheduler. No prueba que exista un proceso Chrome ni que
la cookie esté autenticada.

El dashboard ahora separa ambos conceptos. La fuente de verdad para la sesión
es la combinación de `worker_active`, `browser_live`,
`browser_authenticated`, `browser_auth_state`, `browser_status`, `browser_mode` y
`browser_checked_at`. La insignia correcta es **Sesión saludable**; no basta con
ver **Disponible**.

## Invariante principal: cuenta = perfil = proxy

La configuración protegida conserva esta relación uno a uno:

| Cuenta lógica | Usuario | Contraseña | Puerto DataImpulse | Perfil nativo |
|---|---|---|---|---|
| `ejecutivo_1` | `CBRS_EJECUTIVO_1_USERNAME` | `CBRS_EJECUTIVO_1_PASSWORD` | `10000` | `G:\CBRS\accounts\ejecutivo_1\chrome-profile` |
| `ejecutivo_2` | `CBRS_EJECUTIVO_2_USERNAME` | `CBRS_EJECUTIVO_2_PASSWORD` | `10001` | `G:\CBRS\accounts\ejecutivo_2\chrome-profile` |
| `ejecutivo_3` | `CBRS_EJECUTIVO_3_USERNAME` | `CBRS_EJECUTIVO_3_PASSWORD` | `10002` | `G:\CBRS\accounts\ejecutivo_3\chrome-profile` |

Los valores reales viven únicamente en `C:\ProgramData\CBRS\cbrs.env`. El
repositorio guarda referencias, nunca usuarios completos, contraseñas, cookies,
tokens ni URLs proxy con credenciales. El runtime construye tres rutas
DataImpulse Residential Proxy en memoria desde una credencial protegida común,
el parámetro `cr.cl;sessttl.120` y un puerto sticky único por cuenta.

No se debe “probar rápido” una identidad con otro perfil, copiar cookies entre
perfiles, reutilizar una URL proxy, cambiar el orden de las cuentas ni crear un
perfil manual alternativo. Si cambia una ruta, se vuelve a ejecutar
proxy-health/preflight y se aprueba el nuevo baseline antes de autenticar.

## Secuencia de recuperación segura

### 1. Congelar tráfico sin perder estado

Pausar endurance y detener el worker antes de tocar perfiles. No cancelar la
cola, no borrar SQLite y no eliminar perfiles.

```powershell
.\.venv\Scripts\python.exe -m cbrs jobs endurance pause
.\deploy\windows\Stop-CbrsNative.ps1
.\deploy\windows\Get-CbrsNativeStatus.ps1
```

Confirmar además que no quede otro worker, lease vigente o Chrome usando uno de
los tres directorios de producción. Un perfil bloqueado por otro proceso no se
debe forzar ni copiar.

### 2. Validar configuración sin revelar secretos

Revisar `G:\CBRS\account-pool.json` y confirmar para cada cuenta:

- `username_env`, `password_env` y `dataimpulse_port` correctos;
- proveedor `dataimpulse_residential_sticky` y tres puertos distintos;
- un directorio de perfil distinto;
- proveedor y marca esperados;
- país `CL`, proxy-health `passed` y baseline `matched`.

No ejecutar comandos que impriman el contenido de `cbrs.env`. El dashboard
puede mostrar host y puerto para el administrador local, pero los reportes y
documentos nunca deben incluir credenciales de proxy.

### 3. Recuperar una cuenta con su pareja exacta

El orden interno de una única invocación de
`BrowserSession.ensure_authenticated(...)` es:

1. abrir el contexto persistente con el perfil y proxy de esa cuenta;
2. comprobar la cookie mediante una llamada browser-origin;
3. si la recarga muestra el formulario protegido completo, devolver `refreshed`
   sin enviar credenciales;
4. si no está vigente, intentar una sola vez el flujo browser-origin soportado;
5. si aún no hay sesión, intentar una sola vez el formulario real del portal;
6. si no se confirma autenticación, detener esa cuenta con `auth_required`.

La regla es **una invocación acotada**, nunca un bucle de login. Un rechazo, un
timeout o `temporary_unavailable` no autoriza a martillar el portal.

Para recuperación visual individual, con el worker detenido:

```powershell
.\deploy\windows\Open-CbrsNativeRecovery.ps1 `
  -Account ejecutivo_1 `
  -AcknowledgeAuthorizedLiveTraffic
```

Repetir con otra cuenta solo después de cerrar correctamente la ventana previa y
verificar que el identificador, perfil y proxy pertenecen a la nueva cuenta.

### 4. Mantener el contexto vivo

En producción, el owner debe ser el worker que posee el lease. Ese worker crea
`_PersistentAccountBrowsers` una vez y mantiene su diccionario de contextos
durante toda su vida. El bloque `with browser_pool.session(...)` devuelve el
mismo scraper para la cuenta; no cierra Chrome al terminar cada job.

Solo se descarta un contexto cuando existe una falla real de conexión del
navegador, se detiene el worker o termina el proceso. Un error funcional de CBRS
no debe provocar logout, borrado de cookies ni cambio de IP.

Los gates de arranque abren y autentican una vez cada contexto habilitado antes
de aceptar jobs. Luego el reconciliador comprueba los tres cada 30 segundos,
recarga tras dos estados DOM desconocidos consecutivos y reautentica con un
piso de 60 segundos. Solo relanza el contexto desconectado.

### 5. Verificar el estado real

El dashboard escucha en loopback:

```powershell
$status = Invoke-RestMethod http://127.0.0.1:8765/api/status
$status.accounts |
  Select-Object account_id,status,worker_active,browser_live,browser_authenticated,browser_mode,browser_status,browser_checked_at
```

Para cada cuenta lista para uso se espera:

```text
worker_active         True
browser_live          True
browser_authenticated True
browser_status        authenticated
browser_checked_at    reciente
```

`browser_owner` debe coincidir con el owner del lease activo; el backend del
dashboard descarta como stale cualquier estado de navegador cuyo owner ya no
sea el worker vigente. También se debe verificar en Task Scheduler o en la lista
de procesos que existe el worker esperado: durante una recuperación temporal,
un lease de keeper puede hacer visible `worker_active=true` aunque el worker de
jobs siga detenido.

### 6. Reanudar trabajos solo después del gate

No reanudar endurance ni el procesamiento ordinario hasta que las tres cuentas
requeridas tengan ruta validada y sesión saludable. Después del arranque normal:

```powershell
.\deploy\windows\Start-CbrsNative.ps1 -AcknowledgeAuthorizedLiveTraffic
.\deploy\windows\Get-CbrsNativeStatus.ps1
```

Volver a consultar `/api/status` y comprobar que el owner cambió al worker real,
que los contextos siguen vivos después de un job y que la cola avanza sin crear
nuevos logins por PDF.

## Cómo tratar `temporary_unavailable`

`temporary_unavailable:http_400:intente-mas-tarde` es una respuesta genérica de
CBRS. Por sí sola no demuestra:

- que la contraseña sea incorrecta;
- que la cuenta haya cerrado sesión;
- que el proxy esté bloqueado;
- que un CAPTCHA externo haya sido rechazado.

Se conserva la misma cuenta, perfil y ruta; se aplica el cooldown acotado y se
prueba otra cuenta elegible. El backoff externo configurado es de 120 segundos y
una búsqueda exitosa lo limpia. Nunca se cambia de identidad ni se aumenta la
concurrencia para eludir la respuesta.

Un solve externo solo puede registrarse como exitoso si hay dos evidencias en el
mismo intento: el proveedor generó una solución y CBRS la aceptó explícitamente.
`portal_status=indeterminate`, `sin_decision` o `temporary_unavailable` siguen
siendo resultados no confirmados.

### Cadena externa acotada

Cuando el modo configurado es `capsolver_manual`, la preferencia automática está
activa y existen ambas claves protegidas, el orden es determinista:

1. token Enterprise v3 generado por el navegador real;
2. CapSolver proxy-bound únicamente tras un rechazo CAPTCHA explícito;
3. un solo intento 2Captcha si CapSolver falla antes de enviar el token o si
   CBRS rechaza explícitamente el token CapSolver;
4. detenerse después de ese intento y registrar proveedor, costo, demora y
   resultado CBRS sanitizado.

CapSolver y 2Captcha comparten el límite diario y el circuito de gasto. La
segunda reserva nunca se crea por `temporary_unavailable`, `intente-mas-tarde`,
un error de transporte o un resultado indeterminado. La generación correcta de
un token tampoco es un resultado positivo por sí sola.

## Keeper usado en la validación del 31-08-2026

Para aislar la recuperación de la cola se usó un keeper temporal que:

- abrió los tres perfiles de producción en modo headed;
- ejecutó una sola validación acotada por cuenta;
- mantuvo un lease y actualizó el estado local cada 15 segundos;
- comprobó que el contexto y la página siguieran vivos;
- no realizó búsquedas ni llamadas periódicas a CBRS;
- finaliza al cambiar la fecha local, cerrar una ventana, perder el lease,
  terminar Codex o apagar el PC.

Ese keeper fue una herramienta de diagnóstico, **no** un servicio durable ni un
reemplazo del worker. No sobrevive un reinicio y no entrega sus procesos
Playwright a otro proceso. El worker normal debe abrir sus propios contextos
persistentes y conservarlos durante su vida.

## Criterio de aceptación para el cliente

La recuperación E2E se considera aprobada únicamente cuando:

- las tres relaciones cuenta/perfil/proxy son únicas y coinciden con la
  configuración protegida;
- proxy-health es `passed`, país es `CL` y baseline es `matched`;
- cada cuenta muestra `authenticated_form` después de la recarga protegida;
- el dashboard muestra **Sesión saludable** con timestamp reciente;
- el owner de cada navegador coincide con el lease vigente;
- un job posterior reutiliza el mismo contexto y no cierra Chrome;
- detener el worker cierra los contextos limpiamente y marca
  `browser_status=worker_stopped`;
- no se imprimieron ni persistieron secretos;
- una respuesta temporal se manejó con backoff/failover, no con relogin.

## Señales de que la recuperación no es válida

Detenerse y corregir antes de emitir tráfico si ocurre cualquiera de estas
condiciones:

- el usuario visible no corresponde al `ejecutivo_N` de la ventana;
- un perfil temporal o nuevo aparece en lugar del perfil durable;
- dos cuentas comparten proxy, directorio o cookie;
- el dashboard dice `available` pero Chrome está detenido;
- `browser_owner` no coincide con el lease;
- el código cierra el contexto después de cada PDF;
- se repite login para “ver si ahora funciona”;
- se interpreta un token de solver como aceptación de CBRS;
- se reactiva la cola antes de completar el gate de las tres sesiones.
