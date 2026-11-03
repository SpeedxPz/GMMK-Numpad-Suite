import hid
for d in hid.enumerate(0x320F, 0x5088):
    print(f"if={d['interface_number']} usage_page=0x{d['usage_page']:04x} usage=0x{d['usage']:04x} path={d['path']!r}")
