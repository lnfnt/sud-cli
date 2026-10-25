"""Lookup of bundled historical editions of selected Russian codes."""

import json
from datetime import date
from pathlib import Path

DATA_PATH = Path(__file__).with_name("data") / "npa_historical_editions.json"


def lookup_as_of(act, article, on_date, path=None):
    try:
        target = date.fromisoformat(on_date) if isinstance(on_date, str) else on_date
        records = json.loads(Path(path or DATA_PATH).read_text(encoding="utf-8"))
        rows = records.get(act, {}).get(str(article), [])
        eligible = [row for row in rows if date.fromisoformat(row["effective_date"]) <= target]
        if not eligible:
            return {"status": "error", "detail": "historical edition not found for requested date"}
        row = max(eligible, key=lambda item: item["effective_date"])
        return {"status": "ok", "act": act, "article": str(article), **row}
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        return {"status": "error", "detail": str(exc)}
