from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .browser_session import BrowserSession
from .client import BrowserOriginClient
from .config import SETTINGS, Settings
from .pdf import create_pdf

logger = logging.getLogger(__name__)


class CBRSScraper:
    def __init__(self, *, headless: bool = False, settings: Settings = SETTINGS) -> None:
        self.settings = settings
        self.browser = BrowserSession(settings, headless=headless)
        self.client = BrowserOriginClient(self.browser, settings)

    def close(self) -> None:
        self.client.close()
        self.browser.close()

    def __enter__(self) -> CBRSScraper:
        self.browser.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def init_session(self, *, timeout_seconds: int | None = None) -> None:
        self.browser.open()
        self.browser.wait_for_login(timeout_seconds=timeout_seconds)

    def search_by_text(self, texto: str) -> list[dict[str, Any]]:
        logger.info("Searching commerce index by text")
        body = {
            "foja": None,
            "numero": None,
            "ano": None,
            "texto": texto,
            "recaptchaToken": None,
            "ticket": None,
            "titulosAnteriores": False,
            "comuna": None,
            "anoP": None,
            "origen": "texto",
        }
        return self._search(body)

    def search_by_fna(self, foja: int, numero: int, ano: int) -> list[dict[str, Any]]:
        logger.info("Searching commerce index by FNA")
        body = {
            "foja": foja,
            "numero": numero,
            "ano": ano,
            "texto": None,
            "recaptchaToken": None,
            "ticket": None,
            "titulosAnteriores": False,
            "comuna": None,
            "anoP": None,
            "origen": "fna",
        }
        return self._search(body)

    def get_image_refs(self, ticket: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        logger.info("Validating selected ticket")
        ticket_info = self.client.post_json(
            "/api/v1/comercio/indice/fnaTicket",
            {"ticket": ticket},
            captcha_action="indice_com_texto",
            context="ticket validation",
        )

        logger.info("Getting image references")
        refs_result = self.client.post_json(
            "/api/v1/comercio/indice/img",
            ticket_info,
            context="image reference lookup",
        )
        refs = refs_result.get("refs", []) if isinstance(refs_result, dict) else []
        logger.info("Found %d page(s)", len(refs))
        return ticket_info, refs

    def download_image(self, uuid: str, output_path: Path) -> Path:
        logger.info("Downloading image page to %s", output_path)
        content = self.client.get_bytes(
            f"/api/v1/comercio/indice/img/{uuid}",
            context="image download",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)
        return output_path

    def download_all_images(
        self,
        ticket: str,
        output_dir: Path,
        *,
        keep_images: bool = False,
    ) -> Path:
        ticket_info, refs = self.get_image_refs(ticket)
        if not refs:
            raise RuntimeError("No image references returned for this inscription.")

        foja = _safe_part(ticket_info.get("foja", "unknown"))
        numero = _safe_part(ticket_info.get("numero", "unknown"))
        ano = _safe_part(ticket_info.get("ano", "unknown"))

        downloaded: list[Path] = []
        for ref in refs:
            page_num = ref["pageNumber"]
            uuid = ref["dataRef"]
            output_path = output_dir / f"{foja}_{numero}_{ano}_page{page_num}.jpg"
            downloaded.append(self.download_image(uuid, output_path))

        pdf_path = output_dir / f"{foja}_{numero}_{ano}.pdf"
        create_pdf(downloaded, pdf_path)

        if not keep_images:
            for path in downloaded:
                path.unlink(missing_ok=True)

        return pdf_path

    def _search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        result = self.client.post_json(
            "/api/v1/comercio/indice/texto",
            body,
            captcha_action="indice_com_texto",
            include_recaptcha_in_body=True,
            context="commerce search",
        )
        if not isinstance(result, list):
            raise RuntimeError("Search did not return a result list.")
        return result


def _safe_part(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return safe[:80] or "unknown"
