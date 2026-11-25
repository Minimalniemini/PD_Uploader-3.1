"""Local, bounded diagnostics; no browser HTML, cookies or screenshots."""
from __future__ import annotations
import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .paths import app_data_dir, logs_dir


def sanitize(value: str) -> str:
    value = re.sub(r'https?://[^\s<>\"\']+', '[address removed]', str(value), flags=re.I)
    value = re.sub(r'''(?i)["']?\b(token|password|passwd|cookie|authorization|secret|api[_-]?key)\b["']?\s*[:=]\s*[^\r\n;]+''', r'\1=[removed]', value)
    value = re.sub(r'(?i)(?:[A-Z]:\\|/Users/|/home/|\\\\)[^\n\r;]+', '[path removed]', value)
    return value.replace('\x00', '')[:2000]

class Diagnostics:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or logs_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.logger = logging.Logger(f'pd-uploader-{id(self)}')
        handler = RotatingFileHandler(self.directory / 'activity.log', maxBytes=512_000, backupCount=6, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s', '%Y-%m-%d %H:%M:%S'))
        self.logger.addHandler(handler)

    def write(self, text: str) -> str:
        text = sanitize(text)
        self.logger.info(text)
        return text

    def checkpoint(self, snapshot: dict) -> None:
        # Deliberately excludes queue contents, filenames, paths, mappings and auth state.
        summary = {'phase': snapshot['phase'], 'total': snapshot['total'],
                   'done': snapshot['done'], 'elapsed': snapshot['elapsed'],
                   'results': [{'job': j.id, 'rows': [{'row': r.row.row_number, 'status': r.status,
                                                    'images': r.images} for r in j.rows if r.selected]}
                               for j in snapshot['jobs']]}
        temp = self.directory / 'last-result.tmp'
        temp.write_text(json.dumps(summary), encoding='utf-8')
        temp.replace(self.directory / 'last-result.json')

    def close(self) -> None:
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)


def picker_directory() -> str:
    try:
        return json.loads((app_data_dir() / 'preferences.json').read_text()).get('last_directory', '')
    except (OSError, ValueError, AttributeError):
        return ''


def remember_directory(path: Path) -> None:
    root = app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'preferences.json').write_text(json.dumps({'last_directory': str(path)}), encoding='utf-8')
