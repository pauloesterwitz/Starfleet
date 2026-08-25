# Starfleet Command

Menu-bar app that polls two DGX Sparks over SSH every 5s for opencode
session status + host RAM/GPU load: **Jean-Luc** (`spark-2706`, the
original box) and **Kathryn** (a second, newer Spark). No Dock icon, no
third-party dependencies. Renamed from "OpencodeMonitor" — same app,
Starfleet theming to match the rest of this project (Jean-Luc/Kathryn are
Star Trek captains; this was the natural next step).

## Setup (on the Mac)

1. Copy this folder to the Mac (`scp -r`, `rsync`, or git).
2. Confirm passwordless SSH works exactly as the app will call it, for
   both hosts:
   ```
   ssh -o BatchMode=yes spark-2706 ~/bin/opencode-status.py
   ssh -o BatchMode=yes kathryn ~/bin/opencode-status.py
   ```
   Should print JSON with no password/passphrase prompt. If it prompts, load your
   key into the agent (`ssh-add --apple-use-keychain ~/.ssh/<key>`) or set
   `UseKeychain yes` in `~/.ssh/config` for each host.
3. Add a control-connection stanza per host to `~/.ssh/config` so the 5s poll
   loop reuses one SSH connection instead of a fresh handshake each time.
   **Use the Tailscale MagicDNS FQDN as `Hostname`, not a pinned raw `100.x`
   IP** — a pinned IP goes stale whenever Tailscale reassigns the device's
   address (this broke live, for both hosts at once, right after switching
   networks — see "Known limits" below):
   ```
   Host spark-2706
     Hostname jean-luc.<your-tailnet>.ts.net
     ControlMaster auto
     ControlPersist 60s
     ControlPath ~/.ssh/cm-%r@%h:%p

   Host kathryn
     Hostname kathryn.<your-tailnet>.ts.net
     ControlMaster auto
     ControlPersist 60s
     ControlPath ~/.ssh/cm-%r@%h:%p
   ```
4. Quick test: `swift run`. For real use, package it instead: `./package.sh`
   builds `Starfleet Command.app` (release build, ad-hoc signed) — open that,
   or drag it to `/Applications`. The `.app` form is what makes notifications
   and "Launch at Login" work; bare `swift run` often can't register either.

The menu bar shows two colored dots (Jean-Luc, then Kathryn, fixed
left-right order — green = all sessions normal, red = something's stalled,
gray = can't reach that host) plus a `⚡ N` total-processing-session count
across both, shown only when `N > 0`. Hover the label for a tooltip with
each host's last error, if any. Click it for the full per-host breakdown:
session list, host stats (RAM/GPU/temp), and a "Launch at Login" toggle.
The first time a session stalls you'll also get a system notification
(once per stall episode, not spammed every 5s), prefixed with which host
it's from.

The session list (both hosts' Claude Code + opencode sections together)
scrolls inside a capped `ScrollView` (max height 420pt) rather than growing
the dropdown freely -- two hosts' worth of sessions, especially with a
transcript expanded, can easily outgrow screen height otherwise. The
"Show desktop widget"/"Launch at Login"/"Quit" controls sit below it,
outside the scrollable area, so they're always reachable regardless of how
long the session list gets.

Click a session's row to expand it and see its last few messages (via
`~/bin/opencode-export-tail.py` on that host) — text replies, and which
tool ran with what status, so you have context before deciding whether to
respond. Read-only for now; re-fetches fresh every time you expand it.

Each session row's own dot (distinct from the two host-level dots above) is
purely recency-based, the same rule for both Claude Code and opencode
sessions: green if updated less than 30 minutes ago, orange between 30
minutes and 8 hours, gray past 8 hours. Sessions idle more than 24 hours
don't show up at all (`SessionStatus.recentEnoughToShow` in `Models.swift`,
enforced in the UI regardless of what either host's script itself already
filters to). This replaced an earlier scheme keyed off opencode's own
`working`/`waiting`/`idle` classification, which doesn't have a directly
comparable equivalent for Claude Code sessions.

## Notes / known limits

- The 2026-07-21 rename (OpencodeMonitor → Starfleet Command) changed
  `CFBundleIdentifier` too (`com.oesterwitz.opencodemonitor` →
  `com.oesterwitz.starfleetcommand`) -- macOS treats it as a genuinely new
  app, same as the Captain's Log rename. Notification permission and
  "Launch at Login" registration are tied to the identifier, so both
  needed re-confirming after the first launch under the new name; any
  saved desktop-widget position/visibility from before also reset to
  defaults (orphaned under the old identifier, not deleted).
- "Stalled" is a heuristic (process alive, CPU idle, DB untouched >90s, todos
  still open) — opencode doesn't expose a real "waiting for permission" flag
  today. Watch it for a while and tighten the threshold in
  `~/bin/opencode-status.py` (`STALE_SECS`) on each host if it's too
  jumpy/lazy.
- Hardcoded to two named hosts (`StatusPoller(host:label:)` instantiated
  twice in `App.swift`) — add a settings UI only if you ever need a third.
- **Pin SSH `Hostname` to the Tailscale MagicDNS FQDN, never a raw `100.x`
  IP.** Observed live (2026-07-15): switching this Mac from home Wi-Fi to a
  phone hotspot triggered a Tailscale IP reassignment for Jean-Luc, and
  every alias pinned to the old IP broke at once (Kathryn had also just
  joined the tailnet, replacing an interim LAN-IP-only alias with the same
  MagicDNS pattern). The FQDN always resolves to whatever the device's
  current tailnet IP is; a bare unqualified name (e.g. `spark-2706` with no
  domain) isn't safe either — it races against the home router's own mDNS
  answer for the same short name.
- Kathryn is a brand-new Spark: `~/bin/opencode-status.py` and
  `~/bin/opencode-export-tail.py` are deployed there, but `opencode` itself
  isn't installed yet, so its session list stays empty and the script
  reports `"error": "unable to open database file"` until it's set up and
  actually used there. This does NOT turn Kathryn's dot gray -- the dot
  means "is this host reachable over SSH", not "does its opencode DB
  exist" (`StatusPoller.poll()` only sets `lastError` from a connection-level
  failure -- SSH failure, timeout, Tailscale reauth -- never from the
  payload's own `error` field). The payload error is shown as plain
  informational text instead, alongside the host's live RAM/GPU stats,
  which is itself proof the host answered.

## Desktop widget

A checkbox toggle in the dropdown ("Show desktop widget") opens a
borderless, always-on-top floating panel with the same live data, stacked
per host (Jean-Luc's stats + session list, then Kathryn's) — for when you
want it glanceable without clicking the menu bar icon. It's a plain
`NSPanel` (not a WidgetKit widget): real desktop widgets only refresh a few
times an hour, which can't carry the 5s poll / ~90s stall detection this
app is built around, so a floating panel sharing the same `StatusPoller`s
is the closer fit. Drag it anywhere; position and the on/off state both
persist across restarts.

## "Currently processing" marker

A green ⚡ next to a session's title (menu bar dropdown and desktop widget)
marks it as actively generating a response right now, and such sessions
always sort first (and, in the widget, are never pushed out by the
per-host row cap). This does NOT use `status`/`cpu_pct` -- those are
derived from `match_pid()`, which falls back to attributing ANY opencode
process in a session's directory when it can't find the session ID in a
process's cmdline, so several sessions sharing a directory all inherit one
active process's CPU and get falsely marked "working" (observed live: a
session last touched 3+ hours ago still showed "working"). Instead,
`opencode-status.py` now also emits a `generating` field computed from
real ground truth: a session's *latest* message row in the DB has a
`time` object in its JSON that gets a `completed` key only once that
message finishes -- so "latest message is from the assistant and has no
`completed` yet" means it's genuinely mid-generation right now (see
`fetch_generating_flags()`). A dangling last message from the *user*
(no reply yet) also lacks `completed` but is correctly excluded by
checking the role. This is additive only -- `classify()`/`status`/
`stalled` are untouched, so the Telegram bot's `/sessions`/`/pending`
(which read `status`) are unaffected.

## Claude Code sessions

Each host section now shows two labeled sub-groups: **Claude Code** sessions,
then **OpenCode** sessions -- two entirely separate tools with separate data
sources, so they never get merged under one heading. `~/bin/opencode-status.py`
also scans `~/.claude/projects/*/*.jsonl` (Claude Code's own transcript
files, one per session) alongside its original opencode DB query, and tags
every session with a `tool` field (`"claude-code"` or `"opencode"`) so the
Mac app knows which group to render it in.

Claude Code has no separate DB and no per-session PID to correlate (sessions
run through a remote server/bridge process, not one directly-attributable
CLI process each like opencode) -- so for these sessions:
- `pid`/`cpu_pct` are always null/0; recency (file mtime, confirmed live to
  track last-append time within ~0.1s) is the only "is this active" signal.
- `generating` is `updated_secs_ago < 20`, not a ground-truth in-flight flag --
  unlike opencode's message-table `completed` timestamp, a transcript line is
  only ever written once a turn is fully formed, so there's no equivalent
  "still streaming" state to read directly.
- `status`/`stalled` treat a session whose last transcript line is an
  assistant tool call with no follow-up turn for >90s as `"waiting"`/stalled
  (a decent proxy for "sitting on an unanswered permission prompt").
- Title comes from the transcript's `ai-title` line if present, else the
  first real human message (skipping Claude Code's own injected wrapper
  text -- `isMeta: true` caveat notices, and `<command-name>`/
  `<local-command-*>` slash-command plumbing -- so a title never surfaces
  internal bookkeeping instead of what was actually typed). A session that
  is 100% slash-commands with no free-text turn falls back to `"(untitled)"`.
- Todo counts come from `TodoWrite` tool calls if any exist in the
  transcript, else read as all-zero -- unlike opencode, todo-tracking isn't
  a given for Claude Code sessions (unused in every real session checked
  while building this), so absence just means "no todo signal available."
- Expanding a Claude Code session's row uses the same `opencode-export-tail.py`
  script as opencode sessions, now dispatching on a `tool` argument the Mac app
  passes through (`fetchSessionDetail(id:tool:)`): opencode still goes through
  `opencode export`, Claude Code parses the JSONL transcript directly (there's
  no export command for it). Each `tool_use`/`tool_result` pair folds into one
  "tool" part, same shape as opencode's own tool parts -- its one-line summary
  prefers the call's own `description`/`command`/`file_path`-shaped input
  fields over the result text, so it doesn't just dump raw tool output.
- Old opencode-status.py payloads (no `tool` key at all) decode with `tool`
  defaulting to `"opencode"`, so a host that hasn't been redeployed yet
  doesn't break decoding for the other host.

## llama-swap model overview

Each host's stats now also report which models llama-swap currently has
loaded, via `read_llama_swap()` in `opencode-status.py` hitting llama-swap's
own `http://127.0.0.1:28080/running` locally on that host (not `/v1/models`,
which lists the entire ~28-model catalog regardless of whether anything's
actually running). Rendered as a "Serving: ..." line under the RAM/GPU stats
in the dropdown, and compactly in the desktop widget (only when non-empty
there, to keep that panel light). Not every Starfleet member runs llama-swap
today (Kathryn doesn't) -- a connection refused there is treated the same as
an absent GPU reading: silently omitted, not shown as an error. A model
whose `state` isn't `"ready"` (e.g. still starting) shows that state
alongside its name.

**Cluster models show under every Spark they actually occupy, not just the
one whose llama-swap answered.** Some models (the `starfleet` routing group,
plus `ds4-nvfp4-tp2`) are TP=2 vLLM+Ray clusters spanning BOTH Sparks at
once -- but llama-swap itself only runs on one host (Jean-Luc; Kathryn's
service is present but disabled). Naively that would make a cluster model
appear ONLY under Jean-Luc, even though it's genuinely also running on
Kathryn. `read_cluster_model_ids()` re-reads `~/llama-swap/config.yaml`'s
routing groups on every poll (so a model Paul adds to `starfleet` later is
picked up with no redeploy needed) and tags each `/running` entry `cluster:
true`/`false` accordingly; `effectiveLlamaSwapModels()` in `Models.swift`
then folds any `cluster: true` entry reported by one host into the other
host's display list too (deduped by model name), while a plain single-node
model is never borrowed this way. This is why Kathryn's "Serving" line can
show a model even though ITS OWN llama-swap is unreachable -- it's showing
what Jean-Luc reported, for a model that provably also runs on Kathryn.

## Tailscale re-auth handling

If a host's Tailscale SSH path is in "check" mode, the poller recognizes
the re-auth banner (instead of just showing a bare "timed out"), surfaces
a clickable "Open sign-in page" button (a `Button`+`NSWorkspace.shared.open`
wrapper, not SwiftUI's `Link` -- `Link` doesn't reliably register clicks
inside a `MenuBarExtra` popover or a non-activating `NSPanel`, both of
which this app uses), and gates auto-opening it behind a Touch ID prompt
(`LocalAuthentication`, biometrics only, no passcode fallback) so the app
doesn't just pop a browser window unprompted. Silently no-ops -- leaving
the manual button as the fallback -- if Touch ID isn't available or you
cancel/fail the prompt. Kathryn isn't on Tailscale's "check" mode path
today, so this only really applies to Jean-Luc in practice.

## Branding

**`MenuBarExtra`'s label has two hard constraints, learned the hard way:**
it renders its content through `NSStatusItem`'s template-image pipeline,
which (a) does NOT reliably rasterize raw SwiftUI `Shape`/`Path` vector
content -- it silently falls back to a generic placeholder glyph instead
of showing anything -- and (b) strips custom RGB colors from whatever DOES
render (confirmed live: a `Circle().fill(.green)` and a color-tinted SF
Symbol both came out as a flat, system-chosen tint). Only `Text`,
`Image(systemName:)`, and bitmap-backed `Image(nsImage:)` survive intact.
This has two consequences for anything shown in the menu bar label
specifically (the dropdown/widget are unaffected -- those are regular
SwiftUI-hosted windows, where `Circle`/`Path`/custom colors all render
completely normally):
- The brand mark (`StarfleetMark.swift`) is a small bundled **bitmap**
  (base64-embedded PNG, `isTemplate = true`), not a SwiftUI `Path` redraw.
- Menu bar health status uses **glyph choice** (`questionmark.circle.fill`
  / `exclamationmark.triangle.fill`), not colored dots -- color literally
  isn't available there. `StatusPoller.indicatorColor` still exists and is
  still used for the real colored dots in the dropdown/widget.

The mark is the classic Starfleet delta/arrowhead insignia, geometry
transcribed from the community vector recreation at
https://commons.wikimedia.org/wiki/File:Delta-shield.svg (its "ffb634"
foreground path -- a single closed loop: tip at top, two lower "wing"
points with a notch between). If the mark ever needs regenerating at a
different size, `assets/gen-starfleet-mark.swift` has the exact
CoreGraphics drawing code (same 4 cubic-bezier curves, transcribed by hand
from that SVG's path data) -- run it, base64-encode the output, and paste
into `StarfleetMark.swift`'s `base64` constant.

The `.app` bundle's own icon (`assets/app-icon.icns` -- visible in Finder/
Get Info/Spotlight, not the Dock, since this is an `LSUIElement` app) is
the same delta mark in gold (`#ffb634`, the source SVG's own color) on a
dark navy rounded-square badge -- an actual combadge look, not just a
loose template -- generated by `assets/gen-icon.swift` + `gen-icon.sh`
(CoreGraphics, no PIL/ImageMagick dependency). Re-run `assets/gen-icon.sh`
after editing the swift file to regenerate `app-icon.icns`; `package.sh`
just copies whatever's already there into the bundle, it doesn't
regenerate it on every build.
