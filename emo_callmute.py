r"""
emo_callmute.py - Mute EMO (and put him in Carry Mode) while you are in a Teams call,
restore afterwards.

How it works
  Call detection : Windows registry  HKCU\...\CapabilityAccessManager\ConsentStore\microphone
                   Teams holds the mic for the whole call (also while muted in Teams).
  EMO control    : Bluetooth, same protocol as the EMO app (reverse-engineered,
                   see JoVe13/Emo-Scripts volume.py / carry.py).

On call start : save EMO's current volume + carry state, then set volume=mute, carry=on.
During call   : every RECHECK_SECONDS reconnect and re-apply mute/carry if EMO changed them.
On call end   : restore the saved volume and carry state.

Requirements  : Python 3, `pip install bleak`, Bluetooth on, EMO app on the phone closed.
Run           : py emo_callmute.py            (Ctrl+C to stop; restores EMO if a call is active)
Options       : --no-carry      only mute, do not use Carry Mode
                --any-app       trigger on any app using the mic, not only Teams
                --emo MAC       skip scanning, connect to this address
"""

import argparse
import asyncio
import json
import os
import sys
import time
import winreg
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak not installed - run:  pip install bleak")

# ── settings ──────────────────────────────────────────────────────────────────
POLL_SECONDS = 2          # how often to check the mic
RECHECK_SECONDS = 300     # during a call: re-apply mute/carry every 5 min
RETRY_SECONDS = 120       # during a call, when EMO could not be reached: try again every 2 min
TEAMS_MARKERS = ("teams",)  # app key must contain this (case-insensitive) unless --any-app
DEFAULT_EMO_MAC = None    # set your EMO's Bluetooth address here to skip the 10 s scan, e.g. "AA:BB:CC:DD:EE:FF"

CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
CHUNK_SIZE = 20
CHUNK_DELAY = 0.02
SETTING_IN_MIN_VERSION = 21
VOLUME_OPS = ["volume_mute", "volume_low", "volume_med", "volume_high"]
VOLUME_NAMES = ["mute", "low", "med", "high"]

REG_BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emo_callmute.log")


def log(msg: str):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── call detection ────────────────────────────────────────────────────────────
def apps_using_mic() -> list[str]:
    active = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{REG_BASE}\\microphone")
    except OSError:
        return active

    def walk(key, path):
        try:
            value, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
            if value == 0:
                active.append(path)
        except OSError:
            pass
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(key, name) as sub:
                    walk(sub, f"{path}\\{name}" if path else name)
            except OSError:
                pass

    with root:
        walk(root, "")
    return active


def in_call(any_app: bool) -> bool:
    apps = apps_using_mic()
    if any_app:
        return bool(apps)
    return any(m in a.lower() for a in apps for m in TEAMS_MARKERS)


# ── EMO bluetooth ─────────────────────────────────────────────────────────────
def frame(obj: dict) -> bytes:
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    n = len(payload)
    return bytes([0xBB, 0xAA, n & 0xFF, n >> 8]) + payload


class Parser:
    def __init__(self):
        self.buf = bytearray()
        self.total = 0

    def feed(self, data: bytes):
        if len(data) >= 4 and data[0] == 0xBB and data[1] == 0xAA:
            self.total = data[2] + data[3] * 256
            self.buf = bytearray(data[4:])
        elif self.total:
            self.buf.extend(data)
        if self.total and len(self.buf) >= self.total:
            raw = bytes(self.buf[: self.total])
            self.total = 0
            self.buf = bytearray()
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return None
        return None


class Emo:
    """One short Bluetooth session: connect, do things, disconnect."""

    def __init__(self, address: str | None):
        self.address = address
        self.client = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.parser = Parser()
        self.loop = None
        self.version_num = 99
        self.pref: dict = {}

    async def __aenter__(self):
        if not self.address:
            for d in await BleakScanner.discover(timeout=10):
                if d.name and "EMO" in d.name.upper():
                    self.address = d.address
                    break
            if not self.address:
                raise RuntimeError("EMO not found via Bluetooth scan")
        self.loop = asyncio.get_event_loop()
        self.client = BleakClient(self.address, timeout=20)
        await self.client.connect()
        await self.client.start_notify(CHAR_UUID, self._on_notify)
        await asyncio.sleep(0.3)
        # firmware version
        await self._write({"type": "sta_req", "data": {"request": [1]}})
        rsp = await self._wait("sta_rsp")
        ver = ((rsp or {}).get("data") or {}).get("version") or {}
        self.version_num = ver.get("number", 99)
        # preferences
        await self._write({"type": "sta_req", "data": {"request": [12]}})
        rsp = await self._wait("sta_rsp")
        self.pref = ((rsp or {}).get("data") or {}).get("preference") or {}
        return self

    async def __aexit__(self, *exc):
        try:
            await self.client.stop_notify(CHAR_UUID)
        except Exception:
            pass
        try:
            await self.client.disconnect()
        except Exception:
            pass

    def _on_notify(self, _, data):
        msg = self.parser.feed(bytes(data))
        if msg:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, msg)

    async def _write(self, obj: dict):
        packet = frame(obj)
        chunks = [packet[i : i + CHUNK_SIZE] for i in range(0, len(packet), CHUNK_SIZE)]
        for c in chunks:
            await self.client.write_gatt_char(CHAR_UUID, c, response=False)
            if len(chunks) > 1:
                await asyncio.sleep(CHUNK_DELAY)

    async def _wait(self, msg_type: str, timeout: float = 8.0):
        end = self.loop.time() + timeout
        while self.loop.time() < end:
            try:
                msg = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == msg_type:
                return msg
        return None

    async def _setting_result(self) -> int:
        rsp = await self._wait("setting_rsp", timeout=10)
        return ((rsp or {}).get("data") or {}).get("result", 0)

    async def apply(self, ops: list[str]) -> bool:
        """Send a list of setting ops inside one setting_in/out session."""
        if not ops:
            return True
        handshake = self.version_num >= SETTING_IN_MIN_VERSION
        if handshake:
            await self._write({"type": "setting_req", "data": {"op": "in"}})
            if await self._setting_result() != 1:
                log("  setting_in rejected - EMO busy?")
                return False
        ok = True
        for op in ops:
            await self._write({"type": "setting_req", "data": {"op": op}})
            res = await self._setting_result()
            log(f"  {op}: {'ok' if res == 1 else f'failed ({res})'}")
            ok = ok and res == 1
        if handshake:
            await self._write({"type": "setting_req", "data": {"op": "out"}})
            await self._setting_result()
        return ok


# ── main loop ─────────────────────────────────────────────────────────────────
async def set_quiet(address, use_carry, saved):
    """Enter quiet mode. Stores EMO's previous state in `saved` on first call."""
    async with Emo(address) as emo:
        if "volume" not in saved:
            saved["volume"] = emo.pref.get("volume", 2)
            saved["carry"] = emo.pref.get("carry", 0)
            log(f"  EMO was: volume={VOLUME_NAMES[saved['volume']]}, carry={saved['carry']}")
        ops = []
        if emo.pref.get("volume") != 0:
            ops.append("volume_mute")
        if use_carry and emo.pref.get("carry") != 1:
            ops.append("carry_on")
        if not ops:
            log("  EMO already quiet")
            return True
        return await emo.apply(ops)


async def restore(address, use_carry, saved):
    async with Emo(address) as emo:
        ops = []
        if emo.pref.get("volume") != saved.get("volume", 2):
            ops.append(VOLUME_OPS[saved.get("volume", 2)])
        if use_carry and emo.pref.get("carry") != saved.get("carry", 0):
            ops.append("carry_on" if saved.get("carry") else "carry_off")
        if not ops:
            log("  EMO already restored")
            return True
        return await emo.apply(ops)


async def with_retry(coro_factory, what, tries=3):
    for attempt in range(1, tries + 1):
        try:
            if await coro_factory():
                return True
        except Exception as e:
            log(f"  {what} attempt {attempt}/{tries} failed: {e}")
        await asyncio.sleep(5)
    log(f"  {what}: giving up for now")
    return False


async def main(args):
    address = args.emo
    use_carry = not args.no_carry
    log(f"Watching for calls (Teams only: {not args.any_app}; carry mode: {use_carry}). Ctrl+C to stop.")

    call_active = False
    quiet_applied = False
    saved: dict = {}
    last_recheck = 0.0

    try:
        while True:
            now_in_call = in_call(args.any_app)

            if now_in_call and not call_active:
                call_active = True
                log("Call started -> muting EMO")
                quiet_applied = await with_retry(lambda: set_quiet(address, use_carry, saved), "mute")
                last_recheck = time.monotonic()

            elif now_in_call and call_active:
                # EMO reachable: re-check every RECHECK_SECONDS. Not reachable (e.g. not at home):
                # try again every RETRY_SECONDS, quietly, until the call ends.
                due = RECHECK_SECONDS if quiet_applied else RETRY_SECONDS
                if time.monotonic() - last_recheck >= due:
                    log("Re-checking EMO is still quiet" if quiet_applied else "EMO not reached earlier - trying again")
                    quiet_applied = await with_retry(lambda: set_quiet(address, use_carry, saved), "re-mute", tries=1)
                    last_recheck = time.monotonic()

            elif not now_in_call and call_active:
                call_active = False
                log("Call ended -> restoring EMO")
                if saved:
                    await with_retry(lambda: restore(address, use_carry, saved), "restore")
                saved = {}
                quiet_applied = False

            await asyncio.sleep(POLL_SECONDS)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if call_active and saved:
            log("Stopping during a call -> restoring EMO first")
            await with_retry(lambda: restore(address, use_carry, saved), "restore")
        log("Stopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Mute EMO during Teams calls.")
    p.add_argument("--emo", default=DEFAULT_EMO_MAC, help="EMO Bluetooth address (default: scan for a device named EMO-xxxx)")
    p.add_argument("--no-carry", action="store_true", help="only mute, do not use Carry Mode")
    p.add_argument("--any-app", action="store_true", help="trigger on any app using the mic")
    a = p.parse_args()
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        pass
