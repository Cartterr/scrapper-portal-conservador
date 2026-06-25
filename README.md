# CBRS Portal Conservador

Automatización controlada para consultar el Índice del Registro de Comercio del
portal CBRS, validar el flujo real con una sesión persistente y guardar PDFs para
revisión local.

## Quickstart

```powershell
cd V:\scrapper\scrapper-portal-conservador
python -m pip install -r requirements.txt
```

Configura `.env` con el egreso autorizado. Para producción usa un egreso del
cliente o dedicado:

```dotenv
CBRS_EGRESS_MODE=client_office
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_REQUEST_DELAY_SECONDS=5.0
CBRS_PROFILE_DIR=.cbrs/chrome-profile
CBRS_OUTPUT_DIR=outputs
```

Si vas a usar un proveedor de IP estática ISP chilena dedicada, no guardes el
proxy en el repo. Decláralo solo en `.env` y usa el modo dedicado:

```dotenv
CBRS_EGRESS_MODE=dedicated_static_isp
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_PROXY_URL=http://usuario:password@host:puerto
```

El valor real de `CBRS_PROXY_URL` se valida por preflight, pero los reportes
solo guardan esquema, puerto y hash del host. No se guardan usuario, password,
IP cruda ni URL completa.

Para una prueba local explícita desde tu conexión actual:

```dotenv
CBRS_EGRESS_MODE=personal_direct
CBRS_ALLOW_PERSONAL_EGRESS=1
CBRS_EXPECTED_EGRESS_COUNTRY=CL
```

Inicializa y valida el entorno:

```powershell
python -m cbrs doctor
python -m cbrs preflight --approve-egress-baseline
python -m cbrs init --timeout 600
```

Flujo normal, parecido a los scripts originales:

```powershell
python -m cbrs search --query "BANCO DE CHILE"
python -m cbrs download --query "BANCO DE CHILE" --output outputs

python -m cbrs search --foja 9441 --numero 4580 --ano 1980
python -m cbrs download --foja 9441 --numero 4580 --ano 1980 --output outputs
```

El comando `download` muestra los resultados y pide seleccionar `1,3` o `all`,
igual que el flujo original. También se mantiene el alias legado
`--no-headless`; el flag antiguo `--use-proxy` existe solo para fallar con un
mensaje claro porque el runtime productivo usa egreso fijo, no proxy rotativo.

Validación y monitor local:

```powershell
python -m cbrs validate --query "BANCO DE CHILE" --download-first
python -m cbrs soak dashboard
python -m cbrs soak run --dashboard
python -m cbrs soak stop
```

Pool autorizado de 3 cuentas:

```powershell
python -m cbrs pool init --account ejecutivo_1 --timeout 600
python -m cbrs pool init --account ejecutivo_2 --timeout 600
python -m cbrs pool init --account ejecutivo_3 --timeout 600

python -m cbrs pool dashboard
python -m cbrs pool run --dashboard
python -m cbrs pool stop
```

El pool usa `.cbrs/account-pool.json` si existe, pero por defecto crea tres
labels locales: `ejecutivo_1`, `ejecutivo_2`, `ejecutivo_3`. No pongas emails,
RUTs, passwords ni tokens en ese archivo; cada cuenta se inicia con login manual
y perfil persistente propio.

Para asignar un proxy fijo diferente por cuenta, guarda solo el nombre de la
variable de entorno en `.cbrs/account-pool.json`:

```json
{
  "accounts": [
    {
      "id": "ejecutivo_1",
      "label": "Ejecutivo 1",
      "proxy_url_env": "CBRS_EJECUTIVO_1_PROXY_URL"
    }
  ]
}
```

Luego define `CBRS_EJECUTIVO_1_PROXY_URL` fuera de git. El dashboard y los logs
siguen mostrando solo labels sanitizados.

## Stack

- Python 3.14.
- Playwright con Chrome/Edge instalado en la máquina.
- Perfil persistente local en `.cbrs/chrome-profile`.
- `Pillow` para ensamblar imágenes en PDF.
- `pytest` para pruebas automatizadas.
- SQLite + `http.server` para el monitor local de prueba continua.
- Runtime de pool multi-cuenta con SQLite local y perfiles aislados por cuenta.

## Qué Hace

- Abre una sesión manual en el portal CBRS con `python -m cbrs init`.
- Ejecuta búsquedas por razón social o por foja/número/año.
- Descarga el primer resultado o los documentos indicados y genera PDFs.
- Guarda PDFs en `outputs/`.
- Puede repartir ciclos entre tres cuentas autorizadas, con cupo teórico de
  20 consultas por cuenta y 60 consultas diarias totales.
- Guarda PDFs del pool en `outputs/pool/<run>/<cuenta>/<cycle>/`.
- Genera reportes sanitizados de validación en `.cbrs/logs/`.
- Incluye un monitor local de prueba continua en `http://127.0.0.1:8765`.
- Incluye dashboard del pool con barra de consultas disponibles, estado por
  ejecutivo, siguiente ciclo y cuentas pausadas.
- Detiene el flujo ante señales de seguridad como límite diario, CAPTCHA,
  errores WAF, drift de egreso o falta de sesión.

## Mejoras Sobre los Scripts Originales

- Se reemplazó el flujo basado en credenciales y rotación por login manual con
  perfil persistente.
- Se dejó de guardar sesiones crudas tipo `.cbrs_session.json`.
- Se agregó preflight de egreso, país esperado y hash sanitizado.
- Se agregó soporte para egreso estático ISP dedicado por `CBRS_PROXY_URL`,
  con metadata sanitizada y bloqueo si se usa fuera del modo dedicado.
- Se incorporaron paradas duras ante `403`, `429`, `err-limite`,
  `intente-mas-tarde`, CAPTCHA o HTML de desafío.
- Se organizaron outputs en `outputs/` y `outputs/soak/<run>/<cycle>/`.
- Se agregaron reportes JSON sanitizados para auditoría.
- Se implementó `doctor`, `preflight`, `validate` y el grupo `soak`.
- Se implementó `pool` para operar cuentas nominales autorizadas con perfiles
  separados y cupo diario por cuenta.
- Se agregó dashboard local en español con estado vivo, countdown, PDFs,
  ciclos, eventos y alertas críticas.
- Se añadió cobertura de pruebas para configuración, seguridad, preflight,
  validación, PDFs, runtime de navegador, soak y pool multi-cuenta.

## Caveats

- El portal impone límites diarios de consulta; cuando responde `err-limite`, el
  sistema se pausa y no sigue consultando.
- El login es manual; no se guardan credenciales ni se automatiza el ingreso.
- La confiabilidad depende de mantener el mismo perfil de navegador y un egreso
  estable/autorizado.
- `CBRS_PROXY_URL` solo es válido para egresos estáticos/dedicados; no está
  pensado para rotación, pools residenciales variables ni fallback automático.
- El monitor local no aumenta tráfico por sí solo, pero el runner de soak sí
  ejecuta ciclos reales cuando está activo.
- El pool no es rotación evasiva: solo debe usarse con cuentas nominales
  autorizadas por el cliente/CBRS, y una cuenta pausada no se fuerza ni se
  reintenta agresivamente.
- No hay rotación de IP, resolución externa de CAPTCHA ni reintentos agresivos.

## Áreas a Explorar

- Confirmar con CBRS o el cliente un modelo oficial de acceso, cuota o
  allowlisting para uso productivo.
- Definir una cuota diaria operacional segura según contrato o autorización del
  portal.
- Confirmar si las 60 consultas teóricas del pool deben quedar como límite duro
  o si conviene usar un margen operacional más conservador.
- Mejorar captura visual automática del portal cuando ocurra un safety stop.
- Evaluar egreso dedicado/cliente si la red actual no es el ambiente final.
- Separar un modo de prueba completamente offline con fixtures para demos sin
  tocar el portal real.

