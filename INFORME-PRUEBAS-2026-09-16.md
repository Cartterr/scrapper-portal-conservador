# Informe de pruebas locales — 2026-09-15 / 2026-09-16

> Continuación con los resultados del 17 de septiembre, incluidas las
> correcciones del contratista y las de esta rama:
> [INFORME-PRUEBAS-2026-09-17.md](INFORME-PRUEBAS-2026-09-17.md).

Base probada: `origin/master` en `8692cbf` (rama local `handoff-review-fixes`,
mismo código más dos documentos de handoff). Máquina: Ubuntu nativo, sin
systemd, sin sudo. Criterios de referencia: `CRITERIOS-DE-ACEPTACION.md`.

**Resultado global: no se obtuvo ningún PDF.** Ninguna cuenta logró
autenticarse de forma automática. La causa raíz está identificada y no es de
credenciales ni de proxy: el portal rechaza el reCAPTCHA del Chrome lanzado por
Playwright. Además se encontraron nueve defectos de robustez y operación que
impiden cumplir R4, R5 y R6 aunque el login se corrija.

---

## 1. Entorno y preparación

| Ítem | Detalle |
|---|---|
| Python del sistema | 3.10 (el proyecto exige 3.11+). Se usó 3.12.4 de pyenv para crear `.venv`. |
| Instalación | `pip install -r requirements-dev.txt` y `pip install --no-deps -e .` |
| Chrome | Google Chrome estable instalado en `/usr/bin/google-chrome-stable` |
| Servicio | `cbrs jobs worker` en primer plano (modo `embedded`), sin systemd |
| Configuración | `.env` con 3 cuentas `CBRS_EJECUTIVO_1..3` y DataImpulse |
| Suite offline (A17) | 548 tests pasan, 1 falla (`tests/test_readiness.py`, test legado de WSL, anterior a esta entrega) |

## 2. Cronología de lo que pasó

1. **Primer arranque**: `cbrs status` mostró las tres cuentas en
   `login_pending` sin error durante horas. Chrome nunca se abrió.
   - Causa: el preflight de cada cuenta exige una "línea base de egreso"
     aprobada a mano. No existe en una instalación nueva y el sistema se niega a
     crearla. Este paso no está en `INSTALL.md`.
   - `INSTALL.md` y el mensaje de error citan `cbrs preflight
     --approve-egress-baseline`, que escribe el archivo en la raíz del runtime.
     El worker lo busca en `accounts/<id>/`. El comando correcto es
     `cbrs pool proxy-health --approve-egress-baseline`.
2. **Tras aprobar la línea base**: el worker siguió sin abrir Chrome durante
   más de 8 minutos.
   - Causa: si el gate de una cuenta falla al arrancar, el ciclo de
     mantenimiento nunca vuelve a intentarla. Solo un reinicio del worker la
     recupera.
3. **Tras reiniciar el worker**: Chrome se abrió para las tres cuentas y el
   login falló en las tres.
   - Cuenta 1: HTTP 401 `auth-exception`. Verificado por el mandante: la cuenta
     está dada de baja en el portal. Es correcto que falle.
   - Cuentas 2 y 3: HTTP 400 `captcha-rechazado`.
   - Las tres quedaron `paused` con motivo `credentials_invalid` y sin fecha de
     reanudación. Cero rotaciones de proxy.
4. **Con `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS=ejecutivo_2,ejecutivo_3`**:
   mismo resultado exacto. La rotación por login rechazado nunca se disparó.
5. **Con clave de 2Captcha y `CBRS_CAPTCHA_SOLVER_MODE=2captcha`**: mismo
   resultado. El saldo de 2Captcha no cambió: el solver nunca fue invocado.
6. **Rotación manual** con `cbrs jobs proxy-rotate --account ejecutivo_2`: el
   worker probó el puerto sticky 10115, hizo login y recibió otra vez
   `captcha-rechazado`. Solo probó un candidato porque el `.env` entregado trae
   `CBRS_DATAIMPULSE_CANDIDATES_PER_RECOVERY=1`.
7. **Pruebas manuales del mandante** con la cuenta 3:
   - Dentro de la ventana de Chrome del worker, contraseña correcta y dos
     incorrectas: las tres dan el mismo error. El portal rechaza antes de
     validar credenciales.
   - Firefox de la máquina, sin proxy: **login exitoso**.
   - Firefox de la máquina, saliendo por DataImpulse puerto 10001 (la misma
     salida que usa el worker): **login exitoso**.

## 3. Diagnóstico

La única variable que separa el éxito del fracaso es el navegador. Con la misma
cuenta y la misma IP de DataImpulse, Firefox entra y el Chrome lanzado por
Playwright recibe `captcha-rechazado`. reCAPTCHA Enterprise califica la huella
del cliente y el Chrome del worker se delata como automatizado.

Evidencia: el worker lanza Chrome con `launch_persistent_context` y argumentos
vacíos, es decir, con todos los flags por defecto de Playwright
(`--disable-extensions`, `--disable-background-networking`,
`--metrics-recording-only`, `--remote-debugging-pipe`, `--no-first-run`,
`--password-store=basic`, `--use-mock-keychain`, lista larga de features
desactivadas) sobre un perfil nuevo sin historial. No hay `ignore_default_args`
ni ajuste de huella.

Esto también explica que al contratista "a veces le funcione": el puntaje es
probabilístico, y sus éxitos documentados provienen de sesiones recuperadas o
perfiles con historial, no de logins automáticos en instalación limpia.

Descartado por las pruebas: credenciales de las cuentas 2 y 3, IP de salida de
DataImpulse, límite diario, bloqueo de cuenta.

## 4. Defectos encontrados

Numerados para referencia. Cada uno indica el criterio que incumple.

| # | Defecto | Criterio |
|---|---|---|
| D1 | Instalación limpia con solo credenciales no arranca: exige aprobar a mano la línea base de egreso. No documentado. | R6 |
| D2 | `INSTALL.md` y el mensaje de error citan `cbrs preflight --approve-egress-baseline`, que escribe en una ruta que el worker no lee. | R6 |
| D3 | `cbrs status` oculta el motivo real. Mostró `login_pending` sin error mientras el preflight fallaba, y luego `credentials_invalid` cuando el motivo era captcha. | R4 |
| D4 | Un gate fallido al arranque deja la cuenta muerta hasta reiniciar el worker. | R4 |
| D5 | `captcha-rechazado` no está en el clasificador de respuestas: se trata como credenciales inválidas y pausa la cuenta sin reanudación ni rotación. | R5 |
| D6 | La rotación por login rechazado (`CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS`) nunca se dispara para ese caso: la cuenta pausada queda excluida antes de evaluarla. | R5 |
| D7 | Cuenta dada de baja (401) se trata igual que captcha rechazado. Debe marcarse `disabled` con el mensaje del portal, excluirse del pool y continuar con las demás. | R5 |
| D8 | El solver externo de captcha (2Captcha/CapSolver) no participa en el login por formulario. La clave configurada es inerte. | R5 |
| D9 | Ctrl+C no detiene el worker con Chrome abierto: más de 90 s sin salir. SIGTERM lo mata en 1 s pero deja 24 procesos Chrome huérfanos. | R4 |
| D10 | Tras una muerte abrupta, el lease y el "pool run" bloquean el relanzamiento 2 minutos ("Another CBRS job worker has an active lease"). En una ocasión quedaron **dos workers corriendo a la vez**. | R4 |
| D11 | El `.env` entregado limita la recuperación a 1 candidato por intento y 3 rotaciones por hora. Con esos valores R5 es incumplible. | R5 |
| D12 | El mensaje de "servicio caído" dice `cbrs service start worker` (systemd), que no aplica al flujo `cbrs serve` / `jobs worker`. | R4 |
| D13 | Logs: el worker no escribe log de aplicación a stdout ni a archivo. Todo el diagnóstico hubo que hacerlo leyendo SQLite. | R4 |
| D14 | Huella del navegador: Chrome lanzado con los defaults de Playwright es rechazado por reCAPTCHA Enterprise en el login. Causa raíz del bloqueo total. | R5 / todo |

## 5. Lo que sí funciona

- `pip install -e .` y el comando `cbrs` con `get`, `get-batch` y `status`.
- Detección de N cuentas desde `.env`.
- `cbrs config validate` y `cbrs pool proxy-health`: proxies DataImpulse con
  salida en Chile, reCAPTCHA y portal alcanzables.
- Con el servicio detenido, `cbrs get` devuelve código 2 (A6), aunque con el
  mensaje equivocado (D12).
- La rotación manual de proxy funciona mecánicamente: pide puerto sticky nuevo,
  abre Chrome y prueba el login.
- `cbrs captcha-health` autentica con 2Captcha y muestra saldo.

## 6. Lo que no se pudo probar

Por no existir ninguna sesión autenticada, quedan sin ejecutar: A2, A3, A4, A7,
A8, A9, A10, A11, A12, A13, A15, A16, A19, A20. Es decir, toda la funcionalidad
de descarga y cuota. A1 falla en el primer paso. A14 falla (D9).

## 7. Lo que tiene que arreglar el contratista

En orden de prioridad:

1. **Login automático en instalación limpia** (D14). Con perfil nuevo, cuentas
   y proxy entregados por el mandante, las N cuentas deben autenticarse solas.
   La política "Chrome normal con Playwright sin ajustes" no lo logra hoy. Cómo
   resolverlo es decisión técnica del contratista; el criterio no cambia.
2. **Clasificación y recuperación de rechazos** (D5, D6, D7, D8, D11). Captcha
   rechazado debe rotar proxy y reintentar mientras haya cuota. Cuenta dada de
   baja debe excluirse y seguir con las demás. Los valores por defecto deben
   permitir seguir probando.
3. **Instalación sin pasos ocultos** (D1, D2). La línea base de egreso se crea
   sola o desaparece en modo `mobile_sticky`.
4. **Visibilidad** (D3, D12, D13). `cbrs status` con el motivo real; logs a
   stdout y archivo; mensajes que citen el comando correcto.
5. **Ciclo de vida del worker** (D4, D9, D10). Reintento de cuentas tras gate
   fallido; salida limpia en menos de 30 s con Ctrl+C sin Chrome huérfanos; un
   relanzamiento inmediato tras una muerte; una sola instancia siempre.

## 8. Prueba nueva propuesta para los criterios

**A0. Login automático en limpio.** En Ubuntu limpio, con `.env` recién
completado y sin perfiles previos, lanzar el servicio. Antes de 10 minutos y sin
ninguna intervención, `cbrs status` debe mostrar todas las cuentas válidas como
`available` con sesión autenticada. Una cuenta dada de baja debe aparecer como
`disabled` con el mensaje del portal, sin afectar a las demás. Es prerrequisito
de A1 a A20.

## 9. Estado de la máquina al cierre

- `.env`: agregados `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS=ejecutivo_2,ejecutivo_3`,
  `CBRS_CAPTCHA_SOLVER_MODE=2captcha` y `CBRS_2CAPTCHA_API_KEY` (clave del
  proyecto titularia). Ninguno de los tres cambió el resultado.
- Líneas base de egreso creadas en `.cbrs/runtime/accounts/ejecutivo_{1,2,3}/`
  y una global inútil en `.cbrs/runtime/fixed-egress-baseline.json`.
- Cuenta 2 con ruta en estado `candidate_failed` tras la rotación manual.
- Worker posiblemente aún corriendo con tres ventanas de Chrome. Para detenerlo:
  `kill -TERM $(pgrep -f "[p]ython .venv/bin/cbrs jobs worker"); sleep 3; pkill -x chrome`
- Nada de esto está commiteado. `.env` y `.cbrs/` están fuera de git.
