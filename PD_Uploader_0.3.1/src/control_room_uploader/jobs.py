"""Queue records independent of the selected UI tab and browser handles."""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4
from .config import load_csv, ConfigError
from .models import UploadRow

TERMINAL = {'Uploaded', 'Failed', 'Skipped', 'Cancelled', 'Outcome unknown'}

@dataclass
class RowState:
    row: UploadRow
    selected: bool
    status: str = 'Waiting'
    reason: str = ''
    safe_retry: bool = False
    images: dict[int, str] = field(default_factory=dict)
    fingerprints: dict[int, tuple[int, int]] = field(default_factory=dict)
    initial_error: str = ''

    @property
    def count(self) -> int:
        return len(self.images)

    def set_status(self, status: str, reason: str = '') -> None:
        self.status, self.reason = status, reason
        for slot in self.images:
            if self.images[slot] != 'Uploaded':
                self.images[slot] = status

    def upload_row(self):
        """Validate each pending file once; retain successful slots on a later retry."""
        paths = []
        reasons = []
        for slot, path in enumerate(self.row.image_paths, 1):
            if path is None or self.images.get(slot) == 'Uploaded':
                paths.append(None)
                continue
            single = replace(self.row, image_paths=tuple(path if i == slot else None for i in range(1, 7)))
            probe = RowState(single, True, fingerprints={slot: self.fingerprints[slot]} if slot in self.fingerprints else {})
            try:
                probe.preflight()
            except ConfigError as exc:
                self.images[slot] = 'Failed'
                reasons.append(str(exc))
                paths.append(None)
            else:
                self.images[slot] = 'Uploading'
                paths.append(path)
        self.reason = '; '.join(reasons)
        return replace(self.row, image_paths=tuple(paths))

    def upload_complete(self):
        for slot, status in self.images.items():
            if status == 'Uploading':
                self.images[slot] = 'Uploaded'
        self.status = 'Failed' if any(s == 'Failed' for s in self.images.values()) else 'Uploaded'
        self.safe_retry = self.status == 'Failed'

    def preflight(self) -> None:
        for slot, path in enumerate(self.row.image_paths, 1):
            if path is None:
                continue
            if path.suffix.lower() != ".jpg":
                raise ConfigError(f"Image {slot} must be a .jpg file. Start a new upload to change the mapping.")
            try:
                stat = path.stat()
                if slot in self.fingerprints and self.fingerprints[slot] != (stat.st_size, stat.st_mtime_ns):
                    raise ConfigError(f'Image {slot} changed after CSV load. Start a new upload to reload it.')
                with path.open('rb') as handle:
                    if handle.read(3) != b'\xff\xd8\xff':
                        raise ConfigError(f'Image {slot} is not a readable JPG.')
                self.fingerprints.setdefault(slot, (stat.st_size, stat.st_mtime_ns))
            except OSError as exc:
                raise ConfigError(f'Image {slot} is missing or unreadable. Fix the file before retrying.') from exc

@dataclass
class Job:
    path: Path
    rows: list[RowState]
    id: str = field(default_factory=lambda: uuid4().hex)
    prepared: bool = False
    state: str = 'Waiting'

    @classmethod
    def load(cls, path: Path) -> 'Job':
        config = load_csv(path, diagnose_images=True)
        rows = []
        for row in config.rows:
            if row.enabled and not all((row.tier, row.department, row.shift, row.metric)):
                raise ConfigError(f'Row {row.row_number}: Tier, Department, Shift and Metric are required.')
            state = RowState(row, row.enabled and row.image_count > 0)
            for slot, image in enumerate(row.image_paths, 1):
                if image is not None:
                    state.images[slot] = 'Waiting'
                    try:
                        stat = image.stat()
                        state.fingerprints[slot] = (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        pass
            errors = [reason for (number, _), reason in config.image_errors.items() if number == row.row_number]
            state.initial_error = '; '.join(errors)
            state.reason = state.initial_error
            rows.append(state)
        return cls(path.expanduser().resolve(), rows)

    @property
    def selected_count(self) -> int:
        return sum(r.count for r in self.rows if r.selected)

    @property
    def successful_count(self) -> int:
        return sum(sum(s == 'Uploaded' for s in r.images.values()) for r in self.rows if r.selected)
