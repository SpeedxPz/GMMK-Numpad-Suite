"""Single owner of the numpad: composes user lighting + OBS status lights and writes it, serialised.

The numpad has 9 slots (3 profiles x 3 layers); each stores its own key table, macro modes, knob
mode, effect and LEDs. Rules (all verified on the device):
  - cmd 0x01 activates its slot, and the setters send it by default; a write to a slot without
    0x01 is still stored and switches nothing. So the ACTIVE slot is written with 0x01 (it is
    already active, nothing moves), every other slot silently. A switch made on the numpad
    (Fn+9/7, Fn+3/1) therefore never races a write that would pull it back.
  - What each slot holds is cached (memory + device_cache.json) and only differences are written.
    At startup over USB the cache is checked against each slot's key table (read request 0x83);
    the dashboard's Resync button drops it (after a factory reset or after CORE touched the numpad).
  - The active slot is written straight away; the hidden slots catch up a few seconds after the
    last change (HIDDEN_DELAY), so editing stays responsive.

Requests from any thread only mark the state dirty and wake the worker, which debounces bursts
(e.g. dragging a colour picker), so the flash is written once per settled change.
"""
import asyncio
import collections
import json
import os
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import hid  # noqa: E402
from gmmk_numpad import ALL_LEDS, BT_PID, BT_VID, EFFECT_NORMALLY_ON, SIDE_LEFT, SIDE_RIGHT, Numpad  # noqa: E402

import keymap  # noqa: E402
from config import CONFIG_DIR, LAYERS, PROFILES  # noqa: E402
from discord_link import discord_overlay  # noqa: E402
from obs_link import overlay_for  # noqa: E402
from pc_link import pc_overlay  # noqa: E402
from voicemeeter_link import vm_overlay  # noqa: E402

INTEGRATION_OVERLAYS = {"pc": pc_overlay, "discord": discord_overlay, "voicemeeter": vm_overlay}

DEBOUNCE_SECONDS = 0.25
RECONNECT_SECONDS = 3.0
INFO_SECONDS = 300.0         # battery / firmware refresh
HIDDEN_DELAY = 4.0           # hidden slots are synced this long after the last change
SWITCH_ECHO_SECONDS = 1.5    # ignore 04 F1 echoes of the previous slot after a dashboard switch
# While the numpad runs a write (flash write, profile apply) it does not scan its keys, so a quick tap
# in that window is lost. Writes therefore wait until the keys have been quiet for a moment: after
# a press, quickly pressing the next scene key must not land in the tally repaint the first caused.
KEY_QUIET_SECONDS = 0.4      # active slot: repaint this long after the last key press
LAYER_FLASH_SECONDS = 3.0    # layer indicator shown over the OBS side bars after a switch
BATTERY_SHOW_SECONDS = 4.0   # "battery" key: gauge on the side bars this long
BATTERY_WARN_SECONDS = 5.0   # low battery (config "battery_warning"): breathing gauge this long, every read
HIDDEN_KEY_QUIET = 3.0       # hidden slots: only while nobody is pressing keys
CACHE_PATH = os.path.join(CONFIG_DIR, "device_cache.json")
SLOTS = [(p, l) for p in PROFILES for l in LAYERS]
# Firmware add-on (fw/src/patch.c) feature bits, reported by read request 0x60. The suite works on
# stock firmware; features listed here are used only when the numpad reports them.
FW_FEATURES = {
    0x01: "info",            # 0x60 itself (always set by the add-on)
    0x02: "live_leds",       # 0x61 / 0x62: per-key colours in RAM, no flash write (status lights)
    0x04: "command_queue",   # frames queued instead of overwriting the single command buffer
    0x08: "knob_fix",        # properties [11]=0 no longer silences the knob
    0x10: "peek",            # 0x63 reads RAM / flash (diagnostics, tools/addon_probe_switch.py)
}
BATTERY_SERVICE = uuid.UUID("0000180f-0000-1000-8000-00805f9b34fb")
BATTERY_LEVEL = uuid.UUID("00002a19-0000-1000-8000-00805f9b34fb")


def _norm(value):
    """JSON round trip, so freshly computed values compare equal to ones loaded from the cache."""
    return json.loads(json.dumps(value, sort_keys=True))


def battery_gauge(percent):
    """Battery % -> {led: colour} for both side bars: lit from the bottom, one LED per ~14 %, red < 20 %,
    orange < 50 %, yellow < 90 %, then green (the firmware's own battery colour order); the rest dimly."""
    color = ((220, 0, 0) if percent < 20 else (255, 90, 0) if percent < 50
             else (255, 200, 0) if percent < 90 else (0, 200, 60))
    dim = tuple(c // 12 for c in color)
    lit = max(1, round(percent * len(SIDE_LEFT) / 100)) if percent > 0 else 0
    return {i: color if n >= len(bar) - lit else dim
            for bar in (SIDE_LEFT, SIDE_RIGHT) for n, i in enumerate(bar)}


def ble_battery():
    """Battery % from the standard GATT battery service, or None. Best effort: Windows may keep the
    service to itself, and the address is taken from the BLE HID device path."""
    found = [re.search(r"_([0-9a-f]{12})(?:&|#)", d["path"].decode(errors="ignore").lower())
             for d in hid.enumerate(BT_VID, BT_PID)]
    m = next((f for f in found if f), None)
    if not m:
        return None
    from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothLEDevice
    from winrt.windows.storage.streams import DataReader

    async def read():
        dev = await BluetoothLEDevice.from_bluetooth_address_async(int(m.group(1), 16))
        if dev is None:
            return None
        services = (await dev.get_gatt_services_for_uuid_async(BATTERY_SERVICE)).services
        for s in services:
            for ch in (await s.get_characteristics_for_uuid_async(BATTERY_LEVEL)).characteristics:
                # CACHED on purpose: the numpad answers a direct (uncached) read with a stale 100 %, while
                # its notifications carry the real level and Windows keeps the cache updated from them.
                res = await ch.read_value_async(BluetoothCacheMode.CACHED)
                if res.value is not None:
                    return DataReader.from_buffer(res.value).read_byte()
        return None

    return asyncio.run(asyncio.wait_for(read(), 10))


class DeviceManager(threading.Thread):
    def __init__(self, config, log=print):
        super().__init__(daemon=True, name="device")
        self.config = config
        self.log = log
        self.pad = None
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.obs_view = None       # OBS state for the status lights (see ObsLink._show), None = no lights
        self.app_views = {}        # {"discord" | "voicemeeter": state for their keys' status lights} (INTEGRATION_OVERLAYS)
        self.dirty = True
        self.edited = False          # a settings edit (debounced) rather than only new OBS state
        self.switch_pending = False  # send cmd 0x01 for the active slot even if nothing else changed
        self.resync_pending = False
        self.ignore_switch_until = 0.0
        self.expected_slot = None
        self._next_info = 0.0
        self._hidden_due = None    # when the hidden slots should be brought up to date
        self._sent = {}            # "p.l" -> what that slot holds on the device
        self._sent_macros = {}     # macro id -> events (macros are global on the device)
        self.status = {"connected": False, "transport": None, "last_paint_s": None, "error": None,
                       "profile": None, "layer": None, "battery": None, "firmware": None, "syncing": None,
                       "fw_addon": None, "fw_addon_source": None}
        self._fw_addon_cached = None  # last add-on info seen over USB (readback is USB only)
        self._paused = None            # reason while another component (firmware update) owns the numpad
        self._live_sent, self._live_slot = {}, None  # add-on live LEDs currently shown (see _push_live)
        self._released = threading.Event()
        self._replugged = False        # see on_reconnected
        self._lost = False             # see on_lost
        self._layer_flash_until = 0.0  # see compose: layer indicator over the OBS side bars
        self._battery_until = 0.0      # see request_battery
        self._battery_breathe = False  # the gauge shown is the low-battery warning (breathes)
        self._last_key = 0.0           # time of the last key press seen on the numpad (see note_key)

    # ---- requests (any thread) ----
    def request_sync(self):
        """Lighting, key map or macros changed: write whatever differs from the device."""
        with self.lock:
            self.dirty = self.edited = True
        self.wake.set()

    request_lighting = request_sync

    def request_obs(self, view):
        """New OBS state for the status lights: {"state", "scene", "preview", "muted", "replay", "saved"}
        or None."""
        view = _norm(view)
        with self.lock:
            if view == self.obs_view:
                return
            self.obs_view = view
            self.dirty = True
        self.wake.set()

    def request_integration(self, name, view):
        """New state of an integration (INTEGRATION_OVERLAYS) for its keys' status lights, or None."""
        view = _norm(view)
        with self.lock:
            if view == self.app_views.get(name):
                return
            self.app_views[name] = view
            self.dirty = True
        self.wake.set()

    def request_battery(self):
        """The "battery" key: show the battery level on the side bars for BATTERY_SHOW_SECONDS, read fresh."""
        self._battery_until = time.time() + BATTERY_SHOW_SECONDS
        self._battery_breathe = False
        self._next_info = 0.0
        with self.lock:
            self.dirty = True
        self.wake.set()

    def request_slot(self, profile, layer=None):
        """Dashboard switched profile and/or layer: activate it on the numpad."""
        self.config.set_active(profile, layer)
        self._layer_flash_until = time.time() + LAYER_FLASH_SECONDS
        with self.lock:
            self.dirty = self.switch_pending = True
            self.expected_slot = self.config.active_slot()
            self.ignore_switch_until = time.time() + SWITCH_ECHO_SECONDS
        self.wake.set()

    def request_resync(self):
        """Forget what the numpad is believed to hold and write every slot again."""
        with self.lock:
            self.resync_pending = self.dirty = True
        self.wake.set()

    def on_lost(self):
        """The event reader just lost the numpad (unplugged, BT asleep). Show it at once instead of
        at the next failed write, and drop the command handle."""
        with self.lock:
            self._lost = True
        self.status.update(connected=False, transport=None, battery=None, syncing=None, error="numpad unplugged")
        self.wake.set()

    def on_reconnected(self):
        """The event reader lost the numpad and found it again (unplug/replug, BT reconnect, power
        cycle). The command handle may be stale without knowing it, and the numpad came back without
        any add-on live colours, so reconnect and paint everything again."""
        with self.lock:
            self._replugged = self.dirty = True
        self.wake.set()

    def note_key(self):
        """A key was pressed on the numpad (controls thread): hold writes back (KEY_QUIET_SECONDS)."""
        self._last_key = time.time()

    def _wait_keys_quiet(self, quiet):
        """Sleep until no key was pressed for `quiet` seconds (or the numpad is being released)."""
        while not self._paused:
            left = self._last_key + quiet - time.time()
            if left <= 0:
                return
            time.sleep(left)

    def on_device_slot(self, profile, layer):
        """04 F1 from the numpad (Fn+9/7, Fn+3/1, a profile/layer key, or the echo of our cmd 0x01).

        Right after a dashboard switch, echoes of writes for the previous slot can still arrive; only
        those (a different slot, inside the window) are ignored. A switch made on the numpad itself
        needs no cmd 0x01 and, since every slot is already written, usually no writes at all."""
        with self.lock:
            if time.time() < self.ignore_switch_until and (profile, layer) != self.expected_slot:
                return
        if profile not in PROFILES or layer not in LAYERS or (profile, layer) == self.config.active_slot():
            return
        self.log(f"numpad switched to profile {profile}, layer {layer}")
        self.config.set_active(profile, layer)
        self._layer_flash_until = time.time() + LAYER_FLASH_SECONDS
        self.request_sync()


    # ---- composition ----
    def compose(self, p, l, with_overlay=True):
        """Everything slot (p, l) should hold: (colors, breathing, base, static, keymap stuff...).
        Static mode: every real LED gets a colour. Animated effects: only the user's own per-key
        colours, the status lights and the layer indicator, because any per-key entry overrides the
        effect on that LED (verified) and everything else should keep animating."""
        light, lay = self.config.profile(p)["lighting"], self.config.layer(p, l)
        static = light["effect"] == EFFECT_NORMALLY_ON
        per_key = {int(k): tuple(v) for k, v in light["per_key"].items()}
        user = {i: per_key.get(i, tuple(light["color"])) for i in ALL_LEDS} if static else per_key
        with self.lock:
            view, apps = self.obs_view, dict(self.app_views)
        if (p, l) != self.config.active_slot():
            # Hidden slots are stored without any OBS / Discord state: the status lights only matter on
            # the shown slot, which is rewritten on every switch anyway. Otherwise each OBS change
            # (stream start/stop, OBS closing) rewrote the hidden slots' lighting, and every frame
            # briefly shows the slot it writes (0x20000212/13), so another profile flashed up.
            view, apps = None, {}
        if view:
            overlay, breathing = overlay_for(tuple(view["state"]) if view["state"] else None, lay["keymap"],
                                             view.get("scene"), view.get("muted"), view.get("preview"),
                                             view.get("replay"), view.get("saved", False))
        else:
            overlay, breathing = {}, set()
        for name, overlay_of in INTEGRATION_OVERLAYS.items():  # keys only, never the side bars
            overlay.update(overlay_of(apps.get(name), lay["keymap"]))
        under = dict(user)  # what each LED shows without the status lights
        if not light.get("sidelights", True):
            # "Side lights off" = black per-key entries (they override any effect). The firmware's own
            # switch (cmd 0x07 [7] -> 0x200002A6) is not an off switch: it hands the side bars to an
            # undocumented status mode (they blinked purple/blue over BT), so it is never sent.
            for i in SIDE_LEFT + SIDE_RIGHT:
                under[i] = (0, 0, 0)
        indicator = light.get("layer_indicator") or {}
        ind_leds = [i for bar in (SIDE_LEFT, SIDE_RIGHT) for i in bar[-l:]]  # bottom 1/2/3 LEDs = layer
        # While OBS records or streams, the side bars belong to OBS; the layer indicator only shows for
        # LAYER_FLASH_SECONDS after a switch (as a status light on top). Otherwise it is part of the
        # slot's own lighting.
        obs_bars = any(i in overlay for i in SIDE_LEFT + SIDE_RIGHT)
        flash = (p, l) == self.config.active_slot() and time.time() < self._layer_flash_until
        persistent = indicator.get("enabled") and not obs_bars
        if persistent:
            for i in ind_leds:
                under[i] = tuple(indicator["color"])
        # status lights as {led: (colour, breathing)}; the layer indicator wins on its LEDs
        live = {i: (tuple(c), i in breathing) for i, c in overlay.items() if not (persistent and i in ind_leds)}
        if indicator.get("enabled") and obs_bars and flash:
            live.update({i: (tuple(indicator["color"]), False) for i in ind_leds})
        battery = self.status.get("battery")
        if (p, l) == self.config.active_slot() and time.time() < self._battery_until and battery is not None:
            live.update({i: (c, self._battery_breathe) for i, c in battery_gauge(battery).items()})  # over everything
        if with_overlay:
            colors = {**under, **{i: c for i, (c, _) in live.items()}}
            breathing = {i for i, (_, br) in live.items() if br}
        else:  # firmware add-on: status lights go out as live LEDs (cmd 0x61), not into the saved frame
            colors, breathing = dict(under), set()
        # Over Bluetooth only LEDs that differ from the effect colour are sent, so in static mode make
        # the effect colour the most common user colour (fewer entries, faster repaints).
        base = collections.Counter(user.values()).most_common(1)[0][0] if static else tuple(light["color"])
        return light, lay, colors, breathing, base, static, live, under

    # ---- worker ----
    def pause(self, reason):
        """Let go of the numpad (e.g. for a firmware update) and wait until the worker did."""
        self._released.clear()
        self._paused = reason
        self.wake.set()
        if not self._released.wait(30):
            raise RuntimeError("the numpad connection did not close in time")

    def resume(self):
        self._paused = None
        with self.lock:
            self.dirty = True
        self.wake.set()

    def run(self):
        while True:
            if self._paused:
                if self.pad is not None:
                    try:
                        self.pad.close()
                    except Exception:
                        pass
                    self.pad = None
                self.status.update(connected=False, transport=None, error=self._paused, syncing=None)
                self._released.set()
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            if self.pad is None:
                timeout = RECONNECT_SECONDS
            else:
                due = [self._next_info] + ([self._hidden_due] if self._hidden_due else [])
                if self._layer_flash_until > time.time():  # repaint when the layer flash ends
                    due.append(self._layer_flash_until + 0.05)
                if self._battery_until > time.time():  # and when the battery gauge ends
                    due.append(self._battery_until + 0.05)
                timeout = max(0.2, min(due) - time.time())
            self.wake.wait(timeout)
            if self.edited:  # dashboard edits come in bursts (colour picker drags): let them settle
                self.edited = False
                time.sleep(DEBOUNCE_SECONDS)
            if not self.has("live_leds"):  # stock: status lights cost a flash write, so wait for the taps
                self._wait_keys_quiet(KEY_QUIET_SECONDS)
            self.wake.clear()
            try:
                self._sync()
                self._refresh_info()
            except Exception as e:  # unplugged / BT asleep: drop the handle, retry later
                self.status.update(connected=False, transport=None, error=str(e), battery=None, syncing=None)
                if self.pad is not None:
                    self.log(f"numpad lost: {e}")
                    try:
                        self.pad.close()
                    except Exception:
                        pass
                self.pad = None
                with self.lock:
                    self.dirty = True

    def _connect(self):
        p, l = self.config.active_slot()
        self.pad = Numpad(profile=p, layer=l)
        self._live_sent, self._live_slot = {}, None  # a numpad that was away shows no live colours
        self._next_info = 0.0
        self.status.update(connected=True, transport="Bluetooth" if self.pad.bluetooth else "USB", error=None)
        self._load_cache()
        self._detect_firmware()
        if not self.pad.bluetooth:
            self._verify_cache()
        self.log(f"numpad connected over {self.status['transport']}; "
                 f"{sum(f'{p}.{l}' in self._sent for p, l in SLOTS)} of {len(SLOTS)} slots already up to date")
        with self.lock:
            self.dirty = self.switch_pending = True
        self._hidden_due = time.time()  # bring the hidden slots up to date right after the active one

    def _sync(self):
        if self._layer_flash_until and time.time() >= self._layer_flash_until:
            self._layer_flash_until = 0.0
            with self.lock:
                self.dirty = True  # the layer flash is over: give the side bars back to OBS
        if self._battery_until and time.time() >= self._battery_until:
            self._battery_until = 0.0
            with self.lock:
                self.dirty = True
        with self.lock:
            lost, self._lost = self._lost, False
        if lost and self.pad is not None:
            self.log("numpad disconnected")
            try:
                self.pad.close()
            except Exception:
                pass
            self.pad = None
        if lost and not self._replugged:  # wait for on_reconnected (or the regular retry)
            return
        if self._replugged:
            self._replugged = False
            if self.pad is not None:
                self.log("numpad reconnected: repainting")
                try:
                    self.pad.close()
                except Exception:
                    pass
                self.pad = None
        if self.pad is None:
            self._connect()
        with self.lock:
            dirty, switch, resync = self.dirty, self.switch_pending, self.resync_pending
            self.dirty = self.switch_pending = self.resync_pending = False
        if resync:
            self._sent, self._sent_macros = {}, {}
            self.log("resync: writing every profile and layer again")
            self._hidden_due = time.time()
        wrote = False
        if dirty or switch:
            wrote = self._write_device_settings()
            w, _ = self._write_slot(*self.config.active_slot(), active=True, switch=switch)
            wrote = wrote or w
            if self._hidden_due is None or self._hidden_due > time.time():  # unless a full pass is due now
                self._hidden_due = time.time() + HIDDEN_DELAY  # hidden slots follow once edits settle
        if self._hidden_due and time.time() - self._last_key < HIDDEN_KEY_QUIET:
            self._hidden_due = max(self._hidden_due, self._last_key + HIDDEN_KEY_QUIET)  # keys in use
        if self._hidden_due and time.time() >= self._hidden_due:
            self._hidden_due = None
            lighting = False
            for slot in SLOTS:
                if self._paused:  # firmware update waiting: finish the hidden slots later
                    self._hidden_due = time.time()
                    break
                if self.dirty or self.switch_pending:  # a new change arrived: serve the active slot first
                    self._hidden_due = time.time()
                    break
                if time.time() - self._last_key < HIDDEN_KEY_QUIET:  # someone is pressing keys: later
                    self._hidden_due = self._last_key + HIDDEN_KEY_QUIET
                    break
                if slot != self.config.active_slot():
                    self.status["syncing"] = f"profile {slot[0]}, layer {slot[1]}"
                    w, lit = self._write_slot(*slot, active=False)
                    wrote, lighting = wrote or w, lighting or lit
            self.status["syncing"] = None
            if lighting:
                # Hidden lighting writes leak into what the numpad shows: cmd 0x06 copies into the live
                # LEDs, and every frame overwrites the firmware's current profile/layer (0x20000212/13),
                # which left the active effect broken (seen over BT after an effect change). Re-apply
                # the active slot (0x01 reloads its effect and LEDs), then repaint it.
                self._write_slot(*self.config.active_slot(), active=True, switch=True, force_leds=True)
        if wrote:
            self._save_cache()

    def _write_slot(self, p, l, active, switch=False, force_leds=False):
        """Write whatever differs from what slot (p, l) holds. Returns (wrote anything, wrote lighting).
        active: the numpad shows this slot (writes end with cmd 0x01); otherwise write silently."""
        macros = self.config.get("macros")
        live_leds = self.has("live_leds")
        light, lay, colors, breathing, base, static, live, under = self.compose(p, l, with_overlay=not live_leds)
        knob = keymap.knob_event_mode(lay["controls"], self.config)
        sent = self._sent.setdefault(f"{p}.{l}", {})
        t0, wrote = time.time(), False
        self.pad.profile, self.pad.layer = p, l
        self.pad.effect_color = tuple(sent["effect_color"]) if sent.get("effect_color") else None
        if active and switch:
            self.pad.apply_profile()
        if active:
            self.status.update(profile=p, layer=l)
        for mid, m in macros.items():  # global: upload new or edited macros once
            events = _norm(m["events"])
            if self._sent_macros.get(mid) != events:
                self.pad.set_macro(int(mid), [tuple(e) for e in events])
                self._sent_macros[mid] = events
                wrote = True
        table = keymap.key_table(lay["keymap"], macros)
        if _norm(table) != sent.get("keys"):
            self.pad.set_key_bindings(table, apply=active)
            sent["keys"], sent["table"] = _norm(table), list(Numpad.key_table_bytes(table))
            wrote = True
        modes = _norm(keymap.macro_modes(lay["keymap"], macros))
        if modes != sent.get("modes", {}):
            self.pad.set_macro_modes(modes)
            sent["modes"] = modes
            wrote = True
        args = dict(effect=light["effect"], color=base, speed=light["speed"], brightness=light["brightness"],
                    sidelights=True, multicolor=bool(light.get("multicolor")),  # see compose: side lights
                    scan_ms=self.config.get("device")["scan_ms"])
        lighting_written = False
        if _norm(args) != sent.get("effect"):  # effect commands cost ~1 s + a flash write; skip if unchanged
            self.pad.set_effect(**args, apply=active)
            sent["effect"], sent["effect_color"] = _norm(args), _norm(self.pad.effect_color)
            wrote = lighting_written = True
        if knob != sent.get("knob"):
            self.pad.set_knob_events(knob, colors, breathing=breathing)
            sent["knob"] = knob
            wrote = lighting_written = True  # 0x09 carries LED entries too
            self._live_sent = {}  # LED writes replace the numpad's live table (add-on finding)
        # Animated effects: send only the overriding LEDs (an empty frame clears old overrides).
        frame = _norm([sorted(colors.items()), sorted(breathing), static, sent.get("effect")])
        if force_leds or frame != sent.get("leds"):
            # cmd 0x06 also copies the frame into the live LEDs (firmware-update.md), so a repaint of
            # the active slot needs no 0x01 profile apply: that reload costs time in which the numpad
            # misses key taps. Apply only when the slot showed no per-key colours before (the
            # renderer may be switched off then) or after a hidden-slot leak.
            prev = sent.get("leds")
            apply = active and (force_leds or not prev or not prev[0])
            self.pad.set_leds(colors, breathing=breathing, only=not static, apply=apply)
            sent["leds"] = frame
            wrote = lighting_written = True
            self._live_sent = {}  # cmd 0x06 copies into the live table whatever slot it targets
            if active:
                self.status["last_paint_s"] = round(time.time() - t0, 2)
        if active and live_leds:
            if wrote:  # an apply (0x01, sent by the key-table and effect writes too) reloads the
                self._live_sent = {}  # slot's saved colours over the live ones: send them all again
            self._push_live(p, l, live, under)
            self.status["last_paint_s"] = round(time.time() - t0, 2)
        return wrote, lighting_written

    def _push_live(self, p, l, live, under):
        """Add-on only: send the status lights as live LEDs (RAM, no flash write), just the changes.
        A profile/layer switch or any LED write clears the numpad's live colours, so start over then."""
        if self._live_slot != (p, l):
            self._live_sent, self._live_slot = {}, (p, l)
        changed = {i: v for i, v in live.items() if self._live_sent.get(i) != v}
        gone = [i for i in self._live_sent if i not in live]
        solid = {i: c for i, (c, br) in changed.items() if not br}
        solid.update({i: under[i] for i in gone if i in under})  # back to the user's own colour
        breathe = {i: c for i, (c, br) in changed.items() if br}
        remove = {i: (0, 0, 0) for i in gone if i not in under}  # nothing underneath: show the effect
        for colors, mode in ((solid, Numpad.LIVE_SOLID), (breathe, Numpad.LIVE_BREATHE), (remove, Numpad.LIVE_REMOVE)):
            if colors:
                self.pad.set_live_leds(colors, mode)
        self._live_sent = dict(live)

    # ---- firmware add-on ----
    def _detect_firmware(self):
        """Stock or with our add-on? Asked over USB (read request 0x60); over Bluetooth the last USB
        answer is used, since feature-report readback only exists on USB."""
        if self.pad.bluetooth:
            info, source = self._fw_addon_cached, "last USB check" if self._fw_addon_cached else None
        else:
            info, source = self.pad.suite_firmware(), "detected"
            self._fw_addon_cached = info
            self._save_cache()
        self.status.update(fw_addon=info, fw_addon_source=source)
        self.log("firmware: " + (f"suite add-on v{info['version']} ({', '.join(self.features())})"
                                 if info else "stock (no suite add-on)"))

    def features(self):
        """Names of the add-on features the numpad has (empty on stock firmware)."""
        info = self.status.get("fw_addon")
        return [name for bit, name in FW_FEATURES.items() if info and info["features"] & bit]

    def has(self, feature):
        return feature in self.features()

    def _write_device_settings(self):
        """Polling rate + lighting sleep (cmd 0x08, numpad-wide), only when they changed."""
        dev = self.config.get("device")
        want = _norm({"polling_hz": dev["polling_hz"], "led_sleep_min": dev["led_sleep_min"]})
        if self._sent.get("device") == want:
            return False
        self.pad.set_device_settings(dev["polling_hz"], dev["led_sleep_min"])
        self._sent["device"] = want
        return True

    # ---- cache of what the numpad holds ----
    def _load_cache(self):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self._sent, self._sent_macros = data.get("slots", {}), data.get("macros", {})
            self._fw_addon_cached = data.get("fw_addon")
        except (OSError, ValueError):
            self._sent, self._sent_macros = {}, {}

    def _save_cache(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"slots": self._sent, "macros": self._sent_macros, "fw_addon": self._fw_addon_cached}, f)
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass

    def _verify_cache(self):
        """USB only: drop cached slots whose key table on the numpad differs (factory reset, CORE...).
        If any slot was changed behind our back, macros are re-uploaded too."""
        stale = False
        for p, l in SLOTS:
            key = f"{p}.{l}"
            if key not in self._sent:
                continue
            self.pad.profile, self.pad.layer = p, l
            for _ in range(2):  # the first reply after other traffic can be stale: check its slot
                t = self.pad.read_key_table()
                if (t["profile"], t["layer"]) == (p, l):
                    break
            if (t["profile"], t["layer"]) != (p, l) or t["types"] + t["codes"] != self._sent[key].get("table"):
                del self._sent[key]
                stale = True
        if stale:
            self._sent_macros = {}
            self.log("some profiles/layers changed outside the suite; rewriting them")

    def _refresh_info(self):
        if self.pad is None or time.time() < self._next_info:
            return
        self._next_info = time.time() + INFO_SECONDS
        before = self.status.get("battery")
        if self.pad.bluetooth:
            try:
                self.status["battery"] = ble_battery()
            except Exception as e:
                self.status["battery"] = None
                self.log(f"Bluetooth battery not readable: {e}")
        else:
            p, l = self.config.active_slot()
            self.pad.profile, self.pad.layer = p, l
            self.status.update(self.pad.device_info())
        warn = self.config.get("battery_warning")
        battery = self.status.get("battery")
        if self.pad.bluetooth and warn["enabled"] and battery is not None and battery <= warn["level"]:
            # On USB it charges, so only Bluetooth warns. Every read (INFO_SECONDS) while low = a reminder.
            self.log(f"battery low: {battery}%")
            self._battery_until, self._battery_breathe = time.time() + BATTERY_WARN_SECONDS, True
            with self.lock:
                self.dirty = True
            self.wake.set()
        if self.status.get("battery") != before and time.time() < self._battery_until:
            with self.lock:
                self.dirty = True  # the gauge was drawn with the old reading (or none)
            self.wake.set()
