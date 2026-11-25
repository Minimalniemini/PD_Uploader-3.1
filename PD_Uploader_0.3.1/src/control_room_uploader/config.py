from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import UploadRow


REQUIRED_COLUMNS = (
    "enabled",
    "page_url",
    "tier",
    "department",
    "shift",
    "metric",
    "image_1",
    "image_2",
    "image_3",
    "image_4",
    "image_5",
    "image_6",
)

TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled"}
FALSE_VALUES = {"", "0", "false", "no", "n", "off", "disabled"}


class ConfigError(Exception):
    """Raised when the CSV cannot be used safely."""


@dataclass(frozen=True)
class ConfigResult:
    rows: list[UploadRow]
    warnings: list[str]
    image_errors: dict[tuple[int, int], str] = field(default_factory=dict)

    @property
    def enabled_rows(self) -> list[UploadRow]:
        return [row for row in self.rows if row.enabled]


def first_enabled_page_url(path: Path | str) -> tuple[str | None, list[str]]:
    csv_path = Path(path).expanduser()
    if not csv_path.exists():
        raise ConfigError(f"CSV file does not exist: {csv_path}")
    if not csv_path.is_file():
        raise ConfigError(f"CSV path is not a file: {csv_path}")

    warnings: list[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ConfigError("CSV file is empty or has no header row.")

        header_map = {column.strip().lower(): column for column in reader.fieldnames}
        missing = [column for column in ("enabled", "page_url") if column not in header_map]
        if missing:
            raise ConfigError("CSV is missing required columns: " + ", ".join(missing))

        for physical_row_number, raw_row in enumerate(reader, start=2):
            row = _normalise_row(raw_row)
            enabled = _parse_enabled(row.get("enabled", ""), physical_row_number)
            page_url = row.get("page_url", "")
            if enabled and page_url:
                return page_url, warnings
    return None, warnings


def load_csv(path: Path | str, *, diagnose_images: bool = False) -> ConfigResult:
    csv_path = Path(path).expanduser()
    if not csv_path.exists():
        raise ConfigError(f"CSV file does not exist: {csv_path}")
    if not csv_path.is_file():
        raise ConfigError(f"CSV path is not a file: {csv_path}")

    warnings: list[str] = []
    rows: list[UploadRow] = []
    image_errors: dict[tuple[int, int], str] = {}

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ConfigError("CSV file is empty or has no header row.")

        header_map = {column.strip().lower(): column for column in reader.fieldnames}
        missing = [column for column in REQUIRED_COLUMNS if column not in header_map]
        if missing:
            raise ConfigError("CSV is missing required columns: " + ", ".join(missing))

        for physical_row_number, raw_row in enumerate(reader, start=2):
            row = _normalise_row(raw_row)
            enabled = _parse_enabled(row.get("enabled", ""), physical_row_number)
            page_url = row.get("page_url", "")

            if enabled:
                paths = []
                for slot in range(1, 7):
                    raw = row.get(f"image_{slot}", "")
                    try:
                        image = _resolve_image_path(raw, csv_path.parent, physical_row_number, f"image_{slot}")
                    except ConfigError:
                        if not diagnose_images:
                            raise
                        # Retain the intended slot and selected denominator, even for a missing file.
                        value = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
                        image = Path(value)
                        if not image.is_absolute():
                            image = csv_path.parent / image
                        image_errors[(physical_row_number, slot)] = f"Image {slot} is missing, unreadable or not a .jpg file. This row was not sent."
                    paths.append(image)
                image_paths = tuple(paths)
            else:
                image_paths = (None, None, None, None, None, None)

            if enabled and not page_url:
                raise ConfigError(f"Row {physical_row_number}: page_url is required for enabled rows.")

            if enabled and all(path is None for path in image_paths):
                warnings.append(f"Row {physical_row_number}: no JPG files are set in image_1 to image_6.")

            rows.append(
                UploadRow(
                    row_number=physical_row_number,
                    enabled=enabled,
                    page_url=page_url,
                    tier=row.get("tier", ""),
                    department=row.get("department", ""),
                    shift=row.get("shift", ""),
                    metric=row.get("metric", ""),
                    image_paths=image_paths,
                )
            )

    if not rows:
        raise ConfigError("CSV has no data rows.")

    return ConfigResult(rows=rows, warnings=warnings, image_errors=image_errors)


def _normalise_row(raw_row: dict[str, str | None]) -> dict[str, str]:
    return {
        str(key).strip().lower(): (value or "").strip()
        for key, value in raw_row.items()
        if key is not None
    }


def _parse_enabled(value: str, row_number: int) -> bool:
    normalised = value.strip().lower()
    if normalised in TRUE_VALUES:
        return True
    if normalised in FALSE_VALUES:
        return False
    raise ConfigError(
        f"Row {row_number}: enabled must be yes/no, true/false, 1/0, enabled/disabled, or blank."
    )


def _resolve_image_path(raw_value: str, csv_dir: Path, row_number: int, column_name: str) -> Path | None:
    value = raw_value.strip().strip('"')
    if not value:
        return None

    expanded = os.path.expandvars(os.path.expanduser(value))
    image_path = Path(expanded)
    if not image_path.is_absolute():
        image_path = csv_dir / image_path

    if image_path.suffix.lower() != ".jpg":
        raise ConfigError(f"Row {row_number}, {column_name}: only .jpg files are allowed.")
    if not image_path.exists():
        raise ConfigError(f"Row {row_number}, {column_name}: JPG file does not exist: {image_path}")
    if not image_path.is_file():
        raise ConfigError(f"Row {row_number}, {column_name}: path is not a file: {image_path}")
    return image_path.resolve()
