# Plataforma de Consulta Documental CBRS

> Solución controlada para consultar el Índice del Registro de Comercio del
> Conservador de Bienes Raíces de Santiago (CBRS), organizar resultados y generar
> documentos PDF con trazabilidad operacional.

![Python](https://img.shields.io/badge/Python-3.14-2563EB?style=flat-square&logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Chrome%20persistente-16A34A?style=flat-square&logo=googlechrome&logoColor=white)
![Operación](https://img.shields.io/badge/Operación-Controlada-7C3AED?style=flat-square)
![Seguridad](https://img.shields.io/badge/Seguridad-Detención%20preventiva-DC2626?style=flat-square)

## Resumen ejecutivo

Esta plataforma facilita consultas documentales autorizadas en el portal CBRS y
convierte las imágenes obtenidas en archivos PDF ordenados para revisión local.
La operación combina sesiones de navegador persistentes, validaciones previas,
ritmo controlado, monitoreo local y paradas automáticas ante señales de riesgo.

El sistema está diseñado para asistir a operadores autorizados, no para sustituir
los controles del portal. El inicio de sesión y la resolución de CAPTCHA son
manuales; no se almacenan credenciales en el repositorio, no se rotan identidades
y no se realizan reintentos agresivos.

| Valor para la operación | Cómo se materializa |
|---|---|
| **Automatización controlada** | Estandariza la búsqueda, selección y descarga sin perder supervisión humana. |
| **Trazabilidad operacional** | Registra ciclos, estados y evidencia sanitizada para facilitar seguimiento y auditoría. |
| **Protección de datos** | Mantiene perfiles, credenciales, resultados y configuración sensible fuera de Git. |
| **Seguridad preventiva** | Detiene la actividad ante límites, CAPTCHA, WAF, sesión inválida o cambios de egreso. |

## Cómo funciona

El operador prepara el entorno una vez, inicia sesión manualmente y luego utiliza
el mismo perfil autorizado para ejecutar consultas y generar documentos.

```mermaid
flowchart LR
    A["👤 Operador<br/>autorizado"] --> B["🩺 Doctor y<br/>preflight"]
    B --> C["🔐 Inicio de<br/>sesión manual"]
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
    DASH -. "lectura local" .-> DB
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

- **Interfaz de comandos:** concentra preparación, consultas, descargas,
  validación y operación de largo plazo.
- **Preflight de egreso:** confirma navegador, país, modalidad de conexión y
  estabilidad del egreso antes de acceder al portal.
- **Navegador persistente:** conserva localmente la sesión iniciada por el
  operador sin exportar cookies o credenciales.
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
requiere revisión o resolución manual de CAPTCHA.

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
- Comprobación previa del proxy: egreso chileno, carga de reCAPTCHA Enterprise y
  disponibilidad inicial del portal.
- Dashboard local en español para estado, ciclos, cupos, PDFs y alertas.
- Cobertura automatizada para configuración, navegador, seguridad, PDF,
  preflight, validación, soak y pool.

## Tecnología

| Componente | Uso |
|---|---|
| **Python 3.14** | Aplicación, orquestación y comandos. |
| **Playwright + Chrome/Edge** | Navegación con perfiles persistentes y sesiones aisladas. |
| **Pillow** | Ensamblaje de imágenes en documentos PDF. |
| **SQLite** | Estado local del monitoreo y del pool. |
| **`http.server`** | Dashboards locales de solo lectura. |
| **pytest** | Pruebas automatizadas de regresión y seguridad. |

## Inicio rápido

### 1. Instalar dependencias

La solución se ejecuta nativamente en Windows; no requiere Docker, WSL ni
máquinas virtuales.

```powershell
cd V:\scrapper\scrapper-portal-conservador
python -m pip install -r requirements.txt
```

### 2. Configurar el egreso autorizado

Crea un archivo `.env` local. Para producción utiliza una red del cliente o un
egreso dedicado:

```dotenv
CBRS_EGRESS_MODE=client_office
CBRS_EXPECTED_EGRESS_COUNTRY=CL
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

### 3. Validar e iniciar la sesión

```powershell
python -m cbrs doctor
python -m cbrs preflight --approve-egress-baseline
python -m cbrs init --timeout 600
```

`init` abre Chrome o Edge para que el operador inicie sesión manualmente. El
perfil queda almacenado localmente y se reutiliza en las siguientes operaciones,
sin exportar cookies ni archivos de sesión crudos.

## Consultas y documentos

### Búsqueda por razón social

```powershell
python -m cbrs search --query "BANCO DE CHILE"
python -m cbrs download --query "BANCO DE CHILE" --output outputs
```

### Búsqueda por inscripción

```powershell
python -m cbrs search --foja 9441 --numero 4580 --ano 1980
python -m cbrs download --foja 9441 --numero 4580 --ano 1980 --output outputs
```

`download` muestra los resultados y permite seleccionar valores como `1,3` o
`all`. El alias legado `--no-headless` se conserva. El flag antiguo
`--use-proxy` falla de forma explícita porque el runtime productivo requiere un
egreso fijo declarado, no rotación automática.

## Validación y monitoreo de largo plazo

Ejecuta una validación real de bajo volumen:

```powershell
python -m cbrs validate --query "BANCO DE CHILE" --download-first
```

Abre el dashboard sin iniciar tráfico hacia el portal:

```powershell
python -m cbrs soak dashboard
```

Ejecuta una prueba completamente local:

```powershell
python -m cbrs soak run --dry-run --max-cycles 3 --dashboard
```

Ejecuta o detén el monitoreo real:

```powershell
python -m cbrs soak run --dashboard
python -m cbrs soak stop
```

El dashboard de soak utiliza por defecto
[`http://127.0.0.1:8765`](http://127.0.0.1:8765). El runner real ejecuta ciclos
contra CBRS; el dashboard por sí solo es de solo lectura.

## Pool de cuentas autorizadas

### Comprobar las rutas antes del login

```powershell
python -m cbrs pool proxy-health
python -m cbrs pool proxy-health --account ejecutivo_1
```

Este gate verifica país `CL`, carga de Google reCAPTCHA Enterprise y respuesta
inicial del portal CBRS. Si falla, `pool init`, `pool login-debug` y los ciclos
reales no deben abrir el flujo del portal.

### Inicializar perfiles separados

```powershell
python -m cbrs pool init --account ejecutivo_1 --timeout 600
python -m cbrs pool init --account ejecutivo_2 --timeout 600
python -m cbrs pool init --account ejecutivo_3 --timeout 600
```

Cada comando abre una instancia con perfil persistente propio. El login es manual
y no se guardan RUT, email, contraseña ni token en la configuración del pool.

### Dashboard y ejecución

```powershell
python -m cbrs pool dashboard
python -m cbrs pool run --dashboard
python -m cbrs pool stop
```

El dashboard del pool también usa el puerto `8765` por defecto. Si se necesita
mantener ambos dashboards abiertos al mismo tiempo, asigna otro puerto:

```powershell
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
      "proxy_url_env": "CBRS_EJECUTIVO_1_PROXY_URL"
    }
  ]
}
```

La URL real se define fuera de Git:

```powershell
$env:CBRS_EJECUTIVO_1_PROXY_URL = "http://usuario:password@host:puerto"
```

Si CBRS responde `captcha_rejected`, la cuenta queda marcada como
`captcha pendiente` y sale temporalmente del scheduler. El botón **Resolver
captcha** abre el perfil correspondiente en modo visible; después ejecuta una
verificación segura y reincorpora la cuenta únicamente si la sesión es válida.

## Resultados y trazabilidad

| Ruta local | Contenido |
|---|---|
| `outputs/` | PDFs generados por operaciones manuales. |
| `outputs/soak/<run>/<cycle>/` | Evidencia documental del monitoreo prolongado. |
| `outputs/pool/<run>/<cuenta>/<cycle>/` | PDFs organizados por ejecución, cuenta y ciclo. |
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
  browser_runtime.py         Detección de Chrome/Edge
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
- El dashboard no produce tráfico por sí solo; los runners `soak` y `pool` sí
  ejecutan consultas reales cuando están activos.
- No existe resolución externa de CAPTCHA, rotación automática de IP, cambio de
  identidad ni reintentos agresivos.
- Una cuenta pausada requiere revisión y no se fuerza automáticamente.

## Próximos pasos recomendados

- Confirmar con CBRS o con el cliente el modelo oficial de acceso, cuota o
  allowlisting para producción.
- Definir un límite diario operacional conservador según la autorización vigente.
- Confirmar si el máximo teórico de 60 consultas del pool debe reducirse para
  mantener margen operacional.
- Incorporar capturas visuales sanitizadas cuando ocurra una detención preventiva.
- Preparar fixtures completamente offline para demostraciones sin acceso al
  portal real.

## Documentación adicional

- [Arquitectura y modelo de confianza](docs/architecture.md)
- [Runtime de confianza fija](docs/fixed-trust-runtime.md)
- [Pruebas de largo plazo](docs/soak-testing.md)
- [Plan de validación](docs/validation-plan.md)
