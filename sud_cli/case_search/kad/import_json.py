"""KAD import adapter: saved EXTRACT_EXPRESSION JSON -> RawSourceResult.

Import mode (no network): the caller saves the JSON produced by running
`case_search.kad.parser.EXTRACT_EXPRESSION` inside a browser page
(Camofox evaluate) to a local file, then stages it for one case_ref.
The parser pipeline (fetch_kad_html) re-parses and identity-checks the
payload exactly as in the live path — one code path, one validation.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import SourceUnavailableError
from ..normalize_refs import refs_match
from ..sources.base import RawSourceResult


class KadImportProvider:
    """Local import adapter: one saved KAD extract JSON file."""

    name = "kad-import"
    media_type = "application/json"
    accepts_fixtures = False

    def __init__(self, json_path: Path | str, case_ref: str):
        self.json_path = Path(json_path)
        self.case_ref = case_ref

    def lookup(self, case_ref: str) -> RawSourceResult:
        if not refs_match(case_ref, self.case_ref):
            raise SourceUnavailableError(
                self.name,
                f"requested '{case_ref}' but import was staged for "
                f"'{self.case_ref}'")
        if not self.json_path.is_file():
            raise SourceUnavailableError(
                self.name, f"import file missing: {self.json_path}")
        try:
            data = json.loads(
                self.json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            from ..errors import MalformedPayloadError
            raise MalformedPayloadError(
                self.name, f"import file is not valid JSON: {exc}") from exc
        # Browser evaluate may return the extract as a JSON-encoded string
        # (double-encoded); unwrap one level in that case.
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as exc:
                from ..errors import MalformedPayloadError
                raise MalformedPayloadError(
                    self.name,
                    f"import file string payload is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            from ..errors import MalformedPayloadError
            raise MalformedPayloadError(
                self.name, "import file must contain a JSON object")
        body = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return RawSourceResult(
            source=f"{self.name}:{self.json_path.name}",
            source_url=f"file://{self.json_path.resolve()}",
            status_code=200,
            body=body,
            media_type="text/html",
            metadata={"kad_data": data},
        )


__all__ = ["KadImportProvider"]
