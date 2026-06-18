# CBRS Portal Conservador

Automatización controlada para consultar el Índice del Registro de Comercio del
portal CBRS, validar el flujo real con una sesión persistente y guardar PDFs para
revisión local.

## Stack

- Python 3.14.
- Playwright con Chrome/Edge instalado en la máquina.
- Perfil persistente local en `.cbrs/chrome-profile`.
- `Pillow` para ensamblar imágenes en PDF.
- `pytest` para pruebas automatizadas.
- SQLite + `http.server` para el monitor local de prueba continua.

## Qué Hace

- Abre una sesión manual en el portal CBRS con `python -m cbrs init`.
- Ejecuta búsquedas por razón social o por foja/número/año.
- Descarga el primer resultado o los documentos indicados y genera PDFs.
- Guarda PDFs en `outputs/`.
- Genera reportes sanitizados de validación en `.cbrs/logs/`.
- Incluye un monitor local de prueba continua en `http://127.0.0.1:8765`.
- Detiene el flujo ante señales de seguridad como límite diario, CAPTCHA,
  errores WAF, drift de egreso o falta de sesión.

## Caveats

- El portal impone límites diarios de consulta; cuando responde `err-limite`, el
  sistema se pausa y no sigue consultando.
- El login es manual; no se guardan credenciales ni se automatiza el ingreso.
- La confiabilidad depende de mantener el mismo perfil de navegador y un egreso
  estable/autorizado.
- El monitor local no aumenta tráfico por sí solo, pero el runner de soak sí
  ejecuta ciclos reales cuando está activo.
- No hay rotación de cuentas, rotación de IP, resolución externa de CAPTCHA ni
  reintentos agresivos.

## Mejoras Sobre los Scripts Originales

- Se reemplazó el flujo basado en credenciales y rotación por login manual con
  perfil persistente.
- Se dejó de guardar sesiones crudas tipo `.cbrs_session.json`.
- Se agregó preflight de egreso, país esperado y hash sanitizado.
- Se incorporaron paradas duras ante `403`, `429`, `err-limite`,
  `intente-mas-tarde`, CAPTCHA o HTML de desafío.
- Se organizaron outputs en `outputs/` y `outputs/soak/<run>/<cycle>/`.
- Se agregaron reportes JSON sanitizados para auditoría.
- Se implementó `doctor`, `preflight`, `validate` y el grupo `soak`.
- Se agregó dashboard local en español con estado vivo, countdown, PDFs,
  ciclos, eventos y alertas críticas.
- Se añadió cobertura de pruebas para configuración, seguridad, preflight,
  validación, PDFs, runtime de navegador y soak.

## Áreas a Explorar

- Confirmar con CBRS o el cliente un modelo oficial de acceso, cuota o
  allowlisting para uso productivo.
- Definir una cuota diaria operacional segura según contrato o autorización del
  portal.
- Mejorar captura visual automática del portal cuando ocurra un safety stop.
- Evaluar egreso dedicado/cliente si la red actual no es el ambiente final.
- Separar un modo de prueba completamente offline con fixtures para demos sin
  tocar el portal real.
