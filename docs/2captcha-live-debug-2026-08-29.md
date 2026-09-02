# Auditoría viva de 2Captcha — 2026-08-29

Este archivo conserva únicamente evidencia saneada. No contiene claves, tokens,
credenciales, URLs de proxy, IDs de tarea del proveedor ni IPs.

## Contrato confirmado

La página CBRS carga `recaptcha/enterprise.js` y ejecuta reCAPTCHA Enterprise v3.
El bundle público vigente confirmó:

- sitekey igual al configurado localmente;
- acción de login: `login`;
- acción de búsqueda de Comercio: `indice_com_texto`;
- token enviado como header `recaptcha-token` y, para la búsqueda, también en
  el campo JSON `recaptchaToken`.

La integración 2Captcha debe usar `RecaptchaV3TaskProxyless` con
`isEnterprise=true`, `websiteURL`, `websiteKey`, `minScore` y el `pageAction`
exacto. La respuesta válida está en `solution.gRecaptchaResponse`. Para v3 no
se debe agregar el proxy del navegador a la tarea. Fuentes oficiales:

- [reCAPTCHA v3 / Enterprise](https://2captcha.com/api-docs/recaptcha-v3)
- [getTaskResult](https://2captcha.com/api-docs/get-task-result)
- [límites de solicitudes](https://2captcha.com/api-docs/limits)
- [códigos de error](https://2captcha.com/api-docs/error-codes)

Los parámetros actuales coinciden con ese contrato. Por tanto, “token resuelto”
solo demuestra que 2Captcha devolvió un token; la aceptación debe probarse por
separado contra CBRS y registrarse como `accepted`.

Google documenta que el backend puede invalidar el token por expiración,
reutilización, action inesperada, clave o dominio distintos, y que la evaluación
puede incorporar el user-agent y la IP del usuario. CBRS no expone cuál de esas
propiedades causó su respuesta genérica:

- [propiedades e invalidación del token](https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments)
- [creación de assessments web](https://cloud.google.com/recaptcha/docs/create-assessment-website)

## Pausas reproducidas

1. `egress_preflight_failed`: dos consultas consecutivas de egreso por el proxy
   terminaron con `SSL UNEXPECTED_EOF`; una comprobación posterior obtuvo Chile.
   Es un fallo transitorio de transporte, no un cambio de país demostrado.
2. `temporary_unavailable`: CBRS devolvió su respuesta temporal durante la
   búsqueda protegida. No fue un error del API de 2Captcha.
3. `credentials_invalid`: el runtime recibió una respuesta HTTP de rechazo en
   login, pero la telemetría anterior no preservó el código saneado del portal.
   Se debe distinguir este caso de un rechazo CAPTCHA antes de concluir que la
   contraseña es incorrecta.

## Intentos controlados

Con preflight Chile aprobado y sesión autenticada, se forzó 2Captcha en los tres
perfiles aislados. Hubo cuatro tareas pagadas `succeeded` (US$0.00299 cada una):
búsqueda con scores 0.9 y 0.3, y login con score 0.9. CBRS respondió en todos los
casos HTTP 400 con código saneado `intente-mas-tarde`; no se generaron PDFs.

La repetición entre tres cuentas, tres proxies y dos acciones descarta saldo,
resolución del proveedor, autenticación, score 0.9 y formato de envío como causas
inmediatas. No hubo un token 2Captcha aceptado por CBRS. La evidencia no permite
inventar uno ni atribuir a CBRS un `invalidReason` que su API pública no devuelve.

## Causa corregida en el runtime

`intente-mas-tarde` es una condición genérica de refrescar y reintentar, no una
prueba de que el token reCAPTCHA sea inválido. Clasificarla como
`captcha_rejected` disparaba un solve pagado que no modificaba la condición del
portal. Ahora se clasifica como `temporary_unavailable`, aplica un cooldown de
120 segundos y no consume 2Captcha. Solo marcadores explícitos de CAPTCHA pueden
activar el fallback automático.

El límite máximo también se aplica como invariante: pausas de cuenta, circuitos
globales, rechazos pagados y cooldown configurable de endurance no pueden exceder
300 segundos. Los tres baselines rotados fueron validados y reemplazados mediante
el gate protegido, con archivos anteriores archivados de forma saneada.

## Recuperación estable posterior

Las tres rutas 2Captcha Residential siguieron recibiendo `intente-mas-tarde`
incluso después de renovar sus sesiones, baselines y usar Chrome headed. Una
prueba A/B con el mismo perfil, credenciales, token del navegador y consulta
confirmó que el proxy estático protegido anterior sí era aceptado. Se restauró
ese paquete local sin comprar tráfico adicional y los tres egresos pasaron país
Chile, unicidad, CBRS, reCAPTCHA y baseline.

La segunda A/B aisló el runtime del worker: Chrome headed off-screen completó la
búsqueda mientras el worker programado con `--headless` recibió el rechazo
temporal. Ese resultado histórico motivó pruebas adicionales; el worker nativo
actual vuelve a usar `--headless` y cada
cuenta conserva su perfil y proxy aislados.

Para evitar replay idéntico, el plan endurance rota seis FNA confirmadas. Todas
mantienen `sample_pages: 3`, una sola descarga secuencial y cooldown de cinco
minutos.

## Política corregida

`daily_limit` sigue siendo el único estado que espera al siguiente día. Ningún
cooldown operativo excede 300 segundos:

- egress/proxy: 60 s;
- CAPTCHA rechazado, indisponibilidad y respuesta inesperada: 120 s;
- auth, solver, rate-limit y WAF: 300 s;
- circuito y rechazo pagado de 2Captcha: 300 s;
- separación entre jobs endurance: 300 s.

Los cooldowns siguen evitando reintentos inmediatos. Rate-limit y WAF no se
ignoran; se vuelven a evaluar al terminar sus cinco minutos.

## Criterio de cierre

Una aceptación real continúa pendiente hasta que una fila nueva de “Intentos
2Captcha” muestre:

1. tarea pagada `succeeded`;
2. resultado CBRS `accepted`;
3. búsqueda controlada terminada correctamente;
4. ningún secreto en SQLite, logs, dashboard o Markdown.

## Verificación controlada final

Se ejecutó el mismo cliente API contra la demo oficial de reCAPTCHA v3
Enterprise de 2Captcha, usando `RecaptchaV3TaskProxyless`, score 0.9, acción
`demo_action` y el callback real de la página. El verificador servidor respondió
HTTP 200 y mostró `Captcha is passed successfully`. Esto confirma extremo a
extremo que el cliente crea la tarea correcta, extrae el token correcto y lo
entrega correctamente a un verificador Enterprise v3.

En CBRS se repitió la prueba mediante su propia interfaz Vue/Axios, sin el fetch
personalizado del scraper. Se cubrieron scores 0.3, 0.7 y 0.9, las acciones
`login` e `indice_com_texto`, y más de una cuenta. Todos los tokens 2Captcha
recibieron `intente-mas-tarde`, mientras el token generado por Google en ese
mismo navegador, cuenta, proxy, formulario y FNA recibió HTTP 200 de inmediato.
La única variable restante fue el contexto de generación del token.

La documentación vigente de 2Captcha especifica que v3 Enterprise solo admite
`RecaptchaV3TaskProxyless`: no acepta proxy ni user-agent del navegador. CBRS
está aplicando una evaluación de riesgo que no acepta esos tokens proxyless. No
hay otro parámetro soportado que permita unir el token al perfil/IP aislado del
navegador. Por ello el runtime conserva el token nativo de Google como camino
operativo y no debe gastar repetidamente saldo ante `intente-mas-tarde`.

También se añadió el feedback API recomendado por 2Captcha: un resultado
explícitamente aceptado envía `reportCorrect`; un rechazo CAPTCHA explícito
envía `reportIncorrect`; resultados indeterminados o temporales no se reportan
como error del proveedor. El task ID sigue siendo solo memoria transitoria.

## Barrido completo de productos 2Captcha

Se amplió la investigación más allá de `RecaptchaV3TaskProxyless`. Todas las
pruebas usaron credenciales ficticias para que una respuesta de credenciales
inválidas demostrara que CBRS había aceptado primero el CAPTCHA. Nunca se
persistieron tokens, URLs CDP, claves, credenciales de proxy ni IPs.

| Ruta | Contexto de resolución | Resultado 2Captcha | Resultado CBRS |
|---|---|---|---|
| API v3 Enterprise, scores 0.3/0.7/0.9 | proxyless | token listo | HTTP 400 `intente-mas-tarde` |
| Browser API auto-solve | navegador 2Captcha + proxy chileno de la cuenta | `solveFinished` | HTTP 400 `intente-mas-tarde` |
| Browser API `Captcha.solve` manual | token creado y enviado desde el mismo navegador/proxy | `solveFinished` | HTTP 400 `intente-mas-tarde` |
| Browser API `clickcaptcha` | modo de clic documentado | no produjo una solución aplicable a v3 invisible | sin aceptación |
| Extensión oficial Chrome 3.3.3 | interceptor Enterprise v3 dentro del formulario Vue real | estado `solved` | HTTP 400 `intente-mas-tarde` |
| `RecaptchaV2EnterpriseTask` con proxy | tarea Enterprise v2 más costosa, proxy chileno y mismo user-agent | `ready` | HTTP 400 `intente-mas-tarde` |

La prueba Enterprise v2 fue deliberadamente excepcional: CBRS usa v3 y ambos
protocolos no son intercambiables, pero permitió descartar directamente la idea
de que el tipo de tarea con proxy y mayor costo fuese aceptado por casualidad.

La Browser API sí estaba habilitada y con tráfico disponible. Su extensión CDP
detectó y resolvió el desafío; aun así, CBRS rechazó tanto la inyección automática
como el token manual enviado en el mismo navegador remoto. La extensión oficial
local también interceptó la llamada dinámica `grecaptcha.enterprise.execute`,
esperó la solución y dejó avanzar el submit nativo antes del mismo rechazo.

Fuentes oficiales usadas para este barrido:

- [Browser API y dominio CDP `Captcha`](https://2captcha.com/scraper/browser-api/api)
- [Extensión oficial de navegador](https://2captcha.com/captcha-bypass-extension)
- [Enterprise v2 con tarea proxy](https://2captcha.com/api-docs/recaptcha-v2-enterprise)
- [Restricción de proxy para v3/Enterprise v3](https://2captcha.com/api-docs/proxy)

### Conclusión técnica vigente

No es posible demostrar una imposibilidad absoluta para siempre. Sí queda
demostrado que **ninguna ruta publicada y disponible hoy en 2Captcha produce un
CAPTCHA que CBRS acepte**. El control positivo continúa siendo el token nativo de
Google generado por Chrome en el perfil y proxy de la cuenta, que CBRS aceptó en
el mismo flujo donde rechazó las respuestas de 2Captcha.

Por tanto, repetir tareas 2Captcha no aumenta la probabilidad de completar CBRS:
solo consume saldo. El camino operativo debe conservar el token nativo de Google
y dejar la integración 2Captcha desarmada hasta que 2Captcha publique un tipo v3
Enterprise ligado al proxy/user-agent, o cambie la política de evaluación de
riesgo de CBRS.
