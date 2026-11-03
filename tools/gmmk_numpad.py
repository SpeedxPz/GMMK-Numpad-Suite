"""Minimal driver for the GMMK Numpad stock firmware, reverse engineered from Glorious CORE 2.1.23
and the firmware (GMMK_Numpad_88). Works over USB and Bluetooth LE; see .knowledge/hid-protocol.md.

Transports (both verified on device):
  - USB 320F:5088: every command is a 264-byte feature report, report ID 7, on usage page 0xFF01
    (MI_02). Events arrive on 0xFF00/0xFF00, one per input report.
  - BLE 093A:2860: the same command bytes without the report ID, cut into 21-byte output reports
    (ID 7) on usage page 0xFF02: [seq][19 bytes]. seq 0 clears, 1..13 are chunks, 0xFF executes
    (firmware FUN_08011ae4). Each chunk must wait for its ack, event 04 FC <seq>, or chunks get
    dropped. Events arrive on 0xFF01 input (ID 4), with up to two 9-byte events packed per report.
Commands (byte 1 is the command):
      0x01 profile update   [1]=1, [2]=profile(1-based), [3]=layer(1-based)
      0x02 LED effect       [4]=effect, [8]=speed, [15..17]=RGB
      0x03 key assignment   [8..27]=binding type per slot, [28..47]=HID code per slot
      0x06 per-key LEDs     [4]=42, [5]=page, [6]=20, [8..207]=40 x (vis, index, r, g, b)
                            vis 0xFF = solid, 0xFE = breathing; only page 0 is applied
      0x07 LED properties   [4]=effect, [7]=sidelights off, [8]/[13]=brightness 0..20,
                            [11]=knob click hold (~30 ms units; 0 = knob silent)
"""
import time

import hid

VID, PID = 0x320F, 0x5088
BT_VID, BT_PID = 0x093A, 0x2860
BT_CHUNK = 19
BT_ACK_TIMEOUT = 0.3  # measured: acks arrive in 0.05-0.21 s or never (about 2% are lost)
SLOT_CHANGE_GAP = 0.4  # s between the last command and one for another profile/layer (see Numpad._send)
REPORT_ID = 7
REPORT_LEN = 264
LED_COUNT = 42  # 6 x 7 LED grid; columns 0 and 5 are the side lights

# LED buffer index per key (CORE: KeyBufferIndexMaps[GMMKNumpad])
KEY_LED = {
    "NumLock": 1, "Divide": 2, "Multiply": 3, "Subtract": 4,
    "7": 7, "8": 8, "9": 9, "Add": 10,
    "4": 19, "5": 20, "6": 21,
    "1": 25, "2": 26, "3": 27, "Enter": 28,
    "0": 38, "Decimal": 39,
}
# CORE: GMMK_Numpad_SIDE_LED
SIDE_LEDS = [0, 5, 6, 11, 12, 17, 18, 23, 24, 29, 30, 35, 36, 41]
SIDE_LEFT = [i for i in SIDE_LEDS if i % 6 == 0]
SIDE_RIGHT = [i for i in SIDE_LEDS if i % 6 == 5]
ALL_LEDS = sorted([*KEY_LED.values(), *SIDE_LEDS])

# Key assignment slot per key (CORE: NumpadKeyAssignmentKeyMap)
KEY_SLOT = {
    "NumLock": 0, "Divide": 1, "Multiply": 2, "Subtract": 3,
    "7": 4, "8": 5, "9": 6, "Add": 7, "4": 8, "5": 9, "6": 10,
    "Encoder": 11, "1": 12, "2": 13, "3": 14, "Enter": 15, "0": 16, "Decimal": 19,
}
# CORE: DefaultKeyBuffer$1[GMMKNumpad] -> 20 binding types followed by 20 HID usage codes
DEFAULT_KEYS = bytes([
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 3, 1, 1, 1, 1, 1, 0, 0, 1,
    83, 84, 85, 86, 95, 96, 97, 87, 92, 93, 94, 2, 89, 90, 91, 88, 98, 0, 0, 99,
])
# CORE: LEDEffectBufferValue (0 = off, 1 = Glorious mode, 6 = breathing, 7 = normally on, 13 = rainbow ...)
EFFECT_NORMALLY_ON = 7
EFFECT_RAINBOW = 13
EFFECTS = {  # CORE: LEDEffectBufferValue / PresetLightingEffectIdentifier
    0: "Off", 1: "Glorious mode", 2: "Wave 1", 3: "Wave 2", 4: "Spiraling wave", 5: "Acid mode",
    6: "Breathing", 7: "Static (per-key)", 8: "Ripple graff", 9: "Pass without trace",
    10: "Fast run without trace", 11: "Matrix 2", 12: "Matrix 3", 13: "Rainbow",
    14: "Heartbeat sensor", 15: "Digital times", 16: "Kamehameha", 17: "Ping pong", 18: "Surmount",
}
BIND_KEYSTROKE = 1
BIND_CONSUMER = 3    # media keys / Windows shortcuts (codes below)
BIND_MACRO = 5       # code = macro ID
BIND_HOST_EVENT = 7  # CORE: ShortcutOptionType.LaunchProgram
BIND_KB_FUNCTION = 8  # profile / layer cycling
BIND_MODIFIERS = 0xE0  # | 1 Ctrl, 2 Shift, 4 Alt, 8 Win -> keystroke with modifiers
MOD_BITS = {"ctrl": 1, "shift": 2, "alt": 4, "win": 8}
# CORE: MultimediaOptions / WindowsShortcutType -> code for BIND_CONSUMER
CONSUMER_CODES = {
    "media_player": 0, "play_pause": 1, "mute": 2, "volume_up": 3, "volume_down": 4, "stop": 5,
    "previous": 6, "next": 7, "browser_home": 8, "browser_refresh": 9, "browser_back": 0x0B,
    "browser_forward": 0x0C, "browser_search": 0x0E, "my_computer": 0x0F, "calculator": 0x10, "email": 0x11,
}
KB_FUNCTION_CODES = {"profile_down": 1, "profile_up": 2, "layer_down": 3, "layer_up": 4}
MACRO_MODES = {"once": 0x0001, "while_held": 0xFFFF, "toggle": 0x0000}
MACRO_MAX_EVENTS = 82
HID_F13 = 0x68  # F13..F24 = 0x68..0x73


# Matrix position reported by BIND_HOST_EVENT keys (verified on device; row 0 = top row)
# Host-event keys report their matrix (row, column); the key-table slot is row * 4 + column.
# Verified for / (0,1), * (0,2), - (0,3) and the knob press (2,3) = slot 11; other keys follow the same rule.
SLOT_KEY = {slot: name for name, slot in KEY_SLOT.items()}
EVENT_KEY = {(slot // 4, slot % 4): name for slot, name in SLOT_KEY.items()}


def _open(vid, pid, usage_page, usage=None):
    for d in hid.enumerate(vid, pid):
        if d["usage_page"] == usage_page and (usage is None or d["usage"] == usage):
            dev = hid.device()
            dev.open_path(d["path"])
            return dev
    return None


def _open_events():
    """(device, is_bluetooth) for the event input collection, preferring USB."""
    dev = _open(VID, PID, 0xFF00, 0xFF00)
    if dev:
        return dev, False
    dev = _open(BT_VID, BT_PID, 0xFF01, 0x0001)
    if dev:
        return dev, True
    raise RuntimeError("GMMK Numpad not found (USB unplugged and Bluetooth asleep or off?)")


def split_events(report, bluetooth):
    """Raw input report -> list of 9-byte events (BLE packs up to two per 21-byte report)."""
    if not bluetooth:
        return [report]
    return [report[o:o + 9] for o in range(0, len(report) - 8, 9) if report[o] == 0x04 and report[o + 1]]


class EventReader:
    """Events from the numpad, over USB (0xFF00/0xFF00, MI_01) or BLE (0xFF01, input ID 4).

    Verified on device:
      slider          04 F8 32 00 <position 0..100> <raw ADC, 16-bit big endian>, on every move
      host-event key  04 F7 <0x80 | row> <column> on press, 04 F7 <row> <column> on release
      knob turn       04 FA <signed 16-bit little-endian steps> (only in knob event mode, see
                      Numpad.set_knob_events); +1 = one click clockwise
      profile change  04 F1 <profile> <layer> <effect> ..., after cmd 0x01 and after Fn+9/Fn+7
      BLE chunk ack   04 FC <seq> (consumed by Numpad, ignored here)
    """

    def __init__(self):
        self.dev, self.bluetooth = _open_events()
        self.pending = []

    def close(self):
        self.dev.close()

    def read(self, timeout_ms=1000):
        """Return ("slider", 0..100), ("press" | "release", key_name), ("knob", steps),
        ("profile", (profile, layer)), or None (timeout / other)."""
        if not self.pending:
            self.pending = split_events(self.dev.read(64, timeout_ms), self.bluetooth)
            if not self.pending:
                return None
        return self._decode(self.pending.pop(0))

    @staticmethod
    def _decode(r):
        if len(r) < 5 or r[0] != 0x04:
            return None
        if r[1] == 0xF8 and r[2] == 0x32:
            return "slider", r[4]
        if r[1] == 0xFA:
            steps = r[2] | (r[3] << 8)
            return "knob", steps - 0x10000 if steps & 0x8000 else steps
        if r[1] == 0xF7:
            name = EVENT_KEY.get((r[2] & 0x7F, r[3]))
            return ("press" if r[2] & 0x80 else "release", name) if name else None
        if r[1] == 0xF1:
            return "profile", (r[2], r[3])
        return None


class Numpad:
    def __init__(self, profile=1, layer=1):
        self.profile, self.layer = profile, layer
        self.acks = None
        self.effect_color = None  # set by set_effect(); lets BT frames omit LEDs that match it
        self._bt_clean = False  # first BT frame always starts with a clear chunk
        self._bt_dirty = 0  # bytes of the firmware chunk buffer holding data since the last clear
        self.bt_retries = 0  # chunks resent after a lost ack (diagnostics)
        self._last_ok = True
        self._last_slot, self._last_send = None, 0.0  # see _send: slot changes wait for the firmware
        self.dev = _open(VID, PID, 0xFF01)
        self.bluetooth = self.dev is None
        if self.bluetooth:
            self.dev = _open(BT_VID, BT_PID, 0xFF02)
            self.acks = _open(BT_VID, BT_PID, 0xFF01, 0x0001)
            if self.dev is None or self.acks is None:
                for d in (self.dev, self.acks):
                    if d:
                        d.close()
                raise RuntimeError("GMMK Numpad not found (USB unplugged and Bluetooth asleep or off?)")
            self.acks.set_nonblocking(True)

    def close(self):
        self.dev.close()
        if self.acks:
            self.acks.close()

    def _send(self, cmd, payload=b"", delay=0.1):
        # The firmware copies every command, profile/layer included, into ONE buffer (FUN_080145fc)
        # and runs it later from its main loop. A command for another slot that arrives before the
        # previous one ran overwrites it: an apply (0x01) for slot 1/1 followed 50 ms later by a write
        # to 1/2 activated 1/2 (verified with a trace). So switching slots waits for the last command.
        slot = (self.profile, self.layer)
        if self._last_slot not in (None, slot):
            time.sleep(max(0.0, self._last_send + SLOT_CHANGE_GAP - time.time()))
        self._last_slot = slot
        frame = bytes([cmd, self.profile, self.layer]) + bytes(payload)
        if self.bluetooth:
            self._send_bt(frame)
        else:
            buf = bytearray(REPORT_LEN + 1)  # hidapi expects the report ID as byte 0
            buf[0] = REPORT_ID
            buf[1:1 + len(frame)] = frame
            self._feature(buf)
        self._last_send = time.time()
        time.sleep(delay)

    def _feature(self, buf):
        # hidapi reports a failed write by returning -1, not by raising. After an unplug the old
        # handle fails every write, so raise: the caller reconnects instead of painting into nothing.
        if self.dev.send_feature_report(bytes(buf)) < 0:
            raise OSError("numpad write failed (unplugged?)")

    def _send_bt(self, frame, attempts=3):
        """Send one command over BLE with per-chunk acks (about 2% of acks are lost, never just late).

        Recovery follows the firmware's chunk logic (FUN_08011ae4):
          - seq 2..13 store at a fixed offset, and seq 0xFF re-executes the same buffer, so after a
            lost ack those are simply resent (every command we send sets absolute values);
          - a repeated seq 1 would execute the partial frame, so a lost seq 1 ack restarts the frame
            from seq 0 (clear), which turns the repeat into a no-op (cmd 0).
        """
        chunks = [(k + 1, frame[k * BT_CHUNK:(k + 1) * BT_CHUNK])
                  for k in range((len(frame) + BT_CHUNK - 1) // BT_CHUNK)]
        for _ in range(attempts):
            # The firmware never clears its chunk buffer after executing, so a frame shorter than an
            # earlier one would run with the old tail appended (verified: stale side-LED entries won).
            # seq 0 (clear) is skipped only when the previous frame completed and this one overwrites
            # every byte written since the last clear.
            span = len(chunks) * BT_CHUNK
            clear = not self._bt_clean or span < self._bt_dirty
            steps = ([(0, b"")] if clear else []) + chunks + [(0xFF, b"")]
            self._bt_dirty = span if clear else max(self._bt_dirty, span)
            self._bt_clean = False
            for seq, data in steps:
                for _try in range(attempts):
                    if self._write_chunk(seq, data):
                        break
                    self.bt_retries += 1
                    if seq in (0, 1):
                        break  # restart the whole frame
                else:
                    raise TimeoutError(f"no Bluetooth ack for chunk {seq} after {attempts} tries")
                if self._last_ok is False:
                    break
            else:
                self._bt_clean = True
                return
        raise TimeoutError(f"Bluetooth frame failed after {attempts} restarts")

    def _write_chunk(self, seq, data):
        while self.acks.read(64):  # drop stale reports so the ack we see is for this chunk
            pass
        self.dev.write(bytes([REPORT_ID, seq]) + data.ljust(BT_CHUNK, b"\0"))
        self._last_ok = self._wait_ack(seq)
        return self._last_ok

    def _wait_ack(self, seq):
        end = time.time() + BT_ACK_TIMEOUT
        while time.time() < end:
            for ev in split_events(self.acks.read(64), True):
                if ev[1] == 0xFC and ev[2] == seq:
                    return True
            time.sleep(0.005)
        return False

    def read(self):
        """Feature-report readback (USB only): the reply to the last read request (0x83/0x8A/0x8F)."""
        if self.bluetooth:
            raise NotImplementedError("readback is USB only")
        return bytes(self.dev.get_feature_report(REPORT_ID, REPORT_LEN + 1))

    def _request(self, cmd):
        # The reply lags behind a preceding write (verified: a read right after a key-table write
        # returned half-updated data), so give the firmware time to finish first.
        time.sleep(0.2)
        self._send(cmd, delay=0.2)
        return self.read()

    def device_info(self):
        """Read request 0x8A (USB, verified): reply 07 8A <battery %> <fw hi> <fw lo>."""
        r = self._request(0x8A)
        return {"battery": r[2], "firmware": f"{r[3]:02X}{r[4]:02X}"}

    def suite_firmware(self):
        """Our firmware add-on (fw/src/patch.c) answers read request 0x60 with
        07 60 'G' 'S' <version> <feature bits> <boot count>; stock firmware ignores 0x60 (verified),
        so the reply buffer still holds an older reply. Returns {"version", "features"} or None.
        USB only (feature-report readback)."""
        r = self._request(0x60)
        if r[1] == 0x60 and r[2:4] == b"GS":
            return {"version": r[4], "features": r[5]}
        return None

    # ---- firmware add-on only (fw/src/patch.c; check suite_firmware() feature bit 0x02 first) ----
    LIVE_SOLID, LIVE_BREATHE, LIVE_REMOVE = 0x00, 0x01, 0x02

    def set_live_leds(self, colors, mode=LIVE_SOLID):
        """Add-on cmd 0x61: show {led: (r, g, b)} immediately, in RAM only (no flash write).
        Frame [mode, count, (led, r, g, b) x count]; up to 62 LEDs per frame. Over Bluetooth 3 LEDs
        fit one 19-byte chunk. Lost on a profile/layer switch and when the active slot gets a 0x06."""
        items = sorted(colors.items())
        for start in range(0, len(items), 62):
            batch = items[start:start + 62]
            payload = bytearray((mode, len(batch)))
            for led, (r, g, b) in batch:
                payload += bytes((led, r, g, b))
            self._send(0x61, payload, delay=0.02 if not self.bluetooth else 0)

    def clear_live_leds(self):
        """Add-on cmd 0x62: undo the live colours still in place."""
        self._send(0x62, b"", delay=0.02 if not self.bluetooth else 0)

    def read_effect(self):
        """Read request 0x8F (USB, verified): reply is the stored 0x02 effect frame for the active
        profile/layer: [2]=profile [3]=layer [4]=effect [8]=speed byte (20 - speed*19/100)."""
        r = self._request(0x8F)
        return {"profile": r[2], "layer": r[3], "effect": r[4], "speed": round((20 - r[8]) * 100 / 19)}

    def read_key_table(self):
        """Read request 0x83 (USB): reply 07 03 <profile> <layer> .. [8..27] types, [28..47] codes."""
        r = self._request(0x83)
        return {"profile": r[2], "layer": r[3], "types": list(r[8:28]), "codes": list(r[28:48])}

    def apply_profile(self):
        """Cmd 0x01. Also makes self.profile/self.layer the active one (verified: the numpad switches
        and echoes 04 F1 <profile> <layer> <effect>), so every write activates its profile.

        The data commands do NOT need it to be stored: a key table written to a non-active
        profile/layer without 0x01 reads back correctly and nothing switches (verified). Pass
        apply=False to the setters to write a slot silently."""
        self._send(0x01, delay=0.15)  # the job runs later from the firmware's main loop (see _send)

    def set_leds(self, colors, default=(0, 0, 0), breathing=(), only=False, apply=True):
        """colors: {led_index: (r, g, b)}; other real LEDs get `default`; `breathing` LEDs pulse.

        Each entry carries its own LED index and the firmware only applies page 0 (verified on
        device: CORE's positional layout leaves LED 41 in page 1, where it is ignored), so all
        31 real LEDs are packed into page 0.

        A per-key entry overrides the running effect on that LED; LEDs left out of the frame show
        the effect (verified). only=True sends just the LEDs in `colors`, so an animated effect keeps
        running everywhere else.
        """
        leds = sorted(colors) if only else ALL_LEDS
        if self.bluetooth:
            # Each chunk is an acked BLE round trip, so keep frames short; no profile apply needed.
            leds = [i for i in leds if i in breathing or colors.get(i, default) != self.effect_color]
            self._send(0x06, bytes((42, 0, 20, 0)) + self._led_page(colors, default, breathing, leds), delay=0)
        else:
            self._send(0x06, bytes((42, 0, 20, 0)) + self._led_page(colors, default, breathing, leds))
            if apply:
                self.apply_profile()

    def set_knob_events(self, enabled, colors, default=(0, 0, 0), breathing=()):
        """Knob turns -> 04 FA events on the vendor interface (enabled) or Windows volume keys.

        Firmware (FUN_0801082c): report command 0x09 is a twin of the 0x06 per-key command that
        also stores byte [7] as the knob mode flag, saved per profile/layer in flash. CORE never
        sends it for the numpad. Its 42 LED entries go to a separate buffer; the current colours
        are sent anyway so nothing visible changes. Verified on device.
        """
        self._send(0x09, bytes((42, 0, 20, 1 if enabled else 0)) + self._led_page(colors, default, breathing))

    def _led_page(self, colors, default, breathing, leds=ALL_LEDS):
        data = bytearray()
        for i in leds:
            r, g, b = colors.get(i, default)
            data += bytes((0xFE if i in breathing else 0xFF, i, r, g, b))
        return bytes(data)  # unpadded: BT frames must keep their exact length (USB pads to 264)

    def set_effect(self, effect=EFFECT_NORMALLY_ON, color=(255, 255, 255), speed=50, brightness=100,
                   knob_hold=1, sidelights=True, multicolor=False, scan_ms=8, apply=True):
        """Preset effect + brightness, sent the way CORE's setPresetLightingEffect does (02, 07, 01).

        speed and brightness are 0..100 (CORE's UI scale). multicolor and rainbow flags follow CORE's
        PrepareLEDEffectBuffer. Unlike CORE, also sets properties byte [11]. Verified on device: it is
        how long each knob click holds its volume key (in ~30 ms units). CORE leaves it 0, which makes
        the knob go silent; 1 = one volume step per click, 2 = two steps, 255 = ~7 s of repeat per click.
        """
        r, g, b = color
        effect_payload = bytearray(14)
        effect_payload[0] = effect                       # [4]
        effect_payload[4] = round(20 - speed * 19 / 100)  # [8]
        effect_payload[6] = 1 if multicolor else 0       # [10]
        effect_payload[8] = 1 if effect == EFFECT_RAINBOW else 0  # [12]
        effect_payload[11:14] = bytes((r, g, b))         # [15..17]
        self._send(0x02, effect_payload)
        level = round(brightness * 20 / 100)
        props = bytearray(10)
        props[0] = effect   # [4]
        props[2] = 1        # [6]
        props[3] = 0 if sidelights else 1  # [7] 0 = side lights on
        props[4] = level    # [8]
        props[6] = max(2, min(8, int(scan_ms)))  # [10] key scan interval, ms (main loop clamps 2..8)
        props[7] = knob_hold  # [11]
        props[9] = level    # [13]
        self._send(0x07, props)
        if apply:
            self.apply_profile()
        self.effect_color = tuple(color) if effect == EFFECT_NORMALLY_ON else None
        time.sleep(1.0)  # a per-key write sent straight after this is dropped (verified on device)

    def set_device_settings(self, polling_hz=1000, idle_minutes=0):
        """Cmd 0x08 (firmware job 8; values persist in the settings flash). Decoded in the firmware:
          [8]  polling code 0/1/2/3 -> a 1/2/4/8 ms gap between USB key reports (FUN_08013802,
               FUN_08007ef0), i.e. 1000/500/250/125 Hz. USB only.
          [9]  1, as CORE sends it.
          [11..19] lighting sleep per profile/layer (p1l1, p1l2, ... p3l3): LEDs switch off after
               that many minutes without a key press (FUN_08009cd8: n * 60000 ms); 0 = never.
               CORE always sends 0 here.
        idle_minutes: one value for every slot, or a list of 9."""
        code = {1000: 0, 500: 1, 250: 2, 125: 3}[int(polling_hz)]
        idle = idle_minutes if isinstance(idle_minutes, (list, tuple)) else [idle_minutes] * 9
        payload = bytearray(16)
        payload[4], payload[5] = code, 1                                   # [8], [9]
        payload[7:16] = bytes(max(0, min(254, int(m))) for m in idle[:9])  # [11..19]
        self._send(0x08, payload, delay=0.2)

    def set_macro(self, macro_id, events):
        """Store macro `macro_id` (1..255) on the device (cmd 0x05; global, not per profile).

        events: [(hid_code, pressed, delay_ms_after), ...]; each is encoded as 3 bytes
        [(delay >> 8) | 0x80 if pressed, delay & 0xFF, code] (CORE convertMacroEventsToBuffer).
        Firmware FUN_080103c8 keeps a 256-byte flash slot per ID, so at most 82 events fit.
        """
        data = bytearray()
        for code, pressed, delay in events[:MACRO_MAX_EVENTS]:
            delay = max(1, min(int(delay), 0x7FFF))
            data += bytes(((delay >> 8) | (0x80 if pressed else 0), delay & 0xFF, code))
        payload = bytearray(4 + len(data))  # [4..7] unused, events from [8]
        payload[4:] = data
        # [2] is the macro ID here, not the profile, so build the frame by hand. It still goes through
        # the firmware's single command buffer, so give the previous command time to run (see _send).
        frame = bytes([0x05, macro_id, 0]) + bytes(payload)
        time.sleep(max(0.0, self._last_send + SLOT_CHANGE_GAP - time.time()))
        self._last_slot = "macro"  # not a slot: the next slot command waits for this one too
        if self.bluetooth:
            self._send_bt(frame)
        else:
            buf = bytearray(REPORT_LEN + 1)
            buf[0] = REPORT_ID
            buf[1:1 + len(frame)] = frame
            self._feature(buf)
        self._last_send = time.time()
        time.sleep(0.15)

    def set_macro_modes(self, modes):
        """Playback mode per key slot (cmd 0x04, per profile/layer): {key_name: mode}.

        Verified in firmware: playback looks the mode up by key slot (table at 0x08015D34 maps
        slot -> slot); CORE writes it at the LED index instead, which looks like a CORE bug.
        """
        payload = bytearray(4 + 40)
        for key, mode in modes.items():
            value = MACRO_MODES[mode]
            payload[4 + KEY_SLOT[key] * 2:6 + KEY_SLOT[key] * 2] = bytes((value >> 8, value & 0xFF))
        self._send(0x04, payload, delay=0.15)

    def set_key_bindings(self, overrides, apply=True):
        """overrides: {key_name: (binding_type, code)}; others keep defaults.

        BIND_KEYSTROKE sends HID usage `code` to the OS; BIND_HOST_EVENT sends no keystroke, only
        an event on the vendor interface (CORE uses it for "Launch program").
        """
        table = bytearray(DEFAULT_KEYS)
        for key, (bind_type, code) in overrides.items():
            slot = KEY_SLOT[key]
            table[slot] = bind_type
            table[20 + slot] = code
        self._send(0x03, bytes(4) + table, delay=0.15)
        if apply:
            self.apply_profile()

    @staticmethod
    def key_table_bytes(overrides):
        """The 40-byte table (20 types + 20 codes) that set_key_bindings writes for `overrides`."""
        table = bytearray(DEFAULT_KEYS)
        for key, (bind_type, code) in overrides.items():
            table[KEY_SLOT[key]] = bind_type
            table[20 + KEY_SLOT[key]] = code
        return bytes(table)
