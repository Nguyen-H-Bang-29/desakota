"""Filesystem layout for the data tree.

Keeps absolute paths out of notebooks. The root defaults to `data/` beside the
repo, and is overridable via the `DESAKOTA_DATA_DIR` environment variable so
the working set can live on native ext4 while the repo sits on `/mnt/c` — a
9P round trip per file is roughly an order of magnitude slower than ext4, which
matters once you are globbing raster tiles or large vector layers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DATA_DIR_ENV_VAR: Final[str] = 'DESAKOTA_DATA_DIR'

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
"""Repo root — the directory containing `pyproject.toml`."""


@dataclass(slots=True, frozen=True)
class DataLayout:
    """The three-stage data tree.

    Stages are append-only in one direction: `raw` is written only by fetchers,
    once per query (sources like OSM change daily, so it can't be regenerated),
    `interim` holds reproducible intermediates, `processed` holds analysis-ready
    outputs. Anything in `interim` or `processed` must be rebuildable from `raw`.

    Attributes:
        root: Directory containing the `raw`, `interim`, and `processed` stages.
    """

    root: Path

    @classmethod
    def from_env(cls) -> DataLayout:
        """Build the layout from `DESAKOTA_DATA_DIR`, falling back to `data/`.

        Returns:
            A layout rooted at the resolved absolute path.
        """
        override = os.environ.get(DATA_DIR_ENV_VAR)
        root = Path(override) if override else PROJECT_ROOT / 'data'
        return cls(root=root.expanduser().resolve())

    @property
    def raw(self) -> Path:
        """Immutable source data exactly as downloaded."""
        return self.root / 'raw'

    @property
    def interim(self) -> Path:
        """Reproducible intermediates — safe to delete and regenerate."""
        return self.root / 'interim'

    @property
    def processed(self) -> Path:
        """Analysis-ready outputs consumed by notebooks and figures."""
        return self.root / 'processed'

    def ensure(self) -> DataLayout:
        """Create every stage directory if missing.

        Returns:
            Self, so the call chains at the top of a notebook.
        """
        for stage in (self.raw, self.interim, self.processed):
            stage.mkdir(parents=True, exist_ok=True)
        return self
