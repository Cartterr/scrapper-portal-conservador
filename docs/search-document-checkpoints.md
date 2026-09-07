# Búsqueda final y procesamiento documental

Una búsqueda aceptada no se repite para el mismo job ID. El worker guarda en
una transacción SQLite los resultados, el recibo `search_completed` de la cuenta
y el uso de cuota. `result_count = 0` también es un checkpoint definitivo.
Un segundo guardado no reemplaza resultados existentes, ni siquiera vacíos.

Las etapas son:

1. Búsqueda CBRS: reserva un cupo; la aceptación persistida lo confirma.
2. Recuperación de páginas: usa los tickets guardados y la cuenta original.
   Sigue contactando CBRS; no es un proceso completamente local.
3. PDF: ensambla las imágenes localmente, valida y publica el archivo mediante
   reemplazo atómico. No consume otro cupo de búsqueda.

La cola durable `job_items` mantiene el trabajo documental separado del resultado
de búsqueda. Las etapas siguen ejecutándose dentro del worker propietario de
Chrome: no se introduce un segundo proceso que compita por perfiles o sesiones.
El estado global del job describe la entrega; `search_status` y `document_status`
en el payload permiten distinguir una búsqueda completada de una entrega pendiente.

Una cuenta con su cupo local de búsquedas agotado puede terminar documentos de
una búsqueda ya pagada, pero conserva los bloqueos de disponibilidad, CAPTCHA,
seguridad y portal. Los tickets no pasan automáticamente a otra cuenta. Un
fallo documental no crea otra búsqueda ni libera la cuota ya consumida.

El lease exclusivo sigue protegiendo al worker; además, `begin_attempt` rechaza
una nueva reserva para un job con búsqueda en curso o ya completada.

## Fallos y límites

Si el proceso cae entre la respuesta externa y el commit local, no es posible
garantizar exactamente una ejecución remota sin idempotencia del portal. El job
se retiene con `search_outcome_unknown`, sin repetirlo automáticamente. Un recibo
legacy de éxito sin resultados se retiene con `search_receipt_incomplete`. Los
resultados legacy sin cuenta identificable requieren revisión (`search_owner_unknown`).
Estos casos usan `waiting_capacity` como estado compatible de espera y el código
de motivo específico; esperar al próximo día no autoriza repetir la búsqueda.

El contador local no demuestra cómo CBRS contabiliza sus propias solicitudes.
Los 60 cupos son búsquedas aceptadas, no una garantía de 60 PDFs.

## Activación

Cambios probados offline. No reiniciar el worker ni Chrome para desplegarlos.
Se cargan únicamente durante el próximo reinicio completo autorizado. Se mantiene
CapSolver antes de 2Captcha y el plan actual de seis muestras de tres páginas.
