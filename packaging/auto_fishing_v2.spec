# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


dxcam_datas, dxcam_binaries, dxcam_hiddenimports = collect_all('dxcam')
root = Path(SPECPATH).parent
manifest = str(root / 'packaging' / 'app.manifest')

a = Analysis(
    [str(root / 'src/auto_fishing/__main_v2__.py')],
    pathex=[str(root / 'src')],
    binaries=dxcam_binaries,
    datas=dxcam_datas,
    hiddenimports=dxcam_hiddenimports,
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
    name='异环自动钓鱼V2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=manifest,
    uac_admin=True,
)
