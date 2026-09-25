# Respuesta técnica al informe de pruebas del 22 al 24 de septiembre

Fuente: `INFORME-PRUEBAS-2026-09-22.md` (rama `handoff-review-fixes`, fusionada
en `master` sin conflictos) y el paquete `evidencia-cuentas-cbrs-2026-09-24.zip`.
Continúa [handoff-fixes-2026-09-17.md](handoff-fixes-2026-09-17.md). Todo cambio
de código tiene prueba de regresión sin tráfico al portal.

## Defectos del informe y su corrección

| Defecto | Causa encontrada | Cambio |
| --- | --- | --- |
| D28 | `admit_quota_check` adelanta una hora `next_check_at` al admitir la prueba de cuota vencida, y sólo la devolvía ante `AUTH_REQUIRED`. Además, un diálogo de error visible antes de enviar y el refresco de la prueba quedaban fuera del bloque que la devolvía. | La admisión se devuelve siempre que la prueba termina sin respuesta del portal sobre la cuota: `captcha_rejected`, `captcha_solver_failed`, `search_not_submitted`, `auth_required` y `temporary_unavailable` (incluye el diálogo de error antes o después de enviar). Sólo un `daily_limit` o una búsqueda respondida consumen la comprobación. Lo mismo vale para búsquedas por nombre. La devolución nunca pisa una comprobación posterior concurrente. |
| D27 | `REPORT_FIELDS` no incluía `resume_at`. | El CSV de `get-batch` tiene la columna `resume_at` (vacía salvo en `pending_quota`). Un CSV de entrada con una columna llamada `resume_at` se rechaza como encabezado reservado, igual que `status`. |
| D29 | `DEFAULT_HEADLESS = True`. | Headful por defecto. `.env.example` y el README indican Xvfb (`deploy/cbrs-display.service`, `DISPLAY=:99`) como la forma soportada de correr sin ventanas en Linux. |
| D30 | Con headless, cada búsqueda recibe `captcha-rechazado` y el diálogo de error, que el servicio toma como ruta comprometida. | Se eligió la segunda opción del informe: `cbrs jobs worker` y el propietario externo se niegan a arrancar en headless (`CBRS_HEADLESS=1` o `--headless`), con un mensaje que explica el motivo y cómo usar Xvfb, antes de tocar la base, un navegador o una ruta. Así ningún rechazo headless puede borrar perfiles. La política de ruta comprometida en headful no cambia; ver «Confirmar la ruta antes de borrar un perfil» más abajo. |
| D31 | En la API síncrona de Playwright, el bucle de espera es `while not task.done(): dispatcher_fiber.switch()`. Un `KeyboardInterrupt` que cae dentro del greenlet despachador (lo normal si hay una llamada a Playwright en curso) lo mata; toda llamada posterior, incluido el cierre, cambia a un greenlet muerto para siempre: 100 % de CPU. Por eso SIGINT con el worker inactivo salía en 1 s y durante un login de candidato colgaba. Además, el driver de Playwright responde por defecto a Ctrl+C cerrando todos los Chrome y saliendo. | El manejador de SIGINT y SIGTERM del worker lanza el `KeyboardInterrupt` en el greenlet que espera, nunca dentro del despachador, que sigue vivo y atiende el cierre ordenado. Chrome se lanza con `handle_sigint=False`, así Ctrl+C sigue exactamente el mismo camino que SIGTERM. Un SIGINT ignorado al lanzar (nohup, segundo plano) sigue ignorado. Reproducido con Chrome real en Linux: sin el cambio, Ctrl+C durante una llamada deja el proceso al 97 % de CPU; con el cambio, cierre limpio en 0,3 s y salida en 0,5 s. |
| Punto 6 | La recuperación de rutas no tenía tope diario de logins por cuenta. | `CBRS_DATAIMPULSE_MAX_CANDIDATE_LOGINS_PER_DAY` (por defecto 10, una pasada completa de recuperación): candidatos que llegaron al login del portal en las últimas 24 h, por cuenta. Al alcanzarlo, la cuenta queda `retry_allowance_exhausted` hasta 24 h después del login más antiguo, con el evento `dataimpulse_rotation_skipped` (`daily_login_limit`, `candidate_logins_24h`). Los candidatos que nunca llegaron al formulario (sin conectividad, salida repetida, fallo de arranque) no cuentan. Para una sola cuenta activa conviene 3 a 5. |

También se corrigió una advertencia `SyntaxWarning: "\d"` en `cbrs/form_search.py`
(la expresión de «Recientes» dentro de JavaScript), que Python convertirá en error.

### Confirmar la ruta antes de borrar un perfil (D30, segunda parte)

No se cambió. El borrado del perfil ante el diálogo exacto es una decisión del
operador (2026-09-13, `docs/portal-error-dialog.md`), no del código. Con headless
bloqueado y el tope diario, el costo máximo por cuenta queda acotado: un perfil
borrado exige un login nuevo, y ese login cuenta contra el tope. Si el mandante
prefiere confirmar con un segundo candidato antes de borrar, es un cambio de
política que hay que pedir explícitamente.

## Headless con nuestras cuentas

Todavía no lo probamos después del informe. Nuestra producción corre headful
sobre Xvfb (`CBRS_HEADLESS=0`, `DISPLAY=:99`) desde al menos el 07-09. Una copia
de la base del 02-09 (instalación Windows, anterior a la migración) muestra
`browser_headless=1` en nuestras tres cuentas, pero en ese período ninguna
autenticaba de forma estable, así que ninguna búsqueda headless con sesión
aceptada quedó registrada. La prueba A/B de la sección 6.2 es concluyente sobre
el rechazo del reCAPTCHA; lo que no sabemos es si una sesión headless puede
provocar una baja (ver abajo).

## Caso `ejecutivo_3` (y `ejecutivo_1`)

**Nuestras cuentas son otras.** En nuestra producción, `ejecutivo_1..3` son
etiquetas de cuentas de desarrollo propias, con otros usuarios del portal. No
hubo sesiones paralelas ni uso cruzado: nada de lo que hizo nuestra producción
tocó sus cuentas. Lo que podemos aportar es la lectura de su paquete de
evidencia y una comparación útil: nuestras cuentas corren el mismo código, con
un uso bastante más intenso, y ninguna fue dada de baja.

### Lo que muestra el paquete de evidencia

- **Alguien más usa sus dos cuentas.** En las capturas del 23-09 a las 01:52
  (UTC), el panel «Recientes» de `ejecutivo_3` lista siete búsquedas del lote y
  tres que no están en su base: `70523/41124/2015`, `51338/35453/2008` y
  `59492/22933/2025`. El de `ejecutivo_2` lista cuatro ajenas:
  `23859/12079/2020`, `33458/12847/2026`, `46703/18149/2025` y
  `11059/8990/1998`. Como nuestras cuentas son otras, no las hizo nuestra
  producción: son de una persona en el navegador, de otra instalación o de las
  pruebas del 16-17 en otra máquina. Las dos cuentas comparten esto, así que
  por sí solo no explica la baja de `ejecutivo_3`.
- **El 24-09, `ejecutivo_3`** (hora UTC): 14:07 primera sesión headless de la
  cuenta, aceptada por el servidor; 14:08 búsqueda con HTTP 400. 19:07 sesión
  headful aceptada, CAPTCHA de búsqueda rechazado, diálogo de error y perfil
  borrado. 22:35 candidato 10491 rechazado en el login; 22:36 candidato 14435:
  HTTP 401 `auth-exception`. `ejecutivo_2` pasó por la misma secuencia headless
  y headful ese día, con más candidatos (13 contra 6), y sigue activa.
- El paquete no incluye nada que distinga a `ejecutivo_3` de `ejecutivo_2` en
  volumen, IP o modo del navegador.

### Comparación: nuestras cuentas, mismo código, sin bajas

| Patrón en nuestra producción | Detalle (UTC) |
| --- | --- |
| Ráfaga de logins desde muchas IP | 14-09 de 01:52 a 04:19: 89 logins de candidatos en las tres cuentas desde ≈88 IP distintas; una sola cuenta hizo 52, todos rechazados con HTTP 400. |
| Muchos logins en un día | 08-09: una cuenta hizo 25 logins, 19 fallidos, uno cada ≈3 min. |
| Bucle de peticiones 24/7 | 14-09 a 21-09: una cuenta pidió 1.889 veces el mismo documento (HTTP 404), ≈26 por hora día y noche, desde 11-15 IP por día (el bucle corregido el 20-09, `e47a2ab`). |
| IP cambiante | Desde el 15-09, cada arranque diario reutiliza la sesión desde una IP móvil nueva. |
| Headless | Hacia el 02-09 las tres corrieron headless, sin sesión estable. |
| Resultado | Ninguna baja. Ningún HTTP 401 `auth-exception` registrado en toda la historia; las tres autenticadas hoy. |

Con esos niveles, ni el número de logins, ni la cantidad de IP, ni un bucle de
peticiones, ni el cambio diario de IP bastaron para una baja. Lo que sus
cuentas tienen y las nuestras no:

1. **Búsquedas headless con una sesión autenticada** (24-09 a las 14:07). No lo
   hemos reproducido nunca. Es la hipótesis que más encaja en el tiempo, aunque
   `ejecutivo_2` hizo lo mismo y sigue activa por ahora.
2. **Uso compartido** por alguien más (los paneles «Recientes»).
3. **Otro tipo de cuenta.** Las suyas son del mandante; las nuestras, de
   desarrollo. El portal puede revisarlas con criterios distintos.

Sólo el portal o el mandante pueden dar la causa: **¿el portal informa la fecha
y el motivo de la baja, o se puede consultar?** ¿Quién más usa estas cuentas?

Como nuestras cuentas son de desarrollo, podemos probar la hipótesis 1 con una
de ellas: sesión autenticada y búsquedas headless, sin rotación, y observar si
la cuenta cae. Lo proponemos como siguiente paso si el mandante lo considera
útil.

### Recomendaciones para la cuenta que queda

1. Nunca headless (ahora bloqueado en el worker y el propietario).
2. `CBRS_DATAIMPULSE_MAX_CANDIDATE_LOGINS_PER_DAY=3` a `5`.
3. Que nadie más la use en paralelo mientras corre el servicio.
4. Si hace falta analizar patrones de peticiones, agregar un registro por
   petición (método, ruta sin parámetros, estado, hora, cuenta; sin cuerpos ni
   tokens). No existe hoy en ninguna de las dos instalaciones.

## Pendiente de activación en la producción

Los cambios están en `master` y probados, pero **no están cargados en la
producción WSL**: el servicio carga código sólo al arrancar y activarlo
requiere reiniciar el propietario y el worker, lo que necesita autorización
explícita. Ninguno cambia la base de datos.

## Validación reproducible

```bash
python -m pytest -q
```

Pruebas nuevas: `tests/test_portal_quota.py`
(`test_probe_without_quota_answer_keeps_its_admission`),
`tests/test_public_api.py` (`resume_at` en el CSV), `tests/test_cli_args.py`
(`test_worker_refuses_headless_before_touching_state`),
`tests/test_informe_2026_09_17.py`
(`test_interrupt_inside_a_greenlet_is_raised_in_its_caller`),
`tests/test_background_auth_recovery.py`
(`test_daily_candidate_login_cap_counts_only_portal_logins`) y la aserción de
`handle_sigint` en `tests/test_browser_session.py`.
