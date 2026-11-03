# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""VS RF adapters (PDF import mode)."""

from .parser import (VSRF_PARSER_VERSION, VsrfImportProvider, VsrfRecord,
                     detect_challenge, extract_pdf_text, extract_pdf_text_info,
                     fetch_vsrf_pdf, parse_vsrf_pdf_text)

__all__ = ["VSRF_PARSER_VERSION", "VsrfImportProvider", "VsrfRecord",
           "detect_challenge", "extract_pdf_text", "extract_pdf_text_info",
           "fetch_vsrf_pdf", "parse_vsrf_pdf_text"]
