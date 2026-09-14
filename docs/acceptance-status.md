# Estado de aceptación — 2026-09-13

Base: `master` en `c0c86bde2943e4e8e170a622946fcf12b2673bc5`, más cambios
locales de esta entrega. Los criterios originales se incorporan sin modificar
desde `handoff-review-fixes` (`6b15e22`). No se fusionó esa rama completa.

**Resultado: aceptación global todavía no certificada.** La implementación y
las pruebas offline no sustituyen A1–A20 con cuentas reales en Ubuntu limpio.
No se modificó ni reinició el servicio productivo. Las cifras y PIDs históricos
citados en la conversación no fueron revalidados durante este trabajo.

## Aclaración del propietario del proyecto

El 2026-09-13, José confirmó que deben mantenerse las reglas de `AGENTS.md`:
si la cuenta sigue autenticada, conservar el proxy y la sesión exitosos.
DOM desconocido no equivale a logout. No se habilita reemplazo automático de
una sesión protegida por fallos de búsqueda, PDF, CAPTCHA o red. La recuperación
de login mantiene las condiciones y cuentas explícitamente autorizadas.

Esto limita la interpretación literal de R5. No se cambia el documento original
del cliente; hace falta que la aceptación contractual refleje esta aclaración.

## Implementación preparada

- `cbrs get`, `get-batch`, `status` y API `Client` sobre la cola durable existente.
- JSON, códigos de salida, CSV con alias, filas inválidas conservadas, columnas
  adicionales, duplicados y lotes superiores a 100 filas.
- Reutilización de trabajos existentes, PDFs validados por hash/páginas,
  publicación de copias sin sobrescribir archivos diferentes, `--force` explícito.
- `not_found` sólo con resultado vacío confirmado. Timeout conserva el trabajo.
- Documentos de producción con fallos temporales quedan pendientes y vuelven
  a intentarse conservando búsquedas aceptadas. El scheduler de endurance
  mantiene sus límites separados. Un PDF original ausente puede reencolar su
  documento al repetir `get`, sin crear otra búsqueda.
- Detección `1..N` desde `.env` cuando no hay un JSON explícito anterior.
- Claves CAPTCHA opcionales, con fallback automático si hay clave y no se
  seleccionó un modo explícito.
- Empaquetado editable/wheel, instalación documentada, wrapper que conserva
  las rutas relativas del usuario y `service install`.
- Worker con `Restart=always`; el propietario conserva `Restart=no` por su
  política de preservación. Ninguna unidad nueva fue activada.
- Instalaciones nuevas con cuentas descubiertas usan 8 como estimación y
  continúan hasta un límite confirmado por el portal. Un JSON existente con
  cuentas/presupuesto explícitos conserva su límite. `enforce_estimated_quota`
  permite expresar esa política en JSON sin confundirla con evidencia CBRS.

## Matriz A1–A20

| Prueba | Evidencia disponible | Estado de aceptación real |
|---|---|---|
| A1 instalación limpia | Instalador y `INSTALL.md` preparados; wheel construido | Pendiente Ubuntu limpio, operador independiente y cronometraje |
| A2 PDF válido | API/CLI, verificación hash/páginas y nombres probados con PDF sintético | Pendiente PDF real y correspondencia visual |
| A3 caché | Dos llamadas reutilizan trabajo, ruta y artefacto sin crear intentos | Pendiente comprobar cuota real |
| A4 inexistente | Resultado vacío confirmado produce `not_found` sin reenviar | Pendiente inscripción inexistente real |
| A5 inválidos | Validación y CLI probados offline | Cubierto offline |
| A6 servicio detenido | Lease ausente produce código 2 y comando de inicio | Cubierto offline; falta prueba systemd |
| A7 lote mixto | 10 PDFs sintéticos + 1 vacío + 1 fila inválida, reporte y código 1 | Cubierto offline; falta lote real |
| A8 repetición | Mismos IDs y sin descargas nuevas en lote simulado | Cubierto offline; falta lote real |
| A9 API Python | Misma implementación que CLI; import sin configuración/red/Chrome | Cubierto offline; falta ejecución contra servicio real |
| A10 agotar cuotas | Estados distinguen cuota CBRS de login fallido y cupo estimado | Pendiente lote real mayor a cuota conjunta |
| A11 reanudación | Cola y reintentos conservan recibos; pruebas de cuota existentes | Pendiente observación cruzando liberación de cuota |
| A12 proxy bloqueado | Pruebas existentes de candidatos/rechazo y preservación | Pendiente prueba aislada; sujeta a aclaración de sesiones |
| A13 matar Chrome | Protección del propietario sin alteraciones | No ejecutada ni certificada |
| A14 matar worker | Unidad preparada y pruebas de recuperación durable | Pendiente prueba aislada; búsquedas ambiguas pueden requerir conciliación |
| A15 reiniciar máquina | Unidades habilitables y almacenamiento durable | No ejecutada ni certificada |
| A16 24 h memoria | No se presenta una simulación como observación real | Pendiente medición de 24 h y umbral acordado |
| A17 pytest | Suite offline; verificación final anotada abajo | Evidencia local, no una instalación Ubuntu limpia |
| A18 secretos | Comparador local `deploy/check_secret_leaks.py` sin revelar valores | Fuente local comprobable; logs del host final pendientes |
| A19 quitar cuenta | Descubrimiento probado con 1, 2, 3, 4 y 12 cuentas | Pendiente edición/reinicio en host de aceptación |
| A20 sin claves CAPTCHA | Configuración browser por defecto y claves opcionales probadas | Pendiente A2 real sin claves |

## Bloqueos que no deben ocultarse

1. Una instalación anterior con presupuesto explícito conserva su límite;
   no se migra automáticamente a la política de cuota confirmada. Se informa
   como `estimated_quota_reached`, nunca como cuota CBRS confirmada. El host
   final debe validar la política nueva sin alterar cuotas históricas.
2. Una búsqueda enviada con resultado ambiguo queda pendiente para conciliar
   el historial. Reintentar a ciegas reproduciría P6 y consumiría cuotas dobles.
   La conciliación automática completa no está implementada/certificada.
3. Fallos permanentes de proveedor, credenciales o indisponibilidad del portal
   no se arreglan por reintentar indefinidamente. R5 necesita definir el estado
   esperado cuando no hay acceso pero tampoco hay evidencia de cuota agotada.
4. La política actual impide garantizar reemplazo de todos los contextos y
   recuperación de cualquier caída sin intervención; el propietario no tiene
   `Restart=always`. La prueba real debe respetar la aclaración anterior.
5. Las pruebas de 24 h, reinicios y consumos de cuota requieren un host dedicado
   y cuentas que no compitan con la producción actual. No se ejecutaron sobre
   las sesiones productivas.
6. Un PDF registrado que haya sido sustituido por otro archivo válido con hash
   distinto se rechaza; no se sobrescribe automáticamente el archivo alterado.

## Ejecución reproducible

```bash
python -m pytest -q --junitxml=.cbrs/acceptance-results.xml
python -m pip wheel --no-deps --wheel-dir .cbrs/acceptance-packages .
python deploy/check_secret_leaks.py --logs .cbrs/runtime/logs
```

Los PDFs usados por `tests/test_public_api.py` son sintéticos y no se presentan
como documentos del portal. `examples/inscripciones.csv` contiene los dos casos
documentados en la revisión; aún no se entrega `resultados.csv` real nuevo.

## Evidencia final

- Entorno de pruebas: Windows nativo, `.venv`, Python **3.14.3**.
- Suite completa: **511 passed, 1 skipped**, 193.48 segundos. JUnit local:
  `.cbrs/acceptance-results.xml`. La omisión corresponde al límite de sockets Unix.
- Después de la revisión final de errores tipados y protección de operaciones
  vivas: `tests/test_public_api.py` + `tests/test_document_cache.py`,
  **33 passed**, 22.05 segundos. Este conjunto se solapa con la suite completa;
  no se suman las cifras como pruebas distintas.
- Compilación de sintaxis con Python 3.11 nativo: correcta. No sustituye la
  ejecución de las dependencias completas en Ubuntu 22.04/24.04.
- Sintaxis de `deploy/install-ubuntu.sh`: `bash -n`, código 0.
- Argumentos inválidos en proceso separado: código 2 y una línea en stderr.
- Comparación de secretos configurados contra **206 archivos fuente**, incluidos
  cambios locales: **0 archivos con coincidencias**, **0 ilegibles**. No incluye
  logs de producción ni datos históricos externos al árbol de código.
- Wheel final: `.cbrs/acceptance-final-packages/cbrs-0.1.0-py3-none-any.whl`.
  SHA-256: `eaf16a827ee653d0ee063fd08adb5bafad6eb4cd0a8dbe6355f5f13c599a404e`.
- Documento original del cliente: blob Git
  `8f4f73be98670a902038bc636bc218fd50052203`, idéntico al de la rama fuente.
- `git diff --check`: correcto. Rama actual: `master`. Cambios locales,
  sin commit, push ni activación en producción.
