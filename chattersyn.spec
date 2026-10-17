# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller spec: onedir bundle, .app on macOS.

Hazards this spec exists to handle (see packaging notes):
- grpcio's TLS roots (grpc/_cython/_credentials/roots.pem) are missed by the
  hooks-contrib grpc hook -> explicit collect_data_files. Without them the
  gRPC secure_channel to youtube.googleapis.com fails to verify TLS.
- Lazy imports missed by static analysis: PySide6.QtMultimedia (highlight
  ping), google_auth_oauthlib.flow + google.auth.transport.requests (OAuth).
- keyring discovers backends via entry points, which freezing loses ->
  per-platform backend hiddenimports.
- Runtime data: chattersyn/assets/{ping.wav,emoji.json} are Path(__file__)
  relative -> datas entries so they resolve inside the bundle.
"""

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
SRC = ROOT / "src"

datas = [
    (str(SRC / "chattersyn" / "assets" / "ping.wav"), "chattersyn/assets"),
    (str(SRC / "chattersyn" / "assets" / "emoji.json"), "chattersyn/assets"),
]
# TLS roots for grpc secure_channel (hooks-contrib does not bundle them).
datas += collect_data_files("grpc", includes=["**/roots.pem"])

hiddenimports = [
    "PySide6.QtMultimedia",
    "google_auth_oauthlib.flow",
    "google.auth.transport.requests",
    "keyring.backends.chainer",
]
if sys.platform == "darwin":
    hiddenimports.append("keyring.backends.macOS")
    icon = str(ROOT / "packaging" / "icon.icns")
elif sys.platform.startswith("win"):
    hiddenimports.append("keyring.backends.Windows")
    icon = str(ROOT / "packaging" / "icon.ico")
else:
    hiddenimports.append("keyring.backends.SecretService")
    icon = str(ROOT / "packaging" / "icon.png")

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="chattersyn",
    console=False,
    icon=icon,
)

coll = COLLECT(exe, a.binaries, a.datas, name="chattersyn")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="chattersyn.app",
        icon=icon,
        bundle_identifier="com.chattersyn.app",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleDisplayName": "ChatterSync",
            "CFBundleName": "ChatterSync",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )
