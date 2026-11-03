"""Send numpad commands over Bluetooth through the BT module's pass-through GATT channel.

Service 0000FF00 / characteristic 0000FF01 (read, write). The module forwards each write to the
MCU as "+Data:<bytes>\\r\\n" (firmware FUN_08011ae4): byte 0 = seq (0 clear, 1..13 = 19-byte chunk
at offset 1 + (seq-1)*19, 0xFF = execute), so a frame is the USB command without its report ID.

  python ble_cmd.py leds      every LED magenta (cmd 06) - test
"""
import asyncio
import sys
import uuid

from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothLEDevice
from winrt.windows.devices.bluetooth.genericattributeprofile import GattWriteOption
from winrt.windows.storage.streams import DataWriter

sys.path.insert(0, r"D:\User Storage\Documents\2.project\Glorious GMMK\tools")
from gmmk_numpad import ALL_LEDS  # noqa: E402

ADDRESS = 0xCFDE7754FA00
SERVICE = uuid.UUID("0000ff00-0000-1000-8000-00805f9b34fb")
CHAR = uuid.UUID("0000ff01-0000-1000-8000-00805f9b34fb")


async def open_char():
    dev = await BluetoothLEDevice.from_bluetooth_address_async(ADDRESS)
    svc = (await dev.get_gatt_services_for_uuid_async(SERVICE, BluetoothCacheMode.UNCACHED)).services[0]
    return dev, (await svc.get_characteristics_for_uuid_async(CHAR, BluetoothCacheMode.UNCACHED)).characteristics[0]


async def write(ch, data):
    w = DataWriter()
    w.write_bytes(bytes(data))
    status = await ch.write_value_with_option_async(w.detach_buffer(), GattWriteOption.WRITE_WITH_RESPONSE)
    return status


async def send_frame(ch, frame):
    results = [await write(ch, bytes([0]) + bytes(19))]
    for k in range((len(frame) + 18) // 19):
        results.append(await write(ch, bytes([k + 1]) + frame[k * 19:(k + 1) * 19].ljust(19, b"\0")))
    results.append(await write(ch, bytes([0xFF]) + bytes(19)))
    return results


async def main():
    dev, ch = await open_char()
    if sys.argv[1] == "leds":
        data = b"".join(bytes((0xFF, i, 255, 0, 255)) for i in ALL_LEDS)
        print("06:", await send_frame(ch, bytes([0x06, 1, 1, 42, 0, 20, 0]) + data))
        await asyncio.sleep(0.2)
        print("01:", await send_frame(ch, bytes([0x01, 1, 1])))
    dev.close()


if __name__ == "__main__":
    asyncio.run(main())


async def main_send(frame):
    """Open the pass-through characteristic, send one frame, close (used by bt_probe.py)."""
    dev, ch = await open_char()
    try:
        return await send_frame(ch, frame)
    finally:
        dev.close()
