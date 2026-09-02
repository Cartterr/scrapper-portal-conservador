# Plataforma de Consulta Documental CBRS

> Solución controlada para consultar el Índice del Registro de Comercio del
> Conservador de Bienes Raíces de Santiago (CBRS), organizar resultados y generar
> documentos PDF con trazabilidad operacional.

![Python](https://img.shields.io/badge/Python-3.14-2563EB?style=flat-square&logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Chrome%20persistente-16A34A?style=flat-square&logo=googlechrome&logoColor=white)
![Operación](https://img.shields.io/badge/Operación-Controlada-7C3AED?style=flat-square)
![Seguridad](https://img.shields.io/badge/Seguridad-Detención%20preventiva-DC2626?style=flat-square)

## Resumen ejecutivo

Antes de instalar, revisar [los prerrequisitos nativos](docs/native-windows-prerequisites.md). Ese archivo
define los requisitos del equipo, autorizaciones, cuentas, proxies, respaldo y
gates que tambien debera aplicar el instalador E2E de Windows.

### Instalación E2E en Windows

La ruta activa es Windows nativo; WSL queda únicamente como referencia legacy.
Consultar [el runbook nativo](docs/native-windows-endurance.md). Desde PowerShell
elevado:

```powershell
.\deploy\windows\Install-CbrsNative.ps1
```

El instalador nativo:

1. instala/reutiliza Python, Chrome y restic nativos;
2. conserva el estado durable bajo `G:\CBRS`;
3. prepara el repositorio restic bajo `E:\CBRS-backup\restic`;
4. restringe `C:\ProgramData\CBRS\cbrs.env` al usuario y `SYSTEM`;
5. registra tareas reiniciables para worker, dashboard y backup diario;
6. deja las tareas deshabilitadas hasta superar readiness y el gate de tráfico.

La instalación no aprueba egresos ni inicia el worker sin confirmaciones
separadas. El arranque requiere explícitamente
`-AcknowledgeAuthorizedLiveTraffic`.

### Recuperación de sesiones persistentes

La guía cliente para recuperar las tres cuentas sin cruzar identidad, perfil ni
proxy está en
[`docs/persistent-account-session-recovery.md`](docs/persistent-account-session-recovery.md).
Incluye el diagnóstico verificado del 31-08-2026, la relación uno a uno entre
cuenta/perfil/ruta, el refresh acotado de cookies, los campos de estado en tiempo
real y las limitaciones del keeper usado para validar la recuperación sin tocar
la cola. **Disponible** expresa elegibilidad; solo **Sesión saludable** confirma
que existe un Chrome vivo y autenticado bajo el lease vigente.

### DataImpulse: egreso recomendado

El runtime de producción usa **DataImpulse Residential Proxy** como proveedor
de egreso: tres rutas sticky de Chile, un puerto distinto por cuenta y una
duración de sesión de 120 minutos. Las URLs autenticadas se construyen solo en
memoria desde `DATAIMPULSE_PROXY_LOGIN` y `DATAIMPULSE_PROXY_PASSWORD`; nunca se
guardan en el pool ni se muestran en el dashboard. `DATAIMPULSE_EMAIL` y
`DATAIMPULSE_PASSWORD`, si se configuran, son credenciales administrativas del
panel y no credenciales proxy ni una API de rotación.

La rotación normal se controla mediante los puertos sticky documentados por el
proveedor. Ante una falla confirmada se prueba otro puerto, se exige egreso
chileno, acceso a CBRS/reCAPTCHA y unicidad, y solo entonces se promueve la ruta
para esa cuenta. 2Captcha y CapSolver permanecen como solvers CAPTCHA; no son el
proxy principal. El procedimiento completo está en
[`docs/dataimpulse-cbrs-operations.md`](docs/dataimpulse-cbrs-operations.md).

Esta plataforma facilita consultas documentales autorizadas en el portal CBRS y
convierte las imágenes obtenidas en archivos PDF ordenados para revisión local.
La operación combina sesiones de navegador persistentes, validaciones previas,
ritmo controlado, monitoreo local y paradas automáticas ante señales de riesgo.

El sistema está diseñado para operar cuentas expresamente autorizadas sin
sustituir los controles del portal. Las sesiones se renuevan o autentican dentro
del navegador persistente usando secretos referenciados por variables de
entorno. Por defecto CAPTCHA sigue siendo una intervención visual; opcionalmente
se puede habilitar un solve 2Captcha manual de un solo uso. No se almacenan secretos en
Git, no se rotan identidades y no se realizan reintentos agresivos.

| Valor para la operación | Cómo se materializa |
|---|---|
| **Automatización controlada** | Estandariza la búsqueda y descarga sin perder supervisión humana. |
| **Trazabilidad operacional** | Registra ciclos, estados y evidencia sanitizada para facilitar seguimiento y auditoría. |
| **Protección de datos** | Mantiene perfiles, credenciales, resultados y configuración sensible fuera de Git. |
| **Seguridad preventiva** | Detiene la actividad ante límites, CAPTCHA, WAF, sesión inválida o cambios de egreso. |

## Cómo funciona

El operador prepara cuentas, proxies y baselines una vez. Poderalia encola una
solicitud y el worker elige una cuenta con cupo, asegura la sesión, descarga todas
las inscripciones y conserva cada PDF.

```mermaid
flowchart LR
    A["👤 Operador<br/>autorizado"] --> B["🩺 Doctor y<br/>preflight"]
    B --> C["🔐 Refresh o login<br/>browser-origin"]
    C --> D["🔎 Consulta<br/>CBRS"]
    D --> E["📋 Selección de<br/>resultados"]
    E --> F["📄 Generación<br/>de PDF"]
    F --> G["🛡️ Reporte<br/>sanitizado"]

    classDef actor fill:#DBEAFE,stroke:#2563EB,color:#1E3A8A,stroke-width:2px;
    classDef control fill:#EDE9FE,stroke:#7C3AED,color:#4C1D95,stroke-width:2px;
    classDef operation fill:#CFFAFE,stroke:#0891B2,color:#164E63,stroke-width:2px;
    classDef result fill:#DCFCE7,stroke:#16A34A,color:#14532D,stroke-width:2px;
    classDef audit fill:#FEF3C7,stroke:#D97706,color:#78350F,stroke-width:2px;

    class A actor;
    class B,C control;
    class D,E operation;
    class F result;
    class G audit;
```

## Arquitectura de la solución

La solución se ejecuta localmente. El navegador conserva la sesión autorizada,
mientras que los controles de seguridad validan el entorno antes de cada flujo.
Los documentos y datos operacionales permanecen en carpetas locales ignoradas
por Git.

```mermaid
flowchart LR
    subgraph UX["Experiencia del operador"]
        direction TB
        CLI["⌨️ Comandos CBRS"]
        DASH["📊 Dashboards locales"]
    end

    subgraph CORE["Núcleo de control"]
        direction TB
        CFG["⚙️ Configuración segura"]
        PREF["✅ Doctor y preflight"]
        SCHED["🗓️ Scheduler y cupos"]
        SAFE["🛑 Motor de seguridad"]
    end

    subgraph ACCESS["Acceso autorizado"]
        direction TB
        PROFILES["🌐 Perfiles Chrome<br/>persistentes y aislados"]
        PORTAL["🏛️ Portal CBRS"]
    end

    subgraph OUTPUTS["Resultados locales"]
        direction TB
        PDF["📄 Documentos PDF"]
        DB["🗃️ Estado operacional<br/>en SQLite"]
        REPORTS["🛡️ Reportes<br/>sanitizados"]
    end

    CLI --> CFG --> PREF
    CLI --> SCHED
    DASH -->|"API y control local"| DB
    PREF --> SAFE
    SCHED --> SAFE
    SAFE --> PROFILES
    PROFILES --> PORTAL
    PORTAL --> PDF
    PORTAL --> DB
    PORTAL --> REPORTS

    classDef interface fill:#DBEAFE,stroke:#2563EB,color:#1E3A8A,stroke-width:2px;
    classDef control fill:#EDE9FE,stroke:#7C3AED,color:#4C1D95,stroke-width:2px;
    classDef access fill:#CFFAFE,stroke:#0891B2,color:#164E63,stroke-width:2px;
    classDef result fill:#DCFCE7,stroke:#16A34A,color:#14532D,stroke-width:2px;
    classDef guard fill:#FEE2E2,stroke:#DC2626,color:#7F1D1D,stroke-width:2px;

    class CLI,DASH interface;
    class CFG,PREF,SCHED control;
    class SAFE guard;
    class PROFILES,PORTAL access;
    class PDF,DB,REPORTS result;

    style UX fill:#F8FAFC,stroke:#93C5FD,color:#0F172A,stroke-width:1px;
    style CORE fill:#FAF5FF,stroke:#C4B5FD,color:#0F172A,stroke-width:1px;
    style ACCESS fill:#ECFEFF,stroke:#67E8F9,color:#0F172A,stroke-width:1px;
    style OUTPUTS fill:#F0FDF4,stroke:#86EFAC,color:#0F172A,stroke-width:1px;
```

### Componentes principales

- **Interfaz de comandos y API loopback:** concentra preparación, cola durable,
  consultas, descargas, validación y operación de largo plazo.
- **Preflight de egreso:** confirma navegador, país, modalidad de conexión y
  estabilidad del egreso antes de acceder al portal.
- **Navegador persistente:** conserva y renueva la sesión; si es necesario,
  autentica automáticamente con secretos mantenidos fuera de SQLite y Git.
- **Motor documental:** descarga las imágenes seleccionadas y las ensambla en
  PDF mediante Pillow.
- **Monitoreo local:** utiliza SQLite y un servidor HTTP local para mostrar
  estado, ciclos, cupos, alertas y documentos generados.
- **Capa de seguridad:** clasifica respuestas de riesgo, sanitiza reportes y
  detiene la operación cuando corresponde.

## Seguridad preventiva

La política operacional es detenerse y conservar evidencia cuando el entorno o
la respuesta del portal no son seguros. La plataforma no intenta superar el
bloqueo cambiando de identidad o aumentando la frecuencia.

```mermaid
flowchart TD
    A["▶️ Solicitud controlada"] --> B["🔍 Validaciones previas"]
    B --> C{"¿Entorno seguro?"}
    C -- "Sí" --> D["🌐 Consulta o descarga"]
    C -- "No" --> H["🛑 Pausa segura"]
    D --> E{"¿Respuesta normal?"}
    E -- "Sí" --> F["✅ Resultado procesado"]
    E -- "No" --> G["⚠️ Límite, sesión inválida,<br/>CAPTCHA, WAF o respuesta inesperada"]
    G --> H
    H --> I["🧾 Registro de evidencia"]
    I --> J["👤 Revisión manual"]

    classDef normal fill:#DBEAFE,stroke:#2563EB,color:#1E3A8A,stroke-width:2px;
    classDef decision fill:#FEF3C7,stroke:#D97706,color:#78350F,stroke-width:2px;
    classDef success fill:#DCFCE7,stroke:#16A34A,color:#14532D,stroke-width:2px;
    classDef warning fill:#FFEDD5,stroke:#EA580C,color:#7C2D12,stroke-width:2px;
    classDef stop fill:#FEE2E2,stroke:#DC2626,color:#7F1D1D,stroke-width:2px;

    class A,B,D normal;
    class C,E decision;
    class F success;
    class G warning;
    class H,I,J stop;
```

Las detenciones cubren, entre otras señales:

- cambio de egreso o falla del preflight;
- sesión ausente o inválida;
- respuestas HTTP `401`, `403` o `429`;
- límites diarios o mensajes para intentar más tarde;
- CAPTCHA pendiente o desafío WAF/Imperva;
- HTML inesperado en endpoints de datos o imágenes;
- estados diferentes de los esperados por el flujo.

## Operación con cuentas autorizadas

El pool permite distribuir ciclos entre tres cuentas nominales autorizadas. Cada
cuenta conserva su propio perfil de Chrome, sesión y ruta de egreso fija o sticky.
El scheduler respeta el cupo configurado y excluye temporalmente una cuenta si
requiere revisión o agotó el único intento automático de CAPTCHA configurado.

```mermaid
flowchart LR
    S["🗓️ Scheduler<br/>controlado"]

    S --> A1["👤 Ejecutivo 1"]
    S --> A2["👤 Ejecutivo 2"]
    S --> A3["👤 Ejecutivo 3"]

    A1 --> P1["🌐 Perfil Chrome 1<br/>+ ruta chilena 1"]
    A2 --> P2["🌐 Perfil Chrome 2<br/>+ ruta chilena 2"]
    A3 --> P3["🌐 Perfil Chrome 3<br/>+ ruta chilena 3"]

    P1 --> C["🏛️ Portal CBRS"]
    P2 --> C
    P3 --> C

    C --> Q["📊 Cupos, estado<br/>y evidencia local"]
    Q -. "retroalimentación" .-> S

    classDef scheduler fill:#EDE9FE,stroke:#7C3AED,color:#4C1D95,stroke-width:2px;
    classDef account fill:#DBEAFE,stroke:#2563EB,color:#1E3A8A,stroke-width:2px;
    classDef profile fill:#CFFAFE,stroke:#0891B2,color:#164E63,stroke-width:2px;
    classDef portal fill:#FEF3C7,stroke:#D97706,color:#78350F,stroke-width:2px;
    classDef evidence fill:#DCFCE7,stroke:#16A34A,color:#14532D,stroke-width:2px;

    class S scheduler;
    class A1,A2,A3 account;
    class P1,P2,P3 profile;
    class C portal;
    class Q evidence;
```

> **Principio operacional:** este modelo aísla sesiones y administra cuentas
> expresamente autorizadas. No constituye rotación evasiva de identidades, no
> fuerza cuentas pausadas y no intenta eludir límites del portal.

Por defecto, el pool define `ejecutivo_1`, `ejecutivo_2` y `ejecutivo_3`, con un
cupo teórico de 20 consultas diarias por cuenta y 60 totales. El cupo final debe
alinearse con la autorización o contrato aplicable.

## Capacidades

- Búsqueda por razón social.
- Búsqueda por foja, número y año.
- Selección interactiva de uno o varios resultados.
- Descarga y ensamblaje local de imágenes en PDF.
- Validación controlada de bajo volumen con evidencia sanitizada.
- Monitoreo de largo plazo con intervalos configurables y detención segura.
- Pool de tres cuentas autorizadas con perfiles y cupos separados.
- Cola SQLite idempotente, recuperación tras reinicios y un worker secuencial.
- API JSON loopback para altas, estado, cancelación y entrega de artefactos.
- Descarga automática de todas las coincidencias y estado parcial por inscripción.
- Respaldo cifrado permanente mediante snapshots SQLite y restic.
- Comprobación previa del proxy: egreso chileno, carga de reCAPTCHA Enterprise y
  disponibilidad inicial del portal.
- Dashboard local en español para estado, ciclos, cupos, PDFs y alertas.
- Controles locales para administrar cuentas, crear solicitudes por empresa o
  documento, usar ejemplos comprobados y previsualizar PDFs generados.
- Cobertura automatizada para configuración, navegador, seguridad, PDF,
  preflight, validación, soak y pool.

## Tecnología

| Componente | Uso |
|---|---|
| **Python 3.14** | Aplicación, orquestación y comandos. |
| **Playwright + Google Chrome** | Perfiles persistentes y aislados en Windows nativo. |
| **Pillow** | Ensamblaje de imágenes en documentos PDF. |
| **SQLite** | Estado local del monitoreo y del pool. |
| **`http.server`** | Dashboard y API JSON exclusivamente en loopback. |
| **pytest** | Pruebas automatizadas de regresión y seguridad. |

## Límite del entorno soportado

La prueba endurance usa exclusivamente **Windows nativo**: Python 3.14, Chrome,
Playwright, SQLite, restic y Windows Scheduled Tasks. No se debe instalar ni
ejecutar esta ruta mediante WSL, Docker, Xvfb o una máquina virtual. Los assets
Ubuntu/WSL permanecen en el repositorio solo como material legacy y no forman
parte del runbook activo.

### Operación headless persistente

El worker programado ejecuta tres Chrome reales en modo headless
(`CBRS_HEADLESS=1`). Los conserva durante periodos idle, cooldowns y fallas
funcionales del portal mientras las tareas del runtime continúen activas. Para
recuperación visual manual, primero se pausa endurance y se detiene el worker.
El worker y la recuperación nunca comparten un perfil simultáneamente.

## Inicio rápido

### 1. Instalar dependencias

Abre PowerShell nativo como administrador y ejecuta:

```powershell
.\deploy\windows\Install-CbrsNative.ps1 -InstallDevelopmentRequirements
.\.venv\Scripts\python.exe -m pytest -q
```

El procedimiento completo y el gate de aceptación están en
[Endurance E2E nativo en Windows](docs/native-windows-endurance.md).

### 2. Configurar el egreso autorizado

Parte desde `.env.example`; el ejemplo solo contiene placeholders. Para
producción, los secretos viven en `C:\ProgramData\CBRS\cbrs.env`:

```dotenv
CBRS_EGRESS_MODE=residential_sticky
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_HEADLESS=1
CBRS_WINDOW_MODE=normal
DATAIMPULSE_PROXY_HOST=gw.dataimpulse.com
DATAIMPULSE_COUNTRY=cl
DATAIMPULSE_STICKY_TTL_MINUTES=120
CBRS_REQUEST_DELAY_SECONDS=5.0
CBRS_PROFILE_DIR=.cbrs/chrome-profile
CBRS_OUTPUT_DIR=outputs
```

Para un proveedor de IP estática ISP chilena dedicada, declara la URL solamente
en `.env`; nunca la agregues al repositorio:

```dotenv
CBRS_EGRESS_MODE=dedicated_static_isp
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_PROXY_URL=http://usuario:password@host:puerto
```

Los reportes guardan únicamente esquema, puerto y hash del host. No almacenan
usuario, contraseña, IP cruda ni URL completa.

Para una prueba local explícita desde una conexión personal:

```dotenv
CBRS_EGRESS_MODE=personal_direct
CBRS_ALLOW_PERSONAL_EGRESS=1
CBRS_EXPECTED_EGRESS_COUNTRY=CL
```

Este modo es solo para pruebas puntuales y no se considera un entorno productivo.

### 3. Validar el entorno

```bash
python -m cbrs doctor
python -m cbrs preflight --approve-egress-baseline
python -m cbrs pool proxy-health --approve-egress-baseline
```

El worker intenta primero el refresh persistente y después el login automático
browser-origin. `init` y `pool init` permanecen como herramientas de diagnóstico
manual; no forman parte de la preparación diaria.

## Cola de producción

```bash
python -m cbrs jobs enqueue --text "EMPRESA AUTORIZADA" --idempotency-key req-123
python -m cbrs jobs enqueue --foja 9441 --numero 4580 --year 1980
python -m cbrs jobs worker
python -m cbrs jobs dashboard
python -m cbrs jobs show JOB_ID
```

El **Centro de control** del dashboard ofrece esas mismas rutas sin requerir la
CLI: se puede buscar **Por empresa** o **Por documento** (`foja`, `número` y
`año`, equivalente al flujo histórico `download --foja --numero --ano`). Para
cualquiera de los dos tipos se puede elegir **Agregar a cola** o **Buscar y
descargar ahora**. La segunda opción se marca como prioritaria y, si el worker
está detenido, solicita su inicio seguro. Sigue respetando un único
worker, cupos, pacing, preflight y safety stops; no salta los gates de CBRS.
Si CBRS devuelve más de una inscripción válida, se descarga un PDF
permanente por cada resultado.

El dashboard/API escucha exclusivamente en `127.0.0.1:8765`; no admite un bind
privado durante la operación nativa. Las búsquedas publican PDFs permanentes en
`G:\CBRS\outputs\jobs\<job_id>`. Las tareas `CBRS Worker`, `CBRS Dashboard` y
`CBRS Daily Backup` recuperan la operación después del siguiente inicio de
sesión del mismo usuario.

`Start-CbrsNative.ps1` ejecuta además un gate operacional post-arranque. No
declara éxito hasta comprobar tareas habilitadas, worker y dashboard corriendo,
heartbeat vivo y `/api/health`; ante un fallo detiene sólo los procesos que el
arranque actual creó y restaura el estado habilitado/deshabilitado previo.

### Controles de operación en el dashboard

En la parte superior del dashboard, **Crear solicitud** reúne los controles que
antes requerían la CLI: elegir **Por empresa** o **Por documento** (`foja`,
`número`, `año`) y luego elegir **Agregar a cola** o **Buscar y descargar ahora**.
La acción inmediata se prioriza y puede arrancar el worker de forma segura; por
ello puede iniciar tráfico real, aunque nunca evita cupos, pacing, autenticación,
preflight o safety stops. El botón pequeño **Ej.** carga solamente coordenadas
que ya generaron PDFs correctos en el equipo. En **Solicitudes recientes**,
**Ver PDF** abre el artefacto ya descargado dentro de un modal local. La
recuperación visual solo se ofrece desde una cuenta con CAPTCHA pendiente.

## Consultas y documentos

### Búsqueda por razón social

```bash
python -m cbrs search --query "BANCO DE CHILE"
python -m cbrs download --query "BANCO DE CHILE" --output outputs
```

### Búsqueda por inscripción

```bash
python -m cbrs search --foja 9441 --numero 4580 --ano 1980
python -m cbrs download --foja 9441 --numero 4580 --ano 1980 --output outputs
```

`download` muestra los resultados y permite seleccionar valores como `1,3` o
`all`. El alias legado `--no-headless` se conserva. El flag antiguo
`--use-proxy` falla de forma explícita porque el runtime productivo requiere un
egreso fijo declarado, no rotación automática.

## Validación y monitoreo de largo plazo

Los comandos `soak` siguientes son diagnósticos legacy. La prueba indefinida
activa usa `jobs worker` más el controlador `jobs endurance`.

Ejecuta una validación real de bajo volumen:

```bash
python -m cbrs validate --query "BANCO DE CHILE" --download-first
```

Abre el dashboard sin iniciar tráfico hacia el portal:

```bash
python -m cbrs soak dashboard
```

Ejecuta una prueba completamente local:

```bash
python -m cbrs soak run --dry-run --max-cycles 3 --dashboard
```

Ejecuta o detén el monitoreo real:

```bash
python -m cbrs soak run --dashboard
python -m cbrs soak stop
```

El dashboard de soak utiliza por defecto
[`http://127.0.0.1:8765`](http://127.0.0.1:8765). El runner real ejecuta ciclos
contra CBRS; el dashboard por sí solo es de solo lectura.

Para comprobar la preparación E2E nativa sin iniciar Chrome ni crear un CAPTCHA:

```powershell
.\.venv\Scripts\python.exe -m cbrs readiness `
  --target windows `
  --env-file C:\ProgramData\CBRS\cbrs.env `
  --config G:\CBRS\account-pool.json `
  --json-report G:\CBRS\readiness\indefinite-test.json
```

El procedimiento escalonado, los controles de arranque/parada y los criterios
de observación están en
[`docs/native-windows-endurance.md`](docs/native-windows-endurance.md).

## Pool de cuentas autorizadas

### Comprobar las rutas antes del login

```bash
python -m cbrs pool proxy-health
python -m cbrs pool proxy-health --account ejecutivo_1
python -m cbrs pool proxy-health --account ejecutivo_1 --replace-egress-baseline
```

Este gate verifica país `CL`, carga de Google reCAPTCHA Enterprise y respuesta
inicial del portal CBRS. Si falla, `pool init`, `pool login-debug` y los ciclos
reales no deben abrir el flujo del portal.

El reemplazo de baseline sólo se acepta con endurance pausado y sin lease del
worker. Exige egreso chileno, CBRS y reCAPTCHA accesibles, y una salida distinta
de las otras cuentas; archiva el baseline anterior saneado antes del cambio
atómico.

La integración opcional de 2Captcha y el procedimiento de prueba larga están en
[`docs/2captcha-long-run.md`](docs/2captcha-long-run.md). El proxy del navegador
y el solver v3 son rutas separadas: 2Captcha documenta reCAPTCHA Enterprise v3
únicamente como tarea `proxyless`.
La decisión de usar tres sesiones residenciales sticky de Chile con duración de
120 minutos para la validación finita está documentada en
[`docs/2captcha-proxy-options.md`](docs/2captcha-proxy-options.md).

### Diagnosticar perfiles separados manualmente

```bash
python -m cbrs pool init --account ejecutivo_1 --timeout 600
python -m cbrs pool init --account ejecutivo_2 --timeout 600
python -m cbrs pool init --account ejecutivo_3 --timeout 600
```

Cada comando abre una instancia con perfil persistente propio. Este camino sirve
para diagnóstico visual; la operación normal usa los nombres de variables de
secreto declarados en la configuración del pool.

### Dashboard y ejecución

`pool run` es legacy. Para endurance usar los comandos `jobs endurance` y las
tareas nativas del runbook.

```bash
python -m cbrs pool dashboard
python -m cbrs pool run --dashboard
python -m cbrs pool stop
```

El dashboard del pool también usa el puerto `8765` por defecto. Si se necesita
mantener ambos dashboards abiertos al mismo tiempo, asigna otro puerto:

```bash
python -m cbrs pool dashboard --port 8766
```

### Configuración local del pool

El archivo `.cbrs/account-pool.json` puede referenciar variables de entorno sin
contener los proxies reales:

```json
{
  "accounts": [
    {
      "id": "ejecutivo_1",
      "label": "Ejecutivo 1",
      "username_env": "CBRS_ACCOUNT_1_USERNAME",
      "password_env": "CBRS_ACCOUNT_1_PASSWORD",
      "proxy_provider": "dataimpulse_residential_sticky",
      "proxy_brand": "DataImpulse",
      "dataimpulse_port": 10000,
      "profile_dir": "G:\\CBRS\\accounts\\ejecutivo_1\\chrome-profile",
      "daily_quota": 20
    }
  ]
}
```

Las credenciales comunes del proxy se definen fuera de Git:

```dotenv
DATAIMPULSE_PROXY_LOGIN=REPLACE_ME
DATAIMPULSE_PROXY_PASSWORD=REPLACE_ME
```

Cada cuenta DataImpulse declara un puerto sticky distinto (`10000`, `10001`,
`10002` inicialmente). El runtime agrega `cr.cl;sessttl.120`, codifica la URL en
memoria y rechaza configuraciones ambiguas que también incluyan
`proxy_url_env`. Los proveedores genéricos heredados siguen aceptando una URL
por cuenta para compatibilidad.

En modo `2captcha_manual`, un rechazo del token del navegador deja la cuenta como
`captcha pendiente` sin contactar a 2Captcha. El operador puede pulsar
**Autorizar 1 solve 2Captcha**; esa autorización no se acumula y se consume solo
en el siguiente intento real de esa cuenta. Si no se usa, vence después de 15
minutos. Un segundo rechazo vuelve a pausar la cuenta. **Resolver visualmente**
conserva el camino headed alternativo.

## Resultados y trazabilidad

| Ruta local | Contenido |
|---|---|
| `outputs/` | PDFs generados por operaciones manuales. |
| `outputs/soak/<run>/<cycle>/` | Evidencia documental del monitoreo prolongado. |
| `outputs/pool/<run>/<cuenta>/<cycle>/` | PDFs organizados por ejecución, cuenta y ciclo. |
| `outputs/jobs/<job_id>/` | PDFs permanentes de cada solicitud de producción. |
| `.cbrs/logs/` | Reportes JSON sanitizados de preflight y validación. |
| `.cbrs/pool/pool.sqlite3` | Estado operacional local del pool. |

`.env`, `.cbrs/`, `outputs/`, cookies y archivos de sesión están excluidos por
`.gitignore` para evitar que datos operacionales o secretos lleguen al
repositorio.

## Estructura del repositorio

```text
cbrs/
  cli.py                     Comandos y experiencia del operador
  config.py                  Configuración CBRS_* y valores seguros
  preflight.py               Validación de egreso y reportes sanitizados
  browser_runtime.py         Detección de Google Chrome nativo y fallbacks diagnósticos
  browser_session.py         Perfiles persistentes y acceso same-origin
  client.py                  Ritmo, sesión y validación de respuestas
  safety.py                  Clasificación de detenciones y redacción
  scraper.py                 Búsqueda, descarga y flujo documental
  pdf.py                     Ensamblaje local de PDF
  validation.py              Validación controlada de bajo volumen
  soak.py                    Monitoreo prolongado
  soak_dashboard.py          Dashboard del monitoreo
  account_pool.py            Scheduler y estado multicuenta
  account_pool_dashboard.py  Dashboard del pool autorizado
  jobs.py                    Cola durable, leases, scheduler y worker productivo
  backup.py                  Snapshot SQLite, restic y salud de almacenamiento
  proxy_health.py            Gate de egreso, reCAPTCHA y portal
tests/                       Pruebas automatizadas
docs/                        Arquitectura y guías operacionales
```

## Límites y responsabilidades

- El portal puede imponer límites diarios; ante `err-limite`, la plataforma se
  detiene y no sigue consultando.
- El acceso debe contar con autorización del cliente y respetar los términos,
  cuotas y controles aplicables del CBRS.
- La confiabilidad depende de mantener perfiles de navegador y egresos estables.
- Un proxy puede salir por Chile y aun así no ser apto si bloquea reCAPTCHA o el
  endpoint inicial del portal.
- El dashboard es principalmente operativo; **Buscar y descargar ahora** puede
  encolar una solicitud prioritaria y solicitar el arranque seguro del worker.
  Los runners `soak` y `pool` siguen siendo los únicos procesos que ejecutan
  tráfico real contra CBRS.
- La resolución externa es opt-in y hace como máximo un fallback por operación;
  no existe rotación automática de IP, cambio de identidad ni reintento agresivo.
- Una cuenta pausada requiere revisión y no se fuerza automáticamente.
- Las pausas de seguridad sobreviven al reinicio del runner; un heartbeat vivo
  impide ejecutar dos runners sobre los mismos perfiles.
- Los dry-runs no consumen capacidad real. En una ejecución viva, todo intento
  iniciado se reserva de forma conservadora aunque termine bloqueado o fallido.
- `pool run` se conserva únicamente para validaciones estáticas con opt-in. La
  ruta productiva es `jobs worker` y la API loopback idempotente.

## Validación de infraestructura pendiente

La suite offline valida la cola, idempotencia, failover, cupos, PDFs, API,
redacción y respaldo. La aceptación final todavía debe ejecutar en el host
Windows del cliente el gate vivo de las tres cuentas, el probe pagado único,
reinicio durante descarga, 24 horas y luego siete días de endurance.

## Documentación adicional

- [Arquitectura y modelo de confianza](docs/architecture.md)
- [Runtime de confianza fija](docs/fixed-trust-runtime.md)
- [Pruebas de largo plazo](docs/soak-testing.md)
- [Plan de validación](docs/validation-plan.md)
- [Prerrequisitos Windows nativo](docs/native-windows-prerequisites.md)
- [Endurance E2E nativo](docs/native-windows-endurance.md)
- [Recuperación E2E de sesiones persistentes](docs/persistent-account-session-recovery.md)
- [Operación CBRS con DataImpulse](docs/dataimpulse-cbrs-operations.md)
- [Preparación Ubuntu legacy](docs/e2e-production-readiness.md)
