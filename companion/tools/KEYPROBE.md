# keyprobe checklist

Goal: see what Windows receives for each Magic Keyboard key over USB-C and over Bluetooth, and whether the Fn state is visible to user mode.

## Run

```
python companion\tools\keyprobe.py --list        # collections, caps, openable?
python companion\tools\keyprobe.py 90 > usb.txt  # 90 s capture (use bt.txt for Bluetooth)
```

Use a normal (non-elevated) terminal. Nothing is swallowed. Click into the terminal is NOT required (input sink). Stop other key-capturing tools (companion/server.py is fine to leave running, but note its remapping may alter LL results).
Do the full list once with the keyboard on USB-C, once on Bluetooth (unplug the cable, wait for reconnect). Pause ~1 s between presses so the summary groups them separately. The `MAGIC` tag marks events from the Magic Keyboard.

## Keys, in order (press, release, pause)

1. Fn alone (3 presses)
2. F1 ... F12 alone, one at a time
3. Fn+F1 ... Fn+F12
4. Fn+Backspace (Delete)
5. Fn+Left, Fn+Right, Fn+Up, Fn+Down (Home/End/PgUp/PgDn)
6. Eject or Lock key (top-right)
7. Section/backtick key (the one left of Z on ISO, and the one under Esc)
8. Caps Lock (tap, then once more to turn off)
9. Each modifier, left then right: Shift, Control, Option/Alt, Command
10. Cmd+Space

## What to look at

- Does Fn alone produce any RI-KBD / LL / RI-HID / READ line? If only a READ or RI-HID vendor line (page FF00/FF01) appears, Fn is visible there; compare report bytes with vs without Fn held.
- For F-keys: is the event a plain F1..F12 VK, or a media/brightness consumer report (RI-HID rid=0x02/0x03 or READ)?
- For Fn+key: does Windows see Delete/Home/etc. already translated, or the raw key plus a separate Fn indicator?
- Compare USB-C vs Bluetooth: which sources fire for the same key.
- The SUMMARY at the end lists, per cluster, which sources saw it and any vendor-page reports.
