from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactRecord, commerce_stem, create_pdf, save_image_bytes
from ..portal_client import PortalClient

logger = logging.getLogger(__name__)


class CommerceAdapter:
    def __init__(self, client: PortalClient):
        self.client = client

    def search_text(self, text: str) -> list[dict[str, Any]]:
        token = self._fresh_token("indice_com_texto", "/api/v1/comercio/indice/texto")
        body = {
            "foja": None,
            "numero": None,
            "ano": None,
            "texto": text,
            "recaptchaToken": token,
            "ticket": None,
            "titulosAnteriores": False,
            "comuna": None,
            "anoP": None,
            "origen": "texto",
        }
        response = self.client.post_json(
            "/api/v1/comercio/indice/texto",
            body,
            auth=True,
            captcha_token=token,
        )
        return _expect_list(response.data)

    def search_fna(self, foja: int, numero: int, ano: int) -> list[dict[str, Any]]:
        token = self._fresh_token("indice_com_texto", "/api/v1/comercio/indice/texto")
        body = {
            "foja": foja,
            "numero": numero,
            "ano": ano,
            "texto": None,
            "recaptchaToken": token,
            "ticket": None,
            "titulosAnteriores": False,
            "comuna": None,
            "anoP": None,
            "origen": "fna",
        }
        response = self.client.post_json(
            "/api/v1/comercio/indice/texto",
            body,
            auth=True,
            captcha_token=token,
        )
        return _expect_list(response.data)

    def validate_ticket(self, ticket: str) -> dict[str, Any]:
        token = self._fresh_token("indice_com_texto", "/api/v1/comercio/indice/fnaTicket")
        response = self.client.post_json(
            "/api/v1/comercio/indice/fnaTicket",
            {"ticket": ticket},
            auth=True,
            captcha_token=token,
        )
        if not isinstance(response.data, dict):
            raise ValueError("Ticket validation response shape changed")
        return response.data

    def image_refs(self, ticket_info: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post_json(
            "/api/v1/comercio/indice/img",
            ticket_info,
            auth=True,
        )
        if not isinstance(response.data, dict) or "refs" not in response.data:
            raise ValueError("Image refs response shape changed")
        return response.data

    def download_image(self, data_ref: str, output_path: Path) -> ArtifactRecord:
        _status, content, headers = self.client.get_bytes(
            f"/api/v1/comercio/indice/img/{data_ref}",
            auth=False,
        )
        content_type = headers.get("content-type", "application/octet-stream").split(";")[0]
        return save_image_bytes(content, output_path, content_type=content_type)

    def download_all_images(
        self,
        ticket: str,
        output_dir: Path,
        *,
        keep_images: bool = False,
    ) -> ArtifactRecord:
        ticket_info = self.validate_ticket(ticket)
        refs_payload = self.image_refs(ticket_info)
        refs = refs_payload.get("refs") or []
        if not refs:
            raise ValueError("Image refs response did not include any pages")
        logger.info("CBRS download preflight: %s page image request(s)", len(refs))
        print(f"Download preflight: {len(refs)} page image request(s), sequential and paced.")
        stem = commerce_stem(ticket_info.get("foja"), ticket_info.get("numero"), ticket_info.get("ano"))
        image_paths: list[Path] = []
        for ref in refs:
            page_number = ref.get("pageNumber")
            data_ref = ref.get("dataRef")
            if not data_ref:
                raise ValueError("Image ref missing dataRef")
            image_path = output_dir / f"{stem}_page{page_number}.png"
            self.download_image(data_ref, image_path)
            image_paths.append(image_path)
        pdf_record = create_pdf(image_paths, output_dir / f"{stem}.pdf")
        if not keep_images:
            for path in image_paths:
                path.unlink(missing_ok=True)
        return pdf_record

    def _fresh_token(self, action: str, endpoint: str) -> str:
        created_at = time.monotonic()
        token = self.client.browser.recaptcha_token(action)
        self.client.safety.assert_fresh_token(
            token_age_seconds=time.monotonic() - created_at,
            endpoint=endpoint,
        )
        return token


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "foja",
        "num",
        "numero",
        "ano",
        "acto",
        "tipo",
        "folio",
        "nombreSociedad",
        "personas",
        "esVisible",
        "esVisibleMsg",
    }
    return {key: value for key, value in result.items() if key in allowed}


def _expect_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Search response shape changed")
    return value
