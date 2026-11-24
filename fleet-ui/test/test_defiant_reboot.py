#!/usr/bin/env python3
"""Tests for the Defiant reboot in fleet.py: the Reboot Defiant button (POST /api/defiant/reboot), the reboot that an install
starts by itself when it ended well, needs one and was asked for by a page that announced it, the hold that survives a restart
of Fleet, and the entries a reboot leaves in the Messages list.

Plain asserts, standard library only:   python3 test/test_defiant_reboot.py

Nothing here touches a real host, a real chat, llama-swap or a model, and nothing here can reboot anything: ssh, the Telegram
sender, the Messages list and the Starfleet key check are replaced, the made-up host is defiant.example, and the key is the
string fixture-key. The two shell scripts only ever run against stand-in programs that record their arguments and execute
nothing. run_script checks, before it starts a script, that sudo, systemctl, cat, systemd-run, fuser, reboot, shutdown,
poweroff, halt and kexec resolve to the stand-ins in its own folder, so a missing stand-in fails the run instead of reaching a
real program. Any real subprocess call that slips through fails the run.

The helpers (fleet_state, done, probe, scan_text, ...) come from test_defiant_updates.py, which holds the tests of the update
scan and the install this feature builds on.
"""
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import urllib.error
from contextlib import contextmanager, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_defiant_updates as U                      # noqa: E402  (its helpers; its own tests are not run from here)

fleet = U.fleet
done, fleet_state, lock_is_free, running_job, probe, scan_text, fixture, S, P = (
    U.done, U.fleet_state, U.lock_is_free, U.running_job, U.probe, U.scan_text, U.fixture, U.S, U.P)
REAL_AUTH, REAL_TG, REAL_AUDIT = fleet.computer_auth, fleet._tg_send, fleet.ev_audit      # the fail-closed key check, the Telegram sender and the Messages list, before the tests replace them

BOOT_A = "3f2a9c1e-5b7d-4e86-9a0c-1d2e3f4a5b6c"       # the boot that is running when the reboot is asked for
BOOT_B = "a1b2c3d4-0e1f-4a2b-8c3d-4e5f6a7b8c9d"       # the boot after it
VIEW_KEYS = {"state", "started_at", "ended_at", "down_at", "back_after", "error", "auto"}      # the boot id stays inside
DID_NOT_ACCEPT = "Defiant did not accept the reboot request."
UNSURE = "No answer from Defiant. It may still reboot."
UNSURE_AUTO = "The reboot after the install got no answer from Defiant. It may still reboot: wait a few minutes before rebooting it again."
GAP_TEXT = "A reboot was requested a moment ago. Try again in a few minutes."
APT_BUSY = "apt is busy on Defiant, an upgrade may be running. Try again in a few minutes."
GIVEUP_ERROR = "Defiant did not answer again within 15 minutes. Check it in your hosting provider's console."
NO_REBOOT_ERROR = ("Defiant is still on the same boot three minutes after the request. It may still reboot. "
                   "If it does not, check it in your hosting provider's console.")
NO_SUCH_THING = "no ssh and no scan was expected here"
ENTRY_GO = "#/systems/defiant"                        # where a Messages list entry of a reboot leads
NO_ARG = object()                                     # click_install: leave the reboot_ok argument out altogether
EM = chr(0x2014)                                      # the one character the new texts must not carry


# ---------------------------------------------------------------- helpers

def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def rb(**kw):
    """A reboot state: idle, with the given fields."""
    return dict(fleet._upd_reboot_idle(), **kw)


def rb_running(started=1000.0, boot=BOOT_A, **kw):
    return rb(state="running", started_at=started, boot=boot, **kw)


def rb_done(started=1000.0, back_after=60, **kw):
    return rb(state="done", started_at=started, ended_at=started + back_after, back_after=back_after, boot=BOOT_A, **kw)


def rb_failed(started=1000.0, error=NO_REBOOT_ERROR, **kw):
    return rb(state="failed", started_at=started, ended_at=started + 180, error=error, boot=BOOT_A, **kw)


def start_out(boot=BOOT_A, rc=0):
    """What the start script prints when Defiant accepted the reboot (the systemd-run line is its own words)."""
    return "FLEET-BOOT %s\nRunning as unit: fleet-reboot.timer; fleet-reboot.service\nFLEET-RC:%d\n" % (boot, rc)


def poll_out(boot=BOOT_A, up="12.34"):
    return "BOOT:%s\nUP:%s\n" % (boot, up)


def outcome(key="fixture-key"):
    """What POST /api/defiant/reboot answers: (200, result) or (code, text), the exceptions mapped the way Handler.do_POST maps
    them (test_http_route runs the real handler)."""
    try:
        return 200, fleet.defiant_updates_reboot(key)
    except urllib.error.HTTPError as e:
        return 403, fleet._err_text(e)
    except PermissionError as e:
        return 409, str(e)
    except ValueError as e:
        return 400, str(e)
    except (urllib.error.URLError, OSError, subprocess.SubprocessError, RuntimeError) as e:
        return 502, str(e)[:200]


def telegram(sent, ok=True):
    return lambda text: (sent.append(text), ok)[1]


class Host:
    """A made-up Defiant. It answers the fixed scripts by what they are and runs nothing. down: ssh fails, as while the machine
    reboots. A test plays a reboot by changing boot and down."""

    def __init__(self):
        self.boot, self.down, self.calls, self.lock_free, self.starting, self.log = BOOT_A, False, [], [], [], io.StringIO()
        self.on_disk = []                             # the saved job state at the moment a reboot was asked for
        self.asked = []                               # (the hold in memory, the saved file as text or None) at the moment a reboot was asked for
        self.install_reply = None                    # None: the unit starts cleanly; else the answer (or the exception) of the install start
        self.start_reply = None                       # None: accept the reboot; else the stdout, or the exception, that answers it
        self.flag = "yes"                             # what the install poll says about /var/run/reboot-required
        self.exit = 0                                 # how the install ends
        self.probe = probe("active")
        self.scan = scan_text(fixture("apt-sim-phased.txt"))

    def __call__(self, script, timeout, connect=8):
        self.calls.append((script, timeout, connect))
        self.lock_free.append(lock_is_free())
        if self.down:
            return done("", "ssh: connect to host defiant.example port 22: Connection refused", 255)
        if script == fleet.REBOOT_START:
            self.starting.append(fleet._UPD["reboot_starting"])                       # the guard that keeps a scan, an install and a second reboot out
            self.asked.append((fleet._UPD["reboot_unsure"], read(fleet.UPD_FILE) if os.path.exists(fleet.UPD_FILE) else None))
            if self.asked[-1][1] is not None:
                self.on_disk.append(json.loads(self.asked[-1][1])["job"]["state"])
            if isinstance(self.start_reply, BaseException):
                raise self.start_reply
            return done(start_out(self.boot) if self.start_reply is None else self.start_reply)
        if script == fleet.REBOOT_POLL:
            return done(poll_out(self.boot))
        if script == fleet.UPD_POLL:
            return done(self.probe)
        if script == fleet.UPD_SCAN:
            return done(self.scan)
        if script in (fleet.upd_start(True), fleet.upd_start(False)):
            if isinstance(self.install_reply, BaseException):
                raise self.install_reply
            return self.install_reply or done("Running as unit: fleet-apt-upgrade.service\nFLEET-RC:0\n")
        raise AssertionError("an unexpected script: %r" % script[:60])

    def scripts(self):
        names = {fleet.REBOOT_START: "REBOOT_START", fleet.REBOOT_POLL: "REBOOT_POLL", fleet.UPD_POLL: "UPD_POLL", fleet.UPD_SCAN: "UPD_SCAN"}
        return [names.get(c[0], "install start") for c in self.calls]

    def end_install(self, t0):
        """The unit that began at t0 ends, as the host is set to say, 90 seconds later."""
        self.probe = probe("inactive", "2", "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", self.flag,
                           ["FLEET-APT-START %d" % t0, "FLEET-APT-EXIT %d %d" % (self.exit, t0 + 90)])

    def click_install(self, reboot_ok=True):
        """The keyed click on Install updates (by default from a page that announces the reboot; NO_ARG leaves the argument out); the unit
        then ends 90 seconds after it began."""
        try:
            t0 = fleet.defiant_updates_install("fixture-key", **({} if reboot_ok is NO_ARG else {"reboot_ok": reboot_ok}))["started_at"]
        except (PermissionError, OSError):                                                      # answered with an error, and the unit is followed all the same
            t0 = fleet._UPD["job"]["started_at"]
        self.end_install(t0)
        return t0


@contextmanager
def story(**state):
    """A clean state with the made-up host, a Telegram recorder and a quiet stdout in place; yields (host, sent). The keyword
    arguments are put into the state."""
    host, sent = Host(), []
    with fleet_state(), mock.patch.object(fleet, "_def_ssh", host), mock.patch.object(fleet, "_tg_send", telegram(sent)), redirect_stdout(host.log):
        fleet._UPD.update(state)
        yield host, sent


# ---------------------------------------------------------------- import, constants, scripts

def test_import_has_no_side_effects():
    assert U.THREADS_AT_IMPORT == 1, "importing fleet started a thread"
    assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and fleet._UPD["reboot_starting"] is False
    assert fleet._UPD["reboot"] == {"state": "idle", "started_at": None, "ended_at": None, "down_at": None, "back_after": None,
                                    "error": None, "boot": None, "auto": False}


def test_constants():
    assert (fleet.REBOOT_POLL_S, fleet.REBOOT_START_WAIT, fleet.REBOOT_GIVEUP, fleet.REBOOT_GAP, fleet.REBOOT_SHOW) == (5, 180, 900, 600, 600)
    assert fleet.REBOOT_UNIT == "fleet-reboot"


START_TEXT = '''if systemctl is-active --quiet fleet-apt-upgrade; then echo "FLEET-BUSY install"; exit 0; fi
if systemctl is-active --quiet fleet-reboot.timer || systemctl is-active --quiet fleet-reboot.service; then echo "FLEET-BUSY reboot"; exit 0; fi
sudo -n fuser -s /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock 2>/dev/null; r=$?
if [ $r -eq 0 ]; then echo "FLEET-BUSY apt"; exit 0; fi
if [ $r -ne 1 ] || ! sudo -n fuser -V >/dev/null 2>&1; then echo "FLEET-NOCHECK fuser $r"; exit 0; fi
echo "FLEET-BOOT $(cat /proc/sys/kernel/random/boot_id)"
sudo -n systemd-run --unit=fleet-reboot --on-active=3s --timer-property=AccuracySec=1s --collect /bin/systemctl reboot 2>&1
echo FLEET-RC:$?'''


def test_script_texts_are_fixed():
    s = fleet.REBOOT_START
    assert s == START_TEXT                                                    # the exact text of the start script
    assert s.count("systemctl reboot") == 1 and s.count("systemd-run") == 1 and "--on-active=3s" in s and "--timer-property=AccuracySec=1s" in s and "--collect" in s
    assert not re.search(r"--no-block|shutdown|poweroff|halt|kexec|reboot -f|--force|--ignore-inhibitors|\sreboot\s*$", s, re.M)   # the clean systemctl reboot, nothing else
    assert not re.search("[{}%" + EM + "]", s)                                # a fixed text: nothing is filled in per request
    lines = s.splitlines()                                                    # the refusals come before anything is touched
    assert [l.split('echo "')[1].split('"')[0] for l in lines[:2] + lines[3:5]] == ["FLEET-BUSY install", "FLEET-BUSY reboot", "FLEET-BUSY apt", "FLEET-NOCHECK fuser $r"]
    assert all("systemd-run" not in l for l in lines[:6])
    assert lines[2].startswith("sudo -n fuser -s ") and lines[2].endswith("2>/dev/null; r=$?") and "[ $r -eq 0 ]" in lines[3] and "[ $r -ne 1 ]" in lines[4]   # only a 1 is "none": a fuser that cannot run is no answer
    p = fleet.REBOOT_POLL
    assert 'echo "BOOT:$(cat /proc/sys/kernel/random/boot_id)"' in p and 'echo "UP:$(cut -d\' \' -f1 /proc/uptime)"' in p
    assert not re.search(r"sudo|systemctl|systemd-run|reboot|kill|\brm\b|>", p) and EM not in p       # a poll changes nothing


def test_scripts_parse_as_shell():
    for name, script in (("REBOOT_START", fleet.REBOOT_START), ("REBOOT_POLL", fleet.REBOOT_POLL)):
        for sh in (["/bin/sh", "-n"], ["bash", "-n"]):
            try:
                r = U.real_run(sh + ["-c", script], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
            except OSError:
                continue
            assert r.returncode == 0, (name, sh, r.stderr)


def test_ssh_calls_of_the_reboot():
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return done(poll_out() if argv[-1] == fleet.REBOOT_POLL else start_out())
    with fleet_state(), mock.patch.object(fleet.subprocess, "run", run):
        fleet._UPD["reboot"] = rb_running(1000.0)
        fleet._upd_reboot_poll(1010.0)                                         # the poll: connect in 5 s, the whole call within 12
        fleet._UPD["reboot"] = fleet._upd_reboot_idle()
        with redirect_stdout(io.StringIO()):
            assert outcome()[0] == 200                                         # the start: the whole call within 30
    (poll, pkw), (start, skw) = seen
    assert poll == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "defiant.example", fleet.REBOOT_POLL]
    assert start == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "defiant.example", fleet.REBOOT_START]
    for kw, timeout in ((pkw, 12), (skw, 30)):
        assert kw["timeout"] == timeout and kw["stdin"] == subprocess.DEVNULL and kw["capture_output"] and kw["errors"] == "replace"


# ---------------------------------------------------------------- the two scripts, run against stand-ins

def stand_ins(d):
    """A folder of stand-in programs, each of which records what it was called with and runs nothing. It is put first on PATH. sudo in
    particular never runs its arguments: it answers `fuser -s` from FAKE_FUSER, `fuser -V` from FAKE_FUSER_V (0: it runs; 1: sudo cannot
    run it, which sudo answers with a 1 like a fuser that found no holder) and systemd-run from FAKE_RUN_RC, and everything that could
    stop or reboot a machine is a recorder that writes to danger.log. Returns the folder."""
    b = os.path.join(d, "bin")
    os.makedirs(b)

    def prog(name, body):
        path = os.path.join(b, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
    prog("systemctl", 'if [ "$1" = is-active ] && [ "$2" = --quiet ]; then case " $FAKE_ACTIVE " in *" $3 "*) exit 0;; esac; exit 3; fi\n'
                      'echo "systemctl $*" >> "$FAKE_DIR/danger.log"; exit 99')
    prog("sudo", 'echo "$*" >> "$FAKE_DIR/sudo.log"\n[ "$1" = -n ] || exit 98\n'
                 'case "$2" in\n fuser) [ "$3" = -V ] && exit "${FAKE_FUSER_V:-0}"; exit "${FAKE_FUSER:-1}";;\n'
                 ' systemd-run) echo "Running as unit: fleet-reboot.timer; fleet-reboot.service"; exit "${FAKE_RUN_RC:-0}";;\nesac\nexit 97')
    prog("cat", 'if [ "$1" = /proc/sys/kernel/random/boot_id ]; then echo "$FAKE_BOOT"; else exec /bin/cat "$@"; fi')
    for name in ("reboot", "shutdown", "poweroff", "halt", "kexec", "systemd-run", "fuser"):
        prog(name, 'echo "%s $*" >> "$FAKE_DIR/danger.log"; exit 99' % name)
    return b


def run_script(script, shell="bash", **env):
    """Run one of the scripts in a local shell against the stand-ins. Returns (exit code, stdout lines, sudo.log lines, danger.log lines)."""
    shell = shutil.which(shell) if shell != "/bin/sh" else shell
    with tempfile.TemporaryDirectory(prefix="fleet-reboot-") as d:
        b = stand_ins(d)
        e = {"PATH": b + ":/usr/bin:/bin", "FAKE_DIR": d, "FAKE_BOOT": BOOT_A, "FAKE_ACTIVE": "", "HOME": d}
        e.update({k: str(v) for k, v in env.items()})
        for tool in ("sudo", "systemctl", "cat", "reboot", "shutdown", "poweroff", "halt", "kexec", "systemd-run", "fuser"):     # nothing real can be reached
            r = U.real_run([shell, "-c", "command -v " + tool], capture_output=True, text=True, timeout=10, env=e, stdin=subprocess.DEVNULL)
            assert r.stdout.strip() == os.path.join(b, tool), (tool, r.stdout)
        r = U.real_run([shell, "-c", script], capture_output=True, text=True, timeout=30, env=e, stdin=subprocess.DEVNULL)
        log = lambda n: read(os.path.join(d, n)).splitlines() if os.path.exists(os.path.join(d, n)) else []
        return r.returncode, r.stdout.splitlines(), log("sudo.log"), log("danger.log")


FUSER = "-n fuser -s /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock"
FUSER_V = "-n fuser -V"
RUN = "-n systemd-run --unit=fleet-reboot --on-active=3s --timer-property=AccuracySec=1s --collect /bin/systemctl reboot"


def test_start_script_against_stand_ins():
    if not U.have_bash():
        return
    for shell in ("bash", "/bin/sh"):
        # idle: the boot id, the transient timer, the exit code
        rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell)
        assert rc == 0 and out == ["FLEET-BOOT " + BOOT_A, "Running as unit: fleet-reboot.timer; fleet-reboot.service", "FLEET-RC:0"], (shell, out)
        assert sudo == [FUSER, FUSER_V, RUN] and danger == []                 # the lock check and its control, then a timer for a plain systemctl reboot
        assert fleet.parse_reboot_probe("BOOT:" + out[0].split()[1]) == {"boot": BOOT_A, "up": None}
        # systemd-run fails: the exit code says so, and the caller refuses it
        rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell, FAKE_RUN_RC=1)
        assert out[-1] == "FLEET-RC:1" and danger == []
        # the three refusals, in this order, each before anything is touched
        rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell, FAKE_ACTIVE="fleet-apt-upgrade fleet-reboot.timer", FAKE_FUSER=0)
        assert (rc, out, sudo, danger) == (0, ["FLEET-BUSY install"], [], [])
        for unit in ("fleet-reboot.timer", "fleet-reboot.service"):
            rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell, FAKE_ACTIVE=unit, FAKE_FUSER=0)
            assert (rc, out, sudo, danger) == (0, ["FLEET-BUSY reboot"], [], []), unit
        rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell, FAKE_ACTIVE="something-else.service", FAKE_FUSER=0)
        assert (rc, out, sudo, danger) == (0, ["FLEET-BUSY apt"], [FUSER], [])             # a dpkg or apt lock is held: the timer is never asked for
        # the lock check fails closed: a 1 is "no holder" only while sudo can run fuser at all (sudo answers 1 when it cannot), and no other
        # answer is one: nothing is scheduled, and the line says that the check could not be made
        rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell, FAKE_FUSER=1, FAKE_FUSER_V=1)
        assert (rc, out, sudo, danger) == (0, ["FLEET-NOCHECK fuser 1"], [FUSER, FUSER_V], []), shell
        for code in (2, 126, 127, 255):                                                     # fuser or sudo failed in some other way
            rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell, FAKE_FUSER=code)
            assert (rc, out, sudo, danger) == (0, ["FLEET-NOCHECK fuser %d" % code], [FUSER], []), (shell, code)
        for code in (0, 1):                                                                 # a real answer, with a working fuser: the lock decides
            rc, out, sudo, danger = run_script(fleet.REBOOT_START, shell, FAKE_FUSER=code)
            assert out[0] == ("FLEET-BUSY apt" if code == 0 else "FLEET-BOOT " + BOOT_A) and not any("systemctl" in d for d in danger), (shell, code)
        for out_line in ("FLEET-BUSY install", "FLEET-BUSY reboot", "FLEET-BUSY apt"):      # what the caller makes of each
            with fleet_state(), mock.patch.object(fleet, "_def_ssh", lambda s, t, c=8, o=out_line: done(o + "\n")):
                assert outcome()[0] == 409
        for out_line in ("FLEET-NOCHECK fuser 1", "FLEET-NOCHECK fuser 127"):               # a check that could not be made is a refusal that says nothing more: 502
            with fleet_state(), redirect_stdout(io.StringIO()), mock.patch.object(fleet, "_def_ssh", lambda s, t, c=8, o=out_line: done(o + "\n")):
                assert outcome() == (502, DID_NOT_ACCEPT) and fleet._UPD["reboot"] == fleet._upd_reboot_idle() and fleet._UPD["reboot_unsure"] == 0.0


def test_poll_script_against_stand_ins():
    if not U.have_bash() or not os.path.exists("/proc/uptime"):
        return
    for shell in ("bash", "/bin/sh"):
        rc, out, sudo, danger = run_script(fleet.REBOOT_POLL, shell)
        obs = fleet.parse_reboot_probe("\n".join(out))
        assert rc == 0 and obs["boot"] == BOOT_A and isinstance(obs["up"], float) and obs["up"] >= 0, (shell, out)
        assert sudo == [] and danger == []


# ---------------------------------------------------------------- the boot id and the poll's answer

def test_boot_id_pattern():
    for good in (BOOT_A, BOOT_B, "00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"):
        assert fleet._BOOT_ID.fullmatch(good), good
    for bad in ("", " " + BOOT_A, BOOT_A + " ", BOOT_A + "\n", BOOT_A.upper(), BOOT_A[:-1], BOOT_A + "0", BOOT_A.replace("-", ""),
                BOOT_A.replace("a", "g", 1), BOOT_A[:8] + "_" + BOOT_A[9:], "\u0663" + BOOT_A[1:], BOOT_A + "\x00", "x" * 36):
        assert not fleet._BOOT_ID.fullmatch(bad), repr(bad)


def test_parse_reboot_probe():
    p = fleet.parse_reboot_probe
    assert p(poll_out(BOOT_A, "12.34")) == {"boot": BOOT_A, "up": 12.34}
    assert p("UP:5\nBOOT:%s\n" % BOOT_A) == {"boot": BOOT_A, "up": 5.0}                     # the order does not matter
    assert p("BOOT:%s\r\nUP:7.5\r\n" % BOOT_A) == {"boot": BOOT_A, "up": 7.5}
    assert p("a banner\nBOOT: %s\nUP:abc\n" % BOOT_A) == {"boot": BOOT_A, "up": None}       # a bad uptime is none; the boot id still counts
    assert p("BOOT:%s\nBOOT:%s\n" % (BOOT_A, BOOT_B))["boot"] == BOOT_A                     # the first one counts
    for up, value in (("0", 0.0), ("12", 12.0), ("12.3", 12.3), ("12.345", 12.345), ("123456789", 123456789.0), ("  7.25  ", 7.25)):
        assert p(poll_out(BOOT_A, up))["up"] == value, up
    for up in ("-5", "1e5", "NaN", "inf", "1.2345", "1234567890", "5.", ".5", "1,5", "12 s", "0x10", "\u0661\u0662", "1.2.3", ""):       # a number, or none
        assert p(poll_out(BOOT_A, up)) == {"boot": BOOT_A, "up": None}, up
    assert p("UP:5\nUP:6\nBOOT:%s\n" % BOOT_A)["up"] == 5.0                                # the first one counts here too
    for text in ("", "UP:12\n", "BOOT:\n", "BOOT:not-a-boot-id\n", "BOOT:%s\n" % BOOT_A.upper(), "BOOT:%s extra\n" % BOOT_A,
                 "BOOT:%s:x\n" % BOOT_A, "boot:%s\n" % BOOT_A, " BOOT:%s\n" % BOOT_A, "BOOT" + BOOT_A, "BOOT:" + "a" * 10 ** 6,
                 "\x00\x01garbage\xff", "x\n" * 100000):
        assert p(text) is None, repr(text[:40])
    assert p("x\n" * 100000 + poll_out()) == {"boot": BOOT_A, "up": 12.34}


# ---------------------------------------------------------------- reboot_step

def test_reboot_step_only_a_running_reboot_moves():
    for st in (rb(), rb_done(), rb_failed()):
        for obs in (None, {"boot": BOOT_B, "up": 1.0}, {"boot": BOOT_A, "up": 1.0}, {"boot": "garbage", "up": 1.0}):
            assert fleet.reboot_step(st, obs, 99999.0) == st, (st["state"], obs)


def test_reboot_step_is_pure():
    st = rb_running(1000.0)
    before = dict(st)
    for obs, now in ((None, 5000.0), (None, 1010.0), ({"boot": BOOT_B, "up": 1.0}, 1100.0), ({"boot": BOOT_A, "up": 1.0}, 1500.0)):
        r = fleet.reboot_step(st, obs, now)
        assert r is not st and st == before


def test_reboot_step_no_answer_and_the_give_up_rule():
    st = rb_running(1000.0)
    a = fleet.reboot_step(st, None, 1010.0)
    assert a == dict(st, down_at=1010.0)                                                  # the first time it did not answer
    assert fleet.reboot_step(a, None, 1020.0) == a and fleet.reboot_step(a, None, 1500.0)["down_at"] == 1010.0      # set once
    assert fleet.reboot_step(a, None, 1000.0 + fleet.REBOOT_GIVEUP)["state"] == "running"         # exactly 900 s: still waiting ("more than")
    f = fleet.reboot_step(a, None, 1000.0 + fleet.REBOOT_GIVEUP + 0.5)
    assert f == dict(a, state="failed", ended_at=1900.5, error=GIVEUP_ERROR) and f["back_after"] is None
    e = fleet.reboot_step(st, None, 5000.0)                                                # Fleet was away: down and given up in one step
    assert e == dict(st, state="failed", down_at=5000.0, ended_at=5000.0, error=GIVEUP_ERROR)


def test_reboot_step_a_new_boot_is_done():
    st = rb_running(1000.0, down_at=1010.0)
    d = fleet.reboot_step(st, {"boot": BOOT_B, "up": 20.0}, 1061.4)
    assert d == dict(st, state="done", ended_at=1061.4, back_after=61) and isinstance(d["back_after"], int)
    assert fleet.reboot_step(st, {"boot": BOOT_B, "up": 20.0}, 1061.6)["back_after"] == 62
    assert fleet.reboot_step(rb_running(1000.0), {"boot": BOOT_B, "up": 20.0}, 1030.0)["down_at"] is None      # back before Fleet saw it down
    late = fleet.reboot_step(rb_running(1000.0), {"boot": BOOT_B, "up": 20.0}, 1000.0 + 5000)                 # a new boot is done however late
    assert late["state"] == "done" and late["back_after"] == 5000 and late["error"] is None
    assert fleet.reboot_step(st, {"boot": BOOT_B}, 1061.4)["state"] == "done"               # the uptime is not needed


def test_reboot_step_the_same_boot_is_a_reboot_that_did_not_start():
    st = rb_running(1000.0)
    same = {"boot": BOOT_A, "up": 3000.0}
    assert fleet.reboot_step(st, same, 1000.0) == st and fleet.reboot_step(st, same, 1100.0) == st
    assert fleet.reboot_step(st, same, 1000.0 + fleet.REBOOT_START_WAIT) == st             # exactly 180 s: still waiting ("more than")
    f = fleet.reboot_step(st, same, 1000.0 + fleet.REBOOT_START_WAIT + 0.5)
    assert f == dict(st, state="failed", ended_at=1180.5, error=NO_REBOOT_ERROR) and f["back_after"] is None
    down = dict(st, down_at=1050.0)                                                        # a dropped connection is not a reboot
    assert fleet.reboot_step(down, same, 1100.0) == down
    g = fleet.reboot_step(down, same, 1181.0)
    assert g["state"] == "failed" and g["error"] == NO_REBOOT_ERROR and g["down_at"] == 1050.0 and g["ended_at"] == 1181.0


def test_reboot_step_a_bad_boot_id_is_no_answer():
    st = rb_running(1000.0)
    for obs in ({"boot": "garbage", "up": 1.0}, {"boot": None, "up": 1.0}, {"boot": 5}, {"boot": BOOT_B.upper()}, {"boot": BOOT_B + "\n"},
                {"boot": BOOT_B[:-1]}, {"boot": [BOOT_B]}, {"up": 3}, {}, "text", 7, [], [BOOT_B], (BOOT_B,)):
        r = fleet.reboot_step(st, obs, 1010.0)
        assert r["state"] == "running" and r["down_at"] == 1010.0 and r["boot"] == BOOT_A and r["back_after"] is None, obs     # not a new boot, not done
        r = fleet.reboot_step(st, obs, 1000.0 + fleet.REBOOT_GIVEUP + 1)
        assert r["state"] == "failed" and r["error"] == GIVEUP_ERROR, obs


def test_reboot_step_a_clock_that_went_back_gives_no_negative_time():
    st = rb_running(5000.0)                                                               # started "in the future": the clock was stepped back since
    d = fleet.reboot_step(st, {"boot": BOOT_B, "up": 5.0}, 4000.0)
    assert d["state"] == "done" and d["back_after"] == 0 and d["ended_at"] == 4000.0       # never "answered again after -1000 s"
    assert fleet.reboot_step(st, {"boot": BOOT_A, "up": 5.0}, 4000.0) == st                # waiting, as for a reboot that has just been asked for
    a = fleet.reboot_step(st, None, 4000.0)
    assert a == dict(st, down_at=4000.0)
    assert fleet.reboot_step(a, None, 5000.0 + fleet.REBOOT_GIVEUP + 1)["state"] == "failed"      # the limits count from the request once the clock is past it


# ---------------------------------------------------------------- the Telegram text

def test_the_state_machine_texts_are_the_exact_ones():
    """The two errors the state machine writes, word for word: what the page shows and Telegram sends. The same-boot one says that the
    reboot may still happen (a slow shutdown can end after the verdict); the 15 minute one carries the advice to look at the console."""
    gone = fleet.reboot_step(rb_running(1000.0), None, 1000.0 + 901)
    same = fleet.reboot_step(rb_running(1000.0), {"boot": BOOT_A, "up": 3000.0}, 1000.0 + 181)
    assert gone["error"] == "Defiant did not answer again within 15 minutes. Check it in your hosting provider's console."
    assert same["error"] == ("Defiant is still on the same boot three minutes after the request. It may still reboot. "
                             "If it does not, check it in your hosting provider's console.")
    assert (GIVEUP_ERROR, NO_REBOOT_ERROR) == (gone["error"], same["error"])
    for text in (gone["error"], same["error"]):
        assert "\n" not in text and EM not in text and "did not reboot" not in text and "Check Defiant" not in text


def test_reboot_fail_text():
    t = fleet.reboot_fail_text(rb_failed(error=NO_REBOOT_ERROR))
    assert t == "Defiant: reboot failed\n\n" + NO_REBOOT_ERROR                              # the headline, a blank line and the error: nothing else
    lines = t.splitlines()
    assert lines == ["Defiant: reboot failed", "", NO_REBOOT_ERROR] and len(lines[0]) <= 100
    assert fleet.reboot_fail_text(rb_failed(error=GIVEUP_ERROR)) == "Defiant: reboot failed\n\n" + GIVEUP_ERROR
    assert "Check Defiant in your hosting provider's console." not in t and not t.endswith("\n")       # the old trailing advice line is gone
    assert not re.search("[*_`<>" + EM + "]", t)                                              # plain text
    for error in ("x" * 9000, "x" * 9000 + "\n" + "y" * 9000, "line one\nline two\n\n\tline three", "a\x1b[31m\x00b\x7fc"):
        t = fleet.reboot_fail_text(rb_failed(error=error))
        lines = t.splitlines()
        assert len(t) < 3500 and lines[0] == "Defiant: reboot failed" and lines[1] == "" and len(lines) == 3, repr(error)
        assert len(lines[2]) <= 300 and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", t), repr(error)       # the error is one line, cleaned and capped
    assert fleet.reboot_fail_text(rb_failed(error="x" * 9000)).splitlines()[2] == "x" * 300
    assert fleet.reboot_fail_text(rb_failed(error="line one\nline two")).splitlines()[2] == "line one line two"
    for error in (None, ""):                                                                  # a failed reboot always has an error; if one did not, nothing else is added
        assert fleet.reboot_fail_text(rb_failed(error=error)) == "Defiant: reboot failed\n\n"
    for st in (fleet.reboot_step(rb_running(1000.0), None, 5000.0), fleet.reboot_step(rb_running(1000.0), {"boot": BOOT_A}, 5000.0)):
        assert fleet.reboot_fail_text(st) == "Defiant: reboot failed\n\n" + st["error"]         # the texts the state machine writes fit the message whole


# ---------------------------------------------------------------- the route function

def test_route_checks_the_key_before_anything():
    with fleet_state():
        ssh = mock.Mock(side_effect=AssertionError("no ssh without a valid key"))
        with mock.patch.object(fleet, "_def_ssh", ssh):
            for bad in (None, "", "wrong"):
                assert outcome(bad) == (403, fleet.POLICY), bad
            with mock.patch.object(fleet, "DEFIANT_HOST", ""):                              # the key comes before "not configured" ...
                assert outcome("wrong") == (403, fleet.POLICY) and outcome(None) == (403, fleet.POLICY)
            for state in ({"reboot": rb_running(1000.0)}, {"job": running_job(1000.0)}, {"busy": True}, {"reboot_starting": True},
                          {"reboot": rb_done(time.time() - 10)}):                           # ... and before every other refusal
                fleet._UPD.update(state)
                assert outcome("wrong") == (403, fleet.POLICY), state
                fleet._UPD.update(fleet._upd_fresh())
        assert ssh.call_count == 0 and not fleet._UPD_WAKE.is_set() and fleet._UPD["reboot_starting"] is False
        assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and not os.path.exists(fleet.UPD_FILE)


def test_route_uses_the_fail_closed_key_check():
    """computer_auth, not the fail-open key_check: no key, a key the gate refuses, a gate that does not answer or answers anything but
    204 all end in a 403 and nothing is asked of Defiant. fleet_state makes key_check raise if the route ever used it."""
    class Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    for behaviour, expect in ((urllib.error.URLError("down"), "Cannot verify"), (OSError("refused"), "Cannot verify"),
                              (urllib.error.HTTPError("x", 403, "no", {}, io.BytesIO(b"{}")), fleet.POLICY), (Resp(200), "Cannot verify"),
                              (Resp(500), "Cannot verify"), (urllib.error.HTTPError("x", 500, "no", {}, io.BytesIO(b"{}")), "Cannot verify")):
        with fleet_state():
            seen = []

            def gate(req, timeout=None, behaviour=behaviour):
                seen.append((req.full_url, req.get_header("X-starfleet-key"), timeout))
                if isinstance(behaviour, BaseException):
                    raise behaviour
                return behaviour
            with mock.patch.object(fleet, "computer_auth", REAL_AUTH), mock.patch.object(fleet.urllib.request, "urlopen", gate), \
                    mock.patch.object(fleet, "_def_ssh", mock.Mock(side_effect=AssertionError("no ssh"))):
                code, text = outcome("fixture-key")
                assert code == 403 and expect in text, (behaviour, code, text)
                assert seen and seen[0][0] == fleet.SWAP + "/starfleet/check" and seen[0][1] == "fixture-key" and seen[0][2] == 5
                assert outcome(None) == (403, fleet.POLICY) and len(seen) == 1             # no key: the gate is not even asked
            assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and fleet._UPD["reboot_starting"] is False
    with fleet_state():                                                                    # the gate says 204: the request goes on
        with mock.patch.object(fleet, "computer_auth", REAL_AUTH), mock.patch.object(fleet.urllib.request, "urlopen", lambda req, timeout=None: Resp(204)), \
                mock.patch.object(fleet, "_def_ssh", lambda s, t, c=8: done(start_out())), redirect_stdout(io.StringIO()):
            assert outcome("fixture-key")[0] == 200


def test_route_refusals_come_in_this_order_and_start_nothing():
    T0 = 5000.0
    with fleet_state():
        ssh = mock.Mock(side_effect=AssertionError("a refused request asked Defiant"))
        st = fleet._UPD
        with mock.patch.object(fleet, "_def_ssh", ssh), mock.patch.object(fleet.time, "time", lambda: T0):
            with mock.patch.object(fleet, "DEFIANT_HOST", ""):
                assert outcome() == (400, "Defiant is not configured.")
            st["reboot"], st["job"], st["busy"] = rb_running(T0 - 10), running_job(T0 - 5), True       # every reason at once: the first one answers
            assert outcome() == (409, "A reboot is already running.")
            st["reboot"] = rb_done(T0 - 100)
            assert outcome() == (409, "An install is running. Wait until it is done.")
            st["job"], st["starting"] = fleet._upd_job_idle(), True                                       # an install on its way counts as running
            assert outcome() == (409, "An install is running. Wait until it is done.")
            st["starting"] = False
            assert outcome() == (409, "A scan is running. Try again in a moment.")
            st["busy"] = False
            assert outcome() == (409, "A reboot was requested a moment ago. Try again in a few minutes.")
            st["reboot"], st["reboot_starting"] = fleet._upd_reboot_idle(), True                         # a start of the button or of an install, on its way
            assert outcome() == (409, "A reboot is already running.")
            st["reboot_starting"] = False
            assert ssh.call_count == 0 and not fleet._UPD_WAKE.is_set() and not os.path.exists(fleet.UPD_FILE)
            assert st["reboot"] == fleet._upd_reboot_idle() and st["reboot_starting"] is False and st["job"]["state"] == "idle"


def test_route_the_gap_counts_from_the_last_request_that_may_have_taken_effect():
    """A reboot that went through, one that failed (a slow shutdown can end after the verdict, so the machine may reboot a moment later all the
    same) and a start that got no answer hold the button for REBOOT_GAP from the request. A request that nothing came of does not."""
    T0 = 5000.0
    with fleet_state():
        st = fleet._UPD
        host = lambda s, t, c=8: done(start_out())
        with mock.patch.object(fleet, "_def_ssh", host), mock.patch.object(fleet.time, "time", lambda: T0), redirect_stdout(io.StringIO()):
            for make in (rb_done, rb_failed):
                for age, code in ((0, 409), (1, 409), (599.5, 409), (600, 200), (601, 200), (86400, 200), (-50, 200)):       # a clock that went back locks nothing
                    st["reboot"] = make(T0 - age)
                    assert outcome()[0] == code, (make.__name__, age)
                    if code == 409:
                        assert st["reboot"] == make(T0 - age) and st["reboot_starting"] is False
            st["reboot"] = rb_failed(T0 - 181)                                                              # the verdict came at 181 s: still held until 600 s
            assert outcome() == (409, GAP_TEXT)
            st["reboot"] = fleet._upd_reboot_idle()
            for kind in (rb(state="failed", ended_at=T0 - 5, error="The reboot after the install did not start: x. Reboot Defiant when apt is idle.", auto=True), rb()):
                st["reboot"] = kind
                assert outcome()[0] == 200, kind["state"]                                                   # nothing was asked of Defiant: nothing to wait for
            for age, code in ((0, 409), (599.5, 409), (600, 200), (-50, 200)):                              # a start that got no answer
                st["reboot"], st["reboot_unsure"] = fleet._upd_reboot_idle(), T0 - age
                assert outcome()[0] == code, age
            st["reboot"], st["reboot_unsure"] = rb_done(T0 - 100), T0 - 5000                                # an old mark does not hide a recent reboot, and the other way round
            assert outcome() == (409, GAP_TEXT)
            st["reboot"], st["reboot_unsure"] = rb_done(T0 - 5000), T0 - 100
            assert outcome() == (409, GAP_TEXT)


def test_route_remote_refusals_and_failures_leave_the_state_alone():
    busy = [("FLEET-BUSY install\n", "An install is running. Wait until it is done."), ("FLEET-BUSY reboot\n", "A reboot is already running."),
            ("FLEET-BUSY apt\n", APT_BUSY), ("FLEET-BUSY apt\r\n", APT_BUSY)]
    bad = ["", "ssh said nothing useful", "FLEET-RC:0\n", start_out(rc=1), start_out(rc=255), "FLEET-BOOT %s\nFLEET-RC:00\n" % BOOT_A,
           "FLEET-BOOT %s\nFLEET-RC:10\n" % BOOT_A, "FLEET-BOOT garbage\nFLEET-RC:0\n", "FLEET-BOOT %s\nFLEET-RC:0\n" % BOOT_A.upper(),
           "FLEET-BOOT %s extra\nFLEET-RC:0\n" % BOOT_A, "FLEET-BOOT\nFLEET-RC:0\n", "x FLEET-BOOT %s\nFLEET-RC:0\n" % BOOT_A,
           "FLEET-BUSY nothing\n" + start_out(rc=1), "NOT-FLEET-BUSY apt\n", "FLEET-NOCHECK fuser 1\n", "FLEET-NOCHECK fuser 127\n"]
    cut = ["FLEET-BOOT %s\n" % BOOT_A, "FLEET-BOOT %s\nx FLEET-RC:0\n" % BOOT_A, "FLEET-BOOT %s\nFLEET-RC:\n" % BOOT_A,           # the boot id came and the end did not:
           "Running as unit: fleet-reboot.timer\nFLEET-BOOT %s\nRunning as unit: fleet-reboot.timer\n" % BOOT_A]                 # the timer may be set all the same
    boom = [(done("", "ssh: connect to host defiant.example port 22: Connection refused", 255), DID_NOT_ACCEPT),
            (done("", "Permission denied (publickey).", 255), DID_NOT_ACCEPT), (OSError("boom defiant.example"), DID_NOT_ACCEPT),
            (subprocess.SubprocessError("boom"), DID_NOT_ACCEPT), (FileNotFoundError("ssh"), DID_NOT_ACCEPT),
            (subprocess.TimeoutExpired(["ssh", "-o", "BatchMode=yes", "defiant.example", fleet.REBOOT_START], 30), UNSURE)]
    cases = [(done(o), 409, text) for o, text in busy] + [(done(o), 502, DID_NOT_ACCEPT) for o in bad] + [(done(o, "", 255), 502, UNSURE) for o in cut] + [(r, 502, text) for r, text in boom]
    previous = rb_failed(1000.0)
    for reply, code, text in cases:
        with fleet_state():
            fleet._UPD["reboot"] = previous

            def ssh(s, t, c=8, reply=reply):
                if isinstance(reply, BaseException):
                    raise reply
                return reply
            log = io.StringIO()
            with mock.patch.object(fleet, "_def_ssh", ssh), redirect_stdout(log), mock.patch.object(fleet.time, "time", lambda: 10 ** 6):
                assert outcome() == (code, text), (reply, outcome())
            assert fleet._UPD["reboot"] is previous and fleet._UPD["reboot_starting"] is False, reply        # as it was; the guard is lifted again
            assert fleet._UPD["reboot_unsure"] == (10 ** 6 if text == UNSURE else 0.0), reply                # only a request that said nothing leaves a mark
            assert not fleet._UPD_WAKE.is_set() and U.AUDIT == [], reply                                      # nothing started: no wake-up, no entry in the Messages list
            with open(fleet.UPD_FILE) as f:                                                                   # the mark is written before Defiant is asked, so the file is there: it keeps the mark
                saved = json.load(f)                                                                          # for a request that said nothing, and the old hold (none) is put back for the rest
            assert saved["reboot_unsure"] == (10 ** 6 if text == UNSURE else 0.0) and saved["reboot"] == previous, reply
            if code == 502:                                                                                   # no argv, no host, no script in what the page gets ...
                assert not re.search(r"defiant\.example|BatchMode|systemd|sudo|ssh:|FLEET|Connection|Permission", text), text
            for line in log.getvalue().splitlines():                                                          # ... and what the journal gets is one capped line
                assert len(line) < 300 and line.startswith("defiant updates: reboot not accepted: "), line
    # the shape of the refusal does not depend on what else the host printed around the markers
    with fleet_state(), redirect_stdout(io.StringIO()):
        noisy = "Welcome to Ubuntu\n" + start_out() + "\nsome trailing words\n"
        with mock.patch.object(fleet, "_def_ssh", lambda s, t, c=8: done(noisy)):
            assert outcome()[0] == 200 and fleet._UPD["reboot"]["boot"] == BOOT_A


def test_route_success_starts_the_reboot():
    with fleet_state():
        seen, before = [], time.time()
        fleet._UPD["reboot"] = rb_done(100.0, auto=True)                                  # an old result: replaced as a whole
        fleet._UPD["job"] = dict(fleet._upd_job_idle(), state="done", started_at=1.0, ended_at=2.0, exit=0, installed=3, reboot_required=True)
        fleet._UPD["scan"] = S([P("a")], reboot=True)
        keep_job, keep_scan = fleet._UPD["job"], fleet._UPD["scan"]

        def ssh(script, timeout, connect=8):
            seen.append((script, timeout, connect, lock_is_free(), fleet._UPD["reboot_starting"], fleet._UPD["reboot"]["state"]))
            return done(start_out())
        log = io.StringIO()
        with mock.patch.object(fleet, "_def_ssh", ssh), redirect_stdout(log):
            code, res = outcome()
        assert log.getvalue() == "DEFIANT reboot scheduled\n"
        assert code == 200 and res["ok"] is True and set(res) == {"ok", "started_at"} and before <= res["started_at"] <= time.time()
        assert seen == [(fleet.REBOOT_START, 30, 8, True, True, "done")]                  # the exact script, 30 s, no lock held, the guard set meanwhile
        r = fleet._UPD["reboot"]
        assert r == {"state": "running", "started_at": res["started_at"], "ended_at": None, "down_at": None, "back_after": None, "error": None,
                     "boot": BOOT_A, "auto": False}
        assert fleet._UPD["reboot_starting"] is False and fleet._UPD_WAKE.is_set()
        assert fleet._UPD["job"] is keep_job and fleet._UPD["scan"] is keep_scan          # nothing else was touched
        with open(fleet.UPD_FILE) as f:
            saved = json.load(f)
        assert saved["reboot"] == r and saved["job"]["state"] == "done" and saved["reboot_unsure"] == 0.0     # saved, so a Fleet restart keeps following it
        assert U.AUDIT == [("Defiant reboot started", "Started with the Reboot Defiant button.", ENTRY_GO, True)]     # one list entry, written with the lock free
        v = fleet.defiant_updates_view()["reboot"]
        assert v == {k: r[k] for k in VIEW_KEYS} and "boot" not in v


def test_route_a_second_request_while_the_start_is_on_its_way():
    with fleet_state():
        inner = []

        def ssh(script, timeout, connect=8):
            inner.append(outcome())
            try:
                fleet.defiant_updates_install("fixture-key")
            except PermissionError as e:
                inner.append(str(e))
            inner.append(fleet.defiant_updates_scan_request())
            return done(start_out())
        with mock.patch.object(fleet, "_def_ssh", ssh), redirect_stdout(io.StringIO()):
            assert outcome()[0] == 200
        assert inner == [(409, "A reboot is already running."), "A reboot is running.",
                         {"ok": True, "started": False, "reason": "rebooting"}]
        assert fleet._UPD["reboot"]["state"] == "running" and fleet._UPD["job"]["state"] == "idle" and fleet._UPD["busy"] is False


def test_install_and_scan_are_refused_while_a_reboot_runs():
    for flag in ("running", "starting"):
        with fleet_state():
            fleet._UPD.update({"running": {"reboot": rb_running(1000.0)}, "starting": {"reboot_starting": True}}[flag])
            ssh = mock.Mock(side_effect=AssertionError("asked Defiant during a reboot"))
            with mock.patch.object(fleet, "_def_ssh", ssh):
                assert fleet.defiant_updates_scan_request() == {"ok": True, "started": False, "reason": "rebooting"}
                assert (fleet._UPD["busy"], fleet._UPD["want"]) == (False, False) and not fleet._UPD_WAKE.is_set()
                for bad in (None, "wrong"):                                              # the key is still checked first
                    try:
                        fleet.defiant_updates_install(bad)
                    except urllib.error.HTTPError as e:
                        assert e.code == 403
                    else:
                        raise AssertionError("a bad key got past the check")
                try:
                    fleet.defiant_updates_install("fixture-key")
                except PermissionError as e:
                    assert str(e) == "A reboot is running."
                else:
                    raise AssertionError("an install started during a reboot")
            assert ssh.call_count == 0 and fleet._UPD["job"]["state"] == "idle" and fleet._UPD["starting"] is False
    for finished in (rb_done(1000.0), rb_failed(1000.0)):                                # a finished reboot blocks nothing
        with fleet_state():
            fleet._UPD["reboot"] = finished
            assert fleet.defiant_updates_scan_request() == {"ok": True, "started": True}
            fleet._UPD["busy"] = False
            with mock.patch.object(fleet, "_def_ssh", lambda s, t, c=8: done("Running as unit: x\nFLEET-RC:0\n")):
                assert fleet.defiant_updates_install("fixture-key")["ok"] is True
            assert fleet._UPD["reboot"] == finished


def test_route_not_configured():
    with fleet_state(), mock.patch.object(fleet, "DEFIANT_HOST", ""), mock.patch.object(fleet, "_def_ssh", mock.Mock(side_effect=AssertionError(NO_SUCH_THING))):
        assert outcome() == (400, "Defiant is not configured.")
        assert fleet.defiant_updates_view() == {"configured": False}


# ---------------------------------------------------------------- views

def test_view_and_summary():
    with fleet_state():
        v = fleet.defiant_updates_view()
        assert set(v) == {"configured", "now", "scan", "critical", "job", "reboot"} and set(v["reboot"]) == VIEW_KEYS
        assert v["reboot"] == {"state": "idle", "started_at": None, "ended_at": None, "down_at": None, "back_after": None, "error": None, "auto": False}
        assert fleet.defiant_updates_summary() is None                                  # until the first good scan, as before
        fleet._UPD["scan"] = S([P("a")], at=77.0)
        assert fleet.defiant_updates_summary()["reboot_state"] == "idle"
        for st in (rb_running(1000.0, down_at=1010.0, auto=True), rb_done(1000.0, auto=True), rb_failed(1000.0),
                   rb(state="failed", ended_at=5.0, error="x", auto=True)):
            fleet._UPD["reboot"] = st
            v = fleet.defiant_updates_view()
            assert set(v["reboot"]) == VIEW_KEYS and "boot" not in v["reboot"] and all(v["reboot"][k] == st[k] for k in VIEW_KEYS)
            assert fleet.defiant_updates_summary()["reboot_state"] == st["state"]
            json.dumps(v, allow_nan=False)
            v["reboot"]["state"] = "tampered"                                            # a copy: the state is not reachable through the view
            assert fleet._UPD["reboot"]["state"] == st["state"]
    with mock.patch.object(fleet, "DEFIANT_HOST", ""):
        assert fleet.defiant_updates_view() == {"configured": False}


def test_defiant_state_carries_the_reboot_state():
    probe_out = ("H:defiant.example\nUP:100.5\nLOAD:0.1 0.2 0.3\nCPUS:2\nOS:Ubuntu\nKERNEL:7.0.0\nMEM:MemTotal:4000000\n"
                 "MEM:MemAvailable:2000000\nDISK:100 50 50\nREBOOT:no\nUPD:5\n")
    with fleet_state(), mock.patch.object(fleet, "_DEF", {"at": 0.0, "data": None}), \
            mock.patch.object(fleet.subprocess, "run", lambda argv, **kw: done(probe_out)):
        fleet._UPD["scan"] = S([P("a")], at=77.0)
        assert fleet.defiant_state(force=True)["upd"]["reboot_state"] == "idle"
        fleet._UPD["reboot"] = rb_running(1000.0)
        d = fleet.defiant_state(force=True)
        assert d["upd"]["reboot_state"] == "running" and d["problems"] == [] and d["status"] == "ok"      # the probe's own words are untouched


# ---------------------------------------------------------------- saved state

def test_persistence_round_trip():
    kinds = [rb_running(1000.0, down_at=1010.0), rb_running(1000.0, auto=True), rb_done(1000.0, 75, down_at=1020.0, auto=True), rb_failed(1000.0, GIVEUP_ERROR, down_at=1010.0),
             rb(state="failed", ended_at=1000.0, error="The reboot after the install did not start: " + APT_BUSY.rstrip(".") + ". Reboot Defiant when apt is idle.", auto=True)]
    for st in kinds:
        with fleet_state():
            scan = S([P("a", security=True)], at=500.0)
            fleet._UPD.update(reboot=st, scan=scan, job=running_job(900.0, contact=950.0), alerted={"sig": "a=2", "at": 1.0}, reboot_unsure=980.25)
            with fleet._UPD_LOCK:
                fleet._upd_save_locked()
            with open(fleet.UPD_FILE) as f:
                saved = json.load(f)
            assert set(saved) == {"scan", "alerted", "job", "reboot", "reboot_unsure"} and saved["reboot"] == st and saved["reboot_unsure"] == 980.25
            assert os.listdir(os.path.dirname(fleet.UPD_FILE)) == ["defiant-updates.json"]
            fleet._UPD.update(fleet._upd_fresh())
            fleet.defiant_updates_load()
            assert fleet._UPD["reboot"] == st, st
            assert fleet._UPD["reboot_unsure"] == 980.25 and isinstance(fleet._UPD["reboot_unsure"], float)
            assert fleet._UPD["scan"] == scan and fleet._UPD["job"]["state"] == "running" and fleet._UPD["alerted"]["sig"] == "a=2"     # the others still load
            assert fleet._UPD["reboot_starting"] is False                                                                           # memory only


def test_load_replaces_a_reboot_that_does_not_look_right_by_idle():
    good = rb_running(1000.0)
    bad = [None, 5, "running", [], [good], {}, {k: v for k, v in good.items() if k != "auto"}, {k: v for k, v in good.items() if k != "boot"},
           {k: v for k, v in good.items() if k != "error"}, rb(state="bogus", started_at=1.0), rb(state="idle", started_at=5.0, boot=BOOT_A), rb(state=None),
           rb(state=["running"], started_at=1.0, boot=BOOT_A), rb(state=5), rb_running(started=None), rb_running(started="yesterday"),
           rb_running(started=float("nan")), rb_running(started=float("inf")), rb_running(started=-1.0), rb_running(started=10 ** 13), rb_running(started=True),
           rb_running(boot=None), rb_running(boot="garbage"), rb_running(boot=BOOT_A.upper()), rb_running(boot=5), rb_running(boot=BOOT_A + "\n"),
           rb_running(down_at="x"), rb_running(down_at=float("nan")), rb_running(down_at=-5), rb_running(ended_at="x"), rb_running(back_after=float("inf")),
           rb_running(error=5), rb_running(error=["x"]), rb_running(auto="yes"), rb_running(auto=1), rb_running(auto=None),
           rb(state="done", started_at=1.0, ended_at=2.0, back_after=None, boot=BOOT_A), rb(state="done", started_at=1.0, ended_at=None, back_after=1, boot=BOOT_A),
           rb(state="done", started_at=None, ended_at=2.0, back_after=1, boot=BOOT_A), rb(state="done", started_at=1.0, ended_at=2.0, back_after="1", boot=BOOT_A),
           rb(state="done", started_at=1.0, ended_at=2.0, back_after=1, boot="x"),
           rb(state="failed", started_at=1.0, ended_at=2.0, error=None), rb(state="failed", started_at=1.0, ended_at=None, error="x"),
           rb(state="failed", ended_at="2", error="x"), rb(state="failed", started_at="1", ended_at=2.0, error="x"), rb(state="failed", ended_at=2.0, error=7)]
    future = time.time() + 10 ** 6                                                               # saved while the clock was far ahead: a reboot that could never give up
    bad += [rb_running(started=future), rb_running(down_at=future), rb_running(ended_at=future), rb_done(started=future), dict(rb_done(time.time() - 1000), ended_at=future),
            rb_failed(started=future), rb(state="failed", ended_at=future, error="x"), rb_running(started=time.time() + 120)]
    for value in bad:
        with fleet_state():
            os.makedirs(os.path.dirname(fleet.UPD_FILE))
            with open(fleet.UPD_FILE, "w") as f:
                json.dump({"scan": S([P("a")]), "alerted": {}, "job": dict(fleet._upd_job_idle(), state="done", log=["x"]), "reboot": value}, f)
            fleet.defiant_updates_load()
            assert fleet._UPD["reboot"] == fleet._upd_reboot_idle(), value                       # ignored: idle
            assert fleet._UPD["scan"] == S([P("a")]) and fleet._UPD["job"]["state"] == "done"      # the rest of the file is not lost with it
            json.dumps(fleet.defiant_updates_view(), allow_nan=False)
            json.dumps(fleet.defiant_updates_summary(), allow_nan=False)
            fleet._UPD["next_at"] = 1e12                                                         # the worker goes on as if there was none
            with mock.patch.object(fleet, "_def_ssh", mock.Mock(side_effect=AssertionError(NO_SUCH_THING))):
                assert fleet._upd_tick(time.time()) >= 1
    with fleet_state():                                                                          # a file from before the reboot has no such key
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": S([P("a")]), "alerted": {}, "job": fleet._upd_job_idle()}, f)
        fleet.defiant_updates_load()
        assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and fleet._UPD["scan"] == S([P("a")])
    with fleet_state():                                                                          # only the known keys come back, the text is cleaned
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": None, "alerted": {}, "job": fleet._upd_job_idle(),
                       "reboot": dict(rb_failed(1000.0, "a\x1b[31m\x00" + "x" * 5000), evil="y" * 1000)}, f)
        fleet.defiant_updates_load()
        r = fleet._UPD["reboot"]
        assert r["state"] == "failed" and set(r) == set(fleet._upd_reboot_idle()) and len(r["error"]) <= 300 and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", r["error"])
    with fleet_state():                                                                          # an empty error stays what it is
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": None, "alerted": {}, "job": fleet._upd_job_idle(), "reboot": rb_done(1000.0)}, f)
        fleet.defiant_updates_load()
        assert fleet._UPD["reboot"] == rb_done(1000.0) and fleet._UPD["reboot"]["error"] is None
    with fleet_state():                                                                          # a clock a few seconds ahead of this one is no "future": the worker follows it
        ahead = rb_running(time.time() + 30, down_at=time.time() + 40)
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": None, "alerted": {}, "job": fleet._upd_job_idle(), "reboot": ahead}, f)
        fleet.defiant_updates_load()
        assert fleet._UPD["reboot"] == ahead


def test_a_reboot_saved_while_the_clock_was_ahead_does_not_lock_the_page_forever():
    """Before the fix such a state loaded as running and could never give up: the install, the scan and the button were refused for good."""
    future = time.time() + 5 * 10 ** 8
    with fleet_state():
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": S([P("a")]), "alerted": {}, "job": fleet._upd_job_idle(), "reboot": rb_running(future)}, f)
        fleet.defiant_updates_load()
        assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and fleet.defiant_updates_view()["reboot"]["state"] == "idle"
        assert fleet.defiant_updates_scan_request() == {"ok": True, "started": True}           # not "rebooting"
        fleet._UPD["busy"] = False
        with mock.patch.object(fleet, "_def_ssh", lambda s, t, c=8: done("Running as unit: x\nFLEET-RC:0\n")):
            assert fleet.defiant_updates_install("fixture-key")["ok"] is True               # not "A reboot is running."


def test_the_hold_after_a_start_that_got_no_answer_survives_a_restart_of_fleet():
    """A start that got no answer (ssh timed out, or the answer was cut off after the boot id) may have scheduled the reboot. Fleet is
    restarted often: a restart inside the next ten minutes must not forget the hold, or a second click schedules a second reboot of a
    production server. The mark is saved by the start itself, so no other save has to come first."""
    T0 = 5000.0
    for reply in (subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30), "FLEET-BOOT %s\n" % BOOT_A):
        with story() as (h, sent), mock.patch.object(fleet.time, "time", lambda: T0):
            h.start_reply = reply
            assert outcome() == (502, UNSURE) and fleet._UPD["reboot_unsure"] == T0
            with open(fleet.UPD_FILE) as f:
                saved = json.load(f)
            assert saved["reboot_unsure"] == T0 and saved["reboot"]["state"] == "idle"          # on disk at once, not with the next save
            fleet._UPD.update(fleet._upd_fresh())                                                # the restart: only the file is left
            fleet.defiant_updates_load()
            assert fleet._UPD["reboot_unsure"] == T0 and fleet._UPD["reboot"] == fleet._upd_reboot_idle()
            h.start_reply, calls = None, len(h.calls)
            for now, code in ((T0, 409), (T0 + 1, 409), (T0 + 300, 409), (T0 + fleet.REBOOT_GAP - 0.5, 409), (T0 + fleet.REBOOT_GAP, 200)):
                with mock.patch.object(fleet.time, "time", lambda now=now: now):
                    res = outcome()
                assert (res == (409, GAP_TEXT)) if code == 409 else (res[0] == 200), (now, res)
                assert len(h.calls) == calls + (code == 200), now                               # a refused click asks nothing of Defiant
            assert fleet._UPD["reboot"]["state"] == "running" and fleet._UPD["reboot_unsure"] == T0
    with story() as (h, sent), mock.patch.object(fleet.time, "time", lambda: T0):                # a start that said something puts the old hold back (the file is back to none)
        h.start_reply = "FLEET-BUSY apt\n"
        assert outcome() == (409, APT_BUSY)
        fleet._UPD.update(fleet._upd_fresh())
        fleet.defiant_updates_load()
        h.start_reply = None
        assert fleet._UPD["reboot_unsure"] == 0.0 and outcome()[0] == 200


def test_the_hold_of_an_automatic_start_that_got_no_answer_survives_a_restart_of_fleet():
    with story() as (h, sent):
        h.start_reply = subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30)
        t0 = h.click_install()
        assert fleet._upd_tick(t0 + 100) == 0 and fleet._UPD["reboot"]["error"] == UNSURE_AUTO and fleet._UPD["reboot_unsure"] == t0 + 100
        fleet._UPD.update(fleet._upd_fresh())                                                    # the restart (the worker's clock is the test's: t0 + 100)
        with mock.patch.object(fleet.time, "time", lambda: t0 + 100):
            fleet.defiant_updates_load()
        assert fleet._UPD["reboot_unsure"] == t0 + 100 and fleet._UPD["reboot"]["state"] == "failed"
        h.start_reply, calls = None, len(h.calls)
        with mock.patch.object(fleet.time, "time", lambda: t0 + 160):
            assert outcome() == (409, GAP_TEXT) and len(h.calls) == calls
        with mock.patch.object(fleet.time, "time", lambda: t0 + 100 + fleet.REBOOT_GAP):
            assert outcome()[0] == 200


def after_a_restart_during_the_call(h, text, t):
    """A restart of Fleet in the middle of the start call (after Defiant may have scheduled the reboot, before the answer): only what the file
    held at the moment Defiant was asked is left. The hold must be in it: a click a moment later asks nothing of Defiant, and the gap, counted
    from the request at t, ends as always."""
    fleet._UPD.update(fleet._upd_fresh())
    with open(fleet.UPD_FILE, "w") as f:
        f.write(text)
    with mock.patch.object(fleet.time, "time", lambda: t):
        fleet.defiant_updates_load()
    assert fleet._UPD["reboot_unsure"] == t and fleet._UPD["reboot"] == fleet._upd_reboot_idle()
    h.start_reply, n = None, len(h.calls)
    with mock.patch.object(fleet.time, "time", lambda: t + 1):
        assert outcome() == (409, GAP_TEXT) and len(h.calls) == n
    with mock.patch.object(fleet.time, "time", lambda: t + fleet.REBOOT_GAP):
        assert outcome()[0] == 200


def test_the_hold_is_written_before_defiant_is_asked():
    """A restart of Fleet can come while the start call is on its way, after Defiant scheduled the reboot and before Fleet got the answer
    (Fleet is deployed often, and a stop runs no cleanup). The hold must be in the file by then, not set when the answer comes. An answer
    that shows that nothing was scheduled puts the previous hold back (none, or one that ran out long ago); a timeout or an answer cut off
    keeps the mark; a start that worked leaves the button to the reboot's own time."""
    T0 = 50000.0
    cases = [("busy", "FLEET-BUSY apt\n", 409, False), ("not accepted", "", 502, False), ("script failed", start_out(rc=1), 502, False),
             ("ssh error", OSError("boom"), 502, False), ("started", None, 200, False),
             ("timeout", subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30), 502, True), ("cut off", "FLEET-BOOT %s\n" % BOOT_A, 502, True)]
    for older in (0.0, T0 - 5000):
        for kind, reply, code, kept in cases:
            with story(reboot_unsure=older) as (h, sent), mock.patch.object(fleet.time, "time", lambda: T0):
                h.start_reply = reply
                assert outcome()[0] == code, kind
                hold, text = h.asked[0]
                assert len(h.asked) == 1 and hold == T0 and json.loads(text)["reboot_unsure"] == T0, kind           # in memory and in the file when Defiant was asked
                after = T0 if kept else older
                assert fleet._UPD["reboot_unsure"] == after and json.loads(read(fleet.UPD_FILE))["reboot_unsure"] == after, kind      # memory and file agree afterwards
                after_a_restart_during_the_call(h, text, T0)


def test_the_hold_is_written_before_the_automatic_start_asks_defiant():
    for reply, kept in (("FLEET-BUSY apt\n", False), (subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30), True)):
        with story() as (h, sent):
            h.start_reply = reply
            t0 = h.click_install()
            assert fleet._upd_tick(t0 + 100) == 0
            hold, text = h.asked[0]
            assert len(h.asked) == 1 and hold == t0 + 100 and json.loads(text)["reboot_unsure"] == t0 + 100 and json.loads(text)["job"]["state"] == "done"
            assert fleet._UPD["reboot_unsure"] == (t0 + 100 if kept else 0.0)
            assert json.loads(read(fleet.UPD_FILE))["reboot_unsure"] == fleet._UPD["reboot_unsure"]
            after_a_restart_during_the_call(h, text, t0 + 100)                                              # the worker's clock is the test's: t0 + 100
            assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["reboot"]["auto"] is False             # the install is done and stays so; the button's start went through


def test_a_save_that_fails_in_an_unexpected_way_strands_no_reboot_flag():
    """reboot_starting keeps the scan, the install and both buttons out while a start is on its way. A save that raised in the middle of
    a start would leave it set until Fleet restarts. The save never raises, so every way out of a start clears the flag and the worker
    carries on."""
    def boom(*a, **kw):
        raise TypeError("not serializable")
    for reply, code, state in ((None, 200, "running"), ("FLEET-BUSY apt\n", 409, "idle"), (subprocess.TimeoutExpired(["ssh"], 30), 502, "idle")):      # the button
        with story() as (h, sent):
            h.start_reply = reply
            with mock.patch.object(fleet.json, "dump", boom):
                assert outcome()[0] == code, reply
            assert fleet._UPD["reboot_starting"] is False and fleet._UPD["reboot"]["state"] == state, reply
            assert len(h.asked) == 1 and h.starting == [True]                                      # Defiant was asked once, with the guard up
    with story() as (h, sent):                                                                   # the reboot after an install
        t0 = h.click_install()
        with mock.patch.object(fleet.json, "dump", boom):
            assert fleet._upd_tick(t0 + 100) == 0
            assert fleet._UPD["reboot_starting"] is False and fleet._UPD["reboot"]["state"] == "running" and fleet._UPD["reboot"]["auto"] is True
            assert fleet._upd_tick(t0 + 101) == fleet.REBOOT_POLL_S                                # the worker follows the reboot: it is not stuck
        assert h.scripts().count("REBOOT_START") == 1 and sent == []


def test_load_takes_the_hold_only_from_a_good_number():
    """The rule of the numbers of a saved reboot: a number (not a bool, NaN or infinite), 0 or more, below 1e12 and not more than a minute
    ahead of this clock (the file was written while the clock was ahead). Anything else, and a file without the key, restores none."""
    NOW = 10 ** 6
    good = [0, 0.0, 1, 5000, 5000.5, NOW - 1, NOW, NOW + 60, NOW + 59.5]
    bad = ["5000", "", "x", "NaN", None, True, False, [], [5000.0], {}, {"a": 1}, -1, -0.5, -10 ** 6, float("nan"), float("inf"), float("-inf"),
           10 ** 12, 10 ** 13, 10 ** 400, 1e300, NOW + 60.5, NOW + 61, NOW + 10 ** 6, 5 * 10 ** 8 + NOW, 999999999999, 10 ** 11 + 0.5]
    for value, expected in [(v, float(v)) for v in good] + [(v, 0.0) for v in bad]:
        with fleet_state(), mock.patch.object(fleet.time, "time", lambda: NOW):
            os.makedirs(os.path.dirname(fleet.UPD_FILE))
            with open(fleet.UPD_FILE, "w") as f:
                json.dump({"scan": S([P("a")]), "alerted": {}, "job": dict(fleet._upd_job_idle(), state="done", log=["x"]), "reboot": rb_done(900.0),
                           "reboot_unsure": value}, f)
            fleet.defiant_updates_load()
            assert fleet._UPD["reboot_unsure"] == expected and isinstance(fleet._UPD["reboot_unsure"], float), repr(value)
            assert fleet._UPD["scan"] == S([P("a")]) and fleet._UPD["job"]["state"] == "done" and fleet._UPD["reboot"] == rb_done(900.0), repr(value)   # the rest loads
            json.dumps(fleet.defiant_updates_view(), allow_nan=False)
    for value, expected in ((5 * 10 ** 11, 5e11), (999999999999, 999999999999.0), (10 ** 12, 0.0), (1.5e12, 0.0), (1e300, 0.0)):       # the bound for the absurd holds on its own, whatever the clock says
        with fleet_state(), mock.patch.object(fleet.time, "time", lambda: 2e12):
            os.makedirs(os.path.dirname(fleet.UPD_FILE))
            with open(fleet.UPD_FILE, "w") as f:
                json.dump({"scan": None, "alerted": {}, "job": fleet._upd_job_idle(), "reboot_unsure": value}, f)
            fleet.defiant_updates_load()
            assert fleet._UPD["reboot_unsure"] == expected, value
    for text in ('{"scan": null, "alerted": {}, "job": 3, "reboot_unsure": 999999}',):          # next to a part of the file that is junk: the hold still counts
        with fleet_state(), mock.patch.object(fleet.time, "time", lambda: NOW):
            os.makedirs(os.path.dirname(fleet.UPD_FILE))
            with open(fleet.UPD_FILE, "w") as f:
                f.write(text)
            fleet.defiant_updates_load()
            assert fleet._UPD["reboot_unsure"] == 999999.0 and fleet._UPD["job"] == fleet._upd_job_idle()
    with fleet_state():                                                                          # a file from before the hold was saved: none
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": S([P("a")]), "alerted": {}, "job": fleet._upd_job_idle(), "reboot": rb_done(900.0)}, f)
        fleet.defiant_updates_load()
        assert fleet._UPD["reboot_unsure"] == 0.0 and fleet._UPD["scan"] == S([P("a")]) and fleet._UPD["reboot"] == rb_done(900.0)
    with fleet_state():                                                                          # a hold from the future, however far, never locks the button
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": None, "alerted": {}, "job": fleet._upd_job_idle(), "reboot_unsure": time.time() + 10 ** 6}, f)
        fleet.defiant_updates_load()
        with mock.patch.object(fleet, "_def_ssh", lambda s, t, c=8: done(start_out())), redirect_stdout(io.StringIO()):
            assert fleet._UPD["reboot_unsure"] == 0.0 and outcome()[0] == 200


# ---------------------------------------------------------------- the worker

def test_the_worker_polls_only_the_reboot_while_it_runs():
    saves = []
    real_save = fleet._upd_save_locked
    with story(reboot=rb_running(1000.0), want=True, next_at=0.0, job=running_job(990.0)) as (h, sent), \
            mock.patch.object(fleet, "_upd_save_locked", lambda: (saves.append(1), real_save())[1]):
        for i in range(3):                                                                       # a scan is due and an install poll too: neither runs
            assert fleet._upd_tick(1010.0 + 5 * i) == fleet.REBOOT_POLL_S
        assert h.scripts() == ["REBOOT_POLL"] * 3 and all(c[1:] == (12, 5) for c in h.calls) and all(h.lock_free)
        assert fleet._UPD["job"]["state"] == "running" and fleet._UPD["job"]["contact_at"] == 990.0      # the install was not polled
        assert fleet._UPD["scan"] is None and fleet._UPD["busy"] is False and fleet._UPD["want"] is True
        assert saves == [] and not os.path.exists(fleet.UPD_FILE)                                        # nothing changed: a poll every 5 s writes nothing
        h.down = True                                                                            # the machine goes down
        assert fleet._upd_tick(1030.0) == fleet.REBOOT_POLL_S
        assert fleet._UPD["reboot"]["down_at"] == 1030.0 and len(saves) == 1
        with open(fleet.UPD_FILE) as f:
            assert json.load(f)["reboot"]["down_at"] == 1030.0
        for t in (1040.0, 1050.0, 1060.0):
            assert fleet._upd_tick(t) == fleet.REBOOT_POLL_S
        assert fleet._UPD["reboot"]["down_at"] == 1030.0 and len(saves) == 1 and sent == []             # set once, written once, no message
        assert h.scripts() == ["REBOOT_POLL"] * 7 and all(h.lock_free)
        h.down, h.boot = False, BOOT_B                                                           # back, on another boot
        assert fleet._upd_tick(1075.0) == 0
        r = fleet._UPD["reboot"]
        assert r["state"] == "done" and r["back_after"] == 75 and r["ended_at"] == 1075.0 and r["down_at"] == 1030.0 and r["error"] is None
        assert len(saves) == 2 and sent == []
        assert fleet._upd_tick(1076.0) == 5 and h.scripts()[-1] == "UPD_POLL"                    # with the reboot over, the install is polled again


def test_done_makes_a_rescan_due_at_once_and_sends_nothing():
    with story(scan=S([P("linux-image-7", security=True)], reboot=True, reboot_pkgs=["linux-image-7"], at=500.0), reboot=rb_running(1000.0), want=False,
               next_at=1e12) as (h, sent):
        h.boot = BOOT_B
        assert fleet._upd_tick(1070.0) == 0
        assert fleet._UPD["reboot"]["state"] == "done" and fleet._UPD["want"] is True and fleet._UPD["next_at"] == 1070.0 and sent == []
        assert h.scripts() == ["REBOOT_POLL"]
        s = fleet._UPD["scan"]                                                                   # the new boot has no reboot flag: gone at once, before the rescan
        assert s["reboot_required"] is False and s["reboot_pkgs"] == [] and s["at"] == 500.0 and s["packages"][0]["name"] == "linux-image-7"
        assert fleet._upd_tick(1071.0) == 0                                                      # the rescan, right after
        assert h.scripts() == ["REBOOT_POLL", "UPD_SCAN"]
        assert fleet._UPD["scan"]["at"] == 1071.0 and fleet._UPD["scan"]["reboot_required"] is False and fleet._UPD["want"] is False     # the flag is gone
        assert fleet._UPD["reboot"]["state"] == "done" and fleet._UPD["reboot"]["back_after"] == 70                                  # the result stays until the next reboot
        assert 1 <= fleet._upd_tick(1072.0) <= 60 and h.scripts() == ["REBOOT_POLL", "UPD_SCAN"] and sent == []                      # and that is all
        with open(fleet.UPD_FILE) as f:
            assert json.load(f)["reboot"]["state"] == "done"


def test_a_rescan_that_fails_right_after_the_reboot_does_not_bring_the_reboot_flag_back():
    """apt is often busy for a minute after boot, so the first rescan can fail. The scan it leaves in place is the old one: it must not
    still say "reboot required" (and critical) for the ten minutes until the next try, when the machine has just rebooted."""
    with story(scan=S([P("a")], reboot=True, reboot_pkgs=["linux-image-7"], at=500.0), reboot=rb_running(1000.0), want=False, next_at=1e12) as (h, sent):
        assert fleet.defiant_updates_view()["critical"] is True                                 # before: critical only because of the reboot
        h.boot = BOOT_B
        h.scan = scan_text(fixture("apt-sim-phased.txt"), rc=100, said="E: Could not get lock /var/lib/apt/lists/lock")
        assert fleet._upd_tick(1070.0) == 0 and fleet._UPD["reboot"]["state"] == "done"
        assert fleet.defiant_updates_view()["critical"] is False
        assert fleet._upd_tick(1071.0) == 0 and h.scripts() == ["REBOOT_POLL", "UPD_SCAN"]       # the rescan runs, and fails
        s = fleet._UPD["scan"]
        assert s["at"] == 500.0 and s["error"] and s["error_at"] == 1071.0 and s["reboot_required"] is False and s["reboot_pkgs"] == []
        v = fleet.defiant_updates_view()
        assert v["critical"] is False and v["scan"]["reboot_required"] is False and v["scan"]["next_at"] == 1071.0 + fleet.UPD_RETRY
        with open(fleet.UPD_FILE) as f:
            assert json.load(f)["scan"]["reboot_required"] is False and sent == []
    with story(scan=None, reboot=rb_running(1000.0), next_at=1e12) as (h, sent):                  # no scan yet: nothing to clear, nothing breaks
        h.boot = BOOT_B
        assert fleet._upd_tick(1070.0) == 0 and fleet._UPD["reboot"]["state"] == "done" and fleet._UPD["scan"] is None


def test_failed_sends_one_telegram_message_the_same_boot():
    with story(reboot=rb_running(1000.0), next_at=1e12, want=False) as (h, sent):
        for t in (1050.0, 1100.0, 1180.0):                                                       # still the same boot, within 180 s: waiting
            assert fleet._upd_tick(t) == 5 and fleet._UPD["reboot"]["state"] == "running"
        assert sent == []
        assert fleet._upd_tick(1181.0) == 0
        r = fleet._UPD["reboot"]
        assert r["state"] == "failed" and r["error"] == NO_REBOOT_ERROR and r["ended_at"] == 1181.0
        assert sent == ["Defiant: reboot failed\n\n" + NO_REBOOT_ERROR]
        assert fleet._UPD["want"] is False and fleet._UPD["next_at"] == 1e12                     # only a done reboot makes a rescan due
        n = len(h.calls)
        for t in (1182.0, 1190.0, 1300.0):                                                       # nothing polls a failed reboot, nothing is sent again
            assert 1 <= fleet._upd_tick(t) <= 60
        assert len(h.calls) == n and len(sent) == 1 and fleet._UPD["reboot"] == r
        assert fleet._upd_reboot_poll(1400.0) is False and len(sent) == 1                        # not even when asked to poll it
        with open(fleet.UPD_FILE) as f:
            assert json.load(f)["reboot"]["state"] == "failed"


def test_failed_sends_one_telegram_message_when_it_never_comes_back():
    with story(reboot=rb_running(1000.0), next_at=1e12, want=False) as (h, sent):
        h.down = True
        for t in (1010.0, 1100.0, 1500.0, 1900.0):                                               # 900 s after the request: still waiting
            assert fleet._upd_tick(t) == 5
        assert sent == [] and fleet._UPD["reboot"]["down_at"] == 1010.0
        assert fleet._upd_tick(1901.0) == 0
        r = fleet._UPD["reboot"]
        assert r["state"] == "failed" and r["error"] == GIVEUP_ERROR and r["ended_at"] == 1901.0 and r["down_at"] == 1010.0
        assert sent == [fleet.reboot_fail_text(r)] and sent[0].splitlines()[0] == "Defiant: reboot failed" and GIVEUP_ERROR in sent[0]
        assert fleet._UPD["want"] is False and fleet._UPD["next_at"] == 1e12
        h.down = False                                                                           # it answers later: too late, the result stays
        assert 1 <= fleet._upd_tick(1950.0) <= 60 and fleet._UPD["reboot"] == r and len(sent) == 1


def test_a_failed_telegram_send_is_not_fatal():
    sent = []
    with story(reboot=rb_running(1000.0), next_at=1e12) as (h, _):
        with mock.patch.object(fleet, "_tg_send", telegram(sent, ok=False)):
            assert fleet._upd_tick(1500.0) == 0
            assert fleet._UPD["reboot"]["state"] == "failed" and len(sent) == 1                  # one attempt, no retry, no exception
            assert 1 <= fleet._upd_tick(1501.0) <= 60 and len(sent) == 1
        with mock.patch.object(fleet, "_tg_send", REAL_TG), mock.patch.object(fleet.subprocess, "run", side_effect=OSError("no sender")):
            fleet._UPD["reboot"] = rb_running(1000.0)
            assert fleet._upd_tick(1500.0) == 0 and fleet._UPD["reboot"]["state"] == "failed"   # the real sender failing is no surprise either


def test_the_lock_is_free_while_the_failure_is_sent():
    seen = []
    with story(reboot=rb_running(1000.0), next_at=1e12):
        with mock.patch.object(fleet, "_tg_send", lambda text: (seen.append(lock_is_free()), True)[1]):
            fleet._upd_tick(1500.0)
    assert seen == [True]


def test_the_reboot_poll_survives_odd_answers():
    answers = [(done(""), 1010.0), (done("", "x", 255), 1010.0), (done(poll_out(), "client_loop: send disconnect", 255), 1010.0),   # cut: no answer
               (done("BOOT:" + "a" * 10 ** 6), 1010.0), (done("\x00\x01garbage\xff\nBOOT"), 1010.0), (done(poll_out(BOOT_A.upper())), 1010.0),
               (subprocess.TimeoutExpired(["ssh"], 12), 1010.0), (OSError("no route"), 1010.0), (subprocess.SubprocessError("x"), 1010.0),
               (done("BOOT:%s\nBOOT:%s\n" % (BOOT_A, BOOT_B)), None), (done(poll_out(), "", 1), None)]      # the same boot: nothing to see
    for reply, down in answers:
        with fleet_state():
            fleet._UPD["reboot"] = rb_running(1000.0)

            def ssh(s, t, c=8, reply=reply):
                if isinstance(reply, BaseException):
                    raise reply
                return reply
            with mock.patch.object(fleet, "_def_ssh", ssh), mock.patch.object(fleet, "_tg_send", mock.Mock(side_effect=AssertionError("no message"))):
                assert fleet._upd_reboot_poll(1010.0) is True, reply
            r = fleet._UPD["reboot"]
            assert r["state"] == "running" and r["down_at"] == down and r["boot"] == BOOT_A, reply


def test_a_fleet_restart_resumes_a_running_reboot():
    with story(reboot=rb_running(1000.0, down_at=1020.0, auto=True)) as (h, sent):
        with fleet._UPD_LOCK:
            fleet._upd_save_locked()
        fleet._UPD.update(fleet._upd_fresh())
        fleet.defiant_updates_load()
        assert fleet._UPD["reboot"] == rb_running(1000.0, down_at=1020.0, auto=True)
        fleet._UPD["next_at"] = 0.0                                                              # a scan would be due ...
        h.boot = BOOT_B
        assert fleet._upd_tick(1090.0) == 0 and h.scripts() == ["REBOOT_POLL"]                  # ... but the reboot is followed first
        r = fleet._UPD["reboot"]
        assert r["state"] == "done" and r["back_after"] == 90 and r["auto"] is True and sent == [] and fleet._UPD["want"] is True
        assert fleet._upd_tick(1091.0) == 0 and h.scripts() == ["REBOOT_POLL", "UPD_SCAN"]
    with story(reboot=rb_running(1000.0)) as (h, sent):                                          # Fleet was away for a long time and the host never rebooted
        with fleet._UPD_LOCK:
            fleet._upd_save_locked()
        fleet._UPD.update(fleet._upd_fresh())
        fleet.defiant_updates_load()
        fleet._UPD["next_at"] = 1e12
        assert fleet._upd_tick(5000.0) == 0
        assert fleet._UPD["reboot"]["state"] == "failed" and fleet._UPD["reboot"]["error"] == NO_REBOOT_ERROR and len(sent) == 1


def test_the_worker_does_not_scan_while_a_reboot_starts():
    with story(want=True, next_at=0.0, reboot_starting=True) as (h, sent):
        assert 1 <= fleet._upd_tick(5000.0) <= 60 and h.calls == []
        fleet._UPD["reboot_starting"] = False
        assert fleet._upd_tick(5001.0) == 0 and h.scripts() == ["UPD_SCAN"]


def test_a_scan_that_is_in_flight_is_not_confused_by_the_reboot_state():
    with story(next_at=0.0, reboot=rb_done(1000.0)) as (h, sent):                                # a finished reboot changes nothing for the worker
        assert fleet._upd_tick(5000.0) == 0 and h.scripts() == ["UPD_SCAN"] and fleet._UPD["reboot"] == rb_done(1000.0)


def test_a_scan_that_became_due_does_not_start_when_a_reboot_has_begun_since():
    """The worker looks at "due" under the lock, and starts the scan under it again a moment later. A click on Reboot Defiant, or a
    reboot that has just become running, can fall in between: the scan must stay out (it would fail with ssh going down) and stay wanted."""
    for begun in ({"reboot_starting": True}, {"reboot": rb_running(1000.0)}, {"reboot": rb_running(1000.0), "reboot_starting": True}):
        for want, next_at in ((True, 1e12), (False, 0.0)):                                       # asked for by a click, or due by the clock
            with story(want=want, next_at=next_at, busy=False) as (h, sent):
                real = fleet._upd_scan_once

                def late(now, begun=begun, real=real):                                           # the click lands after the tick has found the scan due
                    fleet._UPD.update(begun)
                    return real(now)
                with mock.patch.object(fleet, "_upd_scan_once", late):
                    assert fleet._upd_tick(5000.0) == 0
                assert h.calls == [] and sent == [] and fleet._UPD["busy"] is False and fleet._UPD["started"] == 0.0, begun
                assert fleet._UPD["want"] is want and fleet._UPD["next_at"] == next_at and fleet._UPD["scan"] is None, begun     # nothing was taken or reset
                assert not os.path.exists(fleet.UPD_FILE)
                for key, idle in (("reboot_starting", False), ("reboot", fleet._upd_reboot_idle())):
                    fleet._UPD[key] = idle                                                       # the reboot is over (or its start was refused)
                assert fleet._upd_tick(5001.0) == 0 and h.scripts() == ["UPD_SCAN"] and fleet._UPD["want"] is False      # the scan that was wanted runs now
    with story(want=True) as (h, sent):                                                          # called directly, in the two states that count as "rebooting"
        for begun in ({"reboot_starting": True}, {"reboot": rb_running(1000.0)}):
            fleet._UPD.update(begun)
            assert fleet._upd_rebooting() is True
            fleet._upd_scan_once(5000.0)
            assert h.calls == [] and fleet._UPD["busy"] is False and fleet._UPD["want"] is True and fleet._UPD["started"] == 0.0
            fleet._UPD.update(reboot_starting=False, reboot=fleet._upd_reboot_idle())
        fleet._upd_scan_once(5000.0)                                                             # no reboot: the scan starts as always
        assert h.scripts() == ["UPD_SCAN"] and fleet._UPD["want"] is False and fleet._UPD["started"] == 5000.0 and fleet._UPD["scan"]["at"] == 5000.0


# ---------------------------------------------------------------- part A: an install that needs a reboot reboots by itself

def test_an_install_that_needs_a_reboot_reboots_by_itself():
    with story() as (h, sent):
        t0 = h.click_install()
        assert h.scripts() == ["install start"] and fleet._UPD["reboot"]["state"] == "idle" and fleet._UPD["auto_reboot"] is True      # the click's mark
        assert fleet._upd_tick(t0 + 100) == 0                                                    # the poll sees the end of the job ...
        assert h.scripts() == ["install start", "UPD_POLL", "REBOOT_START"]                      # ... and the same tick starts the reboot
        assert h.calls[-1][:2] == (fleet.REBOOT_START, 30) and all(h.lock_free) and h.starting == [True]
        assert h.on_disk == ["done"]                                                             # the end of the install is on disk before Defiant is asked: once, also across a crash
        assert h.log.getvalue() == "DEFIANT reboot scheduled after the install\n"
        assert U.AUDIT == [("Defiant reboot started", "Started by Fleet after the install.", ENTRY_GO, True)]       # one list entry, the lock free while it is written
        j, r = fleet._UPD["job"], fleet._UPD["reboot"]
        assert j["state"] == "done" and j["exit"] == 0 and j["reboot_required"] is True
        assert r == {"state": "running", "started_at": t0 + 100, "ended_at": None, "down_at": None, "back_after": None, "error": None,
                     "boot": BOOT_A, "auto": True}
        assert sent == [] and fleet._UPD["reboot_starting"] is False and fleet._UPD_WAKE.is_set()
        assert fleet.defiant_updates_view()["reboot"]["auto"] is True and "boot" not in fleet.defiant_updates_view()["reboot"]
        assert fleet._UPD["want"] is True                                                        # a rescan is due, but not while the reboot runs
        with open(fleet.UPD_FILE) as f:
            saved = json.load(f)
        assert saved["job"]["state"] == "done" and saved["reboot"] == r
        h.boot, h.down = BOOT_B, True                                                            # the machine goes down and comes back on another boot
        for t in (t0 + 105, t0 + 110, t0 + 120):
            assert fleet._upd_tick(t) == fleet.REBOOT_POLL_S
        assert fleet._UPD["reboot"]["down_at"] == t0 + 105 and h.scripts()[3:] == ["REBOOT_POLL"] * 3 and "UPD_SCAN" not in h.scripts()
        h.down = False
        assert fleet._upd_tick(t0 + 150) == 0
        r = fleet._UPD["reboot"]
        assert r["state"] == "done" and r["back_after"] == 50 and r["auto"] is True and sent == [] and "UPD_SCAN" not in h.scripts()
        assert U.AUDIT[1:] == [("Defiant rebooted", "Defiant answered again with a new boot after 50 seconds.", ENTRY_GO, True)]   # and one when it is over
        h.scan = scan_text(fixture("apt-sim-phased.txt"), reboot=False)                         # now the rescan: the flag is gone
        assert fleet._upd_tick(t0 + 151) == 0
        assert h.scripts()[-1] == "UPD_SCAN" and h.scripts().count("UPD_SCAN") == 1 and h.scripts().count("REBOOT_START") == 1
        assert fleet._UPD["scan"]["reboot_required"] is False and fleet._UPD["job"]["reboot_required"] is True      # the job's own line stays factual
        assert sent == [] and 1 <= fleet._upd_tick(t0 + 152) <= 60 and fleet._UPD["auto_reboot"] is False


def test_an_install_without_a_reboot_flag_does_not_reboot():
    nothing = "ACTIVE:inactive\nSETUP:2\nSUMMARY:2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\nLOG:\n%s\n"
    for flag in ("no", "", "maybe", "YES", "yes please"):                                         # no flag, no answer, an answer that is none
        with story() as (h, sent):
            h.flag = flag
            t0 = h.click_install()
            assert fleet._upd_tick(t0 + 100) == 0
            assert h.scripts() == ["install start", "UPD_POLL"] and fleet._UPD["job"]["state"] == "done", flag
            assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and sent == [] and fleet._UPD["reboot_starting"] is False
            assert fleet._upd_tick(t0 + 101) == 0 and h.scripts()[-1] == "UPD_SCAN"                # the rescan runs as before
    with story() as (h, sent):                                                                    # a poll that has no REBOOT line at all
        t0 = h.click_install()
        h.probe = nothing % ("FLEET-APT-START %d\nFLEET-APT-EXIT 0 %d" % (t0, t0 + 90))
        assert fleet._upd_tick(t0 + 100) == 0 and fleet._UPD["job"]["state"] == "done" and fleet._UPD["job"]["reboot_required"] is None
        assert h.scripts() == ["install start", "UPD_POLL"] and fleet._UPD["reboot"]["state"] == "idle"


def test_an_install_whose_page_did_not_announce_the_reboot_never_reboots():
    """The page that sent the click says in its body (reboot_ok: true) that it announced the reboot. A page that was open before that
    existed shows the old confirm text and would trigger a reboot it never announced. Without the flag the install runs as before and
    ends done with the reboot waiting; nothing is scheduled, the rescan shows the waiting reboot, and the button is there."""
    for flag in (NO_ARG, False, None, "true", "True", "yes", 1, 1.0, 0, [True], {"reboot_ok": True}, b"x", object()):
        with story() as (h, sent):
            t0 = h.click_install(flag)
            assert fleet._UPD["job"]["state"] == "running" and fleet._UPD["auto_reboot"] is False, flag      # the unit is followed all the same
            assert fleet._upd_tick(t0 + 100) == 0
            j = fleet._UPD["job"]
            assert j["state"] == "done" and j["exit"] == 0 and j["reboot_required"] is True, flag             # the job says that a reboot is waiting
            assert h.scripts() == ["install start", "UPD_POLL"] and fleet._UPD["reboot"] == fleet._upd_reboot_idle(), flag      # no REBOOT_START, no state
            assert fleet._UPD["reboot_starting"] is False and fleet._UPD["reboot_unsure"] == 0.0 and sent == [] and U.AUDIT == [], flag
            h.scan = scan_text(fixture("apt-sim-phased.txt"), reboot=True, pkgs=["linux-image-7"])
            assert fleet._UPD["want"] is True and fleet._upd_tick(t0 + 101) == 0 and h.scripts()[-1] == "UPD_SCAN"      # the rescan follows at once ...
            assert fleet._UPD["scan"]["reboot_required"] is True and fleet._UPD["auto_reboot"] is False
            assert len(sent) == 1 and sent[0].startswith("Defiant: reboot required") and "REBOOT_START" not in h.scripts()   # ... and the pending reboot is alerted as before
            assert 1 <= fleet._upd_tick(t0 + 102) <= 60
            assert outcome()[0] == 200 and fleet._UPD["reboot"]["state"] == "running" and fleet._UPD["reboot"]["auto"] is False      # the button reboots when the person wants
    with story() as (h, sent):                                                                    # with the flag it is the automatic reboot, as before
        t0 = h.click_install(True)
        assert fleet._UPD["auto_reboot"] is True and fleet._upd_tick(t0 + 100) == 0 and h.scripts()[-1] == "REBOOT_START"
        assert fleet._UPD["reboot"]["state"] == "running" and fleet._UPD["reboot"]["auto"] is True


def test_the_flag_does_not_replace_the_key_or_the_clean_start():
    with story() as (h, sent):                                                                    # a bad key with the flag: nothing started, nothing promised
        for bad in (None, "", "wrong"):
            try:
                fleet.defiant_updates_install(bad, reboot_ok=True)
            except urllib.error.HTTPError as e:
                assert e.code == 403
            else:
                raise AssertionError("a bad key got past the check")
        assert h.calls == [] and fleet._UPD["job"]["state"] == "idle" and fleet._UPD["auto_reboot"] is False
    for reply in (done("FLEET-BUSY\n"), subprocess.TimeoutExpired(["ssh", "-o", "BatchMode=yes", "defiant.example", "a script"], 40)):      # a unit found, a start that timed out
        with story() as (h, sent):
            h.install_reply = reply
            t0 = h.click_install(True)
            assert fleet._UPD["job"]["state"] == "running" and fleet._UPD["auto_reboot"] is False, reply      # promised by the page, but this click did not start the unit cleanly
            assert fleet._upd_tick(t0 + 100) == 0 and "REBOOT_START" not in h.scripts() and fleet._UPD["reboot"] == fleet._upd_reboot_idle()
    with story() as (h, sent):                                                                    # a start that failed: no job, nothing promised
        h.install_reply = done("", "ssh: connect to host defiant.example port 22: Connection refused", 255)
        try:
            fleet.defiant_updates_install("fixture-key", reboot_ok=True)
        except OSError:
            pass
        else:
            raise AssertionError("a failed start was taken for a start")
        assert fleet._UPD["job"]["state"] == "idle" and fleet._UPD["auto_reboot"] is False


def test_the_mark_of_a_click_is_replaced_by_the_next_click():
    """The mark belongs to one click: a later click without the flag must not inherit the earlier one, and the other way round."""
    with story() as (h, sent):
        t0 = h.click_install(True)
        assert fleet._UPD["auto_reboot"] is True
        fleet._UPD["job"] = fleet._upd_job_idle()                                                 # (that unit is gone: the next click starts another)
        t1 = h.click_install(False)
        assert fleet._UPD["auto_reboot"] is False
        assert fleet._upd_tick(t1 + 100) == 0 and "REBOOT_START" not in h.scripts()
    with story() as (h, sent):
        h.click_install(False)
        fleet._UPD["job"] = fleet._upd_job_idle()
        t1 = h.click_install(True)
        assert fleet._UPD["auto_reboot"] is True and fleet._upd_tick(t1 + 100) == 0 and h.scripts()[-1] == "REBOOT_START"


def test_an_install_that_failed_or_was_lost_does_not_reboot():
    with story() as (h, sent):                                                                    # failed, with the flag
        h.exit = 100
        t0 = h.click_install()
        assert fleet._upd_tick(t0 + 100) == 0
        assert fleet._UPD["job"]["state"] == "failed" and fleet._UPD["job"]["reboot_required"] is True
        assert h.scripts() == ["install start", "UPD_POLL"] and fleet._UPD["reboot"] == fleet._upd_reboot_idle()
        assert len(sent) == 1 and sent[0].startswith("Defiant: update install failed (exit 100)")      # the install's own message, no reboot message
    with story() as (h, sent):                                                                    # the unit ended without an exit line
        t0 = h.click_install()
        h.probe = probe("inactive", "2", "", "yes", ["FLEET-APT-START %d" % t0])
        assert fleet._upd_tick(t0 + 100) == 0 and fleet._UPD["job"]["state"] == "failed"
        assert "REBOOT_START" not in h.scripts() and fleet._UPD["reboot"]["state"] == "idle"
    with story() as (h, sent):                                                                    # lost: no contact for 30 minutes
        t0 = h.click_install()
        h.down = True
        assert fleet._upd_tick(t0 + 60) == 5                                                     # no contact, the job keeps running quietly
        assert fleet._upd_tick(t0 + 1801) == 0
        assert fleet._UPD["job"]["state"] == "failed" and "lost contact" in fleet._UPD["job"]["log"][-1]
        h.down = False
        assert "REBOOT_START" not in h.scripts() and fleet._UPD["reboot"] == fleet._upd_reboot_idle() and len(sent) == 1


def test_nothing_but_a_keyed_install_click_starts_a_reboot():
    # a scan that finds a reboot flag, with the alert it sends: no reboot
    with story(next_at=0.0) as (h, sent):
        h.scan = scan_text(fixture("apt-sim-phased.txt"), reboot=True, pkgs=["linux-image-7"])
        assert fleet._upd_tick(5000.0) == 0 and fleet._UPD["scan"]["reboot_required"] is True
        assert h.scripts() == ["UPD_SCAN"] and fleet._UPD["reboot"] == fleet._upd_reboot_idle() and len(sent) == 1 and "reboot required" in sent[0]
        for t in (5001.0, 5100.0, 9000.0):
            fleet._upd_tick(t)
        assert "REBOOT_START" not in h.scripts()
    # a Fleet that starts with an install that is done and had the flag: no reboot, no scan
    done_job = dict(fleet._upd_job_idle(), state="done", started_at=1.0, ended_at=2.0, exit=0, installed=2, reboot_required=True, log=["x"])
    with story(job=done_job, scan=S(reboot=True, at=time.time()), next_at=1e12) as (h, sent):
        with fleet._UPD_LOCK:
            fleet._upd_save_locked()
        fleet._UPD.update(fleet._upd_fresh())
        fleet.defiant_updates_load()
        fleet._UPD["next_at"] = 1e12
        assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["job"]["reboot_required"] is True
        for t in (5000.0, 5001.0):
            assert 1 <= fleet._upd_tick(t) <= 60
        assert h.calls == [] and fleet._UPD["reboot"] == fleet._upd_reboot_idle()
    # a poll of a job that already ended starts nothing again: once
    with story() as (h, sent):
        t0 = h.click_install()
        fleet._upd_tick(t0 + 100)
        assert h.scripts().count("REBOOT_START") == 1
        fleet._upd_poll(t0 + 110)
        fleet._upd_poll(t0 + 120)
        assert h.scripts().count("REBOOT_START") == 1
    # an install that a Fleet restart interrupted is followed to its end, but the click that promised the reboot is gone with the old Fleet: no reboot
    with story() as (h, sent):
        t0 = h.click_install()
        with open(fleet.UPD_FILE) as f:
            assert "auto_reboot" not in json.dumps(json.load(f))                                 # a mark of memory only
        with open(fleet.UPD_FILE) as f:
            saved = json.load(f)
        with open(fleet.UPD_FILE, "w") as f:
            json.dump(dict(saved, auto_reboot=True, job=dict(saved["job"], auto_reboot=True)), f)     # a file that claims the mark is not believed either
        fleet._UPD.update(fleet._upd_fresh())
        fleet.defiant_updates_load()                                                             # the running job comes back from the file
        assert fleet._UPD["job"]["state"] == "running" and fleet._UPD["auto_reboot"] is False
        fleet._UPD["next_at"] = 1e12
        assert fleet._upd_tick(t0 + 100) == 0 and h.scripts() == ["install start", "UPD_POLL"]
        assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["job"]["reboot_required"] is True      # the page shows the reboot waiting ...
        assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and sent == [] and fleet._UPD["want"] is True    # ... for the Reboot button, and the rescan follows
        assert fleet._upd_tick(t0 + 101) == 0 and h.scripts()[-1] == "UPD_SCAN" and "REBOOT_START" not in h.scripts()


def test_a_unit_that_this_click_did_not_start_is_followed_but_never_rebooted_after():
    for reply in (done("FLEET-BUSY\n"), subprocess.TimeoutExpired(["ssh", "-o", "BatchMode=yes", "defiant.example", "a script"], 40)):
        with story() as (h, sent):
            h.install_reply = reply
            t0 = h.click_install()
            assert fleet._UPD["job"]["state"] == "running" and fleet._UPD["auto_reboot"] is False        # followed, but not the click's own clean start
            assert fleet._upd_tick(t0 + 100) == 0
            assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["job"]["reboot_required"] is True
            assert "REBOOT_START" not in h.scripts() and fleet._UPD["reboot"] == fleet._upd_reboot_idle() and sent == []
            assert fleet._upd_tick(t0 + 101) == 0 and h.scripts()[-1] == "UPD_SCAN" and fleet._UPD["auto_reboot"] is False
    with story() as (h, sent):                                                                   # a start that failed: no job, no mark
        h.install_reply = done("", "ssh: connect to host defiant.example port 22: Connection refused", 255)
        try:
            fleet.defiant_updates_install("fixture-key")
        except OSError:
            pass
        else:
            raise AssertionError("a failed start was taken for a start")
        assert fleet._UPD["job"]["state"] == "idle" and fleet._UPD["auto_reboot"] is False
    with story(auto_reboot=True) as (h, sent):                                                   # a mark left over from an earlier click cannot start a reboot after another job
        fleet._UPD["job"] = running_job(1000.0)
        h.probe = probe("inactive", "2", "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", "yes", ["FLEET-APT-START 1000", "FLEET-APT-EXIT 0 1100"])
        assert fleet._upd_poll(1200.0) is False and fleet._UPD["reboot"]["state"] == "running" and fleet._UPD["auto_reboot"] is False
        fleet._UPD["job"], fleet._UPD["reboot"] = running_job(1300.0), fleet._upd_reboot_idle()
        h.probe = probe("inactive", "2", "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", "yes", ["FLEET-APT-START 1300", "FLEET-APT-EXIT 0 1400"])
        assert fleet._upd_poll(1500.0) is False and fleet._UPD["reboot"]["state"] == "idle" and h.scripts().count("REBOOT_START") == 1      # the mark was used up


def test_a_refused_automatic_start_ends_failed_with_one_message():
    refusals = [("FLEET-BUSY apt\n", APT_BUSY.rstrip(".")), ("FLEET-BUSY install\n", "An install is running. Wait until it is done"),
                ("FLEET-BUSY reboot\n", "A reboot is already running"), ("", DID_NOT_ACCEPT.rstrip(".")), (start_out(rc=1), DID_NOT_ACCEPT.rstrip(".")),
                ("FLEET-BOOT garbage\nFLEET-RC:0\n", DID_NOT_ACCEPT.rstrip(".")), (OSError("x"), DID_NOT_ACCEPT.rstrip(".")),
                ("FLEET-NOCHECK fuser 1\n", DID_NOT_ACCEPT.rstrip("."))]
    for reply, reason in refusals:
        with story() as (h, sent):
            h.start_reply = reply
            t0 = h.click_install()
            assert fleet._upd_tick(t0 + 100) == 0
            assert h.scripts() == ["install start", "UPD_POLL", "REBOOT_START"]
            r = fleet._UPD["reboot"]
            assert r == {"state": "failed", "started_at": None, "ended_at": t0 + 100, "down_at": None, "back_after": None, "boot": None, "auto": True,
                         "error": "The reboot after the install did not start: %s. Reboot Defiant when apt is idle." % reason}, r
            assert ".." not in r["error"]                                                          # the reason's own full stop is not doubled
            assert len(sent) == 1 and sent[0] == fleet.reboot_fail_text(r) and sent[0].splitlines()[0] == "Defiant: reboot failed" and len(sent[0]) < 3500
            assert sent[0] == "Defiant: reboot failed\n\n" + r["error"]                            # the headline and the error, nothing after it
            assert fleet._UPD["reboot_starting"] is False
            assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["job"]["reboot_required"] is True      # the install itself is done and stays so
            with open(fleet.UPD_FILE) as f:
                assert json.load(f)["reboot"] == r
            assert fleet._UPD["want"] is True and fleet._upd_tick(t0 + 101) == 0 and h.scripts()[-1] == "UPD_SCAN"      # no reboot runs: the rescan comes at once
            assert len(sent) == 1                                                                 # the reboot message is not repeated
            h.start_reply = None                                                                  # nothing was rebooted: the button is not held back by a gap
            assert fleet.defiant_updates_reboot("fixture-key")["ok"] is True
            assert fleet._UPD["reboot"]["state"] == "running" and fleet._UPD["reboot"]["auto"] is False


def test_an_automatic_start_that_got_no_answer_says_so_and_holds_the_button():
    """Defiant may have scheduled the reboot before ssh gave up: the page must not say that it did not start, and the button must not ask
    for a second reboot of a machine that may be about to go down."""
    for reply in (subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30), "FLEET-BOOT %s\n" % BOOT_A):         # a timeout, and an answer cut off after the boot id
        with story() as (h, sent):
            h.start_reply = reply
            t0 = h.click_install()
            assert fleet._upd_tick(t0 + 100) == 0
            r = fleet._UPD["reboot"]
            assert r == {"state": "failed", "started_at": None, "ended_at": t0 + 100, "down_at": None, "back_after": None, "boot": None, "auto": True,
                         "error": UNSURE_AUTO}, r
            assert "did not start" not in r["error"] and fleet._UPD["reboot_unsure"] == t0 + 100 and fleet._UPD["reboot_starting"] is False
            assert len(sent) == 1 and sent[0] == fleet.reboot_fail_text(r) and sent[0].splitlines()[0] == "Defiant: reboot failed" and len(sent[0]) < 3500
            with open(fleet.UPD_FILE) as f:
                assert json.load(f)["reboot"] == r
            assert fleet._UPD["want"] is True and fleet._upd_tick(t0 + 101) == 0 and h.scripts()[-1] == "UPD_SCAN"      # the rescan comes at once, as after any refusal
            n = len(h.calls)
            with mock.patch.object(fleet.time, "time", lambda: t0 + 160):
                assert outcome() == (409, GAP_TEXT) and len(h.calls) == n                          # a click a minute later asks nothing of Defiant
            h.start_reply = None
            with mock.patch.object(fleet.time, "time", lambda: t0 + 100 + fleet.REBOOT_GAP):
                assert outcome()[0] == 200                                                         # when the gap is over, the person can ask again


def test_a_button_start_that_got_no_answer_holds_the_button_for_the_gap():
    T0 = 5000.0
    for reply in (subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30), "FLEET-BOOT %s\n" % BOOT_A):
        with story() as (h, sent), mock.patch.object(fleet.time, "time", lambda: T0):
            h.start_reply = reply
            assert outcome() == (502, UNSURE)
            assert fleet._UPD["reboot"] == fleet._upd_reboot_idle() and fleet._UPD["reboot_unsure"] == T0 and sent == []      # the button's answer is the message
            n = len(h.calls)
            assert outcome() == (409, GAP_TEXT) and outcome() == (409, GAP_TEXT) and len(h.calls) == n                        # more clicks ask nothing of Defiant
            h.start_reply = None
            with mock.patch.object(fleet.time, "time", lambda: T0 + fleet.REBOOT_GAP - 1):
                assert outcome() == (409, GAP_TEXT)
            with mock.patch.object(fleet.time, "time", lambda: T0 + fleet.REBOOT_GAP):
                assert outcome()[0] == 200 and fleet._UPD["reboot"]["state"] == "running" and h.calls[-1][0] == fleet.REBOOT_START
    with story() as (h, sent), mock.patch.object(fleet.time, "time", lambda: T0):                  # a failure that says something (Defiant refused, ssh failed) leaves no mark
        h.start_reply = ""
        assert outcome() == (502, DID_NOT_ACCEPT) and fleet._UPD["reboot_unsure"] == 0.0
        h.start_reply = "FLEET-BUSY apt\n"
        assert outcome() == (409, APT_BUSY) and fleet._UPD["reboot_unsure"] == 0.0
        h.start_reply = None
        assert outcome()[0] == 200                                                                 # the very next click is accepted


def test_a_slow_shutdown_after_the_verdict_does_not_allow_a_second_reboot():
    """Asked at 1000, still on the old boot at 1181: failed ("still on the same boot", and that it may still reboot). The machine then goes
    down all the same and is back at 1260. A click at 1275 must not reboot it again; the gap runs from the request, whatever the verdict was."""
    with story(next_at=1e12) as (h, sent):
        with mock.patch.object(fleet.time, "time", lambda: 1000.0):
            assert outcome()[0] == 200
        assert fleet._upd_tick(1181.0) == 0 and fleet._UPD["reboot"]["state"] == "failed" and len(sent) == 1
        h.boot = BOOT_B                                                                            # it rebooted after the verdict
        n = len(h.calls)
        for t in (1181.0, 1275.0, 1599.0):
            with mock.patch.object(fleet.time, "time", lambda t=t: t):
                assert outcome() == (409, GAP_TEXT), t
        assert len(h.calls) == n and fleet._UPD["reboot"]["state"] == "failed"
        with mock.patch.object(fleet.time, "time", lambda: 1600.0):
            assert outcome()[0] == 200                                                             # ten minutes after the request it may be asked again


def test_the_reason_of_a_refused_automatic_start_is_cleaned_and_capped():
    for exc in (RuntimeError("x" * 500 + "\x1b[31m\x00"), PermissionError("a\nb\x1b" + "y" * 500)):
        with story() as (h, sent):
            t0 = h.click_install()
            with mock.patch.object(fleet, "_upd_reboot_start", mock.Mock(side_effect=exc)):
                assert fleet._upd_tick(t0 + 100) == 0
            e = fleet._UPD["reboot"]["error"]
            assert e.startswith("The reboot after the install did not start: ") and e.endswith(". Reboot Defiant when apt is idle.")
            assert len(e) <= len("The reboot after the install did not start: ") + 200 + len(". Reboot Defiant when apt is idle.")
            assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", e.replace("\n", "")) and len(sent) == 1 and len(sent[0]) < 3500


def test_an_unexpected_error_in_the_automatic_start_is_not_a_refusal():
    """A refusal or a failure that Defiant answers with is a failed reboot with its one message. A surprise (a bug, anything the start
    does not know) is neither: it reaches the worker's own guard, and nothing is left locked, half done or sent."""
    with story() as (h, sent):
        t0 = h.click_install()
        h.start_reply = ValueError("a surprise")
        try:
            fleet._upd_tick(t0 + 100)
        except ValueError:
            pass
        else:
            raise AssertionError("a surprise was taken for a refusal")
        assert fleet._UPD["reboot_starting"] is False and fleet._UPD["reboot"] == fleet._upd_reboot_idle() and sent == []
        assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["want"] is True               # the end of the install is recorded, the rescan is due
        with open(fleet.UPD_FILE) as f:
            assert json.load(f)["job"]["state"] == "done"
        h.start_reply = None
        assert fleet._upd_tick(t0 + 101) == 0 and h.scripts()[-1] == "UPD_SCAN"                  # the worker goes on, nothing is held back by a flag
        assert fleet.defiant_updates_reboot("fixture-key")["ok"] is True                        # and the button still works
    with story() as (h, sent):                                                                   # the same surprise in the button's start: the guard is lifted
        h.start_reply = ValueError("a surprise")
        try:
            fleet.defiant_updates_reboot("fixture-key")
        except ValueError:
            pass
        else:
            raise AssertionError("a surprise was taken for a refusal")
        assert fleet._UPD["reboot_starting"] is False and fleet._UPD["reboot"] == fleet._upd_reboot_idle() and sent == []
        h.start_reply = None
        assert outcome()[0] == 200


def test_the_refused_automatic_start_message_may_fail_to_send():
    sent = []
    with story() as (h, _):
        with mock.patch.object(fleet, "_tg_send", telegram(sent, ok=False)):
            h.start_reply = "FLEET-BUSY apt\n"
            t0 = h.click_install()
            assert fleet._upd_tick(t0 + 100) == 0 and fleet._UPD["reboot"]["state"] == "failed" and len(sent) == 1


def test_the_automatic_start_asks_neither_for_the_key_nor_for_the_gap():
    with story(reboot=rb_done(time.time() - 100)) as (h, sent):                                  # a reboot 100 s ago: the button would be refused now
        assert outcome() == (409, "A reboot was requested a moment ago. Try again in a few minutes.") and h.calls == []
        t0 = h.click_install()                                                                    # the click is keyed
        with mock.patch.object(fleet, "computer_auth", mock.Mock(side_effect=AssertionError("no key check in the automatic path"))), \
                mock.patch.object(fleet, "key_check", mock.Mock(side_effect=AssertionError("no key check in the automatic path"))):
            assert fleet._upd_tick(t0 + 100) == 0
        r = fleet._UPD["reboot"]
        assert r["state"] == "running" and r["auto"] is True and h.scripts()[-1] == "REBOOT_START" and sent == []
    with story(reboot=rb_done(time.time() - 100)) as (h, sent):                                  # the button, in the same state, is refused by the gap
        assert outcome() == (409, "A reboot was requested a moment ago. Try again in a few minutes.") and h.calls == []


def test_the_automatic_start_is_the_buttons_start():
    """One implementation: the same script, the same limits, the same refusals; only the key, the gap and the auto flag differ."""
    with story() as (h, sent):
        assert outcome()[0] == 200
        by_button = h.calls[-1]
        r = fleet._UPD["reboot"]
        assert r["auto"] is False
        fleet._UPD["reboot"] = fleet._upd_reboot_idle()
        fleet._UPD["reboot_starting"] = True
        res = fleet._upd_reboot_start(True, 5000.0)
        assert res == {"ok": True, "started_at": 5000.0} and h.calls[-1] == by_button
        assert fleet._UPD["reboot"] == rb_running(5000.0, auto=True) and fleet._UPD["reboot_starting"] is False


# ---------------------------------------------------------------- the Messages list: one entry per event of a reboot

SAME_BOOT_ENTRY = "Defiant is still on the same boot three minutes after the request. It may still reboot. If it does not, check it in your hosting..."
APT_AUTO_ENTRY = "The reboot after the install did not start: apt is busy on Defiant, an upgrade may be running. Try again in a few minutes. Reboot..."


def check_entries(entries):
    """What every entry has in common: its place, one line, at most 140 characters, and the state lock free while it was written."""
    for title, text, go, free in entries:
        assert go == ENTRY_GO and free is True and "\n" not in text and len(text) <= 140 and EM not in text, (title, text)


def test_a_reboot_leaves_one_entry_per_event_in_the_messages_list():
    with story() as (h, sent):                                                                    # the button, to the end
        assert outcome()[0] == 200
        t0 = fleet._UPD["reboot"]["started_at"]
        h.boot, h.down = BOOT_B, True
        for t in (t0 + 5, t0 + 10, t0 + 20):                                                      # down: the state changes once, the list not at all
            fleet._upd_tick(t)
        assert fleet._UPD["reboot"]["down_at"] == t0 + 5 and len(U.AUDIT) == 1
        h.down = False
        fleet._upd_tick(t0 + 75.4)
        assert U.AUDIT == [("Defiant reboot started", "Started with the Reboot Defiant button.", ENTRY_GO, True),
                           ("Defiant rebooted", "Defiant answered again with a new boot after 75 seconds.", ENTRY_GO, True)]
        for t in (t0 + 76, t0 + 90, t0 + 4000):                                                   # the rescan, later ticks: nothing more
            fleet._upd_tick(t)
        assert len(U.AUDIT) == 2 and sent == []
        check_entries(U.AUDIT)
    with story() as (h, sent):                                                                    # the same boot after three minutes: failed, with the error cut to one line
        assert outcome()[0] == 200
        t0 = fleet._UPD["reboot"]["started_at"]
        for t in (t0 + 100, t0 + 180):
            fleet._upd_tick(t)
        assert len(U.AUDIT) == 1
        fleet._upd_tick(t0 + 181)
        assert fleet._UPD["reboot"]["error"] == NO_REBOOT_ERROR and len(NO_REBOOT_ERROR) > 140
        assert U.AUDIT[1:] == [("Defiant reboot failed", SAME_BOOT_ENTRY, ENTRY_GO, True)] and len(SAME_BOOT_ENTRY) <= 140
        assert sent == ["Defiant: reboot failed\n\n" + NO_REBOOT_ERROR]                           # Telegram carries the whole text
        for t in (t0 + 182, t0 + 500, t0 + 5000):
            fleet._upd_tick(t)
        fleet._upd_reboot_poll(t0 + 6000)
        assert len(U.AUDIT) == 2 and len(sent) == 1                                               # once
        check_entries(U.AUDIT)
    with story() as (h, sent):                                                                    # a machine that does not come back: failed, the whole short text
        assert outcome()[0] == 200
        t0 = fleet._UPD["reboot"]["started_at"]
        h.down = True
        for t in (t0 + 10, t0 + 900):
            fleet._upd_tick(t)
        assert len(U.AUDIT) == 1
        fleet._upd_tick(t0 + 901)
        assert U.AUDIT[1:] == [("Defiant reboot failed", GIVEUP_ERROR, ENTRY_GO, True)] and len(U.AUDIT) == 2 and len(sent) == 1
        check_entries(U.AUDIT)
    with story() as (h, sent):                                                                    # the automatic one: started by Fleet, then done
        t0 = h.click_install()
        assert U.AUDIT == [] and fleet._upd_tick(t0 + 100) == 0
        assert U.AUDIT == [("Defiant reboot started", "Started by Fleet after the install.", ENTRY_GO, True)]
        h.boot = BOOT_B
        fleet._upd_tick(t0 + 130)
        assert [a[:2] for a in U.AUDIT[1:]] == [("Defiant rebooted", "Defiant answered again with a new boot after 30 seconds.")]
        check_entries(U.AUDIT)
    with story(reboot=rb_running(1000.0), next_at=1e12) as (h, sent):                             # a Fleet that was away: the end is listed once it is seen
        with fleet._UPD_LOCK:
            fleet._upd_save_locked()
        fleet._UPD.update(fleet._upd_fresh())
        fleet.defiant_updates_load()
        h.boot = BOOT_B
        assert fleet._upd_tick(1090.0) == 0 and U.AUDIT == [("Defiant rebooted", "Defiant answered again with a new boot after 90 seconds.", ENTRY_GO, True)]


def test_a_refused_automatic_start_leaves_one_failed_entry_and_no_started_one():
    refusals = [("FLEET-BUSY apt\n", APT_AUTO_ENTRY), ("FLEET-BUSY install\n", None), ("FLEET-BUSY reboot\n", None), ("", None), (start_out(rc=1), None),
                (subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30), UNSURE_AUTO), ("FLEET-BOOT %s\n" % BOOT_A, UNSURE_AUTO)]
    for reply, expected in refusals:
        with story() as (h, sent):
            h.start_reply = reply
            t0 = h.click_install()
            assert fleet._upd_tick(t0 + 100) == 0
            e = fleet._UPD["reboot"]["error"]
            assert fleet._UPD["reboot"]["state"] == "failed" and len(sent) == 1 and sent[0] == "Defiant: reboot failed\n\n" + e, reply
            want = expected or (e if len(e) <= 140 else e[:137].rsplit(" ", 1)[0] + "...")           # whole when it fits, else cut at a word
            assert U.AUDIT == [("Defiant reboot failed", want, ENTRY_GO, True)], (reply, U.AUDIT)
            check_entries(U.AUDIT)
            fleet._upd_tick(t0 + 101)
            fleet._upd_tick(t0 + 2000)
            assert len(U.AUDIT) == 1 and len(sent) == 1                                           # once
    with story() as (h, sent):                                                                    # a surprise in the start is no refusal: no entry either
        t0 = h.click_install()
        h.start_reply = ValueError("a surprise")
        try:
            fleet._upd_tick(t0 + 100)
        except ValueError:
            pass
        assert U.AUDIT == [] and sent == []


def test_what_does_not_start_or_end_a_reboot_leaves_no_entry():
    with story() as (h, sent):
        for reply, code in (("FLEET-BUSY apt\n", 409), ("FLEET-BUSY install\n", 409), ("FLEET-BUSY reboot\n", 409), ("", 502), (start_out(rc=1), 502),
                            (subprocess.TimeoutExpired(["ssh", fleet.REBOOT_START], 30), 502), ("FLEET-BOOT %s\n" % BOOT_A, 502), ("FLEET-NOCHECK fuser 1\n", 502)):
            h.start_reply = reply
            fleet._UPD["reboot_unsure"] = 0.0                                                     # (a start that got no answer holds the next click for ten minutes)
            assert outcome()[0] == code, reply
            assert U.AUDIT == [], reply                                                           # a button that was refused or got no answer: the page already said so
        for bad in (None, "wrong"):
            assert outcome(bad) == (403, fleet.POLICY)
        h.start_reply = None
        fleet._UPD["reboot_unsure"] = 0.0
        t0 = h.click_install()
        assert fleet._upd_tick(t0 + 100) == 0 and U.AUDIT[0][0] == "Defiant reboot started"       # (the one that does start)
    with story(next_at=0.0, want=True) as (h, sent):                                              # a scan, an install, the unattended upgrades: not reboots
        h.scan = scan_text(fixture("apt-sim-phased.txt"), reboot=True, pkgs=["linux-image-7"])
        fleet._upd_tick(5000.0)
        t0 = h.click_install(False)
        fleet._upd_tick(t0 + 100)
        fleet._upd_tick(t0 + 101)
        assert U.AUDIT == [] and "REBOOT_START" not in h.scripts()


def test_a_failed_write_to_the_messages_list_never_stops_a_reboot():
    for exc in (OSError("disk full"), RuntimeError("boom"), ValueError("v"), KeyError("k"), TypeError("t"), AttributeError("a")):
        def broken(title, text, go="", exc=exc):
            raise exc
        with story() as (h, sent), mock.patch.object(fleet, "ev_audit", broken):
            res = outcome()
            assert res[0] == 200 and fleet._UPD["reboot"]["state"] == "running" and fleet._UPD_WAKE.is_set() and fleet._UPD["reboot_starting"] is False, exc
            t0 = fleet._UPD["reboot"]["started_at"]
            h.boot = BOOT_B
            assert fleet._upd_tick(t0 + 60) == 0 and fleet._UPD["reboot"]["state"] == "done" and fleet._UPD["want"] is True, exc
            lines = [l for l in h.log.getvalue().splitlines() if "Messages entry not written" in l]
            assert lines == ["defiant updates: Messages entry not written: %s: %s" % (exc.__class__.__name__, exc)] * 2, (exc, lines)       # one journal line for each of the two entries
    with story() as (h, sent), mock.patch.object(fleet, "ev_audit", mock.Mock(side_effect=OSError("disk full"))):             # a refused automatic start: still failed, still one message
        h.start_reply = "FLEET-BUSY apt\n"
        t0 = h.click_install()
        assert fleet._upd_tick(t0 + 100) == 0 and fleet._UPD["reboot"]["state"] == "failed" and len(sent) == 1 and fleet._UPD["reboot_starting"] is False


def test_the_text_of_an_entry_is_one_line_of_at_most_140_characters():
    long_words = " ".join(["word"] * 80)
    for text, expected in (("short", "short"), ("a" * 140, "a" * 140), ("a" * 141, "a" * 137 + "..."), ("x" * 500, "x" * 137 + "..."),
                           (" a  b\n\tc ", "a b c"), ("line one\nline two", "line one line two"), (None, ""), ("", ""),
                           (long_words, " ".join(["word"] * 27) + "..."), ("ab " * 70, " ".join(["ab"] * 45) + "...")):
        with fleet_state():
            fleet._upd_audit("A title", text)
            assert U.AUDIT == [("A title", expected, ENTRY_GO, True)], text
            assert len(expected) <= 140
    with fleet_state():
        fleet._upd_audit("A title", "x" * 138)                                                    # 138 characters fit as they are
        assert U.AUDIT[0][1] == "x" * 138


def test_the_entries_are_list_only_audit_entries_of_the_real_messages_list():
    """With the real ev_audit (its files in a temp folder): level info, kind audit, state point, no episode. That is the kind that no rule
    and no push touches: a push is only made for an "act" entry of an episode. The phone reads the same list."""
    with story() as (h, sent), tempfile.TemporaryDirectory(prefix="fleet-ev-") as d, mock.patch.object(fleet, "ev_audit", REAL_AUDIT), \
            mock.patch.object(fleet, "EV_DIR", d), mock.patch.object(fleet, "EV_FILE", os.path.join(d, "events.jsonl")), \
            mock.patch.object(fleet, "EV_STATE", os.path.join(d, "events-state.json")), mock.patch.object(fleet, "_EP", {}), \
            mock.patch.object(fleet, "_EV", {"last": 0, "items": [], "lines": 0, "boots": {}, "ma": None, "blob": ""}):
        assert outcome()[0] == 200
        t0 = fleet._UPD["reboot"]["started_at"]
        h.boot = BOOT_B
        fleet._upd_tick(t0 + 60)
        with open(os.path.join(d, "events.jsonl"), encoding="utf-8") as f:
            entries = [json.loads(line) for line in f]
        assert [(e["title"], e["text"]) for e in entries] == [("Defiant reboot started", "Started with the Reboot Defiant button."),
                                                              ("Defiant rebooted", "Defiant answered again with a new boot after 60 seconds.")]
        for e in entries:
            assert (e["kind"], e["level"], e["state"], e["episode"], e["go"], e["node"], e["reminder"]) == ("audit", "info", "point", "", ENTRY_GO, "", 0), e
        assert [e["id"] for e in entries] == [1, 2]
        listed = fleet.events_json()
        assert listed["events"] == entries and listed["open"] == [] and listed["latest"] == 2        # the phone reads this list; nothing opened an episode
        assert sorted(os.listdir(d)) == ["events.jsonl"]


# ---------------------------------------------------------------- the real handler

def test_http_route():
    no_network = mock.Mock(side_effect=AssertionError("a request reached the network (llama-swap?)"))
    with fleet_state(), mock.patch.object(fleet.urllib.request, "urlopen", no_network):
        ssh = mock.Mock(side_effect=AssertionError("a request thread used ssh without being allowed to"))
        with mock.patch.object(fleet, "_def_ssh", ssh), U.server() as port, redirect_stdout(io.StringIO()):
            post = lambda key="fixture-key", body=None, path="/api/defiant/reboot": U.call(port, "POST", path, {} if body is None else body, key)
            for key in (None, "wrong"):                                                           # the key first, and nothing asked of Defiant
                st, h, v = post(key)
                assert st == 403 and v == {"error": fleet.POLICY} and h["cache-control"] == "no-store", (st, v)
            assert fleet._UPD["reboot"]["state"] == "idle"
            for key in (None, "wrong"):                                                           # a key in the body is no key
                st, h, v = post(key, {"key": "fixture-key", "x-starfleet-key": "fixture-key", "starfleet_key": "fixture-key"})
                assert st == 403 and v == {"error": fleet.POLICY}, (st, v)
            assert fleet._UPD["reboot"]["state"] == "idle" and ssh.call_count == 0
            ssh.side_effect = None
            ssh.return_value = done(start_out())
            st, h, v = post(body={"anything": 1, "delay": 0})                                      # the body is ignored
            assert st == 200 and v["ok"] is True and isinstance(v["started_at"], float) and h["cache-control"] == "no-store" and h["content-type"] == "application/json"
            ssh.assert_called_once_with(fleet.REBOOT_START, 30)
            g = U.call(port, "GET", "/api/defiant/updates")[2]
            assert g["reboot"]["state"] == "running" and g["reboot"]["auto"] is False and "boot" not in g["reboot"] and set(g["reboot"]) == VIEW_KEYS
            assert post()[::2] == (409, {"error": "A reboot is already running."})
            assert U.call(port, "POST", "/api/defiant/updates/install", {}, "fixture-key")[::2] == (409, {"error": "A reboot is running."})
            assert U.call(port, "POST", "/api/defiant/updates/scan", {})[::2] == (200, {"ok": True, "started": False, "reason": "rebooting"})
            assert ssh.call_count == 1
            fleet._UPD["reboot"] = rb_done(time.time() - 10)
            assert post()[::2] == (409, {"error": "A reboot was requested a moment ago. Try again in a few minutes."})
            fleet._UPD["reboot"] = fleet._upd_reboot_idle()
            fleet._UPD["busy"] = True
            assert post()[::2] == (409, {"error": "A scan is running. Try again in a moment."})
            fleet._UPD["busy"], fleet._UPD["job"] = False, running_job(time.time())
            assert post()[::2] == (409, {"error": "An install is running. Wait until it is done."})
            fleet._UPD["job"] = fleet._upd_job_idle()
            ssh.return_value = done("FLEET-BUSY apt\n")
            assert post()[::2] == (409, {"error": APT_BUSY})
            for reply, text in ((done("FLEET-BOOT garbage\n"), DID_NOT_ACCEPT), (done("", "ssh: connect to host defiant.example port 22: Refused", 255), DID_NOT_ACCEPT),
                                (subprocess.TimeoutExpired(["ssh", "defiant.example", "a script text"], 30), UNSURE),
                                (done("FLEET-BOOT %s\n" % BOOT_A, "client_loop: send disconnect: Broken pipe", 255), UNSURE),
                                (OSError("boom"), DID_NOT_ACCEPT)):
                ssh.return_value, ssh.side_effect = (reply, None) if not isinstance(reply, BaseException) else (None, reply)
                st, h, v = post()
                assert (st, v) == (502, {"error": text}) and fleet._UPD["reboot"]["state"] == "idle", (st, v)
                if text == UNSURE:                                                                 # it may have been scheduled: the next click waits, and asks nothing
                    calls = ssh.call_count
                    assert post()[::2] == (409, {"error": GAP_TEXT}) and ssh.call_count == calls
                    fleet._UPD["reboot_unsure"] = 0.0                                              # (the next case starts clean)
            for exc in (RuntimeError("x" * 500), OSError("y" * 500), subprocess.SubprocessError("z" * 500)):       # whatever is raised, the page gets at most 200 characters
                with mock.patch.object(fleet, "defiant_updates_reboot", mock.Mock(side_effect=exc)):
                    st, h, v = post()
                assert st == 502 and len(v["error"]) == 200 and v["error"] == str(exc)[:200], (st, v)
            ssh.return_value, ssh.side_effect = done(start_out()), None
            with mock.patch.object(fleet, "DEFIANT_HOST", ""):
                assert post()[::2] == (400, {"error": "Defiant is not configured."})
                assert U.call(port, "GET", "/api/defiant/updates")[2] == {"configured": False}
            assert post()[0] == 200                                                                # a new request after all of that works
            assert U.call(port, "POST", "/api/defiant/restart", {}, "fixture-key")[::2] == (400, {"error": "unknown model"})     # one word for it: no second route
        assert no_network.call_count == 0


def test_http_install_flag():
    """The real handler: only a body that is the object {"reboot_ok": true} announces the reboot. Every other body installs as before and
    never reboots afterwards. A body that is no object (a list, null, a number, a string) must not end the request: it is the same as no flag."""
    announce = [b'{"reboot_ok": true}', b'{"reboot_ok":true,"x":1}', b'  {"reboot_ok": true}\n', b'{"x": 1, "reboot_ok": true}']
    silent = [b"", b"{}", b'{"reboot_ok": false}', b'{"reboot_ok": null}', b'{"reboot_ok": "true"}', b'{"reboot_ok": 1}', b'{"reboot_ok": 1.0}',
              b'{"reboot_ok": [true]}', b'{"reboot_ok": {"a": true}}', b'{"reboot": true}', b'{"REBOOT_OK": true}', b'{"reboot_ok": "yes"}',
              b'{"other": {"reboot_ok": true}}', b"true", b"false", b"1", b"3.5", b"null", b'"true"', b'""', b"[]", b"[true]", b'[{"reboot_ok": true}]']
    no_network = mock.Mock(side_effect=AssertionError("a request reached the network (llama-swap?)"))
    for raw, announced in [(r, True) for r in announce] + [(r, False) for r in silent]:
        with story() as (h, sent), mock.patch.object(fleet.urllib.request, "urlopen", no_network), U.server() as port:
            st, hd, v = U.call(port, "POST", "/api/defiant/updates/install", raw_body=raw, key="fixture-key")
            assert st == 200 and v["ok"] is True and isinstance(v["started_at"], float) and hd["cache-control"] == "no-store", (raw, st, v)
            assert fleet._UPD["job"]["state"] == "running" and fleet._UPD["auto_reboot"] is announced, raw
            h.end_install(v["started_at"])
            fleet._upd_tick(v["started_at"] + 100)
            assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["job"]["reboot_required"] is True, raw
            assert ("REBOOT_START" in h.scripts()) is announced and (fleet._UPD["reboot"]["state"] == "running") is announced, raw
            assert fleet._UPD["reboot"]["auto"] is True if announced else fleet._UPD["reboot"] == fleet._upd_reboot_idle(), raw
        assert no_network.call_count == 0
    with story() as (h, sent), U.server() as port:                                                # the key decides, whatever the body says; bad json is the old 400
        for key in (None, "wrong"):
            st, hd, v = U.call(port, "POST", "/api/defiant/updates/install", raw_body=b'{"reboot_ok": true}', key=key)
            assert st == 403 and v == {"error": fleet.POLICY}
        st, hd, v = U.call(port, "POST", "/api/defiant/updates/install", raw_body=b'{"reboot_ok": tru', key="fixture-key")
        assert st == 400 and v == {"error": "bad json"}
        assert h.calls == [] and fleet._UPD["job"]["state"] == "idle" and fleet._UPD["auto_reboot"] is False


# ---------------------------------------------------------------- the worker is started as before

def test_the_worker_loop_follows_a_running_reboot_first():
    with story(reboot=rb_running(time.time()), next_at=0.0, want=True) as (h, sent):                 # the loop runs on the real clock
        ticks, real_tick = [], fleet._upd_tick

        def tick(now):
            ticks.append(real_tick(now))
            if len(ticks) == 2:
                raise KeyboardInterrupt
            return ticks[-1]
        wake = mock.Mock()
        waits = []
        wake.wait.side_effect = lambda t: waits.append(t)
        with mock.patch.object(fleet, "_UPD_WAKE", wake), mock.patch.object(fleet, "_upd_tick", tick):
            try:
                fleet.defiant_updates_worker()
            except KeyboardInterrupt:
                pass
        assert waits == [fleet.REBOOT_POLL_S] and h.scripts() == ["REBOOT_POLL", "REBOOT_POLL"]


# ---------------------------------------------------------------- everything together

class SimHost:
    """A made-up Defiant with a clock: ssh fails while it is down (or now and then, or with a timeout); a reboot that it accepted
    happens three seconds later, takes it down for a while and gives it another boot id, unless it is set to ignore it."""

    def __init__(self, rnd, clock):
        self.rnd, self.clock, self.calls = rnd, clock, []
        self.boot_i, self.down_until, self.reboot_at, self.unit_until = 0, 0.0, None, None
        self.apt_busy = self.never = False
        self.flaky, self.flag, self.rc = 0.0, "yes", 0

    def __call__(self, script, timeout, connect=8):
        now = self.clock.t
        if self.reboot_at is not None and now >= self.reboot_at:
            self.reboot_at = None
            if not self.never:
                self.down_until, self.boot_i = now + self.rnd.choice([20, 60, 400, 2000]), self.boot_i + 1
        names = {fleet.REBOOT_START: "REBOOT_START", fleet.REBOOT_POLL: "REBOOT_POLL", fleet.UPD_POLL: "UPD_POLL", fleet.UPD_SCAN: "UPD_SCAN"}
        self.calls.append(names.get(script, "install start"))
        if now < self.down_until or self.rnd.random() < self.flaky:
            if self.rnd.random() < 0.2:
                raise subprocess.TimeoutExpired(["ssh"], timeout)
            return done("", "ssh: connect to host defiant.example port 22: Connection refused", 255)
        unit, boot = self.unit_until is not None and now < self.unit_until, "%08x-0000-4000-8000-%012x" % (self.boot_i + 1, self.boot_i + 1)
        if script == fleet.REBOOT_START:
            if unit or self.reboot_at is not None or self.apt_busy:
                return done("FLEET-BUSY %s\n" % ("install" if unit else "reboot" if self.reboot_at is not None else "apt"))
            self.reboot_at = now + 3
            return done(start_out(boot))
        if script == fleet.REBOOT_POLL:
            return done(poll_out(boot))
        if script == fleet.UPD_POLL:
            end = self.unit_until or now
            return done(probe("active") if unit else probe("inactive", "2", "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", self.flag,
                                                           ["FLEET-APT-START %d" % (end - 30), "FLEET-APT-EXIT %d %d" % (self.rc, end)]))
        if script == fleet.UPD_SCAN:
            return done(scan_text(fixture("apt-sim-phased.txt")))
        if unit:                                                                                 # the install start
            return done("FLEET-BUSY\n")
        self.unit_until, self.flag, self.rc = now + self.rnd.choice([10, 30, 90]), self.rnd.choice(["yes", "yes", "no", ""]), self.rnd.choice([0, 0, 0, 100])
        return done("Running as unit: fleet-apt-upgrade.service\nFLEET-RC:0\n")


def test_a_random_walk_keeps_the_rules():
    """Seeded random walks over the whole machinery (button, install from a page that announces the reboot or from one that does not, scan
    request, worker ticks, a host that goes down, never reboots, is flaky or busy, a Fleet restart like a crash, a Telegram that fails),
    with a clock and a made-up host. After every step the rules that always hold are checked; the walks are long enough to reach a
    reboot, an automatic one, an install that needed one without announcing it, a finished one and a failed one many times."""
    seen = {"button": 0, "auto": 0, "silent": 0, "done": 0, "failed": 0, "unsure": 0, "gap": 0}
    for seed in range(40):
        rnd, clock, sent = random.Random(seed), types.SimpleNamespace(t=1_000_000.0), []
        host = SimHost(rnd, clock)
        with fleet_state(), mock.patch.object(fleet, "_def_ssh", host), mock.patch.object(fleet.time, "time", lambda: clock.t), \
                mock.patch.object(fleet, "_tg_send", lambda text: (sent.append(text), rnd.random() < 0.8)[1]), redirect_stdout(io.StringIO()):
            u, failures, starts, dones, clicked = fleet._UPD, 0, 0, 0, False                     # clicked: a keyed install click of this Fleet started the unit cleanly, its page
            u["next_at"] = clock.t                                                               # announced the reboot, and the unit has not ended (kept here, not read from the state)
            for i in range(150):
                where, before, job_was, n = "seed %d step %d" % (seed, i), dict(u["reboot"]), u["job"]["state"], len(host.calls)
                running = before["state"] == "running"
                held = any(0 <= clock.t - t < fleet.REBOOT_GAP for t in (u["reboot_unsure"], before["started_at"] or 0))      # the last request that may have taken effect is recent
                act = rnd.choice(["tick"] * 8 + ["wait", "button", "badkey", "install", "scan", "down", "apt", "never", "flaky", "heal", "restart"])
                if act == "tick":
                    clock.t += rnd.choice([0.5, 1, 5, 5, 5, 20])
                    fleet._upd_tick(clock.t)
                    new = host.calls[n:]
                    if running:
                        assert set(new) <= {"REBOOT_POLL"}, (where, new)                       # while a reboot runs nothing else is asked of Defiant
                    ended = job_was == "running" and u["job"]["state"] != "running"
                    needs = ended and u["job"]["state"] == "done" and u["job"]["reboot_required"] is True
                    wanted = needs and clicked
                    assert new.count("REBOOT_START") == int(bool(wanted)), (where, new)       # only the end of an announced, clicked install that needs one starts a reboot
                    if ended:
                        seen["auto"] += int(bool(wanted))
                        seen["silent"] += int(bool(needs and not clicked))
                        assert not needs or clicked or u["reboot"] == before, where              # an install that needs a reboot but did not announce it leaves the reboot alone
                        clicked = False
                        assert not wanted or (u["reboot"]["auto"] is True and u["reboot"]["state"] in ("running", "failed")), where
                elif act == "wait":
                    clock.t += rnd.choice([30, 100, 200, 500, 1000, 2000])
                elif act == "button":
                    try:
                        fleet.defiant_updates_reboot("fixture-key")
                        seen["button"] += 1
                        assert not running and job_was != "running" and not u["busy"] and not held, where
                    except PermissionError as e:
                        assert str(e) in ("A reboot is already running.", "An install is running. Wait until it is done.", "A scan is running. Try again in a moment.",
                                          GAP_TEXT, APT_BUSY, "An install is running. Wait until it is done."), (where, str(e))
                        assert held or str(e) != GAP_TEXT, where                                    # the gap is only ever the reason when a request is that recent
                        seen["gap"] += int(str(e) == GAP_TEXT)
                    except RuntimeError as e:
                        assert str(e) in (DID_NOT_ACCEPT, UNSURE), (where, str(e))
                        assert (u["reboot_unsure"] == clock.t) == (str(e) == UNSURE), (where, str(e))                  # only a start that said nothing is marked
                        seen["unsure"] += int(str(e) == UNSURE)
                    assert not running or len(host.calls) == n, where                           # nothing is asked of Defiant while a reboot runs
                elif act == "badkey":
                    for bad in (None, "wrong"):
                        assert outcome(bad) == (403, fleet.POLICY), where
                    assert len(host.calls) == n, where
                elif act == "install":
                    ok = rnd.random() < 0.7                                                      # the page announced the reboot, or did not (an older page, a script)
                    try:
                        fleet.defiant_updates_install("fixture-key", reboot_ok=ok)
                        clicked = ok                                                             # started cleanly: promised exactly when the page announced it
                    except (PermissionError, OSError) as e:
                        assert not running or str(e) == "A reboot is running.", (where, str(e))
                        if job_was != "running" and u["job"]["state"] == "running":              # a unit that Fleet found or lost track of: followed, never rebooted after
                            clicked = False
                elif act == "scan":
                    res = fleet.defiant_updates_scan_request()
                    assert not running or res == {"ok": True, "started": False, "reason": "rebooting"}, where
                elif act == "down":
                    host.down_until = clock.t + rnd.choice([5, 30, 120, 600, 1200])
                elif act == "apt":
                    host.apt_busy = not host.apt_busy
                elif act == "never":
                    host.never = not host.never
                elif act == "flaky":
                    host.flaky = rnd.choice([0.0, 0.3, 0.9])
                elif act == "heal":
                    host.flaky, host.down_until = 0.0, 0.0
                else:                                                                            # a Fleet restart, as a crash does it: only what was saved comes back
                    hold = u["reboot_unsure"]
                    u.clear()
                    u.update(fleet._upd_fresh())
                    fleet.defiant_updates_load()
                    assert u["reboot_unsure"] == hold, where                                     # the hold after a start that got no answer outlives the restart
                    u["next_at"], clicked = clock.t, False
                r = u["reboot"]
                assert u["reboot_starting"] is False and u["starting"] is False, where        # nothing is left locked
                assert r["state"] == "idle" or fleet._upd_reboot_ok(r), (where, r)
                if os.path.exists(fleet.UPD_FILE):
                    with open(fleet.UPD_FILE) as f:
                        saved = json.load(f)
                    assert saved["reboot"] == r and saved["reboot_unsure"] == u["reboot_unsure"], where       # what is on disk is what is in memory, the hold included
                else:
                    assert u["reboot_unsure"] == 0.0, where
                assert u["auto_reboot"] == (clicked and u["job"]["state"] == "running"), where   # the mark belongs to the one announced click, until its unit ends
                assert "boot" not in fleet.defiant_updates_view()["reboot"], where
                if r["state"] == "done" and before["state"] == "running":
                    seen["done"] += 1
                    dones += 1
                    assert u["want"] is True and u["next_at"] <= clock.t, where                  # a finished reboot makes a rescan due at once
                if r["state"] == "failed" and r != before:
                    failures += 1
                starts += int(r["state"] == "running" and before["state"] != "running")
            seen["failed"] += failures
            assert len([m for m in sent if m.startswith("Defiant: reboot failed")]) == failures, seed      # one message for every failed reboot, no more
            titles = [a[0] for a in U.AUDIT]                                                     # and one list entry for each start, end and failure, no more
            assert [titles.count(t) for t in ("Defiant reboot started", "Defiant rebooted", "Defiant reboot failed")] == [starts, dones, failures], (seed, titles)
            assert len(titles) == starts + dones + failures, seed
            check_entries(U.AUDIT)
    assert all(seen.values()), seen                                                              # the walks did reach every part



# ---------------------------------------------------------------- single flight under real threads

class YieldLock:
    """The state lock, but another thread gets in right after every release: a check and a claim that are not one step show up."""

    def __init__(self, real):
        self.real = real

    def acquire(self, *a, **k):
        return self.real.acquire(*a, **k)

    def release(self):
        self.real.release()

    def __enter__(self):
        self.real.acquire()
        return self

    def __exit__(self, *a):
        self.real.release()
        time.sleep(0.02)
        return False


def at_once(*fns):
    """The functions, each in its own thread, started together; their results (or exceptions) in order."""
    res, bar = [None] * len(fns), threading.Barrier(len(fns))

    def go(i, fn):
        bar.wait()
        try:
            res[i] = fn()
        except BaseException as e:
            res[i] = e
    ts = [threading.Thread(target=go, args=(i, f)) for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert not any(t.is_alive() for t in ts), "a thread hung"
    return res


def slow_host(h, asked, delay=0.15):
    """The made-up host, noting which starts reach it and slow enough that two requests overlap."""
    def ssh(script, timeout, connect=8):
        if script == fleet.REBOOT_START or script in (fleet.upd_start(True), fleet.upd_start(False)):
            asked.append("reboot" if script == fleet.REBOOT_START else "install")
            time.sleep(delay)
        return h(script, timeout, connect)
    return ssh


def test_two_clicks_at_once_ask_defiant_once():
    """Check and claim are one step: of two clicks on the button, or a click on the button and one on Install updates, that arrive together
    only one asks Defiant, and the other gets its 409."""
    for second in ("button", "install"):
        with story() as (h, sent):
            asked = []
            click = (lambda: outcome()) if second == "button" else (lambda: fleet.defiant_updates_install("fixture-key", reboot_ok=True))
            with mock.patch.object(fleet, "_def_ssh", slow_host(h, asked)), mock.patch.object(fleet, "_UPD_LOCK", YieldLock(fleet._UPD_LOCK)):
                res = at_once(lambda: outcome(), click)
            assert len(asked) == 1, (second, asked, res)
            refused = [r for r in res if isinstance(r, PermissionError) or (isinstance(r, tuple) and r[0] == 409)]
            assert len(refused) == 1, (second, res)


def test_a_click_right_after_the_end_of_an_install_does_not_start_a_second_reboot():
    """The worker records the end of an install that needs a reboot and starts it itself; a click on the button that comes in right after
    the end was recorded must find the start claimed."""
    with story() as (h, sent):
        asked = []
        t0 = h.click_install()

        def click_after_done():
            end = time.time() + 5
            while time.time() < end and fleet._UPD["job"]["state"] != "done":
                time.sleep(0.0005)
            return outcome()
        with mock.patch.object(fleet, "_def_ssh", slow_host(h, asked)), mock.patch.object(fleet, "_UPD_LOCK", YieldLock(fleet._UPD_LOCK)):
            res = at_once(lambda: fleet._upd_tick(t0 + 100), click_after_done)
        assert asked == ["reboot"], (asked, res)
        assert res[1][0] == 409, res


# ---------------------------------------------------------------- every save of an unsure start carries the hold

def test_every_save_after_the_hold_is_written_ahead_carries_it():
    """Once the time of a start is written ahead, no later save of that start may lack it: a restart of Fleet between two saves would
    lose the hold. Watched on both paths and for both answers that say nothing (no answer at all; an answer cut off after the boot id)."""
    for path in ("button", "auto"):
        for reply in (subprocess.TimeoutExpired(["ssh"], 30), "FLEET-BOOT %s\n" % BOOT_A):
            with story() as (h, sent):
                h.start_reply = reply
                holds, real = [], fleet._upd_save_locked

                def spy():
                    real()
                    holds.append(json.loads(read(fleet.UPD_FILE))["reboot_unsure"])
                if path == "button":
                    t0 = 50000.0
                    with mock.patch.object(fleet, "_upd_save_locked", spy), mock.patch.object(fleet.time, "time", lambda: t0):
                        assert outcome() == (502, UNSURE)
                else:
                    t0 = h.click_install() + 100
                    with mock.patch.object(fleet, "_upd_save_locked", spy):
                        fleet._upd_tick(t0)
                assert t0 in holds, (path, reply, holds)                                    # the save that is written ahead carries it
                assert all(v == t0 for v in holds[holds.index(t0):]), (path, reply, holds)   # and no save after that one drops it


# ---------------------------------------------------------------- hygiene

def test_the_new_texts_are_plain_and_public():
    src = read(os.path.join(os.path.dirname(HERE), "fleet.py"))
    a = src.index("# ---------------------------------------------------------------- Defiant updates")
    b = src.index("# ---------------------------------------------------------------- Computer (knowledge chat)")
    for where, text in (("the Defiant section of fleet.py", src[a:b]), ("this file", read(os.path.abspath(__file__)))):
        assert EM not in text, where                                                         # no em dash in a comment, a doc or a string
        assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text), where                      # no address
    for text in (GIVEUP_ERROR, NO_REBOOT_ERROR, DID_NOT_ACCEPT, UNSURE, UNSURE_AUTO, GAP_TEXT, APT_BUSY, "A reboot is already running.", "A reboot is running.",
                 "An install is running. Wait until it is done.", "A scan is running. Try again in a moment.",
                 fleet.reboot_fail_text(rb_failed()), fleet.REBOOT_START, fleet.REBOOT_POLL, SAME_BOOT_ENTRY, APT_AUTO_ENTRY,
                 "Defiant reboot started", "Started with the Reboot Defiant button.", "Started by Fleet after the install.", "Defiant rebooted",
                 "Defiant answered again with a new boot after 75 seconds.", "Defiant reboot failed"):
        assert EM not in text and "restart" not in text.lower()                                 # "reboot" is the word of this action


# ---------------------------------------------------------------- runner

def main():
    # safety nets: no real subprocess, and never the real saved-state file or Messages list, whatever a test forgets to patch
    tmp = tempfile.mkdtemp(prefix="fleet-reboot-run-")
    U.point_unpatched_files_away(tmp)
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    with mock.patch.object(subprocess, "run", U._forbidden), mock.patch.object(subprocess, "Popen", U._forbidden):
        for name, fn in tests:
            try:
                fn()
                print("ok    " + name)
            except Exception:
                failed.append(name)
                print("FAIL  " + name)
                traceback.print_exc()
    if U.unpatched_files_written():
        failed.append("(a test wrote the unpatched state file or Messages list)")
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d tests, %d failed%s" % (len(tests), len(failed), ": " + ", ".join(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
