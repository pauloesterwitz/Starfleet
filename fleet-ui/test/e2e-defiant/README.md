# End to end test: Defiant updates

Runs the real `fleet.py` against a made-up Defiant and drives the real desktop page and phone page in WebKit.

    PW=<folder of the playwright package> ./run.sh          # all stories, about 6 minutes
    ./run.sh schedule                                       # only the stories whose name contains "schedule"

Needs Linux with GNU tools (it reads `/proc`, uses `setsid`, `timeout`, `date +%N`), Python 3.8+, and node with playwright
for the page stories. One line per check, exit code 0 only when every check passed. `KEEP=1` keeps the sandbox (logs), `OUT=` sets the
folder for the pictures, `FONTS=1` lets the pages load Google Fonts. Without it the page stories run WebKit with a dead loopback port as
its proxy (the pages' own origin is bypassed), so no connection leaves the machine, not even the font preconnects that both pages
carry; `strace -f -e trace=connect` over a run shows loopback addresses only.

## What is real, what is made up

Real: `fleet.py` (started by `fleet_e2e.py`, which only shortens its intervals to seconds), the scripts it sends
over ssh (scan, poll, install unit, probe), its HTTP API, `fleet.html` and `padd.html`.

Made up: the host. `fake-bin/ssh` accepts only `fake@defiant.example` and runs the script with bash in a sandbox,
where `sudo`, `apt-get`, `apt`, `systemctl`, `systemd-run`, `docker`, `hostname`, `df` and `curl` are stand-ins. What the
host answers is set by files in the sandbox: `apt-sim.txt` (the simulated upgrade, one of `fixtures/`; a `Remv` line in it
means apt would have to remove a package, and then `apt-get --no-remove` refuses, exit 100, as apt does), `job.conf`
(how the next install ends), `apt-lock`, `scan-delay`, `ssh-down-until`. `applied.log` gets a line for every upgrade that
went through, so a test can tell it from one that was refused. `telegram.py` records messages instead of sending them,
`gate.py` answers the key check (`fixture-key` is the only valid key) and nothing else.

Fleet runs with a sandbox `HOME` and a `PATH` of stand-ins and a few harmless tools, so it cannot reach a real host,
service, model or file of its own. `e2e.py` checks that, and the gate log proves Fleet asked for nothing but the key
check and the (empty) model lists.

The Messages system (events and push) and the Speech probe run in that Fleet too, so they are walled in as well:
`FLEET_EVENTS_DIR` is a folder of the sandbox, `fleet_e2e.py` points the device file there, replaces `push_send` and
`apns_post` with recorders that append to `push.jsonl` (never the device token) and points the Speech tool at a path that
does not exist; `curl`, the program that would reach Apple, is a stand-in that only logs; `run.sh` unsets the push variables
of the shell, and Fleet's environment is built from nothing. `start_fleet(push=True)` gives Fleet a made-up push key
(a file of the sandbox) so its push sender reads as ready: what it would push then lands in `push.jsonl`. A made-up
device registers through the real endpoint, so no function of Fleet is patched to make the sender ready. The Messages
worker waits 45 seconds before its first look, 2 minutes between its slow checks and 6 hours before a reminder: the stories
`security-push` and `security-message` shorten these (`EV.update(...)` in `e2e.py`, passed as `E2E_EV_GRACE`, `E2E_EV_HEAVY`
and `E2E_EV_REMIND_S` and applied in `fleet_e2e.py`, test code only); every other story runs with Fleet's own waits.

## Stories

`sandbox` the walls above, with a positive control: a made-up device registers, a test push is recorded, the test entry lands
in the sandbox's events folder, curl never runs. `security-push` the push for security updates: one recorded push (title, count,
the way to the Defiant view, no package name), the entry in `GET /api/events`, the long text on Telegram once, then a scan with
the same updates plus a Fleet restart, a Fleet that has lost its saved scan (what it knows is unknown, not clean: the episode
is held), a scan with another count and a failed scan (none of them pushes, adds an entry or closes the episode), and finally
the install and its rescan, which resolve the episode without another push. `security-message` the same rule on a Fleet
without a push key: the entry is written, nothing is pushed. `scan-and-alerts` first scan (stub, error, busy), the alert text
and its dedupe, restart, clearing, a failed send, a failed scan, fewer alerted packages than before (no news). `rate-limit`. `schedule` (scans by itself, retry after
`UPD_RETRY`, never two at once). `install` (no key, wrong key, gate down, success, 409, progress, rescan, reboot, a
short failing install right after a long one). `install-guard` (the scan lists no removals, then the host needs one: apt is run with `--no-remove`, refuses, the job ends failed with
the hint, one Telegram message carries it, a rescan lists the removal, the next install runs without the flag and goes
through) and `install-removals` (the scan lists a removal: no flag). `install-failure` (also: no scan yet, so the flag).
`ssh-drop`. `restart-mid-job`.
`unit-unknown-to-fleet` (a unit that runs on Defiant while Fleet has no job is followed from the next click on).
`page-desktop` and `page-phone` (no scan yet, failed first scan, Scanning..., data, Scan now, wrong key, confirm,
progress, result, rescan, an install that apt refuses and the removal named in the next scan and confirm step; the phone also
checks its Systems row and Bridge item).
