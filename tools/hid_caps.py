"""List every numpad HID collection (USB 320F:5088 or BLE 093A:2860) with its report sizes."""
import ctypes
from ctypes import wintypes

import hid

hid_dll = ctypes.WinDLL("hid")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InputReportByteLength", ctypes.c_ushort), ("OutputReportByteLength", ctypes.c_ushort),
                ("FeatureReportByteLength", ctypes.c_ushort), ("Reserved", ctypes.c_ushort * 17),
                ("rest", ctypes.c_ushort * 10)]


def caps(path):
    h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)  # no access rights needed to query caps
    if h in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        pp = ctypes.c_void_p()
        if not hid_dll.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            return None
        c = HIDP_CAPS()
        hid_dll.HidP_GetCaps(pp, ctypes.byref(c))
        hid_dll.HidD_FreePreparsedData(pp)
        return c
    finally:
        k32.CloseHandle(h)


for d in hid.enumerate():
    if (d["vendor_id"], d["product_id"]) not in ((0x320F, 0x5088), (0x093A, 0x2860)):
        continue
    c = caps(d["path"].decode())
    sizes = f"in={c.InputReportByteLength} out={c.OutputReportByteLength} feat={c.FeatureReportByteLength}" if c else "caps n/a"
    print(f"{d['vendor_id']:04x}:{d['product_id']:04x} up={d['usage_page']:04x} u={d['usage']:04x} {sizes}")


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", ctypes.c_ushort), ("ReportID", ctypes.c_ubyte), ("rest", ctypes.c_ubyte * 69)]


def report_ids(path, report_type):
    """report_type: 0 input, 1 output, 2 feature -> sorted report IDs used by value caps."""
    h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)
    pp = ctypes.c_void_p()
    hid_dll.HidD_GetPreparsedData(h, ctypes.byref(pp))
    n = ctypes.c_ushort(64)
    arr = (HIDP_VALUE_CAPS * 64)()
    hid_dll.HidP_GetValueCaps(report_type, arr, ctypes.byref(n), pp)
    hid_dll.HidD_FreePreparsedData(pp)
    k32.CloseHandle(h)
    return sorted({arr[i].ReportID for i in range(n.value)})


if __name__ == "__main__":
    print("report IDs:")
    for d in hid.enumerate(0x093A, 0x2860):
        p = d["path"].decode()
        print(f"  up={d['usage_page']:04x} u={d['usage']:04x} in={report_ids(p, 0)} out={report_ids(p, 1)} feat={report_ids(p, 2)}")
