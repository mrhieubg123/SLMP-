# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main_V1.0.py'],
    pathex=[],
    binaries=[],
    datas=[('runtime_config.json', '.'), ('connect.json', '.'), ('config_oracle.json', '.'), ('config_api.json', '.'), ('config_group.json', '.')],
    hiddenimports=['connectDB', 'connectAPI', 'connectSQL', 'Gui_main', 'requests', 'oracledb', 'pyodbc', 'pymcprotocol'],
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
    name='PLC Machine RB Test Status hieu V1.4.0',
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
)
