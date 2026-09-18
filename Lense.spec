# -*- mode: python ; coding: utf-8 -*-


# Build with:  pyinstaller Lense.spec        (see build_windows.bat / build.sh)
#
# src/data ships whole: the equipment catalog JSON, the logo and .ico, the icons folder
# and the splash chime. utils.get_data_dir() resolves it to sys._MEIPASS/data when
# frozen, so nothing in the app needs to know whether it is running from source.
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('src/data', 'data')],
    # QtMultimedia is imported lazily inside main.py so a machine without it still
    # starts; naming it here makes sure the frozen build actually carries it, along
    # with the platform audio plugin the splash chime needs.
    hiddenimports=['PySide6.QtMultimedia'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Lense',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='src/data/Catmera.ico',
    # Windows-only metadata (Properties > Details); ignored elsewhere.
    version='version_info.txt',
)
