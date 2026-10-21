# Informe de cambios: registro de cuentas CBRS

**Fecha de las pruebas:** 20 de octubre de 2026, hora de Santiago.

**Repositorio:** `scrapper-portal-conservador`. **Rama de entrega:** `master`.

**Comparación:** cambios acumulados respecto de `f4ca36c`.

## Resultado ejecutivo

Se corrigió y amplió el proceso independiente de registro de cuentas: ahora
completa el formulario visible con Playwright, comprueba sus valores, captura
evidencia visual por etapa y permite diagnósticos acotados con los dos proveedores
de CAPTCHA y con rutas de proxy temporales.

La validación automatizada terminó con **195 pruebas aprobadas**. En las pruebas
reales se confirmó el llenado del formulario y la salida de red chilena, pero
**no se consiguió crear y verificar una cuenta**. El portal devolvió rechazos de
CAPTCHA y errores genéricos. Cambiar de IP no permitió demostrar que la IP fuera
la causa única del problema.

Esta entrega contiene código, pruebas y documentación. No incluye despliegue en
el servicio productivo ni activación de cuentas nuevas en su conjunto operativo.

## Alcance de la comparación

El punto de partida ya contenía el registro, la activación por correo, la
verificación y una interfaz de comandos. Esta entrega incorpora las correcciones
pendientes del primer diagnóstico y las mejoras posteriores. También integra
las modificaciones previas de compatibilidad IMAP y generación opcional de
celular, conservadas sin modificar durante esta continuación.

| Área | Situación anterior | Comportamiento entregado |
| --- | --- | --- |
| RUT | La inserción podía conservar formatos distintos y omitir duplicados equivalentes. | Normalización canónica antes de validar, comparar e insertar; limpieza de espacios en nombres. |
| Lotes | El agotamiento del conjunto de RUT podía interrumpir un lote y ocultar su resultado parcial. | Detención controlada, conservación de los resultados ya obtenidos y explicación desde la CLI. |
| Selección de ruta | La prevención de colisiones consultaba una ubicación antigua de la base de rutas. | Consulta de la ubicación correcta; recuperación con exclusiones adicionales y acceso de solo lectura. |
| Correo | La implementación y las opciones de configuración asumían principalmente Gmail. | Contrato público de buzón, IMAP configurable con SSL o STARTTLS, carpeta configurable y comprobación de conexión. |
| Formulario | El alta se enviaba mediante una llamada JSON desde el navegador, sin completar los controles visibles. | Llenado real, validación de diez campos y aceptación de términos; envío desde el botón nativo. |
| Diagnóstico visual | Faltaba evidencia del estado de la página en cada etapa. | Capturas numeradas opcionales, con campos de contraseña y token enmascarados. |
| CAPTCHA | Un token generado pero rechazado no utilizaba la cadena completa disponible en el repositorio. | Uso opcional y acotado de navegador, CapSolver y 2Captcha, con presupuesto y auditoría propios del registro. |
| Recuperación | Un intento ordinario utilizaba una sola ruta temporal. | Modo explícito para una identidad y candidatos distintos, con límites persistentes y condiciones de parada. |
| Exportación | El formato exportado no era compatible con el cargador del conjunto de cuentas del worker. | Parche JSON compatible, referencias a variables de entorno y cuentas inicialmente deshabilitadas. |
| Errores | Algunos fallos del portal aparecían como trazas de excepción en la consola. | Mensajes controlados y resultados por etapa. |

## Detalle funcional

### Formulario y envío

Playwright utiliza Google Chrome regular en un perfil creado exclusivamente para
el registro. Se completan correo y confirmación, contraseña y confirmación, RUT,
nombres, ambos apellidos, dirección y celular; luego se comprueba la aceptación
de términos. Los valores se leen nuevamente antes del envío.

Se corrigió la adaptación del celular: el campo visible ya incorpora el prefijo
chileno y recibe los ocho dígitos restantes del número nacional almacenado.
La comparación contempla el formato visual del RUT y del teléfono.

El botón «Crear cuenta» genera la solicitud de la propia aplicación. Una guarda
contrasta el contenido saliente con la identidad preparada y bloquea la solicitud
si encuentra diferencias. La preparación de la siguiente cuenta después de un
alta aceptada no vuelve a enviar el alta anterior.

El modo `--dry-run` completa y comprueba el formulario y obtiene un token de
diagnóstico, sin pulsar el botón de creación; al terminar libera el RUT reservado.
No demuestra que el servidor vaya a aceptar una creación real.

### Evidencia y proveedores de CAPTCHA

`--screenshots` guarda capturas durante la carga, preparación del formulario,
obtención de tokens, envío y respuesta. Un fallo de captura no debe impedir el
cierre del navegador temporal. Los registros de respuesta incluyen ruta HTTP,
estado y código saneado, sin volcar cuerpos de respuesta, contraseñas ni JWT.
Las imágenes pueden contener datos personales visibles y permanecen fuera de Git.

`--external-captcha` habilita explícitamente los proveedores externos. El
navegador se utiliza primero; un rechazo explícito de CAPTCHA puede avanzar a
CapSolver y después a 2Captcha. El contrato de cada cliente existente se respeta:
CapSolver recibe el proxy y la identidad del navegador temporal; el cliente
Enterprise v3 de 2Captcha funciona sin proxy asociado.

El presupuesto de registro se almacena separado del worker, respeta su límite
diario configurado y los períodos de espera por rechazo, y admite como máximo
cuatro reservas de tareas pagadas por invocación. Obtener un token no equivale a
que el portal lo acepte. La cadena de proveedores no reintenta automáticamente
una creación aceptada ni una solicitud cuyo resultado sea desconocido.

### Recuperación limitada de rutas

`--recover-routes` requiere `--count 1`. Conserva la misma identidad, correo,
contraseña y datos de formulario entre candidatos y entre continuaciones
permitidas. No genera una cuenta distinta por cada IP.

Antes de reservar una identidad, lee las exclusiones productivas. Si no puede
leerlas, se detiene. Cada candidato comprueba país de salida, evita puertos y
salidas protegidas o previamente intentadas, y utiliza su propio perfil temporal.
El historial de diagnóstico guarda hashes de salida y metadatos, sin IPs ni
credenciales en texto claro.

Solo los rechazos clasificados expresamente o el error conocido anterior al
envío permiten pasar a otro candidato. La aceptación, el resultado incierto,
los bloqueos y límites HTTP pertinentes, y los errores terminales del proveedor
detienen la recuperación. Tras un alta aceptada, la activación y verificación
continúan sobre ese mismo candidato.

Se respetan los límites configurados por pasada, por hora y por identidad durante
24 horas móviles. En la prueba realizada se aplicaron tres candidatos por
pasada, diez segundos de espera entre rechazos del portal y diez reservas de
posible envío por identidad. No se aumentaron esos límites.

## Archivos incluidos

| Archivo | Cambio principal |
| --- | --- |
| `.env.example` | Documentación previa de correo IMAP configurable y celular opcional. |
| `cbrs/registration/__init__.py` | Exportaciones públicas del contrato de buzón y generador de celular. |
| `cbrs/registration/__main__.py` | Opciones de diagnóstico y recuperación, comprobación de buzón, errores y exportación. |
| `cbrs/registration/config.py` | Configuración de IMAP, proveedores, evidencia, límites y ruta correcta de estado. |
| `cbrs/registration/email_inbox.py` | Compatibilidad IMAP y consulta de carpetas. |
| `cbrs/registration/identity.py` | Generación opcional de celular por cuenta. |
| `cbrs/registration/orchestrator.py` | Preparación de formulario, lotes parciales y finalización común de altas aceptadas. |
| `cbrs/registration/portal.py` | Llenado y envío nativo, comprobaciones, capturas y cadena acotada de proveedores. |
| `cbrs/registration/rut_refill.py` | Normalización, validación y deduplicación de RUT. |
| `cbrs/registration/store.py` | Reutilización controlada de credenciales, seguimiento de candidato y exportación compatible. |
| `cbrs/registration/routes.py` | Exclusiones productivas de solo lectura e historial independiente con límites persistentes. |
| `cbrs/registration/recovery.py` | Coordinación de candidatos para una sola identidad y parada por aceptación o incertidumbre. |
| `tests/test_registration.py` | Ampliación de pruebas de regresión y cuatro casos con Chrome contra localhost. |
| `docs/handoff-registration-e2e-2026-10-20.md` | Historial técnico del diagnóstico, con identificadores operativos anonimizados para versionado. |
| `docs/client-registration-changes-2026-10-20.md` | Este informe para entrega al cliente. |

## Validación realizada

La ejecución combinada aprobó **195 pruebas**: 84 de registro, incluidas cuatro
con Chrome real contra un servidor local, y 111 de los módulos de CAPTCHA,
cliente, navegador, DataImpulse y conjunto de cuentas. Los casos con Chrome
local verifican el envío nativo, la sustitución autorizada del token, el bloqueo
de datos inconsistentes y el tratamiento del error anterior al envío. No utilizan
el portal productivo para esas comprobaciones.

Comando utilizado en PowerShell:

```powershell
$env:CBRS_RUN_BROWSER_TESTS = '1'
python -m pytest tests/test_registration.py tests/test_capsolver.py tests/test_captcha_solver.py tests/test_client.py tests/test_browser_session.py tests/test_dataimpulse.py tests/test_account_pool.py -q
```

Sin `CBRS_RUN_BROWSER_TESTS=1`, se omiten los cuatro casos que abren Chrome.
También se comprobó el formato del diff con `git diff --check`.

Las pruebas cubren normalización, liberación y consumo de RUT; resultados
parciales; contratos IMAP; exportación sin secretos dentro del JSON; continuidad
de identidad; exclusiones productivas; presupuestos; y detención ante alta
aceptada o resultado incierto.

### Resultado de la prueba real de rutas

Entre las 22:42 y las 22:57 del 20 de octubre se probaron diez puertos y diez
salidas chilenas distintas, manteniendo una misma identidad durante la secuencia.

| Observación | Resultado |
| --- | --- |
| Candidatos que llegaron al endpoint de creación | 8 de 10. |
| Respuestas de creación, contando reintentos de proveedor | 11, todas HTTP 400. |
| Rechazos explícitos de CAPTCHA | 8 respuestas. |
| Rechazos genéricos del portal | 3 respuestas. |
| Candidatos con error visible antes de observar un POST | 2. |
| Tareas externas reservadas durante esta secuencia | 4: tres tokens devueltos y rechazados, y un fallo del proveedor antes del envío. |
| Cuentas creadas y verificadas satisfactoriamente | 0. |
| Motivo de detención final | Límite existente de diez reservas por identidad en 24 horas móviles. |

Una reserva de posible envío se consume antes del clic y puede incluir un fallo
anterior al POST. Por eso diez reservas no significan diez solicitudes HTTP.
Las capturas permitieron comprobar el formulario lleno en los fallos observados.
No permiten identificar por sí solas la causa de un rechazo del servidor.

## Operación y pendientes

La base de identidades existente no se vació ni se volvió a cargar. Se conservó
la fila con interpretación de apellidos pendiente de confirmación del operador.
Durante el diagnóstico se corrigió el manejo de un rechazo de candidato y se
liberó únicamente la reserva afectada, después de comprobar su estado. No se
restablecieron identidades en bloque.

El servicio, sus navegadores, sesiones, cookies y rutas productivas se
mantuvieron. No se modificaron `.env` ni el JSON operativo del conjunto de
cuentas. Las bases locales, perfiles, capturas y credenciales siguen excluidos de
Git. La entrega a `master` no activa estos cambios en el checkout productivo WSL.

Antes de declarar completo el registro de extremo a extremo falta obtener un
alta aceptada y comprobar correo de activación, activación, inicio de sesión y
consulta del usuario. El diagnóstico disponible no distingue entre restricciones
de la red del proveedor, condiciones del portal u otras causas asociadas al alta.
El siguiente paso necesita información del lado del portal o una prueba
diagnóstica acordada; no corresponde repetir altas sin reconciliar resultados ni
aumentar automáticamente los límites.

Una vez verificadas las cuentas, `export-pool` preparará un parche JSON con las
cuentas deshabilitadas y un archivo local separado de credenciales. Sus tres
pasos manuales son:

1. Incorporar las variables de credenciales generadas a la configuración privada.
2. Integrar las entradas del parche en el conjunto operativo que lee el worker.
3. Habilitar únicamente las cuentas y rutas que el operador haya aprobado.

La selección de proveedor y puerto para uso productivo sigue siendo una decisión
explícita del operador. La exportación y esta integración no se ejecutaron en las
pruebas reales porque no hubo cuentas verificadas.

## Cómo revisar la entrega

```text
git diff --stat f4ca36c..master
git diff f4ca36c..master -- cbrs/registration tests/test_registration.py .env.example docs/client-registration-changes-2026-10-20.md docs/handoff-registration-e2e-2026-10-20.md
```

El hash definitivo de la entrega puede consultarse con `git log -1 master`.
Este informe omite identidades, buzones, credenciales, tokens e IPs individuales.
