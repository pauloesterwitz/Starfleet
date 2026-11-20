"""Install updates on the Starfleet Cluster: what NVIDIA Sync's (DGX Dashboard's) Update button does, for both Sparks.

NVIDIA's button calls the dashboard's root service (UpdateAndReboot), which: refreshes the apt lists, upgrades the
packages, refreshes fwupd metadata and installs firmware if there is any, writes a package snapshot, then always
reboots. That service only answers its own dashboard user (even root is refused), so Fleet runs the same steps
itself, as root through sudo, in a transient systemd unit per node (it survives a Fleet restart). The apt commands
are NVIDIA's own (nvidia-spark-run-apt-upgrade-once.sh: `apt-get -y update`, `apt-get -y full-upgrade`).

One difference, by request: a node is restarted only when it needs it (/run/reboot-required, a kernel, NVIDIA driver
or firmware change). Kathryn restarts first, Jean-Luc last, because Fleet itself runs on Jean-Luc. Starting needs
the Starfleet key. State lives in STATE_FILE so a run (and its result) survives Fleet's own restart."""
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid

STATE_FILE = os.path.expanduser("~/.local/share/fleet-ui/cluster-update.json")
LOG = "/var/log/starfleet-cluster-update.log"
UNIT = "starfleet-cluster-update-"     # + run id
POLL = 5                                # seconds between status reads while a run is going
RUN_MAX = 3 * 3600                      # a run that takes longer is reported as stuck
PENDING_TTL = 300                       # seconds the "updates pending" probe is reused
# changed packages that only take effect after a restart (besides /run/reboot-required)
REBOOT_PKG = r"^(linux-|nvidia-|libnvidia-|cuda-drivers|.*-dkms$|.*firmware|systemd$|libc6$|dbus$)"

# Runs as root (systemd-run). RUN = run id, DRY=1 changes nothing: no apt-get update, a simulated upgrade, no
# firmware refresh or install. Markers carry the run id, so lines of an older run in the shared log never count.
SCRIPT = r'''
m(){ echo "STARFLEET-UPDATE-$1 $RUN ${*:2}"; }
m BEGIN "dry=$DRY host=$(hostname)"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
m STEP lists
if [ "$DRY" = 1 ]; then echo "dry run: apt lists not refreshed"
else apt-get -y update || { m EXIT 10; exit 10; }; fi
before=$(dpkg-query -W -f='${Package}=${Version}\n' | sort)
m STEP packages
if [ "$DRY" = 1 ]; then
  changed=$(apt-get -s full-upgrade | awk '/^Inst /{print $2}')
else
  apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade || { m EXIT 11; exit 11; }
  changed=$(comm -13 <(echo "$before") <(dpkg-query -W -f='${Package}=${Version}\n' | sort) | cut -d= -f1)
fi
n=$(printf '%s\n' "$changed" | grep -c .)
[ "$n" -gt 0 ] && m CHANGED "$(printf '%s\n' "$changed" | head -40 | tr '\n' ' ')"
m STEP firmware
fw=0
[ "$DRY" = 1 ] || fwupdmgr refresh --force >/dev/null 2>&1 || true
if fwupdmgr get-updates --json 2>/dev/null | python3 -c 'import json,sys
d=json.load(sys.stdin); sys.exit(0 if any(x.get("Releases") for x in d.get("Devices", [])) else 1)' 2>/dev/null; then
  if [ "$DRY" = 1 ]; then echo "firmware update available"; fw=1
  else fwupdmgr update -y --no-reboot-check && fw=1; fi
fi
need=0; why=""
[ -e /run/reboot-required ] && { need=1; why="$why reboot-required"; }
printf '%s\n' "$changed" | grep -Eq "$REBOOT_PKG" && { need=1; why="$why driver-or-kernel"; }
[ "$fw" = 1 ] && { need=1; why="$why firmware"; }
why=$(echo $why | tr ' ' ',')
m RESULT "changed=$n reboot=$need firmware=$fw why=$why"
m EXIT 0
'''

_lock = threading.Lock()
_pend = {"at": 0.0, "nodes": {}, "busy": False}
_cfg = {"nodes": {}, "key_check": None, "busy": None, "reboot": None}


def init(nodes, key_check, busy, reboot):
    """nodes: {name: ssh host or None (this host)}; key_check(key) raises on a bad key; busy() lists model loads in
    flight; reboot(host, delay, unit) schedules a graceful reboot. Resumes a run that Fleet's restart interrupted."""
    _cfg.update(nodes=nodes, key_check=key_check, busy=busy, reboot=reboot)
    st = _load()
    if st.get("phase") == "updating":
        threading.Thread(target=_watch, args=(st["run"],), daemon=True).start()
    elif st.get("phase") == "rebooting":          # Fleet came back: the restart it scheduled has happened
        st.update(phase="done", finished=st.get("finished") or time.time())
        st["note"] = "Restarted. " + (st.get("note") or "")
        _save(st)


def _load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"phase": "idle"}


def _save(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def _sh(host, script, timeout=60):
    cmd = ["bash", "-c", script] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------- what is pending (read-only, no root)

def _probe_pending():
    out = {}
    for node, host in _cfg["nodes"].items():
        try:
            r = _sh(host, "apt list --upgradable 2>/dev/null | grep -c upgradable; test -e /run/reboot-required && echo R || echo -",
                    timeout=40)
            f = r.stdout.split()
            out[node] = {"pending": int(f[0]) if f and f[0].isdigit() else None, "reboot_required": "R" in f,
                         "error": None if r.returncode in (0, 1) and f else (r.stderr or "probe failed").strip()[-160:]}
        except (OSError, subprocess.SubprocessError) as e:
            out[node] = {"pending": None, "reboot_required": False, "error": str(e)[:160]}
    _pend.update(at=time.time(), nodes=out, busy=False)


def status():
    """Run state plus, per node, how many packages apt lists as upgradable (as of its last list refresh) and whether
    a restart is already pending. The pending probe runs in the background; a stale answer is served meanwhile."""
    with _lock:
        if time.time() - _pend["at"] > PENDING_TTL and not _pend["busy"]:
            _pend["busy"] = True
            threading.Thread(target=_probe_pending, daemon=True).start()
    return {"state": _load(), "pending": _pend["nodes"], "pending_at": _pend["at"] or None}


# ---------------------------------------------------------------- a run

def start(key, dry=False):
    """Begin a run on every node (needs the Starfleet key). Refused while a run is going or a model is loading."""
    _cfg["key_check"](key)
    return _start(dry)


def _start(dry=False):
    with _lock:
        st = _load()
        if st.get("phase") in ("updating", "rebooting"):
            raise RuntimeError("An update is already running on the Starfleet Cluster.")
        loading = _cfg["busy"]() if _cfg["busy"] else []
        if loading:
            raise RuntimeError("A model is loading (" + ", ".join(loading)[:120] + "). Install updates after it finished.")
        run = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        st = {"phase": "updating", "run": run, "dry": bool(dry), "started": time.time(), "finished": None, "note": "",
              "nodes": {n: {"state": "starting", "step": None, "changed": None, "reboot": None, "firmware": None,
                            "why": "", "exit": None, "tail": [], "packages": ""} for n in _cfg["nodes"]}}
        _save(st)
    errs = {}
    for node, host in _cfg["nodes"].items():
        cmd = (f"sudo -n systemd-run --quiet --collect --unit={UNIT}{run} "
               f"-p StandardOutput=append:{LOG} -p StandardError=append:{LOG} "
               f"--setenv=RUN={run} --setenv=DRY={1 if dry else 0} --setenv=REBOOT_PKG={shlex.quote(REBOOT_PKG)} "
               f"/bin/bash -c {shlex.quote(SCRIPT)}")
        try:
            r = _sh(host, cmd, timeout=40)
            if r.returncode != 0:
                errs[node] = (r.stderr or f"systemd-run exit {r.returncode}").strip()[-200:]
        except (OSError, subprocess.SubprocessError) as e:
            errs[node] = str(e)[:200]
    if errs:
        st = _load()
        for node, e in errs.items():
            st["nodes"][node].update(state="failed", exit=-1, tail=[e])
        _save(st)
    threading.Thread(target=_watch, args=(run,), daemon=True).start()
    return {"ok": True, "run": run, "dry": bool(dry), "errors": errs}


def parse(run, text):
    """Read one node's markers for this run from the shared log."""
    r = {"step": None, "changed": None, "reboot": None, "firmware": None, "why": "", "exit": None, "packages": "",
         "tail": []}
    seen = False
    for line in text.splitlines():
        m = re.match(r"STARFLEET-UPDATE-(\w+) (\S+) ?(.*)$", line)
        if m and m.group(2) == run:
            seen = True
            k, v = m.group(1), m.group(3)
            if k == "STEP":
                r["step"] = v
            elif k == "CHANGED":
                r["packages"] = v.strip()
            elif k == "RESULT":
                kv = dict(x.split("=", 1) for x in v.split() if "=" in x)
                r.update(changed=int(kv.get("changed", 0)), reboot=kv.get("reboot") == "1",
                         firmware=kv.get("firmware") == "1", why=kv.get("why", ""))
            elif k == "EXIT":
                r["exit"] = int(v) if v.strip().lstrip("-").isdigit() else -1
        elif seen and not m and line.strip():
            r["tail"] = (r["tail"] + [line[-200:]])[-6:]
    return r if seen else None


def _watch(run):
    """Follow a run until every node finished, then restart the nodes that need it: Kathryn first, Jean-Luc last."""
    t0 = time.time()
    while True:
        st = _load()
        if st.get("run") != run or st.get("phase") != "updating":
            return
        for node, host in _cfg["nodes"].items():
            n = st["nodes"][node]
            if n["state"] in ("done", "failed"):
                continue
            try:
                r = _sh(host, f"systemctl is-active {UNIT}{run}.service; sudo -n tail -n 600 {LOG}", timeout=40)
            except (OSError, subprocess.SubprocessError) as e:
                n["tail"] = [f"cannot read status: {str(e)[:120]}"]
                continue
            lines = r.stdout.splitlines()
            active = lines[0].strip() if lines else ""
            p = parse(run, "\n".join(lines[1:]))
            if p:
                n.update({k: v for k, v in p.items() if v is not None})
            if active in ("active", "activating", "reloading"):
                n["state"] = "running"
            elif p and p["exit"] == 0:
                n["state"] = "done"
            elif p and p["exit"] is not None:
                n["state"] = "failed"
            elif active in ("inactive", "failed", "unknown", ""):
                if time.time() - t0 > 60:            # the unit is gone and left no result
                    n.update(state="failed", exit=-1)
                    n["tail"] = (n["tail"] or []) + ["the update unit ended without a result"]
        if time.time() - t0 > RUN_MAX:
            for n in st["nodes"].values():
                if n["state"] not in ("done", "failed"):
                    n.update(state="failed", exit=-1, tail=(n["tail"] or []) + ["no result after 3 hours"])
        if all(n["state"] in ("done", "failed") for n in st["nodes"].values()):
            _finish(st)
            return
        _save(st)
        time.sleep(POLL)


def _finish(st):
    st["finished"] = time.time()
    nodes = st["nodes"]
    need = [n for n in nodes if nodes[n]["state"] == "done" and nodes[n]["reboot"]]
    failed = [n for n in nodes if nodes[n]["state"] == "failed"]
    if st.get("dry"):
        st.update(phase="done", note="Dry run: nothing was changed. " +
                  (f"Would restart {', '.join(need)}." if need else "No restart would be needed."))
        _save(st)
        return
    if not need:
        st.update(phase="failed" if failed else "done",
                  note=("Failed on " + ", ".join(failed) + ". " if failed else "") + "No restart needed.")
        _save(st)
        return
    st.update(phase="rebooting", note=("Failed on " + ", ".join(failed) + ". " if failed else "") +
              "Restarting " + ", ".join(need) + ".")
    _save(st)
    order = sorted(need, key=lambda n: _cfg["nodes"][n] is None)      # remote nodes first, this host (Fleet) last
    try:
        for i, node in enumerate(order):
            _cfg["reboot"](_cfg["nodes"][node], 3 if _cfg["nodes"][node] is not None else 20, f"{UNIT}reboot-{st['run']}")
    except (OSError, subprocess.SubprocessError) as e:
        st.update(phase="failed", note=st["note"] + f" Restart could not be scheduled: {str(e)[:160]}")
        _save(st)
        return
    if None not in [_cfg["nodes"][n] for n in need]:     # Fleet stays up: wait for the remote nodes to answer again
        time.sleep(90)
        for _ in range(120):
            if all(_sh(_cfg["nodes"][n], "true", timeout=15).returncode == 0 for n in need):
                break
            time.sleep(10)
        st = _load()
        st.update(phase="done", note="Restarted " + ", ".join(need) + ".")
        _save(st)


if __name__ == "__main__":       # self-check: python3 clusterupdate.py
    log = ("STARFLEET-UPDATE-BEGIN old dry=0\nSTARFLEET-UPDATE-EXIT old 0\n"
           "STARFLEET-UPDATE-BEGIN r1 dry=1 host=x\nSTARFLEET-UPDATE-STEP r1 packages\nsome apt line\n"
           "STARFLEET-UPDATE-CHANGED r1 linux-image-6 nvidia-driver\n"
           "STARFLEET-UPDATE-RESULT r1 changed=2 reboot=1 firmware=0 why=driver-or-kernel\nSTARFLEET-UPDATE-EXIT r1 0\n")
    p = parse("r1", log)
    assert p["exit"] == 0 and p["changed"] == 2 and p["reboot"] and p["why"] == "driver-or-kernel", p
    assert p["packages"] == "linux-image-6 nvidia-driver" and p["tail"] == ["some apt line"], p
    assert parse("r2", log) is None, "a run without markers is not seen"
    assert parse("old", log)["exit"] == 0
    for pkg, need in (("linux-image-6.17", True), ("nvidia-driver-580-open", True), ("libnvidia-gl-580", True),
                      ("nvidia-dkms-580", True), ("curl", False), ("python3-requests", False)):
        assert bool(re.match(REBOOT_PKG, pkg)) == need, pkg
    print("clusterupdate self-check ok")
