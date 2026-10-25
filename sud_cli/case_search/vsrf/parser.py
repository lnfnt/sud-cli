"""VS RF (vsrf.ru) PDF document import parser (saved file, no network).

vsrf.ru publishes Supreme Court acts as PDF files (постановления
Президиума, обзоры практики — verified live 2026-09-09). Text is
extracted with pymupdf; fail-closed on empty/unreadable text,
challenge markers, or missing case identity.

Fail-closed: a requested case_ref must appear in the extracted text
(full form, e.g. 'А40-217461/2024') or in a VS-internal form
('272-ПЭК25'). PII policy: parties are NOT extracted; only structural
fields (doc kind, court, judge composition head, case numbers).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import (EmptyPayloadError, MalformedPayloadError,
                      OcrUnavailableError, WafDetectedError)
from ..normalize import normalize
from ..normalize_refs import refs_match
from ..schema import CaseSnapshot, compute_raw_hash
from ..sources.base import RawSourceResult

VSRF_PARSER_VERSION = "vsrf-import-0.1.0"

logger = logging.getLogger(__name__)

DOC_KIND_RE = re.compile(
    r"ПОСТАНОВЛЕНИЕ|ОПРЕДЕЛЕНИЕ|РЕШЕНИЕ|ОБЗОР|ПРОТЕКОЛ|ЗАКЛЮЧЕНИЕ",
    re.IGNORECASE)
COURT_RE = re.compile(
    r"(ПРЕЗИДИУМА|ПЛЕНУМА|АПЕЛЛЯЦИОННОЙ|КАССАЦИОННОЙ|"
    r"СУДЕБНОЙ КОЛЛЕГИИ)[^\n]*ВЕРХОВНОГО СУДА", re.IGNORECASE)
CASE_NUM_RE = re.compile(r"[А-ЯA-Z0-9]{1,6}-\d{2,7}/\d{2,4}")
VS_CASE_RE = re.compile(r"Дело\s*№\s*([0-9А-ЯA-Z-]+)")
DOC_DATE_RE = re.compile(r"(\d{1,2}\s+\w+\s+\d{4})\s*г\.")

CHALLENGE_MARKERS = ("captcha", "доступ ограничен", "робот")


def detect_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def extract_pdf_text_info(pdf_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from a PDF and return it with provenance observations.

    The returned info dict has keys:
    - ``text_layer`` (bool): True when the PDF had an extractable text layer.
    - ``ocr_used`` (bool): True when the OCR fallback produced the returned
      text.
    - ``ocr_backend`` (str | None): OCR backend identifier, or None.
    - ``ocr_error`` (str): Empty on success, otherwise the OCR failure reason.
    """
    info: dict[str, Any] = {
        "text_layer": False,
        "ocr_used": False,
        "ocr_backend": None,
        "ocr_error": "",
    }
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise MalformedPayloadError(
            "vsrf-import", f"pymupdf unavailable: {exc}") from exc
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise MalformedPayloadError(
            "vsrf-import", f"cannot open PDF: {exc}") from exc
    try:
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    if text and text.strip():
        info["text_layer"] = True
        return text, info

    # No text layer: try OCR on scan PDFs.
    info["text_layer"] = False
    from ..ocr import extract_text_with_ocr_info, ocr_available
    if ocr_available():
        ocr_text, ocr_info = extract_text_with_ocr_info(pdf_path)
        info["ocr_used"] = ocr_info["ocr_used"]
        info["ocr_backend"] = ocr_info["ocr_backend"]
        info["ocr_error"] = ocr_info["ocr_error"]
        if ocr_text:
            return ocr_text, info
    else:
        info["ocr_error"] = "RapidOCR backend is not installed"

    raise EmptyPayloadError(
        "vsrf-import",
        f"no text layer and OCR unavailable ({info['ocr_error']})")


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF via pymupdf; fall back to OCR for scan PDFs."""
    text, _ = extract_pdf_text_info(pdf_path)
    return text


@dataclass(frozen=True)
class VsrfRecord:
    case_number: str
    title: str
    court: str
    judge: str
    doc_kind: str
    all_case_numbers: tuple[str, ...]
    warnings: tuple[str, ...]
    source: str
    source_url: str
    retrieved_at: str
    raw_artifact_hash: str

    def to_case_snapshot(self) -> CaseSnapshot:
        payload = {
            "case_number": self.case_number,
            "court": self.court,
            "judge": self.judge or "unknown",
            "status": self.title[:200],
            "participants": {},
            "next_hearing": None,
            "last_event": None,
            "events_timeline": [],
            "updated_at": self.retrieved_at[:10],
        }
        return normalize(payload, source=self.source,
                         source_url=self.source_url,
                         retrieved_at=self.retrieved_at,
                         raw_artifact_hash=self.raw_artifact_hash,
                         warnings=self.warnings,
                         parser_version=VSRF_PARSER_VERSION)


def parse_vsrf_pdf_text(text: str, *, case_ref: str, source: str,
                        source_url: str, retrieved_at: str,
                        raw_artifact_hash: str,
                        ocr_info: dict[str, Any] | None = None) -> VsrfRecord:
    """Parse extracted PDF text. Fail-closed, no PII extraction."""
    if not text or not text.strip():
        raise EmptyPayloadError(source)
    if detect_challenge(text):
        raise WafDetectedError(source, 200)

    kind_m = DOC_KIND_RE.search(text[:2000])
    if not kind_m:
        raise MalformedPayloadError(
            source, "no document kind marker (ПОСТАНОВЛЕНИЕ/ОПРЕДЕЛЕНИЕ/…)")
    doc_kind = kind_m.group(0).upper()

    court_m = COURT_RE.search(text[:2000])
    court = ("Верховный Суд РФ — "
             + court_m.group(0).strip().title()) if court_m else ""
    if not court:
        raise MalformedPayloadError(
            source, "no VS court body marker in document head")

    all_nums = tuple(dict.fromkeys(CASE_NUM_RE.findall(text)))[:20]
    vs_nums = tuple(dict.fromkeys(VS_CASE_RE.findall(text)))[:10]

    # Case identity: requested ref must be present (full form anywhere,
    # or a VS-internal case number).
    if not refs_match(case_ref, case_ref if case_ref in text else "") \
            and not any(refs_match(case_ref, v) for v in vs_nums) \
            and not any(refs_match(case_ref, n) for n in all_nums):
        raise MalformedPayloadError(
            source, f"case identity mismatch: '{case_ref}' not found "
                    f"in document (found: {all_nums[:5]})")

    warnings: list[str] = []
    if not all_nums and not vs_nums:
        raise MalformedPayloadError(source, "no case numbers in document")

    if ocr_info and ocr_info.get("ocr_used"):
        warnings.append("ocr-fallback-used")

    date_m = DOC_DATE_RE.search(text[:3000])
    title = f"{doc_kind.capitalize()} ВС РФ"
    if date_m:
        title += f" от {date_m.group(1)}"

    judge = ""
    pres_m = re.search(
        r"председательствующ(?:его|ий)\s*[–-]?\s*([А-ЯЁ][а-яё]+\s[А-ЯЁ]\.[А-ЯЁ]\.)",
        text)
    if pres_m:
        judge = pres_m.group(1)
    else:
        warnings.append("chairman judge not found in text")

    return VsrfRecord(
        case_number=case_ref,
        title=title,
        court=court,
        judge=judge,
        doc_kind=doc_kind,
        all_case_numbers=all_nums,
        warnings=tuple(warnings),
        source=source,
        source_url=source_url,
        retrieved_at=retrieved_at,
        raw_artifact_hash=raw_artifact_hash,
    )


class VsrfImportProvider:
    """Local import adapter: one saved vsrf.ru PDF file."""

    name = "vsrf-import"
    media_type = "application/pdf"
    accepts_fixtures = False

    def __init__(self, pdf_path: Path | str, case_ref: str):
        self.pdf_path = Path(pdf_path)
        self.case_ref = case_ref

    def lookup(self, case_ref: str) -> RawSourceResult:
        from ..errors import SourceUnavailableError
        if not refs_match(case_ref, self.case_ref):
            raise SourceUnavailableError(
                self.name,
                f"requested '{case_ref}' but import was staged for "
                f"'{self.case_ref}'")
        if not self.pdf_path.is_file():
            raise SourceUnavailableError(
                self.name, f"import file missing: {self.pdf_path}")
        body = self.pdf_path.read_bytes()
        import hashlib
        return RawSourceResult(
            source=f"{self.name}:{self.pdf_path.name}",
            source_url=f"file://{self.pdf_path.resolve()}",
            status_code=200,
            body="",
            media_type="application/pdf",
            metadata={"pdf_path": str(self.pdf_path),
                      "raw_sha256": hashlib.sha256(body).hexdigest()},
        )


def fetch_vsrf_pdf(provider, case_ref: str,
                   retrieved_at: str) -> FetchOutcome:
    """lookup -> classify -> parse -> FetchOutcome (vsrf PDF pipeline)."""
    from ..errors import CaseSearchError
    from ..pipeline import DEGRADED, ERROR, OK, FetchOutcome
    try:
        raw = provider.lookup(case_ref)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    if raw.media_type != "application/pdf":
        return FetchOutcome(ERROR, None,
                            "vsrf pipeline requires application/pdf")
    try:
        text, ocr_info = extract_pdf_text_info(Path(raw.metadata["pdf_path"]))
        if isinstance(raw.metadata, dict):
            raw.metadata["ocr"] = ocr_info
        record = parse_vsrf_pdf_text(
            text, case_ref=case_ref, source=raw.source,
            source_url=raw.source_url, retrieved_at=retrieved_at,
            raw_artifact_hash=raw.metadata["raw_sha256"],
            ocr_info=ocr_info)
        snapshot = record.to_case_snapshot()
    except WafDetectedError as exc:
        return FetchOutcome(DEGRADED, None, str(exc), exc.retry_after)
    except CaseSearchError as exc:
        return FetchOutcome(ERROR, None, str(exc))
    return FetchOutcome(OK, snapshot, "normalized")


__all__ = ["VsrfRecord", "VsrfImportProvider", "parse_vsrf_pdf_text",
           "fetch_vsrf_pdf", "extract_pdf_text", "extract_pdf_text_info",
           "detect_challenge", "VSRF_PARSER_VERSION"]
