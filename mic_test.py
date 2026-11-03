r"""
mic_test.py - Test whether Windows reports the microphone as "in use" during a Teams call,
also while you are muted in Teams.

Reads only. Nothing is changed on the PC and nothing is sent to EMO.

How it works (Windows CapabilityAccessManager registry):
  HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone
  Every app that used the mic has a subkey with a value LastUsedTimeStop.
  LastUsedTimeStop == 0  ->  that app is using the mic right now.

Run:   py mic_test.py      (or: python mic_test.py)
Stop:  Ctrl+C
"""

import time
import winreg
from datetime import datetime

BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
POLL_SECONDS = 2


def apps_using(device: str) -> list[str]:
    """Return the app keys under ConsentStore\\<device> whose LastUsedTimeStop is 0."""
    active = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{BASE}\\{device}")
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


def short(name: str) -> str:
    # NonPackaged keys look like C:#Users#...#ms-teams.exe -> keep the exe name
    return name.split("#")[-1].split("\\")[-1]


def main():
    print("Watching microphone and webcam use. Join a Teams call, mute yourself, unmute, leave.")
    print("Ctrl+C to stop.\n")
    last = None
    while True:
        mic = apps_using("microphone")
        cam = apps_using("webcam")
        state = (tuple(sorted(mic)), tuple(sorted(cam)))
        if state != last:
            now = datetime.now().strftime("%H:%M:%S")
            mic_txt = ", ".join(short(a) for a in mic) or "-"
            cam_txt = ", ".join(short(a) for a in cam) or "-"
            print(f"{now}  mic in use by: {mic_txt:<30}  webcam in use by: {cam_txt}")
            last = state
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
