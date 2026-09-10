# Onboarding del operador por CLI

Esta guía deja al operador listo para trabajar desde una terminal pequeña de
Ubuntu/WSL. La lista exhaustiva está en [cli-reference.md](cli-reference.md).

## Primera vez

```bash
cd /opt/scrapper-portal-conservador
bash deploy/install-wsl.sh
sudo bash deploy/install-ubuntu.sh
```

Cierra y abre una terminal WSL para recibir el grupo `cbrs`. Luego:

```bash
cbrs config paths
sudoedit /opt/scrapper-portal-conservador/.env
cbrs config validate
cbrs health
cbrs overview
```

El archivo de cuentas está indicado por `cbrs config paths`. Los valores
sensibles se editan localmente y nunca deben pegarse en tickets, documentación
o capturas.

## Rutina diaria

```bash
cbrs overview --watch
cbrs jobs enqueue --text "RAZÓN SOCIAL"
cbrs jobs list --limit 10
cbrs jobs show JOB_ID
```

Presiona `Ctrl+C` para salir del panel; no detiene el servicio.

## Si algo no avanza

```bash
cbrs health
cbrs service logs worker --lines 120
cbrs accounts
cbrs jobs status
```

No reinicies el owner ni cierres Chrome. Si el cambio es solo del worker y fue
validado, usa `cbrs service restart worker`. Si hay sesiones vivas y la causa
no está clara, conserva el estado y escala el diagnóstico.
