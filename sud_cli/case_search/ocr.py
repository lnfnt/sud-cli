"""OCR fallback for scan-PDFs that have no extractable text layer.

Backend: rapidocr-onnxruntime (no tesseract binary required). The OCR engine
is initialised lazily as a module-level singleton because construction loads
ONNX models and takes ~0.5s.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .errors import EmptyPayloadError, OcrUnavailableError

logger = logging.getLogger(__name__)

_RAPIDOCR_AVAILABLE: bool | None = None
_OCR_ENGINE: Any | None = None


def ocr_available() -> bool:
    """Return True if the RapidOCR backend can be imported."""
    global _RAPIDOCR_AVAILABLE
    if _RAPIDOCR_AVAILABLE is None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            _RAPIDOCR_AVAILABLE = True
        except Exception as exc:  # pragma: no cover
            logger.debug("rapidocr_onnxruntime not importable: %s", exc)
            _RAPIDOCR_AVAILABLE = False
    return _RAPIDOCR_AVAILABLE


def _get_engine() -> Any:
    """Lazy singleton initializer for the RapidOCR engine."""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_ENGINE = RapidOCR()
        except Exception as exc:
            logger.exception("RapidOCR engine initialisation failed")
            raise OcrUnavailableError(
                "vsrf-import", f"RapidOCR engine failed: {exc}") from exc
    return _OCR_ENGINE


def _page_text_from_result(result: list[Any] | None) -> str:
    """Join OCR result lines ordered top-to-bottom."""
    if not result:
        return ""
    # result entries: [box, text, score]. Sort by top-left Y coordinate.
    try:
        result = sorted(
            result,
            key=lambda r: r[0][0][1] if isinstance(r[0], (list, tuple)) else 0,
        )
    except Exception:
        pass
    return "\n".join(str(r[1]) for r in result if len(r) >= 2 and r[1])


def extract_text_with_ocr_info(
    pdf_path: Path,
    *,
    max_pages: int = 20,
    dpi: int = 200,
    lang_hint: str = "ru",
) -> tuple[str, dict[str, Any]]:
    """Rasterize *pdf_path* and extract text via RapidOCR.

    Returns the extracted text together with an observation dict.  The dict is
    always present and never raises OCR-related exceptions; failures are
    described in ``ocr_error`` so callers can distinguish "backend missing",
    "runtime failure" and "genuinely empty".

    Args:
        pdf_path: Path to the PDF file.
        max_pages: Maximum number of pages to process.
        dpi: Resolution used when rasterizing each page.
        lang_hint: Reserved for language selection (RapidOCR ONNXRuntime uses
            a single multilingual model, so this is currently stored but not
            passed to the engine).

    Returns:
        Tuple of (text, info) where info has keys:
        - ``ocr_used`` (bool): True when OCR ran and produced non-empty text.
        - ``ocr_backend`` (str | None): Backend identifier, e.g.
          ``"rapidocr_onnxruntime"`` when available.
        - ``ocr_pages`` (int): Number of pages actually rasterized/OCR'd.
        - ``ocr_error`` (str): Empty on success, otherwise the failure reason.
    """
    del lang_hint  # model is multilingual; kept for API compatibility
    info: dict[str, Any] = {
        "ocr_used": False,
        "ocr_backend": None,
        "ocr_pages": 0,
        "ocr_error": "",
    }

    if not ocr_available():
        info["ocr_error"] = "RapidOCR backend is not installed"
        return "", info

    info["ocr_backend"] = "rapidocr_onnxruntime"

    try:
        import pymupdf
    except Exception as exc:  # pragma: no cover
        info["ocr_error"] = f"pymupdf unavailable: {exc}"
        return "", info

    try:
        engine = _get_engine()
    except OcrUnavailableError as exc:
        info["ocr_error"] = exc.detail if exc.detail else str(exc)
        return "", info

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        info["ocr_error"] = f"cannot open PDF for OCR: {exc}"
        return "", info

    page_texts: list[str] = []
    try:
        for page_index, page in enumerate(doc):
            if page_index >= max_pages:
                logger.warning(
                    "OCR truncated at %d pages for %s", max_pages, pdf_path)
                break
            info["ocr_pages"] = page_index + 1
            try:
                pixmap = page.get_pixmap(dpi=dpi)
                image_bytes = pixmap.tobytes("png")
            except Exception as exc:
                info["ocr_error"] = (
                    f"page {page_index + 1} rasterisation failed: {exc}")
                return "", info

            try:
                result, _ = engine(image_bytes)
            except Exception as exc:
                info["ocr_error"] = (
                    f"OCR engine failed on page {page_index + 1}: {exc}")
                return "", info

            page_text = _page_text_from_result(result)
            if page_text:
                page_texts.append(page_text)
    finally:
        doc.close()

    text = "\n".join(page_texts).strip()
    if text:
        info["ocr_used"] = True
    else:
        info["ocr_error"] = "OCR returned no text"
    return text, info


def _raise_for_ocr_info(info: dict[str, Any]) -> None:
    """Raise the same exceptions ``extract_text_with_ocr`` raised previously.

    Kept as a private helper so the public wrapper stays a thin one-liner.
    """
    error = info.get("ocr_error", "")
    if error == "RapidOCR backend is not installed":
        raise OcrUnavailableError("vsrf-import", error)
    if error.startswith("pymupdf unavailable"):
        raise OcrUnavailableError("vsrf-import", error)
    if error.startswith("cannot open PDF"):
        raise EmptyPayloadError("vsrf-import", error)
    if error.startswith("page") and "rasterisation failed" in error:
        raise OcrUnavailableError("vsrf-import", error)
    if error.startswith("OCR engine failed"):
        raise OcrUnavailableError("vsrf-import", error)
    if error == "OCR returned no text":
        raise EmptyPayloadError("vsrf-import", error)
    if error:
        # _get_engine failures and any future unknown error category.
        raise OcrUnavailableError("vsrf-import", error)


def extract_text_with_ocr(
    pdf_path: Path,
    *,
    max_pages: int = 20,
    dpi: int = 200,
    lang_hint: str = "ru",
) -> str:
    """Rasterize *pdf_path* and extract text via RapidOCR.

    This is the backward-compatible wrapper around
    :func:`extract_text_with_ocr_info`: it returns only the text and raises
    on failure.

    Args:
        pdf_path: Path to the PDF file.
        max_pages: Maximum number of pages to process.
        dpi: Resolution used when rasterizing each page.
        lang_hint: Reserved for language selection.

    Returns:
        Extracted text joined by newlines across pages.

    Raises:
        OcrUnavailableError: If RapidOCR is not importable or the engine
            fails at initialisation/runtime.
        EmptyPayloadError: If the PDF cannot be opened or OCR yields no text.
    """
    text, info = extract_text_with_ocr_info(
        pdf_path, max_pages=max_pages, dpi=dpi, lang_hint=lang_hint)
    if info["ocr_error"]:
        _raise_for_ocr_info(info)
    return text


__all__ = ["ocr_available", "extract_text_with_ocr", "extract_text_with_ocr_info"]
