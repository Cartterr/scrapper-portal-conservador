import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbrs_portal.errors import ErrorCode, classify_response


class ErrorClassificationTests(unittest.TestCase):
    def test_daily_limit(self):
        result = classify_response(400, {"code": "err-limite", "msg": "limit"})
        self.assertEqual(result.code, ErrorCode.DAILY_LIMIT)
        self.assertFalse(result.retryable)

    def test_captcha_rejection(self):
        result = classify_response(400, {"code": "intente-mas-tarde"})
        self.assertEqual(result.code, ErrorCode.CAPTCHA)
        self.assertTrue(result.retryable)

    def test_waf_headers(self):
        result = classify_response(403, {}, {"X-CDN": "Imperva"})
        self.assertEqual(result.code, ErrorCode.WAF)
        self.assertTrue(result.retryable)

    def test_rate_limit(self):
        result = classify_response(429, {})
        self.assertEqual(result.code, ErrorCode.RATE_LIMIT)
        self.assertFalse(result.retryable)

    def test_api_html_is_waf(self):
        result = classify_response(
            200,
            "<html><title>challenge</title></html>",
            {"content-type": "text/html"},
            endpoint="/api/v1/comercio/indice/texto",
        )
        self.assertEqual(result.code, ErrorCode.WAF)

    def test_auth_403_without_waf(self):
        result = classify_response(403, {})
        self.assertEqual(result.code, ErrorCode.AUTH)
        self.assertFalse(result.retryable)


if __name__ == "__main__":
    unittest.main()
