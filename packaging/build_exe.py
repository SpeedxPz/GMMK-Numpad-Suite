"""Build dist/GMMK Numpad Suite.exe: one windowed executable, no Python or conda needed.

    python packaging/build_exe.py

The dashboard (suite/web, fonts included) is bundled; settings stay in %APPDATA%\\GMMK Numpad Suite.
The firmware images (stock GMMK_Numpad_88.hex in the repo root, add-on _re/fw/build/app_suite.bin from
python fw/build.py) are bundled when present. Without them the exe works, but its Settings tab can't
update the firmware.
"""
import os
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE, TOOLS = os.path.join(ROOT, "suite"), os.path.join(ROOT, "tools")
BUILD = os.path.join(ROOT, "_build")
ICON = os.path.join(BUILD, "suite.ico")
# stock firmware (not in git: downloaded by CI) and the add-on built from it by fw/build.py
FIRMWARE = [os.path.join(ROOT, "GMMK_Numpad_88.hex"), os.path.join(ROOT, "_re", "fw", "build", "app_suite.bin")]


def make_icon():
    sys.path.insert(0, SUITE)
    from app import make_icon as draw  # the same picture as the tray icon
    os.makedirs(BUILD, exist_ok=True)
    draw().save(ICON, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])


def main():
    make_icon()
    missing = [f for f in FIRMWARE if not os.path.exists(f)]
    if missing:
        print("WARNING: building without firmware images (missing: " + ", ".join(missing) + ")")
    firmware = [] if missing else [a for f in FIRMWARE for a in ("--add-data", f"{f}{os.pathsep}firmware")]
    PyInstaller.__main__.run([
        os.path.join(SUITE, "app.py"),
        "--name", "GMMK Numpad Suite",
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--icon", ICON,
        "--paths", SUITE, "--paths", TOOLS,
        "--add-data", f"{os.path.join(SUITE, 'web')}{os.pathsep}web",
        *firmware,  # the dashboard's install / restore (suite/firmware.py looks in firmware/)
        # imported lazily or by name at runtime, so the analysis can't see them
        "--hidden-import", "pystray._win32",
        "--hidden-import", "obsws_python",
        "--hidden-import", "pycaw.pycaw",
        "--hidden-import", "comtypes.stream",
        "--hidden-import", "winrt.windows.devices.bluetooth",
        "--hidden-import", "winrt.windows.storage.streams",
        "--hidden-import", "winrt.windows.media.control",
        "--hidden-import", "winrt.windows.foundation",
        "--hidden-import", "winrt.windows.foundation.collections",
        "--hidden-import", "winrt.windows.devices.bluetooth.genericattributeprofile",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(BUILD, "work"),
        "--specpath", BUILD,
    ])


if __name__ == "__main__":
    main()
