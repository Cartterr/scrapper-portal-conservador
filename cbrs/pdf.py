from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

PAGE_RE = re.compile(r"(?:^|[_-])page[_-]?(\d+)(?:\D|$)", re.IGNORECASE)


def page_number_from_path(path: Path) -> int:
    match = PAGE_RE.search(path.name)
    if not match:
        raise ValueError(f"Cannot determine page number from filename: {path.name}")
    return int(match.group(1))


def sort_page_images(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda path: (page_number_from_path(path), path.name))


def create_pdf(image_paths: list[Path], pdf_path: Path) -> Path:
    sorted_paths = sort_page_images(image_paths)
    if not sorted_paths:
        raise ValueError("No images to assemble into PDF")

    images = []
    for path in sorted_paths:
        with Image.open(path) as image:
            images.append(image.convert("RGB"))

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    first, *rest = images
    first.save(pdf_path, "PDF", save_all=True, append_images=rest)

    for image in images:
        image.close()

    return pdf_path
