#!/usr/bin/env python3
"""Tests for the Defiant updates backend in fleet.py: the apt output parser, classification, alerts and their
dedupe, the install job, the saved state, the worker and the endpoints.

Plain asserts, standard library only:   python3 test/test_defiant_updates.py

Nothing here touches a real host, a real chat, llama-swap or a model: ssh, the Telegram sender and the Starfleet
key check are replaced, the made-up host is defiant.example, and the key is the string fixture-key. Any real
subprocess call that slips through fails the run. The shell scripts are only ever run locally against stub
functions (bash) or parsed with `sh -n`; they never reach a package manager or systemd.

Fixtures (test/fixtures):
  apt-sim-large.txt   real output of a simulated full-upgrade, trimmed to 20 packages (its closing summary
                      line was adjusted to the kept lines)
  apt-sim-phased.txt  real output of a simulated full-upgrade with nothing installable and 4 packages held
                      back by phasing
"""
import http.client
import io
import json
import os
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
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
THREADS_AT_IMPORT = threading.active_count()
import fleet                                         # noqa: E402

_REAL_RUN, _REAL_POPEN = subprocess.run, subprocess.Popen       # only the local shell checks below use them


def real_run(*a, **kw):
    with mock.patch.object(subprocess, "run", _REAL_RUN), mock.patch.object(subprocess, "Popen", _REAL_POPEN):
        return _REAL_RUN(*a, **kw)


# ---------------------------------------------------------------- helpers

def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


def done(stdout="", stderr="", rc=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def P(name, to="2.0", frm="1.0", suites=("resolute-updates",), security=False, origin="Ubuntu", third=False):
    return {"name": name, "from": frm, "to": to, "suites": list(suites), "origin": origin, "security": security,
            "third_party": third}


def S(packages=(), at=1000.0, count=None, remove=(), deferred=(), reboot=False, reboot_pkgs=(), error=None, error_at=None):
    pk = list(packages)
    return {"at": at, "error": error, "error_at": error_at, "count": len(pk) if count is None else count,
            "security": sum(p["security"] for p in pk), "packages": pk, "remove": list(remove),
            "deferred": list(deferred), "reboot_required": reboot, "reboot_pkgs": list(reboot_pkgs)}


SEC = "Ubuntu:26.04/resolute-updates, Ubuntu:26.04/resolute-security [arm64]"
UPD = "Ubuntu:26.04/resolute-updates [arm64]"


def inst(name, old, new, rel=SEC):
    return "Inst %s%s (%s %s)" % (name, " [%s]" % old if old else "", new, rel)


def sim_text(lines, summary=None):
    if summary is None:
        up = sum(1 for l in lines if l.startswith("Inst ") and "[" in l.split("(")[0])
        new = sum(1 for l in lines if l.startswith("Inst ")) - up
        rm = sum(1 for l in lines if l.startswith("Remv "))
        summary = "%d upgraded, %d newly installed, %d to remove and 0 not upgraded." % (up, new, rm)
    return "Reading package lists...\nCalculating upgrade...\n" + "\n".join(lines) + "\n" + summary


def scan_text(sim, rc=0, said="", reboot=False, pkgs=()):
    return ("banner before the markers\nFLEET-SCAN update %s\n%s\nFLEET-SCAN sim\n%s\nFLEET-SCAN reboot %s\n"
            "FLEET-SCAN rebootpkgs\n%s\nFLEET-SCAN end\n" % (rc, said, sim, "yes" if reboot else "no", "\n".join(pkgs)))


def probe(active="active", setup="", summary="", reboot="no", log=()):
    return "ACTIVE:%s\nSETUP:%s\nSUMMARY:%s\nREBOOT:%s\nLOG:\n%s" % (active, setup, summary, reboot, "".join(l + "\n" for l in log))


def running_job(started=1000.0, contact=None, log=()):
    return dict(fleet._upd_job_idle(), state="running", started_at=started,
                contact_at=started if contact is None else contact, log=list(log))


def fake_key_check(key):
    if key != "fixture-key":
        raise urllib.error.HTTPError(fleet.SWAP, 403, fleet.POLICY, {}, io.BytesIO(
            json.dumps({"error": {"message": fleet.POLICY}}).encode()))
    return {"ok": True}


@contextmanager
def fleet_state(host="defiant.example"):
    """A clean state with Defiant configured, the saved file in a temp dir and the key check replaced (the install
    uses computer_auth, which fails closed; key_check, which does not, must never be what protects it)."""
    fleet._UPD_WAKE.clear()
    with tempfile.TemporaryDirectory(prefix="fleet-upd-") as d, \
            mock.patch.object(fleet, "DEFIANT_HOST", host), \
            mock.patch.object(fleet, "UPD_FILE", os.path.join(d, "sub", "defiant-updates.json")), \
            mock.patch.object(fleet, "_UPD", fleet._upd_fresh()), \
            mock.patch.object(fleet, "computer_auth", fake_key_check), \
            mock.patch.object(fleet, "key_check", side_effect=AssertionError("the install must not rely on the fail-open key_check")):
        yield d


def lock_is_free():
    ok = fleet._UPD_LOCK.acquire(blocking=False)
    if ok:
        fleet._UPD_LOCK.release()
    return ok


def parse(*lines):
    return fleet.parse_apt_sim("\n".join(lines))


# ---------------------------------------------------------------- import and text hygiene

def test_import_has_no_side_effects():
    assert THREADS_AT_IMPORT == 1, "importing fleet started a thread"


def test_clean_caps_and_strips_control_characters():
    assert fleet._clean("a\x1b[31mb\x00c\x7fd", 99) == "a[31mbcd"
    assert fleet._clean("x" * 500, 10) == "x" * 10
    assert fleet._clean("tab\tand\nnewline", 99) == "tab\tand\nnewline"
    assert fleet._clean(ValueError("boom"), 99) == "boom"


# ---------------------------------------------------------------- parse_apt_sim

def test_parse_large_real_sample():
    r = fleet.parse_apt_sim(fixture("apt-sim-large.txt"))
    pk = {p["name"]: p for p in r["packages"]}
    assert len(r["packages"]) == len(pk) == 20 and r["summary"] == (20, 0, 0, 21) and r["remove"] == []
    libc = pk["libc6"]                      # two suites, Ubuntu, a security pocket among them
    assert libc == {"name": "libc6", "from": "2.39-0ubuntu8.9", "to": "2.39-0ubuntu8.12",
                    "suites": ["noble-updates", "noble-security"], "origin": "Ubuntu", "security": True, "third_party": False}
    assert pk["libperl5.38t64"]["to"] == "5.38.2-3.2ubuntu0.7"          # the trailing "[perl:arm64 ]" is not part of it
    assert pk["openjdk-8-jre"]["to"] == "8u514-b01~us1-0ubuntu1~24.04.1"      # nor is a trailing "[]"
    assert pk["snapd"]["security"] is False and pk["snapd"]["suites"] == ["noble-updates"]
    esm = pk["libgstreamer-plugins-bad1.0-0"]       # Ubuntu Pro archive: security, and still Ubuntu's own
    assert esm["suites"] == ["noble-apps-security"] and esm["security"] and not esm["third_party"] and esm["origin"] == "UbuntuESMApps"
    chrome = pk["google-chrome-stable"]
    assert (chrome["origin"], chrome["suites"], chrome["security"], chrome["third_party"]) == ("Google", ["stable"], False, True)
    nv = pk["nvidia-container-toolkit"]             # a label with a space, and a source that is only a host name
    assert (nv["origin"], nv["suites"], nv["third_party"]) == ("NVIDIA CUDA", ["developer.download.nvidia.com"], True)
    ts = pk["tailscale"]
    assert (ts["origin"], ts["suites"], ts["third_party"], ts["security"]) == ("Tailscale", ["pkgs.tailscale.com"], True, False)
    assert sum(p["security"] for p in r["packages"]) == 11 and sum(p["third_party"] for p in r["packages"]) == 5
    assert len(r["deferred"]) == 21 and "systemd" in r["deferred"] and "udev" in r["deferred"]
    assert "linux-image-6.17.0-1021-nvidia" not in r["deferred"]      # the autoremove block is not a deferred list
    assert "nvidia-firmware-580-580.159.03" not in r["deferred"]


def test_parse_phased_real_sample():
    r = fleet.parse_apt_sim(fixture("apt-sim-phased.txt"))
    assert r["packages"] == [] and r["remove"] == [] and r["summary"] == (0, 0, 0, 4)
    assert r["deferred"] == ["distro-info", "python3-distro-info", "python3-software-properties", "software-properties-common"]


def test_new_install_has_no_old_version():
    r = parse(inst("linux-image-7.0.0-20-generic", None, "7.0.0-20.20"), inst("linux-modules-7.0.0-20-generic", None, "7.0.0-20.20", UPD) + " []",
              "0 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.")
    a, b = r["packages"]
    assert a["from"] is None and a["to"] == "7.0.0-20.20" and a["security"] and a["suites"] == ["resolute-updates", "resolute-security"]
    assert b["from"] is None and b["security"] is False and r["summary"] == (0, 2, 0, 0)


def test_remove_kept_back_and_phased():
    r = parse("The following packages have been kept back:", "  held-one held-two",
              "The following upgrades have been deferred due to phasing:", "  phased-one", "  held-two",
              "The following packages will be REMOVED:", "  old-lib",
              "Remv old-lib [1.0-1]", "Remv old-two [2.0] [dep:arm64 ]", "Purg gone-one [3.1]", "Remv", "Remv ", "Purg   ",
              inst("foo", "1", "2"), "1 upgraded, 0 newly installed, 3 to remove and 3 not upgraded.")
    assert r["remove"] == ["old-lib", "old-two", "gone-one"]
    assert r["deferred"] == ["held-one", "held-two", "phased-one"]      # both headers, deduplicated, in order
    assert [p["name"] for p in r["packages"]] == ["foo"]


def test_unknown_shapes_are_skipped_and_nothing_is_invented():
    good = inst("good", "1.0", "2.0", UPD)
    garbage = ["Inst", "Inst ", "Inst foo", "Inst foo [1.0]", "Inst foo [1.0] (", "Inst foo [1.0] ()", "Inst foo [1.0] (not a version!!!!",
               "Inst foo:bar:baz [a b] (2 x)", "Inst \u00dcn\u00efcode [1.0] (2.0 Ubuntu:26.04/resolute-updates [arm64])",
               "Inst bad name [1.0] (2.0 x)", "Inst nul\x00name [1] (2 x)", "Inst foo [] (2.0 x)", "Inst foo [1.0] (  [arm64])",
               "ssh: connect to host defiant.example port 22: Connection refused", "E: Unable to correct problems",
               " leading space garbage", "The following", "The following packages are weird:", "  not-captured",
               "random:colon", "\x1b[31mred\x1b[0m", "Conf x (1 y [arm64])", "\ufffd\ufffd binary \ufffd", "x" * 5000,
               "Inst long [1] (2 " + "A" * 5000 + ")", "Remv", "Purg ", "0 upgraded"]
    r = fleet.parse_apt_sim("\r\n".join(garbage[:12] + [good] + garbage[12:]) + "\r\n")
    assert [p["name"] for p in r["packages"]] == ["good"], r["packages"]
    assert r["remove"] == [] and r["deferred"] == [] and r["summary"] is None
    assert set(r) == {"packages", "remove", "deferred", "summary"}


def test_empty_output():
    empty = {"packages": [], "remove": [], "deferred": [], "summary": None}
    assert fleet.parse_apt_sim("") == empty and fleet.parse_apt_sim("\n\n   \n\t\n") == empty


def test_remote_text_is_capped_and_cleaned():
    label, suite = "L" * 500, "s" * 500
    r = parse("Inst x [1] (2 %s:1.0/%s\x1b[2J [arm64])" % (label, suite))
    p = r["packages"][0]
    assert len(p["origin"]) == 60 and p["suites"] == ["s" * 80]
    assert parse("Inst x [1] (2 A:1.0/ab\x1bXcd [arm64])")["packages"][0]["suites"] == ["abXcd"]
    many = parse("Inst x [1] (2 " + ", ".join("A:1/suite%d" % i for i in range(30)) + " [arm64])")
    assert len(many["packages"][0]["suites"]) == 8
    names = parse("The following upgrades have been deferred due to phasing:", *["  " + " ".join("p%d-%d" % (i, j) for j in range(10)) for i in range(400)])
    assert len(names["deferred"]) == 300


# ---------------------------------------------------------------- classification

def test_classification():
    def one(rel):
        return parse("Inst x [1] (2 %s)" % rel)["packages"][0]
    assert one("Ubuntu:26.04/resolute-security [arm64]")["security"]
    assert one("UbuntuESMApps:26.04/resolute-apps-security [arm64]")["security"]
    assert one("UbuntuESM:26.04/resolute-infra-security [arm64]")["security"]
    assert not one("Ubuntu:26.04/resolute-updates [arm64]")["security"]
    assert not one("Ubuntu:26.04/resolute-security-extras [arm64]")["security"]            # must END with -security
    assert not one("Example:repo.example [arm64]")["security"]
    assert one("Ubuntu:26.04/resolute-updates, Ubuntu:26.04/resolute-security [arm64]")["security"]
    for rel in ("Ubuntu:26.04/resolute-updates [arm64]", "UbuntuESMApps:26.04/resolute-apps-security [arm64]"):
        assert one(rel)["third_party"] is False
    third = one("Example Repo:stable [arm64]")
    assert third["third_party"] is True and third["origin"] == "Example Repo" and third["suites"] == ["stable"]
    assert one("Ubuntu Toolchain:stable [arm64]")["third_party"] is True        # only Ubuntu, UbuntuESM* are Ubuntu's own
    mixed = one("Example:stable, Ubuntu:26.04/resolute-updates [arm64]")
    assert mixed["third_party"] is False and mixed["origin"] == "Ubuntu"
    bare = one("26.04/resolute-updates [arm64]")                    # no label at all: nothing is claimed
    assert bare["origin"] is None and bare["third_party"] is False and bare["suites"] == ["resolute-updates"]
    none = parse("Inst x [1] (2  [arm64])")["packages"][0]
    assert none["origin"] is None and none["suites"] == [] and not none["security"] and not none["third_party"]


# ---------------------------------------------------------------- parse_scan_output

def test_scan_output_full_pipeline():
    s = fleet.parse_scan_output(scan_text(fixture("apt-sim-large.txt"), reboot=True, pkgs=["linux-image-7.0.0-20-generic", "libc6", "libc6", "bad name"]))
    assert s["count"] == 20 and s["security"] == 11 and len(s["packages"]) == 20
    names = [p["name"] for p in s["packages"]]
    sec_names = [p["name"] for p in s["packages"] if p["security"]]
    assert [p["security"] for p in s["packages"]] == [True] * 11 + [False] * 9           # security first ...
    assert names[:11] == sorted(sec_names) and names[11:] == sorted(names[11:])            # ... then by name
    assert s["reboot_required"] is True and s["reboot_pkgs"] == ["linux-image-7.0.0-20-generic", "libc6"]
    assert s["remove"] == [] and len(s["deferred"]) == 21
    assert set(s) == {"count", "security", "packages", "remove", "deferred", "reboot_required", "reboot_pkgs"}
    p = fleet.parse_scan_output(scan_text(fixture("apt-sim-phased.txt")))
    assert p["count"] == 0 and p["security"] == 0 and p["packages"] == [] and len(p["deferred"]) == 4
    assert p["reboot_required"] is False and p["reboot_pkgs"] == []


def test_scan_output_failures_keep_a_reason():
    ok = sim_text([inst("a", "1", "2")])

    def fails(text, *words):
        try:
            fleet.parse_scan_output(text)
        except ValueError as e:
            assert all(w in str(e) for w in words), (str(e), words)
            assert len(str(e)) <= 200
            return
        raise AssertionError("no ValueError for " + text[:60])
    fails(scan_text(ok, rc=100, said="W: noise\nE: Could not get lock /var/lib/apt/lists/lock. It is held by process 9 (apt-get)"),
          "apt-get update failed (exit 100)", "Could not get lock")
    fails(scan_text(ok, rc=1, said="sudo: a password is required"), "exit 1", "a password is required")
    fails(scan_text(ok, rc=124, said=""), "exit 124")
    fails(scan_text(ok).split("FLEET-SCAN end")[0], "incomplete")                  # cut off: the end marker never came
    fails("", "incomplete")
    fails(scan_text("E: Unable to correct problems, you have held broken packages."), "did not finish", "held broken packages")
    fails(scan_text(""), "did not finish")
    fails(scan_text(sim_text([], "3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.")), "not understood", "3")
    fails(scan_text(sim_text(["Inst new format that apt may print one day"], "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.")), "not understood")


def test_scan_count_cap_and_order():
    lines = [inst("pkg%04d" % i, "1", "2", SEC if i % 7 == 0 else UPD) for i in range(650)]
    s = fleet.parse_scan_output(scan_text(sim_text(lines)))
    assert s["count"] == 650 and len(s["packages"]) == fleet.UPD_MAX == 500           # count stays the true number
    assert s["security"] == 93 and all(p["security"] for p in s["packages"][:93]) and not any(p["security"] for p in s["packages"][93:])
    assert [p["name"] for p in s["packages"][:93]] == sorted(p["name"] for p in s["packages"][:93])
    # apt says 5, only 3 lines were understood: the count is apt's, the list is what was read
    short = fleet.parse_scan_output(scan_text(sim_text([inst("a", "1", "2"), inst("b", "1", "2"), inst("c", "1", "2")],
                                                       "5 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.")))
    assert short["count"] == 5 and len(short["packages"]) == 3


# ---------------------------------------------------------------- signature, text, decision

def test_signature():
    a = S([P("b", "2", security=True), P("a", "3", security=True), P("c", "9")])
    b = S([P("a", "3", security=True), P("c", "10"), P("b", "2", security=True)])
    assert fleet.upd_signature(a) == fleet.upd_signature(b) == "a=3,b=2"            # order and non-security packages do not matter
    assert fleet.upd_signature(S([P("a", "4", security=True), P("b", "2", security=True)])) != "a=3,b=2"      # a new version is a new alert
    assert fleet.upd_signature(S([P("a", "3", security=True)], reboot=True, reboot_pkgs=["z", "y"])) == "a=3,reboot:y,reboot:z"
    assert fleet.upd_signature(S(reboot=True)) == "reboot:"
    assert fleet.upd_signature(S([P("c")])) == ""                                    # not critical
    assert fleet.upd_signature(S([P("c")], reboot_pkgs=["y"])) == ""                  # the packages only count when a reboot is due


def test_alert_headlines():
    one = S([P("a", security=True)])
    assert fleet.upd_alert_text(one).splitlines()[0] == "Defiant: 1 security update pending"
    three = S([P("a", security=True), P("b", security=True), P("c", security=True)])
    assert fleet.upd_alert_text(three).splitlines()[0] == "Defiant: 3 security updates pending"
    assert fleet.upd_alert_text(S(reboot=True)).splitlines()[0] == "Defiant: reboot required"
    both = S([P("a", security=True), P("b", security=True), P("c", security=True)], reboot=True, reboot_pkgs=["linux-image-7"])
    assert fleet.upd_alert_text(both).splitlines()[0] == "Defiant: 3 security updates pending, reboot required"
    assert fleet.upd_alert_text(both).splitlines()[1] == ""


def test_alert_detail_lines():
    pk = [P("zlib1g", "1.3-2", "1.3-1", ["resolute-updates", "resolute-security"], security=True),
          P("openssl", "3.5.1-2", "3.5.1-1", ["resolute-security"], security=True),
          P("linux-image-7", "7.0-20", None, ["resolute-updates", "resolute-security"], security=True),
          P("tailscale", "1.104.7", "1.104.5", ["pkgs.example"], origin="Tailscale", third=True), P("curl")]
    t = fleet.upd_alert_text(S(pk, count=9, reboot=True, reboot_pkgs=["linux-image-7", "libc6"]))
    lines = t.splitlines()
    assert lines[2:5] == ["zlib1g 1.3-1 -> 1.3-2 (resolute-security)",           # the security suite, not the first one
                          "openssl 3.5.1-1 -> 3.5.1-2 (resolute-security)",
                          "linux-image-7 -> 7.0-20 (resolute-security)"]          # a new install has no old version
    assert "tailscale" not in t and "curl" not in t                             # only security packages are listed ...
    assert "Other updates: 6" in lines                                           # ... the rest is a count (9 - 3)
    assert "Reboot required: linux-image-7, libc6" in lines and lines[-1] == "Install: Starfleet, Systems, Defiant, Install updates"
    assert not any(l.startswith("+") for l in lines)
    assert "Other updates" not in fleet.upd_alert_text(S([P("a", security=True)]))


def test_alert_text_limits():
    many = [P("security-package-with-a-fairly-long-name-%02d" % i, "1:2026.1.1+really+long+upstream+version-%d" % i, "1:2025.1.1+long-%d" % i,
              ["resolute-updates", "resolute-security"], security=True) for i in range(60)]
    t = fleet.upd_alert_text(S(many, count=140, reboot=True, reboot_pkgs=["linux-image-7.0.0-%d-generic-with-a-long-name" % i for i in range(60)]))
    lines = t.splitlines()
    assert len(lines[0]) <= 100 and len(t) < 3500, (len(lines[0]), len(t))
    assert sum(1 for l in lines if "->" in l) == 25 and "+35 more" in lines and "Other updates: 80" in lines
    worst = [P("n" * 120, "v" * 120, "w" * 120, ["s" * 100 + "-security"], security=True) for _ in range(30)]
    t = fleet.upd_alert_text(S(worst, count=30, reboot=True, reboot_pkgs=["r" * 100] * 50))
    assert len(t) < 3500 and len(t.splitlines()[0]) <= 100
    assert "\u2014" not in t and "\u2013" not in t and "<" not in t                  # no dashes, plain text
    huge = S([P("a", security=True)], count=10 ** 9)
    huge["security"] = 10 ** 9
    assert len(fleet.upd_alert_text(huge).splitlines()[0]) <= 100


def test_fail_text():
    log = ["line %d" % i for i in range(40)]
    t = fleet.upd_fail_text(dict(fleet._upd_job_idle(), state="failed", exit=100, log=log))
    assert t.splitlines()[0] == "Defiant: update install failed (exit 100)" and t.splitlines()[1] == ""
    assert t.splitlines()[2:] == log[-25:] and len(t) < 3500
    assert fleet.upd_fail_text(dict(fleet._upd_job_idle(), exit=None, log=["x" * 900]))[:30] == "Defiant: update install failed"
    assert len(fleet.upd_fail_text(dict(fleet._upd_job_idle(), exit=2, log=["x" * 900] * 40))) < 3500
    hinted = fleet.upd_fail_text(dict(fleet._upd_job_idle(), exit=100, log=log[:30] + ["E: Packages need to be removed but remove is disabled.",
                                                                                   "FLEET-APT-EXIT 100 1500", fleet.UPD_NO_REMOVE_HINT]))
    assert hinted.splitlines()[-1] == "Fleet: apt would remove packages, nothing was changed. Rescanning now: check the list, then install again."
    assert hinted.splitlines()[0] == "Defiant: update install failed (exit 100)" and len(hinted) < 3500       # the hint is not cut short by the line cap
    assert len(fleet.UPD_NO_REMOVE_HINT) <= 110


def test_alert_decision():
    crit, other = S([P("a", "3", security=True)]), S([P("a", "4", security=True)])
    sig = fleet.upd_signature(crit)
    assert fleet.upd_alert_decision(crit, None) == "send" and fleet.upd_alert_decision(crit, "") == "send"
    assert fleet.upd_alert_decision(crit, sig) is None                                       # already sent
    assert fleet.upd_alert_decision(other, sig) == "send"                                    # something new
    assert fleet.upd_alert_decision(S([P("c")]), sig) == "clear"                             # not critical any more
    assert fleet.upd_alert_decision(S([P("c")]), None) is None
    assert fleet.upd_alert_decision(S(reboot=True), None) == "send"
    assert fleet.upd_alert_decision(S(reboot=True, reboot_pkgs=["k"]), "reboot:k") is None
    # fewer entries than were alerted (some got installed) is no news; a new package or a new version is
    three = S([P("a", "1", security=True), P("b", "1", security=True), P("c", "1", security=True)])
    all3 = fleet.upd_signature(three)
    assert fleet.upd_alert_decision(S([P("c", "1", security=True)]), all3) is None
    assert fleet.upd_alert_decision(S([P("b", "1", security=True), P("c", "1", security=True)]), all3) is None
    assert fleet.upd_alert_decision(S([P("c", "2", security=True)]), all3) == "send"                # c has a new version
    assert fleet.upd_alert_decision(S([P("c", "1", security=True), P("d", "1", security=True)]), all3) == "send"
    assert fleet.upd_alert_decision(S(reboot=True, reboot_pkgs=["k"]), all3) == "send"              # a reboot is new, whatever the packages did
    assert fleet.upd_alert_decision(S([P("c", "1", security=True)], reboot=True, reboot_pkgs=["k"]), "c=1,reboot:k") is None
    assert fleet.upd_alert_decision(S([P("c", "1", security=True)], reboot=True, reboot_pkgs=["k", "m"]), "c=1,reboot:k") == "send"   # a new reason for it


def test_dedupe_through_notify():
    crit = S([P("a", "3", security=True)])
    with fleet_state():
        sent, ok = [], [True]
        with mock.patch.object(fleet, "_tg_send", lambda text: (sent.append(text), ok[0])[1]):
            fleet._upd_notify(crit)
            assert len(sent) == 1 and sent[0].startswith("Defiant: 1 security update pending")
            assert fleet._UPD["alerted"]["sig"] == "a=3"
            fleet._upd_notify(crit)                                                           # the next hourly scan
            assert len(sent) == 1
            with open(fleet.UPD_FILE) as f:                                                   # a restart: the file remembers
                saved = json.load(f)
            assert saved["alerted"]["sig"] == "a=3"
            fleet._UPD.update(fleet._upd_fresh())
            fleet.defiant_updates_load()
            fleet._upd_notify(crit)
            assert len(sent) == 1 and fleet._UPD["alerted"]["sig"] == "a=3"
            fleet._upd_notify(S([P("a", "4", security=True)]))                                 # a new version: alert again
            assert len(sent) == 2 and fleet._UPD["alerted"]["sig"] == "a=4"
            fleet._upd_notify(S([P("c")]))                                                    # cleared ...
            assert fleet._UPD["alerted"] == {} and len(sent) == 2
            fleet._upd_notify(S([P("c")]))                                                    # (nothing to clear twice)
            fleet._upd_notify(S([P("a", "4", security=True)]))                                 # ... so the same one alerts again
            assert len(sent) == 3
            fleet._upd_notify(S(reboot=True, reboot_pkgs=["linux-image-7"]))                  # a reboot alone is critical
            assert len(sent) == 4 and sent[-1].startswith("Defiant: reboot required")
            # three alerted, two get installed (unattended-upgrades): the one left is not announced again, a new one is
            fleet._upd_notify(S([P("a", "1", security=True), P("b", "1", security=True), P("c", "1", security=True)]))
            assert len(sent) == 5 and sent[-1].startswith("Defiant: 3 security updates pending") and fleet._UPD["alerted"]["sig"] == "a=1,b=1,c=1"
            fleet._upd_notify(S([P("c", "1", security=True)]))
            assert len(sent) == 5 and fleet._UPD["alerted"]["sig"] == "a=1,b=1,c=1"
            fleet._upd_notify(S([P("c", "1", security=True), P("d", "1", security=True)]))
            assert len(sent) == 6 and sent[-1].startswith("Defiant: 2 security updates pending") and fleet._UPD["alerted"]["sig"] == "c=1,d=1"
        # a failed send is not remembered: it is retried at the next scan
        fleet._UPD["alerted"] = {}
        sent.clear()
        ok[0] = False
        with mock.patch.object(fleet, "_tg_send", lambda text: (sent.append(text), ok[0])[1]):
            fleet._upd_notify(crit)
            assert len(sent) == 1 and fleet._UPD["alerted"] == {}
            fleet._upd_notify(crit)
            assert len(sent) == 2
            ok[0] = True
            fleet._upd_notify(crit)
            assert len(sent) == 3 and fleet._UPD["alerted"]["sig"] == "a=3"
            fleet._upd_notify(crit)
            assert len(sent) == 3


def test_the_lock_is_free_while_telegram_blocks():
    with fleet_state():
        seen = []
        with mock.patch.object(fleet, "_tg_send", lambda text: (seen.append(lock_is_free()), True)[1]):
            fleet._upd_notify(S([P("a", security=True)]))
        assert seen == [True]


# ---------------------------------------------------------------- Telegram sender

def test_telegram_sender():
    text = "Defiant: 1 security update pending\n\nlibc6 1 -> 2 (resolute-security)"
    calls = []

    def run_ok(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=0, stdout=b"token=SECRET", stderr=b"https://api.example/botSECRET/sendMessage")
    buf = io.StringIO()
    with mock.patch.object(fleet.subprocess, "run", run_ok), redirect_stdout(buf):
        assert fleet._tg_send(text) is True
    argv, kw = calls[0]
    assert argv == ["/usr/bin/python3", fleet.TELEGRAM_SEND, text] and kw["timeout"] == 40 and kw["stdin"] == subprocess.DEVNULL
    assert buf.getvalue() == ""
    if "FLEET_TELEGRAM_SEND" not in os.environ:
        assert fleet.TELEGRAM_SEND == os.path.expanduser("~/telegram-tool/send_progress.py")
    with mock.patch.object(fleet, "TELEGRAM_SEND", "/made/up/send.py"), mock.patch.object(fleet.subprocess, "run", run_ok):
        fleet._tg_send("x")
    assert calls[-1][0][1] == "/made/up/send.py"
    for outcome in (types.SimpleNamespace(returncode=1, stdout=b"token=SECRET", stderr=b"botSECRET"),
                    subprocess.TimeoutExpired(["x"], 40), FileNotFoundError("/usr/bin/python3"), OSError("no")):
        def run(argv, outcome=outcome, **kw):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        buf = io.StringIO()
        with mock.patch.object(fleet.subprocess, "run", run), redirect_stdout(buf):
            assert fleet._tg_send(text) is False                   # and nothing was raised
        assert "SECRET" not in buf.getvalue() and text not in buf.getvalue() and "Telegram message not sent" in buf.getvalue()


def test_ssh_helper_argv():
    seen = []
    with mock.patch.object(fleet, "DEFIANT_HOST", "defiant.example"), \
            mock.patch.object(fleet.subprocess, "run", lambda argv, **kw: (seen.append((argv, kw)), done("ok"))[1]):
        fleet._def_ssh("echo hi", 33)
    argv, kw = seen[0]
    assert argv == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "defiant.example", "echo hi"]
    assert kw["timeout"] == 33 and kw["stdin"] == subprocess.DEVNULL and kw["capture_output"] and kw["errors"] == "replace"


# ---------------------------------------------------------------- the install job

def test_job_probe_parse():
    obs = fleet.parse_job_probe(probe("activating", "12", "5 upgraded, 1 newly installed, 0 to remove and 3 not upgraded.", "yes",
                                      ["FLEET-APT-START 1763632000", "Setting up a (1) ...", "x" * 1000]))
    assert obs["active"] == "activating" and obs["setup"] == 12 and obs["summary"] == (5, 1) and obs["reboot"] is True
    assert obs["log"][0] == "FLEET-APT-START 1763632000" and len(obs["log"][2]) == 300
    obs = fleet.parse_job_probe(probe("inactive", "", "", "no", []))
    assert obs == {"active": "inactive", "setup": None, "summary": None, "reboot": False, "log": []}
    assert len(fleet.parse_job_probe(probe(log=["l%d" % i for i in range(100)]))["log"]) == 40
    assert fleet.parse_job_probe("") is None
    assert fleet.parse_job_probe("ssh: connect to host defiant.example port 22: Connection refused\n") is None
    assert fleet.parse_job_probe("ACTIVE:active\nSETUP:1\n") is None                  # never reached LOG:
    assert fleet.parse_job_probe(probe(setup="\u00b2", reboot="maybe"))["setup"] is None
    assert fleet.parse_job_probe(probe(setup="\u00b2", reboot="maybe"))["reboot"] is None


def test_job_step_running_and_done():
    job = running_job(1000.0)
    j = fleet.upd_job_step(job, fleet.parse_job_probe(probe(log=["FLEET-APT-START 1000", "Unpacking a ..."])), 1010.0)
    assert j["state"] == "running" and j["lost"] is False and j["contact_at"] == 1010.0 and j["log"][-1] == "Unpacking a ..."
    assert job["log"] == [] and job["state"] == "running"                                 # the old job is not modified
    summ = "5 upgraded, 1 newly installed, 0 to remove and 3 not upgraded."
    for body, installed in (({"summary": summ, "setup": "9"}, 6), ({"setup": "12"}, 12), ({"setup": "0"}, None), ({}, None),
                            ({"summary": "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."}, 0)):
        o = fleet.parse_job_probe(probe("inactive", body.get("setup", ""), body.get("summary", ""), "yes",
                                        ["FLEET-APT-START 1000", "FLEET-APT-EXIT 0 1500"]))
        j = fleet.upd_job_step(running_job(1000.0), o, 1600.0)
        assert j["state"] == "done" and j["exit"] == 0 and j["ended_at"] == 1500.0 and j["reboot_required"] is True, j
        assert j["installed"] == installed, (body, j["installed"])
    o = fleet.parse_job_probe(probe("inactive", "3", "", "no", ["FLEET-APT-EXIT 0"]))      # no epoch on the line
    j = fleet.upd_job_step(running_job(1000.0), o, 1600.0)
    assert j["state"] == "done" and j["ended_at"] == 1600.0 and j["reboot_required"] is False
    for tail in ("FLEET-APT-EXIT 0 /bin/sh", "FLEET-APT-EXIT 0 9999999999"):                       # an epoch that is not one, or from the future
        j = fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("inactive", "1", log=[tail])), 1600.0)
        assert j["state"] == "done" and j["ended_at"] == 1600.0, tail
    j = fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("active", log=["FLEET-APT-EXIT 0 1500"])), 1600.0)
    assert j["state"] == "done"                                                       # the exit line wins over a stale "active"
    assert fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("active", log=["text FLEET-APT-EXIT 0 1500"])), 1600.0)["state"] == "running"


def test_job_step_failed():
    o = fleet.parse_job_probe(probe("inactive", "4", "5 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", "no",
                                    ["E: Sub-process /usr/bin/dpkg returned an error code (1)", "FLEET-APT-EXIT 100 1500"]))
    j = fleet.upd_job_step(running_job(1000.0), o, 1600.0)
    assert j["state"] == "failed" and j["exit"] == 100 and j["installed"] is None and j["ended_at"] == 1500.0
    assert j["log"][-2] == "E: Sub-process /usr/bin/dpkg returned an error code (1)"
    # the unit is gone and left no exit line: failed, but only after the grace period
    gone = fleet.parse_job_probe(probe("inactive", "", "", "yes", ["FLEET-APT-START 1000", "Unpacking a ..."]))
    assert fleet.upd_job_step(running_job(1000.0), gone, 1010.0)["state"] == "running"
    j = fleet.upd_job_step(running_job(1000.0), gone, 1030.0)
    assert j["state"] == "failed" and j["exit"] is None and j["installed"] is None and j["reboot_required"] is True
    assert j["log"][-1] == "fleet: the unit ended without an exit line" and j["log"][-2] == "Unpacking a ..." and j["ended_at"] == 1030.0
    assert fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("failed")), 1100.0)["state"] == "failed"
    for unit_state in ("active", "activating", "reloading", "deactivating"):
        assert fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe(unit_state)), 1100.0)["state"] == "running"
    long = fleet.parse_job_probe(probe("inactive", log=["l%d" % i for i in range(60)]))
    assert len(fleet.upd_job_step(running_job(1000.0), long, 1030.0)["log"]) == 40            # the fleet line is part of the 40


def test_job_step_a_stop_by_no_remove_gets_a_hint():
    """apt refuses to remove a package when the unit runs with --no-remove: exit 100, nothing changed. The job's tail then
    ends with one plain line that says what to do (a scan follows by itself, and its list will name the removal)."""
    apt = "E: Packages need to be removed but remove is disabled."
    tail = ["FLEET-APT-START 1000", "Reading package lists...", "1 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.", apt,
            "FLEET-APT-EXIT 100 1500"]
    j = fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("inactive", "0", "", "no", tail)), 1600.0)
    assert j["state"] == "failed" and j["exit"] == 100 and j["installed"] is None and j["ended_at"] == 1500.0
    assert j["log"] == tail + [fleet.UPD_NO_REMOVE_HINT] and fleet.UPD_NO_REMOVE_HINT.startswith("Fleet: ")
    assert fleet.UPD_NO_REMOVE_HINT == "Fleet: apt would remove packages, nothing was changed. Rescanning now: check the list, then install again."
    full = fleet.parse_job_probe(probe("inactive", "0", "", "no", ["l%d" % i for i in range(60)] + [apt, "FLEET-APT-EXIT 100 1500"]))
    j = fleet.upd_job_step(running_job(1000.0), full, 1600.0)
    assert len(j["log"]) == 40 and j["log"][-1] == fleet.UPD_NO_REMOVE_HINT and j["log"][-2] == "FLEET-APT-EXIT 100 1500" and apt in j["log"]
    # no hint without apt's own sentence, for any other failure, and never for a job that went through
    for rc, lines in ((100, ["E: Sub-process /usr/bin/dpkg returned an error code (1)", "FLEET-APT-EXIT 100 1500"]),
                      (1, ["Err:1 http://repo.example resolute InRelease", "FLEET-APT-EXIT 1 1500"]),
                      (0, [apt, "FLEET-APT-EXIT 0 1500"])):
        j = fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("inactive", "1", "", "no", lines)), 1600.0)
        assert fleet.UPD_NO_REMOVE_HINT not in j["log"] and j["log"] == lines and j["exit"] == rc, (rc, j["log"])
    # the words alone are not apt's refusal: a package's own output that mentions them, an upgrade that did change packages
    # ("nothing was changed" would be false), the sentence without apt's E: prefix
    dpkg = "E: Sub-process /usr/bin/dpkg returned an error code (1)"
    for lines in (["Setting up libfoo (1.2) ...", "Setting up nginx (1.28) ...", "nginx postinst: note: automatic remove is disabled in this profile", dpkg, "FLEET-APT-EXIT 100 1500"],
                  ["Unpacking libfoo (1.2) over (1.1) ...", "note: remove is disabled", dpkg, "FLEET-APT-EXIT 100 1500"],
                  ["Setting up libfoo (1.2) ...", apt, "FLEET-APT-EXIT 100 1500"],                         # even apt's sentence, once dpkg has run
                  ["Removing libold (1.0) ...", apt, "FLEET-APT-EXIT 100 1500"],
                  ["Packages need to be removed but remove is disabled.", "FLEET-APT-EXIT 100 1500"],
                  ["W: remove is disabled for this repository", "FLEET-APT-EXIT 100 1500"]):
        j = fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("inactive", "2", "", "no", lines)), 1600.0)
        assert j["state"] == "failed" and j["exit"] == 100 and fleet.UPD_NO_REMOVE_HINT not in j["log"] and j["log"] == lines, lines
    # the message but no exit line: the unit ended on its own, that is the other failure and it keeps its own line
    gone = fleet.parse_job_probe(probe("inactive", "1", "", "no", [apt]))
    j = fleet.upd_job_step(running_job(1000.0), gone, 1030.0)
    assert j["state"] == "failed" and j["exit"] is None and j["log"] == [apt, "fleet: the unit ended without an exit line"]
    # an exit line of an earlier run does not turn the message into a hint
    old = fleet.parse_job_probe(probe("active", "1", "", "no", [apt, "FLEET-APT-EXIT 100 900"]))
    j = fleet.upd_job_step(running_job(1000.0), old, 1010.0)
    assert j["state"] == "running" and fleet.UPD_NO_REMOVE_HINT not in j["log"]


def test_job_step_a_unit_state_that_is_not_one_of_the_words_is_no_answer():
    """systemd re-executes itself when systemd is in the upgrade: `systemctl is-active` then prints an error text, or
    nothing when ssh was cut. That must neither end the install nor count as contact."""
    for unit_state in ("unknown", "", "Failed to connect to bus: No such file or directory", "inactive (dead)"):
        o = fleet.parse_job_probe(probe(unit_state, "12", log=["Setting up systemd (257.9-1) ..."]))
        j = fleet.upd_job_step(running_job(1000.0, contact=1000.0), o, 1100.0)                  # long past the 20 s grace
        assert j["state"] == "running" and j["lost"] is True and j["contact_at"] == 1000.0 and j["exit"] is None, (unit_state, j)
        j = fleet.upd_job_step(j, fleet.parse_job_probe(probe("active", log=["Setting up systemd (257.9-1) ..."])), 1105.0)
        assert j["state"] == "running" and j["lost"] is False and j["contact_at"] == 1105.0     # the next good answer clears it
        j = fleet.upd_job_step(running_job(1000.0, contact=1000.0), o, 1000.0 + fleet.UPD_LOST + 1)
        assert j["state"] == "failed" and j["log"][-1] == "fleet: lost contact with the host for 30 minutes"     # and it is not an endless wait
    o = fleet.parse_job_probe(probe("", log=["FLEET-APT-EXIT 0 1500"]))                          # an exit line is read at the next poll that has an answer
    assert fleet.upd_job_step(running_job(1000.0), o, 1600.0)["state"] == "running"
    assert fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("inactive", log=["FLEET-APT-EXIT 0 1500"])), 1605.0)["state"] == "done"


def test_job_step_an_exit_line_from_an_earlier_run_ends_nothing():
    """The log on the host can still hold the end of the previous run, its exit line included, when it is not
    truncated at the start of the unit. An exit line stamped before this install began is not this install's."""
    stale = ["Setting up a ...", "FLEET-APT-EXIT 0 900"]                                       # 100 s before the start at 1000
    for unit_state in ("active", "activating"):
        j = fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe(unit_state, "1", "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", log=stale)), 1010.0)
        assert j["state"] == "running" and j["exit"] is None and j["installed"] is None and j["ended_at"] is None, (unit_state, j)
    gone = fleet.parse_job_probe(probe("inactive", "1", log=stale))                             # the unit never started, or died without a line of its own
    assert fleet.upd_job_step(running_job(1000.0), gone, 1010.0)["state"] == "running"        # inside the grace period
    j = fleet.upd_job_step(running_job(1000.0), gone, 1030.0)
    assert j["state"] == "failed" and j["exit"] is None and j["log"][-1] == "fleet: the unit ended without an exit line"
    # up to 5 s before the start is clock skew between the two hosts, not an earlier run
    assert fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("inactive", log=["FLEET-APT-EXIT 0 995"])), 1030.0)["state"] == "done"
    assert fleet.upd_job_step(running_job(1000.0), fleet.parse_job_probe(probe("inactive", log=["FLEET-APT-EXIT 0 994"])), 1030.0)["state"] == "failed"
    # the newest exit line wins, so a leftover above this run's own does not hide it
    o = fleet.parse_job_probe(probe("inactive", "1", log=["FLEET-APT-EXIT 0 900", "E: Sub-process /usr/bin/dpkg returned an error code (1)", "FLEET-APT-EXIT 100 1500"]))
    j = fleet.upd_job_step(running_job(1000.0), o, 1600.0)
    assert j["state"] == "failed" and j["exit"] == 100 and j["ended_at"] == 1500.0


def test_job_step_lost_contact():
    job = running_job(1000.0, contact=1000.0)
    j = fleet.upd_job_step(job, None, 1005.0)
    assert j["state"] == "running" and j["lost"] is True and j["log"] == []
    j = fleet.upd_job_step(j, None, 1000.0 + 1800.0)                                       # exactly 30 minutes is still waiting
    assert j["state"] == "running" and j["lost"] is True
    ok = fleet.upd_job_step(j, fleet.parse_job_probe(probe(log=["Unpacking a ..."])), 1000.0 + 1801.0)      # contact again
    assert ok["lost"] is False and ok["state"] == "running" and ok["contact_at"] == 2801.0
    j = fleet.upd_job_step(ok, None, 2801.0 + 1700.0)                                      # the clock restarts at the last contact
    assert j["state"] == "running" and j["lost"] is True
    j = fleet.upd_job_step(j, None, 2801.0 + 1801.0)
    assert j["state"] == "failed" and j["exit"] is None and j["ended_at"] == 2801.0 + 1801.0 and j["lost"] is True
    assert j["log"][-1] == "fleet: lost contact with the host for 30 minutes" and j["log"][-2] == "Unpacking a ..."
    never = fleet.upd_job_step(dict(running_job(1000.0), contact_at=None), None, 1000.0 + 1801.0)     # never polled: counts from the start
    assert never["state"] == "failed"
    assert fleet.upd_job_step(dict(running_job(1000.0), contact_at=None), None, 1100.0)["state"] == "running"


def test_poll_flow_success_and_failure():
    with fleet_state():
        sent = []
        out = {"v": probe("inactive", "2", "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", "yes",
                          ["FLEET-APT-START 1000", "FLEET-APT-EXIT 0 1100"])}
        seen = []

        def ssh(script, timeout):
            seen.append((script, timeout, lock_is_free()))
            if isinstance(out["v"], BaseException):
                raise out["v"]
            return done(out["v"])
        with mock.patch.object(fleet, "_def_ssh", ssh), mock.patch.object(fleet, "_tg_send", lambda t: (sent.append(t), True)[1]):
            fleet._UPD["job"] = running_job(1000.0)
            assert fleet._upd_poll(1200.0) is False                                       # done: no Telegram, a rescan is due at once
            j = fleet._UPD["job"]
            assert j["state"] == "done" and j["installed"] == 2 and j["reboot_required"] is True and sent == []
            assert fleet._UPD["want"] is True and fleet._UPD["next_at"] == 1200.0
            assert seen == [(fleet.UPD_POLL, 30, True)]                                   # polled over ssh, lock free meanwhile
            with open(fleet.UPD_FILE) as f:
                assert json.load(f)["job"]["state"] == "done"
            # a failed job: one long Telegram message
            fleet._UPD["job"], fleet._UPD["want"] = running_job(1000.0), False
            out["v"] = probe("inactive", "1", "", "no", ["Err:1 http://repo.example resolute InRelease", "FLEET-APT-EXIT 100 1100"])
            assert fleet._upd_poll(1200.0) is False
            assert fleet._UPD["job"]["state"] == "failed" and len(sent) == 1
            assert sent[0].splitlines()[0] == "Defiant: update install failed (exit 100)" and "Err:1 http://repo.example resolute InRelease" in sent[0]
            assert fleet._UPD["want"] is True and fleet._UPD["next_at"] == 1200.0               # a failed job is followed by a rescan too
            # lost contact: keeps running quietly, fails once after 30 minutes
            fleet._UPD["job"] = running_job(1000.0, contact=1000.0)
            for err in (subprocess.TimeoutExpired(["ssh"], 30), OSError("no route"), "ssh: connect to host defiant.example port 22: Refused"):
                out["v"] = err
                assert fleet._upd_poll(1500.0) is True and fleet._UPD["job"]["lost"] is True and len(sent) == 1
            assert fleet._upd_poll(1000.0 + 1801.0) is False
            assert fleet._UPD["job"]["state"] == "failed" and len(sent) == 2 and sent[1].startswith("Defiant: update install failed\n")
            assert "lost contact" in sent[1]


def test_poll_with_a_cut_ssh_stream_is_no_contact():
    """ssh exits 255 when its own connection failed. What arrived may stop halfway through the log tail (the exit line
    is its last line), so it is not read: the job stays running as lost contact. Any other exit code is the remote
    script's own (tail finds no log: 1) and is read as it is."""
    with fleet_state():
        cut = probe("inactive", "2", "", "no", ["FLEET-APT-START 1000", "Setting up a ..."])      # the exit line never arrived
        with mock.patch.object(fleet, "_tg_send", lambda t: True):
            with mock.patch.object(fleet, "_def_ssh", lambda s, t: done(cut, "client_loop: send disconnect: Broken pipe", 255)):
                fleet._UPD["job"] = running_job(1000.0, contact=1000.0)
                assert fleet._upd_poll(1200.0) is True
                j = fleet._UPD["job"]
                assert j["state"] == "running" and j["lost"] is True and j["contact_at"] == 1000.0 and j["log"] == []
            with mock.patch.object(fleet, "_def_ssh", lambda s, t: done(cut, "", 1)):
                fleet._UPD["job"] = running_job(1000.0, contact=1000.0)
                assert fleet._upd_poll(1200.0) is False and fleet._UPD["job"]["state"] == "failed" and fleet._UPD["job"]["lost"] is False


def test_tick_polls_a_running_job_and_rescans_after_it():
    with fleet_state():
        calls = []
        scans = scan_text(fixture("apt-sim-phased.txt"))

        def ssh(script, timeout):
            calls.append(script)
            return done(probe("inactive", "1", "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", "no",
                              ["FLEET-APT-EXIT 0 1100"]) if script == fleet.UPD_POLL else scans)
        with mock.patch.object(fleet, "_def_ssh", ssh), mock.patch.object(fleet, "_tg_send", lambda t: True):
            fleet._UPD["job"], fleet._UPD["next_at"] = running_job(1000.0), 1.0                # a scan is overdue, but the job comes first
            assert fleet._upd_tick(1200.0) == 0 and calls == [fleet.UPD_POLL]
            assert fleet._UPD["job"]["state"] == "done"
            assert fleet._upd_tick(1201.0) == 0 and calls == [fleet.UPD_POLL, fleet.UPD_SCAN]  # the rescan right after
            assert fleet._UPD["scan"]["at"] == 1201.0 and fleet._UPD["job"]["state"] == "done"
            running = running_job(1000.0)
            fleet._UPD["job"] = running
            out = probe("active", log=["Unpacking a ..."])
            with mock.patch.object(fleet, "_def_ssh", lambda s, t: done(out)):
                assert fleet._upd_tick(1210.0) == 5 and fleet._UPD["job"]["state"] == "running"


def test_restart_resumes_polling_a_running_job():
    with fleet_state():
        fleet._UPD["job"] = running_job(1000.0, contact=1000.0, log=["Unpacking a ..."])
        with fleet._UPD_LOCK:
            fleet._upd_save_locked()
        fleet._UPD.update(fleet._upd_fresh())
        before = time.time()
        fleet.defiant_updates_load()
        j = fleet._UPD["job"]
        assert j["state"] == "running" and j["log"] == ["Unpacking a ..."] and j["contact_at"] >= before     # a restart is not lost contact
        calls = []
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: (calls.append(s), done(probe("active", log=["x"])))[1]):
            fleet._UPD["next_at"] = 0.0
            assert fleet._upd_tick(time.time()) == 5 and calls == [fleet.UPD_POLL]


# ---------------------------------------------------------------- start an install

def test_install_scripts():
    for allow in (True, False):
        s = fleet.upd_start(allow)
        for part in ("systemctl is-active --quiet fleet-apt-upgrade", "sudo -n systemd-run --unit=fleet-apt-upgrade --collect",
                     "-p StandardOutput=truncate:/var/log/fleet-apt-upgrade.log", "-p StandardError=inherit",
                     "-E DEBIAN_FRONTEND=noninteractive", "-E NEEDRESTART_MODE=a", "-E LC_ALL=C", "/bin/sh -c '",
                     "FLEET-APT-START", "FLEET-APT-EXIT", "DPkg::Lock::Timeout=600", "Dpkg::Options::=--force-confold", "full-upgrade", "-y"):
            assert part in s, (allow, part)
        assert not re.search(r"autoremove|reboot|shutdown|poweroff|halt|systemctl\s+(restart|stop|start|reboot)|\bkill", s)
        assert s.count("'") == 2 and "${" not in s and "\u2014" not in s              # one quoted script; no systemd ${VAR} expansion
        assert "StandardOutput=file:" not in s and "StandardError=file:" not in s     # file: does not truncate: the log would keep the end of the last run
    # two fixed variants: --no-remove is on the full-upgrade command of the one that guards, and nowhere else
    keep, guard = fleet.upd_start(True), fleet.upd_start(False)
    assert "--no-remove" not in keep and guard.count("--no-remove") == 1
    assert "-y -o Dpkg::Options::=--force-confold --no-remove full-upgrade; rc=$?" in guard and "-qq update" in guard
    assert "--no-remove update" not in guard and guard.replace("--no-remove ", "") == keep      # nothing else differs
    assert "--no-remove" not in fleet.UPD_SCAN + fleet.UPD_POLL              # a scan must see the removals, so it never carries the flag
    assert "full-upgrade" in fleet.UPD_SCAN and " -s " in fleet.UPD_SCAN and "Debug::NoLocking=1" in fleet.UPD_SCAN
    assert "sudo -n apt-get -qq update" in fleet.UPD_SCAN and "reboot-required.pkgs" in fleet.UPD_SCAN
    assert not re.search(r"autoremove|\s-y\s|install|dist-upgrade", fleet.UPD_SCAN)            # a scan changes nothing
    assert "tail -n 40" in fleet.UPD_POLL and "is-active fleet-apt-upgrade" in fleet.UPD_POLL


def test_install_starts_the_unit():
    with fleet_state():
        seen, before = [], time.time()
        # while the start command is on its way the job is not running yet: the log on the host is still the previous run's
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: (seen.append((s, t, lock_is_free(), fleet._UPD["starting"], fleet._UPD["job"]["state"])),
                                                                   done("Running as unit: fleet-apt-upgrade.service\nFLEET-RC:0\n"))[1]):
            r = fleet.defiant_updates_install("fixture-key")
        assert r["ok"] is True and before <= r["started_at"] <= time.time()
        assert seen == [(fleet.upd_start(False), 40, True, True, "idle")]                       # no scan yet: apt may not remove anything
        j = fleet._UPD["job"]
        assert j["state"] == "running" and j["started_at"] == r["started_at"] and j["exit"] is None and j["log"] == []
        assert fleet._UPD["starting"] is False and fleet._UPD_WAKE.is_set()
        with open(fleet.UPD_FILE) as f:
            assert json.load(f)["job"]["state"] == "running"


def test_upd_allow_remove():
    stub = dict(S(), at=None, error="apt-get update failed (exit 100): E: Could not get lock", error_at=2000.0)      # what the first failed scan shows
    allow = lambda scan, now=1000.0: fleet.upd_allow_remove(scan, now)
    assert allow(None) is False                                                         # no scan yet
    assert allow(stub) is False                                                         # only an error stub
    assert allow(dict(stub, remove=["libold-compat1"])) is False                        # a stub is no scan, whatever it holds
    assert allow(S([P("a", security=True)], remove=[])) is False                        # a good scan with nothing to remove
    assert allow(S([P("a")], remove=["libold-compat1"])) is True                        # a good scan that lists a removal
    # Fleet cannot tell what apt would do now: the last attempt failed (the old list stays on the page, with the error), or the scan is stale
    failed = S([P("a")], remove=["a", "b"], error="ssh timed out", error_at=3000.0)
    assert allow(failed, 3100.0) is False and allow(dict(failed, error=""), 3100.0) is False      # whatever the error text, empty included
    two = 2 * fleet.UPD_EVERY
    assert allow(S([P("a")], remove=["x"]), 1000.0 + two - 1) is True and allow(S([P("a")], remove=["x"]), 1000.0 + two) is False
    assert allow({}) is False and allow([]) is False


def test_install_chooses_the_variant_from_the_latest_scan():
    stub = dict(S(), at=None, error="ssh timed out", error_at=2000.0)
    t = time.time()
    fresh = lambda **kw: S([P("a")], at=t - 60, **kw)
    for scan, allow in ((None, False), (stub, False), (fresh(), False), (fresh(remove=["libold-compat1"]), True),
                        (fresh(remove=["libold-compat1"], error="ssh timed out", error_at=t - 30), False),       # the last attempt failed
                        (S([P("a")], at=t - 3 * fleet.UPD_EVERY, remove=["libold-compat1"]), False)):             # stale
        with fleet_state():
            fleet._UPD["scan"] = scan
            sent = []
            with mock.patch.object(fleet, "_def_ssh", lambda s, t: (sent.append(s), done("FLEET-RC:0"))[1]):
                fleet.defiant_updates_install("fixture-key")
            assert sent == [fleet.upd_start(allow)], (scan, allow)
            assert ("--no-remove" in sent[0]) is (not allow)
            assert fleet._UPD["job"]["state"] == "running"


def test_install_flag_never_comes_from_the_request():
    """The variant is Fleet's own decision from its own scan: whatever the body of the request says changes nothing."""
    with fleet_state(), mock.patch.object(fleet.urllib.request, "urlopen", mock.Mock(side_effect=AssertionError("network"))):
        fleet._UPD["scan"] = S([P("a")])                                                   # no removals in the scan
        ssh = mock.Mock(return_value=done("FLEET-RC:0"))
        with mock.patch.object(fleet, "_def_ssh", ssh), server() as port, redirect_stdout(io.StringIO()):
            body = {"allow_remove": True, "remove": True, "flags": "", "no_remove": False, "script": "echo injected"}
            st, h, v = call(port, "POST", "/api/defiant/updates/install", body, "fixture-key")
        assert st == 200 and ssh.call_count == 1 and ssh.call_args[0][0] == fleet.upd_start(False), (st, v)
        assert "injected" not in ssh.call_args[0][0]


def test_a_stop_by_no_remove_is_reported_rescanned_and_the_next_install_may_remove():
    """The whole guard, in order: a scan without removals, the install runs with --no-remove and apt refuses (the host
    changed after the scan), the job ends failed with the hint, one long Telegram message carries the hint, a rescan
    follows by itself and lists the removal, and the next install runs without the flag."""
    apt = "E: Packages need to be removed but remove is disabled."
    plain = scan_text(sim_text([inst("tzdata", "1", "2", UPD)]))                              # not security: no alert in the way
    removal = scan_text(sim_text(["Remv libold-compat1 [1.4-2]", inst("tzdata", "1", "2", UPD)]))
    with fleet_state():
        scripts, sent, now = [], [], [1000.0]
        out = {"scan": plain, "poll": probe("active", log=["FLEET-APT-START 1000"])}

        def ssh(script, timeout):
            scripts.append(script)
            if script == fleet.UPD_SCAN:
                return done(out["scan"])
            if script == fleet.UPD_POLL:
                return done(out["poll"])
            return done("FLEET-RC:0")                                                      # an install start
        with mock.patch.object(fleet, "_def_ssh", ssh), mock.patch.object(fleet, "_tg_send", lambda t: (sent.append(t), True)[1]):
            fleet._UPD["next_at"] = 1000.0
            assert fleet._upd_tick(1000.0) == 0 and fleet._UPD["scan"]["remove"] == []     # the scan the page showed: nothing to remove
            fleet.defiant_updates_install("fixture-key")
            assert scripts[-1] == fleet.upd_start(False) and "--no-remove" in scripts[-1]
            fleet._UPD["job"]["started_at"] = fleet._UPD["job"]["contact_at"] = 1100.0
            out["poll"] = probe("inactive", "0", "", "no", ["FLEET-APT-START 1100", "1 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.",
                                                            apt, "FLEET-APT-EXIT 100 1130"])
            assert fleet._upd_tick(1140.0) == 0                                            # the poll: the job ended
            j = fleet._UPD["job"]
            assert j["state"] == "failed" and j["exit"] == 100 and j["log"][-2:] == ["FLEET-APT-EXIT 100 1130", fleet.UPD_NO_REMOVE_HINT]
            assert len(sent) == 1 and sent[0].startswith("Defiant: update install failed (exit 100)\n\n") and sent[0].endswith(fleet.UPD_NO_REMOVE_HINT)
            assert fleet._UPD["want"] is True and fleet._UPD["scan"]["remove"] == []       # a rescan is due, not done yet
            out["scan"] = removal
            t = time.time()                                                                # the install below reads the real clock: the scan must be recent
            assert fleet._upd_tick(t) == 0                                                 # the rescan, right after the failed job
            assert scripts[-1] == fleet.UPD_SCAN and fleet._UPD["scan"]["remove"] == ["libold-compat1"] and fleet._UPD["scan"]["at"] == t
            assert len(sent) == 1                                                          # no second message
            assert fleet.defiant_updates_view()["scan"]["remove"] == ["libold-compat1"]    # what the next confirm text names
            fleet.defiant_updates_install("fixture-key")
            assert scripts[-1] == fleet.upd_start(True) and "--no-remove" not in scripts[-1]


def test_install_refusals_and_failures_leave_the_job_alone():
    with fleet_state():
        calls = []
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: (calls.append(s), done("FLEET-RC:0"))[1]):
            for key in (None, "", "wrong"):
                try:
                    fleet.defiant_updates_install(key)
                except urllib.error.HTTPError as e:                                  # the key is checked before anything else
                    assert e.code == 403
                else:
                    raise AssertionError("accepted a bad key")
            assert calls == [] and fleet._UPD["job"]["state"] == "idle"
            fleet._UPD["job"] = running_job(1000.0)
            try:
                fleet.defiant_updates_install("fixture-key")
            except PermissionError as e:
                assert "already running" in str(e)
            else:
                raise AssertionError("started a second install")
            fleet._UPD["job"] = fleet._upd_job_idle()
            fleet._UPD["busy"] = True
            try:
                fleet.defiant_updates_install("fixture-key")
            except PermissionError as e:
                assert "scan is running" in str(e)
            else:
                raise AssertionError("started during a scan")
            assert calls == []
        with mock.patch.object(fleet, "DEFIANT_HOST", ""):
            try:
                fleet.defiant_updates_install("fixture-key")
            except ValueError as e:
                assert "not configured" in str(e)
            else:
                raise AssertionError("not configured was accepted")
        fleet._UPD["busy"] = False
        previous = dict(fleet._upd_job_idle(), state="done", started_at=1.0, ended_at=2.0, exit=0, installed=7, reboot_required=False, log=["old"])
        # ssh fails; systemd-run fails: nothing was started, so the previous result stays
        for result, exc, word in ((done("", "ssh: connect to host defiant.example port 22: Connection refused", 255), OSError, "Connection refused"),
                                  (done("Failed to start transient service unit: Access denied\nFLEET-RC:1\n"), OSError, "Access denied"),
                                  (done("", "", 255), OSError, "ssh exit 255")):
            fleet._UPD["job"] = previous

            def ssh(s, t, result=result):
                if isinstance(result, BaseException):
                    raise result
                return result
            with mock.patch.object(fleet, "_def_ssh", ssh):
                try:
                    fleet.defiant_updates_install("fixture-key")
                except exc as e:
                    assert word in str(e), (word, str(e))
                else:
                    raise AssertionError("no error for " + word)
            assert fleet._UPD["job"] is previous and fleet._UPD["starting"] is False
        assert not fleet._UPD_WAKE.is_set()


def test_install_follows_a_unit_that_runs_or_may_run():
    """FLEET-BUSY: a unit Fleet knows nothing of is running (Fleet restarted during the start, or the answer got lost). A
    timeout: ssh may have died after systemd-run had started it. Either way the click is answered with an error, but
    the worker follows the unit from then on, so its progress and its result are not lost."""
    for result, exc, word in ((done("FLEET-BUSY\n"), PermissionError, "already running on Defiant"),
                              (subprocess.TimeoutExpired(["ssh", "-o", "BatchMode=yes", "defiant.example", fleet.upd_start(False)], 40), OSError, "timed out")):
        with fleet_state():
            fleet._UPD["job"] = dict(fleet._upd_job_idle(), state="failed", started_at=1.0, ended_at=2.0, exit=1, log=["old"])

            def ssh(s, t, result=result):
                if isinstance(result, BaseException):
                    raise result
                return result
            before = time.time()
            with mock.patch.object(fleet, "_def_ssh", ssh):
                try:
                    fleet.defiant_updates_install("fixture-key")
                except exc as e:
                    assert word in str(e), (word, str(e))
                    assert "defiant.example" not in str(e) and "BatchMode" not in str(e) and "systemd-run" not in str(e)     # never the ssh command
                else:
                    raise AssertionError("no error for " + word)
            j = fleet._UPD["job"]
            assert j["state"] == "running" and before <= j["started_at"] <= time.time() and j["contact_at"] == j["started_at"], j
            assert j["exit"] is None and j["log"] == [] and fleet._UPD["starting"] is False and fleet._UPD_WAKE.is_set()
            with open(fleet.UPD_FILE) as f:
                assert json.load(f)["job"]["state"] == "running"                         # a Fleet restart keeps following it
            try:                                                                         # and a second click now finds the job
                fleet.defiant_updates_install("fixture-key")
            except PermissionError as e:
                assert str(e) == "An update is already running."
            # the worker's poll takes it from here: the unit's own log decides
            fleet._UPD["next_at"] = 1e12
            with mock.patch.object(fleet, "_def_ssh", lambda s, t: done(probe("inactive", "2", "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.", "no",
                                                                              ["FLEET-APT-START %d" % int(j["started_at"]), "FLEET-APT-EXIT 0 %d" % (int(j["started_at"]) + 90)]))), \
                    mock.patch.object(fleet, "_tg_send", lambda t: True):
                assert fleet._upd_tick(j["started_at"] + 100) == 0
            assert fleet._UPD["job"]["state"] == "done" and fleet._UPD["job"]["installed"] == 2 and fleet._UPD["want"] is True


def test_install_second_click_during_the_start_is_refused():
    with fleet_state():
        inner = []

        def ssh(s, t):
            try:                                                                     # a second click while the first is on its way
                fleet.defiant_updates_install("fixture-key")
            except PermissionError as e:
                inner.append(str(e))
            inner.append(fleet.defiant_updates_scan_request())                       # and so is a scan
            return done("FLEET-RC:0")
        with mock.patch.object(fleet, "_def_ssh", ssh):
            fleet.defiant_updates_install("fixture-key")
        assert inner == ["An update is already running.", {"ok": True, "started": False, "reason": "busy"}]
        assert fleet._UPD["job"]["state"] == "running" and fleet._UPD["starting"] is False and fleet._UPD["busy"] is False


def test_worker_neither_polls_nor_scans_while_an_install_starts():
    with fleet_state():
        calls = []
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: (calls.append(s), done(""))[1]), redirect_stdout(io.StringIO()):
            fleet._UPD.update(starting=True, want=True, next_at=0.0)                  # a scan is due, but the start is in flight
            assert 1 <= fleet._upd_tick(5000.0) <= 60 and calls == []
            fleet._UPD["starting"] = False                                            # started: the poll sees the new log, not the old
            fleet._UPD["job"] = running_job(5000.0)
            fleet._upd_tick(5001.0)
            assert calls == [fleet.UPD_POLL]


# ---------------------------------------------------------------- scans and the worker

def test_scan_once_replaces_data_and_alerts_once():
    with fleet_state():
        out = scan_text(fixture("apt-sim-large.txt"))
        calls, sent, locks = [], [], []
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: (calls.append((s, t)), locks.append(lock_is_free()),
                                                                   done(out))[2]), \
                mock.patch.object(fleet, "_tg_send", lambda text: (sent.append(text), True)[1]):
            fleet._UPD["next_at"] = 1045.0
            wait = fleet._upd_tick(1000.0)
            assert calls == [] and 1 <= wait <= 60                                   # nothing is due yet
            fleet._upd_tick(1045.0)
            assert calls == [(fleet.UPD_SCAN, 300)] and locks == [True]
            s = fleet._UPD["scan"]
            assert s["at"] == 1045.0 and s["error"] is None and s["error_at"] is None and s["count"] == 20 and s["security"] == 11
            assert fleet._UPD["next_at"] == 1045.0 + 3600 and fleet._UPD["busy"] is False and fleet._UPD["started"] == 1045.0
            assert len(sent) == 1 and sent[0].splitlines()[0] == "Defiant: 11 security updates pending"
            with open(fleet.UPD_FILE) as f:
                assert json.load(f)["scan"] == s and os.listdir(os.path.dirname(fleet.UPD_FILE)) == ["defiant-updates.json"]
            fleet._upd_tick(1046.0)
            assert len(calls) == 1
            fleet._upd_tick(1045.0 + 3600)                                            # the next hour: scans, does not repeat the alert
            assert len(calls) == 2 and len(sent) == 1 and fleet._UPD["scan"]["at"] == 4645.0
            # a scan that fails keeps the last good data and rides an error on it
            good = fleet._UPD["scan"]
            with mock.patch.object(fleet, "_def_ssh", mock.Mock(side_effect=subprocess.TimeoutExpired(["ssh", "defiant.example"], 300))), \
                    redirect_stdout(io.StringIO()):
                fleet._upd_tick(4645.0 + 3600)
            s = fleet._UPD["scan"]
            assert s["error"] == "ssh timed out" and s["error_at"] == 8245.0 and s["at"] == 4645.0
            assert {k: v for k, v in s.items() if k not in ("error", "error_at")} == {k: v for k, v in good.items() if k not in ("error", "error_at")}
            assert fleet._UPD["busy"] is False and fleet._UPD["next_at"] == 8245.0 + fleet.UPD_RETRY and len(sent) == 1   # a failure is retried sooner
            for bad, words in ((scan_text(sim_text([inst("a", "1", "2")]), rc=100, said="E: Could not get lock /var/lib/apt/lists/lock"), "Could not get lock"),
                               ("", "ssh exit 255"), ("some banner only", "ssh exit 255")):
                with mock.patch.object(fleet, "_def_ssh", lambda s, t, bad=bad: done(bad, "", 255)), redirect_stdout(io.StringIO()):
                    fleet._UPD["next_at"] = 0.0
                    fleet._upd_tick(9000.0)
                assert words in fleet._UPD["scan"]["error"] and fleet._UPD["scan"]["at"] == 4645.0 and "defiant.example" not in fleet._UPD["scan"]["error"]
            fleet._UPD["next_at"] = 0.0                                               # and the next good scan clears it
            fleet._upd_tick(12000.0)
            assert fleet._UPD["scan"]["error"] is None and fleet._UPD["scan"]["error_at"] is None and fleet._UPD["scan"]["at"] == 12000.0


def test_scan_failure_without_a_good_scan_and_parser_bugs_do_not_wedge():
    with fleet_state():
        stderr = "ssh: connect to host defiant.example port 22: Connection refused"
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: done("", stderr, 255)), redirect_stdout(io.StringIO()) as log:
            fleet._UPD["next_at"] = 0.0
            fleet._upd_tick(1000.0)
        assert fleet._UPD["scan"] is None and fleet._UPD["busy"] is False and fleet._UPD["next_at"] == 1000.0 + fleet.UPD_RETRY   # still scheduled
        assert "Connection refused" in log.getvalue()
        v = fleet.defiant_updates_view()["scan"]                                       # the page learns why there is no data
        assert v["at"] is None and "Connection refused" in v["error"] and v["error_at"] == 1000.0 and v["busy"] is False
        assert v["count"] == 0 and v["packages"] == [] and fleet.defiant_updates_summary() is None
        assert fleet.defiant_updates_view()["critical"] is False
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: done(scan_text(sim_text([inst("a", "1", "2")])))), \
                mock.patch.object(fleet, "parse_scan_output", mock.Mock(side_effect=KeyError("boom"))), redirect_stdout(io.StringIO()):
            fleet._UPD["scan"] = S([P("a")], at=900.0)
            fleet._upd_tick(5000.0)
        assert fleet._UPD["busy"] is False and fleet._UPD["scan"]["error"] == "KeyError: 'boom'" and fleet._UPD["scan"]["at"] == 900.0
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: done("\x00\x01 FLEET-SCAN end \udcff")), redirect_stdout(io.StringIO()):
            fleet._upd_tick(9000.0)
        assert fleet._UPD["busy"] is False


def test_failed_scans_never_alert():
    with fleet_state():
        sent = []
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: done("", "ssh: no route", 255)), \
                mock.patch.object(fleet, "_tg_send", lambda t: sent.append(t) or True), redirect_stdout(io.StringIO()):
            fleet._UPD["scan"] = S([P("a", security=True)])
            fleet._upd_tick(5000.0)
        assert sent == []


def test_first_scan_is_visible_while_it_runs_and_when_it_failed():
    with fleet_state():
        assert fleet.defiant_updates_view()["scan"] is None                            # nothing started yet
        fleet._UPD["busy"] = True                                                      # the first scan is running
        s = fleet.defiant_updates_view()["scan"]
        assert set(s) == SCAN_KEYS and s["at"] is None and s["busy"] is True and s["error"] is None and s["count"] == 0
        fleet._UPD["busy"] = False
        with mock.patch.object(fleet, "_def_ssh", lambda sc, t: done("", "ssh: no route", 255)), redirect_stdout(io.StringIO()):
            fleet._UPD["next_at"] = 0.0
            fleet._upd_tick(2000.0)
        s = fleet.defiant_updates_view()["scan"]
        assert s["at"] is None and "no route" in s["error"] and s["error_at"] == 2000.0 and fleet._UPD["next_at"] == 2000.0 + fleet.UPD_RETRY
        assert not os.path.exists(fleet.UPD_FILE) or json.load(open(fleet.UPD_FILE))["scan"] is None      # a stub is never saved
        with mock.patch.object(fleet, "_def_ssh", lambda sc, t: done(scan_text(sim_text([inst("a", "1", "2")])))), \
                mock.patch.object(fleet, "_tg_send", lambda t: True):
            fleet._upd_tick(2000.0 + fleet.UPD_RETRY)                                  # the retry succeeds and replaces the stub
        s = fleet.defiant_updates_view()["scan"]
        assert s["at"] == 2000.0 + fleet.UPD_RETRY and s["error"] is None and s["count"] == 1 and fleet._UPD["first_err"] is None
        assert fleet._UPD["next_at"] == 2000.0 + fleet.UPD_RETRY + 3600


def test_install_uses_the_fail_closed_key_check():
    with fleet_state():
        with mock.patch.object(fleet, "_def_ssh", side_effect=AssertionError("no ssh without a valid key")):
            for bad in (None, "", "wrong"):
                try:
                    fleet.defiant_updates_install(bad)
                except urllib.error.HTTPError as e:
                    assert e.code == 403
                else:
                    raise AssertionError("a bad key started the install")
        assert fleet._UPD["job"]["state"] == "idle" and fleet._UPD["starting"] is False


def test_worker_schedule_at_start():
    def start(scan, now):
        with fleet_state():
            fleet._UPD["scan"] = scan
            with mock.patch.object(fleet.time, "time", lambda: now), mock.patch.object(fleet, "_upd_tick", side_effect=KeyboardInterrupt):
                try:
                    fleet.defiant_updates_worker()
                except KeyboardInterrupt:
                    pass
            return fleet._UPD["next_at"]
    assert start(None, 10000.0) == 10045.0                                            # nothing saved: scan 45 s after start
    assert start(S(at=10000.0 - 600), 10000.0) == 10000.0 - 600 + 3600                  # a young saved scan is not repeated
    assert start(S(at=10000.0 - 3299), 10000.0) == 10000.0 - 3299 + 3600
    assert start(S(at=10000.0 - 3300), 10000.0) == 10045.0                            # 55 minutes old: scan at once
    assert start(S(at=10000.0 - 3 * 3600), 10000.0) == 10045.0
    assert start(S(at=10000.0 - 3 * 3600, error="ssh timed out", error_at=10000.0 - 300), 10000.0) == 10000.0 - 300 + fleet.UPD_RETRY   # a failed attempt is retried sooner


def test_worker_survives_a_surprise():
    with fleet_state():
        outcomes = [ValueError("boom"), KeyError("x"), KeyboardInterrupt()]
        waits = []

        def tick(now):
            e = outcomes.pop(0)
            raise e
        wake = mock.Mock()
        wake.wait.side_effect = lambda t: waits.append(t)
        with mock.patch.object(fleet, "_upd_tick", tick), mock.patch.object(fleet, "_UPD_WAKE", wake), redirect_stdout(io.StringIO()) as log:
            try:
                fleet.defiant_updates_worker()
            except KeyboardInterrupt:
                pass
        assert outcomes == [] and waits == [30, 30] and "ValueError: boom" in log.getvalue()


def test_manual_scan_request():
    with fleet_state():
        with mock.patch.object(fleet, "_def_ssh", mock.Mock(side_effect=AssertionError("a request thread scanned"))):
            assert fleet.defiant_updates_scan_request() == {"ok": True, "started": True}
            assert fleet._UPD["busy"] is True and fleet._UPD["want"] is True and fleet._UPD_WAKE.is_set()
            s = fleet.defiant_updates_view()["scan"]                                       # no scan yet: an empty one that says "running"
            assert s["at"] is None and s["busy"] is True and s["error"] is None and s["count"] == 0
            assert fleet.defiant_updates_scan_request() == {"ok": True, "started": False, "reason": "busy"}
        with mock.patch.object(fleet, "DEFIANT_HOST", ""):
            try:
                fleet.defiant_updates_scan_request()
            except ValueError as e:
                assert "not configured" in str(e)
            else:
                raise AssertionError("not configured was accepted")
    with fleet_state():
        out = scan_text(fixture("apt-sim-phased.txt"))
        with mock.patch.object(fleet, "_def_ssh", lambda s, t: done(out)), mock.patch.object(fleet, "_tg_send", lambda t: True):
            fleet._UPD["scan"], fleet._UPD["next_at"] = S([P("a")], at=500.0), 1e12          # not due for ages
            assert fleet.defiant_updates_scan_request()["started"] is True
            assert fleet.defiant_updates_view()["scan"]["busy"] is True                      # the page shows "Scanning..." at once
            fleet._upd_tick(time.time())                                                      # the worker picks it up
            assert fleet._UPD["busy"] is False and fleet._UPD["want"] is False and fleet._UPD["scan"]["count"] == 0
            assert fleet.defiant_updates_view()["scan"]["busy"] is False
            assert fleet.defiant_updates_scan_request() == {"ok": True, "started": False, "reason": "recent"}      # within 60 s
            fleet._UPD["started"] -= 61
            assert fleet.defiant_updates_scan_request() == {"ok": True, "started": True}
            fleet._UPD["busy"], fleet._UPD["job"] = False, running_job(1000.0)
            assert fleet.defiant_updates_scan_request() == {"ok": True, "started": False, "reason": "busy"}      # an install runs


# ---------------------------------------------------------------- saved state

def test_persistence_round_trip():
    with fleet_state() as d:
        scan = S([P("a", security=True), P("b")], at=1234.5, remove=["old"], deferred=["held"], reboot=True, reboot_pkgs=["linux-image-7"], error="x", error_at=1300.0)
        job = running_job(1000.0, contact=1100.0, log=["l1", "l2"])
        fleet._UPD.update(scan=scan, alerted={"sig": "a=2", "at": 1200.0}, job=job, busy=True, want=True, started=5.0, next_at=9.0)
        with fleet._UPD_LOCK:
            fleet._upd_save_locked()
        assert os.listdir(os.path.dirname(fleet.UPD_FILE)) == ["defiant-updates.json"]                # tmp + rename: nothing left over
        with open(fleet.UPD_FILE) as f:
            assert set(json.load(f)) == {"scan", "alerted", "job"}
        fleet._UPD.update(fleet._upd_fresh())
        before = time.time()
        fleet.defiant_updates_load()
        assert fleet._UPD["scan"] == scan and fleet._UPD["alerted"] == {"sig": "a=2", "at": 1200.0}
        assert fleet._UPD["job"] == dict(job, contact_at=fleet._UPD["job"]["contact_at"]) and fleet._UPD["job"]["contact_at"] >= before
        assert (fleet._UPD["busy"], fleet._UPD["want"], fleet._UPD["started"], fleet._UPD["next_at"]) == (False, False, 0.0, 0.0)
        for state in ("done", "failed"):                              # a finished job comes back as it was
            fleet._UPD["job"] = dict(job, state=state, exit=3, ended_at=1500.0, installed=None)
            with fleet._UPD_LOCK:
                fleet._upd_save_locked()
            fleet._UPD["job"] = fleet._upd_job_idle()
            fleet.defiant_updates_load()
            assert fleet._UPD["job"]["state"] == state and fleet._UPD["job"]["exit"] == 3 and fleet._UPD["job"]["contact_at"] == 1100.0


def test_load_ignores_a_file_that_does_not_look_right():
    junk = ["", "not json", "[]", "null", "42", '{"scan": 1}', '{"scan": null, "job": 3, "alerted": 4}',
            json.dumps({"scan": {"at": "x"}, "job": {"state": "bogus"}, "alerted": {"sig": 5}}),
            json.dumps({"scan": dict(S(), count="many"), "job": {"state": "running"}, "alerted": {}}),
            json.dumps({"scan": dict(S(), count=1, packages=[{"name": "a"}]), "job": {"state": "running", "started_at": "yesterday", "log": []}, "alerted": {}}),
            *[json.dumps({"scan": dict(S(), **bad), "job": {"state": "running"}, "alerted": {}})              # a field of the wrong kind: the worker would die on it
              for bad in ({"error_at": "x"}, {"error_at": float("nan")}, {"error_at": float("inf")}, {"at": float("nan")}, {"at": 10 ** 400}, {"at": True},
                          {"error": {"a": 1}, "error_at": 5.0}, {"error": 5}, {"reboot_required": "no"})]]
    for text in junk:                                                            # nothing of it is used
        with fleet_state():
            os.makedirs(os.path.dirname(fleet.UPD_FILE))
            with open(fleet.UPD_FILE, "w") as f:
                f.write(text)
            fleet.defiant_updates_load()
            assert fleet._UPD == fleet._upd_fresh(), (text[:50], fleet._UPD)
    with fleet_state():                                                          # a good scan next to a job that does not look right
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": S([P("a")]), "job": {"state": "running", "started_at": 5, "log": "text"}, "alerted": {}}, f)
        fleet.defiant_updates_load()
        assert fleet._UPD["scan"] == S([P("a")]) and fleet._UPD["job"] == fleet._upd_job_idle()
    with fleet_state():                                                              # no file at all
        fleet.defiant_updates_load()
        assert fleet._UPD["scan"] is None and fleet._UPD["job"]["state"] == "idle"
    with fleet_state():                                                              # log entries that are not text are made text
        os.makedirs(os.path.dirname(fleet.UPD_FILE))
        with open(fleet.UPD_FILE, "w") as f:
            json.dump({"scan": None, "alerted": {}, "job": dict(fleet._upd_job_idle(), state="failed", log=[1, None, "x" * 999] * 20)}, f)
        fleet.defiant_updates_load()
        assert len(fleet._UPD["job"]["log"]) == 40 and all(isinstance(l, str) and len(l) <= 300 for l in fleet._UPD["job"]["log"])


def test_a_saved_scan_of_the_wrong_kind_cannot_stop_the_worker():
    """A string as error_at made the worker's start-up arithmetic raise outside its try, which ended the thread silently
    (no scans, no polling of an install); NaN made the view invalid JSON. Such a scan is ignored at load."""
    for bad in ({"error_at": "x"}, {"error_at": float("nan")}, {"at": float("nan")}, {"at": 10 ** 400}):
        with fleet_state():
            os.makedirs(os.path.dirname(fleet.UPD_FILE))
            with open(fleet.UPD_FILE, "w") as f:
                json.dump({"scan": dict(S([P("a", security=True)]), **bad), "alerted": {}, "job": fleet._upd_job_idle()}, f)
            fleet.defiant_updates_load()
            assert fleet._UPD["scan"] is None, bad
            with mock.patch.object(fleet.time, "time", lambda: 10000.0), mock.patch.object(fleet, "_upd_tick", side_effect=KeyboardInterrupt):
                try:
                    fleet.defiant_updates_worker()
                except KeyboardInterrupt:
                    pass                                                             # it got as far as its first tick
            assert fleet._UPD["next_at"] == 10045.0, bad
            json.dumps(fleet.defiant_updates_view(), allow_nan=False)
            json.dumps(fleet.defiant_updates_summary(), allow_nan=False)


def test_a_save_that_cannot_write_does_not_raise():
    with fleet_state() as d:
        blocker = os.path.join(d, "file")
        open(blocker, "w").close()
        with mock.patch.object(fleet, "UPD_FILE", os.path.join(blocker, "x", "u.json")), redirect_stdout(io.StringIO()) as log:
            with fleet._UPD_LOCK:
                fleet._upd_save_locked()
        assert "not saved" in log.getvalue()


# ---------------------------------------------------------------- views

SCAN_KEYS = {"at", "next_at", "busy", "error", "error_at", "count", "security", "packages", "remove", "deferred", "reboot_required", "reboot_pkgs"}
JOB_KEYS = {"state", "started_at", "ended_at", "exit", "log", "lost", "installed", "reboot_required"}


def test_view_shape():
    with mock.patch.object(fleet, "DEFIANT_HOST", ""):
        assert fleet.defiant_updates_view() == {"configured": False}
    with fleet_state():
        v = fleet.defiant_updates_view()
        assert set(v) == {"configured", "now", "scan", "critical", "job"} and v["configured"] is True and v["scan"] is None and v["critical"] is False
        assert abs(v["now"] - time.time()) < 5
        assert v["job"] == {"state": "idle", "started_at": None, "ended_at": None, "exit": None, "log": [], "lost": False,
                            "installed": None, "reboot_required": None}
        fleet._UPD["scan"], fleet._UPD["next_at"] = S([P("a", security=True), P("b")], reboot=True, reboot_pkgs=["k"]), 4600.0
        fleet._UPD["job"] = running_job(1000.0, log=["x"])
        v = fleet.defiant_updates_view()
        assert set(v["scan"]) == SCAN_KEYS and set(v["job"]) == JOB_KEYS and v["scan"]["next_at"] == 4600.0 and v["scan"]["busy"] is False
        assert v["critical"] is True and all(set(p) == {"name", "from", "to", "suites", "origin", "security", "third_party"} for p in v["scan"]["packages"])
        assert v["job"]["state"] == "running" and v["job"]["log"] == ["x"] and "contact_at" not in v["job"]
        json.dumps(v)
        for scan, critical in ((S([P("a", security=True)]), True), (S(reboot=True), True), (S([P("a")]), False)):
            fleet._UPD["scan"] = scan
            assert fleet.defiant_updates_view()["critical"] is critical
        assert fleet.defiant_updates_summary() == {"count": 1, "security": 0, "deferred": 0, "remove": 0, "scan_at": 1000.0, "job_state": "running"}


def test_defiant_state_hook():
    probe_out = ("H:defiant.example\nUP:100.5\nLOAD:0.1 0.2 0.3\nCPUS:2\nOS:Ubuntu\nKERNEL:7.0.0\nMEM:MemTotal:4000000\n"
                 "MEM:MemAvailable:2000000\nDISK:100 50 50\nREBOOT:no\nUPD:5\n")
    calls = []
    with fleet_state(), mock.patch.object(fleet, "_DEF", {"at": 0.0, "data": None}), \
            mock.patch.object(fleet.subprocess, "run", lambda argv, **kw: (calls.append(argv), done(probe_out))[1]):
        d = fleet.defiant_state(force=True)
        assert d["upd"] is None and d["problems"] == [] and d["status"] == "ok" and d["updates"] == 5 and d["reboot_required"] is False
        fleet._UPD["scan"] = S([P("a", security=True), P("b")], at=77.0, remove=["x"], deferred=["h1", "h2"])
        d = fleet.defiant_state(force=True)
        assert d["upd"] == {"count": 2, "security": 1, "deferred": 2, "remove": 1, "scan_at": 77.0, "job_state": "idle"}
        assert d["problems"] == ["1 security update pending"] and d["status"] == "warn" and d["updates"] == 5 and d["reboot_required"] is False
        fleet._UPD["scan"] = S([P("a", security=True), P("b", security=True), P("c")])
        assert fleet.defiant_state(force=True)["problems"] == ["2 security updates pending"]
        fleet._UPD["scan"] = S([P("c")])                                                  # updates but nothing security: no problem
        d = fleet.defiant_state(force=True)
        assert d["problems"] == [] and d["status"] == "ok" and d["upd"]["count"] == 1
        assert len(calls) == 4 and all(a[0] == "ssh" and fleet.DEF_PROBE in a for a in calls)      # only the probe went over ssh
    with mock.patch.object(fleet, "DEFIANT_HOST", ""), mock.patch.object(fleet, "_DEF", {"at": 0.0, "data": None}):
        assert fleet.defiant_state(force=True)["configured"] is False


def test_security_updates_rule_gets_its_summary_through_the_real_flow():
    """The events watcher reads the update summary the way the page does: defiant_state() adds it from memory, no ssh.
    The rule is held while there is no good scan or the host cannot be probed, a failed scan closes nothing, a clean
    scan closes it, and what it shows and pushes is the count only."""
    probe_out = ("H:defiant.example\nUP:1\nLOAD:0 0 0\nCPUS:2\nOS:Ubuntu\nKERNEL:7.0.0\nMEM:MemTotal:4000000\nMEM:MemAvailable:2000000\n"
                 "DISK:100 50 50\nREBOOT:no\nUPD:5\n")

    def no_ssh(argv, **kw):
        raise OSError("ssh: no route to host")
    answer = [lambda argv, **kw: done(probe_out)]
    world = lambda d: {"now": 1e5, "nodes": {}, "running": set(), "errors": {}, "pins": [], "busy": set(), "cfg": {}, "alpha": None,
                       "defiant": d, "sessions": None, "containers": None}
    with fleet_state(), mock.patch.object(fleet, "_DEF", {"at": 0.0, "data": None}), \
            mock.patch.object(fleet.subprocess, "run", lambda argv, **kw: answer[0](argv, **kw)):
        detect = lambda: fleet.ev_detect(world(fleet.defiant_state(force=True)), set())
        c, h = detect()
        assert "updates-security" not in c and "updates-security" in h, "no good scan yet: unknown, held"
        assert "updates-security" in fleet.ev_detect(world(None), set())[1], "a snapshot that failed altogether: held"
        good = S([P("openssl", security=True), P("curl", security=True), P("tzdata")], at=500.0)
        fleet._UPD["scan"] = good
        c, h = detect()
        spec = c["updates-security"]
        assert "updates-security" not in h and spec["level"] == "act" and spec["title"] == "Security updates waiting"
        assert spec["go"] == "#/systems/defiant" and spec["push"] == "2 security updates waiting on Defiant." and "2 security updates" in spec["text"]
        assert not any(n in json.dumps(spec) for n in ("openssl", "curl", "tzdata")), "no package name in what is shown or pushed"
        eps = {}
        assert [a[0] for a in fleet.ev_step(eps, c, h, 0)] == ["open"]
        assert fleet.ev_step(eps, c, h, 3 * fleet.EV_REMIND_S) == [], "no reminder: Defiant installs security updates by itself overnight"
        fleet._UPD["scan"] = dict(good, error="ssh timed out", error_at=900.0)           # the last attempt failed: the good numbers stay
        c, h = detect()
        assert fleet.ev_step(eps, c, h, 4 * fleet.EV_REMIND_S) == [] and "updates-security" in eps, "a failed scan closes nothing"
        assert [a[0] for a in fleet.ev_step({}, c, h, 4 * fleet.EV_REMIND_S)] == ["open"], "and it hides nothing: no episode yet, so the last good numbers open one"
        fleet._UPD["scan"] = S([P("curl", security=True), P("tzdata")], at=4000.0)       # one of two installed: still the one episode
        c, h = detect()
        assert fleet.ev_step(eps, c, h, 5 * fleet.EV_REMIND_S) == [] and c["updates-security"]["push"] == "1 security update waiting on Defiant."
        answer[0] = no_ssh                                                               # the host cannot be probed: unknown, held
        c, h = detect()
        assert "web-host" in c and "updates-security" not in c and "updates-security" in h
        assert fleet.ev_step(eps, c, h, 10 ** 6) == [] and "updates-security" in eps
        answer[0] = lambda argv, **kw: done(probe_out)
        fleet._UPD["scan"] = S([P("tzdata")], at=5000.0)                                 # a clean scan: nothing waiting, nothing unknown
        c, h = detect()
        assert "updates-security" not in c and "updates-security" not in h
        acts = fleet.ev_step(eps, c, h, 10 ** 6 + 5)
        assert [a[0] for a in acts] == ["resolve"] and "No security updates" in acts[0][2]["ok"] and "updates-security" not in eps


# ---------------------------------------------------------------- endpoints

def call(port, method, path, body=None, key=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"X-Starfleet-Key": key} if key else {})
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, json.loads(raw)


@contextmanager
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    port = srv.server_address[1]
    assert port > 20000 and port not in (8090, 8444, 28080, 28081, 18081), port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_http_endpoints():
    no_network = mock.Mock(side_effect=AssertionError("a request reached the network (llama-swap?)"))
    with fleet_state(), mock.patch.object(fleet.urllib.request, "urlopen", no_network):
        ssh = mock.Mock(side_effect=AssertionError("a request thread used ssh"))
        with mock.patch.object(fleet, "_def_ssh", ssh), server() as port, redirect_stdout(io.StringIO()):
            st, h, v = call(port, "GET", "/api/defiant/updates")
            assert st == 200 and h["cache-control"] == "no-store" and h["content-type"] == "application/json"
            assert set(v) == {"configured", "now", "scan", "critical", "job"} and v["scan"] is None and v["job"]["state"] == "idle"
            fleet._UPD["scan"] = S([P("a", security=True)])
            st, h, v = call(port, "GET", "/api/defiant/updates")
            assert st == 200 and v["scan"]["count"] == 1 and v["critical"] is True and set(v["scan"]) == SCAN_KEYS
            # scan: no key, answered at once, never scanned in the request
            st, h, v = call(port, "POST", "/api/defiant/updates/scan", {})
            assert (st, v) == (200, {"ok": True, "started": True}) and h["cache-control"] == "no-store"
            assert call(port, "POST", "/api/defiant/updates/scan")[2] == {"ok": True, "started": False, "reason": "busy"}
            assert call(port, "GET", "/api/defiant/updates")[2]["scan"]["busy"] is True
            fleet._UPD["busy"], fleet._UPD["want"], fleet._UPD["started"] = False, False, time.time()
            assert call(port, "POST", "/api/defiant/updates/scan", {})[2] == {"ok": True, "started": False, "reason": "recent"}
            fleet._UPD["started"] = 0.0
            # install: the key first
            for key in (None, "wrong"):
                st, h, v = call(port, "POST", "/api/defiant/updates/install", {}, key)
                assert st == 403 and v == {"error": fleet.POLICY}, (st, v)
            assert fleet._UPD["job"]["state"] == "idle"
            ssh.side_effect = None
            ssh.return_value = done("Running as unit: fleet-apt-upgrade.service\nFLEET-RC:0\n")
            st, h, v = call(port, "POST", "/api/defiant/updates/install", {}, "fixture-key")
            assert st == 200 and v["ok"] is True and isinstance(v["started_at"], float) and h["cache-control"] == "no-store"
            assert call(port, "GET", "/api/defiant/updates")[2]["job"]["state"] == "running"
            st, h, v = call(port, "POST", "/api/defiant/updates/install", {}, "fixture-key")
            assert st == 409 and "already running" in v["error"]
            assert call(port, "POST", "/api/defiant/updates/scan", {})[2] == {"ok": True, "started": False, "reason": "busy"}
            fleet._UPD["job"] = fleet._upd_job_idle()
            ssh.return_value = done("", "ssh: connect to host defiant.example port 22: Connection refused", 255)
            st, h, v = call(port, "POST", "/api/defiant/updates/install", {}, "fixture-key")
            assert st == 502 and "Connection refused" in v["error"] and len(v["error"]) <= 200 and fleet._UPD["job"]["state"] == "idle"
            ssh.side_effect = subprocess.TimeoutExpired(["ssh", "defiant.example", "a script text"], 40)
            st, h, v = call(port, "POST", "/api/defiant/updates/install", {}, "fixture-key")
            assert st == 502 and "timed out" in v["error"] and "defiant.example" not in v["error"] and "a script text" not in v["error"], v      # not the ssh command line
            assert fleet._UPD["job"]["state"] == "running"                                       # the unit may be running: it is followed
            fleet._UPD["job"] = fleet._upd_job_idle()
            ssh.side_effect = None
            ssh.return_value = done("FLEET-BUSY\n")
            assert call(port, "POST", "/api/defiant/updates/install", {}, "fixture-key")[0] == 409
            assert fleet._UPD["job"]["state"] == "running"                                       # a unit Fleet knew nothing of is followed too
            fleet._UPD["job"] = fleet._upd_job_idle()
            with mock.patch.object(fleet, "DEFIANT_HOST", ""):
                assert call(port, "GET", "/api/defiant/updates")[2] == {"configured": False}
                st, h, v = call(port, "POST", "/api/defiant/updates/scan", {})
                assert st == 400 and "not configured" in v["error"]
                st, h, v = call(port, "POST", "/api/defiant/updates/install", {}, "fixture-key")
                assert st == 400 and "not configured" in v["error"]
        assert no_network.call_count == 0


def test_main_starts_the_worker_only_when_defiant_is_configured():
    for host, expected in (("defiant.example", True), ("", False)):
        targets, loads = [], []
        server = mock.Mock()
        with mock.patch.object(fleet, "DEFIANT_HOST", host), mock.patch.object(sys, "argv", ["fleet.py"]), \
                mock.patch.object(fleet.threading, "Thread", lambda target=None, **kw: (targets.append(target), mock.Mock())[1]), \
                mock.patch.object(fleet, "ThreadingHTTPServer", mock.Mock(return_value=server)), \
                mock.patch.object(fleet, "default_host", lambda: "127.0.0.1"), mock.patch.object(fleet, "load_pins", lambda: []), \
                mock.patch.object(fleet, "defiant_updates_load", lambda: loads.append(1)), redirect_stdout(io.StringIO()):
            fleet.main()
        assert (fleet.defiant_updates_worker in targets) is expected and len(loads) == (1 if expected else 0), (host, targets)
        assert fleet.poll_nodes in targets and server.serve_forever.called                       # the rest still starts


# ---------------------------------------------------------------- the shell scripts, against stubs

def bash(script, stubs=""):
    """Run a script in a local bash whose sudo, timeout, apt-get, systemctl and date are stub functions: it only
    ever prints. Returns (exit code, stdout)."""
    r = real_run(["bash", "-c", stubs + "\n" + script], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout


def have_bash():
    try:
        return real_run(["bash", "-c", "true"], timeout=10).returncode == 0
    except OSError:
        return False


def unit_script(allow_remove):
    """The unit's own script, taken out of the start command: its one single-quoted argument."""
    return re.search(r"/bin/sh -c '([^']*)'", fleet.upd_start(allow_remove)).group(1)


def test_scripts_parse_as_shell():
    for name, script in (("UPD_SCAN", fleet.UPD_SCAN), ("UPD_POLL", fleet.UPD_POLL), ("upd_start(True)", fleet.upd_start(True)),
                         ("upd_start(False)", fleet.upd_start(False)), ("unit script", unit_script(True)),
                         ("unit script with --no-remove", unit_script(False))):
        for sh in (["/bin/sh", "-n"], ["bash", "-n"]):
            try:
                r = real_run(sh + ["-c", script], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
            except OSError:
                continue
            assert r.returncode == 0, (name, sh, r.stderr)


def test_start_script_passes_the_unit_script_as_one_argument():
    if not have_bash():
        return
    # sudo prints its arguments instead of running them, so systemd-run never starts
    stubs = 'systemctl() { return 3; }\nsudo() { shift; printf "[%s]\\n" "$@"; }'
    for allow in (True, False):
        rc, out = bash(fleet.upd_start(allow), stubs)
        args = re.findall(r"^\[(.*)\]$", out, re.M)
        assert rc == 0 and args[0] == "systemd-run" and args[-1] == unit_script(allow) and out.rstrip().endswith("FLEET-RC:0"), out
        assert "--unit=fleet-apt-upgrade" in args and "--collect" in args and "StandardOutput=truncate:/var/log/fleet-apt-upgrade.log" in args and "StandardError=inherit" in args
        assert args[args.index("/bin/sh") + 1] == "-c" and args.count("-E") == 3 and "NEEDRESTART_MODE=a" in args
        assert ("--no-remove" in args[-1]) is (not allow) and [a for a in args if "--no-remove" in a] == ([] if allow else [args[-1]])
        # a unit that is already active stops the start before sudo is reached
        rc, out = bash(fleet.upd_start(allow), 'systemctl() { return 0; }\nsudo() { echo REACHED; }')
        assert out.strip() == "FLEET-BUSY"


def test_unit_script_writes_markers_and_passes_the_exit_code_on():
    if not have_bash():
        return
    stubs = 'apt-get() { echo "apt-get $*"; return 7; }\ndate() { echo 1700000000; }'
    for allow, flag in ((True, ""), (False, "--no-remove ")):                    # the flag is on the full-upgrade only, never on the update
        rc, out = bash(unit_script(allow), stubs)
        assert rc == 7
        assert out.splitlines() == ["FLEET-APT-START 1700000000", "apt-get -o DPkg::Lock::Timeout=600 -qq update",
                                    "apt-get -o DPkg::Lock::Timeout=600 -y -o Dpkg::Options::=--force-confold %sfull-upgrade" % flag,
                                    "FLEET-APT-EXIT 7 1700000000"]
        rc, out = bash(unit_script(allow), stubs.replace("return 7", "return 0"))
        assert rc == 0 and out.splitlines()[-1] == "FLEET-APT-EXIT 0 1700000000"
        x = fleet._JOB_EXIT.match(out.splitlines()[-1])
        assert x and x.group(1) == "0"


def test_scan_script_output_parses():
    if not have_bash():
        return
    sim_file = os.path.join(HERE, "fixtures", "apt-sim-large.txt")
    stubs = ('timeout() { shift; "$@"; }\nsudo() { shift; echo "W: stubbed, nothing ran"; return ${UPDATE_RC:-0}; }\n'
             'apt-get() { cat "%s"; }' % sim_file)
    rc, out = bash(fleet.UPD_SCAN, stubs)
    s = fleet.parse_scan_output(out)
    assert s["count"] == 20 and s["security"] == 11 and len(s["deferred"]) == 21
    rc, out = bash(fleet.UPD_SCAN, stubs.replace("${UPDATE_RC:-0}", "100"))
    try:
        fleet.parse_scan_output(out)
    except ValueError as e:
        assert "exit 100" in str(e) and "stubbed" in str(e)
    else:
        raise AssertionError("a failed update was not reported")


def test_poll_script_against_a_real_log_file():
    if not have_bash():
        return
    with tempfile.TemporaryDirectory(prefix="fleet-upd-") as d:
        log = os.path.join(d, "fleet-apt-upgrade.log")
        lines = (["FLEET-APT-START 1700000000", "Reading package lists...", "5 upgraded, 1 newly installed, 0 to remove and 3 not upgraded."]
                 + ["Setting up pkg%d (1.%d) ..." % (i, i) for i in range(60)] + ["FLEET-APT-EXIT 0 1700000100"])
        with open(log, "w") as f:
            f.write("\n".join(lines) + "\n")
        rc, out = bash(fleet.UPD_POLL.replace(fleet.UPD_LOG, log), 'systemctl() { echo inactive; }')
        obs = fleet.parse_job_probe(out)
        assert obs["active"] == "inactive" and obs["setup"] == 60 and obs["summary"] == (5, 1) and obs["reboot"] in (True, False)
        assert obs["log"] == lines[-40:] and len(obs["log"]) == 40
        j = fleet.upd_job_step(running_job(1699999990.0), obs, 1700000200.0)
        assert j["state"] == "done" and j["exit"] == 0 and j["installed"] == 6 and j["ended_at"] == 1700000100.0
        rc, out = bash(fleet.UPD_POLL.replace(fleet.UPD_LOG, os.path.join(d, "missing.log")), 'systemctl() { echo active; }')
        obs = fleet.parse_job_probe(out)
        assert obs["active"] == "active" and obs["log"] == [] and obs["summary"] is None


# ---------------------------------------------------------------- runner

def _forbidden(*a, **kw):
    raise AssertionError("a test reached a real subprocess: %r" % (a[:1],))


def main():
    # safety nets: no real subprocess, and never the real saved-state file, whatever a test forgets to patch
    tmp = tempfile.mkdtemp(prefix="fleet-upd-run-")
    fleet.UPD_FILE = os.path.join(tmp, "unpatched", "defiant-updates.json")
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    with mock.patch.object(subprocess, "run", _forbidden), mock.patch.object(subprocess, "Popen", _forbidden):
        for name, fn in tests:
            try:
                fn()
                print("ok    " + name)
            except Exception:
                failed.append(name)
                print("FAIL  " + name)
                traceback.print_exc()
    if os.path.exists(os.path.dirname(fleet.UPD_FILE)):
        failed.append("(a test wrote the unpatched state file)")
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d tests, %d failed%s" % (len(tests), len(failed), ": " + ", ".join(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
