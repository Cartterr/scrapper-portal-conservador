import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbrs_portal.artifacts import commerce_stem, create_pdf, safe_filename


class ArtifactTests(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(safe_filename("a/b:c d"), "a_b_c_d")
        self.assertEqual(commerce_stem(63244, 27964, 2022), "63244_27964_2022")

    def test_create_pdf_orders_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p2 = root / "doc_page2.png"
            p1 = root / "doc_page1.png"
            Image.new("RGB", (10, 10), "white").save(p2)
            Image.new("RGB", (10, 10), "black").save(p1)
            record = create_pdf([p2, p1], root / "out.pdf")
            self.assertEqual(record.content_type, "application/pdf")
            self.assertEqual(record.page_count, 2)
            self.assertGreater(record.bytes, 0)


if __name__ == "__main__":
    unittest.main()
