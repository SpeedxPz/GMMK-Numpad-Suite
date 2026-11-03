"""Print F13-F24 and volume-key events seen by a low-level keyboard hook, for a while."""
import ctypes
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL, WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 13, 0x0100, 0x0104, 0x0012
NAMES = {0xAD: "VolumeMute", 0xAE: "VolumeDown", 0xAF: "VolumeUp",
         **{0x7C + i: f"F{13 + i}" for i in range(12)}}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE  # default int restype truncates the 64-bit handle


@HOOKPROC
def proc(n, wparam, lparam):
    if n >= 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        vk = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
        if vk in NAMES:
            print(time.strftime("%H:%M:%S"), NAMES[vk], flush=True)
    return user32.CallNextHookEx(None, n, wparam, lparam)


hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
if not hook:
    sys.exit(f"SetWindowsHookEx failed: {ctypes.get_last_error()}")
tid = kernel32.GetCurrentThreadId()
threading.Timer(float(sys.argv[1] if len(sys.argv) > 1 else 600),
                lambda: user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)).start()
print("hook installed", flush=True)
msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    pass
user32.UnhookWindowsHookEx(hook)
print("done", flush=True)
