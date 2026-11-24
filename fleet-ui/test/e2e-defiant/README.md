# End to end test: Defiant updates and reboot

Runs the real `fleet.py` against a made-up Defiant and drives the real desktop page and phone page in WebKit.

    PW=<folder of the playwright package> ./run.sh          # all stories, about 17 minutes
    ./run.sh schedule                                       # only the stories whose name contains "schedule"

Needs Linux with GNU tools (it reads `/proc`, uses `setsid`, `timeout`, `date +%N`), Python 3.8+, and node with playwright
for the page stories. One line per check, exit code 0 only when every check passed. `KEEP=1` keeps the sandbox (logs), `OUT=` sets the
folder for the pictures, `FONTS=1` lets the pages load Google Fonts. Without it the page stories run WebKit with a dead loopback port as
its proxy (the pages' own origin is bypassed), so no connection leaves the machine, not even the font preconnects that both pages
carry; `strace -f -e trace=execve,connect` over a whole run shows loopback addresses and the browser's own local sockets only, and
every `systemctl` and `systemd-run` that was executed is a stand-in of `fake-bin` (no program that reboots a machine is ever executed).

## What is real, what is made up

Real: `fleet.py` (started by `fleet_e2e.py`, which only shortens its intervals to seconds and, for one story, pauses before a scan starts), the scripts it sends
over ssh (scan, poll, install unit, reboot start, reboot poll, probe), its HTTP API, `fleet.html` and `padd.html`.

Made up: the host. `fake-bin/ssh` accepts only `fake@defiant.example` and only the scripts Fleet is known to send (any other
script is refused), and runs the script with bash in a sandbox, where `sudo`, `apt-get`, `apt`, `systemctl`, `systemd-run`, `fuser`,
`docker`, `hostname`, `df` and `curl` are stand-ins. What the host answers is set by files in the sandbox: `apt-sim.txt` (the
simulated upgrade, one of `fixtures/`; a `Remv` line in it means apt would have to remove a package, and then `apt-get --no-remove`
refuses, exit 100, as apt does), `job.conf` (how the next install ends, whether it leaves a reboot pending and whether the nightly
run takes the apt lock the moment it ends), `reboot.conf` (what a reboot does), `apt-lock` (the lock: `apt-get update` fails and
`fuser` says the dpkg lock is in use), `fuser-broken` (sudo cannot run fuser: `fuser -V` fails, and the lock check says 1, as sudo does), `active-UNIT` (a unit that someone else started), `systemd-run-fails` (systemd-run cannot start a timer), `systemd-run-hangs` (it starts the timer, then does not return for 33 s), `scan-delay`, `ssh-down-until`.
`applied.log` gets a line for every upgrade that went through, so a test can tell it from one that was refused. `telegram.py`
records messages instead of sending them, `gate.py` answers the key check (`fixture-key` is the only valid key) and nothing else.

A reboot on the made-up host: `systemd-run --on-active=3s` starts a timer; when it fires it calls the stand-in `systemctl reboot`,
which makes ssh exit 255 for `DOWN` seconds (`reboot.conf`), clears the reboot flag and gives the host a new boot id once it answers
again (`boot-id`, `boot-next`; the uptime comes from `boot-at`). `NEVER=1` makes the host ignore the reboot, and a very large
`DOWN` is a host that never comes back. Nothing in the stand-ins can reboot a machine: the timer runs only the stand-in
`systemctl` by its own path and never the path it was given (`/bin/systemctl`), `systemd-run` refuses a service whose command
names a way to reboot or power off, `sudo` runs only stand-ins and no path, `ssh` refuses a script that Fleet is not known to
send and any script that names a way to reboot or power off apart from the one line that schedules the reboot (that line is
pinned in `fake-bin/ssh`: a change of it has to be made there too, on purpose), and no program that reboots or powers off a
machine is on the PATH at all. `e2e.py` checks all of that first, with a dry run where a missing wall could do harm.

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
The reboot stories do the same for the reboot's waits (`RB.update(...)`, passed as `E2E_REBOOT_POLL_S`, `E2E_REBOOT_START_WAIT`,
`E2E_REBOOT_GIVEUP` and `E2E_REBOOT_GAP`): the worker polls a running reboot every second instead of every 5, the "still on the
same boot" and "did not come back" limits are seconds instead of minutes, so those two failures can be seen, and the gap that
holds the button back after a reboot (or after a start that got no answer) is seconds or a minute instead of ten minutes. The story
`reboot-scan-race` also makes the pause between the worker's decision to scan and the scan itself last six seconds (`SCAN_LAG`,
passed as `E2E_SCAN_LAG` and applied in `fleet_e2e.py`, test code only), so that a reboot request can arrive inside it.

An install that the stories start through the API is sent the way both pages send it, with the body `{"reboot_ok": true}`: that
flag is what lets Fleet reboot Defiant after an install that needs one (`install-without-flag` sends other bodies). The stories
that expect no reboot send it too, so that the rule they prove is the one that decides.

What a reboot leaves in the Messages list is read from the sandbox's events file (`trail()` in `e2e.py`): one list-only entry when
a reboot starts (`Defiant reboot started`), one when it ends (`Defiant rebooted`, with the seconds) and one when it fails
(`Defiant reboot failed`, with the error cut at a word at 140 characters). A refusal of the button, and a start by the button that
got no answer, write none: no reboot ran that Fleet could follow.

## Stories

`sandbox` the walls above, with a positive control: a made-up device registers, a test push is recorded, the test entry lands
in the sandbox's events folder, curl never runs. `security-push` the push for security updates: one recorded push (title, count,
the way to the Defiant view, no package name), the entry in `GET /api/events`, the long text on Telegram once, then a scan with
the same updates plus a Fleet restart, a Fleet that has lost its saved scan (what it knows is unknown, not clean: the episode
is held), a scan with another count and a failed scan (none of them pushes, adds an entry or closes the episode), and finally
the install and its rescan, which resolve the episode without another push. `security-message` the same rule on a Fleet
without a push key: the entry is written, nothing is pushed. `scan-and-alerts` first scan (stub, error, busy), the alert text
and its dedupe, restart, clearing, a failed send, a failed scan, fewer alerted packages than before (no news). `rate-limit`. `schedule` (scans by itself, retry after
`UPD_RETRY`, never two at once). `install` (no key, wrong key, gate down, success, 409, progress, rescan, no reboot after an
install that needs none, a short failing install right after a long one). `install-guard` (the scan lists no removals, then the host needs one: apt is run with `--no-remove`, refuses, exit 100, the job ends failed with
the hint, one Telegram message carries it, a rescan lists the removal, the next install runs without the flag and goes
through) and `install-removals` (the scan lists a removal: no flag). `install-failure` (also: no scan yet, so the flag).
`ssh-drop`. `restart-mid-job` and `unit-unknown-to-fleet` (an install that needs a reboot, from a click that announced it, but a
unit that Fleet did not start by a click of this Fleet: it is followed to its end and not followed by a reboot).

The Reboot Defiant button and the reboot after an install:

- `reboot-button` the button against a host that wants a reboot: what the host is told (a timer three seconds ahead for a plain
  `systemctl reboot`, after the three lock files were asked), the refusals while it runs (a second reboot, an install, a scan request),
  Defiant going down and coming back with another boot id, the probe that says it does not answer meanwhile, no scan or install poll
  while it runs, the rescan that clears the reboot flag, no Telegram message for a reboot that worked, the gap that refuses the next one,
  and no push to the phone (the story gives Fleet a made-up push key and one made-up device, so that a push would be recorded).
- `reboot-refusals` no key, a wrong key and a gate that does not answer (403, the host is not asked), then a scan that is busy, apt
  that holds its lock, a reboot timer already on the host, an install that Fleet knows of and one that only the host knows of
  (409 each), a Defiant that does not answer, one whose lock check cannot be made (sudo cannot run fuser) and one whose timer cannot be
  started (502 each): nothing scheduled, nothing rebooted, the same boot id.
- `reboot-no-answer` a start that gets no answer: the stand-in systemd-run sets the timer and then hangs (`systemd-run-hangs`), Fleet gives up after its 30 s with 502 and the fixed
  text that says Defiant may still reboot, the host reboots all the same, and a second click is refused (409) and schedules nothing more.
  `reboot-unsure-restart` the same start, and then a restart of Fleet inside the gap: the saved file carries the time of the request, the
  restarted Fleet still answers a click with 409 and schedules nothing more, and once the gap is over (a minute here) the button works again.
  `reboot-mid-start-restart` a restart of Fleet while the start call is on its way: the stand-in systemd-run sets the timer and then hangs, and Fleet is stopped
  before it gets the answer, with no cleanup, as a deploy stops it. The saved file already holds the time of the request (it is written before Defiant is asked), the restarted
  Fleet answers a click with 409 and schedules nothing more, and the made-up host rebooted all the same.
  `reboot-scan-race` a scan that the worker has decided on but not yet started, and a reboot requested in that moment: the scan is
  skipped, nothing but the reboot's own calls reaches the host until the reboot ends, and the scan runs after it.
- `reboot-never` a host that ignores the reboot: failed after `REBOOT_START_WAIT` with the fixed text (it says that Defiant may still
  reboot and where to look) and one Telegram message, still failed later, and the button works again. `reboot-giveup` a host that
  goes down and does not come back: failed after `REBOOT_GIVEUP`, one message, and a Defiant that answers again later does not undo
  it. `reboot-restart` a Fleet restart while Defiant is down: the reboot resumes from the saved state and ends done, and no scan
  starts although one is due all the time.
- `reboot-after-install` an install that needs a reboot is followed by one that Fleet starts in the tick that sees the install end:
  `auto`, in spite of the gap, with no second key check, the rescan after the reboot and not after the install, no Telegram message.
  `reboot-after-install-busy` the nightly run takes the apt lock the moment the install ends: the reboot is failed with the reason,
  one Telegram message, the install stays done, nothing is scheduled, and the button works once apt is idle. `no-auto-reboot` an
  install that needs none, a failed install on a host that wants one and the scans that find one: none starts a reboot.
  `install-without-flag` an install that leaves a reboot pending, started by a click without the flag (an older page's `{}`, the
  string `"true"`, a list): it ends done and none follows (nothing asked of the host, the scan shows the reboot waiting, the alert
  for it is as always); then the same install with the flag, which Fleet follows with its own reboot, without any push (a ready sender and a registered device again).

`page-desktop` and `page-phone` (no scan yet, failed first scan, Scanning..., data, Scan now, wrong key, confirm (when no reboot is
waiting it says that Defiant reboots by itself if one is needed), progress (the Mac page's Scan hint says that a scan follows the
install and the reboot after it), result, an install that needs a reboot and the reboot Fleet starts after it (the pages send the
flag, so it follows: the lines below the install result, Defiant going down, the seconds counting up, back, the rescan), a failed
install, an install that apt refuses and the removal named in the next scan and confirm step; then the Reboot Defiant button by
itself: a wrong key, the amber button while a reboot is waiting, the button off (with its reason) while a scan runs, a reboot while
Defiant does not answer, the confirm step and its 450 ms guard, a reboot that does not happen (the red line stays) and one that
works; an install whose own reboot cannot start (the red line below the install result, then the hint to use the button once a scan
finds the reboot still wanted); last the install confirm while that reboot is waiting, which says so definitely; the phone also
checks its Systems row and Bridge item). Each of the two stories ends by checking the Messages list: one entry for each start, end and
failure.
