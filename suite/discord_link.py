"""Discord integration: mute / deafen keys with status lights, over Discord's local RPC pipe.

Discord only lets an app control voice settings over RPC with the "rpc" scopes, and those work
without Discord's review only for the app's owner (and testers added to the app). So the user
creates their own app in the Discord Developer Portal and pastes its client ID and secret into
the dashboard. The first connect shows an "Authorize" popup in Discord; the token is then kept in
the config and refreshed when it runs out.

One worker thread owns the pipe and only reads right after it sends (a synchronous pipe handle
can't read and write from two threads at once), so the mute state is polled, not subscribed to.
See .knowledge/suite.md "Discord integration".
"""
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gmmk_numpad import KEY_LED  # noqa: E402

POLL_SECONDS = 0.5
RETRY_SECONDS = 5.0
SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
TOKEN_URL = "https://discord.com/api/oauth2/token"
REDIRECT_URI = "http://localhost"
OP_HANDSHAKE, OP_FRAME, OP_CLOSE = 0, 1, 2
DISCORD_ACTIONS = ("discord_mute", "discord_deafen")

LIVE_GREEN, MUTED_RED = (0, 200, 60), (220, 0, 0)  # same as the OBS mute keys


def discord_overlay(view, keymap):
    """Discord voice state {"mute", "deaf"} + key map -> {led: colour}. view None -> no lights."""
    if not view:
        return {}
    colors = {}
    for key, b in keymap.items():
        led = KEY_LED.get(key)
        if led is None or b.get("type") != "action":
            continue
        if b["action"] == "discord_mute":
            colors[led] = MUTED_RED if view["mute"] else LIVE_GREEN
        elif b["action"] == "discord_deafen":
            colors[led] = MUTED_RED if view["deaf"] else LIVE_GREEN
    return colors


class DiscordError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class DiscordLink:
    def __init__(self, config, device, log=print):
        self.config, self.device, self.log = config, device, log
        self.status = "off"        # off | setup | not_running | authorizing | connected | error
        self.error = ""
        self.voice = None          # {"mute": bool, "deaf": bool} while connected
        self.wake = threading.Event()
        self.queue = []            # actions pressed on the numpad, run by the worker
        self.retry = True          # False after the user declined in Discord: wait for "Connect"
        self._pipe = None

    def start(self):
        threading.Thread(target=self._worker, daemon=True, name="discord").start()

    # ---- called from other threads ----
    def perform(self, binding, phase="press"):
        self.queue.append((binding["action"], binding.get("hold"), phase))
        self.wake.set()

    def reconnect(self):
        """Dashboard "Connect": drop the pipe and try again (also after the user declined)."""
        self.retry = True
        self._close()
        self.wake.set()

    def forget(self):
        self.config.update("discord", {"access_token": "", "refresh_token": "", "expires_at": 0})
        self.reconnect()

    # ---- pipe ----
    def _open(self):
        for i in range(10):
            try:
                self._pipe = open(rf"\\?\pipe\discord-ipc-{i}", "r+b", buffering=0)
                return
            except OSError:
                continue
        raise FileNotFoundError("Discord is not running")

    def _close(self):
        pipe, self._pipe = self._pipe, None
        if pipe:
            try:
                pipe.close()
            except OSError:
                pass

    def _write(self, op, payload):
        data = json.dumps(payload).encode()
        self._pipe.write(struct.pack("<II", op, len(data)) + data)

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._pipe.read(n - len(buf))
            if not chunk:
                raise OSError("Discord closed the connection")
            buf += chunk
        return buf

    def _read(self):
        op, length = struct.unpack("<II", self._read_exact(8))
        payload = json.loads(self._read_exact(length))
        if op == OP_CLOSE:
            raise DiscordError(payload.get("message", "Discord closed the connection"), payload.get("code"))
        return payload

    def _request(self, cmd, args=None):
        nonce = str(uuid.uuid4())
        self._write(OP_FRAME, {"cmd": cmd, "args": args or {}, "nonce": nonce})
        while True:
            msg = self._read()
            if msg.get("nonce") != nonce:
                continue  # an event (READY, ...) rather than our answer
            if msg.get("evt") == "ERROR":
                raise DiscordError(msg["data"].get("message", "error"), msg["data"].get("code"))
            return msg.get("data") or {}

    # ---- OAuth ----
    def _token(self, cfg, **form):
        form.update(client_id=cfg["client_id"], client_secret=cfg["client_secret"])
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GMMKNumpadSuite (local, 1.0)"})  # Discord's CDN blocks urllib's default UA
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                tok = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")
            raise DiscordError(f"token request failed ({e.code}): {detail[:200]}")
        self.config.update("discord", {"access_token": tok["access_token"],
                                       "refresh_token": tok.get("refresh_token", ""),
                                       "expires_at": time.time() + tok.get("expires_in", 604800)})
        return tok["access_token"]

    def _authorize(self, cfg):
        """Ask the user in Discord (popup), then trade the code for a token."""
        self.status = "authorizing"
        code = self._request("AUTHORIZE", {"client_id": cfg["client_id"], "scopes": SCOPES})["code"]
        try:
            return self._token(cfg, grant_type="authorization_code", code=code)
        except DiscordError:  # apps with a redirect URI set want it repeated here
            return self._token(cfg, grant_type="authorization_code", code=code, redirect_uri=REDIRECT_URI)

    def _login(self):
        cfg = self.config.get("discord")
        self._open()
        self._write(OP_HANDSHAKE, {"v": 1, "client_id": cfg["client_id"]})
        self._read()  # READY
        token = cfg["access_token"] if cfg["expires_at"] > time.time() + 60 else ""
        if not token and cfg["refresh_token"]:
            try:
                token = self._token(cfg, grant_type="refresh_token", refresh_token=cfg["refresh_token"])
            except DiscordError:
                token = ""
        for attempt in range(2):
            if not token:
                token = self._authorize(cfg)
            try:
                self._request("AUTHENTICATE", {"access_token": token})
                break
            except DiscordError:
                if attempt:
                    raise
                token = ""  # revoked or from another app: authorize again
        self.log("Discord connected")

    # ---- worker ----
    def _uses_keys(self):
        for prof in self.config.get("profiles").values():
            for lay in prof["layers"].values():
                if any(b.get("type") == "action" and b.get("action") in DISCORD_ACTIONS
                       for b in lay["keymap"].values()):
                    return True
        return False

    def _set(self, status, error="", voice=None):
        self.status, self.error, self.voice = status, error, voice
        self.device.request_integration("discord", voice)

    def _worker(self):
        while True:
            cfg = self.config.get("discord")
            if not cfg["enabled"]:
                self._close()
                self.queue.clear()
                self._set("off")
            elif not (cfg["client_id"] and cfg["client_secret"]):
                self._close()
                self._set("setup")
            elif self.retry:
                try:
                    if self._pipe is None:
                        self._login()
                    while self.queue:
                        # A press flips mute / deafen. Hold "on" / "off" = the state while held: the
                        # press sets it, the release the other one ("off" on mute = push-to-talk).
                        action, hold, phase = self.queue.pop(0)
                        field = {"discord_mute": "mute", "discord_deafen": "deaf"}.get(action)
                        if not field:
                            continue
                        if hold in ("on", "off"):
                            self._request("SET_VOICE_SETTINGS", {field: (hold == "on") == (phase == "press")})
                        elif phase == "press":
                            current = bool(self._request("GET_VOICE_SETTINGS")[field])
                            self._request("SET_VOICE_SETTINGS", {field: not current})
                    if self._uses_keys() or self.voice is None:
                        voice = self._request("GET_VOICE_SETTINGS")
                        self._set("connected", voice={"mute": bool(voice["mute"]), "deaf": bool(voice["deaf"])})
                    else:
                        self.status = "connected"
                except FileNotFoundError:
                    self._close()
                    self._set("not_running")
                    self.wake.wait(RETRY_SECONDS)
                except (OSError, DiscordError, KeyError, ValueError) as e:
                    self._close()
                    declined = self.status == "authorizing"
                    self._set("error", str(e) or type(e).__name__)
                    self.log(f"Discord: {self.error}")
                    if declined:  # don't pop the Authorize dialog up again by itself
                        self.retry = False
                    self.wake.wait(RETRY_SECONDS)
                except Exception as e:  # a bug must not end the worker silently (status stuck on "off")
                    self._close()
                    self._set("error", f"{type(e).__name__}: {e}")
                    self.log(f"Discord: {self.error}")
                    self.wake.wait(RETRY_SECONDS)
            self.wake.wait(POLL_SECONDS if self.status == "connected" else 2)
            self.wake.clear()
