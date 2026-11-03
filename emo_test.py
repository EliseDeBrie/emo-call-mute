"""
emo_test.py - Read-only Bluetooth test for EMO.

Scans for EMO, connects, reads firmware version + current preferences, disconnects.
Changes nothing.

Protocol: same as JoVe13/Emo-Scripts (volume.py / settings.py), reverse-engineered
from the EMO app. Framing: 0xBB 0xAA <len lo> <len hi> + JSON, in 20-byte chunks.

Before running:
  - Bluetooth on in Windows
  - EMO NOT connected to the phone app at that moment
  - pip install bleak

Run:  py emo_test.py
"""

import asyncio
import json
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak not installed - run:  pip install bleak")

CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
CHUNK_SIZE = 20
CHUNK_DELAY = 0.02

VOLUME_NAMES = {0: "mute", 1: "low", 2: "med", 3: "high"}


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


async def main():
    print("Scanning for EMO (10 s) ...")
    addr = None
    for d in await BleakScanner.discover(timeout=10):
        if d.name and d.name.upper().startswith("EMO"):
            addr = d.address
            print(f"Found: {d.name}  {d.address}")
            break
    if not addr:
        sys.exit("No EMO found. Is Bluetooth on, and is EMO awake and not connected to the app?")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    parser = Parser()

    def on_notify(_, data):
        msg = parser.feed(bytes(data))
        if msg:
            loop.call_soon_threadsafe(queue.put_nowait, msg)

    async def write(obj):
        packet = frame(obj)
        chunks = [packet[i : i + CHUNK_SIZE] for i in range(0, len(packet), CHUNK_SIZE)]
        for c in chunks:
            await client.write_gatt_char(CHAR_UUID, c, response=False)
            if len(chunks) > 1:
                await asyncio.sleep(CHUNK_DELAY)

    async def wait_for(msg_type, timeout=8.0):
        end = loop.time() + timeout
        while loop.time() < end:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == msg_type:
                return msg
        return None

    print(f"Connecting to {addr} ...")
    async with BleakClient(addr, timeout=20) as client:
        print("Connected.")
        await client.start_notify(CHAR_UUID, on_notify)
        await asyncio.sleep(0.3)

        await write({"type": "sta_req", "data": {"request": [1]}})
        rsp = await wait_for("sta_rsp")
        ver = ((rsp or {}).get("data") or {}).get("version") or {}
        print(f"Firmware: {ver.get('name', '?')}  (#{ver.get('number', '?')})")

        await write({"type": "sta_req", "data": {"request": [12]}})
        rsp = await wait_for("sta_rsp")
        pref = ((rsp or {}).get("data") or {}).get("preference") or {}
        if pref:
            print(f"Volume:         {VOLUME_NAMES.get(pref.get('volume'), pref.get('volume'))}")
            print(f"Carry mode:     {pref.get('carry', '?')}")
            print(f"Schedule sound: {pref.get('schedule_sound', '?')}")
            print(f"Always reply:   {pref.get('always_reply', '?')}")
            print(f"Wake sens.:     {pref.get('wake_sens', '?')}")
        else:
            print("No preference response received.")

        await client.stop_notify(CHAR_UUID)
    print("Disconnected. Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
