#!/usr/bin/env python3
"""End to end test of the Defiant updates: the real fleet.py (started by fleet_e2e.py with short intervals) against a
made-up Defiant, then the real desktop and phone pages in WebKit against that same server.

Real: fleet.py, the scripts it sends over ssh (scan, poll, install unit, probe), the HTTP API, both pages.
Made up: the host. fake-bin/ssh runs those scripts in a sandbox with stand-ins for sudo, apt-get, apt, systemctl,
systemd-run and docker (the apt-sim.txt, job.conf and flag files in the sandbox's host folder are what a step
changes); telegram.py records the messages; gate.py answers the key check. Fleet runs with a sandbox HOME and a
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
SCAN, INSTALL = "/api/defiant/updates/scan", "/api/defiant/updates/install"
HINT = "Fleet: apt would remove packages, nothing was changed. Rescanning now: check the list, then install again."
SAFE_TOOLS = ("awk basename bash cat cp cut date dirname env grep head ip nproc ps rm sed setsid sh sleep stat tail "
              "timeout touch tr uname").split()
STAND_INS = "ssh sudo apt-get apt systemctl systemd-run docker hostname df curl".split()      # everything that could change a machine or reach out

SBX = Path(tempfile.mkdtemp(prefix="defiant-e2e-"))
HOST, HOME, STATE, SAFE = SBX / "host", SBX / "home", SBX / "state" / "defiant-updates.json", SBX / "safe-bin"
TG_LOG, TG_FAIL, GATE_LOG, FLEET_LOG = SBX / "telegram.jsonl", SBX / "telegram-fail", SBX / "gate.log", SBX / "fleet.log"
EV_DIR, PUSH_LOG, PUSH_DEV, APNS_KEY = SBX / "events", SBX / "push.jsonl", SBX / "push-devices.json", SBX / "fixture-apns-key.txt"
PATH = f"{FAKE}:{SAFE}"
DEFAULTS = dict(FIRST=2, GAP=2, RETRY=3600, EVERY=3600, LOST=20, DEF_TTL=1)      # seconds; a story overrides what it needs
UPD, procs, results = dict(DEFAULTS), {}, []
EV = {}      # the events worker's waits (EV_GRACE, EV_HEAVY, EV_REMIND_S, in seconds) when a story shortens them; empty: Fleet's own


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
    r = subprocess.run([str(FAKE / "ssh"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "backup.example", "echo reached"],
                       env={"PATH": PATH, "E2E_HOST": str(HOST)}, capture_output=True, text=True)
    check("sandbox: the ssh stand-in refuses every host but the made-up one", r.returncode == 255 and "reached" not in r.stdout, (r.returncode, r.stdout))


def kill_unit():
    """Stops a unit the fake systemd-run left running, if that pid still is the unit's script (never a recycled pid)."""
    try:
        pid = int((HOST / "unit.pid").read_text())
        if b"FLEET-APT" in Path(f"/proc/{pid}/cmdline").read_bytes():
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
    sim("quiet")
    job()
    for f in (TG_LOG, TG_FAIL, STATE, PUSH_LOG, PUSH_DEV):
        f.unlink(missing_ok=True)


def sim(name):
    shutil.copy(SIMS[name], HOST / "apt-sim.txt")


def sim_without(name, *drop):
    """The sim with the named packages gone: what is left after some of them got installed on the host."""
    out = [l for l in SIMS[name].read_text().splitlines() if not re.match(r"(Inst|Conf) (%s) " % "|".join(drop), l)]
    n = sum(1 for l in out if l.startswith("Inst "))
    (HOST / "apt-sim.txt").write_text(re.sub(r"^\d+ upgraded, 0 newly", f"{n} upgraded, 0 newly", "\n".join(out) + "\n", flags=re.M))


def job(rc=0, delay=3, per=2, reboot=0, after=None):
    """What the next install does: how it ends, how long it takes, lines a second, a reboot left pending, a new sim afterwards."""
    (HOST / "job.conf").write_text(f"RC={rc}\nDELAY={delay}\nPER={per}\nREBOOT={reboot}\n")
    (HOST / "apt-sim-after.txt").unlink(missing_ok=True)
    if after:
        shutil.copy(SIMS[after], HOST / "apt-sim-after.txt")


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
           **{"E2E_" + k: str(v) for k, v in EV.items()}}
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


def install(key=KEY):
    return call("POST", INSTALL, key)


def tracebacks(log):
    """The tracebacks in Fleet's log, except a browser that went away mid-answer (a page reload cuts requests short)."""
    found = [b for b in log.split("Traceback (most recent call last):")[1:]]
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
          "scan_at": s["at"], "job_state": "idle"} and "3 security updates pending" in d["problems"] and d["status"] == "warn", d)
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

    job(rc=0, delay=16, per=2, reboot=1, after="quiet")
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
    check("the install ends done, exit 0, 20 installed, a reboot pending", j["state"] == "done" and j["exit"] == 0 and j["installed"] == 20
          and j["reboot_required"] is True and j["lost"] is False and not any(l for _, _, l in logs), j)
    check("the job ends after it started and its log ends with the exit line", j["ended_at"] >= j["started_at"]
          and j["log"][-1].startswith("FLEET-APT-EXIT 0 "), (j["started_at"], j["ended_at"], j["log"][-2:]))
    u = must(until(lambda: (v := view()) and v["scan"]["at"] > v["job"]["ended_at"] and not v["scan"]["busy"] and v, 30), "no rescan after the install")
    s = u["scan"]
    check("a rescan follows at once and reads the reboot flag", s["count"] == 0 and s["reboot_required"] is True
          and s["reboot_pkgs"] == ["linux-image-6.8.0-99-generic", "libc6"] and u["critical"] is True, s)
    m = until(lambda: len(telegram()) == alerts + 1 and telegram()[-1], 15)
    check("the pending reboot is alerted once, the install success is not", m and m.split("\n")[0] == "Defiant: reboot required"
          and "Reboot required: linux-image-6.8.0-99-generic, libc6" in m, telegram()[alerts:])
    time.sleep(1.1)
    d = call("GET", "/api/defiant")[1]
    check("/api/defiant shows the reboot and the finished job", d["reboot_required"] is True and d["upd"]["job_state"] == "done"
          and d["upd"]["count"] == 0, d)

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
    job(rc=0, delay=16, per=2)
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
    check("a rescan follows the resumed job", until(lambda: view()["scan"] and view()["scan"]["at"] > u["job"]["ended_at"], 30), view()["scan"])


def story_unit_unknown_to_fleet():
    reset(FIRST=3600)
    job(rc=0, delay=14, per=2)
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
    check("a rescan follows it", until(lambda: view()["scan"] and view()["scan"]["at"] > u["job"]["ended_at"], 30), view()["scan"])


def story_pages(page):
    reset(FIRST=3600)
    start_fleet()
    env = dict(os.environ, E2E_BASE=f"http://127.0.0.1:{PORT}", E2E_HOST=str(HOST), E2E_SIMS=json.dumps({k: str(v) for k, v in SIMS.items()}),
               OUT=os.environ.get("OUT", str(HERE / "out")))
    r = subprocess.run(["node", str(HERE / "ui.js"), page], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
    for line in r.stdout.splitlines():
        if line.startswith(("ok   ", "FAIL ")):
            results.append(line.startswith("ok   "))
        print(line, flush=True)
    if r.returncode and "FAIL " not in r.stdout:
        check(f"{page} page script ran", False, r.stdout[-600:])


STORIES = [("sandbox", story_sandbox), ("security-push", story_security_push), ("security-message", story_security_message), ("scan-and-alerts", story_scan_and_alerts), ("rate-limit", story_rate_limit), ("schedule", story_schedule),
           ("install", story_install), ("install-guard", story_install_guard), ("install-removals", story_install_removals), ("install-failure", story_install_failure), ("ssh-drop", story_ssh_drop),
           ("restart-mid-job", story_restart_mid_job), ("unit-unknown-to-fleet", story_unit_unknown_to_fleet), ("page-desktop", lambda: story_pages("desktop")),
           ("page-phone", lambda: story_pages("phone"))]


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
            bad = tracebacks(FLEET_LOG.read_text() if FLEET_LOG.exists() else "")
            check("Fleet's own log has no traceback", not bad, bad[:1])
    finally:
        teardown()
    bad = results.count(False)
    print(f"{len(results) - bad} of {len(results)} checks passed" + (f", {bad} FAILED" if bad else ""), flush=True)
    return 1 if bad or not results else 0


if __name__ == "__main__":
    sys.exit(main())
