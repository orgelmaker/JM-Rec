# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['qrcode.image.svg']
hiddenimports += collect_submodules('qrcode')
hiddenimports += collect_submodules('soundcard')
hiddenimports += ['comtypes', 'comtypes.stream']
hiddenimports += ['audioop', 'audioop_lts']


a = Analysis(
    ['jm_rec.py'],
    pathex=[],
    binaries=[('lame.exe', '.')],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pydub', 'tkinter', 'unittest', 'test', 'xmlrpc', 'pydoc',
        'doctest', 'ftplib', 'lib2to3', 'curses', 'distutils',
        'setuptools', 'pip', 'ensurepip', 'sqlite3',
        'scipy.spatial', 'scipy.optimize', 'scipy.integrate',
        'scipy.interpolate', 'scipy.stats', 'scipy.linalg',
        'scipy.sparse', 'scipy.special', 'scipy.cluster',
        'scipy.odr', 'scipy.ndimage',
        'numpy.testing', 'numpy.f2py', 'numpy.distutils',
    ],
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
    name='JM-Rec',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='jm_rec_icon.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
