"""Push live Starfleet cluster stats to the 3.5" USB HID panel.

Polls both DGX Sparks over SSH (the same `~/bin/opencode-status.py` the
Starfleet Command menu bar app uses) and writes the numbers into the panel's
sensor channels. The panel's loaded theme decides where each channel is drawn
and what it is labelled -- the channel ids below are just numeric slots, so
their firmware names (cpu_temperature etc.) are irrelevant here.

Channel map -- must stay in sync with build_theme.py:
    1 Jean-Luc GPU %      5 Kathryn GPU %
    2 Jean-Luc GPU temp   6 Kathryn GPU temp
    3 Jean-Luc RAM %      7 Kathryn RAM %
    4 Jean-Luc GPU watts  8 Kathryn GPU watts
    9 total actively-generating agent sessions, both nodes
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from smartpanel import Panel

log = logging.getLogger("starfleet-panel")

STATUS_CMD = "~/bin/opencode-status.py"
# ssh aliases from ~/.ssh/config; each pins the Tailscale MagicDNS FQDN and the
# NVIDIA Sync key, which is what makes BatchMode auth work from a GUI context.
NODES = [("jean-luc", 1, 2, 3, 4), ("kathryn", 5, 6, 7, 8)]
SESSIONS_CHANNEL = 9


def poll(alias: str, timeout: float = 10.0) -> dict | None:
    """Return the node's status JSON, or None if it can't be reached."""
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, STATUS_CMD],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("%s: ssh timed out after %.0fs", alias, timeout)
        return None
    if out.returncode != 0:
        log.warning("%s: ssh exit %d: %s", alias, out.returncode,
                    out.stderr.decode(errors="replace").strip()[:200])
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        log.warning("%s: bad JSON: %s", alias, exc)
        return None


def _clamp(value: float | None) -> int:
    """Panel channels are 16-bit; None/absent readings become 0."""
    if value is None:
        return 0
    return max(0, min(0xFFFF, int(round(float(value)))))


def collect() -> dict[int, int]:
    """Poll both nodes concurrently and flatten into {channel: value}."""
    with ThreadPoolExecutor(max_workers=len(NODES)) as pool:
        results = list(pool.map(lambda n: poll(n[0]), NODES))

    values: dict[int, int] = {}
    generating = 0
    for (alias, ch_gpu, ch_temp, ch_ram, ch_pwr), data in zip(NODES, results):
        host = (data or {}).get("host", {})
        values[ch_gpu] = _clamp(host.get("gpu_util_pct"))
        values[ch_temp] = _clamp(host.get("gpu_temp_c"))
        values[ch_ram] = _clamp(host.get("ram_pct"))
        values[ch_pwr] = _clamp(host.get("gpu_power_w"))
        # A node that's down contributes nothing rather than breaking the total.
        generating += sum(1 for s in (data or {}).get("sessions", []) if s.get("generating"))
        log.info("%-9s gpu=%3s%% temp=%3sC ram=%3s%% pwr=%3sW",
                 alias, values[ch_gpu], values[ch_temp], values[ch_ram], values[ch_pwr])
    values[SESSIONS_CHANNEL] = _clamp(generating)
    return values


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between pushes")
    ap.add_argument("--once", action="store_true", help="push a single update and exit")
    ap.add_argument("--brightness", type=int, default=100, help="backlight 1..100")
    ap.add_argument("--dry-run", action="store_true",
                    help="poll and print, but never open or write the panel")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    panel: Panel | None = None
    try:
        while True:
            values = collect()
            log.info("channels: %s", {k: values[k] for k in sorted(values)})

            if not args.dry_run:
                try:
                    if panel is None:
                        panel = Panel().open()
                        log.info("panel opened")
                    panel.send_sensors(values)
                    # Also refreshes the clock and pins the backlight on.
                    panel.send_datetime(brightness=args.brightness, backlight_timeout=0)
                except Exception as exc:
                    # Unplugged or asleep: drop the handle and re-open next cycle
                    # rather than dying, so the daemon survives a cable wobble.
                    log.warning("panel write failed (%s); will reopen", exc)
                    if panel is not None:
                        panel.close()
                    panel = None

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        if panel is not None:
            panel.close()


if __name__ == "__main__":
    main()
