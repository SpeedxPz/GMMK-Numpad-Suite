"""List the GATT services/characteristics of the paired, connected GMMK Numpad (read-only).

Connected BLE devices stop advertising, so open it by address through WinRT instead of scanning.
"""
import asyncio

from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothLEDevice

ADDRESS = 0xCFDE7754FA00  # from the PnP instance id BTHLE\DEV_CFDE7754FA00

PROPS = {1: "broadcast", 2: "read", 4: "write-no-resp", 8: "write", 16: "notify", 32: "indicate"}


async def main():
    dev = await BluetoothLEDevice.from_bluetooth_address_async(ADDRESS)
    if dev is None:
        raise SystemExit("numpad not found")
    print("device:", dev.name, "status:", dev.connection_status)
    res = await dev.get_gatt_services_async(BluetoothCacheMode.UNCACHED)
    print("services status:", res.status)
    for s in res.services:
        print("service", s.uuid)
        try:
            cr = await s.get_characteristics_async(BluetoothCacheMode.UNCACHED)
        except Exception as e:  # HID service is owned by Windows
            print("  (characteristics not accessible:", e, ")")
            continue
        for ch in cr.characteristics:
            props = [n for bit, n in PROPS.items() if int(ch.characteristic_properties) & bit]
            print(f"  char {ch.uuid} handle={ch.attribute_handle} props={','.join(props)}")


asyncio.run(main())
