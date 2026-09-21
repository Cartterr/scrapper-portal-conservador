# Cambios

## 0.3.0 — informe de pruebas del 17 de septiembre, 2026-09-20

- Diálogos del portal reconocidos por frase, no por oración exacta: el modal de
  límite diario que bloquea el formulario marca la cuenta `held` sin enviar
  otra búsqueda; un modal desconocido se registra con su título y mensaje
  (`search_form_blocked_by_dialog`) y se cierra en vez de repetirse cada minuto (D17).
- «No se pudo realizar búsqueda, intente nuevamente por favor» se reconoce como
  fallo previo al registro: se cierra el modal y la búsqueda vuelve a intentarse
  sin consumir cuota (D25).
- Ante un resultado desconocido, el panel «Recientes» del portal decide: si la
  inscripción no figura, el reintento es automático; si figura o el panel no
  puede leerse, el trabajo queda en `pending_reconciliation` con el comando
  exacto en el reporte (D22).
- `cbrs jobs reconcile JOB_ID [--apply]` reemplaza al script de despliegue y
  funciona con y sin propietario de navegador independiente (D26).
- Con todas las cuentas utilizables retenidas por cuota, el trabajo espera hasta
  la liberación más próxima (sin reclamos cada minuto) y el reporte muestra
  `pending_quota` con `resume_at`, ignorando cuentas deshabilitadas (D16).
- Sin pausa fija entre trabajos en instalaciones configuradas sólo por `.env`
  (`interval_minutes` 0 por defecto) (D23).
- La recuperación de rutas comprometidas prueba todos los candidatos configurados
  de forma consecutiva cuando la cola está vacía; uno por pase cuando otras
  cuentas tienen trabajo pendiente (D24).
- SIGTERM sigue el mismo camino que Ctrl+C; el cierre del Chrome embebido tiene
  plazo máximo y termina cualquier proceso que use los perfiles del runtime, sin
  huérfanos. El propietario independiente conserva sus navegadores (D9).
- El mensaje de servicio detenido indica el comando exacto del modo instalado (D12).
- Un HTTP 404 al descargar las páginas de una búsqueda aceptada revalida el ticket
  una vez y luego termina el trabajo como `failed` / `document_unavailable` con la
  indicación de `--force`, en lugar de pausar la cuenta y reintentar cada dos
  minutos de forma indefinida.
- Integrados los cuatro cambios del mandante: rotación por CAPTCHA rechazado en
  login y toma del lease con Chrome muerto, alias `anio`, credenciales rechazadas
  durables y recuperación del ciclo del worker (D15, D18, D19, D20, D21).

## 0.2.0 — diálogo de error del portal como proxy comprometido, 2026-09-13

- El diálogo visible `Atención / Se ha detectado un problema, refresque la
  página e intente nuevamente. / Cerrar` identifica una salida proxy
  comprometida, no un fallo de cuenta ni una caída del portal.
- Detección en tiempo real durante la búsqueda y también de forma pasiva en
  cualquier página viva, sin recargar, hacer clic ni cerrar nada para observar.
- Se elimina la recarga acotada y el reintento de la misma búsqueda en esa
  ruta: el intento termina sin reenvío y sin consumir cupo.
- La ruta queda en cuarentena de forma duradera (`compromised_since`). Mientras
  dure, la cuenta no se abre, no inicia sesión ni consulta por esa salida.
- Se cierra esa instancia de Chrome tras cerrar sesión y limpiar cookies y
  almacenamiento, y se borran sus perfiles; nada de esa salida se reutiliza.
- La recuperación adopta un puerto sticky nuevo probado con formulario
  protegido autenticado; solo esa adopción levanta la cuarentena.
- Otra cuenta autenticada atiende la misma búsqueda de inmediato. Si todas las
  cuentas están comprometidas, la búsqueda espera a la primera recuperación en
  lugar de abrir el circuito de caída global.
- Si la recuperación falla, la cuenta sigue retenida sin reloj y cada pase del
  worker reintenta según el ritmo de la ruta, sin esperas de 24 horas.
- La detección devuelve la búsqueda al pool de inmediato: la cuenta sana
  consulta primero y el reemplazo de la salida ocurre en el pase siguiente.
- El reemplazo va a un ritmo de una cuenta y un candidato por pase, como máximo
  cada 60 s y por turnos, para no bloquear al propietario único del navegador.
- Al reiniciar, el worker vuelve a publicar la retención desde la cuarentena
  duradera; una salida comprometida nunca se anuncia como disponible.
- Con el pool en cuarentena no se publica cuenta regresiva de renovación de
  cupo: lo que se espera es la recuperación, no el día siguiente.
- El panel muestra la cuarentena, su evidencia y el reemplazo en curso.

## 0.1.0 — preparación de aceptación, 2026-09-13

- API pública `Client`, `Result` y excepciones tipadas; importación sin I/O.
- `get`, `get-batch` y `status`, con caché/idempotencia, reportes CSV y estados públicos.
- Instalación editable y wheel; `CBRS_REPOSITORY` selecciona la instalación local.
- Detección de cuentas desde `.env` cuando no hay cuentas JSON explícitas.
- La cuota estimada no bloquea instalaciones nuevas con cuentas descubiertas;
  los presupuestos explícitos existentes conservan su límite.
- Recuperación de documentos de producción pendiente, conservando búsquedas
  aceptadas y PDFs ya completos. El programador experimental de endurance
  mantiene su política separada.
- `.env.example` mínimo y `INSTALL.md` para Ubuntu.
- El propietario de Chrome conserva las reglas de protección de sesiones.

Estos cambios están preparados para pruebas y no significan que A1–A20 se
hayan certificado en una instalación limpia. La matriz de aceptación contiene
las limitaciones y evidencia disponible.
