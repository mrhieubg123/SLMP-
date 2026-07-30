# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main_V1.0.py'],
    pathex=[],
    binaries=[],
    datas=[('config_5.json', '.'), ('config_group.json', '.')],
    hiddenimports=['secrets', 'getpass', 'asyncio', 'uuid', 'ssl', 'platform', 'time', 'decimal', 'base64', 'hashlib', 'PyQt5.sip', 'pymcprotocol', 'oracledb', 'oracledb.base_impl', 'oracledb.thin_impl', 'oracledb.thick_impl'],
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
    name='PLC Machine Status V1.1',
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
