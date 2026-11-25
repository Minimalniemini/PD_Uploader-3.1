from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UploadRow:
    row_number: int
    enabled: bool
    page_url: str
    tier: str
    department: str
    shift: str
    metric: str
    image_paths: tuple[Path | None, Path | None, Path | None, Path | None, Path | None, Path | None]

    @property
    def image_count(self) -> int:
        return sum(1 for path in self.image_paths if path is not None)
