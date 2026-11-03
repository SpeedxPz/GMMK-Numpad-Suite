"""Patch and flash the GMMK Numpad's stock application through Glorious's own bootloader.

Everything below comes from the 0088 firmware image (see .knowledge/firmware-update.md):

  Bootloader (0x08000000, never written by this protocol)
    - boots the app at 0x08007000 unless:
        * flash 0x08006800 == 0x78563412 ("update pending", set by page 0, erased on a good checksum)
        * register 0x40010018 == 0x4A379CEB (bootloader requested by the app, see below)
        * Enter + Plus held while plugging in (Glorious; QMK readme)
    - shows up as USB 320F:B00D, usage page 0xFF01, feature report ID 7 with 2055 data bytes
    - report [1] = 0x32 ('2') writes app page [2] (2 KB at 0x08007000 + [2] * 0x800) from [8..2055];
      on the last page ([2] == [3]) the low 16 bits of the byte sum of everything sent are compared
      with [4..5] (big endian); a match clears the pending flag
    - report [1] = 0x30 ('0') ends the session (sets 0x2000003C = 0x5AA5A5A5)
  Application
    - command 0x30 (USB feature report 7: 07 30 ...) sets 0x20000218 = 0x5AA5A5A5; FUN_08012ce4 then
      shuts the LEDs/USB down, writes 0x4A379CEB to 0x40010018 and resets into the bootloader
  Recovery if all else fails: the WB32 ROM DFU bootloader (pinhole under the 0 key while plugging in)
  with wb32-dfu-updater_cli and the official GMMK_Numpad_88.hex. It cannot be overwritten.

Usage:
    python fw_flash.py build  [--patch NAME ...]            # dry run: build + verify pages, write .bin
    python fw_flash.py flash  [--patch NAME ...] --i-have-the-pinhole-fallback
    python fw_flash.py flash  --image _re/fw/build/app_suite.bin --i-have-the-pinhole-fallback
    python fw_flash.py info                                 # is our C code running? (command 0x60)
    python fw_flash.py list                                 # available patches

fw/build.py builds app_suite.bin (stock app + our C code + hooks).

Only `build` has been run so far; the device protocol is untested.
"""
import argparse
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_HEX = os.path.join(HERE, "..", "GMMK_Numpad_88.hex")
OUT_DIR = os.path.join(HERE, "..", "_re", "fw", "build")

FLASH_BASE = 0x08000000
APP_BASE = 0x08007000
PAGE = 0x800                  # bytes per update report
SETTINGS_FLOOR = 0x0801F000   # the app keeps settings around 0x0801F0xx: never upload that high
BOOT_VID, BOOT_PID = 0x320F, 0xB00D
APP_VID, APP_PID = 0x320F, 0x5088
REPORT_ID = 7
REPORT_LEN = 2056             # ID + 2055 data bytes (bootloader report descriptor: 96 07 08)

# Patches: name -> (description, [(address, original bytes, new bytes)]). Each original is checked
# against the stock image before anything is written, so a patch can never apply to the wrong code.
PATCHES = {}


def load_hex(path):
    """Intel HEX -> {address: byte}."""
    mem, upper = {}, 0
    with open(path, encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if not line.startswith(":"):
                continue
            rec = bytes.fromhex(line[1:])
            if (sum(rec) & 0xFF) != 0:
                raise ValueError(f"bad checksum in hex line {line[:20]}")
            n, addr, kind, data = rec[0], (rec[1] << 8) | rec[2], rec[3], rec[4:4 + rec[0]]
            if kind == 0:
                for i, v in enumerate(data):
                    mem[upper + addr + i] = v
            elif kind == 4:
                upper = struct.unpack(">H", data)[0] << 16
            elif kind == 1:
                break
    return mem


def app_image(mem):
    """The application region as bytes, padded with 0x00 to whole 2 KB pages (what the chip holds
    after the official update; verified with a ROM DFU read of the whole flash)."""
    end = max(a for a in mem if a >= APP_BASE) + 1
    size = (end - APP_BASE + PAGE - 1) // PAGE * PAGE
    if APP_BASE + size > SETTINGS_FLOOR:
        raise ValueError("app image would reach the settings area")
    return bytearray(mem.get(APP_BASE + i, 0x00) for i in range(size))


def apply_patches(image, names):
    for name in names:
        desc, edits = PATCHES[name]
        for addr, old, new in edits:
            off = addr - APP_BASE
            if bytes(image[off:off + len(old)]) != old:
                raise ValueError(f"patch {name}: bytes at {addr:#x} are not the expected stock bytes")
            if len(new) != len(old):
                raise ValueError(f"patch {name}: replacement at {addr:#x} changes the length")
            image[off:off + len(new)] = new
    return image


def pages(image):
    """Update reports (2056 bytes each) for the image, checksum on the last one."""
    count = len(image) // PAGE
    total = sum(image) & 0xFFFF
    out = []
    for n in range(count):
        rep = bytearray(REPORT_LEN)
        rep[0], rep[1], rep[2], rep[3] = REPORT_ID, 0x32, n, count - 1
        rep[4], rep[5] = total >> 8, total & 0xFF
        rep[8:8 + PAGE] = image[n * PAGE:(n + 1) * PAGE]
        out.append(bytes(rep))
    return out, total


def build(names):
    mem = load_hex(STOCK_HEX)
    image = apply_patches(app_image(mem), names)
    reports, total = pages(image)
    vec_sp, vec_reset = struct.unpack_from("<II", image, 0)
    assert 0x20000000 <= vec_sp < 0x20010000 and APP_BASE <= vec_reset < SETTINGS_FLOOR, "bad vector table"
    os.makedirs(OUT_DIR, exist_ok=True)
    tag = "+".join(names) or "stock"
    path = os.path.join(OUT_DIR, f"app_{tag}.bin")
    with open(path, "wb") as f:
        f.write(image)
    print(f"app image: {len(image)} bytes = {len(reports)} pages of 2 KB at {APP_BASE:#x}..{APP_BASE + len(image):#x}")
    print(f"patches: {', '.join(names) or 'none (stock)'}; checksum {total:#06x}; written to {path}")
    print(f"vector table: SP {vec_sp:#x}, reset {vec_reset:#x}")
    return reports


class FlashError(RuntimeError):
    pass


def stock_image():
    """The official 0088 application as update pages (to restore stock firmware)."""
    return app_image(load_hex(STOCK_HEX))


def flash(reports, progress=None):
    """Write update reports through the Glorious bootloader. progress(step, done, total) is called
    as it goes (step: "bootloader", "writing", "restarting"). Raises FlashError."""
    progress = progress or (lambda step, done, total: print(f"  {step} {done}/{total}", end="\r"))
    dev = _open(BOOT_VID, BOOT_PID, 0xFF01)
    if dev is None:
        app = _open(APP_VID, APP_PID, 0xFF01)
        if app is None:
            raise FlashError("numpad not found over USB (flashing needs the cable, not Bluetooth)")
        progress("bootloader", 0, 1)
        buf = bytearray(265)
        buf[0], buf[1] = REPORT_ID, 0x30  # app command 0x30: reboot into the bootloader
        app.send_feature_report(bytes(buf))
        app.close()
        for _ in range(50):
            time.sleep(0.2)
            dev = _open(BOOT_VID, BOOT_PID, 0xFF01)
            if dev:
                break
        else:
            raise FlashError("bootloader did not appear: unplug, hold Enter + Plus, plug in, and try again")
    try:
        for n, rep in enumerate(reports):
            progress("writing", n, len(reports))
            if dev.send_feature_report(rep) < 0:  # hidapi returns -1, it does not raise
                raise FlashError(f"page {n} was not accepted; the numpad stays in the bootloader, so "
                                 "flashing again (or restoring stock) is safe")
            time.sleep(0.15)  # 8 x 256-byte erase+program per page
        progress("restarting", len(reports), len(reports))
        end = bytearray(REPORT_LEN)
        end[0], end[1] = REPORT_ID, 0x30
        dev.send_feature_report(bytes(end))
    finally:
        dev.close()
    for _ in range(50):  # wait for the application to come back
        time.sleep(0.2)
        if hid_present(APP_VID, APP_PID):
            return
    raise FlashError("the numpad did not restart into the application; it stays in the bootloader, "
                     "so flashing again (or restoring stock) is safe")


def hid_present(vid, pid):
    import hid
    return bool(hid.enumerate(vid, pid))


def info():
    """Ask the running firmware for command 0x60 (only our patched firmware answers it)."""
    dev = _open(APP_VID, APP_PID, 0xFF01)
    if dev is None:
        sys.exit("numpad not found over USB")
    buf = bytearray(265)
    buf[0], buf[1] = REPORT_ID, 0x60
    dev.send_feature_report(bytes(buf))
    time.sleep(0.3)
    r = bytes(dev.get_feature_report(REPORT_ID, 265))
    dev.close()
    if r[1] == 0x60 and r[2:4] == b"GS":
        print(f"suite firmware extensions running: version {r[4]}, features {r[5]:#04x}, boot count {r[6]}")
    else:
        print("stock firmware (no answer to 0x60); last reply:", r[:8].hex(" "))


def _open(vid, pid, usage_page):
    import hid
    for d in hid.enumerate(vid, pid):
        if d["usage_page"] == usage_page:
            dev = hid.device()
            dev.open_path(d["path"])
            return dev
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["build", "flash", "info", "list"])
    ap.add_argument("--image", help="flash this prebuilt app image (e.g. from fw/build.py) instead of building")
    ap.add_argument("--patch", nargs="*", default=[], choices=sorted(PATCHES))
    ap.add_argument("--i-have-the-pinhole-fallback", action="store_true")
    args = ap.parse_args()
    if args.action == "list":
        for name, (desc, _) in sorted(PATCHES.items()):
            print(f"{name}: {desc}")
        return
    if args.action == "info":
        return info()
    if args.image:
        with open(args.image, "rb") as f:
            image = bytearray(f.read())
        if len(image) % PAGE or APP_BASE + len(image) > SETTINGS_FLOOR:
            sys.exit("image must be whole 2 KB pages and stay below the settings area")
        reports, total = pages(image)
        print(f"image {args.image}: {len(reports)} pages, checksum {total:#06x}")
    else:
        reports = build(args.patch)
    if args.action == "flash":
        if not args.i_have_the_pinhole_fallback:
            sys.exit("refusing to flash: set up the ROM DFU fallback first (see .knowledge/firmware-update.md)")
        try:
            flash(reports)
        except FlashError as e:
            sys.exit(f"flash failed: {e}")
        print("\ndone: the numpad restarted into the new firmware")


if __name__ == "__main__":
    main()
