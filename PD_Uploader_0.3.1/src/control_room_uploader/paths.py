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

def hide_internal_directory() -> bool:
    """Keep the bundled support folder out of the normal Windows folder view."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return False
    directory = Path(sys.executable).resolve().parent / '_internal'
    if not directory.is_dir():
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel.GetFileAttributesW.restype = wintypes.DWORD
        kernel.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel.SetFileAttributesW.restype = wintypes.BOOL
        attributes = kernel.GetFileAttributesW(str(directory))
        if attributes == 0xFFFFFFFF:
            return False
        return bool(kernel.SetFileAttributesW(str(directory), attributes | 0x2))
    except OSError:
        return False

