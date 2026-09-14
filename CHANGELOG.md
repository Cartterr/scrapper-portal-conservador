# Cambios

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
