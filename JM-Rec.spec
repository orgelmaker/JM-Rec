# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['qrcode.image.svg']
hiddenimports += collect_submodules('qrcode')
hiddenimports += collect_submodules('soundcard')
hiddenimports += ['audioop', 'audioop_lts']
hiddenimports += collect_submodules('jaraco')
hiddenimports += ['soundfile', '_soundfile_data']
if sys.platform == 'win32':
    hiddenimports += ['comtypes', 'comtypes.stream']

# lame.exe alleen op Windows meebundelen (elders doet ffmpeg de MP3-conversie)
_binaries = []
if sys.platform == 'win32' and os.path.exists('lame.exe'):
    _binaries.append(('lame.exe', '.'))

a = Analysis(
    ['jm_rec.py'],
    pathex=[],
    binaries=_binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pydub', 'tkinter', 'unittest', 'test', 'xmlrpc', 'pydoc',
        'doctest', 'ftplib', 'lib2to3', 'curses',
        'pip', 'ensurepip', 'sqlite3',
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
    [],
    exclude_binaries=True,
    name='JM-Rec',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='jm_rec_icon.ico' if sys.platform == 'win32' else None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='JM-Rec',
)
