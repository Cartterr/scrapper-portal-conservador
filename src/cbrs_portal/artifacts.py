from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class ArtifactRecord:
    path: Path
    sha256: str
    bytes: int
    content_type: str
    page_count: int | None = None


def safe_filename(value: object, *, fallback: str = "artifact") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^\w.\-]+", "_", text, flags=re.ASCII)
    text = text.strip("._")
    return text[:140] or fallback


def commerce_stem(foja: object, numero: object, ano: object) -> str:
    return "_".join(
        [
            safe_filename(foja, fallback="foja"),
            safe_filename(numero, fallback="numero"),
            safe_filename(ano, fallback="ano"),
        ]
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_image_bytes(data: bytes, output_path: Path, *, content_type: str) -> ArtifactRecord:
    if not content_type.lower().startswith("image/"):
        raise ValueError(f"Expected image content-type, got {content_type!r}")
    if not data:
        raise ValueError("Cannot save empty image response")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return ArtifactRecord(
        path=output_path,
        sha256=sha256_file(output_path),
        bytes=output_path.stat().st_size,
        content_type=content_type,
        page_count=1,
    )


def create_pdf(image_paths: list[Path], pdf_path: Path) -> ArtifactRecord:
    if not image_paths:
        raise ValueError("No images to assemble into PDF")
    sorted_paths = sorted(image_paths, key=_page_sort_key)
    images = []
    try:
        for path in sorted_paths:
            with Image.open(path) as img:
                images.append(img.convert("RGB"))
        first, *rest = images
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        first.save(pdf_path, "PDF", save_all=True, append_images=rest)
    finally:
        for image in images:
            image.close()
    return ArtifactRecord(
        path=pdf_path,
        sha256=sha256_file(pdf_path),
        bytes=pdf_path.stat().st_size,
        content_type="application/pdf",
        page_count=len(sorted_paths),
    )


def _page_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(?:page|pagina|p)[_-]?(\d+)", path.stem, re.IGNORECASE)
    return (int(match.group(1)) if match else 0, path.name)
