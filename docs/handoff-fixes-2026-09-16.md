# Respuesta técnica al informe de pruebas del 16 de septiembre

Fuente: `origin/handoff-review-fixes:INFORME-PRUEBAS-2026-09-16.md`, base
`8692cbf`. El login Firefox con la misma cuenta y salida es evidencia útil de
una diferencia de cliente. Sin los motivos de evaluación de reCAPTCHA del
servidor no demuestra qué flag ni qué señal causó el rechazo.

## Cambios implementados

| Defecto | Cambio / límite |
| --- | --- |
| D1–D2 | Inicialización de primera línea base móvil tras validar país/proxy; no sustituye líneas existentes. Comando de pool documentado. |
| D3 | Estado público incluye pausa/captcha, motivo y plazo. Un rechazo de credenciales queda `disabled` en el estado público y excluido del pool; no se confunde con CAPTCHA. |
| D4 | Gates fallidos sin navegador se reevalúan en el ciclo idle después del cooldown, sin requerir trabajo en cola. |
| D5 | Código exacto `captcha-rechazado` se clasifica antes de HTTP 400/401/422. |
| D6 | Rechazo CAPTCHA de login permite recuperación con candidatos sólo con cuenta autorizada, formulario rechazado visible y sin sesión protegida. Se mantienen límites y adopción del candidato exacto. |
| D7 | `auth-exception` 401 sigue siendo rechazo terminal de autenticación. No se infiere baja administrativa de un código genérico ni se guarda el cuerpo del portal. |
| D8 | El rechazo explícito del formulario puede invocar el solver ya configurado mediante el flujo browser-fetch existente, conservando presupuestos y comprobación posterior de sesión. |
| D9 | Pendiente: no se ha certificado Ctrl+C <30 s ni salida sin procesos supervivientes en Ubuntu. No se introduce cierre forzado de sesiones protegidas. |
| D10 | Lock de proceso independiente del heartbeat. Recuperación inmediata de lease/run sólo con PID local probado ausente y sin Chrome registrado, o con propietario externo activo. Chrome huérfano de un worker embedded requiere handoff explícito. |
| D11 | Defaults ya eran 10 candidatos / 30 por hora; `.env` anterior prevalece. Se documenta cómo reconocer y ajustar esos overrides. |
| D12 | Ayuda de servicio incluye comando con systemd y worker en primer plano. |
| D13 | Eventos operacionales a stderr y archivo rotatorio; sin cuerpos de respuestas ni cargas completas de eventos. |
| D14 | Chrome restaura servicios de red/actualización y almacenamiento de credenciales del SO al omitir cinco defaults de Playwright. Mantiene sandbox, transporte y declaración de automatización. Aceptación de login aún requiere prueba real. |

No se afirma cumplimiento de A0 ni que CAPTCHA vaya a aceptar todos los logins.
Tampoco se introducen huellas falsas, Firefox automático, eliminación de
perfiles, cambios en proxies productivos ni reinicios del propietario.

## Validación reproducible

```bash
python -m pytest -q
CBRS_RUN_CHROME_SMOKE=1 python -m pytest tests/test_chrome_launch_smoke.py -q
```

El smoke usa un perfil temporal y una página local. Demuestra arranque y
operación de Chrome, no autenticación CBRS. Los tests de regresión usan
respuestas simuladas y bases SQLite aisladas.

Verificación local del 16 de septiembre: 575 tests pasaron y 3 se omitieron;
el smoke de Chrome se ejecutó por separado y pasó. La comparación de secretos
configurados contra 223 archivos de fuente encontró cero coincidencias.
La suite local incluye cambios preexistentes del monitor de aceptación que
se conservaron fuera de este commit. CI verifica la fuente publicada en
Windows y Ubuntu 24.04; Ubuntu también ejecuta el smoke local de Chrome.

Antes de aceptar A0/A2 en un host de prueba independiente: instalar el commit,
usar cuentas autorizadas para ese host, validar configuración y observar el
estado y logs durante el primer login. Después obtener un PDF real y comprobar
su inscripción y caché. Registrar versión, tiempos, resultado por cuenta y
códigos sanitizados, sin incluir contraseñas/tokens. No repetir búsquedas con
recibo aceptado. Si persiste `captcha-rechazado`, conservar la evidencia y
resolver acceso con el operador del portal; más reintentos no garantizan éxito.

Los cambios de browser-owner/arranque requieren una activación coordinada.
Un push de código no actualiza los procesos que ya están ejecutándose.
