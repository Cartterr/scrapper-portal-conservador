# Diseño de documentación profesional para clientes

## Objetivo

Transformar la portada del repositorio en una presentación profesional, clara y
confiable para un cliente no técnico, manteniendo suficientes detalles técnicos
para explicar cómo opera la solución y cuáles son sus controles. El cambio será
exclusivamente documental: no modificará código, configuración ni lógica de
ejecución.

## Audiencia

La audiencia principal es un cliente no técnico que necesita comprender:

- qué problema resuelve la solución;
- cómo es el proceso de consulta y generación de documentos;
- qué controles protegen la operación;
- qué componentes técnicos principales utiliza;
- cuáles son sus límites y responsabilidades operacionales.

## Enfoque seleccionado

Se reorganizará `README.md` como una portada ejecutiva con profundidad técnica
progresiva. La información más importante para el cliente aparecerá primero y
las instrucciones operacionales quedarán en secciones posteriores para conservar
el valor del README como guía de uso.

No se creará una presentación separada: GitHub mostrará la explicación completa
al abrir el repositorio, evitando que el cliente tenga que navegar a otro archivo.

## Estructura de contenido

El README incluirá, en este orden:

1. Título profesional y propuesta de valor.
2. Resumen ejecutivo de la solución.
3. Beneficios y capacidades principales.
4. Explicación visual del proceso completo.
5. Arquitectura simplificada y componentes técnicos.
6. Controles de seguridad, privacidad y detención preventiva.
7. Modelo operacional de cuentas autorizadas.
8. Resumen de tecnologías utilizadas.
9. Inicio rápido, configuración y comandos disponibles.
10. Estructura de resultados y reportes.
11. Alcance, limitaciones y próximos pasos recomendados.

La redacción evitará jerga innecesaria y definirá los conceptos técnicos que sean
importantes para la confianza del cliente.

## Diagramas

Se incorporarán diagramas Mermaid en español para que se rendericen directamente
en GitHub. Cada diagrama usará una paleta profesional consistente con colores
azul, celeste, verde, ámbar y rojo, reservando cada color para una función clara.

### 1. Flujo de operación

Representará la secuencia:

`Preparación segura → sesión manual → consulta → selección → generación de PDF → reporte`.

También mostrará que las validaciones previas se ejecutan antes de acceder al
portal.

### 2. Arquitectura simplificada

Mostrará las relaciones entre:

- operador;
- interfaz de comandos;
- controles de configuración y egreso;
- navegador con perfil persistente;
- portal CBRS;
- generación de PDF;
- almacenamiento local de resultados y reportes;
- dashboards locales de monitoreo.

### 3. Decisión de seguridad

Explicará visualmente que una operación normal continúa, mientras que señales de
límite, sesión inválida, CAPTCHA, WAF, cambio de egreso o respuesta inesperada
producen una pausa segura y revisión manual, sin reintentos agresivos.

### 4. Operación con cuentas autorizadas

Explicará el uso de perfiles aislados, cupos por cuenta y un scheduler controlado.
El texto dejará explícito que se trata de cuentas nominales autorizadas, no de un
mecanismo para eludir límites o controles del portal.

## Mensajes clave

- La solución automatiza consultas autorizadas y organiza documentos del Índice
  del Registro de Comercio del CBRS.
- El acceso se realiza mediante inicio de sesión manual y perfiles persistentes;
  no se almacenan credenciales en el repositorio.
- El entorno valida navegador, configuración y egreso antes de operar.
- Los controles priorizan detenerse y pedir revisión ante señales de riesgo.
- Los PDFs, reportes sanitizados y datos operacionales permanecen en rutas locales
  excluidas de Git.
- La solución no resuelve CAPTCHA externamente, no rota identidades y no ejecuta
  reintentos agresivos.

## Detalles técnicos que se conservarán

- Python 3.14 y Playwright con Chrome o Edge instalado localmente.
- Perfiles de navegador persistentes y separados por cuenta autorizada.
- Ejecución desde una interfaz de línea de comandos.
- Ensamblaje local de imágenes en PDF con Pillow.
- SQLite y `http.server` para monitoreo local.
- Reportes JSON sanitizados y pruebas automatizadas con pytest.
- Modos `doctor`, `preflight`, `init`, `search`, `download`, `validate`, `soak`
  y `pool`.

## Restricciones

- No cambiar archivos Python, pruebas, dependencias ni configuración de ejecución.
- No alterar comandos, rutas, límites, valores predeterminados ni comportamiento.
- No incluir credenciales, identificadores personales, proxies reales ni otros
  secretos.
- No prometer disponibilidad, capacidad o autorización que el código y la
  documentación existente no demuestren.
- Mantener las advertencias operacionales relevantes del README actual.

## Validación

La documentación se considerará lista cuando:

- los bloques Mermaid tengan sintaxis válida y etiquetas en español;
- todos los comandos existentes importantes sigan documentados;
- las afirmaciones coincidan con los módulos y el flujo real del repositorio;
- `git diff` confirme que solo se modificaron archivos Markdown;
- la suite de pruebas continúe pasando sin cambios de lógica;
- la rama se publique en GitHub mediante un commit descriptivo.
