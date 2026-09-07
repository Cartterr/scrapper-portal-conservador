# Capturas por intento con error

El worker captura el viewport de la cuenta antes de propagar errores de una
operación del job. Los rechazos de API, incluidos los rechazos CAPTCHA manejados
por el fallback, también notifican al capturador. Los fallos de descarga/montaje
documental atrapados por item se capturan antes de marcar el item fallido.

La captura se asocia al último intento de esa cuenta dentro del job en ejecución.
Si la búsqueda ya terminó, un fallo documental puede aparecer adjunto al recibo
de búsqueda exitoso; la evidencia no cambia ni duplica su cuota ni su estado.
Una sesión sin job/intento activo no recibe una asociación histórica inventada.

Cada evidencia guarda ID aleatorio, hora de captura UTC, tipo de error, HTTP si
existe y resultado de captura. No guarda cuerpos de respuesta, URLs, tokens ni
contraseñas. Se enmascaran inputs, textareas y campos editables. Las capturas
pueden contener datos visibles del portal: se almacenan localmente junto al
estado y se sirven solo a clientes loopback, con `no-store`.

Los JPEG son independientes de los previews sobrescribibles y se publican
atómicamente. Límite: ocho evidencias por intento, 4 MB por imagen y tres segundos
de timeout de captura. El mismo objeto de excepción no se captura dos veces.
No hay borrado automático del histórico en esta versión; considerar su tamaño
en la política operativa de retención. Un navegador inaccesible se registra como
`unavailable`, sin reiniciarlo ni usar un preview viejo como falsa evidencia.

En el historial expandable de intentos se muestra la miniatura, la fecha, el
error y un enlace para ampliar en otra pestaña. Si no se pudo capturar, se indica
explícitamente. La captura no es prueba de un mensaje visible: los errores de
`fetch` pueden no modificar el DOM aunque el backend haya rechazado la solicitud.

No se refresca, navega, cierra, pausa, cambia proxy ni reautentica para capturar.
El capturador es best-effort: jamás sustituye el error original ni decide la
recuperación. No puede reconstruir errores anteriores a su activación.

Activación pendiente: el worker y dashboard actuales no han sido reiniciados.
No detener las sesiones autenticadas para cargar estos cambios.
