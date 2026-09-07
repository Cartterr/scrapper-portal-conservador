# Búsqueda FNA desde el formulario del portal

La nueva implementación de `CBRSScraper.search_by_fna` usa los inputs reales
`#input-fojas`, `#input-numero` y `#input-ano` del formulario protegido.
Verifica los valores después de rellenarlos y pulsa `Buscar` exactamente una vez.
El listener se arma antes del click y acepta únicamente el POST `/api/v1/comercio/indice/texto`
del mismo origen con las coordenadas de esa búsqueda. `/api/v1/user/recientes`
solo registra historial y jamás es prueba suficiente de una búsqueda exitosa.

El sitio genera su propio token y cuerpo inicial, sin `fetch` sintético para la
primera búsqueda. Se preservan los resultados/tickets devueltos para el pipeline
existente de páginas y PDF. Una lista vacía también es una respuesta válida.
Las búsquedas por texto conservan su implementación anterior; este cambio cubre
FNA, utilizado por los seis fixtures de endurance.

No se navega ni refresca automáticamente una sesión existente. La ruta y el
formulario deben estar abiertos; si no, el job falla de forma acotada. No se
presupone que un cambio visual equivalga a un logout. Timeout, respuesta inválida
o `intente-mas-tarde` nunca generan otro click ni fallback silencioso al API.
Los controles de cuota, cooldown, circuitos y preservación del worker se mantienen.

Un rechazo CAPTCHA explícito puede transferirse a la cadena existente de solver
externo (CapSolver y luego 2Captcha según configuración), reutilizando la
respuesta observada: no se vuelve a enviar primero un token de navegador. La
generación pagada y aceptación siguen registrándose por el cliente existente.
Un error temporal genérico NO se convierte en una autorización CAPTCHA.

Pruebas: Chrome nativo efímero con rutas interceptadas localmente para comprobar
fill/click/respuesta, ignorar recientes, lista vacía, error temporal y formato
inválido. No se usan sesiones, cuentas ni tráfico CBRS reales en estos tests.
Esto demuestra integración local, no fiabilidad de largo plazo en el portal.

Activación pendiente: el worker vivo no ha sido reiniciado ni modificado en
memoria. No iniciar otro controlador sobre sus páginas ni cerrar sus contextos
para desplegar. Una verificación E2E real de esta implementación sigue pendiente
de una vía segura de activación compatible con preservar las sesiones existentes.
