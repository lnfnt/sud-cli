"""OCR fallback tests — mocked RapidOCR, no model download/network."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.errors import EmptyPayloadError  # noqa: E402
from case_search.vsrf.parser import (  # noqa: E402
    extract_pdf_text,
    extract_pdf_text_info,
)


def _make_pdf_with_text(path: Path, text: str) -> None:
    import pymupdf  # noqa: E402
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), text, fontsize=12)
    doc.save(str(path))
    doc.close()


def _make_empty_pdf(path: Path) -> None:
    import pymupdf  # noqa: E402
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.save(str(path))
    doc.close()


class OcrFallbackTests(unittest.TestCase):
    """Exercise the OCR fallback path used by case_search.vsrf.parser."""

    def test_pdf_with_text_layer_does_not_invoke_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "text.pdf"
            _make_pdf_with_text(pdf, "Hello from the text layer")
            fake_engine = mock.MagicMock()
            with mock.patch("case_search.ocr._OCR_ENGINE", fake_engine):
                text = extract_pdf_text(pdf)
            self.assertIn("Hello from the text layer", text)
            fake_engine.assert_not_called()

    def test_empty_pdf_with_mocked_ocr_returns_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "scan.pdf"
            _make_empty_pdf(pdf)
            fake_engine = mock.MagicMock(
                return_value=([
                    [[[0.0, 0.0], [100.0, 0.0], [100.0, 20.0], [0.0, 20.0]],
                     "OCR recovered text", 0.99],
                ], None),
            )
            with mock.patch("case_search.ocr._OCR_ENGINE", fake_engine):
                text = extract_pdf_text(pdf)
            self.assertIn("OCR recovered text", text)
            fake_engine.assert_called()

    def test_empty_pdf_when_ocr_unavailable_raises_empty_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "scan.pdf"
            _make_empty_pdf(pdf)
            with mock.patch("case_search.ocr.ocr_available",
                            return_value=False):
                with self.assertRaises(EmptyPayloadError):
                    extract_pdf_text(pdf)


class ExtractPdfTextInfoTests(unittest.TestCase):
    """Observability: extract_pdf_text_info reports how text was obtained."""

    def test_text_layer_pdf_reports_no_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "text.pdf"
            _make_pdf_with_text(pdf, "Hello from the text layer")
            fake_engine = mock.MagicMock()
            with mock.patch("case_search.ocr._OCR_ENGINE", fake_engine):
                text, info = extract_pdf_text_info(pdf)
            self.assertIn("Hello from the text layer", text)
            self.assertTrue(info["text_layer"])
            self.assertFalse(info["ocr_used"])
            self.assertIsNone(info["ocr_backend"])
            self.assertEqual(info["ocr_error"], "")
            fake_engine.assert_not_called()

    def test_scan_pdf_with_mocked_ocr_reports_ocr_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "scan.pdf"
            _make_empty_pdf(pdf)
            fake_engine = mock.MagicMock(
                return_value=([
                    [[[0.0, 0.0], [100.0, 0.0], [100.0, 20.0], [0.0, 20.0]],
                     "OCR recovered text", 0.99],
                ], None),
            )
            with mock.patch("case_search.ocr._OCR_ENGINE", fake_engine):
                text, info = extract_pdf_text_info(pdf)
            self.assertIn("OCR recovered text", text)
            self.assertFalse(info["text_layer"])
            self.assertTrue(info["ocr_used"])
            self.assertEqual(info["ocr_backend"], "rapidocr_onnxruntime")
            self.assertEqual(info["ocr_error"], "")
            fake_engine.assert_called()

    def test_scan_pdf_when_ocr_unavailable_is_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "scan.pdf"
            _make_empty_pdf(pdf)
            with mock.patch("case_search.ocr.ocr_available",
                            return_value=False):
                with self.assertRaises(EmptyPayloadError) as ctx:
                    extract_pdf_text_info(pdf)
            self.assertIn("OCR unavailable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
