"""Build the patched GMMK Numpad application: stock 0088 app + our C code (fw/src) + 3 hooks.

    python fw/build.py            # compile, link, patch, verify -> _re/fw/build/app_suite.bin

Nothing is sent to the device; flash the result with tools/fw_flash.py flash --image ...
Every patched instruction is checked against its stock bytes first, and the result is
disassembled to confirm the hooks call our functions.
"""
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import fw_flash  # noqa: E402

# xPack arm-none-eabi-gcc 15.2.1-1.1: its bin folder in $ARM_GCC_BIN, else arm-none-eabi-* on PATH
GCC_BIN = os.environ.get("ARM_GCC_BIN", "")
OUT = os.path.join(ROOT, "_re", "fw", "build")
CODE_BASE, CODE_END = 0x08016800, 0x08018000

# (address of a BL in the stock app, its stock target, our function that should be called instead)
HOOKS = [
    (0x080140A0, 0x080115F8, "patch_init"),     # main(): first call (system init)
    (0x08012A02, 0x080145FC, "patch_command"),  # USB feature report -> command parser
    (0x080141AE, 0x080145FC, "patch_command"),  # Bluetooth frame   -> command parser
    (0x08014236, 0x08008BE0, "patch_tick"),     # main loop, right after the job / read dispatch
]


def tool(name):
    path = os.path.join(GCC_BIN, f"arm-none-eabi-{name}.exe")
    return path if os.path.exists(path) else f"arm-none-eabi-{name}"


def encode_bl(at, target):
    """Thumb-2 BL at `at` to `target` (both even addresses)."""
    off = target - (at + 4)
    if not -(1 << 24) <= off < (1 << 24) or off & 1:
        raise ValueError(f"BL out of range: {at:#x} -> {target:#x}")
    s = (off >> 24) & 1
    i1, i2 = (off >> 23) & 1, (off >> 22) & 1
    j1, j2 = (~(i1 ^ s)) & 1, (~(i2 ^ s)) & 1
    imm10, imm11 = (off >> 12) & 0x3FF, (off >> 1) & 0x7FF
    hw1 = 0xF000 | (s << 10) | imm10
    hw2 = 0xD000 | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def compile_patch():
    os.makedirs(OUT, exist_ok=True)
    elf, binf = os.path.join(OUT, "patch.elf"), os.path.join(OUT, "patch.bin")
    cflags = ["-mcpu=cortex-m3", "-mthumb", "-Os", "-ffreestanding", "-fno-builtin", "-nostdlib",
              "-ffunction-sections", "-Wall", "-Wextra", "-Werror", "-std=c11"]
    subprocess.run([tool("gcc"), *cflags, "-T", os.path.join(HERE, "patch.ld"), "-Wl,--gc-sections",
                    "-Wl,-Map=" + os.path.join(OUT, "patch.map"), "-Wl,--undefined=patch_init",
                    "-Wl,--undefined=patch_command", "-Wl,--undefined=patch_tick", "-o", elf, os.path.join(HERE, "src", "patch.c")],
                   check=True)
    subprocess.run([tool("objcopy"), "-O", "binary", elf, binf], check=True)
    syms = {}
    for line in subprocess.run([tool("nm"), elf], check=True, capture_output=True, text=True).stdout.splitlines():
        addr, _, name = line.split()
        syms[name] = int(addr, 16)
    with open(binf, "rb") as f:
        return f.read(), syms


def build():
    code, syms = compile_patch()
    if CODE_BASE + len(code) > CODE_END:
        raise SystemExit(f"patch code too big: {len(code)} bytes")
    image = fw_flash.app_image(fw_flash.load_hex(fw_flash.STOCK_HEX))
    end = CODE_END - fw_flash.APP_BASE
    image += b"\xFF" * (end - len(image))  # extend the update to cover our code
    off = CODE_BASE - fw_flash.APP_BASE
    if set(image[off:off + len(code)]) != {0xFF}:
        raise SystemExit("the code area is not empty in the stock image")
    image[off:off + len(code)] = code
    for at, stock_target, name in HOOKS:
        o = at - fw_flash.APP_BASE
        if bytes(image[o:o + 4]) != encode_bl(at, stock_target):
            raise SystemExit(f"stock bytes at {at:#x} are not 'bl {stock_target:#x}'")
        image[o:o + 4] = encode_bl(at, syms[name] & ~1)
    verify(image, syms, len(code))
    path = os.path.join(OUT, "app_suite.bin")
    with open(path, "wb") as f:
        f.write(image)
    reports, total = fw_flash.pages(image)
    ram = syms["__bss_end"] - syms["__data_start"]
    print(f"patch code: {len(code)} bytes at {CODE_BASE:#x}; RAM {ram} bytes at {syms['__data_start']:#x} "
          f"(patch_init {syms['patch_init']:#x}, patch_command {syms['patch_command']:#x}, "
          f"patch_tick {syms['patch_tick']:#x})")
    if ram > 3072:
        raise SystemExit("add-on RAM use is over the 3 KB budget")
    print(f"image: {len(image)} bytes = {len(reports)} pages, checksum {total:#06x} -> {path}")


def verify(image, syms, code_len):
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    for at, _, name in HOOKS:
        o = at - fw_flash.APP_BASE
        ins = next(md.disasm(bytes(image[o:o + 4]), at))
        want = f"#{syms[name] & ~1:#x}"
        if ins.mnemonic != "bl" or ins.op_str != want:
            raise SystemExit(f"hook at {at:#x} decodes as {ins.mnemonic} {ins.op_str}, expected bl {want}")
        print(f"hook {at:#x}: bl {want}  ({name})")
    o = CODE_BASE - fw_flash.APP_BASE
    listing = list(md.disasm(bytes(image[o:o + code_len]), CODE_BASE))
    with open(os.path.join(OUT, "patch.lst"), "w") as f:
        for i in listing:
            f.write(f"{i.address:#010x}  {i.bytes.hex():10s} {i.mnemonic} {i.op_str}\n")
    if any(i.mnemonic in ("blx",) and not i.op_str.startswith("r") for i in listing):
        raise SystemExit("patch code contains an immediate BLX (would switch to ARM state)")


if __name__ == "__main__":
    build()
