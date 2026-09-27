# Respuesta al informe de DataImpulse del 24-09-2026

Este documento responde las preguntas 2–4 de
[`INFORME-DATAIMPULSE-2026-09-24.md`](../INFORME-DATAIMPULSE-2026-09-24.md).
Las coincidencias de IP son una hipótesis sobre la baja, no una causa probada.
Las cuentas y credenciales del desarrollador son distintas de las del mandante.

## 2. Duración de la salida y sesión del portal

**No bajar `sessttl` ni renovar automáticamente la sesión al cambiar la IP.**
Un intervalo menor haría que una sesión persistente pase por más salidas. DataImpulse
define `sessttl` como el intervalo de rotación y advierte que puede sustituir una
IP antes si deja de estar disponible
([documentación de DataImpulse](https://docs.dataimpulse.com/proxies/parameters/session-interval)).
Por tanto, incluso un intervalo mayor no garantiza una salida fija. Volver a iniciar
sesión sólo por el vencimiento añade logins y exige intervenir el navegador autenticado.
Mantener la sesión y la ruta actuales; evaluar un intervalo mayor o una salida
dedicada únicamente con una prueba aislada y medidas de continuidad. No hay
evidencia de que el cambio de IP por sí solo causara la baja: `ejecutivo_2` también
cambió de salida y siguió activo.

## 3. Unicidad frente al historial

**Sí, para candidatos nuevos.** El control ahora recuerda durante siete días las
huellas de salida verificadas para cada cuenta y descarta un candidato si esa misma
huella fue observada recientemente en otra cuenta. La verificación ocurre antes
del login del candidato. Se conservan el control de salidas vigentes, el límite de
intentos y los contextos existentes. Sólo se guardan huellas, no IP sin cifrar ni
credenciales. También se consideran los logins rechazados de otros candidatos;
las comprobaciones que acabaron antes del login quedan excluidas. La ventana de
siete días es conservadora pero no demuestra que
el portal olvide una IP después de ese plazo.

El historial nuevo comienza al activar el código y hacer las verificaciones de
salida; también se consulta el registro de candidatos que ya exista en la base.
Los informes `preflight` anteriores no se importan de forma automática. Las
sesiones que ya están vivas no se cierran ni rotan por una coincidencia histórica.
El esquema 10 y esta lógica quedaron activos en la producción WSL el 27-09-2026.

## 4. Tráfico de fondo de Chrome

**Medir antes de cambiar Chrome.** El informe atribuye gran parte del consumo
inactivo a `mtalk.google.com:5228`, pero no muestra que ese tráfico causara la baja.
No cerrar el canal en los perfiles autenticados ni aplicar un bloqueo de red a los
navegadores vivos. Chromium tiene `--disable-background-networking`, descrito
por su propio código como una opción para pruebas de rendimiento
([código de Chromium](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/common/chrome_switches.h));
su efecto en el portal y reCAPTCHA no está validado aquí. Una comparación en
perfiles desechables, sin cuentas del mandante, tendría que medir conexiones,
bytes, login, búsqueda y continuidad antes de plantear un cambio de lanzamiento.

## Alcance de la observación

El registro de peticiones identifica cada solicitud que **esta instalación**
envía al portal y su puerto de proxy. Ahora agrega la última huella de salida
observada para ese perfil y esa configuración exacta de proxy, con los campos
`egress_observed_hash`, `egress_observed_at` y `egress_observation_source`.
Se actualizan con los `preflight` y las comprobaciones de salud existentes;
no se añaden llamadas de red ni se guardan IP sin cifrar. Si no hay observación
para ese perfil y esa ruta en el proceso, los campos se omiten. Los datos se pueden
consultar con `cbrs requests CUENTA --json`.

Estos campos no prueban la IP exacta de cada solicitud:
`sessttl` o una sustitución del proveedor puede cambiarla entre dos mediciones.
Los `preflight` registran observaciones puntuales de la huella de salida. Tampoco
puede observar peticiones hechas por otra instalación con las mismas credenciales.
El panel «Recientes» puede mostrar búsquedas que no figuran en los trabajos locales,
pero sin una línea de base completa no permite atribuirlas a una persona concreta.
El motivo de una baja tampoco se puede inferir de los códigos HTTP.
Los códigos de rechazo y los diálogos visibles siguen en los eventos y la
evidencia de cada intento; no se copian cuerpos ni mensajes sin filtrar al
registro de peticiones.

## Otros pendientes del handoff

El overview ya cuenta con etiquetas en español para `pending_reconciliation`,
`portal_quota_exhausted` y `portal_quota_check_due`. El README dirige una
instalación nueva, incluida Ubuntu sin systemd, a `INSTALL.md`, y distingue los
comandos propios del alojamiento WSL del desarrollador.

A10 sigue requiriendo una observación real del vencimiento de cuota. El resultado
A11 del cliente no se vuelve a presentar como una validación local nueva. A16
requiere 24 horas continuas; no puede darse por aprobado mientras el host tenga
reinicios diarios. Ninguna prueba sintética sustituye esas observaciones.

## Validación y activación

La release se probó en un directorio aislado de WSL con la `.venv` Linux y un
usuario sin privilegios: 629 tests aprobados y uno omitido; tras el ajuste final
de persistencia, los 138 tests afectados volvieron a pasar. Las nueve
comprobaciones JavaScript del overview también pasaron. Los tests cubren la exclusión de una salida que otra
cuenta usó antes, la expiración del historial, los candidatos descartados antes
del login y la asociación de las observaciones de salida a su perfil y ruta.
La prueba de Chrome usa respuestas simuladas; no consume búsquedas del portal.

El operador autorizó el reinicio del propietario, Chrome y worker y otorgó
autorización permanente para los reinicios necesarios en futuras releases,
registrada en `AGENTS.md`. La activación terminó el 27-09-2026 a las 20:25 UTC:
las tres cuentas mostraban el formulario autenticado, el registro de peticiones
de producción incluía las observaciones de salida y el esquema era 10.
Se conservaron las rutas y los perfiles; `cbrs-display` siguió en el mismo proceso.
Los 45 recibos de búsquedas exitosas anteriores seguían intactos y ninguno de
los 36 registros de uso de cuota retrocedió. Esta observación no garantiza la
vigencia de una sesión en futuros reinicios ni reemplaza A10/A16.
