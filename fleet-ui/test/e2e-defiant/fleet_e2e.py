#!/usr/bin/env python3
"""Starts the real fleet.py for the end to end test. Fleet is imported, not copied: the only differences from a
normal start are the intervals (seconds instead of minutes, from E2E_UPD_* and E2E_DEF_TTL, and the events worker's
waits and reminder interval and the reboot's waits from E2E_EV_* and E2E_REBOOT_*, only when a story sets them), a pause between the
worker's decision to scan and the scan (E2E_SCAN_LAG, only when a story sets it: it makes the moment in which a reboot request can
arrive long enough to hit), three probe workers
that would look at this machine's containers and GPU, and the two ways out of the Messages system: a push is
recorded in a file of the sandbox instead of being sent (the senders are replaced here, in test code), and the
device file, the events folder and the Speech tool all live in or point into the sandbox. Everything else Fleet
reaches for (ssh, Telegram, the gate, its files) is pointed at stand-ins by the environment; this script refuses
to start when any of them is not."""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import fleet  # noqa: E402

sandbox, port = os.environ["E2E_SANDBOX"], int(os.environ["E2E_PORT"])
gate = int(fleet.SWAP.rsplit(":", 1)[-1])
inside = lambda p: os.path.realpath(p).startswith(os.path.realpath(sandbox) + os.sep)
assert fleet.DEFIANT_HOST == "fake@defiant.example", "Fleet must talk to the made-up host"
assert fleet.SWAP.startswith("http://127.0.0.1:") and gate > 20000 and gate not in (28080, 28081), "Fleet must talk to the gate stand-in"
assert fleet.TELEGRAM_SEND == os.path.join(HERE, "telegram.py"), "Fleet must send through the Telegram stand-in"
assert os.path.expanduser("~").startswith(sandbox) and fleet.UPD_FILE.startswith(sandbox), "Fleet must run and save inside the sandbox"
assert port > 20000 and port not in (8090, 8444, 28080, 28081, 18081), "Fleet must not take a port of the real services"

# Messages and push: the events in a folder of the sandbox, the devices in a file of the sandbox, no other push host, a
# push key only when it is a file of the sandbox. Even then nothing reaches Apple: both senders are replaced below.
assert inside(fleet.EV_DIR) and inside(fleet.EV_FILE) and inside(fleet.EV_STATE), "Fleet must write its events inside the sandbox"
assert inside(os.environ["E2E_PUSH_DEVICES"]) and inside(os.environ["E2E_PUSH_LOG"]), "the push files must be inside the sandbox"
assert not os.environ.get("FLEET_APNS_HOST"), "Fleet must not be pointed at another push host"
assert not os.environ.get("APNS_KEY_FILE") or inside(os.environ["APNS_KEY_FILE"]), "a push key must be a file of the sandbox"
fleet.PUSH_DEV_FILE = os.environ["E2E_PUSH_DEVICES"]


def record(via, names, result):
    """A sender that appends what it was asked to send as one JSON line, sends nothing and says it went through."""
    def send(*args, **kw):
        row = dict(zip(names, args), **kw)
        if via == "apns_post":                       # what the phone would show; the device token never goes in
            row = {"env": (row.get("dev") or {}).get("env"), "payload": row.get("payload"), "collapse": row.get("collapse", "")}
        with open(os.environ["E2E_PUSH_LOG"], "a") as f:
            f.write(json.dumps({"at": time.time(), "via": via, **row}) + "\n")
        return result
    return send


fleet.push_send = record("push_send", ("title", "body", "go", "badge", "collapse"), 1)
fleet.apns_post = record("apns_post", ("dev", "payload", "collapse"), (200, ""))
fleet.speech.CTL = os.path.join(sandbox, "no-such-folder", "speech-ctl")           # the Speech probe finds no tool to run
assert not os.path.exists(fleet.speech.CTL), "the Speech tool must not exist"

for name in ("UPD_FIRST", "UPD_GAP", "UPD_RETRY", "UPD_EVERY", "UPD_LOST", "DEF_TTL"):
    setattr(fleet, name, float(os.environ["E2E_" + name]))
for name in ("EV_GRACE", "EV_HEAVY", "EV_REMIND_S", "REBOOT_POLL_S", "REBOOT_START_WAIT", "REBOOT_GIVEUP", "REBOOT_GAP"):
    if "E2E_" + name in os.environ:             # the events worker's waits and the reboot's: Fleet's own unless the story shortens them
        setattr(fleet, name, float(os.environ["E2E_" + name]))
if "E2E_SCAN_LAG" in os.environ:                # the worker has decided to scan: this long passes, and a marker file says so, before the scan starts
    real_scan_once = fleet._upd_scan_once

    def lagging_scan_once(now):
        open(os.path.join(sandbox, "scan-decided"), "w").close()
        time.sleep(float(os.environ["E2E_SCAN_LAG"]))
        return real_scan_once(now)
    fleet._upd_scan_once = lagging_scan_once
fleet.poll_nodes = fleet.ct_worker = fleet.ctx_max_worker = lambda: None
sys.argv = ["fleet.py", "--host", "127.0.0.1", "--port", str(port)]
fleet.main()
