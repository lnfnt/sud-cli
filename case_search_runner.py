#!/usr/bin/env python3
"""Fail-closed compatibility runner for the removed external binary."""

import sys
from runner_protocol import RunnerResponse, response_json


if __name__ == "__main__":
    print(response_json(RunnerResponse("error", data=None,
                                       reason="live runner unavailable"))
          .replace('"data": null', '"data": null, "snapshot": null'))
    raise SystemExit(3)
