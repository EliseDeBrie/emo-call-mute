r"""
emo_callmute.py - Mute EMO (and put him in Carry Mode) while you are in a Teams call,
restore afterwards.

How it works
  Call detection : Windows registry  HKCU\...\CapabilityAccessManager\ConsentStore\microphone
                   Teams holds the mic for the whole call (also while muted in Teams).
  EMO control    : Bluetooth, same protocol as the EMO app (reverse-engineered,
                   see JoVe13/Emo-Scripts volume.py / carry.py).

On call start : save EMO's current volume + carry state (in memory and in
                emo_callmute.state.json), then set volume=mute, carry=on.
During call   : every RECHECK_SECONDS reconnect and re-apply mute/carry if EMO changed them.
On call end   : restore the saved volume and carry state. If EMO cannot be reached,
                keep trying every RETRY_SECONDS until it works. The state file survives
                a restart of the script, so a crash or reboot during a call does not
                make "muted" look like EMO's normal state.

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
import traceback
import winreg
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak not installed - run:  pip install bleak")

# ── settings ──────────────────────────────────────────────────────────────────
POLL_SECONDS = 2          # how often to check the mic
RECHECK_SECONDS = 300     # during a call: re-apply mute/carry every 5 min
RETRY_SECONDS = 120       # EMO could not be reached (to mute or to restore): try again every 2 min
RETRY_PAUSE = 5           # pause between attempts inside one retry round
TEAMS_MARKERS = ("teams",)  # app key must contain this (case-insensitive) unless --any-app
DEFAULT_EMO_MAC = None    # set your EMO's Bluetooth address here to skip the 10 s scan, e.g. "AA:BB:CC:DD:EE:FF"
EMO_NAME_PREFIX = "EMO"   # scan: device name must start with this (EMO advertises as EMO-xxxx)

CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
CHUNK_SIZE = 20
CHUNK_DELAY = 0.02
SETTING_IN_MIN_VERSION = 21
VOLUME_OPS = ["volume_mute", "volume_low", "volume_med", "volume_high"]
VOLUME_NAMES = ["mute", "low", "med", "high"]

REG_BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"


HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "emo_callmute.log")
STATE_FILE = os.path.join(HERE, "emo_callmute.state.json")


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


# ── saved EMO state (survives a restart of the script) ────────────────────────
def valid_state(volume, carry) -> bool:
    # bool is a subclass of int; reject it for volume so `true` is not read as "low"
    return type(volume) is int and 0 <= volume < len(VOLUME_OPS) and carry in (0, 1)


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f"Ignoring unreadable state file {STATE_FILE}: {e}")
        return {}
    if not isinstance(s, dict) or not valid_state(s.get("volume"), s.get("carry")):
        log(f"Ignoring invalid state file {STATE_FILE}: {s!r}")
        return {}
    return {"volume": s["volume"], "carry": int(s["carry"])}


def save_state(saved: dict):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(saved, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"  could not write state file: {e}")


def clear_state():
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"  could not remove state file: {e}")


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
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                return None
            return msg if isinstance(msg, dict) else None
        return None


class Target:
    """Where EMO is. A scanned address is remembered so later connects skip the scan."""

    def __init__(self, address: str | None):
        self.pinned = address
        self.address = address

    def forget(self):
        if not self.pinned:
            self.address = None


async def find_emo() -> str:
    for d in await BleakScanner.discover(timeout=10):
        if d.name and d.name.upper().startswith(EMO_NAME_PREFIX):
            log(f"  found {d.name} at {d.address}")
            return d.address
    raise RuntimeError("EMO not found via Bluetooth scan")


class Emo:
    """One short Bluetooth session: connect, do things, disconnect."""

    def __init__(self, target: Target):
        self.target = target
        self.client = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.parser = Parser()
        self.loop = None
        self.version_num = 99
        self.pref: dict = {}

    async def __aenter__(self):
        if not self.target.address:
            self.target.address = await find_emo()
        self.loop = asyncio.get_running_loop()
        self.client = BleakClient(self.target.address, timeout=20)
        try:
            await self.client.connect()
        except BaseException:
            self.target.forget()  # address may be stale: scan again next time
            raise
        # From here on the link is open: if anything fails, close it before giving up,
        # because `async with` does not call __aexit__ when __aenter__ raises.
        try:
            await self.client.start_notify(CHAR_UUID, self._on_notify)
            await asyncio.sleep(0.3)
            ver = await self._status(1, "version")
            num = (ver or {}).get("number")
            self.version_num = num if type(num) is int else 99
            pref = await self._status(12, "preference")
            if not pref:
                raise RuntimeError("EMO did not report his settings")
            self.pref = pref
        except BaseException:
            await self.__aexit__()
            raise
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

    async def _wait(self, msg_type: str, accept=None, timeout: float = 8.0):
        end = self.loop.time() + timeout
        while self.loop.time() < end:
            try:
                msg = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == msg_type and (accept is None or accept(msg)):
                return msg
        return None

    async def _status(self, request: int, key: str) -> dict | None:
        """Ask for one status block and return data[key]. Only an answer that really
        contains `key` counts, so a late answer to an earlier request is skipped."""

        def has_key(msg):
            data = msg.get("data")
            return isinstance(data, dict) and isinstance(data.get(key), dict)

        await self._write({"type": "sta_req", "data": {"request": [request]}})
        rsp = await self._wait("sta_rsp", has_key)
        return rsp["data"][key] if rsp else None

    async def _setting_result(self) -> int:
        rsp = await self._wait("setting_rsp", timeout=10)
        data = (rsp or {}).get("data")
        return data.get("result", 0) if isinstance(data, dict) else 0

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
async def set_quiet(target, use_carry, saved):
    """Enter quiet mode. Stores EMO's previous state in `saved` (and the state file)
    unless an earlier, not yet restored state is already there."""
    async with Emo(target) as emo:
        if "volume" not in saved:
            volume, carry = emo.pref.get("volume"), emo.pref.get("carry", 0)
            if not valid_state(volume, carry):
                raise RuntimeError(f"unexpected settings from EMO: volume={volume!r}, carry={carry!r}")
            saved.update(volume=volume, carry=int(carry))
            save_state(saved)
            log(f"  EMO was: volume={VOLUME_NAMES[volume]}, carry={saved['carry']}")
        ops = []
        if emo.pref.get("volume") != 0:
            ops.append("volume_mute")
        if use_carry and emo.pref.get("carry") != 1:
            ops.append("carry_on")
        if not ops:
            log("  EMO already quiet")
            return True
        return await emo.apply(ops)


async def restore(target, use_carry, saved):
    async with Emo(target) as emo:
        ops = []
        if emo.pref.get("volume") != saved["volume"]:
            ops.append(VOLUME_OPS[saved["volume"]])
        if use_carry and emo.pref.get("carry") != saved["carry"]:
            ops.append("carry_on" if saved["carry"] else "carry_off")
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
        if attempt < tries:
            await asyncio.sleep(RETRY_PAUSE)
    log(f"  {what}: giving up for now")
    return False


async def restore_and_clear(target, use_carry, saved, tries=3) -> bool:
    """Restore EMO. Only on success forget the saved state; otherwise keep it (and the
    state file) so the restore is retried later instead of being lost."""
    if await with_retry(lambda: restore(target, use_carry, saved), "restore", tries):
        saved.clear()
        clear_state()
        return True
    log("  restore still pending - EMO state kept for a later attempt")
    return False


async def main(args):
    target = Target(args.emo)
    use_carry = not args.no_carry
    log(f"Watching for calls (Teams only: {not args.any_app}; carry mode: {use_carry}). Ctrl+C to stop.")

    call_active = False
    quiet_applied = False
    saved: dict = load_state()  # mutated in place, never rebound: the retry lambdas share it
    if saved:
        log(f"EMO state saved by an earlier run (volume={VOLUME_NAMES[saved['volume']]}, carry={saved['carry']}) "
            "- will restore it when no call is active")
    last_attempt = float("-inf")  # monotonic time of the last re-check / retry

    try:
        while True:
            now_in_call = in_call(args.any_app)

            if now_in_call and not call_active:
                call_active = True
                log("Call started -> muting EMO")
                quiet_applied = await with_retry(lambda: set_quiet(target, use_carry, saved), "mute")
                last_attempt = time.monotonic()

            elif now_in_call and call_active:
                # EMO reachable: re-check every RECHECK_SECONDS. Not reachable (e.g. not at home):
                # try again every RETRY_SECONDS, quietly, until the call ends.
                due = RECHECK_SECONDS if quiet_applied else RETRY_SECONDS
                if time.monotonic() - last_attempt >= due:
                    log("Re-checking EMO is still quiet" if quiet_applied else "EMO not reached earlier - trying again")
                    quiet_applied = await with_retry(lambda: set_quiet(target, use_carry, saved), "re-mute", tries=1)
                    last_attempt = time.monotonic()

            elif not now_in_call and call_active:
                call_active = False
                quiet_applied = False
                if saved:
                    log("Call ended -> restoring EMO")
                    await restore_and_clear(target, use_carry, saved)
                else:
                    log("Call ended (EMO was never reached, nothing to restore)")
                last_attempt = time.monotonic()

            elif saved and time.monotonic() - last_attempt >= RETRY_SECONDS:
                # No call, but an earlier restore failed (or the script was restarted mid-call).
                log("Restoring EMO (pending from earlier)")
                await restore_and_clear(target, use_carry, saved, tries=1)
                last_attempt = time.monotonic()

            await asyncio.sleep(POLL_SECONDS)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception:
        # Under pyw there is no console: make sure a crash ends up in the log.
        log("Unexpected error:\n" + traceback.format_exc())
        raise
    finally:
        if saved:
            log("Stopping with EMO still quiet -> restoring first")
            if not await restore_and_clear(target, use_carry, saved, tries=3 if call_active else 1):
                log(f"  state kept in {STATE_FILE}; the next start restores EMO")
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
