# Tráfico de DataImpulse de `ejecutivo_3` y `ejecutivo_2`, 23 y 24 de septiembre

Complementa la sección 8 de [INFORME-PRUEBAS-2026-09-22.md](INFORME-PRUEBAS-2026-09-22.md).
El portal dio de baja `ejecutivo_3` el 24-09 (HTTP 401 `auth-exception` en el
primer login nuevo, a las 22:37 UTC). La instalación registraba eventos, no
peticiones, así que este informe usa las estadísticas por sesión de DataImpulse.

## Método

- API de estadísticas de DataImpulse (`gw.dataimpulse.com:777`), sólo `GET` a
  `/api/usage`, `/api/errors_stats_with_parameters` y
  `/api/stats_with_history`, con las credenciales del proxy. Fueron 26 llamadas
(dos respondieron 400 por una agrupación no soportada),
  sin `rotate_ip` ni `list`, sin tocar el portal, sin iniciar el worker y sin
  hacer logins.
- **Todas las horas están en UTC** (hora de Chile = UTC−3). Se verificó con la
  serie por hora: los picos coinciden con las corridas locales desplazadas 3 horas.
- **`requests` son conexiones del proxy, no peticiones HTTP.** El proxy ve la
  apertura de cada túnel hacia `host:puerto`, no las peticiones HTTPS que viajan
  dentro. Por eso el portal suma sólo 49 conexiones el 23-09, aunque ese día
  hubo más de 20 búsquedas.
- La API no combina agrupaciones: `groupby` acepta `session` o `domain`, no los
  dos, ni por hora. La serie por puerto se armó consultando cada hora activa por
  separado.
- Los puertos de cada cuenta salen de `pool.sqlite3` (`account_proxy_routes`,
  `proxy_candidate_attempts`, eventos `dataimpulse_route_rotated`), abierto sólo
  en lectura. Las huellas de salida (SHA-256 de la IP, 12 caracteres) salen de
  los `preflight-*.json` locales; la API no devolvió IPs.
- Las respuestas crudas quedan en `.cbrs/evidencia-dataimpulse/` de la máquina
  de pruebas, fuera de git.

## 1. Conexiones y bytes por puerto

Todos los puertos que DataImpulse registró el 23 y el 24 corresponden a una de
las dos cuentas. `ejecutivo_2` incluye dos pruebas manuales del 24 (sección 6.2
del informe de pruebas).

### `ejecutivo_3`

| Puerto | Uso | Conex. 23-09 | MB 23-09 | Conex. 24-09 | MB 24-09 | Conex. total | MB total |
|---|---|---|---|---|---|---|---|
| 15215 | ruta inicial | 63 | 14.09 | 0 | 0.00 | 63 | 14.09 |
| 18761 | candidato, CAPTCHA | 18 | 6.16 | 0 | 0.00 | 18 | 6.16 |
| 17959 | ruta promovida | 27 | 8.85 | 0 | 0.00 | 27 | 8.85 |
| 17844 | candidato, `auth_required` | 21 | 3.56 | 0 | 0.00 | 21 | 3.56 |
| 18325 | ruta hasta la baja | 726 | 35.79 | 492 | 24.87 | 1218 | 60.66 |
| 13703 | candidato, sólo verificación de salida (worker detenido) | 0 | 0.00 | 3 | 0.02 | 3 | 0.02 |
| 10491 | candidato, CAPTCHA | 0 | 0.00 | 19 | 6.16 | 19 | 6.16 |
| 14435 | candidato, **401** | 0 | 0.00 | 18 | 6.15 | 18 | 6.15 |
| **Total** | | **855** | **68.44** | **532** | **37.19** | **1387** | **105.64** |

### `ejecutivo_2` (sigue activa)

| Puerto | Uso | Conex. 23-09 | MB 23-09 | Conex. 24-09 | MB 24-09 | Conex. total | MB total |
|---|---|---|---|---|---|---|---|
| 10001 | ruta inicial | 35 | 5.88 | 0 | 0.00 | 35 | 5.88 |
| 19314 | ruta promovida | 56 | 11.12 | 0 | 0.00 | 56 | 11.12 |
| 18298 | candidato, conectividad | 4 | 0.03 | 0 | 0.00 | 4 | 0.03 |
| 17340 | ruta promovida | 743 | 37.00 | 66 | 4.30 | 809 | 41.29 |
| 13992 | candidato, CAPTCHA | 0 | 0.00 | 18 | 6.15 | 18 | 6.15 |
| 18036 | ruta promovida | 0 | 0.00 | 424 | 25.35 | 424 | 25.35 |
| 15215 | candidato, CAPTCHA (puerto que fue de `ejecutivo_3` el 23) | 0 | 0.00 | 18 | 6.16 | 18 | 6.16 |
| 13550 | candidato, `temporary_unavailable` | 0 | 0.00 | 18 | 6.16 | 18 | 6.16 |
| 18516 | candidato, `temporary_unavailable` | 0 | 0.00 | 34 | 6.59 | 34 | 6.59 |
| 17286 | candidato, CAPTCHA | 0 | 0.00 | 19 | 6.15 | 19 | 6.15 |
| 16532 | ruta promovida | 0 | 0.00 | 26 | 8.15 | 26 | 8.15 |
| 10771 | candidato pendiente (worker detenido) | 0 | 0.00 | 19 | 4.30 | 19 | 4.30 |
| 16099 | prueba manual, login rechazado | 0 | 0.00 | 15 | 6.10 | 15 | 6.10 |
| 12024 | prueba manual, búsqueda aceptada | 0 | 0.00 | 17 | 7.91 | 17 | 7.91 |
| **Total** | | **838** | **54.03** | **674** | **87.32** | **1512** | **141.35** |

El 15215 no mezcla tráfico: lo usó `ejecutivo_3` el 23 entre las 01:33 y las
01:52, y `ejecutivo_2` el 24 a las 14:08, con otra salida (hash `c5df974f044d`
el 23 y `c7f95823c1ee` el 24).

Cada login de un candidato deja casi la misma huella: 18 o 19 conexiones y unos
6,15 MB, sobre todo recursos de `www.gstatic.com` y de reCAPTCHA.

## 2. Por dominio

La API no separa dominio por puerto; son las dos cuentas juntas.

| Destino | 23-09 01:00–03:00 (lote y 4 rutas) | 23-09 03:00 → 24-09 01:00 (sólo Chrome inactivo) | 24-09 14:00–23:00 (ventana de la baja) |
|---|---|---|---|
| `nuevo-portal.conservador.cl:443` | 41 conex., 30.62 MB | 8 conex., 2.96 MB | 29 conex., 28.54 MB |
| `www.google.com:443` (reCAPTCHA) | 84 / 3.10 MB | 105 / 2.34 MB | 71 / 2.66 MB |
| `www.gstatic.com:443` (recursos de reCAPTCHA) | 26 / 31.94 MB | 98 / 37.86 MB | 40 / 39.23 MB |
| `mtalk.google.com:5228` (push de Chrome) | 86 / 0.80 MB | **1149 / 10.51 MB** | 31 / 0.25 MB |
| `ipinfo.io:443` (verificación de salida) | 33 / 0.25 MB | 8 / 0.06 MB | 39 / 0.29 MB |
| Otros de Google (`accounts`, `android.clients`, `content-autofill`, `fonts.gstatic`, `passwordsleakcheck`, `csp`, `mtalk:443`) | 62 / 1.74 MB | 64 / 2.39 MB | 62 / 2.03 MB |
| `o90030.ingest.sentry.io:443` (telemetría del portal) | 8 / 0.09 MB | 4 / 0.03 MB | 15 / 0.26 MB |

En los días completos, el portal tuvo 49 conexiones (33,6 MB) el 23 y 39 (36,9 MB)
el 24. La mayor parte de las conexiones no va al portal sino al canal push de
Chrome, que no se cerró mientras el worker mantenía los navegadores abiertos.

## 3. Serie por hora

Consumo total del plan por hora (`stats_with_history`). Las horas sin fila no
tuvieron tráfico. Entre el 23 a las 03:00 y el 24 a las 13:00 se mantiene
estable en unas 60–65 conexiones y 2,2 MB por hora: los dos Chrome inactivos del
worker con su sesión abierta.

| Hora UTC | Conexiones | MB | Errores | Local (UTC−3) |
|---|---|---|---|---|
| 23-09 01 | 232 | 55.02 | 0 | lote del informe, 22:33–23:00 del 22 |
| 23-09 02 | 108 | 13.51 | 0 | cierre del lote, límite diario |
| 23-09 03–24-09 00 | 60–68 por hora | ≈2.2 por hora | 1 (18 h) | inactivo |
| 24-09 01 | 131 | 20.79 | 0 | vence la retención de cuota, A11 |
| 24-09 02 | 74 | 4.00 | 1 | segunda prueba de cuota |
| 24-09 03–13 | 59–64 por hora | ≈2.2 por hora | 0 | inactivo |
| 24-09 14 | 121 | 25.30 | 1 | worker headless, 11:07–11:15 |
| 24-09 19 | 45 | 9.13 | 0 | worker headful, 16:06–16:08 |
| 24-09 21 | 33 | 14.02 | 0 | dos pruebas manuales de `ejecutivo_2` |
| 24-09 22 | 88 | 24.80 | 0 | worker headful, 19:34–19:37, y 401 |

Por puerto, en cada ventana (conexiones y MB):

| Ventana (UTC) | Puertos |
|---|---|
| 23-09 01–02 | 15215: 63 (14.09), 19314: 56 (11.12), 10001: 35 (5.88), 17340: 30 (9.27), 17959: 26 (8.84), 18761: 18 (6.16), 18298: 4 (0.03) |
| 23-09 02–03 | 18325: 46 (8.39), 17340: 39 (1.19), 17844: 21 (3.56), 17959: 1 (0.01) |
| 23-09 03 → 24-09 01 | 18325: 711 (28.51), 17340: 705 (27.65) |
| 24-09 01–02 | 18325: 46 (3.29), 17340: 35 (3.19), 18036: 31 (8.16), 13992: 18 (6.15) |
| 24-09 02 → 14 | 18325: 368 (13.28), 18036: 367 (14.98) |
| 24-09 14–15 | 18516: 34 (6.59), 18325: 24 (4.22), 18036: 21 (2.17), 15215: 18 (6.16), 13550: 18 (6.16) |
| 24-09 19–20 | 17286: 19 (6.15), 18325: 18 (2.93), 18036: 5 (0.04), 13703: 3 (0.02) |
| 24-09 21–22 | 12024: 17 (7.91), 16099: 15 (6.10) |
| 24-09 22–23 | 16532: 26 (8.15), 10491: 19 (6.16), 10771: 19 (4.30), 14435: 18 (6.15), 18325: 5 (0.04) |

### Errores

`errors_stats_with_parameters` registra sólo 3 errores en los dos días, todos
`NO_HOST_CONNECTION` hacia `mtalk.google.com` (23-09 18:00, 24-09 02:00 y 24-09
14:00). **Ningún error del proxy hacia el portal ni hacia reCAPTCHA.** Los
rechazos (`captcha-rechazado`, diálogo de error, 401) son respuestas HTTP dentro
del túnel TLS y el proxy no los ve.

## 4. Cruce con los eventos locales de `ejecutivo_3`

| Hora UTC | Evento local | DataImpulse |
|---|---|---|
| 23-09 01:33–01:52 | 10 búsquedas aceptadas en la ruta 15215; CAPTCHA rechazado y diálogo a las 01:52 | 15215: 63 conex., 14.09 MB |
| 23-09 01:55–01:56 | candidato 18761 rechazado (CAPTCHA) | 18761: 18 / 6.16 MB |
| 23-09 01:57–02:01 | 17959 promovido; búsquedas; CAPTCHA y diálogo a las 02:01 | 17959: 27 / 8.85 MB |
| 23-09 02:01–02:04 | candidato 17844 rechazado (`auth_required`) | 17844: 21 / 3.56 MB |
| 23-09 02:06–02:07 | 18325 promovido; última búsqueda aceptada; límite diario | 18325: 46 / 8.39 MB en la hora |
| 23-09 02:07 → 24-09 01:39 | cuenta retenida por cuota; Chrome abierto sin búsquedas | 18325: 711 conex., casi todas push de Chrome; portal 8 conex. en total entre ambas cuentas |
| 24-09 01:39 | vence la retención; búsqueda no enviada (D25), «Recientes» leído | 18325: 46 / 3.29 MB |
| 24-09 01:39 → 14:07 | inactiva | 18325: 368 / 13.28 MB |
| 24-09 14:07–14:08 | worker **headless**: HTTP 400 «Problemas obteniendo índice de comercio» | 18325: 24 / 4.22 MB |
| 24-09 19:07–19:08 | worker headful: CAPTCHA rechazado, diálogo, perfil borrado; verificación del candidato 13703 | 18325: 18 / 2.93 MB; 13703: 3 / 0.02 MB |
| 24-09 22:35 | arranque: diálogo en la ruta 18325 | 18325: 5 / 0.04 MB |
| 24-09 22:35–22:36 | candidato 10491 rechazado (CAPTCHA) | 10491: 19 / 6.16 MB |
| 24-09 22:36–22:37 | candidato 14435: **HTTP 401 `auth-exception`** | 14435: 18 / 6.15 MB |

En la ventana clave (24-09 14:07 → 22:37), `ejecutivo_3` abrió 87 conexiones
(19,5 MB) en 4 puertos. Ese volumen es menor que el de cualquier ventana con
búsquedas del 23 y no tiene nada anómalo para el proxy.

## 5. Hallazgos

1. **La sesión autenticada salió por varias IP.** DataImpulse cambia la IP de
   un puerto cuando vence `sessttl` (`DATAIMPULSE_STICKY_TTL_MINUTES=120`), pero
   el perfil de Chrome conserva la sesión del portal. Huellas de salida del
   puerto 18325 de `ejecutivo_3`, según los preflight locales:

   | Hora UTC | Hash de salida |
   |---|---|
   | 23-09 02:06 – 03:25 | `658713bbb2b7` |
   | 24-09 01:39 | `2b2e9486efe9` |
   | 24-09 14:07 | `406ca71abd70` |
   | 24-09 19:07 | `ef6902d269bd` |
   | 24-09 22:35 | `366288e56d86` |

   Hasta que el servicio borró el perfil (24-09 19:08), el portal recibió la
   misma sesión desde al menos 4 IP en 41 horas; la quinta huella es del
   arranque de las 22:35, ya sin ese perfil. A
   `ejecutivo_2` le pasó lo mismo (17340: `9844d0a28d8d` → `8bc1a9ffce64`;
   18036: `52b6d1c56ce5` → `5fb1cb787fd2` → `e13011fe48ba`), y sigue activa, así
   que esto solo no explica la baja.

2. **Las dos cuentas compartieron IP.** El pool chileno de DataImpulse repite
   salidas entre puertos:
   - `658713bbb2b7`: `ejecutivo_3` (18325) el 23-09 a las 02:06 y `ejecutivo_2`
     (candidato 13550) el 24-09 a las 14:09.
   - `8bc1a9ffce64`: `ejecutivo_2` (17340) el 24-09 a las 01:36 y `ejecutivo_3`
     (candidato 10491) el 24-09 a las 22:35, un minuto antes del 401.

   El control de unicidad del servicio (`egress_owner`) sólo compara contra las
   salidas vigentes, no contra el historial. Un portal que vincule cuentas por IP
   vería dos cuentas desde la misma IP.

3. **Otra instalación usó la misma cuenta de DataImpulse del 18 al 21 de
   septiembre**, con puertos que esta máquina nunca usó:
   - el 18-09, 26 puertos, de los cuales 14 tienen la huella de un login de
     candidato (18 o 19 conexiones): `10596`, `10712`, `10927`, `11411`,
     `12131`, `13438`, `13947`, `14452`, `15727`, `15930`, `16221`, `16417`,
     `17728`, `18570`;
   - dos sesiones que quedaron abiertas del 18 al 21: `12093` y `17687`, con
     unas 600 conexiones al día cada una (Chrome inactivo). El 21 bajan a 46 y el
     22 no hay tráfico hasta que empieza esta máquina.

   Estas estadísticas no dicen qué cuentas del portal usaron esas sesiones. El
   desarrollador puede cruzar esos puertos con su `account_proxy_routes`. Si una
   era `ejecutivo_3`, explica las búsquedas de su panel «Recientes» que esta
   máquina nunca hizo (sección 8.2 del informe de pruebas).

4. **Chrome inactivo mantiene tráfico constante por el proxy móvil:** unas 60
   conexiones y 2,2 MB por hora, casi todo el canal push de Google. En 24 horas,
   con dos cuentas, son unos 53 MB del plan sin ninguna búsqueda.

## 6. Lo que la API no permite saber

- El número de peticiones HTTP al portal, sus rutas (`/api/v1/...`) y sus
  respuestas. Sólo cuenta conexiones por túnel.
- Dominio por puerto y serie por hora por dominio.
- Las IP de salida; sólo existen como hash en los preflight locales, cuando se
  hizo un preflight.
- Qué cuenta del portal usó cada puerto de la otra instalación.
- Nada anterior al 18-09. La retención es de unos 7 días: las cifras del 23 y
  el 24 se pierden hacia el 30-09 y el 01-10.

Desde `15a7f53` y `18d941b` (ya en `master`), el servicio registra cada petición
al portal por cuenta en `.cbrs/runtime/logs/requests/<cuenta>/`, y
`cbrs requests CUENTA --since … --until …` las cuenta por endpoint. Ese registro
cubre lo que el proxy no ve.

## 7. Preguntas para el desarrollador

1. ¿Qué cuentas usaron los puertos `12093` y `17687` (18 al 21 de septiembre) y
   los candidatos del 18? ¿Alguno era `ejecutivo_3`?
2. ¿Conviene bajar `sessttl` o renovar la sesión del portal cuando cambia la
   salida del puerto, para que una sesión autenticada no aparezca desde muchas IP?
3. ¿Conviene que el control de unicidad compare contra el historial reciente de
   salidas de las otras cuentas, no sólo contra las vigentes?
4. ¿Conviene cerrar el canal push de Chrome (`mtalk.google.com:5228`) o limitar
   el tráfico de fondo, que consume plan del proxy móvil sin búsquedas?
