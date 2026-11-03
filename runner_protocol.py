# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Small JSON-line protocol kept for the legacy court watcher runner."""

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class RunnerRequest:
    case_ref: str
    source: str = "mos-gorsud"


@dataclass(frozen=True)
class RunnerResponse:
    status: str
    data: object = None
    reason: str = ""


def parse_request(raw: str) -> RunnerRequest:
    value = json.loads(raw)
    if not isinstance(value, dict) or not value.get("case_ref"):
        raise ValueError("case_ref is required")
    source = value.get("source", "mos-gorsud")
    if source != "mos-gorsud":
        raise ValueError("only mos-gorsud is supported")
    return RunnerRequest(value["case_ref"], source)


def response_json(response: RunnerResponse) -> str:
    return json.dumps({"status": response.status, "data": response.data,
                       "reason": response.reason}, ensure_ascii=False)
