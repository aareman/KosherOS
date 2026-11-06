#!/usr/bin/env python3
"""Type keys on a new virtual keyboard and print what keyd sends on.

    keys.py CODE...      Linux key codes: 45 is x, 21 is y, 63 is F5

Root only (/dev/uinput). The keyboard is made with uinput, the way a USB
keyboard appears, so keyd grabs it as it grabs any other. keyd's own
virtual keyboard is read beside it, and the keys it pressed are printed in
order: what a remap turned the typed keys into. An empty line means keyd
sent no key at all, which is what a command() key does. Exit 2 when keyd
has no virtual keyboard (it is not running).
"""
import fcntl
import os
import re
import select
import struct
import sys
import time

UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502
EV_SYN, EV_KEY = 0, 1
EVENT = struct.Struct("llHHi")


def keyd_output():
    with open("/proc/bus/input/devices") as f:
        blocks = f.read().split("\n\n")
    for block in blocks:
        if 'Name="keyd virtual keyboard"' in block:
            m = re.search(r"Handlers=.*?\b(event\d+)", block)
            if m:
                return "/dev/input/" + m.group(1)
    return None


def main() -> int:
    codes = [int(c) for c in sys.argv[1:]]
    path = keyd_output()
    if path is None:
        print("no keyd virtual keyboard")
        return 2
    out = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for k in range(1, 128):
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 3, 0x4B4F, 0x5052, 1,
                                              b"KosherOS pr-test keyboard", 0))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(2)  # keyd sees the new keyboard and grabs it

    def drain(seconds: float) -> list:
        pressed, end = [], time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([out], [], [], left)
            if not ready:
                break
            data = os.read(out, EVENT.size * 64)
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, kind, code, value = EVENT.unpack_from(data, i)
                if kind == EV_KEY and value == 1:
                    pressed.append(code)
        return pressed

    drain(0.3)
    for code in codes:
        os.write(fd, EVENT.pack(0, 0, EV_KEY, code, 1) + EVENT.pack(0, 0, EV_SYN, 0, 0))
        time.sleep(0.05)
        os.write(fd, EVENT.pack(0, 0, EV_KEY, code, 0) + EVENT.pack(0, 0, EV_SYN, 0, 0))
        time.sleep(0.25)
    print(" ".join(str(c) for c in drain(1.0)))
    fcntl.ioctl(fd, UI_DEV_DESTROY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
