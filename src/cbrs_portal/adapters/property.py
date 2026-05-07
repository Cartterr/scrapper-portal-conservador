from __future__ import annotations

from typing import Any

from ..portal_client import PortalClient


class PropertyAdapter:
    """Mapped but not yet production-certified Propiedad index adapter.

    The SPA exposes the endpoints below, but this flow still needs fresh logged-in
    probe validation before being enabled for automated jobs.
    """

    ENDPOINTS = {
        "base": "/api/v1/propiedad/indice/base",
        "text": "/api/v1/propiedad/indice/texto",
        "fna": "/api/v1/propiedad/indice/fna",
        "extra_features": "/api/v1/propiedad/indice/texto-extra-features",
        "image": "/api/v1/propiedad/indice/img",
    }

    def __init__(self, client: PortalClient):
        self.client = client

    def not_certified(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "Propiedad flow is mapped but not production-certified. "
            "Run a logged-in probe before enabling this adapter."
        )
