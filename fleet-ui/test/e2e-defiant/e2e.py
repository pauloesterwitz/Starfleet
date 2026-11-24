#!/usr/bin/env python3
"""End to end test of the Defiant updates: the real fleet.py (started by fleet_e2e.py with short intervals) against a
made-up Defiant, then the real desktop and phone pages in WebKit against that same server.

Real: fleet.py, the scripts it sends over ssh (scan, poll, install unit, reboot, probe), the HTTP API, both pages.
Made up: the host. fake-bin/ssh runs those scripts in a sandbox with stand-ins for sudo, apt-get, apt, systemctl,
systemd-run, fuser and docker (the apt-sim.txt, job.conf, reboot.conf and flag files in the sandbox's host folder are what a step
changes; a reboot is a host that stops answering for a while and comes back with a new boot id); telegram.py records the
messages; gate.py answers the key check. Fleet runs with a sandbox HOME and a
PATH that holds only those stand-ins and harmless tools, so it cannot reach a real host, service or model. The
Messages system is walled in too: its events folder and device file are in the sandbox, curl is a stand-in, and
fleet_e2e.py replaces the push senders with recorders (push.jsonl), so no push can leave and no real file is written.

Usage: e2e.py [name-substring]    (use run.sh; PW=<playwright package> is needed for the page checks)"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX, E2E_FIX = HERE.parent / "fixtures", HERE / "fixtures"
SIMS = {"many": FIX / "apt-sim-large.txt", "quiet": FIX / "apt-sim-phased.txt", "three": E2E_FIX / "sim-three-security.txt",
        "one": E2E_FIX / "sim-new-security.txt", "removal": E2E_FIX / "sim-removal.txt"}
KEY, FAKE = "fixture-key", HERE / "fake-bin"
POLICY = re.search(r'^POLICY = "(.*)"$', (HERE.parent.parent / "fleet.py").read_text(encoding="utf-8"), re.M).group(1)      # Fleet's own wording, whatever it is today
SCAN, INSTALL, REBOOT = "/api/defiant/updates/scan", "/api/defiant/updates/install", "/api/defiant/reboot"
HINT = "Fleet: apt would remove packages, nothing was changed. Rescanning now: check the list, then install again."
E_SAME_BOOT = "Defiant is still on the same boot three minutes after the request. It may still reboot. If it does not, check it in your hosting provider's console."
E_GIVEUP = "Defiant did not answer again within 15 minutes. Check it in your hosting provider's console."
E_APT = "apt is busy on Defiant, an upgrade may be running. Try again in a few minutes."
E_AUTO_APT = f"The reboot after the install did not start: {E_APT.rstrip('.')}. Reboot Defiant when apt is idle."
E_INSTALL = "An install is running. Wait until it is done."
E_UNSURE = "No answer from Defiant. It may still reboot."
E_GAP = "A reboot was requested a moment ago. Try again in a few minutes."
# What a reboot leaves in the Messages list: one list-only entry (title, text) when it starts, ends or fails. The seconds of a finished reboot read N (see trail()).
# An error longer than 140 characters is cut at a word and ends in "...": the two long ones are spelled out here, so a change of the cut shows.
R_START, R_AUTO = ("Defiant reboot started", "Started with the Reboot Defiant button."), ("Defiant reboot started", "Started by Fleet after the install.")
R_DONE = ("Defiant rebooted", "Defiant answered again with a new boot after N seconds.")
R_SAME_BOOT = ("Defiant reboot failed", "Defiant is still on the same boot three minutes after the request. It may still reboot. If it does not, check it in your hosting...")
R_GIVEUP = ("Defiant reboot failed", E_GIVEUP)
R_AUTO_APT = ("Defiant reboot failed", "The reboot after the install did not start: apt is busy on Defiant, an upgrade may be running. Try again in a few minutes. Reboot...")
SAFE_TOOLS = ("awk basename bash cat cp cut date dirname env grep head ip nproc ps rm sed setsid sh sleep stat tail "
              "timeout touch tr uname").split()
STAND_INS = "ssh sudo apt-get apt systemctl systemd-run fuser docker hostname df curl".split()      # everything that could change a machine or reach out
NO_WAY_OUT = "reboot shutdown poweroff halt kexec".split()      # no way to reboot or power off a machine is on the PATH at all
BOOT0 = "00000000-0000-4000-8000-000000000000"      # the made-up host's first boot id (the stand-in systemctl makes the next ones)

SBX = Path(tempfile.mkdtemp(prefix="defiant-e2e-"))
HOST, HOME, STATE, SAFE = SBX / "host", SBX / "home", SBX / "state" / "defiant-updates.json", SBX / "safe-bin"
TG_LOG, TG_FAIL, GATE_LOG, FLEET_LOG = SBX / "telegram.jsonl", SBX / "telegram-fail", SBX / "gate.log", SBX / "fleet.log"
EV_DIR, PUSH_LOG, PUSH_DEV, APNS_KEY = SBX / "events", SBX / "push.jsonl", SBX / "push-devices.json", SBX / "fixture-apns-key.txt"
PATH = f"{FAKE}:{SAFE}"
DEFAULTS = dict(FIRST=2, GAP=2, RETRY=3600, EVERY=3600, LOST=20, DEF_TTL=1)      # seconds; a story overrides what it needs
UPD, procs, results = dict(DEFAULTS), {}, []
EV = {}      # the events worker's waits (EV_GRACE, EV_HEAVY, EV_REMIND_S, in seconds) when a story shortens them; empty: Fleet's own
RB = {}      # the same for the reboot's waits (REBOOT_POLL_S, REBOOT_START_WAIT, REBOOT_GIVEUP, REBOOT_GAP), and SCAN_LAG: the pause between the worker's decision to scan and the scan


class Abort(Exception):
    """The story cannot go on: a step it depends on failed."""


# ------------------------------------------------------------------ reporting and waiting

def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  [{str(detail)[:500]}]"), flush=True)
    return bool(ok)


def until(fn, timeout=30, every=0.25):
    """Calls fn until it returns something true and returns that; None when the time is up."""
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() > end:
            return v or None
        time.sleep(every)


def must(value, what):
    if not value:
        raise Abort(what)
    return value


# ------------------------------------------------------------------ the sandbox and its processes

def free_port():
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port > 20000 and port not in (8090, 8444, 28080, 28081, 18081):
            return port


def build_sandbox():
    SAFE.mkdir(parents=True)
    for name in SAFE_TOOLS:        # the only real programs the made-up host and Fleet can run, found without the shell's help
        real = next((p for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin") if os.access(p := f"{d}/{name}", os.X_OK)), None)
        if real:
            (SAFE / name).symlink_to(real)
    (HOME / ".config" / "fleet-ui").mkdir(parents=True)
    (HOME / ".config" / "fleet-ui" / "computer-mcp.json").write_text("{}")        # Computer starts no tool server
    APNS_KEY.write_text("not a key: the push senders are replaced, nothing ever reads this\n")
    (SBX / "tmp").mkdir()
    host_reset()
    loose = [n for n in STAND_INS if shutil.which(n, path=PATH) != str(FAKE / n)]
    check("sandbox: every command that could change a machine resolves to a stand-in", not loose, loose)
    found = [n for n in NO_WAY_OUT if shutil.which(n, path=PATH)]
    check("sandbox: no program that reboots or powers off a machine is on the PATH of Fleet or of the made-up host", not found, found)
    env = {"PATH": PATH, "E2E_HOST": str(HOST)}
    run = lambda *argv: subprocess.run([str(FAKE / argv[0]), *argv[1:]], env=env, capture_output=True, text=True)
    r = run("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "backup.example", "echo reached")
    check("sandbox: the ssh stand-in refuses every host but the made-up one", r.returncode == 255 and "reached" not in r.stdout, (r.returncode, r.stdout))
    r = run("ssh", "-o", "BatchMode=yes", "fake@defiant.example", "echo reached")
    check("sandbox: ... and every script on the made-up host that Fleet is not known to send", r.returncode == 255 and "reached" not in r.stdout, (r.returncode, r.stdout))
    r = run("ssh", "-o", "BatchMode=yes", "fake@defiant.example", "echo FLEET-BOOT; /bin/systemctl reboot --dry-run")      # a dry run, in case the wall were missing
    check("sandbox: ... and a script of a known kind that names a way to reboot, apart from the one pinned line", r.returncode == 255 and "refused" in r.stderr and "FLEET-BOOT" not in r.stdout,
          (r.returncode, r.stdout, r.stderr))
    refused = [run("sudo", "-n", "/bin/true"), run("sudo", "-n", "true"), run("systemd-run", "--unit=x", "--collect", "/bin/sh", "-c", "systemctl reboot"),
               run("systemd-run", "--unit=fleet-reboot", "--on-active=3s", "/bin/true")]
    check("sandbox: sudo runs no path, systemd-run no command that reboots and no timer that is not the reboot's",
          all(x.returncode == 1 for x in refused) and not (HOST / "unit.pid").exists() and not (HOST / "reboot-timer.pid").exists(), [(x.returncode, x.stderr) for x in refused])


def kill_unit():
    """Stops a unit the fake systemd-run left running (the install, or a reboot timer that has not fired), if that pid still is
    the unit's script (never a recycled pid)."""
    for name, marker in (("unit.pid", b"FLEET-APT"), ("reboot-timer.pid", b"FLEET-REBOOT")):
        try:
            pid = int((HOST / name).read_text())
            if marker in Path(f"/proc/{pid}/cmdline").read_bytes():
                os.killpg(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass


def host_reset():
    kill_unit()
    note_reach()
    shutil.rmtree(HOST, ignore_errors=True)
    shutil.rmtree(EV_DIR, ignore_errors=True)
    for d in ("var/run", "var/log"):
        (HOST / d).mkdir(parents=True)
    (HOST / "boot-id").write_text(BOOT0 + "\n")
    (HOST / "boot-at").write_text(f"{int(time.time()) - 3 * 86400}\n")      # up for three days
    sim("quiet")
    job()
    host_reboot()
    for f in (TG_LOG, TG_FAIL, STATE, PUSH_LOG, PUSH_DEV):
        f.unlink(missing_ok=True)


def sim(name):
    shutil.copy(SIMS[name], HOST / "apt-sim.txt")


def sim_without(name, *drop):
    """The sim with the named packages gone: what is left after some of them got installed on the host."""
    out = [l for l in SIMS[name].read_text().splitlines() if not re.match(r"(Inst|Conf) (%s) " % "|".join(drop), l)]
    n = sum(1 for l in out if l.startswith("Inst "))
    (HOST / "apt-sim.txt").write_text(re.sub(r"^\d+ upgraded, 0 newly", f"{n} upgraded, 0 newly", "\n".join(out) + "\n", flags=re.M))


def job(rc=0, delay=3, per=2, reboot=0, after=None, lock=0):
    """What the next install does: how it ends, how long it takes, lines a second, a reboot left pending, a new sim afterwards,
    the nightly run taking the apt lock the moment it ends."""
    (HOST / "job.conf").write_text(f"RC={rc}\nDELAY={delay}\nPER={per}\nREBOOT={reboot}\nLOCK={lock}\n")
    (HOST / "apt-sim-after.txt").unlink(missing_ok=True)
    if after:
        shutil.copy(SIMS[after], HOST / "apt-sim-after.txt")


def host_reboot(down=6, never=False):
    """What the made-up host does when its reboot timer fires: no answer for `down` seconds, then a new boot id; or nothing at all (never)."""
    (HOST / "reboot.conf").write_text(f"DOWN={down}\nNEVER={int(never)}\n")


def pending_reboot(on=True):
    """The host wants a reboot: the flag and the packages that want it, as after an upgrade by the nightly run."""
    for name, text in (("reboot-required", ""), ("reboot-required.pkgs", "linux-image-6.8.0-99-generic\nlibc6\n")):
        (HOST / "var/run" / name).write_text(text) if on else (HOST / "var/run" / name).unlink(missing_ok=True)


def flag(path, on):
    p = path if isinstance(path, Path) else HOST / path
    p.touch() if on else p.unlink(missing_ok=True)


def up(port):
    try:
        socket.create_connection(("127.0.0.1", port), 1).close()
        return True
    except OSError:
        return False


def start_gate():
    procs["gate"] = subprocess.Popen([sys.executable, str(HERE / "gate.py"), str(GATE_PORT), str(GATE_LOG)], env={"PATH": PATH})
    must(until(lambda: up(GATE_PORT), 10), "the gate stand-in did not start")


def stop_gate():
    procs.pop("gate").terminate()


def start_fleet(push=False):
    """push=True gives Fleet a (made-up) push key, so its Messages system believes it can push: the senders it would
    use are the recorders of fleet_e2e.py, so the push lands in PUSH_LOG. Without it the sender is off, as on a Fleet
    that has no key yet. The environment is built here from nothing: no APNS_* of the shell ever reaches Fleet."""
    env = {"PATH": PATH, "HOME": str(HOME), "TMPDIR": str(SBX / "tmp"), "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8",
           "E2E_SANDBOX": str(SBX), "E2E_HOST": str(HOST), "E2E_PORT": str(PORT), "E2E_TELEGRAM_LOG": str(TG_LOG),
           "E2E_TELEGRAM_FAIL": str(TG_FAIL), "FLEET_DEFIANT_HOST": "fake@defiant.example", "FLEET_DEFIANT_UPDATES_FILE": str(STATE),
           "FLEET_TELEGRAM_SEND": str(HERE / "telegram.py"), "FLEET_SWAP": f"http://127.0.0.1:{GATE_PORT}",
           "FLEET_EVENTS_DIR": str(EV_DIR), "E2E_PUSH_LOG": str(PUSH_LOG), "E2E_PUSH_DEVICES": str(PUSH_DEV),
           **({"APNS_KEY_FILE": str(APNS_KEY), "APNS_KEY_ID": "FIXTUREKEY1", "APNS_TEAM_ID": "FIXTURETEAM"} if push else {}),
           **{"E2E_" + ("" if k == "DEF_TTL" else "UPD_") + k: str(v) for k, v in UPD.items()},
           **{"E2E_" + k: str(v) for k, v in {**EV, **RB}.items()}}
    procs["fleet"] = subprocess.Popen([sys.executable, str(HERE / "fleet_e2e.py")], env=env, cwd=SBX,
                                      stdout=open(FLEET_LOG, "a"), stderr=subprocess.STDOUT)
    must(until(lambda: up(PORT), 15), "Fleet did not come up: " + FLEET_LOG.read_text()[-600:])


def stop_fleet():
    p = procs.pop("fleet", None)
    if p:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


def reset(**upd):
    """A fresh Defiant and a fresh saved state, nothing running but the gate; Fleet is started by the story, with these intervals."""
    stop_fleet()
    host_reset()
    UPD.clear()
    UPD.update(DEFAULTS, **upd)
    EV.clear()
    RB.clear()
    if "gate" not in procs:
        start_gate()


def teardown():
    stop_fleet()
    if "gate" in procs:
        stop_gate()
    kill_unit()
    if os.environ.get("KEEP"):
        print(f"sandbox kept: {SBX}")
    else:
        shutil.rmtree(SBX, ignore_errors=True)


# ------------------------------------------------------------------ talking to Fleet and reading the host

def call(method, path, key=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=json.dumps(body or {}).encode() if method == "POST" else None,
                                 headers={"X-Starfleet-Key": key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e.headers


def view():
    return call("GET", "/api/defiant/updates")[1]


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


def telegram():
    return [json.loads(l)["text"] for l in lines(TG_LOG)]


def failures():
    return [t for t in telegram() if t.startswith("Defiant: update install failed")]


def reboot_failures():
    return [t for t in telegram() if t.startswith("Defiant: reboot failed")]


def ssh_times(kind, what="BEGIN"):
    return [float(l.split()[0]) for l in lines(HOST / "ssh.log") if l.split()[1:3] == [what, kind]]


def host_calls(program):
    return [l for l in lines(HOST / "calls.log") if l.split()[1] == program]


def upgrades():
    """The real upgrades apt-get was asked for (not the scan's simulation), as the line the stand-in logged."""
    return [l for l in host_calls("apt-get") if " full-upgrade" in l and " -s " not in l]


def saved():
    return json.loads(STATE.read_text())


def pushes():
    """What Fleet's push sender was asked to send (the recorders of fleet_e2e.py), one dict per push."""
    return [json.loads(l) for l in lines(PUSH_LOG)]


def push_device():
    """Registers a made-up device through the real endpoint (the push key of start_fleet(push=True) is made up too), so that a push which Fleet
    sent would be recorded in PUSH_LOG: the sender reads as ready and one device is known."""
    st, r, _ = call("POST", "/api/computer/push/register", KEY, {"token": "ef" * 32, "env": "development"})
    must(st == 200 and r["devices"] == 1, f"the made-up device could not register: {st} {r}")
    st, r, _ = call("GET", "/api/computer/push", KEY)
    must(st == 200 and r["sender"] == "ready" and r["devices"] == 1, f"the push sender is not ready: {st} {r}")


def entries(kind):
    """The entries of one kind in GET /api/events (what the phone's Messages list reads), and the open episodes."""
    ev = call("GET", "/api/events")[1]
    return [e for e in ev["events"] if e["kind"] == kind], ev["open"]


def episode(key):
    """The rule the events worker holds for an open episode (from its saved state); None when none is open."""
    try:
        return json.loads((EV_DIR / "events-state.json").read_text())["eps"][key]["spec"]
    except (OSError, ValueError, KeyError):
        return None


REACH = {"curl": 0, "refused": set()}


def note_reach():
    """Keeps what the stand-ins saw of the story that ends, before its host folder is wiped: how often curl was called
    (never) and which other hosts ssh was asked for (Fleet's other workers ask for the nodes and the NAS: refused)."""
    REACH["curl"] += len(host_calls("curl"))
    REACH["refused"] |= {l.split()[2] for l in lines(HOST / "ssh.log") if l.split()[1:2] == ["refused"] and len(l.split()) > 2}


def stamp(s):
    return (s["at"], s["error_at"])


def scan_now():
    """Asks for a scan until Fleet takes the request (it refuses within UPD_GAP of the last one), then waits for the scan to end."""
    s = view()["scan"]
    before = stamp(s) if s else None
    must(until(lambda: call("POST", SCAN)[1].get("started"), 30, 0.4), "Fleet never took a scan request")
    return must(until(lambda: (u := view()) and u["scan"] and not u["scan"]["busy"] and stamp(u["scan"]) != before and u, 30), "the scan did not end")


def install(key=KEY, body=None):
    """The install click as both pages send it: the body {"reboot_ok": true} announces the reboot, and only a click that sent it can be followed by
    one. body= sends another (an older page and a script send {})."""
    return call("POST", INSTALL, key, {"reboot_ok": True} if body is None else body)


def reboot(key=KEY):
    return call("POST", REBOOT, key)


def rb():
    """The reboot as the page sees it (no boot id in it)."""
    return view()["reboot"]


def reboot_when_idle():
    """Presses Reboot until Fleet takes it: a scan that is busy at that moment (some stories scan every few seconds) answers 409."""
    return must(until(lambda: (a := reboot()) and a[0] == 200 and a, 20, 0.2), "Fleet never took the reboot")


def reboot_over(timeout=60):
    """Waits until the reboot is no longer running and returns it."""
    return must(until(lambda: (r := rb())["state"] != "running" and r, timeout, 0.5), "the reboot did not end")


def boot_id():
    return (HOST / "boot-id").read_text().strip()


def scheduled():
    """The reboots Fleet scheduled on the host: its systemd-run calls for the reboot timer, as the stand-in logged them."""
    return [l for l in host_calls("systemd-run") if "--unit=fleet-reboot " in l]


def reboots():
    """What the host did with its timers: one word for each call of `systemctl reboot`, "rebooted" or "ignored" (a host that never reboots)."""
    return [l.split()[1] for l in lines(HOST / "reboots.log")]


def reboot_text(error):
    """The Telegram message of a failed reboot: the headline, a blank line and the error, nothing after it (the errors carry their own advice)."""
    return f"Defiant: reboot failed\n\n{error}"


def trail_raw():
    """What reboots left in the Messages list, as (title, text) in the order they were written, read from the sandbox's events file (a reboot writes
    one list-only entry when it starts, one when it ends and one when it fails; entries of other kinds are not in it)."""
    rows = []
    for l in lines(EV_DIR / "events.jsonl"):
        try:
            rows.append(json.loads(l))
        except ValueError:                    # a line that Fleet is still writing
            pass
    return [(e["title"], e["text"]) for e in rows if e["kind"] == "audit" and e["title"].startswith(("Defiant reboot", "Defiant rebooted"))]


def trail():
    """trail_raw() with the seconds of a finished reboot read as N."""
    return [(t, re.sub(r"after \d+ seconds", "after N seconds", x)) for t, x in trail_raw()]


def trail_is(*expected, wait=10):
    """True when the trail becomes exactly `expected` (an entry is written a moment after the state changes, after the Telegram message of a failure)
    and still is a second later: no entry twice, none more."""
    ok = until(lambda: trail() == list(expected), wait)
    time.sleep(1)
    return bool(ok) and trail() == list(expected)


def key_checks():
    """The key checks the gate stand-in answered (for the whole run: take a count before and after)."""
    return [l for l in lines(GATE_LOG) if l.split()[1] == "/starfleet/check"]


def ssh_kinds(t0, t1):
    """The kinds of script Fleet sent to the host between two times, answered or not (ssh.log: BEGIN KIND, or down KIND)."""
    return {l.split()[2] for l in lines(HOST / "ssh.log") if l.split()[1] in ("BEGIN", "down") and t0 <= float(l.split()[0]) <= t1}


def tracebacks(log):
    """The tracebacks in Fleet's log, except a browser that went away mid-answer (a page reload cuts requests short)."""
    found = [b for b in log.split("Traceback (most recent call last):")[1:] if b.strip()]      # an empty block is a print cut short by a second thread's, or one still being written
    last = lambda b: re.search(r"^[A-Za-z_.]*(?:Error|Exception)\b.*$", b, re.M)
    return [b.strip()[-400:] for b in found if not (last(b) and last(b).group(0).startswith(("BrokenPipeError", "ConnectionResetError")))]


# ------------------------------------------------------------------ the stories

def story_sandbox():
    """The positive control for the walls around the Messages system: with a made-up push key, a device and a test push,
    everything Fleet writes lands in the sandbox, the push is recorded, and nothing is sent anywhere."""
    reset(FIRST=3600)
    start_fleet(push=True)
    st, r, _ = call("GET", "/api/computer/push", KEY)
    check("with the made-up key the push sender reads as ready, and no device has registered", st == 200 and r["sender"] == "ready" and r["devices"] == 0, (st, r))
    token = "ab" * 32
    st, r, _ = call("POST", "/api/computer/push/register", KEY, {"token": token, "env": "development"})
    check("a device registers in the sandbox's device file, readable by its owner only", st == 200 and r["devices"] == 1
          and json.loads(PUSH_DEV.read_text())[0]["token"] == token and oct(PUSH_DEV.stat().st_mode & 0o777) == "0o600", (st, r))
    st, r, _ = call("POST", "/api/computer/push/test", KEY)
    check("a test push goes to the stand-in and reads as sent", st == 200 and r == {"ok": True, "sent": 1}, (st, r))
    rec = [json.loads(l) for l in lines(PUSH_LOG)]
    check("the record holds the test message and never the device token", len(rec) == 1 and rec[0]["via"] == "push_send"
          and rec[0]["title"] == "Starfleet test" and token not in PUSH_LOG.read_text(), rec)
    ev = [json.loads(l) for l in lines(EV_DIR / "events.jsonl")]
    check("the test entry went into the sandbox's events folder", [e["kind"] for e in ev] == ["test"], ev)
    check("curl never ran", host_calls("curl") == [], host_calls("curl"))
    st, r, _ = call("POST", "/api/computer/push/unregister", KEY, {"token": token})
    check("the device unregisters again", st == 200 and r["devices"] == 0 and json.loads(PUSH_DEV.read_text()) == [], (st, r))


def story_security_push():
    """The push for security updates, end to end. Fleet's own events worker (its waits and its reminder interval shortened
    in test code, so that a reminder would show) reads the summary of the real scan, opens one act entry and pushes it once
    through the recorder: the count only, no package name (those stay on Telegram). Later scans with the same or another
    count are the same episode, a Fleet restart, a Fleet that has lost its saved scan and a failed scan repeat and close nothing,
    and the install with its rescan closes it without another push."""
    reset(FIRST=3600)
    EV.update(EV_GRACE=1, EV_HEAVY=2, EV_REMIND_S=6)
    sim("three")                                         # curl, libssl3t64 and openssl are security updates, libcurl4t64 and tzdata are not
    job(rc=0, delay=3, per=2, after="quiet")             # what the install does: nothing is left to install afterwards
    start_fleet(push=True)
    token, names = "cd" * 32, ("curl", "libssl3t64", "openssl", "libcurl4t64", "tzdata")
    st, r, _ = call("POST", "/api/computer/push/register", KEY, {"token": token, "env": "development"})
    must(st == 200 and r["devices"] == 1, f"the made-up device could not register: {st} {r}")
    st, r, _ = call("GET", "/api/computer/push", KEY)
    check("with the made-up key and one device, the push sender reads as ready", st == 200 and r["sender"] == "ready" and r["devices"] == 1, (st, r))

    scan_now()
    must(until(pushes, 40), "no push was recorded after a scan that found security updates")
    time.sleep(14)                                       # past two reminder intervals: a rule that repeats would have pushed again
    p = pushes()
    check("one push is recorded: Security updates waiting, the count, the way to the Defiant view", len(p) == 1 and p[0]["via"] == "push_send"
          and p[0]["title"] == "Security updates waiting" and p[0]["body"] == "3 security updates waiting on Defiant."
          and p[0]["go"] == "#/systems/defiant" and p[0]["badge"] == 1, p)
    check("the push names no package, and the record no device", not any(n in PUSH_LOG.read_text() for n in names + (token,)), PUSH_LOG.read_text())
    mine, open_ = entries("updates-security")
    check("GET /api/events lists one open act entry for it, count only, and its episode is open", len(mine) == 1 and open_ == ["updates-security"]
          and mine[0]["level"] == "act" and mine[0]["state"] == "open" and mine[0]["title"] == "Security updates waiting"
          and mine[0]["go"] == "#/systems/defiant" and mine[0]["episode"] == "updates-security" and mine[0]["reminder"] == 0
          and "3 security updates" in mine[0]["text"] and not any(n in json.dumps(mine[0]) for n in names), (mine, open_))
    check("the long text with the package names went to Telegram, once", len(telegram()) == 1 and "openssl" in telegram()[0], telegram())

    scan_now()                                           # the next hourly scan finds the same updates, and Fleet restarts
    stop_fleet()
    start_fleet(push=True)
    time.sleep(14)
    mine, open_ = entries("updates-security")
    check("a scan with the same updates and a Fleet restart push nothing and add no entry", len(pushes()) == 1 and len(mine) == 1
          and open_ == ["updates-security"] and len(telegram()) == 1, (pushes(), mine, open_))

    stop_fleet()                                         # Fleet comes back without its scan: what it knows is unknown, not clean
    st_ = saved()
    st_["scan"] = None
    STATE.write_text(json.dumps(st_))
    start_fleet(push=True)
    time.sleep(14)
    mine, open_ = entries("updates-security")
    check("a Fleet without a scan holds the episode: it stays open, nothing is closed or pushed", view()["scan"] is None and len(pushes()) == 1
          and len(mine) == 1 and open_ == ["updates-security"], (view()["scan"], pushes(), mine, open_))
    scan_now()                                           # the scan is known again, with the same updates: the same episode goes on

    sim_without("three", "curl", "libssl3t64")           # two of the three got installed by the nightly run: one security update is left
    must(scan_now()["scan"]["security"] == 1, "the scan did not find one security update")
    must(until(lambda: (episode("updates-security") or {}).get("push") == "1 security update waiting on Defiant.", 40), "the events worker never saw the new count")
    time.sleep(8)
    mine, open_ = entries("updates-security")
    check("a scan with another count is the same episode: its text follows the count, no new entry, no new push",
          len(mine) == 1 and open_ == ["updates-security"] and len(pushes()) == 1, (mine, pushes()))

    flag("apt-lock", True)
    s = scan_now()["scan"]
    must(s["error"] and s["security"] == 1, s)           # the failed scan keeps the last good numbers and carries the error
    time.sleep(14)
    mine, open_ = entries("updates-security")
    check("a failed scan (apt busy) closes nothing and pushes nothing", len(mine) == 1 and open_ == ["updates-security"] and len(pushes()) == 1, (mine, open_))
    flag("apt-lock", False)

    st, r, _ = install()
    must(st == 200, f"install refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 1), "the install did not end")
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > j["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the install")
    check("the install went through and the rescan finds no security update", j["state"] == "done" and u["scan"]["security"] == 0
          and u["scan"]["error"] is None, (j["state"], u["scan"]))
    mine, open_ = must(until(lambda: (e := entries("updates-security")) and not e[1] and e, 40), "the episode did not close after the rescan")
    check("the episode resolves: a resolved entry says so and nothing is open", [e["state"] for e in mine] == ["open", "resolved"]
          and mine[1]["level"] == "act" and mine[1]["episode"] == "updates-security" and open_ == []
          and mine[1]["text"] == "No security updates are waiting on Defiant any more.", (mine, open_))
    time.sleep(14)
    check("the resolve is not pushed: still the one push, one Telegram alert and no failure message", len(pushes()) == 1 and len(telegram()) == 1
          and failures() == [], (pushes(), telegram()))


def story_security_message():
    """On a Fleet without a push key (the state of a Fleet that has none yet) the rule still writes its entry for the
    Messages list. Nothing is pushed and nothing breaks."""
    reset(FIRST=3600)
    EV.update(EV_GRACE=1, EV_HEAVY=2)
    sim("one")                                           # one security update: python3-urllib3
    start_fleet()
    scan_now()
    mine, open_ = must(until(lambda: (e := entries("updates-security")) and e[0] and e, 40), "no entry for the security update")
    check("the entry is there for the Messages list: one open act entry, the count and no package name", len(mine) == 1 and open_ == ["updates-security"]
          and mine[0]["level"] == "act" and mine[0]["state"] == "open" and mine[0]["text"].startswith("1 security update waiting on Defiant.")
          and "urllib3" not in json.dumps(mine[0]), (mine, open_))
    time.sleep(8)
    st, r, _ = call("GET", "/api/computer/push", KEY)
    check("the push sender is off and nothing was pushed", st == 200 and r["sender"] == "off" and pushes() == [], (st, r, pushes()))


def story_scan_and_alerts():
    reset(FIRST=3, RETRY=77)                          # a lock on apt: the first scan, by itself, fails
    flag("apt-lock", True)
    sim("three")
    start_fleet()
    u = view()
    check("scan is null before the first scan has started", u["configured"] is True and u["scan"] is None
          and u["job"]["state"] == "idle" and u["critical"] is False, u)
    u = must(until(lambda: (v := view()) and v["scan"] and v["scan"]["error"] and v, 20), "the first scan never ran")
    s = u["scan"]
    check("a failed first scan shows its error in a stub scan, no packages", s["at"] is None and "lock" in s["error"]
          and s["error_at"] and s["packages"] == [] and s["busy"] is False and s["count"] == 0, s)
    check("a failed first scan is retried after UPD_RETRY, not UPD_EVERY", 75 < s["next_at"] - s["error_at"] < 79, s)
    d = call("GET", "/api/defiant")[1]
    check("the stub is not saved and /api/defiant has no upd yet", saved()["scan"] is None and d["upd"] is None, d.get("upd"))
    check("a failed first scan sends no alert", telegram() == [])

    flag("apt-lock", False)
    (HOST / "scan-delay").write_text("3")
    must(until(lambda: call("POST", SCAN)[1].get("started"), 20, 0.4), "the request for the second try was refused")
    s = view()["scan"]
    check("while the first scan runs, the stub scan says busy", s["busy"] is True and s["at"] is None and s["packages"] == [], s)
    check("while the first scan runs, /api/defiant still has no upd", call("GET", "/api/defiant")[1]["upd"] is None)
    u = must(until(lambda: (v := view()) and v["scan"]["at"] and v, 30), "the first scan did not finish")
    (HOST / "scan-delay").write_text("0")
    s = u["scan"]
    check("the first scan lists what apt would install, security first", [p["name"] for p in s["packages"]]
          == ["curl", "libssl3t64", "openssl", "libcurl4t64", "tzdata"] and s["count"] == 5 and s["security"] == 3
          and s["error"] is None and s["busy"] is False, s)
    p = s["packages"][0]
    check("a package carries version, suites and origin", (p["from"], p["to"], p["suites"], p["origin"], p["security"], p["third_party"])
          == ("8.14.1-2ubuntu1", "8.14.1-2ubuntu2", ["resolute-updates", "resolute-security"], "Ubuntu", True, False), p)
    check("the scan has the held back package, no removals, no reboot", s["deferred"] == ["distro-info"] and s["remove"] == []
          and s["reboot_required"] is False and s["reboot_pkgs"] == [], s)
    check("a good scan is repeated after UPD_EVERY", 3598 < s["next_at"] - s["at"] < 3602, s)
    check("the view is critical, the job idle and nothing internal leaks", u["critical"] is True and u["job"]["state"] == "idle"
          and "contact_at" not in u["job"], u["job"])

    must(until(telegram, 10), "no alert was sent")
    time.sleep(1.5)
    msgs = telegram()
    text = msgs[0].split("\n")
    check("a critical scan sends exactly one Telegram message", len(msgs) == 1, msgs)
    check("its first line is the headline", text[0] == "Defiant: 3 security updates pending" and len(text[0]) <= 100, text[0])
    check("then a blank line, the security packages as name from -> to (suite), the counts, how to install",
          text[1] == "" and text[2:5] == ["curl 8.14.1-2ubuntu1 -> 8.14.1-2ubuntu2 (resolute-security)",
          "libssl3t64 3.5.1-1ubuntu1 -> 3.5.1-1ubuntu2 (resolute-security)", "openssl 3.5.1-1ubuntu1 -> 3.5.1-1ubuntu2 (resolute-security)"]
          and text[5:] == ["Other updates: 2", "Install: Starfleet, Systems, Defiant, Install updates"], text)
    check("the message is plain text under 3500 characters and names no other package", len(msgs[0]) < 3500
          and chr(0x2014) not in msgs[0] and "tzdata" not in msgs[0], msgs[0])

    time.sleep(1.1)                                   # past the probe cache
    d = call("GET", "/api/defiant")[1]
    check("/api/defiant carries upd and the security problem", d["upd"] == {"count": 5, "security": 3, "deferred": 1, "remove": 0,
          "scan_at": s["at"], "job_state": "idle", "reboot_state": "idle"} and "3 security updates pending" in d["problems"] and d["status"] == "warn", d)
    check("/api/defiant keeps updates and reboot_required", d["updates"] == 6 and d["reboot_required"] is False
          and d["host"] == "defiant.example", d)

    scan_now()
    time.sleep(1.5)
    check("a second scan does not repeat the alert", len(telegram()) == 1, telegram())
    at = view()["scan"]["at"]
    stop_fleet()
    start_fleet()
    s = view()["scan"]
    check("after a Fleet restart the saved scan is served at once", s and s["at"] == at and s["count"] == 5, s)
    scan_now()
    time.sleep(1.5)
    check("a Fleet restart does not repeat the alert", len(telegram()) == 1, telegram())

    sim("quiet")
    u = scan_now()
    time.sleep(1)
    check("a scan with nothing pending is not critical", u["scan"]["count"] == 0 and u["scan"]["security"] == 0
          and u["critical"] is False and len(u["scan"]["deferred"]) == 4, u["scan"])
    check("clearing sends nothing and forgets the alert", len(telegram()) == 1 and saved()["alerted"] == {}, saved()["alerted"])
    sim("one")
    scan_now()
    m = until(lambda: len(telegram()) == 2 and telegram()[1], 10)
    check("a new security update alerts again, in the singular", m and m.split("\n")[0] == "Defiant: 1 security update pending", m)
    scan_now()
    time.sleep(1.5)
    check("the same update is not alerted twice", len(telegram()) == 2, telegram())

    flag(TG_FAIL, True)
    sim("three")
    scan_now()
    time.sleep(1.5)
    check("a failed send is not remembered", len(telegram()) == 2 and saved()["alerted"]["sig"] == "python3-urllib3=2.4.0-1ubuntu0.1",
          saved()["alerted"])
    flag(TG_FAIL, False)
    scan_now()
    check("the next scan sends it, once", until(lambda: len(telegram()) == 3, 10) and telegram()[2].startswith("Defiant: 3 security"), telegram())

    before = view()["scan"]
    flag("apt-lock", True)
    s = scan_now()["scan"]
    check("a failed scan (lock busy) keeps the old data and sets the error", s["at"] == before["at"] and s["packages"] == before["packages"]
          and s["count"] == 5 and "lock" in s["error"] and s["error_at"] > s["at"] and s["busy"] is False, s)
    check("a failed scan is retried after UPD_RETRY", 75 < s["next_at"] - s["error_at"] < 79, s)
    time.sleep(1)
    d = call("GET", "/api/defiant")[1]
    check("a failed scan sends no alert, and /api/defiant keeps the last good upd", len(telegram()) == 3 and d["upd"]["count"] == 5, d.get("upd"))
    flag("apt-lock", False)
    s = scan_now()["scan"]
    check("the next good scan clears the error", s["error"] is None and s["error_at"] is None and s["at"] > before["at"], s)

    sim_without("three", "curl", "libssl3t64")        # two of the three alerted packages got installed (unattended-upgrades)
    s = scan_now()["scan"]
    time.sleep(1.5)
    check("fewer alerted packages than before is no news: the one left is not announced again", s["security"] == 1
          and [p["name"] for p in s["packages"] if p["security"]] == ["openssl"] and len(telegram()) == 3, (s["security"], telegram()[3:]))
    sim("one")
    scan_now()
    m = until(lambda: len(telegram()) == 4 and telegram()[3], 10)
    check("a package that is new is announced", m and m.split("\n")[0] == "Defiant: 1 security update pending" and "python3-urllib3" in m, m)


def story_rate_limit():
    reset(FIRST=3600, GAP=6)
    sim("three")
    (HOST / "scan-delay").write_text("1.5")
    start_fleet()
    a, b, c = call("POST", SCAN), call("POST", SCAN), call("POST", SCAN)
    check("a scan request needs no key and starts a scan", a[0] == 200 and a[1] == {"ok": True, "started": True}, a[:2])
    check("requests during the scan answer busy", b[1] == c[1] == {"ok": True, "started": False, "reason": "busy"}, (b[1], c[1]))
    st, r, _ = install()
    check("an install during a scan answers 409 and starts nothing", st == 409 and r["error"] == "A scan is running. Try again in a minute."
          and host_calls("systemd-run") == [], (st, r))
    must(until(lambda: (u := view()) and u["scan"]["at"] and not u["scan"]["busy"], 20), "the scan did not end")
    d = call("POST", SCAN)
    check("a request soon after a scan answers recent", d[1] == {"ok": True, "started": False, "reason": "recent"}, d[1])
    time.sleep(max(0, ssh_times("scan")[0] + UPD["GAP"] + 0.7 - time.time()))
    e = call("POST", SCAN)
    check("after UPD_GAP the next request starts a scan", e[1] == {"ok": True, "started": True}, e[1])
    must(until(lambda: not view()["scan"]["busy"], 20), "the second scan did not end")
    check("only the two accepted requests scanned", len(ssh_times("scan")) == 2, ssh_times("scan"))


def story_schedule():
    reset(FIRST=2, RETRY=5, EVERY=10)
    sim("three")
    (HOST / "scan-delay").write_text("1")
    t0 = time.time()
    start_fleet()
    first = until(lambda: ssh_times("scan"), 15)
    check("the first scan starts by itself shortly after start", first and 1 < first[0] - t0 < 8, (first, t0))
    must(until(lambda: len(ssh_times("scan", "END")) >= 3, 40), "fewer than three scans in 40 seconds")
    begins, ends = ssh_times("scan"), ssh_times("scan", "END")
    gaps = [b - a for a, b in zip(begins, begins[1:])]
    check("then it scans by itself every UPD_EVERY", all(9.5 < g < 13 for g in gaps), gaps)
    check("two scans never run at once", len(begins) - len(ends) in (0, 1) and all(b > e for e, b in zip(ends, begins[1:])), (begins, ends))
    flag("apt-lock", True)
    n = len(ssh_times("scan"))
    must(until(lambda: len(ssh_times("scan")) >= n + 4, 60), "no retries")
    tries = ssh_times("scan")[n:]
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    check("a failed scan is retried after UPD_RETRY", len(gaps) >= 3 and all(4.5 < g < 8 for g in gaps), gaps)
    check("the failed scans show their error on the last good data", "lock" in view()["scan"]["error"] and view()["scan"]["count"] == 5, view()["scan"])
    flag("apt-lock", False)
    check("a retry that works clears the error", until(lambda: view()["scan"]["error"] is None, 20), view()["scan"])


def story_install():
    reset(FIRST=3600)
    sim("many")
    start_fleet()
    scan_now()
    time.sleep(1.5)
    alerts = len(telegram())
    for label, key in (("without a key", None), ("with a wrong key", "nope")):
        st, r, _ = install(key)
        check(f"install {label} answers 403 with the policy text", st == 403 and r["error"] == POLICY, (st, r))
    stop_gate()
    st, r, _ = install(KEY)
    check("install answers 403 when the gate does not answer (fails closed)", st == 403 and "Cannot verify the Starfleet key" in r["error"], (st, r))
    start_gate()
    check("none of them started anything on the host", host_calls("systemd-run") == [] and ssh_times("install") == []
          and view()["job"]["state"] == "idle", view()["job"])
    asked = lines(GATE_LOG)
    check("the gate was only asked for the key check and the model lists", {l.split()[:2][1] for l in asked} <= {"/starfleet/check", "/v1/models", "/running"}
          and all(l.startswith("GET ") for l in asked), sorted(set(asked)))

    job(rc=0, delay=16, per=2, after="quiet")
    t0 = time.time()
    st, r, hdr = install()
    check("install with the key starts the unit and returns", st == 200 and r["ok"] is True and abs(r["started_at"] - t0) < 5
          and hdr["Cache-Control"] == "no-store", (st, r))
    u = view()
    check("the job is running at once", u["job"]["state"] == "running" and u["job"]["started_at"] == r["started_at"], u["job"])
    st, r2, _ = install()
    check("a second install answers 409", st == 409 and r2["error"] == "An update is already running.", (st, r2))
    check("a scan request during the install answers busy", call("POST", SCAN)[1] == {"ok": True, "started": False, "reason": "busy"})
    started = host_calls("systemd-run")
    check("the unit is started once, detached, and never autoremoves or reboots", len(started) == 1
          and all(w in started[0] for w in ("--unit=fleet-apt-upgrade", "--collect", "-E DEBIAN_FRONTEND=noninteractive", "full-upgrade", "-p StandardOutput=truncate:"))
          and not any(w in started[0] for w in ("autoremove", "reboot")), started)
    check("the scan listed no removals, so the unit runs apt with --no-remove", view()["scan"]["remove"] == [] and "--no-remove full-upgrade" in started[0], started)

    logs = []
    def poll():
        j = view()["job"]
        logs.append((len(j["log"]), j["log"][:1], j["lost"]))
        return j["state"] != "running" and view()
    u = must(until(poll, 60, 1), "the install did not end in 60 seconds")
    j, sizes = u["job"], [n for n, _, _ in logs]
    first = next((first[0] for n, first, _ in logs if n), "")
    check("the log tail grows while the install runs, from the unit's start line", len(set(sizes)) >= 3 and sizes == sorted(sizes)
          and first.startswith("FLEET-APT-START "), (sizes, first))
    check("the install ends done, exit 0, 20 installed, no reboot pending", j["state"] == "done" and j["exit"] == 0 and j["installed"] == 20
          and j["reboot_required"] is False and j["lost"] is False and not any(l for _, _, l in logs), j)
    check("the job ends after it started and its log ends with the exit line", j["ended_at"] >= j["started_at"]
          and j["log"][-1].startswith("FLEET-APT-EXIT 0 "), (j["started_at"], j["ended_at"], j["log"][-2:]))
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > v["job"]["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the install")
    s = u["scan"]
    check("a rescan follows at once: nothing left to install, no reboot pending", s["count"] == 0 and s["reboot_required"] is False
          and s["reboot_pkgs"] == [] and u["critical"] is False, s)
    time.sleep(1.5)
    check("the install success is not alerted, and an install that needs no reboot is followed by none", len(telegram()) == alerts
          and view()["reboot"]["state"] == "idle" and ssh_times("reboot") == [], (telegram()[alerts:], view()["reboot"]))
    d = call("GET", "/api/defiant")[1]
    check("/api/defiant shows the finished job", d["reboot_required"] is False and d["upd"]["job_state"] == "done"
          and d["upd"]["count"] == 0 and d["upd"]["reboot_state"] == "idle", d)

    # a short failing run right after a long one: the unit's log must be its own, not the end of the long run's with
    # that run's exit line (a log opened with file: keeps it; the unit truncates it)
    job(rc=100, delay=2, per=1)
    must(until(lambda: not view()["scan"]["busy"], 20), "a scan was still running")
    st, r, _ = install()
    must(st == 200, f"the second install was refused: {st} {r}")
    u = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v, 40, 1), "the second install did not end")
    j = u["job"]
    check("a short failing install after a long one ends failed with its own exit code, not the long one's result", j["state"] == "failed"
          and j["exit"] == 100 and j["installed"] is None and j["started_at"] == r["started_at"] and j["ended_at"] >= j["started_at"], j)
    check("... and its log is its own: from its start line to its exit line, nothing of the long run", j["log"][0].startswith("FLEET-APT-START ")
          and j["log"][-1].startswith("FLEET-APT-EXIT 100 ") and not any("libexample16" in l or "Processing triggers" in l for l in j["log"]), j["log"])


def story_install_guard():
    """The guard: the install runs apt with --no-remove unless the latest scan is good, recent and lists removals. Here the scan lists
    none, then the host changes: apt would have to remove a package nobody was shown. It refuses and changes nothing,
    the job ends failed with the hint, one long Telegram message says so, a rescan lists the removal, and the next install,
    which the confirm step named it for, goes through without the flag."""
    reset(FIRST=3600)
    sim("many")                                          # what the page shows: 20 packages, no removals
    start_fleet()
    s = must(scan_now()["scan"], "no scan")
    must(s["count"] == 20 and s["remove"] == [], s)
    sim("removal")                                       # the host changed after that scan: apt would now have to remove libold-compat1
    job(rc=0, delay=3, per=2, after="quiet")             # what a real upgrade would do afterwards
    st, r, _ = install()
    must(st == 200, f"install refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 1), "the install did not end")
    check("apt was started with --no-remove, once, and only the upgrade carries it", len(upgrades()) == 1 and " --no-remove full-upgrade" in upgrades()[0]
          and not any("--no-remove" in l for l in host_calls("apt-get") if " update" in l), upgrades())
    check("it refused: the job ended failed, exit 100, nothing installed", j["state"] == "failed" and j["exit"] == 100 and j["installed"] is None
          and any("Packages need to be removed but remove is disabled" in l for l in j["log"]), j)
    check("the tail of the log ends with the hint, after apt's exit line", j["log"][-1] == HINT and j["log"][-2].startswith("FLEET-APT-EXIT 100 ")
          and len(j["log"]) <= 40, j["log"][-3:])
    check("nothing was changed on the host", not (HOST / "applied.log").exists() and (HOST / "apt-sim.txt").read_text() == SIMS["removal"].read_text())
    must(until(failures, 20), "no failure message")
    f = failures()
    body = f[0].split("\n")
    check("one long Telegram message says it failed, and carries the hint as its last line", len(f) == 1 and body[0] == "Defiant: update install failed (exit 100)"
          and body[1] == "" and body[-1] == HINT and len(f[0]) < 3500, f)
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > s["at"] and not v["scan"]["busy"] and v, 30), "no rescan after the failed install")   # newer than the scan before the install
    check("a rescan follows the failed install, and it lists the removal", u["scan"]["at"] > j["ended_at"] and u["scan"]["remove"] == ["libold-compat1"]
          and u["scan"]["count"] == 2 and u["job"]["state"] == "failed", u["scan"])
    time.sleep(2)
    check("no second failure message", len(failures()) == 1, failures())
    st, r, _ = install()
    must(st == 200, f"the second install was refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 1), "the second install did not end")
    check("the next install runs apt without the flag and goes through", j["state"] == "done" and j["exit"] == 0 and len(upgrades()) == 2
          and "--no-remove" not in upgrades()[1] and "--no-remove" not in host_calls("systemd-run")[1], (j["state"], upgrades()))
    check("... and it changed the host, once", len(lines(HOST / "applied.log")) == 1 and "--no-remove" not in lines(HOST / "applied.log")[0], lines(HOST / "applied.log"))
    check("... with no new failure message", len(failures()) == 1, failures())


def story_install_removals():
    """The scan lists a removal, so the confirm step named it: apt runs without --no-remove and may remove it."""
    reset(FIRST=3600)
    sim("removal")
    job(rc=0, delay=3, per=2, after="quiet")
    start_fleet()
    s = must(scan_now()["scan"], "no scan")
    check("the scan lists the removal", s["remove"] == ["libold-compat1"] and s["count"] == 2 and s["security"] == 2, s)
    st, r, _ = install()
    must(st == 200, f"install refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 1), "the install did not end")
    check("apt was started without --no-remove", len(upgrades()) == 1 and "--no-remove" not in upgrades()[0] and " full-upgrade" in upgrades()[0]
          and "--no-remove" not in host_calls("systemd-run")[0], (upgrades(), host_calls("systemd-run")))
    check("the install went through and changed the host", j["state"] == "done" and j["exit"] == 0 and len(lines(HOST / "applied.log")) == 1
          and not any(l.startswith("Fleet: ") for l in j["log"]) and failures() == [], j)


def story_install_failure():
    reset(FIRST=3600)
    job(rc=100, delay=3, per=16)           # 48 lines: more than the 40 Fleet keeps
    start_fleet()
    st, r, _ = install()
    must(st == 200, f"install refused: {st} {r}")
    u = must(until(lambda: (v := view()) and v["job"]["state"] == "failed" and v, 40, 1), "the install did not fail")
    j = u["job"]
    check("a failed install is failed, with its exit code and no installed count", j["exit"] == 100 and j["installed"] is None
          and j["log"][-1].startswith("FLEET-APT-EXIT 100 ") and len(j["log"]) == 40, j)
    must(until(failures, 20), "no failure message")
    time.sleep(12)                                   # a rescan and more polls must not send it again
    f = failures()
    body = f[0].split("\n")
    check("a failed install sends one long Telegram message", len(f) == 1 and body[0] == "Defiant: update install failed (exit 100)"
          and body[1] == "" and len(body) == 2 + 25 and len(f[0]) < 3500 and body[-1].startswith("FLEET-APT-EXIT 100"), (len(f), len(body)))
    check("after a failure the job stays failed and the rescan ran", view()["job"]["state"] == "failed" and view()["scan"] is not None, view()["job"]["state"])
    check("no scan had run when it was started, so apt was told --no-remove", len(upgrades()) == 1 and " --no-remove full-upgrade" in upgrades()[0]
          and "--no-remove full-upgrade" in host_calls("systemd-run")[0], upgrades())


def story_ssh_drop():
    reset(FIRST=3600, LOST=40)                        # the outage must stay well below the time after which Fleet gives up
    job(rc=0, delay=26, per=2)
    start_fleet()
    must(install()[0] == 200, "install refused")
    must(until(lambda: len(view()["job"]["log"]) >= 2, 20), "no log yet")
    (HOST / "ssh-down-until").write_text(str(int(time.time()) + 9))
    u = until(lambda: (v := view()) and v["job"]["lost"] and v, 15)
    check("a dropped connection sets lost and the job keeps running", u and u["job"]["state"] == "running", u and u["job"])
    u = until(lambda: (v := view()) and not v["job"]["lost"] and v, 25)
    check("when the host answers again lost clears", u and u["job"]["state"] in ("running", "done"), u and u["job"])
    u = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v, 50, 1), "the job did not end")
    check("the install still ends done, with no failure message", u["job"]["state"] == "done" and u["job"]["exit"] == 0 and failures() == [], u["job"])


def story_restart_mid_job():
    reset(FIRST=3600)
    job(rc=0, delay=16, per=2, reboot=1)                 # the install leaves a reboot pending, but Fleet restarts first: it does not reboot Defiant
    start_fleet()
    st, r, _ = install()
    must(st == 200, "install refused")
    must(until(lambda: len(view()["job"]["log"]) >= 2, 20), "no log yet")
    stop_fleet()
    start_fleet()
    j = view()["job"]
    check("after a Fleet restart the job is still running, with its start time", j["state"] == "running" and j["started_at"] == r["started_at"], j)
    u = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v, 50, 1), "the resumed job did not end")
    check("Fleet resumes watching and sees the install end", u["job"]["state"] == "done" and u["job"]["log"][-1].startswith("FLEET-APT-EXIT 0 ")
          and failures() == [], u["job"])
    check("a rescan follows the resumed job", until(lambda: ((view()["scan"] or {}).get("at") or 0) > u["job"]["ended_at"], 30), view()["scan"])
    check("it needs a reboot, and none follows although the click announced one: that click was another Fleet's", u["job"]["reboot_required"] is True
          and view()["reboot"]["state"] == "idle" and ssh_times("reboot") == [] and scheduled() == [], (u["job"]["reboot_required"], view()["reboot"]))


def story_unit_unknown_to_fleet():
    reset(FIRST=3600)
    job(rc=0, delay=14, per=2, reboot=1)
    start_fleet()
    st, r, _ = install()
    must(st == 200, f"install refused: {st} {r}")
    must(until(lambda: len(view()["job"]["log"]) >= 2, 20), "no log yet")
    stop_fleet()                                     # Fleet went away inside the start window: nothing was saved, the unit runs on
    STATE.unlink(missing_ok=True)
    start_fleet()
    check("a Fleet that lost its state does not know about the running unit", view()["job"]["state"] == "idle", view()["job"])
    st, r, _ = install()
    check("a click is refused: the unit is already running on Defiant", st == 409 and r["error"] == "The update unit is already running on Defiant.", (st, r))
    j = view()["job"]
    check("... but Fleet follows the unit from then on, and starts no second one", j["state"] == "running" and j["started_at"] and len(host_calls("systemd-run")) == 1, j)
    check("... the page can show its progress", until(lambda: len(view()["job"]["log"]) >= 2, 15), view()["job"])
    u = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v, 50, 1), "the followed install did not end")
    check("the install it followed ends done from the unit's own exit line, with no failure message", u["job"]["state"] == "done"
          and u["job"]["exit"] == 0 and u["job"]["log"][-1].startswith("FLEET-APT-EXIT 0 ") and failures() == [], u["job"])
    check("a rescan follows it", until(lambda: ((view()["scan"] or {}).get("at") or 0) > u["job"]["ended_at"], 30), view()["scan"])
    check("it needs a reboot, and none follows although the click announced one: Fleet did not start that unit", u["job"]["reboot_required"] is True
          and view()["reboot"]["state"] == "idle" and ssh_times("reboot") == [] and scheduled() == [], (u["job"]["reboot_required"], view()["reboot"]))


def story_reboot():
    """The button, Reboot Defiant, against a Defiant that wants a reboot: the request and what it asks of the host, the refusals
    while it runs, the host going down and coming back with another boot id, the rescan that clears the flag, no Telegram message for
    a reboot that worked, and the gap that refuses the next one."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1)
    pending_reboot()
    host_reboot(down=6)
    start_fleet(push=True)                              # a ready push sender and a registered device: a push that the reboot caused would be recorded
    push_device()
    scan_now()
    m = must(until(telegram, 10), "no alert for the pending reboot")[0]
    time.sleep(1.5)
    check("the scan found the pending reboot and alerted once, and the alert points at Reboot Defiant (nothing is left to install)", len(telegram()) == 1
          and m.split("\n")[0] == "Defiant: reboot required" and m.split("\n")[-1] == "Reboot: Starfleet, Systems, Defiant, Reboot Defiant"
          and "Install updates" not in m, telegram())
    u = view()
    check("the reboot is idle, and the view carries no boot id", u["reboot"] == {"state": "idle", "started_at": None, "ended_at": None, "down_at": None,
          "back_after": None, "error": None, "auto": False} and u["scan"]["reboot_required"] is True, u["reboot"])

    old, t0 = boot_id(), time.time()
    st, r, hdr = reboot()
    check("Reboot with the key answers 200 and returns at once", st == 200 and r["ok"] is True and abs(r["started_at"] - t0) < 5
          and hdr["Cache-Control"] == "no-store", (st, r))
    x = rb()
    check("the reboot is running, started by the button, and remembers the boot id the host had", x["state"] == "running" and x["started_at"] == r["started_at"]
          and x["auto"] is False and x["down_at"] is None and "boot" not in x and saved()["reboot"]["boot"] == old, x)
    s = scheduled()
    check("the host was told once: a timer three seconds ahead for a plain systemctl reboot, nothing else", len(s) == 1
          and s[0].endswith(" systemd-run --unit=fleet-reboot --on-active=3s --timer-property=AccuracySec=1s --collect /bin/systemctl reboot") and reboots() == [], s)
    check("it asked the three lock files first, never a process name, and checked that fuser runs at all", len(host_calls("fuser")) == 2
          and host_calls("fuser")[0].endswith(" fuser -s /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock")
          and host_calls("fuser")[1].endswith(" fuser -V"), host_calls("fuser"))
    st, r2, _ = reboot()
    check("a second Reboot while it runs answers 409 and schedules nothing", st == 409 and r2["error"] == "A reboot is already running."
          and len(scheduled()) == 1, (st, r2))
    st, r2, _ = install()
    check("an install while it runs answers 409 and starts nothing", st == 409 and r2["error"] == "A reboot is running."
          and host_calls("systemd-run") == scheduled(), (st, r2))
    check("a scan request while it runs says rebooting", call("POST", SCAN)[1] == {"ok": True, "started": False, "reason": "rebooting"})

    x = must(until(lambda: (v := rb())["down_at"] and v, 30, 0.5), "Fleet never saw Defiant go down")
    check("Fleet saw Defiant go down, and the reboot is still running", x["state"] == "running" and x["started_at"] < x["down_at"] and x["ended_at"] is None, x)
    d = call("GET", "/api/defiant")[1]
    check("while Defiant is down its probe says that it does not answer, as always", d["status"] == "error" and d["errors"], (d["status"], d["errors"]))
    done = reboot_over()
    check("it ends done: Defiant answers with another boot id, the seconds are counted from the request", done["state"] == "done" and done["error"] is None
          and done["down_at"] < done["ended_at"] and 8 <= done["back_after"] <= 20 and boot_id() != old and reboots() == ["rebooted"], done)
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > done["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the reboot")
    check("a rescan follows at once, and the reboot flag went with the reboot", u["scan"]["reboot_required"] is False and u["scan"]["reboot_pkgs"] == []
          and u["critical"] is False, u["scan"])
    check("from the request to the end Fleet asked the host for the reboot and the probe only: no scan, no install poll",
          ssh_kinds(x["started_at"], done["ended_at"]) <= {"reboot", "rpoll", "probe"}, ssh_kinds(x["started_at"], done["ended_at"]))
    time.sleep(1.5)
    check("a reboot that worked sends no Telegram message", len(telegram()) == 1 and reboot_failures() == [], telegram())
    st, r2, _ = reboot()
    check("a new Reboot a moment after one answers 409 and schedules nothing", st == 409 and r2["error"] == E_GAP
          and len(scheduled()) == 1, (st, r2))
    d = call("GET", "/api/defiant")[1]
    check("/api/defiant says done, the saved file keeps it, and the host no longer wants a reboot", d["upd"]["reboot_state"] == "done"
          and saved()["reboot"]["state"] == "done" and d["reboot_required"] is False, (d["upd"], saved()["reboot"]))
    check("the Messages list got one entry when it started (by the button) and one when it ended, with the seconds the page shows, and no entry for the refusals",
          trail_is(R_START, R_DONE) and trail_raw()[1] == ("Defiant rebooted", f"Defiant answered again with a new boot after {done['back_after']} seconds."), trail_raw())
    mine = [e for e in entries("audit")[0] if e["title"] in ("Defiant reboot started", "Defiant rebooted")]
    check("... and GET /api/events lists the same two as list-only entries: audit kind, info level, a point in time, no episode, no node, and they open the Defiant view",
          [(e["title"], e["text"]) for e in mine] == trail_raw() and all(e["level"] == "info" and e["state"] == "point" and e["episode"] == "" and e["node"] == ""
          and e["reminder"] == 0 and e["go"] == "#/systems/defiant" for e in mine), mine)
    check("nothing was pushed to the phone, although the sender was ready and a device was registered: a reboot's entries are list only", pushes() == [], pushes())


def story_reboot_refusals():
    """Everything that refuses a reboot, and what each leaves behind: nothing scheduled, nothing rebooted, the same boot id, the reboot idle.
    The key is checked before the host is asked anything. Fleet's own refusals (a scan or an install it knows of) come before it asks
    the host; a host that is busy refuses by its own script."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1)
    start_fleet()
    old = boot_id()
    for label, key in (("without a key", None), ("with a wrong key", "nope")):
        st, r, _ = reboot(key)
        check(f"Reboot {label} answers 403 with the policy text", st == 403 and r["error"] == POLICY, (st, r))
    stop_gate()
    st, r, _ = reboot()
    check("Reboot answers 403 when the gate does not answer (fails closed)", st == 403 and "Cannot verify the Starfleet key" in r["error"], (st, r))
    start_gate()
    check("none of the three asked the host anything", ssh_times("reboot") == [] and scheduled() == [] and rb()["state"] == "idle", rb())

    def refused(why, text, asks_host):
        n = len(ssh_times("reboot"))
        st, r, _ = reboot()
        check(f"{why}: 409, and nothing was scheduled, rebooted or changed", st == 409 and r["error"] == text and scheduled() == [] and reboots() == []
              and boot_id() == old and rb()["state"] == "idle" and len(ssh_times("reboot")) == n + asks_host, (st, r, rb()))

    (HOST / "scan-delay").write_text("4")
    must(call("POST", SCAN)[1].get("started"), "no scan started")
    must(until(lambda: (u := view()["scan"]) and u["busy"], 10), "the scan is not busy")
    refused("a scan is running", "A scan is running. Try again in a moment.", False)
    must(until(lambda: (u := view()["scan"]) and u["at"] and not u["busy"], 20), "the scan did not end")
    (HOST / "scan-delay").write_text("0")
    flag("apt-lock", True)
    refused("apt holds its lock (the fuser stand-in says so)", E_APT, True)
    flag("apt-lock", False)
    flag("fuser-broken", True)                           # sudo cannot run fuser and says 1: the word for "no process holds a lock"
    st, r, _ = reboot()
    check("a lock check that cannot be made is no all-clear: 502 with the fixed text, nothing was scheduled, rebooted or changed", st == 502
          and r["error"] == "Defiant did not accept the reboot request." and scheduled() == [] and reboots() == [] and boot_id() == old and rb()["state"] == "idle", (st, r))
    flag("fuser-broken", False)
    flag("active-fleet-reboot.timer", True)
    refused("a reboot timer is already pending on the host", "A reboot is already running.", True)
    flag("active-fleet-reboot.timer", False)
    (HOST / "ssh-down-until").write_text(str(int(time.time()) + 4))
    st, r, _ = reboot()
    check("a Defiant that does not answer gets 502 and a fixed text, and nothing was scheduled or changed", st == 502 and r["error"] == "Defiant did not accept the reboot request."
          and scheduled() == [] and reboots() == [] and rb()["state"] == "idle", (st, r))
    time.sleep(4.5)
    job(rc=0, delay=12, per=2)
    must(install()[0] == 200, "the install was refused")
    refused("an install is running that Fleet started", E_INSTALL, False)
    stop_fleet()
    STATE.unlink(missing_ok=True)
    start_fleet()
    check("a Fleet that lost its state does not know of the unit", view()["job"]["state"] == "idle", view()["job"])
    refused("an install is running that Fleet does not know of: the host says so", E_INSTALL, True)
    must(until(lambda: "FLEET-APT-EXIT" in (HOST / "var/log/fleet-apt-upgrade.log").read_text(), 30), "the install did not end")
    check("all in all nothing was scheduled, the host never rebooted and no Telegram message was sent", scheduled() == [] and reboots() == []
          and boot_id() == old and reboot_failures() == [], (scheduled(), reboots()))
    flag("systemd-run-fails", True)                      # last, because it is the one refusal that reaches the host's systemd-run
    st, r, _ = reboot()
    check("a Defiant that cannot start the timer gets 502 and a fixed text, and nothing was rebooted or changed", st == 502 and r["error"] == "Defiant did not accept the reboot request."
          and len(scheduled()) == 1 and reboots() == [] and rb()["state"] == "idle" and boot_id() == old, (st, r, scheduled()))
    check("no refusal and no failed start leaves anything in the Messages list: a reboot writes an entry only once it runs", trail_is(), trail_raw())


def story_reboot_no_answer():
    """A start that gets no answer: Defiant sets the timer, but the call does not return (the stand-in systemd-run hangs), so Fleet gives up after
    its 30 s. The host reboots all the same. Fleet must say that it may still reboot (not "did not start") and must not allow a second reboot."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1)
    host_reboot(down=4)
    flag("systemd-run-hangs", True)
    start_fleet()
    t0 = time.time()
    st, r, _ = reboot()
    check("a start that gets no answer within 30 s answers 502 with the fixed text that says Defiant may still reboot", st == 502 and r["error"] == E_UNSURE
          and 29 <= time.time() - t0 < 45, (st, r, time.time() - t0))
    check("Defiant had set the timer all the same, and the host rebooted", len(scheduled()) == 1 and until(lambda: reboots() == ["rebooted"], 15), (scheduled(), reboots()))
    check("Fleet shows no reboot (it never saw one), and sent no message", rb()["state"] == "idle" and telegram() == [], (rb(), telegram()))
    st, r2, _ = reboot()
    check("a second click a moment later answers 409 and schedules nothing more: the first one took effect", st == 409 and r2["error"] == E_GAP
          and len(scheduled()) == 1 and reboots() == ["rebooted"], (st, r2))
    check("nothing in the Messages list either: Fleet never saw this reboot run", trail_is(), trail_raw())
    time.sleep(max(0, 35 - (time.time() - t0)))          # the hung stand-in call ends before the next story resets the host folder


def story_reboot_unsure_restart():
    """A start that got no answer holds the button back for REBOOT_GAP, and that hold is saved: a Fleet that restarts inside the gap (it is deployed
    often) still refuses a second reboot and schedules nothing more, while the made-up host really did reboot after the first request. The gap is
    shortened to a minute in test code only; once it is over, the button works again."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1, REBOOT_GAP=60)
    host_reboot(down=4)
    flag("systemd-run-hangs", True)
    start_fleet()
    t0 = time.time()
    st, r, _ = reboot()
    check("a start that gets no answer within 30 s answers 502 with the fixed text that says Defiant may still reboot", st == 502 and r["error"] == E_UNSURE
          and 29 <= time.time() - t0 < 45, (st, r, time.time() - t0))
    check("Defiant had set the timer all the same, and the host rebooted", len(scheduled()) == 1 and until(lambda: reboots() == ["rebooted"], 15), (scheduled(), reboots()))
    hold = lambda: saved().get("reboot_unsure") if STATE.exists() else None         # the file is written when the hold is set, so it is there
    mark = hold()
    check("the hold is in the saved file, as the time of the request", isinstance(mark, (int, float)) and not isinstance(mark, bool) and t0 - 1 <= mark <= t0 + 10, (mark, t0))
    stop_fleet()
    flag("systemd-run-hangs", False)                     # the next start must answer; the hung call of the first ends by itself
    start_fleet()
    check("the restarted Fleet shows no reboot (it never saw one), and the saved file still holds the same time", rb()["state"] == "idle" and hold() == mark, (rb(), hold(), mark))
    st, r2, _ = reboot()
    check("a click inside the gap answers 409 with the gap's sentence, asks the host nothing, and schedules nothing more", st == 409 and r2["error"] == E_GAP
          and len(scheduled()) == 1 and len(ssh_times("reboot")) == 1 and reboots() == ["rebooted"], (st, r2, scheduled(), ssh_times("reboot")))
    time.sleep(max(0, (mark or t0) + 61 - time.time()))      # the gap, counted from the first request, is over
    st, r3, _ = reboot()
    must(st == 200, f"the button did not work after the gap: {st} {r3}")
    done = reboot_over()
    check("after the gap the button reboots Defiant: done, started by the button, two reboots and two timers in all", done["state"] == "done" and done["auto"] is False
          and done["error"] is None and reboots() == ["rebooted", "rebooted"] and len(scheduled()) == 2, (done, reboots(), scheduled()))
    check("the Messages list has only this reboot's two entries: the first one was never seen to run", trail_is(R_START, R_DONE), trail_raw())


def story_reboot_mid_start_restart():
    """A restart of Fleet while the start call is on its way: Defiant has set the timer (the stand-in systemd-run does, then hangs) and Fleet is stopped before it gets
    the answer, with no cleanup, as a deploy stops it. The hold is in the saved file already (it is written before Defiant is asked), so the restarted Fleet answers a
    click with 409 and schedules nothing more, while the made-up host really reboots after the first request. The gap is shortened to a minute in test code only."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1, REBOOT_GAP=60)
    host_reboot(down=4)
    flag("systemd-run-hangs", True)
    start_fleet()
    t0, answers = time.time(), []

    def press():
        try:
            answers.append(reboot())
        except Exception as e:                           # Fleet is stopped under the call: the connection is cut, and no answer comes
            answers.append(type(e).__name__)
    threading.Thread(target=press, daemon=True).start()
    must(until(lambda: len(scheduled()) == 1, 15, 0.1), "Defiant never got the request")
    hold = lambda: saved().get("reboot_unsure") if STATE.exists() else None         # None: no file yet
    mark = hold()
    check("while the call is on its way, the saved file already holds the hold as the time of the request, and no reboot is running", isinstance(mark, (int, float))
          and not isinstance(mark, bool) and t0 - 1 <= mark <= t0 + 10 and saved()["reboot"]["state"] == "idle" and answers == [], (mark, t0, answers))
    stop_fleet()                                         # a stop runs no cleanup: only what the file holds is left
    flag("systemd-run-hangs", False)                     # the next start must answer; the hung call of the first ends by itself
    start_fleet()
    check("the restarted Fleet shows no reboot (it never saw one), and the saved file still holds the same time", rb()["state"] == "idle" and hold() == mark, (rb(), hold(), mark))
    st, r2, _ = reboot()
    check("a click right after the restart answers 409 with the gap's sentence, asks the host nothing, and schedules nothing more", st == 409 and r2["error"] == E_GAP
          and len(scheduled()) == 1 and len(ssh_times("reboot")) == 1, (st, r2, scheduled(), ssh_times("reboot")))
    check("the host rebooted after the first request all the same, once, and the call that the stop cut off got no answer", until(lambda: reboots() == ["rebooted"], 15)
          and reboots() == ["rebooted"] and until(lambda: answers, 5) and isinstance(answers[0], str), (reboots(), answers))
    check("Fleet never saw that reboot run: no Telegram message, and nothing in the Messages list", telegram() == [] and trail_is(), (telegram(), trail_raw()))
    time.sleep(max(0, 36 - (time.time() - t0)))         # the hung stand-in call ends before the next story resets the host folder


def story_reboot_scan_race():
    """The worker has decided to scan, and a reboot is requested before the scan starts: the two must not run together (a scan would ask a host that is
    going down). Test code makes that moment last SCAN_LAG seconds (it leaves a marker when it begins) and the button is pressed inside it. The scan
    that was decided on must not start while the reboot runs; it runs after it."""
    reset(FIRST=3, EVERY=3600, RETRY=3600)
    RB.update(REBOOT_POLL_S=1, SCAN_LAG=6)
    host_reboot(down=8)
    (SBX / "scan-decided").unlink(missing_ok=True)
    start_fleet()
    must(until(lambda: (SBX / "scan-decided").exists(), 30, 0.1), "the worker never decided to scan")
    st, r, _ = reboot()
    must(st == 200, f"the reboot was refused: {st} {r}")
    check("the request came in the moment between the decision and the scan: no scan had started, and the host was told to reboot", ssh_times("scan") == []
          and ssh_times("scan", "down") == [] and len(ssh_times("reboot")) == 1 and rb()["state"] == "running", (ssh_times("scan"), rb()))
    done = reboot_over(60)
    check("the reboot ended done, and from the request to the end no scan was started or tried: only the reboot's own calls reached the host",
          done["state"] == "done" and ssh_kinds(r["started_at"], done["ended_at"]) <= {"reboot", "rpoll", "probe"}, (done, ssh_kinds(r["started_at"], done["ended_at"])))
    u = must(until(lambda: (v := view()) and v["scan"] and v["scan"]["at"] and not v["scan"]["busy"] and v, 45), "the scan that was decided on never ran")
    check("the scan that was skipped follows the reboot: it started after the reboot ended, and the reboot flag is gone", len(ssh_times("scan")) >= 1
          and min(ssh_times("scan")) > done["ended_at"] and u["scan"]["at"] > done["ended_at"] and u["scan"]["error"] is None, (ssh_times("scan"), done["ended_at"], u["scan"]))


def story_reboot_never():
    """A host that ignores the reboot stays on its boot id: after REBOOT_START_WAIT (shortened here) the reboot is failed, with the fixed text
    and one Telegram message, and it stays failed. The button works again afterwards."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1, REBOOT_START_WAIT=8, REBOOT_GAP=5)
    host_reboot(never=True)
    start_fleet()
    old = boot_id()
    st, r, _ = reboot()
    must(st == 200, f"the reboot was refused: {st} {r}")
    f = must(until(lambda: (v := rb())["state"] == "failed" and v, 40, 0.5), "the reboot did not fail")
    check("it ends failed with the fixed text: Defiant never went down and kept its boot id", f["error"] == E_SAME_BOOT and f["down_at"] is None
          and f["back_after"] is None and f["ended_at"] - f["started_at"] > 8 and reboots() == ["ignored"] and boot_id() == old, f)
    must(until(reboot_failures, 15), "no failure message")
    time.sleep(6)                                       # more polls must not send it again, move the state or rescan
    t = telegram()
    check("exactly one Telegram message: the headline, a blank line and the error, nothing after it (the error says where to look)", t == [reboot_text(E_SAME_BOOT)] and len(t[0]) < 3500
          and t[0].split("\n")[0] == "Defiant: reboot failed" and t[0].endswith("check it in your hosting provider's console.") and chr(0x2014) not in t[0], t)
    check("the same failure is in the Messages list once: the start, then the failure with the error cut at a word (140 characters at most)",
          trail_is(R_START, R_SAME_BOOT) and len(trail_raw()[1][1]) <= 140, trail_raw())
    check("the failed reboot stays as it was, and no rescan follows it", rb() == f and ssh_times("scan") == [], rb())
    host_reboot(down=4)                                 # the host reboots this time
    st, r, _ = reboot()
    check("the button works again after a failed reboot", st == 200, (st, r))
    done = reboot_over()
    check("the new reboot ends done and replaces the failure", done["state"] == "done" and done["error"] is None and boot_id() != old
          and reboots() == ["ignored", "rebooted"], done)
    check("... with no further message, and its own two entries in the Messages list after the failure's", len(telegram()) == 1
          and trail_is(R_START, R_SAME_BOOT, R_START, R_DONE), (telegram(), trail_raw()))


def story_reboot_giveup():
    """A host that goes down and does not come back: after REBOOT_GIVEUP (shortened here) the reboot is failed, with one Telegram message. A
    Defiant that answers again later does not undo that: only a running reboot moves."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1, REBOOT_GIVEUP=10)
    host_reboot(down=100000)
    start_fleet()
    st, r, _ = reboot()
    must(st == 200, f"the reboot was refused: {st} {r}")
    f = must(until(lambda: (v := rb())["state"] == "failed" and v, 40, 0.5), "the reboot did not fail")
    check("it ends failed: Defiant went down and did not answer again", f["error"] == E_GIVEUP and f["down_at"] and f["back_after"] is None
          and f["ended_at"] - f["started_at"] > 10 and reboots() == ["rebooted"], f)
    must(until(reboot_failures, 15), "no failure message")
    check("one Telegram message: the headline, a blank line and the error", telegram() == [reboot_text(E_GIVEUP)], telegram())
    check("the Messages list has the start and the failure, once each", trail_is(R_START, R_GIVEUP), trail_raw())
    (HOST / "ssh-down-until").write_text(str(int(time.time())))        # Defiant answers again, on its new boot id
    time.sleep(4)
    check("a Defiant that answers again later does not undo it, and nothing more is sent or written", rb() == f and len(telegram()) == 1 and trail_is(R_START, R_GIVEUP),
          (rb(), trail_raw()))


def story_reboot_restart():
    """Fleet restarts in the middle of a reboot, while Defiant is down: it picks the reboot up from its saved state and sees it through, counted from
    the original request. A scan is due every few seconds in this story, and none may start until the reboot is over."""
    reset(FIRST=2, EVERY=3, RETRY=3)
    RB.update(REBOOT_POLL_S=1)
    pending_reboot()
    host_reboot(down=10)
    start_fleet()
    must(until(lambda: view()["scan"] and view()["scan"]["at"], 20), "no first scan")
    st, r, _ = reboot_when_idle()
    x = must(until(lambda: (v := rb())["down_at"] and v, 30, 0.5), "Fleet never saw Defiant go down")
    stop_fleet()
    time.sleep(2)                                       # Fleet is away while Defiant is down
    start_fleet()
    y = rb()
    check("after the restart the reboot is running, with the times it was saved with", y["state"] == "running" and y["started_at"] == r["started_at"]
          and y["down_at"] == x["down_at"] and y["auto"] is False, (y, x))
    done = reboot_over()
    check("Fleet sees it through: done, counted from the original request", done["state"] == "done" and done["started_at"] == r["started_at"]
          and done["back_after"] >= 11 and done["error"] is None, done)
    check("no scan and no install poll started from the request to the end, although a scan was due all the time",
          ssh_kinds(r["started_at"], done["ended_at"]) <= {"reboot", "rpoll", "probe"}, ssh_kinds(r["started_at"], done["ended_at"]))
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > done["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the reboot")
    check("the rescan follows at once, and the reboot flag is gone", u["scan"]["reboot_required"] is False and len(telegram()) == 1 and reboot_failures() == [], telegram())
    check("the Messages list has the start (written before the restart) and the end (after it), once each, with the seconds counted from the original request",
          trail_is(R_START, R_DONE) and trail_raw()[1] == ("Defiant rebooted", f"Defiant answered again with a new boot after {done['back_after']} seconds."), trail_raw())


def story_reboot_after_install():
    """The reboot after an install: an install that leaves a reboot pending is followed by a reboot that Fleet starts by itself, in the tick that records the end of the
    install, without a second key check and without the gap (a reboot by the button just before would refuse another by the button). The
    rescan follows the reboot, not the install."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1)                          # REBOOT_GAP stays Fleet's own: ten minutes
    sim("many")
    start_fleet()
    scan_now()
    must(until(telegram, 10), "no alert for the security updates")
    host_reboot(down=4)
    st, r, _ = reboot()                                 # a reboot by the button first: the gap is open from now on
    must(st == 200, f"the reboot was refused: {st} {r}")
    first = reboot_over()
    must(first["state"] == "done", first)
    must(until(lambda: (v := view()) and v["scan"]["at"] > first["ended_at"] and not v["scan"]["busy"]), "no rescan after the first reboot")
    st, r2, _ = reboot()
    check("the gap is open: a second Reboot by the button answers 409", st == 409 and r2["error"] == E_GAP, (st, r2))
    check("the button's reboot left its two entries in the Messages list", trail_is(R_START, R_DONE), trail_raw())

    host_reboot(down=6)
    job(rc=0, delay=6, per=2, reboot=1, after="quiet")
    checks, calls = len(key_checks()), len(host_calls("systemd-run"))
    st, r, _ = install()
    must(st == 200, f"the install was refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 0.5), "the install did not end")
    check("the install ends done and says that a reboot is needed", j["state"] == "done" and j["exit"] == 0 and j["reboot_required"] is True, j)
    x = must(until(lambda: (v := rb())["auto"] and v, 20, 0.2), "Fleet did not reboot Defiant after the install")
    t_start = ssh_times("reboot")[1]                    # the first was the button's
    t_poll = max(t for t in ssh_times("poll", "END") if t < t_start)
    check("Fleet started the reboot by itself, in spite of the gap: auto, running, and the call after the poll that saw the install end", x["state"] == "running"
          and x["down_at"] is None and "boot" not in x and t_start - t_poll < 4 and ssh_kinds(t_poll + 1e-4, t_start - 1e-4) == set(), (x, t_start - t_poll))
    done = reboot_over()
    check("it ends done, still marked as the install's, and the host rebooted", done["state"] == "done" and done["auto"] is True and done["error"] is None
          and done["back_after"] >= 8 and reboots() == ["rebooted", "rebooted"] and saved()["reboot"]["auto"] is True, done)
    check("the key was checked once for all this, at the install click: not for the automatic reboot", len(key_checks()) == checks + 1, key_checks()[checks:])
    check("the host was told what the button tells it, once more: a timer for a plain systemctl reboot", len(scheduled()) == 2
          and scheduled()[1].endswith(" systemd-run --unit=fleet-reboot --on-active=3s --timer-property=AccuracySec=1s --collect /bin/systemctl reboot") and len(host_calls("systemd-run")) == calls + 2, scheduled())
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > done["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the reboot")
    check("the rescan follows the reboot: nothing to install, and the reboot flag cleared with it", u["scan"]["count"] == 0
          and u["scan"]["reboot_required"] is False and u["critical"] is False, u["scan"])
    check("no scan and no install poll ran from the start of the reboot to its end", ssh_kinds(t_start, done["ended_at"]) <= {"reboot", "rpoll", "probe"},
          ssh_kinds(t_start, done["ended_at"]))
    time.sleep(1.5)
    check("no Telegram message for the install or the reboot: only the alert for the security updates", len(telegram()) == 1 and failures() == [] and reboot_failures() == [], telegram())
    check("the install changed the host once", len(lines(HOST / "applied.log")) == 1 and view()["job"]["state"] == "done", lines(HOST / "applied.log"))
    check("the automatic reboot wrote its own two entries after the button's: started by Fleet after the install, then rebooted",
          trail_is(R_START, R_DONE, R_AUTO, R_DONE), trail_raw())


def story_reboot_after_install_busy():
    """The reboot after an install, when it cannot start: the nightly run takes the apt lock the moment the install ends. The reboot is failed with
    the reason, one Telegram message says so, the install stays done, nothing is scheduled, and the button works again once apt is idle."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1)
    job(rc=0, delay=4, per=2, reboot=1, lock=1, after="quiet")
    start_fleet()
    checks = len(key_checks())
    st, r, _ = install()
    must(st == 200, f"the install was refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 0.5), "the install did not end")
    x = must(until(lambda: (v := rb())["state"] == "failed" and v, 20, 0.2), "the reboot did not fail")
    check("the install is done and wants a reboot, and the reboot after it is failed", j["state"] == "done" and j["reboot_required"] is True
          and view()["job"]["state"] == "done" and x["auto"] is True, (j, x))
    check("it names the reason, never started and never saw Defiant go down", x["error"] == E_AUTO_APT and x["started_at"] is None and x["down_at"] is None
          and x["back_after"] is None
          and 0 <= x["ended_at"] - j["ended_at"] < 7, x)      # the poll that saw the install end (every 5 s, and the host's end time has whole seconds) is the one that fails the reboot
    check("the host was asked once, said apt was busy, and was told nothing", len(ssh_times("reboot")) == 1 and len(host_calls("fuser")) == 1
          and scheduled() == [] and reboots() == [], (scheduled(), reboots()))
    must(until(reboot_failures, 15), "no failure message")
    time.sleep(3)
    check("exactly one Telegram message: the headline, a blank line and the reason (nothing after it)", telegram() == [reboot_text(E_AUTO_APT)], telegram())
    check("the Messages list has one entry for it, the failure with the reason cut at a word (140 characters at most), and none for a start that never happened",
          trail_is(R_AUTO_APT) and len(trail_raw()[0][1]) <= 140, trail_raw())
    check("no second key check for the automatic start", len(key_checks()) == checks + 1, key_checks()[checks:])
    flag("apt-lock", False)                             # apt is idle again
    host_reboot(down=4)
    must(until(lambda: not view()["scan"] or not view()["scan"]["busy"], 20), "a scan was still running")
    st, r, _ = reboot_when_idle()
    done = reboot_over()
    check("the button reboots Defiant now, and its result replaces the failure", done["state"] == "done" and done["auto"] is False and done["error"] is None
          and len(scheduled()) == 1 and reboots() == ["rebooted"] and len(telegram()) == 1, done)
    check("... and its two entries follow the failure's in the Messages list", trail_is(R_AUTO_APT, R_START, R_DONE), trail_raw())


def story_no_auto_reboot():
    """The reboot after an install, the cases that must not reboot, with the click as the pages send it (the flag that announces the reboot is there, so it is the case
    that decides): an install that needs no reboot, a failed install on a host that wants one, and the scan that then finds one (what the nightly
    run leaves behind). The host's reboot script is never even asked."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1)
    start_fleet()
    scan_now()
    job(rc=0, delay=3, per=2, after="quiet")
    st, r, _ = install()
    must(st == 200, f"the install was refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 0.5), "the install did not end")
    must(until(lambda: (v := view()) and v["scan"]["at"] > j["ended_at"] and not v["scan"]["busy"]), "no rescan after the install")
    check("an install that needs no reboot is done and followed by none", j["state"] == "done" and j["reboot_required"] is False
          and rb()["state"] == "idle" and ssh_times("reboot") == [], (j, rb()))

    pending_reboot()                                    # the host wants a reboot now, whoever left it
    job(rc=100, delay=2, per=1)
    st, r, _ = install()
    must(st == 200, f"the install was refused: {st} {r}")
    j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 0.5), "the install did not end")
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > j["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the install")
    must(until(lambda: len(telegram()) == 2, 15), "the failure and the reboot alert were not both sent")
    scan_now()
    time.sleep(4)                                       # a reboot would start in the tick that ended the install; give any later one time to show
    check("a failed install on a host that wants a reboot is failed, and none follows", j["state"] == "failed" and j["reboot_required"] is True
          and rb()["state"] == "idle" and ssh_times("reboot") == [] and scheduled() == [] and reboots() == [], (j, rb()))
    check("the scans find the pending reboot and start nothing", u["scan"]["reboot_required"] is True and view()["scan"]["reboot_required"] is True
          and rb()["state"] == "idle" and ssh_times("reboot") == [], (view()["scan"], rb()))
    check("one Telegram message for the failed install and one for the pending reboot, none for a reboot", [t.split("\n")[0] for t in telegram()]
          == ["Defiant: update install failed (exit 100)", "Defiant: reboot required"], telegram())
    check("nothing about a reboot in the Messages list", trail_is(), trail_raw())


def story_install_without_flag():
    """Fleet reboots Defiant after an install only when the click announced it: the body {"reboot_ok": true}, which both pages send. A page that was open
    before this existed (it shows the old confirm text and sends {}), a script, a flag that is not the boolean true and a body that is no object install as
    before and are never followed by a reboot: the install ends done and says that a reboot is needed, the rescan shows it waiting, the alert for the pending
    reboot is as always, and nothing is scheduled or written. Then the same install with the flag reboots, so the flag is what decides."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1)
    start_fleet(push=True)                              # a ready push sender and a registered device: a push that a reboot caused would be recorded
    push_device()
    scan_now()
    host_reboot(down=4)
    for label, body in (("an older page's {}", {}), ('the string "true" for the flag', {"reboot_ok": "true"}), ("a list for a body", [True])):
        job(rc=0, delay=3, per=2, reboot=1, after="quiet")
        st, r, _ = install(body=body)
        must(st == 200, f"the install with {label} was refused: {st} {r}")
        j = must(until(lambda: (v := view()) and v["job"]["state"] != "running" and v["job"], 40, 0.5), "the install did not end")
        u = must(until(lambda: (v := view()) and v["scan"]["at"] > j["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the install")
        time.sleep(2)                                   # a reboot would start in the tick that ended the install; give any later one time to show
        check(f"an install with {label} ends done and needs a reboot, and none follows: the reboot idle, nothing asked of the host or scheduled, the same boot",
              j["state"] == "done" and j["exit"] == 0 and j["reboot_required"] is True and rb()["state"] == "idle" and rb()["auto"] is False
              and ssh_times("reboot") == [] and scheduled() == [] and reboots() == [] and boot_id() == BOOT0, (j, rb()))
        check("... and the scan after it shows the reboot waiting, with its packages, while the job stays done",
              u["scan"]["reboot_required"] is True and sorted(u["scan"]["reboot_pkgs"]) == ["libc6", "linux-image-6.8.0-99-generic"] and u["job"]["state"] == "done", u["scan"])
    t = telegram()
    check("the alert for the pending reboot went out once, as always, and points at Reboot Defiant; no failure message", len(t) == 1 and t[0].split("\n")[0] == "Defiant: reboot required"
          and t[0].split("\n")[-1] == "Reboot: Starfleet, Systems, Defiant, Reboot Defiant" and failures() == [] and reboot_failures() == [], t)
    check("nothing about a reboot in the Messages list", trail_is(), trail_raw())

    job(rc=0, delay=3, per=2, reboot=1, after="quiet")
    st, r, _ = install()                                # the click as the pages send it: with the flag
    must(st == 200, f"the install with the flag was refused: {st} {r}")
    must(until(lambda: (v := rb())["auto"] and v, 40, 0.2), "Fleet did not reboot Defiant after the install that announced it")
    done = reboot_over()
    check("the same install with the flag is followed by the reboot Fleet starts itself: auto, done, one timer, the host on a new boot", done["state"] == "done"
          and done["auto"] is True and done["error"] is None and reboots() == ["rebooted"] and len(scheduled()) == 1 and boot_id() != BOOT0, (done, reboots(), scheduled()))
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > done["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the reboot")
    check("the rescan follows the reboot and the flag is gone; still only the one alert, and the reboot wrote its two entries", u["scan"]["reboot_required"] is False
          and len(telegram()) == 1 and trail_is(R_AUTO, R_DONE), (u["scan"], telegram(), trail_raw()))
    check("nothing was pushed to the phone for the reboot Fleet started, although the sender was ready and a device was registered", pushes() == [], pushes())


def story_pages(page):
    """The page script runs one reboot that Fleet starts after an install, one by hand that the host ignores (ten seconds are enough to call it failed), one by hand that works,
    and an install whose own reboot cannot start because the nightly run takes the apt lock."""
    reset(FIRST=3600)
    RB.update(REBOOT_POLL_S=1, REBOOT_START_WAIT=10, REBOOT_GAP=5)
    start_fleet()
    env = dict(os.environ, E2E_BASE=f"http://127.0.0.1:{PORT}", E2E_HOST=str(HOST), E2E_SIMS=json.dumps({k: str(v) for k, v in SIMS.items()}),
               OUT=os.environ.get("OUT", str(HERE / "out")))
    r = subprocess.run(["node", str(HERE / "ui.js"), page], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
    for line in r.stdout.splitlines():
        if line.startswith(("ok   ", "FAIL ")):
            results.append(line.startswith("ok   "))
        print(line, flush=True)
    if r.returncode and "FAIL " not in r.stdout:
        check(f"{page} page script ran", False, r.stdout[-600:])
    check(f"{page} page: the host rebooted twice, once on its own account after the install and once by hand, and ignored one request", reboots() == ["rebooted", "ignored", "rebooted"]
          and len(scheduled()) == 3 and boot_id() != BOOT0, (reboots(), scheduled()))
    check(f"{page} page: the reboot that did not happen and the one that could not start sent one Telegram message each, those that worked none",
          reboot_failures() == [reboot_text(E_SAME_BOOT), reboot_text(E_AUTO_APT)], reboot_failures())
    check(f"{page} page: the Messages list has one entry for each start, end and failure, in order: the automatic reboot, the one the host ignored, the one that worked, "
          "the one that could not start", trail_is(R_AUTO, R_DONE, R_START, R_SAME_BOOT, R_START, R_DONE, R_AUTO_APT), trail_raw())


STORIES = [("sandbox", story_sandbox), ("security-push", story_security_push), ("security-message", story_security_message), ("scan-and-alerts", story_scan_and_alerts), ("rate-limit", story_rate_limit), ("schedule", story_schedule),
           ("install", story_install), ("install-guard", story_install_guard), ("install-removals", story_install_removals), ("install-failure", story_install_failure), ("ssh-drop", story_ssh_drop),
           ("restart-mid-job", story_restart_mid_job), ("unit-unknown-to-fleet", story_unit_unknown_to_fleet), ("reboot-button", story_reboot), ("reboot-refusals", story_reboot_refusals),
           ("reboot-no-answer", story_reboot_no_answer), ("reboot-unsure-restart", story_reboot_unsure_restart), ("reboot-mid-start-restart", story_reboot_mid_start_restart), ("reboot-scan-race", story_reboot_scan_race),
           ("reboot-never", story_reboot_never), ("reboot-giveup", story_reboot_giveup), ("reboot-restart", story_reboot_restart), ("reboot-after-install", story_reboot_after_install),
           ("reboot-after-install-busy", story_reboot_after_install_busy), ("no-auto-reboot", story_no_auto_reboot), ("install-without-flag", story_install_without_flag),
           ("page-desktop", lambda: story_pages("desktop")), ("page-phone", lambda: story_pages("phone"))]


def main():
    global PORT, GATE_PORT
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))
    PORT, GATE_PORT = free_port(), free_port()
    print(f"e2e: sandbox {SBX}, Fleet on 127.0.0.1:{PORT}, gate stand-in on 127.0.0.1:{GATE_PORT}", flush=True)
    try:
        build_sandbox()
        for name, fn in STORIES:
            if only in name:
                print(f"-- {name}", flush=True)
                t0 = time.time()
                try:
                    fn()
                except Abort as e:
                    check(f"{name}: the story could go on", False, e)
                except Exception as e:
                    check(f"{name}: the story ran without an error", False, f"{type(e).__name__}: {e}")
                print(f"   ({time.time() - t0:.0f} s)", flush=True)
        if results:
            note_reach()
            check("nothing reached out: curl never ran, and ssh was refused for every host but the made-up one", REACH["curl"] == 0,
                  REACH["curl"])
            print(f"   (Fleet's other workers asked ssh for {len(REACH['refused'])} other host(s) and were refused)")
            asked = lines(GATE_LOG)
            check("the gate was asked for nothing but the key check and the model lists, by GET, in the whole run", all(l.split()[0] == "GET" and l.split()[1] in ("/starfleet/check", "/v1/models", "/running")
                  for l in asked), sorted({l for l in asked if l.split()[1] not in ("/starfleet/check", "/v1/models", "/running") or l.split()[0] != "GET"}))
            bad = tracebacks(FLEET_LOG.read_text() if FLEET_LOG.exists() else "")
            check("Fleet's own log has no traceback", not bad, bad[:1])
    finally:
        teardown()
    bad = results.count(False)
    print(f"{len(results) - bad} of {len(results)} checks passed" + (f", {bad} FAILED" if bad else ""), flush=True)
    return 1 if bad or not results else 0


if __name__ == "__main__":
    sys.exit(main())
