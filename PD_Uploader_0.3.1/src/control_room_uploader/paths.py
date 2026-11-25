from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "PDUploader031"


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def browser_profile_dir() -> Path:
    return app_data_dir() / "browser-profile"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def resource_path(relative_path: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return root / relative_path
