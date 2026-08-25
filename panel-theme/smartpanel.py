"""macOS driver for the llhmi.com / llTechCo 3.5" USB HID smart panel (0483:0065).

Protocol transcribed from James Buren's `hidss` reference implementation
(gitlab.com/braewoods/usb-smart-screen, ctl/device.c) -- that one talks Linux
`/dev/hidraw*` directly, which macOS does not have, so the transport here is
hidapi (IOHIDManager underneath) while the packet layouts are unchanged.

Wire format: 64-byte HID output reports, prefixed by a 0x00 HID report-ID byte
(the descriptor declares no report IDs). Byte 0 of the payload selects the
report type:

    0x00  widget   [type][n][ key, u16be value ] * n
    0x01  command  [type]"reset\0"
    0x02  sensor   [type][n][ key, s16be value ] * n     keys 1..20, see SENSORS
    0x03  datetime [type][0x01][0x15][yr-2000][mon][day][hr][min][sec]
                   [backlight_timeout][brightness 1..100]

Only sensor/datetime/widget are used here. Theme upload (YMODEM) writes the
device's flash and is deliberately NOT implemented in this file.
"""
from __future__ import annotations

import time
from datetime import datetime

import hid

VID, PID = 0x0483, 0x0065
REPORT_SIZE = 64

REPORT_WIDGET = 0x00
REPORT_COMMAND = 0x01
REPORT_SENSOR = 0x02
REPORT_DATETIME = 0x03

# Sensor key -> (name, unit). Fixed by device firmware; a theme chooses which
# of these it actually draws, so writing a key the theme ignores is a no-op.
SENSORS = {
    1: ("cpu_temperature", "C"),
    2: ("cpu_clock", "MHz"),
    3: ("cpu_usage", "%"),
    4: ("cpu_fan", "rpm"),
    5: ("gpu_temperature", "C"),
    6: ("gpu_clock", "MHz"),
    7: ("gpu_usage", "%"),
    8: ("gpu_memory_clock", "MHz"),
    9: ("gpu_memory_usage", "%"),
    10: ("ram_used", "MB"),
    11: ("ram_available", "MB"),
    12: ("ram_usage", "%"),
    13: ("disk_temperature", "C"),
    14: ("disk_total", "GB"),
    15: ("disk_used", "GB"),
    16: ("disk_available", "GB"),
    17: ("disk_usage", "%"),
    18: ("network_upload", "KB/s"),
    19: ("network_download", "KB/s"),
    20: ("sound_volume", "%"),
}


class Panel:
    def __init__(self):
        self.h = hid.device()

    def open(self):
        self.h.open(VID, PID)
        return self

    def close(self):
        try:
            self.h.close()
        except Exception:
            pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    def _write(self, payload: bytes):
        if len(payload) > REPORT_SIZE:
            raise ValueError(f"payload {len(payload)} > {REPORT_SIZE}")
        # Leading 0x00 is the HID report-ID byte, not part of the 64-byte payload.
        return self.h.write(bytes([0x00]) + payload.ljust(REPORT_SIZE, b"\x00"))

    def _pairs(self, report_type: int, pairs: list[tuple[int, int]]):
        # 2 header bytes + 3 per pair must fit 64 -> 20 pairs max, which is also
        # exactly the firmware's documented field limit.
        if len(pairs) > 20:
            raise ValueError(f"at most 20 pairs per report, got {len(pairs)}")
        buf = bytearray(REPORT_SIZE)
        buf[0] = report_type
        buf[1] = len(pairs)
        off = 2
        for key, val in pairs:
            buf[off] = key & 0xFF
            # signed 16-bit big-endian for sensors; two's complement for negatives
            buf[off + 1:off + 3] = int(val).to_bytes(2, "big", signed=(val < 0))
            off += 3
        return self._write(bytes(buf))

    def send_sensors(self, values: dict[int, int]):
        """values: {sensor_key 1..20 -> int}. Unlisted keys are left untouched."""
        return self._pairs(REPORT_SENSOR, sorted(values.items()))

    def send_widgets(self, values: dict[int, int]):
        """values: {widget_id -> u16}. Widget ids come from the loaded theme."""
        return self._pairs(REPORT_WIDGET, sorted(values.items()))

    def send_datetime(self, when: datetime | None = None, backlight_timeout: int = 0,
                      brightness: int = 100):
        """brightness 1..100. backlight_timeout in 1/8 s; <8 disables blanking."""
        w = when or datetime.now()
        buf = bytearray(REPORT_SIZE)
        buf[0] = REPORT_DATETIME
        buf[1] = 0x01          # number of fields
        buf[2] = 0x15          # field id 21 = datetime+backlight
        buf[3] = w.year - 2000
        buf[4] = w.month
        buf[5] = w.day
        buf[6] = w.hour
        buf[7] = w.minute
        buf[8] = w.second
        buf[9] = max(0, min(255, backlight_timeout))
        buf[10] = max(1, min(100, brightness))
        return self._write(bytes(buf))


if __name__ == "__main__":
    import sys

    probe = {
        1: 41, 2: 4200, 3: 43, 4: 4400, 5: 45,
        6: 4600, 7: 47, 8: 4800, 9: 49, 10: 5000,
        11: 5100, 12: 52, 13: 53, 14: 5400, 15: 55,
        16: 56, 17: 57, 18: 58, 19: 59, 20: 60,
    }
    with Panel() as p:
        print("opened:", p.h.get_manufacturer_string(), "/", p.h.get_product_string())
        n = p.send_datetime(brightness=100)
        print(f"datetime report written ({n} bytes)")
        time.sleep(0.3)
        n = p.send_sensors(probe)
        print(f"sensor report written ({n} bytes) with {len(probe)} fields")
        for k, v in probe.items():
            name, unit = SENSORS[k]
            print(f"    {k:2d} {name:<20} = {v} {unit}")
        # Re-send a few times: if the theme polls, a single shot can be missed.
        for _ in range(3):
            time.sleep(0.5)
            p.send_sensors(probe)
            p.send_datetime(brightness=100)
    print("done -- look at the panel")
