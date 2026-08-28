# 2Captcha y proxy por cuenta

## Alcance real

CBRS mantiene dos rutas independientes:

1. Cada cuenta abre Chrome con su propio perfil y `CBRS_ACCOUNT_N_PROXY_URL`.
2. 2Captcha resuelve reCAPTCHA v3 Enterprise mediante
   `RecaptchaV3TaskProxyless` cuando se habilita el fallback.

2Captcha no permite enviar proxy en tareas reCAPTCHA v3/Enterprise v3. Por eso
el token del solver no sale por la IP del navegador. No se debe copiar el proxy
del navegador dentro del payload de `createTask`.

Para identidad cuenta-IP estable durante días se usan tres endpoints Proxy-Cheap
Chile static residential IPv4, no proxies suministrados por 2Captcha.

## Configuración recomendada para la prueba

Mantener los secretos únicamente en `C:\ProgramData\CBRS\cbrs.env`:

```dotenv
CBRS_CAPTCHA_SOLVER_MODE=2captcha_manual
CBRS_2CAPTCHA_API_KEY=REEMPLAZAR_EN_EL_HOST
CBRS_2CAPTCHA_MIN_SCORE=0.9
CBRS_2CAPTCHA_TIMEOUT_SECONDS=120
CBRS_2CAPTCHA_POLL_SECONDS=5
CBRS_2CAPTCHA_DAILY_LIMIT=10
CBRS_2CAPTCHA_CIRCUIT_BREAKER_SECONDS=900
CBRS_2CAPTCHA_REJECTION_COOLDOWN_SECONDS=21600
CBRS_PROXY_RECHECK_SECONDS=300

CBRS_EJECUTIVO_1_PROXY_URL=http://LOGIN_1:PASSWORD@STATIC_HOST_1:PORT
CBRS_EJECUTIVO_2_PROXY_URL=http://LOGIN_2:PASSWORD@STATIC_HOST_2:PORT
CBRS_EJECUTIVO_3_PROXY_URL=http://LOGIN_3:PASSWORD@STATIC_HOST_3:PORT
```

Usar tres valores completos y estáticos distintos generados por Proxy-Cheap. El
instalador acepta URLs HTTP(S); no se deben construir credenciales a mano.

El modo requerido es `2captcha_manual`: usa primero el token del navegador y
pausa la cuenta ante `captcha_rejected`. No crea una tarea pagada hasta que el
operador autoriza exactamente un solve para esa cuenta desde CLI o dashboard.

## Gates antes del long-run

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

Ejecutar después una sola solicitud controlada. Confirmar en los eventos que no
hay `captcha_solver_failed`, `proxy_health_failed`, `WAF` ni cambio de egreso.
Solo entonces iniciar la prueba larga.

## Comportamiento de seguridad

- Un rechazo del token browser nunca llama automáticamente al solver.
- La autorización manual permite exactamente un solve, no se acumula y vence a
  los 15 minutos si no se consume.
- Un segundo rechazo deja solo esa cuenta en `captcha_pending`.
- Una autorización manual usa el solve pagado como primer intento; no envía antes
  otro token browser que ya se sabe rechazado.
- Un token pagado rechazado por CBRS bloquea nuevos solves de esa cuenta durante
  seis horas y deriva la recuperación a la ruta visual.
- Error de clave o saldo deshabilita el fallback externo hasta que
  `captcha-health` vuelva a confirmar autenticación y saldo positivo.
- Error de red, timeout o capacidad abre un circuito global de 15 minutos. Las
  cuentas sanas pueden continuar con tokens del navegador; no se encadenan
  intentos pagados entre cuentas.
- El egreso se vuelve a comprobar cada cinco minutos por defecto. Un cambio
  contra el baseline pausa la cuenta; el sistema no aprueba una IP nueva solo.
- Claves, tokens, IPs y URLs de proxy se redactan y nunca deben entrar en Git.

2Captcha publica restricciones para sitios gubernamentales, financieros o de
alto riesgo. El gate vivo del proxy contra CBRS es obligatorio: que el proxy
tenga salida chilena no prueba que el proveedor permita alcanzar el portal.
