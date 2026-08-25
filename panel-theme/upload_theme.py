"""Upload an img.dat theme to the llhmi 3.5" HID panel from macOS.

Port of `upload_send()` in James Buren's hidss reference (ctl/common.c +
ctl/device.c), which drives Linux /dev/hidraw directly; here the transport is
hidapi. The YMODEM framing, block sizes and ACK expectations are unchanged.

Sequence:
    1. enter ymodem   0x01"reset\\0"  then  "ymodem\\0"   -> expect ACK 0x43
    2. metadata       SOH block 0: "img.dat\\0<size>\\0"   -> expect ACK 0x43
    3. data stream    STX blocks of 1024 (pad 0x1A)       -> ACK 0x43, last 0x00
       then EOT 0x04                                      -> expect ACK 0x43
    4. closing meta   SOH block 0, empty name             -> expect ACK 0x00

Each YMODEM block is [type][seq][~seq] + payload + CRC16-XMODEM(2), zero-padded
to a multiple of 64 and shipped as consecutive 64-byte HID output reports.

NOTE: theme upload never enters the bootloader (only firmware upload does), so
this writes the theme region only.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import hid

VID, PID = 0x0483, 0x0065
REPORT = 64
SOH, STX, EOT, ACK, NAK = 0x01, 0x02, 0x04, 0x06, 0x15
THEME_MIN, THEME_MAX = 4096, 4194304


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Uploader:
    def __init__(self, verbose: bool = True):
        self.h: hid.device | None = None
        self.verbose = verbose

    def log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    def open(self):
        self.h = hid.device()
        self.h.open(VID, PID)
        self.h.set_nonblocking(False)

    def close(self):
        if self.h is not None:
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    def reopen(self, delay: float = 2.0):
        self.close()
        time.sleep(delay)
        self.open()

    def write_payload(self, payload: bytes):
        """One 64-byte output report; leading 0x00 is the HID report-ID byte."""
        assert self.h is not None
        self.h.write(bytes([0x00]) + payload.ljust(REPORT, b"\x00")[:REPORT])

    def write_block(self, block: bytes):
        """Ship a padded YMODEM block as consecutive 64-byte reports."""
        for off in range(0, len(block), REPORT):
            self.write_payload(block[off:off + REPORT])

    def read_ack(self, timeout_ms: int = 5000) -> bytes:
        assert self.h is not None
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            data = bytes(self.h.read(REPORT, timeout_ms=remaining))
            if not data:
                continue
            if data[0] in (ACK, NAK):
                return data
            # Anything else is noise from a previous phase; keep waiting.
        return b""

    def expect(self, want: int, what: str, timeout_ms: int = 5000):
        rep = self.read_ack(timeout_ms)
        if not rep:
            raise RuntimeError(f"{what}: timed out waiting for ACK")
        if rep[0] != ACK:
            raise RuntimeError(f"{what}: got NAK ({rep[:4].hex()})")
        if rep[1] != want:
            raise RuntimeError(f"{what}: ACK payload {rep[1]:#04x}, expected {want:#04x}")

    @staticmethod
    def build_block(block_type: int, seq: int, payload: bytes, data_len: int) -> bytes:
        body = payload.ljust(data_len, b"\x00")[:data_len]
        block = bytes([block_type, seq & 0xFF, (~seq) & 0xFF]) + body
        block += crc16_xmodem(body).to_bytes(2, "big")
        # Pad to a whole number of 64-byte reports, as the C reference does.
        pad = (-len(block)) % REPORT
        return block + b"\x00" * pad

    def enter_ymodem(self, attempts: int = 3):
        for attempt in range(1, attempts + 1):
            try:
                self.log(f"  [1/4] entering YMODEM (attempt {attempt}/{attempts})")
                self.write_payload(b"\x01reset\x00")
                time.sleep(0.2)
                self.write_payload(b"ymodem\x00")
                self.expect(0x43, "ymodem entry", timeout_ms=5000)
                self.log("        ymodem mode acknowledged")
                return
            except Exception as exc:
                self.log(f"        attempt failed: {exc}")
                if attempt == attempts:
                    raise
                # Some units re-enumerate on reset; reopen and retry.
                self.reopen(delay=1.0 + attempt)

    def send_metadata(self, name: str, size: int):
        payload = name.encode() + b"\x00"
        if size > 0:
            payload += str(size).encode() + b"\x00"
        self.write_block(self.build_block(SOH, 0, payload, 128))
        self.expect(0x43 if size > 0 else 0x00, f"metadata({name!r})")

    def send_data(self, data: bytes):
        total = (len(data) + 1023) // 1024
        for i in range(total):
            chunk = data[i * 1024:(i + 1) * 1024]
            last = (i == total - 1)
            if len(chunk) < 1024:
                chunk = chunk.ljust(1024, b"\x1A")  # YMODEM pads with SUB
            self.write_block(self.build_block(STX, i + 1, chunk, 1024))
            self.expect(0x00 if last else 0x43, f"data block {i + 1}/{total}")
            if i % 25 == 0 or last:
                self.log(f"        block {i + 1:4d}/{total}  ({100.0 * (i + 1) / total:5.1f}%)")

    def upload(self, path: Path):
        data = path.read_bytes()
        if not THEME_MIN <= len(data) <= THEME_MAX:
            raise SystemExit(f"theme size {len(data)} outside [{THEME_MIN}, {THEME_MAX}]")
        self.log(f"uploading {path.name}: {len(data)} bytes, "
                 f"{(len(data) + 1023) // 1024} blocks")
        self.open()
        self.enter_ymodem()
        self.log("  [2/4] sending metadata")
        self.send_metadata("img.dat", len(data))
        self.log("  [3/4] streaming data")
        self.send_data(data)
        self.write_payload(bytes([EOT]))
        self.expect(0x43, "EOT")
        self.log("  [4/4] closing session")
        self.send_metadata("", 0)
        self.log("upload complete")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("theme", nargs="?", default="img.dat")
    args = ap.parse_args()
    up = Uploader()
    try:
        up.upload(Path(args.theme))
    except Exception as exc:
        print(f"\nUPLOAD FAILED: {exc}", file=sys.stderr)
        print("The panel keeps its previous theme unless the data phase had "
              "already started; re-running this script is the fix.", file=sys.stderr)
        raise SystemExit(1)
    finally:
        up.close()


if __name__ == "__main__":
    main()
