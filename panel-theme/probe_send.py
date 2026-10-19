"""Drive the overlay probe (see probe_overlay.py).  Quit Starfleet Command first:
it writes channels 1..21 every 2 s and would fight this.

    ./.venv/bin/python probe_send.py            # ~54 s, three cover cycles
    ./.venv/bin/python probe_send.py --dry-run  # print the reports, touch nothing
"""
from __future__ import annotations

import argparse
import datetime
import struct
import sys
import time

import probe_overlay as po

CYCLE = 18                      # seconds: 6 off, 6 on, 6 off
VID, PID = 0x0483, 0x0065


HOLD: int | None = None         # --hold on|off pins the cover instead of cycling


def phase(t: int) -> tuple[int, str]:
    if HOLD is not None:
        return HOLD, "HOLD"
    slot = t % CYCLE
    if slot < 6:
        return 0, "OFF "
    if slot < 12:
        return 1, "ON  "
    return 0, "OFF2"


def sensor_report(pairs: list[tuple[int, int]]) -> bytes:
    assert len(pairs) <= 20
    body = b"".join(struct.pack(">Bh", channel, value) for channel, value in pairs)
    return (bytes([0x02, len(pairs)]) + body).ljust(64, b"\0")


def datetime_report(brightness: int) -> bytes:
    now = datetime.datetime.now()
    return bytes([0x03, 0x01, 0x15, now.year - 2000, now.month, now.day,
                  now.hour, now.minute, now.second, 0, brightness]).ljust(64, b"\0")


def tick(t: int) -> tuple[list[tuple[int, int]], str]:
    cover, name = phase(t)
    counter = t % 100
    words = 1 + (t // 2) % 3
    pairs = [
        (po.CH_COUNTER_UNDER, counter),
        (po.CH_COVER, cover),
        (po.CH_COUNTER_OVER, counter),
        (po.CH_MODE, cover),
        (po.CH_SHARED, 5),
        (po.CH_WORDS, words),
    ] + [(channel, i + 1) for i, channel in enumerate(po.CH_FAR)]
    return pairs, f"t={t:>3}s  {name}  counter={counter:>2}  word={('ONE','TWO','THREE')[words - 1]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seconds", type=int, default=54)
    ap.add_argument("--hold", choices=("on", "off"),
                    help="pin the cover on or off instead of cycling it")
    args = ap.parse_args()
    global HOLD
    HOLD = None if args.hold is None else int(args.hold == "on")

    device = None
    if not args.dry_run:
        import hid
        if len(hid.enumerate(VID, PID)) != 1:
            print("expected exactly one panel attached", file=sys.stderr)
            return 1
        device = hid.device()
        device.open(VID, PID)

    try:
        for t in range(args.seconds):
            pairs, line = tick(t)
            print(line, flush=True)
            if device is None:
                if t < 2:
                    print("   ", sensor_report(pairs)[:2 + 3 * len(pairs)].hex(" "))
                continue
            device.write(b"\x00" + sensor_report(pairs))
            device.write(b"\x00" + datetime_report(60))
            time.sleep(1.0)
    finally:
        if device is not None:
            device.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
