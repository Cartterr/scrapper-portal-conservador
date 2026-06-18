from pathlib import Path

import pytest
from PIL import Image

from cbrs.pdf import create_pdf, page_number_from_path, sort_page_images


def _jpg(path: Path) -> Path:
    Image.new("RGB", (10, 10), color="white").save(path, "JPEG")
    return path


def test_page_number_from_path() -> None:
    assert page_number_from_path(Path("632_100_2024_page12.jpg")) == 12


def test_sort_page_images_naturally(tmp_path: Path) -> None:
    page10 = _jpg(tmp_path / "doc_page10.jpg")
    page1 = _jpg(tmp_path / "doc_page1.jpg")
    page2 = _jpg(tmp_path / "doc_page2.jpg")

    assert sort_page_images([page10, page1, page2]) == [page1, page2, page10]


def test_create_pdf(tmp_path: Path) -> None:
    page1 = _jpg(tmp_path / "doc_page1.jpg")
    page2 = _jpg(tmp_path / "doc_page2.jpg")
    pdf_path = create_pdf([page2, page1], tmp_path / "out.pdf")

    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


def test_missing_page_number_raises(tmp_path: Path) -> None:
    bad = _jpg(tmp_path / "doc.jpg")

    with pytest.raises(ValueError):
        sort_page_images([bad])
