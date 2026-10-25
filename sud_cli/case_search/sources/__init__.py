"""Source registry."""

from __future__ import annotations

from pathlib import Path

from .base import Provider
from .live import KadProvider, MosGorsudProvider, SudrfProvider
from .import_html import MosDomImportProvider
from .mos_dom import MosDomReplayProvider
from .synthetic import SyntheticProvider

REGISTRY: dict[str, type[Provider]] = {
    "synthetic": SyntheticProvider,
    "kad": KadProvider,
    "mos-gorsud": MosGorsudProvider,
    "mos-gorsud-dom": MosDomReplayProvider,
    "sudrf": SudrfProvider,
}


def build_provider(name: str, fixtures_dir: Path | str) -> Provider:
    try:
        cls = REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown source '{name}'; known: {sorted(REGISTRY)}") from exc
    if cls.accepts_fixtures:
        return cls(Path(fixtures_dir))
    return cls()
