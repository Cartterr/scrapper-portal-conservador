from __future__ import annotations

from typing import Any

from ..portal_client import PortalClient


class VerificationAdapter:
    """Mapped but not yet production-certified document verification adapter."""

    ENDPOINTS = {
        "state": "/api/v1/consulta-en-linea/estado",
        "validate_code": "/api/v1/consulta-en-linea/verifica-doc/validaCodigo",
        "get_document": "/api/v1/consulta-en-linea/verifica-doc/obtenerDocumento",
        "fna_verify": "/api/v1/fna/verifica/",
        "notary_verify": "/api/v1/notarioElectronico/verifica",
    }

    def __init__(self, client: PortalClient):
        self.client = client

    def not_certified(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "Verification flows are mapped but not production-certified. "
            "Run logged-in probes before enabling this adapter."
        )
