"""Firmware add-on install / stock restore from the dashboard (wraps tools/fw_flash.py).

Flashing goes through Glorious's own bootloader over USB (see .knowledge/firmware-update.md): the
DeviceManager lets go of the numpad first, the app reboots into the bootloader, the pages are
written, and the numpad restarts. An interrupted update leaves the numpad in the bootloader, where
flashing again (or restoring stock) is safe; the ROM DFU pinhole is the last resort.

Disabled until server.FIRMWARE_ADDON["available"] is True (i.e. verified on real hardware).
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import fw_flash  # noqa: E402

if getattr(sys, "frozen", False):  # packaged exe: images bundled under firmware/
    BUNDLE = os.path.join(sys._MEIPASS, "firmware")
    ADDON_IMAGE = os.path.join(BUNDLE, "app_suite.bin")
    fw_flash.STOCK_HEX = os.path.join(BUNDLE, "GMMK_Numpad_88.hex")
else:
    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ADDON_IMAGE = os.path.join(ROOT, "_re", "fw", "build", "app_suite.bin")

# Builds without the firmware images (e.g. CI without the firmware secrets) can't update the firmware.
IMAGES_PRESENT = os.path.exists(ADDON_IMAGE) and os.path.exists(fw_flash.STOCK_HEX)


class FirmwareUpdater:
    def __init__(self, device, log=print):
        self.device, self.log = device, log
        self.job = {"state": "idle", "action": None, "step": None, "done": 0, "total": 0, "error": None}
        self._lock = threading.Lock()

    def start(self, action):
        """action: "install" (add-on) or "restore" (stock 0088). Runs in the background."""
        if action not in ("install", "restore"):
            raise ValueError("unknown firmware action")
        if self.device.status.get("transport") != "USB":
            raise ValueError("connect the numpad with the USB cable first (flashing doesn't work over Bluetooth)")
        if not IMAGES_PRESENT:
            raise ValueError("this build of the suite doesn't include the firmware images")
        with self._lock:
            if self.job["state"] == "running":
                raise ValueError("a firmware update is already running")
            self.job = {"state": "running", "action": action, "step": "preparing", "done": 0, "total": 0, "error": None}
        threading.Thread(target=self._run, args=(action,), daemon=True, name="firmware").start()

    def _progress(self, step, done, total):
        self.job.update(step=step, done=done, total=total)

    def _run(self, action):
        try:
            if action == "install":
                with open(ADDON_IMAGE, "rb") as f:
                    image = bytearray(f.read())
            else:
                image = fw_flash.stock_image()
            reports, total = fw_flash.pages(image)
            self.log(f"firmware: {action} started ({len(reports)} pages, checksum {total:#06x})")
            self.device.pause("firmware update")
            fw_flash.flash(reports, progress=self._progress)
            self.job.update(state="done", step="done")
            self.log(f"firmware: {action} finished")
        except Exception as e:
            self.job.update(state="error", error=str(e))
            self.log(f"firmware: {action} failed: {e}")
        finally:
            time.sleep(1.0)  # let Windows finish enumerating the restarted numpad
            self.device.resume()  # reconnects and re-detects stock / add-on
