# Opciones de proxy 2Captcha para CBRS

Fecha de decisión: 2026-08-29.

## Decisión

La validación finita usará tres sesiones **2Captcha Residential Sticky de
Chile**, configuradas a 120 minutos, una por cuenta. Cada sesión queda fijada a
la misma cuenta y perfil de Chrome durante su TTL. Al vencer, el nuevo egreso
solo se adopta automáticamente para este proveedor después de validar tráfico,
Chile, CBRS, reCAPTCHA y unicidad, archivando el baseline saneado anterior.

Se compró 1 GB de tráfico residential por US$5 con confirmación del operador.
El generador debe ofrecer Chile antes de iniciar una sesión.

## Alternativas evaluadas

| Producto | Precio publicado | Persistencia | Ajuste a CBRS |
| --- | ---: | --- | --- |
| [Residential proxies](https://2captcha.com/proxy/residential-proxies) | Desde US$5/GB | Sesión sticky de 0 a 120 minutos; al vencer rota y no se puede extender | Seleccionado para login y descarga finitos dentro del TTL |
| [Dedicated ISP proxies](https://2captcha.com/proxy/isp-proxies) | Desde US$5/GB | Dirección residencial ISP dedicada y de larga duración | Alternativa futura para continuidad superior a 120 minutos |

El precio publicado no garantiza inventario chileno. Residential permite tres
sesiones concurrentes con el mismo tráfico comprado, pero añade relogins, nuevos
baselines y pausas operativas cada vez que vence el TTL.

## Restricciones y riesgo de blacklist

- Una IP residencial puede tener reputación previa o ser bloqueada.
  La etiqueta ISP/residential no garantiza acceso a CBRS.
- El inventario de país mostrado por el API o generador es orientativo: la
  disponibilidad final del endpoint puede diferir.
- 2Captcha advierte que ciertos destinos gubernamentales, financieros o de alto
  riesgo pueden estar restringidos por política. Por eso el acceso vivo a CBRS
  es un gate obligatorio.
- Un egreso chileno no basta: también deben cargar CBRS y el script de reCAPTCHA
  Enterprise, y la salida no puede coincidir con otra cuenta.
- Un cambio inesperado de IP, falta de tráfico, cuenta proxy inactiva, 403, 429,
  WAF o ruta compartida activa un cooldown. La IP siguiente nunca se adopta si
  cualquiera de los gates falla.

## Integración

`proxy_provider` es opcional y por compatibilidad vale `generic_static`. Las
cuentas migradas declaran:

```json
{
  "proxy_provider": "2captcha_residential_sticky",
  "proxy_url_env": "CBRS_EJECUTIVO_1_PROXY_URL"
}
```

Las URLs completas emitidas por el proveedor se aceptan como HTTP(S), sin
suponer hostname, puerto ni formato de usuario. Permanecen exclusivamente en
`C:\ProgramData\CBRS\cbrs.env`; JSON, SQLite, dashboard, reportes y Git sólo
guardan referencias o estados saneados.

El dashboard y `/api/status` exponen únicamente proveedor, estado activo,
tráfico disponible, estado del baseline, validación Chile y salud del proxy. No
exponen IP, hash de egreso, URL, usuario, contraseña ni API key.

La comprobación de proveedor usa el endpoint de cuenta de proxy 2Captcha en
modo lectura para validar estado y tráfico. La inspección viva del egreso sigue
siendo la autoridad para país, alcance y unicidad.

## Migración segura por cuenta

1. Mantener endurance pausado y detener el worker. Confirmar que no existe lease.
2. Conservar las tres URLs actuales en el archivo de secretos protegido. No
   copiarlas a documentación ni a un paquete sin ACL restringida.
3. Cambiar una sola referencia de proxy y marcar su proveedor como
   `2captcha_residential_sticky`.
4. Validar y reemplazar el baseline:

   ```powershell
   .\.venv\Scripts\python.exe deploy\run_with_env.py C:\ProgramData\CBRS\cbrs.env -- `
     .\.venv\Scripts\python.exe -m cbrs pool proxy-health `
     --account ejecutivo_1 --replace-egress-baseline
   ```

5. El comando exige proveedor activo con tráfico, país `CL`, CBRS y reCAPTCHA
   accesibles, egreso distinto de las otras cuentas, endurance pausado y cero
   leases. Archiva el baseline anterior saneado e instala el nuevo de forma
   atómica.
6. Completar login normal y una descarga controlada antes de continuar con la
   siguiente cuenta.
7. Conservar el rollback local hasta que las tres cuentas aprueben.

## CAPTCHA

El solver Enterprise v3 continúa como `RecaptchaV3TaskProxyless`. Según la
[documentación de proxies para tareas CAPTCHA](https://2captcha.com/api-docs/proxy),
el proxy del navegador no se introduce en el payload de la tarea v3/Enterprise
v3. Son dos rutas independientes.

## Aceptación finita

Antes de reanudar trabajo normal:

- tres hashes de egreso distintos y chilenos, uno por cuenta;
- cada baseline permanece igual tras reiniciar Chrome dentro de la ventana de
  120 minutos;
- login automático y una descarga de documento funcionan en cada cuenta;
- no aparecen 403, 429, WAF, egreso compartido, error de salud ni cambio de IP.

Esta aceptación no demuestra permanencia superior a 120 minutos. El worker
puede continuar después del vencimiento únicamente mediante la renovación
validada y auditada del baseline descrita arriba.

La prueba indefinida queda pausada hasta una solicitud separada.
