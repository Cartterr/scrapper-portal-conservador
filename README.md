# Plataforma de Consulta Documental CBRS

> Solución controlada para consultar el Índice del Registro de Comercio del
> Conservador de Bienes Raíces de Santiago (CBRS), organizar resultados y generar
> documentos PDF con trazabilidad operacional.

![Python](https://img.shields.io/badge/Python-3.14-2563EB?style=flat-square&logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Chrome%20persistente-16A34A?style=flat-square&logo=googlechrome&logoColor=white)
![Operación](https://img.shields.io/badge/Operación-Controlada-7C3AED?style=flat-square)
![Seguridad](https://img.shields.io/badge/Seguridad-Detención%20preventiva-DC2626?style=flat-square)

## Resumen ejecutivo

Antes de instalar, revisar [PREREQUISITES.txt](PREREQUISITES.txt). Ese archivo
define los requisitos del equipo, autorizaciones, cuentas, proxies, respaldo y
gates que tambien debera aplicar el instalador E2E de Windows.

### Instalación E2E en Windows

Después de clonar una release aprobada, el cliente solo debe hacer clic derecho
sobre [`INSTALL-CBRS.bat`](INSTALL-CBRS.bat) y elegir **Ejecutar como
administrador**. El instalador:

1. valida el checkout y habilita/reutiliza WSL2 con Ubuntu 24.04;
2. puede continuar automáticamente después de un reinicio requerido;
3. instala el runtime Linux y conserva cualquier estado existente;
4. solicita cuentas, contraseñas y proxies mediante campos ocultos;
5. configura restic, servicios y el dashboard nativo;
6. ejecuta readiness y muestra un resultado `PASS/FAIL` sanitizado.

La instalación no aprueba egresos ni inicia el worker sin confirmaciones
separadas. Para revisar el plan sin cambios se puede ejecutar
`INSTALL-CBRS.bat --plan` desde una consola.

Esta plataforma facilita consultas documentales autorizadas en el portal CBRS y
convierte las imágenes obtenidas en archivos PDF ordenados para revisión local.
La operación combina sesiones de navegador persistentes, validaciones previas,
ritmo controlado, monitoreo local y paradas automáticas ante señales de riesgo.

El sistema está diseñado para operar cuentas expresamente autorizadas sin
sustituir los controles del portal. Las sesiones se renuevan o autentican dentro
del navegador persistente usando secretos referenciados por variables de
entorno. CAPTCHA sigue siendo una intervención visual: no se almacenan secretos
en Git, no se rotan identidades y no se realizan reintentos agresivos.

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
| **Playwright + Google Chrome** | Perfiles persistentes y aislados sobre Ubuntu/Xvfb o WSLg. |
| **Pillow** | Ensamblaje de imágenes en documentos PDF. |
| **SQLite** | Estado local del monitoreo y del pool. |
| **`http.server`** | Dashboard y API JSON en loopback; en WSL2 puede habilitarse explícitamente la interfaz privada del VM. |
| **pytest** | Pruebas automatizadas de regresión y seguridad. |

## Límite del entorno soportado

CBRS tiene **un solo runtime soportado: Ubuntu Linux**.

- En producción, Python, Playwright, Google Chrome, Xvfb, SQLite, restic,
  dashboards y servicios `systemd` se ejecutan directamente en Ubuntu.
- En un PC Windows, esos mismos componentes se ejecutan dentro de Ubuntu sobre
  WSL2. La operación desatendida usa Xvfb igual que producción. WSLg se reserva
  únicamente para depuración visual o intervención manual solicitada.
- Windows y PowerShell no forman parte del runtime de CBRS. No se debe ejecutar
  `python -m cbrs` con Python de Windows ni conectar Playwright Linux a Chrome de
  Windows.
- El único paso del host Windows es instalar o abrir WSL2. Por ejemplo,
  `wsl --install --distribution Ubuntu-24.04` se ejecuta una sola vez desde una
  terminal elevada de Windows. Después de abrir Ubuntu, todos los comandos de
  este README se ejecutan con Bash dentro de Ubuntu.

El repositorio puede permanecer en una unidad montada como `/mnt/v`, pero el
proceso, virtualenv, navegador, perfiles operativos y servicios siguen siendo
Linux. Los scripts PowerShell de `deploy/windows/` son ayudantes opcionales del
host y no son la ruta normal de instalación, prueba ni operación.

### Operación sin ventana y sin interferencia de foco

La ruta productiva cumple el requisito operativo de ejecución *headless* sin
activar el modo `--headless` de Chrome:

```text
systemd -> Python/Playwright -> Google Chrome headed -> DISPLAY=:99 -> Xvfb
```

Xvfb es un servidor gráfico virtual en memoria, independiente de WSLg, del
escritorio de Windows y de cualquier sesión física de Ubuntu. Chrome no crea una
ventana en el escritorio del operador, no aparece en la barra de tareas y no
puede tomar el foco ni interrumpir escritura en otras aplicaciones. Esto no es
una ventana movida fuera de pantalla: el navegador pertenece a otro servidor de
display que normalmente no tiene visor conectado.

La distinción debe conservarse en diagnósticos y reportes:

- **modo del motor:** Chrome headed convencional, requerido por el comportamiento
  observado de CBRS;
- **modo operativo:** display virtual aislado, sin ventana física ni impacto en
  el foco del operador;
- **modo `--headless` de Chrome:** diagnóstico solamente mientras CBRS lo rechace
  de forma inconsistente.

El servicio `cbrs-display.service` crea `DISPLAY=:99` y
`cbrs-worker.service` depende de él. noVNC/x11vnc permanecen detenidos durante la
operación normal y se levantan solo para una recuperación visual explícita. En
WSL2 no se debe exportar `DISPLAY=:0` al worker, porque ese valor corresponde a
WSLg y sí puede crear una ventana visible en Windows.

## Inicio rápido

### 1. Instalar dependencias

Abre una terminal Ubuntu —directa en producción o mediante WSL2 en desarrollo—
y ejecuta:

```bash
bash deploy/install-wsl.sh
.venv/bin/python -m pytest -q
```

La guía de Ubuntu, el ejemplo de `systemd` y el gate de aceptación están en
[Preparación E2E y despliegue en Ubuntu](docs/e2e-production-readiness.md).

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

El dashboard/API escucha normalmente en `127.0.0.1:8765`. En WSL2, el drop-in
`deploy/cbrs-dashboard-wsl.conf` habilita de forma explícita la interfaz privada
de la VM para que pueda abrirse desde cualquier navegador del mismo PC. Usa:

```bash
hostname -I
```

Si el resultado comienza, por ejemplo, con `172.20.42.4`, las dos rutas desde
Chrome, Edge, Brave o Firefox de Windows son:

```text
http://127.0.0.1:8765/
http://172.20.42.4:8765/
```

La segunda dirección puede cambiar después de apagar completamente WSL. Esta
excepción es solo para la red privada de desarrollo WSL2; la unidad productiva
permanece ligada a loopback y los puertos nunca se exponen a Internet. Las
búsquedas procesan todas las coincidencias y publican PDFs permanentes bajo
`outputs/jobs/<job_id>/`.

En WSL2 el dashboard se abre en el navegador **nativo de Windows**, mientras que
el worker, SQLite, Playwright y todos los procesos CBRS permanecen en Ubuntu. Es
una excepción exclusiva de visualización: WSLg expone actualmente el escritorio
a 60 Hz, mientras que el navegador nativo puede usar la frecuencia real del
monitor. El puente utiliza `http://127.0.0.1:8765/`, espera hasta que responda y
no envía tráfico CBRS ni mueve lógica de aplicación a Windows.

En la estación WSL de desarrollo, el inicio de sesión de Windows puede registrar
`deploy/windows/Start-CbrsWslHidden.vbs` como puente de arranque sin privilegios.
Ese puente mantiene Ubuntu iniciado y abre el dashboard en el navegador nativo
cuando el endpoint esté listo; no ejecuta lógica CBRS en Windows. El botón
**Configurar cuentas** abre un formulario local para agregar, actualizar o
eliminar cuentas. Las contraseñas existentes nunca se cargan ni se muestran: los
campos vacíos las conservan y el botón de revelar solo muestra valores escritos
en la sesión actual. Al guardar, una unidad local de `systemd` aplica la
configuración protegida y mantiene el worker detenido hasta que el operador use
**Reanudar worker**. Una vez iniciado Ubuntu, `systemd` recupera el worker, el
dashboard y el temporizador de respaldo habilitados.

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

Para comprobar la preparación E2E desde el propio Ubuntu sin iniciar Chrome ni
tráfico, ejecutar dentro de `/opt/cbrs`:

```bash
.venv/bin/python -m cbrs readiness \
  --target ubuntu \
  --env-file /etc/cbrs/cbrs.env \
  --config /var/lib/cbrs/account-pool.json \
  --json-report /var/lib/cbrs/readiness/indefinite-test.json
```

El procedimiento escalonado, los controles de arranque/parada y los criterios
de observación están en
[`docs/indefinite-test-runbook.md`](docs/indefinite-test-runbook.md).

## Pool de cuentas autorizadas

### Comprobar las rutas antes del login

```bash
python -m cbrs pool proxy-health
python -m cbrs pool proxy-health --account ejecutivo_1
```

Este gate verifica país `CL`, carga de Google reCAPTCHA Enterprise y respuesta
inicial del portal CBRS. Si falla, `pool init`, `pool login-debug` y los ciclos
reales no deben abrir el flujo del portal.

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
      "proxy_url_env": "CBRS_ACCOUNT_1_PROXY_URL",
      "egress_group": "chile_compartida_1",
      "profile_dir": "/var/lib/cbrs/accounts/ejecutivo_1/chrome-profile",
      "daily_quota": 20
    }
  ]
}
```

La URL real se define fuera de Git:

```bash
export CBRS_ACCOUNT_1_PROXY_URL="http://usuario:password@host:puerto"
```

Por defecto, el pool exige una URL proxy distinta por cuenta. Cuando varias
cuentas autorizadas deben compartir deliberadamente una misma salida chilena,
cada una conserva su propia referencia `proxy_url_env` y declara el mismo
`egress_group`. El valor compartido nunca se infiere silenciosamente: sin ese
grupo explícito la configuración se rechaza. El dashboard muestra la cantidad
de rutas de salida y marca cada cuenta como **salida compartida**, mientras los
perfiles, sesiones y cupos siguen siendo independientes. Compartir una salida
no modifica el límite de 20 consultas por cuenta ni permite tráfico paralelo.

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
  browser_runtime.py         Detección de Google Chrome en Ubuntu y fallbacks diagnósticos
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
- No existe resolución externa de CAPTCHA, rotación automática de IP, cambio de
  identidad ni reintentos agresivos.
- Una cuenta pausada requiere revisión y no se fuerza automáticamente.
- Las pausas de seguridad sobreviven al reinicio del runner; un heartbeat vivo
  impide ejecutar dos runners sobre los mismos perfiles.
- Los dry-runs no consumen capacidad real. En una ejecución viva, todo intento
  iniciado se reserva de forma conservadora aunque termine bloqueado o fallido.
- `pool run` se conserva únicamente para validaciones estáticas con opt-in. La
  ruta productiva es `jobs worker` y la API loopback idempotente.

## Validación de infraestructura pendiente

La suite offline valida la cola, idempotencia, failover, cupos, PDFs, API,
redacción y respaldo. La aceptación final todavía debe ejecutar en Ubuntu del
cliente el gate vivo de cuentas nuevas, 60 consultas autorizadas, CAPTCHA visual,
reinicio durante descarga y soak de siete días.

## Documentación adicional

- [Arquitectura y modelo de confianza](docs/architecture.md)
- [Runtime de confianza fija](docs/fixed-trust-runtime.md)
- [Pruebas de largo plazo](docs/soak-testing.md)
- [Plan de validación](docs/validation-plan.md)
- [Preparación E2E y despliegue en Ubuntu](docs/e2e-production-readiness.md)
