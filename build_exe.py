"""Build a standalone Windows app with the theme and headset helpers included."""

import ctypes
import os
from pathlib import Path
import sys

import PyInstaller.__main__


def refresh_windows_icons() -> None:
    """Tell Explorer to discard icons cached for a previous executable build."""
    notify = ctypes.WinDLL("shell32").SHChangeNotify
    notify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    notify.restype = None
    # SHCNE_ASSOCCHANGED, SHCNF_IDLIST | SHCNF_FLUSH; no file associations change.
    notify(0x08000000, 0x1000, None, None)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Build the Windows executable on Windows.")

    root = Path(__file__).resolve().parent
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(root / "build" / "cache"))
    options = [
        str(root / "main.py"),
        "--name", "Frame Connect",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--noupx",
        "--icon", str(root / "ICO" / "icon.ico"),
        "--add-data", f"{root / 'ICO' / 'icon.ico'}:ICO",
        "--distpath", str(root),
        "--workpath", str(root / "build" / "pyinstaller"),
        "--specpath", str(root / "build"),
    ]
    for filename in ("styles.qss", "probe.py", "remote_files.py", "steam_api.py", "remote_power.py"):
        options.extend(("--add-data", f"{root / filename}:."))
    PyInstaller.__main__.run(options)
    refresh_windows_icons()


if __name__ == "__main__":
    main()
