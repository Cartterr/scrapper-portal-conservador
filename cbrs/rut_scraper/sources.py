"""
Multi-source RUT scraper with rotation.
Working sources (as of Oct 2026):
- https://buscarut.com/
- https://www.rutificador.co/
- https://rutyfirma.com/
- https://rutificadorcl.cl/
- https://elrutificador.com/
"""

WORKING_SOURCES = [
    "https://buscarut.com/",
    "https://www.rutificador.co/",
    "https://rutyfirma.com/",
    "https://rutificadorcl.cl/",
]

BLOCKED_SOURCES = [
    {"name": "Nombrerutyfirma", "url": "https://nombrerutyfirma.com/", "error": "HTTP 403"},
    {"name": "Rutynombre", "url": "https://www.rutynombre.com/", "error": "ERR_ABORTED"},
    {"name": "BuscarRUT.cl", "url": "https://buscarrut.cl/", "error": "ERR_ABORTED"},
    {"name": "NombreYRut", "url": "https://nombreyrut.com/", "error": "HTTP 403"},
    {"name": "Genealog", "url": "https://www.genealog.cl/", "error": "ERR_ABORTED"},
    {"name": "ElRutificador.cl", "url": "https://elrutificador.cl/", "error": "ERR_ABORTED"},
]
