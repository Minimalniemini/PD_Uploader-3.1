# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata

project = Path(SPECPATH)
datas, binaries, hiddenimports = collect_all('playwright')
datas += [(str(project / 'assets' / 'pd.ico'), 'assets'),
          (str(project / 'assets' / 'pd.png'), 'assets')]
datas += copy_metadata('PySide6_Essentials') + copy_metadata('shiboken6')
a = Analysis(
    [str(project / 'src/control_room_uploader/__main__.py')],
    pathex=[str(project / 'src')], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6.QtWebEngineCore',
              'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='PDUploader-0.3.1',
    icon=str(project / 'assets/pd.ico'), version=str(project / 'version_info.txt'),
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    upx_exclude=[], runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name='PDUploader-0.3.1')
