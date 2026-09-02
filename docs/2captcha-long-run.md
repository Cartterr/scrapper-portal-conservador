# 2Captcha y proxy por cuenta

## Alcance real

CBRS mantiene dos rutas independientes:

1. Cada cuenta abre Chrome con su propio perfil y `CBRS_ACCOUNT_N_PROXY_URL`.
2. 2Captcha resuelve reCAPTCHA v3 Enterprise mediante
   `RecaptchaV3TaskProxyless` cuando se habilita el fallback.

2Captcha no permite enviar proxy en tareas reCAPTCHA v3/Enterprise v3. Por eso
el token del solver no sale por la IP del navegador. No se debe copiar el proxy
del navegador dentro del payload de `createTask`.

Para la validación finita se usan tres sesiones 2Captcha Residential Sticky de
Chile de 120 minutos, una por cuenta. No equivalen a identidad estable durante
días. La comparación y decisión están en
[`2captcha-proxy-options.md`](2captcha-proxy-options.md).

## Configuración recomendada para la prueba

Mantener los secretos únicamente en `C:\ProgramData\CBRS\cbrs.env`:

```dotenv
CBRS_CAPTCHA_SOLVER_MODE=2captcha_manual
CBRS_2CAPTCHA_API_KEY=REEMPLAZAR_EN_EL_HOST
CBRS_2CAPTCHA_MIN_SCORE=0.9
CBRS_2CAPTCHA_TIMEOUT_SECONDS=120
CBRS_2CAPTCHA_POLL_SECONDS=5
CBRS_2CAPTCHA_DAILY_LIMIT=60
CBRS_2CAPTCHA_CIRCUIT_BREAKER_SECONDS=300
CBRS_2CAPTCHA_REJECTION_COOLDOWN_SECONDS=300
CBRS_PROXY_RECHECK_SECONDS=300

CBRS_EJECUTIVO_1_PROXY_URL=http://LOGIN_1:PASSWORD@STATIC_HOST_1:PORT
CBRS_EJECUTIVO_2_PROXY_URL=http://LOGIN_2:PASSWORD@STATIC_HOST_2:PORT
CBRS_EJECUTIVO_3_PROXY_URL=http://LOGIN_3:PASSWORD@STATIC_HOST_3:PORT
```

Usar tres valores HTTP completos con sesiones distintas y `sessTime-120`
generados por 2Captcha. El instalador no supone host, puerto ni formato de
usuario; no se deben construir credenciales a mano.

Cuando las tres sesiones acumulen respuestas temporales aunque proveedor,
Chile, CBRS y reCAPTCHA sigan accesibles, se pueden renovar sus identificadores
sin volver a copiar secretos. El comando solo funciona con endurance pausado y
el worker detenido; crea un rollback protegido antes del reemplazo:

```powershell
.\deploy\windows\Set-CbrsResidentialProxySessions.ps1 -RotateCurrentSessions
```

Después se deben validar y reemplazar los tres baselines con el gate protegido
antes de reanudar el worker.

El modo requerido sigue siendo `2captcha_manual`: usa primero el token del
navegador. El control grande **🤖 2CAPTCHA AUTOMÁTICO** permite optar entre
autorizar cada solve o usar automáticamente un solve pagado solo después de un
rechazo real de CBRS. El estado se conserva localmente y el límite diario es 60,
igual al máximo teórico de solicitudes CBRS entre las tres cuentas.

## Gates antes de la validación finita

Con el worker detenido:

```powershell
.\deploy\windows\Stop-CbrsNative.ps1
.\.venv\Scripts\python.exe -m cbrs doctor
.\.venv\Scripts\python.exe -m cbrs captcha-health
.\.venv\Scripts\python.exe -m cbrs pool proxy-health
```

`captcha-health` consulta autenticación y saldo; no crea una tarea ni consume un
CAPTCHA. `pool proxy-health` debe mostrar país `CL`, script Enterprise accesible,
portal accesible y el mismo baseline aprobado para cada perfil.

Durante una migración, con endurance pausado y sin lease del worker, reemplazar
un baseline a la vez con `pool proxy-health --account <id>
--replace-egress-baseline`. El baseline anterior se archiva saneado.

Ejecutar después una sola solicitud controlada. Confirmar en los eventos que no
hay `captcha_solver_failed`, `proxy_health_failed`, `WAF` ni cambio de egreso.
Solo entonces iniciar la validación. En una prueba larga, el vencimiento de la
sesión residencial no instala el nuevo baseline a ciegas: el worker exige
proveedor activo con tráfico, país Chile, CBRS y reCAPTCHA accesibles y egreso
distinto de las otras cuentas. Si todo pasa, archiva el baseline saneado y lo
reemplaza atómicamente; si algo falla aplica un cooldown y vuelve a comprobar.

## Comportamiento de seguridad

- El solver pagado solo puede ejecutarse automáticamente cuando el operador
  mantiene activado el control **🤖 2CAPTCHA AUTOMÁTICO**.
- La autorización manual permite exactamente un solve, no se acumula y vence a
  los 15 minutos si no se consume.
- Un segundo rechazo deja solo esa cuenta en `captcha_pending`.
- Tras la autorización, el intento reanudado vuelve a cargar la página y genera
  primero un token Enterprise v3 del navegador. Solo si CBRS lo rechaza otra vez
  se reserva y consume el solve pagado.
- Si ya no existe ningún job en `waiting_captcha`, la autorización se cierra
  como `no requerido`, no arranca el worker y aparece con costo cero en la tabla
  de actividad 2Captcha.
- Un token pagado rechazado por CBRS aplica un cooldown de cinco minutos a esa
  cuenta antes de permitir otro solve pagado.
- Error de clave o saldo deshabilita el fallback externo hasta que
  `captcha-health` vuelva a confirmar autenticación y saldo positivo.
- Error de red, timeout o capacidad abre un circuito global de cinco minutos. Las
  cuentas sanas pueden continuar con tokens del navegador; no se encadenan
  intentos pagados entre cuentas.
- El egreso se vuelve a comprobar cada cinco minutos por defecto. Solo las
  cuentas `2captcha_residential_sticky` pueden renovar automáticamente su
  baseline y únicamente tras todos los gates anteriores. Cualquier otro
  proveedor sigue requiriendo reemplazo manual.
- `daily_limit` es el único bloqueo hasta el siguiente día. CAPTCHA, auth,
  proxy, indisponibilidad, 403/429 y WAF usan cooldowns con `resume_at`; WAF y
  rate-limit nunca se ignoran ni se reintentan inmediatamente.
- Claves, tokens, IPs y URLs de proxy se redactan y nunca deben entrar en Git.

2Captcha publica restricciones para sitios gubernamentales, financieros o de
alto riesgo. El gate vivo del proxy contra CBRS es obligatorio: que el proxy
tenga salida chilena no prueba que el proveedor permita alcanzar el portal.
