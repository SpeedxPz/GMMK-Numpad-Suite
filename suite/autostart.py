"""Start with Windows: a per-user Run registry entry (HKCU, no admin needed). Only changed on request."""
import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "GMMK Numpad Suite"


def command():
    if getattr(sys, "frozen", False):  # packaged GMMK Numpad Suite.exe (PyInstaller)
        return f'"{sys.executable}" --no-browser'
    # pythonw.exe runs without a console window
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    return f'"{exe}" -I "{app}" --no-browser'


def is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, NAME)
            except FileNotFoundError:
                pass
