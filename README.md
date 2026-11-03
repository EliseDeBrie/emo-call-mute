# EMO call mute

Keeps your [EMO desktop robot](https://living.ai/emo/) quiet while you are in a Microsoft Teams call.

When Teams starts using the microphone, the script mutes EMO and switches on Carry Mode (so he stops wandering and dancing). When the call ends, it puts his previous volume and Carry Mode setting back. It runs in the background on Windows and needs no admin rights.

Not affiliated with Living.ai. The Bluetooth protocol is reverse-engineered by the community; see [Credits](#credits).

## How it works

- **Call detection:** Windows records per app whether the microphone is in use, under
  `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone`.
  A `LastUsedTimeStop` value of `0` means "in use right now". Teams holds the microphone for the whole call, also while you are muted inside Teams.
- **EMO control:** Bluetooth Low Energy, the same commands the EMO app sends (`volume_mute`, `carry_on`, …), via [bleak](https://github.com/hbldh/bleak).
- During a call the script reconnects every 5 minutes and re-applies mute and Carry Mode in case EMO changed them himself. If EMO is out of range (you are not at home), it retries every 2 minutes and otherwise stays out of the way.
- EMO's settings from before the call are also written to `emo_callmute.state.json`. If restoring fails (EMO out of range, phone app connected), the script keeps retrying every 2 minutes after the call. If the script is stopped or restarted during a call, the next start restores EMO from that file. The file is deleted once EMO is restored.

Tested with EMO firmware 3.2.0.p1 (#42) on Windows 11. Carry Mode needs firmware newer than #35.

## Requirements

- Windows 10/11 with Bluetooth
- Python 3.10 or newer (the per-user installer from python.org needs no admin rights)
- `pip install bleak`
- The EMO phone app must be closed while the script talks to EMO; Bluetooth is exclusive.

## Files

| File | What it does |
|---|---|
| `emo_callmute.py` | The script. Watches for calls, mutes and restores EMO, writes `emo_callmute.log` (and, while EMO is muted, `emo_callmute.state.json`) next to itself. |
| `mic_test.py` | Read-only check: prints which apps hold the microphone or webcam, each time that changes. Use it to see what your Teams looks like to Windows. |
| `emo_test.py` | Read-only check: finds EMO over Bluetooth and prints his firmware and current settings. Changes nothing. |

## Try it first

```powershell
pip install bleak
py mic_test.py      # join a Teams call, mute yourself, leave - the mic line should stay on while muted
py emo_test.py      # should print your EMO's name, address, firmware and volume
py emo_callmute.py  # join a call: EMO goes quiet; leave: he comes back. Ctrl+C restores and stops.
```

Options for `emo_callmute.py`:

| Option | Effect |
|---|---|
| `--emo AA:BB:CC:DD:EE:FF` | Connect to this address and skip the 10-second scan. `emo_test.py` prints yours. You can also set `DEFAULT_EMO_MAC` at the top of the script. Recommended: without it the script connects to the first Bluetooth device whose name starts with `EMO`. |
| `--no-carry` | Only mute; EMO keeps moving. |
| `--any-app` | Trigger on any app that uses the microphone, not only Teams. |

## Run it automatically at logon

Put the script in a folder outside OneDrive, for example `C:\Users\<you>\EmoCallMute`, then paste this into Windows PowerShell. It registers a scheduled task under your own account, so no admin rights are needed. `pyw` is Python's windowless launcher, so nothing pops up.

```powershell
$dir    = "$env:USERPROFILE\EmoCallMute"
$pyw    = (Get-Command pyw).Source
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dir\emo_callmute.py`"" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName "EMO call mute" -Action $action -Trigger $trigger -Settings $settings
Start-ScheduledTask -TaskName "EMO call mute"
```

`-ExecutionTimeLimit 0` matters: without it Windows stops the task after three days.

Manage it:

```powershell
Get-ScheduledTask -TaskName "EMO call mute" | Select State        # Running = ok
Get-Content "$env:USERPROFILE\EmoCallMute\emo_callmute.log" -Tail 10
Stop-ScheduledTask -TaskName "EMO call mute"
Unregister-ScheduledTask -TaskName "EMO call mute" -Confirm:$false
```

If `Start-ScheduledTask` leaves the task in state `Ready` with result `2147942667`, the folder in `$dir` does not exist.

## Known limits

- Carry Mode switches itself off when EMO is put back on his charger; the 5-minute re-check switches it on again.
- Whether mute silences every sound EMO makes (singing included) is not documented by Living.ai. In practice he stayed quiet.
- EMO owners report that he sometimes unmutes himself; turning off *Schedule Sound* in the app (Settings > Preferences) helps.
- The script only knows Windows (`winreg`).
- Without `--emo`, any nearby Bluetooth device that names itself `EMO…` can be picked by the scan. The script only sends it volume and Carry Mode commands and checks the values it gets back, but pinning the address avoids it.
- `emo_callmute.log` records when calls started and ended.

## Credits

- Bluetooth protocol and command names: [JoVe13/Emo-Scripts](https://github.com/JoVe13/Emo-Scripts)
- EMO reverse-engineering knowledge base: [EMO Libre](https://github.com/emo-libre)
- Microphone-in-use registry trick: widely used by Teams-status projects for Home Assistant and busy lights

## License

MIT, see [LICENSE](LICENSE).
