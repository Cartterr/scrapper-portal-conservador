# Uso standalone como componente

Este modo sirve para integrar la cola CBRS en otra aplicación Linux sin instalar
systemd. No debe ejecutarse al mismo tiempo que el servicio activo ni contra sus
perfiles o su base SQLite.

## Preparación

1. Clonar el repositorio en una ruta propia y crear `.venv` con Python 3.14.
2. Copiar `.env.example` a `.env` y `deploy/account-pool.json.example` a
   `.cbrs/runtime/account-pool.json`.
3. Cambiar `CBRS_BROWSER_OWNER_MODE=embedded`; cada proceso standalone será dueño
   de sus Chrome hasta terminar.
4. Mantener `CBRS_CAPTCHA_SOLVER_MODE=browser`, salvo que exista una prueba
   explícita y una clave válida para otro modo.
5. Definir `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS` solo con los IDs autorizados
   para probar candidatos nuevos; dejarlo vacío desactiva la sustitución y no se
   admite wildcard.

`python -m cbrs` carga `.env` directamente, incluidas las credenciales de las
cuentas. `deploy/run_with_env.py` sigue disponible por compatibilidad, pero ya no
es obligatorio. Una variable explícita del proceso tiene prioridad sobre `.env`.

## Ejecución

```bash
.venv/bin/python -m cbrs config validate
.venv/bin/python -m cbrs pool proxy-health
.venv/bin/python -m cbrs jobs enqueue --foja 9441 --numero 4580 --year 1980
.venv/bin/python -m cbrs jobs worker
```

Usar un solo worker. Para una llamada embebida, importar `run_job_worker` y pasar
`Settings`, `PoolConfig`, `JobStore` y `AccountPoolStore` propios. El worker debe
permanecer vivo para que cooldowns y recuperación automática sigan avanzando;
`--once` procesa solo una unidad de trabajo y no es un modo de autorecovery
continuo.

## Recuperación y resultados inciertos

Una cuenta con rechazo visible puede probar hasta 10 candidatos Mobile distintos
por recuperación, con un máximo durable de 30 por hora. Solo se adopta el Chrome
que muestra el formulario autenticado; los candidatos fallidos se descartan y
una sesión autenticada nunca se vuelve a abrir para promoverla.

La búsqueda FNA hace un solo click. Espera hasta 90 segundos por el POST del
portal y otros 90 por su respuesta. Si el navegador abandona la ruta antes del
POST, otra cuenta puede continuar. Si el click quedó en un estado ambiguo, el job
conserva la reserva y el scheduler exige reconciliación explícita antes de
autorizar otra cuenta.

Tras confirmar en el historial del portal que el primer POST no fue aceptado,
usar `deploy/resume_unconfirmed_jobs.py JOB_ID` como validación de solo lectura y
repetir con `--apply` para autorizar una única cuenta distinta. Nunca aplicar la
autorización si la inscripción ya figura en el historial.

El límite conservador incluido es 8 consultas diarias por cuenta, basado en la
observación de 7 a 9 del 10 de septiembre de 2026. Ajustarlo solo con evidencia
vigente del contrato y del portal.
