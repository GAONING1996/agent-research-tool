"""Offline tests for PDF detection and extraction in fetch_smart."""

from __future__ import annotations

import fitz
import subprocess
import unittest
from unittest.mock import patch

import fetch_smart


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/pdf", status_code: int = 200):
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def text_pdf() -> bytes:
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "PDF PAGE ONE: first known paragraph.")
    second = document.new_page()
    second.insert_text((72, 72), "PDF PAGE TWO: second known paragraph.")
    document.set_metadata({"title": "Known PDF title"})
    result = document.tobytes()
    document.close()
    return result


def image_pdf() -> bytes:
    source = fitz.open()
    page = source.new_page(width=612, height=792)
    page.insert_text((72, 100), "Scanned document text for OCR path.", fontsize=24)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    scanned = fitz.open()
    scanned_page = scanned.new_page(width=612, height=792)
    scanned_page.insert_image(scanned_page.rect, pixmap=pixmap)
    result = scanned.tobytes()
    source.close()
    scanned.close()
    return result


def repeated_text_pdf(page_count: int, page_text: str) -> bytes:
    document = fitz.open()
    for _ in range(page_count):
        page = document.new_page()
        for index, offset in enumerate(range(0, len(page_text), 65)):
            page.insert_text((10, 20 + index * 12), page_text[offset:offset + 65])
    result = document.tobytes()
    document.close()
    return result


class FetchPdfTests(unittest.TestCase):
    def setUp(self):
        fetch_smart._HTTP_CACHE.clear()

    def tearDown(self):
        fetch_smart._HTTP_CACHE.clear()

    def test_extracts_pdf_pages_in_order_and_uses_pdf_method(self):
        response = FakeResponse(text_pdf())
        with patch.object(fetch_smart.requests, "get", return_value=response):
            result = fetch_smart.fetch_smart("https://example.test/research", min_chars=1)

        self.assertEqual(result["method"], "pdf")
        self.assertEqual(result["title"], "Known PDF title")
        self.assertLess(result["text"].index("PDF PAGE ONE"), result["text"].index("PDF PAGE TWO"))
        self.assertIn("\n\n", result["text"])
        self.assertTrue(response.closed)

    def test_scanned_pdf_uses_ocr_path(self):
        response = FakeResponse(image_pdf())
        with patch.object(fetch_smart.requests, "get", return_value=response), patch.object(
            fetch_smart, "_ocr_pdf_pages", return_value=["Recognized scanned page text."]
        ) as ocr:
            result = fetch_smart.fetch_smart("https://example.test/scanned", min_chars=1)

        self.assertEqual(result["method"], "pdf-ocr")
        self.assertEqual(result["text"], "Recognized scanned page text.")
        ocr.assert_called_once()

    def test_ocr_timeout_returns_a_truncated_pdf_result(self):
        response = FakeResponse(image_pdf())
        with patch.object(fetch_smart.requests, "get", return_value=response), patch.object(
            fetch_smart,
            "_ocr_pdf_pages",
            side_effect=subprocess.TimeoutExpired("ocr", 1),
        ):
            result = fetch_smart.fetch_smart("https://example.test/slow-ocr", min_chars=1)

        self.assertEqual(result["method"], "pdf")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncate_reason"], "time_budget")

    def test_html_response_is_not_routed_to_pdf_parser(self):
        response = FakeResponse(
            b"<html><head><title>HTML page</title></head><body><main>Readable HTML body. "
            + b"More article text. " * 80
            + b"</main></body></html>",
            content_type="text/html",
        )
        with patch.object(fetch_smart.requests, "get", return_value=response), patch.object(
            fetch_smart.fitz, "open", side_effect=AssertionError("PDF parser must not run")
        ):
            result = fetch_smart.fetch_smart("https://example.test/not-a-pdf", min_chars=1)

        self.assertEqual(result["method"], "http")
        self.assertEqual(result["title"], "HTML page")

    def test_pdf_download_limit_caps_body_exactly(self):
        response = FakeResponse(b"%PDF-" + b"x" * 300, content_type="application/octet-stream")
        with patch.object(fetch_smart, "PDF_DOWNLOAD_LIMIT_BYTES", 100):
            body, is_pdf, truncate_reason = fetch_smart._read_http_body(
                response, fetch_smart.time.monotonic() + 5
            )

        self.assertTrue(is_pdf)
        self.assertEqual(len(body), 100)
        self.assertEqual(truncate_reason, "download_limit")

    def test_404_and_damaged_pdf_fall_back_without_uncaught_error(self):
        cases = (
            ("https://example.test/missing", FakeResponse(b"not found", "text/html", 404)),
            ("https://example.test/damaged", FakeResponse(b"%PDF-not a real document")),
        )
        for url, response in cases:
            fetch_smart._HTTP_CACHE.clear()
            with self.subTest(url=url), patch.object(fetch_smart.requests, "get", return_value=response), patch.object(
                fetch_smart, "_tavily_extract", return_value=("Fallback", "Readable fallback article. " * 80)
            ):
                result = fetch_smart.fetch_smart(url, min_chars=1)
            self.assertEqual(result["method"], "tavily")
            self.assertIn("Readable fallback article", result["text"])

    def test_same_url_is_downloaded_and_parsed_once(self):
        response = FakeResponse(text_pdf())
        with patch.object(fetch_smart.requests, "get", return_value=response) as get:
            first = fetch_smart._http_extract("https://example.test/cached", 10)
            second = fetch_smart._http_extract("https://example.test/cached", 10)

        self.assertEqual(first, second)
        get.assert_called_once()

    def test_page_and_character_limits_are_reported(self):
        fixtures = (
            ("page-limit", repeated_text_pdf(201, "Page text contains more than twenty valid characters."), "page_limit"),
            ("char-limit", repeated_text_pdf(130, "C" * 1000), "char_limit"),
        )
        for name, body, reason in fixtures:
            fetch_smart._HTTP_CACHE.clear()
            response = FakeResponse(body)
            with self.subTest(reason=reason), patch.object(fetch_smart.requests, "get", return_value=response):
                result = fetch_smart.fetch_smart(f"https://example.test/{name}", min_chars=1)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["truncate_reason"], reason)
            self.assertLessEqual(result["chars"], fetch_smart.PDF_CHAR_LIMIT)


if __name__ == "__main__":
    unittest.main()
