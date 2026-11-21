/* Core fixtures and scenes for the phone interface: /api/state in a few conditions, the keyed check,
   the model actions, and the Bridge and Models screens. All names and numbers are made up. */
"use strict";
const KB = 1048576, now = () => Math.floor(Date.now() / 1000);
const POLICY = "Only Starfleet Command may load models and Agents may not load models on their own. This is a policy!";   /* fleet.py POLICY */
const keyOk = h => (h || {})["x-starfleet-key"] === "fixture-key";
const denied = {__status: 403, body: {error: POLICY}};

const llm = (id, nodes, o) => Object.assign({id, kind: "llm", nodes, tps: null, pinned: false, error: null, load_eta: null, ctx: null,
  ctx_capable: false, ctx_max: null, ctx_native: null, ctx_deployed: null, vision: null, effort: null, effort_options: [], ctx_pool: null}, o || {});
const chat = {ctx_capable: true, ctx_max: 262144, ctx_native: 262144, ctx_deployed: 131072, effort_options: ["Default", "low", "medium", "high"]};
const img = (id, kind, node, need, note, loaded) => ({id, kind, nodes: [node], img_nodes: ["jean-luc", "kathryn"], need_mb: need, note,
  loaded_at: loaded ? now() - loaded : null, tps: null, pinned: false, error: null, ctx: null, ctx_capable: false, vision: null, effort: null, effort_options: []});

function models(variant){
  return [
    llm("qwen38fn-sglang-tp2-starfleet", ["jean-luc", "kathryn"], Object.assign({}, chat, {tps: 31, pinned: true, ctx: 262144, ctx_pool: 253952, ctx_deployed: 262144, vision: "on", effort_options: ["Default", "low", "medium", "xhigh"]})),
    llm("bge-reranker-v2-m3", ["jean-luc"]),
    llm("gemma4-26b-55tps-jean-luc", ["jean-luc"], Object.assign({}, chat, {tps: 55, vision: "on", ctx_max: 131072, ctx_native: 131072, ctx_deployed: 65536})),
    llm("gemma4-26b-54tps-kathryn", ["kathryn"], Object.assign({}, chat, {tps: 54, vision: "on", ctx_max: 131072, ctx_native: 131072, ctx_deployed: 65536})),
    llm("deepseek-v4-flash", ["jean-luc"], {tps: 18, ctx_capable: true, ctx_max: 400000, ctx_native: 400000, ctx_deployed: 262144}),
    llm("ds4-nvfp4-tp2", ["jean-luc", "kathryn"], {tps: 18.6, ctx_capable: true, ctx: 262144, ctx_max: 262144}),
    llm("embeddinggemma", ["jean-luc"]), llm("embeddinggemma-kathryn", ["kathryn"]),
    llm("glm-5.3-21tps-tp2-starfleet", ["jean-luc", "kathryn"], Object.assign({}, chat, {tps: 21, vision: "off", ctx_max: 500000, ctx_native: 1000000, ctx_pool: 500000, ctx_deployed: 500000, effort_options: ["Default", "low", "high"], error: variant === "fault" ? "exited with code 137 while loading" : null})),
    llm("qwen3.6-35b-85tps-mtp4-jean-luc", ["jean-luc"], Object.assign({}, chat, {tps: 85, vision: "on", ctx: 131072})),
    llm("qwen3.6-35b-87tps-mtp4-kathryn", ["kathryn"], Object.assign({}, chat, {tps: 87, vision: "on"})),
    llm("qwen3.6-35b-86tps-mtp4-fastest-node", ["dynamic"], Object.assign({}, chat, {tps: 86, vision: "on"})),
    llm("qwen3-reranker-8b", ["jean-luc"]), llm("qwen3-reranker-8b-kathryn", ["kathryn"]),
    llm("nomic-embed-text", ["jean-luc"]), llm("nomic-embed-text-kathryn", ["kathryn"]),
    llm("harrier-embed-0.6b", ["kathryn"], {vision: "on"}),
    img("flux2-dev", "image", "kathryn", 24576, "Text to image", 0),
    img("qwen-image-21", "image", "kathryn", 20480, "Text in images", 3600),
    img("wan22-t2v-a14b", "video", "jean-luc", 30720, "Text to video", 0),
    img("qwen3vl-judge", "judge", "kathryn", 22528, "Checks renders", 0),
  ];
}
const node = (host, avail, util, temp, power, load, n) => ({host, mem_total_kb: Math.round(121.7 * KB), mem_avail_kb: Math.round(avail * KB), load,
  gpu_util: String(util), gpu_temp: String(temp), gpu_power: String(power), reserved_mb: 0, containers: Array.from({length: n}, (_, i) => "demo-app-" + (i + 1))});
function state(variant){
  const busy = variant === "busy", s = {
    nodes: { "jean-luc": node("jean-luc", 5.1, 94, 65, 36.64, "4.85 4.58 5.15", 40), "kathryn": node("kathryn", 19.8, 96, 59, 27.43, "3.05 3.08 3.08", 3) },
    running: [{model: "qwen38fn-sglang-tp2-starfleet", state: "ready", ttl: 0}, {model: "bge-reranker-v2-m3", state: "ready", ttl: 720}],
    pins: ["qwen38fn-sglang-tp2-starfleet"], errors: {}, queued: busy ? ["deepseek-v4-flash"] : [], llm_loading: busy ? ["gemma4-26b-54tps-kathryn"] : [],
    models: models(variant), loading: busy ? [{model: "gemma4-26b-54tps-kathryn", elapsed: 134, eta: 390, progress: .34, phase: "Loading weights"}] : [] };
  if(variant === "pinneddown") s.running = s.running.filter(r => r.model !== "qwen38fn-sglang-tp2-starfleet");
  if(variant === "finished"){ s.loading = []; s.llm_loading = []; s.queued = []; }
  if(variant === "fault"){ s.nodes.kathryn = {error: "ssh: connect to host kathryn port 22: Connection timed out"}; s.errors._swap = "llama-swap unreachable on :28080"; s.running = []; }
  return s;
}
const session = (id, title, tool, secs, o) => Object.assign({id, title, directory: "/home/demo/project", updated_secs_ago: secs, todos: {pending: 2, in_progress: 1, completed: 3},
  pid: 4000, cpu_pct: 0, status: "idle", stalled: false, generating: false, tool}, o || {});
function sessions(variant){
  const quiet = variant === "nominal" || /^upd-/.test(variant);
  return {at: now(), errors: {}, nodes: {
    "jean-luc": {host: {}, error: null, sessions: [
      session("11111111-aaaa-4bbb-8ccc-000000000001", "Refactor the budget router", "claude-code", 840, {stalled: !quiet, status: "waiting"}),
      session("11111111-aaaa-4bbb-8ccc-000000000002", "Wiki ingest: container notes", "claude-code", 4, {generating: true, status: "working", todos: {pending: 3, in_progress: 1, completed: 5}}),
      session("11111111-aaaa-4bbb-8ccc-000000000003", "Fix the flaky scrape test", "claude-code", 360, {todos: {pending: 0, in_progress: 0, completed: 2}}),
      session("ses_demo0000000000000001", "Migrate the eval harness", "opencode", 1320, {stalled: !quiet, status: "waiting", todos: {pending: 3, in_progress: 0, completed: 1}}),
      session("ses_demo0000000000000002", "Draft the release notes", "opencode", 2, {generating: true, status: "working", todos: {pending: 3, in_progress: 0, completed: 0}}),
      session("ses_demo0000000000000003", "Tidy up the launch scripts", "opencode", 10800, {todos: {pending: 0, in_progress: 0, completed: 6}}),
    ]},
    "kathryn": {host: {}, error: null, sessions: [
      session("22222222-aaaa-4bbb-8ccc-000000000001", "Benchmark the context ceilings", "claude-code", 540, {stalled: !quiet, status: "waiting", directory: null}),
      session("22222222-aaaa-4bbb-8ccc-000000000002", "Compare reranker latencies", "claude-code", 1260, {todos: {pending: 0, in_progress: 0, completed: 4}}),
    ]} }};
}
const alpha = () => ({containers: [{name: "kopia", state: "running", status: "Up 3 days"}], checked: now() - 240, status: "ok", problems: [], errors: [],
  last: {start: now() - 6 * 3600 - 120, end: now() - 6 * 3600, size: 1.85e12, files: 27862, dirs: 74464, errors: 0, incomplete: false, description: "nightly"},
  stored: {count: 184210, bytes: 1.21e12}, maintenance: {locks: "renewed", next_full: now() + 4.8 * 3600, next_quick: now() + 1800},
  policy: {times: ["03:30"], keep: {latest: 10, daily: 7, monthly: 12, annual: 3}}, next_snapshot: now() + 18 * 3600, trigger_ready: true,
  manual: {state: "idle", started: null, exit: null, error: null, snapshot: null}, at: now()});

/* ---- /api/defiant/updates (defiant_updates in fleet.py): the hourly package scan and the install job. A variant
   "upd-<state>[-<twist>]" picks the state (the twist only changes how a POST is answered); every other variant gets the
   quiet one that agrees with the Defiant probe below: 7 updates for "busy", none for "nominal". */
const SSH_DOWN = "ssh: connect to host demo-host port 22: Connection timed out";
const S_UP = ["resolute-updates"], S_SEC = ["resolute-security"];
const pkg = (name, from, to, suites, o) => Object.assign({name, from, to, suites, origin: "Ubuntu", security: suites.some(s => /-security$/.test(s)), third_party: false}, o || {});
const PKGS = [
  pkg("libc6", "2.42-0ubuntu3", "2.42-0ubuntu3.1", ["resolute-updates", "resolute-security"]), pkg("libssl3t64", "3.5.1-1ubuntu1", "3.5.1-1ubuntu1.2", S_SEC), pkg("openssl", "3.5.1-1ubuntu1", "3.5.1-1ubuntu1.2", S_SEC),
  pkg("libgstreamer-plugins-bad1.0-0", "1.28.0-1ubuntu4+esm2", "1.28.0-1ubuntu4+esm3", ["resolute-apps-security"], {origin: "UbuntuESMApps", third_party: true}),
  pkg("ca-certificates", "20250619", "20260301", S_UP), pkg("containerd.io", "2.1.4-1", "2.1.5-1", ["stable"], {origin: "Docker", third_party: true}),
  pkg("docker-ce", "5:29.1.1-1~ubuntu.26.04~resolute", "5:29.1.2-1~ubuntu.26.04~resolute", ["stable"], {origin: "Docker", third_party: true}),
  pkg("linux-image-7.0.0-1031-generic", null, "7.0.0-1031.31", S_UP), pkg("linux-modules-7.0.0-1031-generic", null, "7.0.0-1031.31", S_UP),
  pkg("linux-modules-nvidia-fs-7.0.0-1031-nvidia", null, "2.28.2+7.0.0-1031.31", S_UP), pkg("nftables", "1.1.3-1", "1.1.3-1ubuntu1", S_UP),
  pkg("python3-cryptography", "45.0.4-1", "45.0.4-1ubuntu1", S_UP), pkg("python3-urllib3", "2.5.0-1", "2.5.0-1ubuntu1", S_UP), pkg("snapd", "2.72+ubuntu26.04", "2.73+ubuntu26.04", S_UP),
  pkg("tailscale", "1.92.3", "1.92.5", ["stable"], {origin: "Tailscale", third_party: true}), pkg("tzdata", "2025b-5", "2026a-1", S_UP), pkg("ubuntu-pro-client", "37.1", "38", S_UP),
  pkg("unzip", "6.0-29", "6.0-29ubuntu1", S_UP), pkg("update-notifier-common", "3.192.99", "3.192.100", S_UP), pkg("usbutils", "1:018-1", "1:018-1ubuntu1", S_UP),
  pkg("util-linux", "2.41.1-4", "2.41.1-4ubuntu1", S_UP), pkg("vim-common", "2:9.1.1500-1", "2:9.1.1620-1", S_UP), pkg("wget", "1.25.0-2", "1.25.0-2ubuntu1", S_UP),
  pkg("xz-utils", "5.8.1-1", "5.8.1-1ubuntu1", S_UP),
];
const byKind = (a, b) => b.security - a.security || a.name.localeCompare(b.name);        /* security first, then by name: the order Fleet sends */
const idleJob = {state: "idle", started_at: null, ended_at: null, exit: null, log: [], lost: false, installed: null, reboot_required: null};
const scan = o => Object.assign({at: now() - 300, next_at: now() + 3300, busy: false, error: null, error_at: null, count: 0, security: 0, packages: [], remove: [], deferred: [],
  reboot_required: false, reboot_pkgs: []}, o);
const pending = pk => scan({count: pk.length, security: pk.filter(p => p.security).length, packages: pk.slice().sort(byKind)});
const HELD = ["distro-info", "python3-distro-info", "python3-software-properties", "software-properties-common"];
const full = () => Object.assign(pending(PKGS), {remove: ["libnsl-compat1", "python3-legacy-shim"], deferred: HELD, reboot_required: true, reboot_pkgs: ["linux-image-7.0.0-1025-generic"]});
const few = () => pending(PKGS.filter(p => ["openssl", "libssl3t64", "ca-certificates", "tzdata", "vim-common"].includes(p.name)));
const LOG = ["FLEET-APT-START 1763632000", "Get:1 http://ports.example.org/ubuntu resolute-updates InRelease [126 kB]", "Fetched 1,204 kB in 2s (601 kB/s)", "Reading package lists...",
  "Preparing to unpack .../00-libc6_2.42-0ubuntu3.1_arm64.deb ...", "Unpacking libc6:arm64 (2.42-0ubuntu3.1) over (2.42-0ubuntu3) ...", "Setting up libc6:arm64 (2.42-0ubuntu3.1) ...",
  "Preparing to unpack .../01-libssl3t64_3.5.1-1ubuntu1.2_arm64.deb ...", "Unpacking libssl3t64:arm64 (3.5.1-1ubuntu1.2) over (3.5.1-1ubuntu1) ...", "Setting up libssl3t64:arm64 (3.5.1-1ubuntu1.2) ...",
  "Preparing to unpack .../02-openssl_3.5.1-1ubuntu1.2_arm64.deb ...", "Unpacking openssl (3.5.1-1ubuntu1.2) over (3.5.1-1ubuntu1) ...", "Setting up openssl (3.5.1-1ubuntu1.2) ...",
  "Setting up docker-ce (5:29.1.2-1~ubuntu.26.04~resolute) ...", "Setting up tailscale (1.92.5) ...", "Processing triggers for man-db (2.13.1-1) ..."];
const FAIL_LOG = ["FLEET-APT-START 1763631000", "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 2211 (unattended-upgr)",
  "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?", "FLEET-APT-EXIT 100 1763631660"];
const body = (s, job) => ({configured: true, now: now() + .5, scan: s, critical: !!s && (s.security > 0 || s.reboot_required), job: Object.assign({}, idleJob, job)});
const running = o => ({state: "running", started_at: now() - 260, log: LOG, ...o});
const HOSTILE = '<img src=x onerror="window.__pwned=1">';
const STATES = {
  never: () => body(null),
  down: () => ({__status: 502, body: {error: "upstream down"}}),                                   /* only this source fails */
  firstfail: () => body(scan({at: null, error: SSH_DOWN, error_at: now() - 90})),                  /* a scan object without a good scan (Fleet sends scan: null then; the page copes with both) */
  scanning: () => body({...full(), busy: true}),
  error: () => body({...full(), error: "E: Failed to fetch http://ports.example.org/ubuntu-ports/dists/resolute-updates/InRelease Temporary failure resolving ports.example.org", error_at: now() - 240}),
  clean: () => body(scan({deferred: HELD})),
  few: () => body(few()),
  pending: () => body(full()),
  running: () => body(full(), running()),
  lost: () => body(full(), running({lost: true})),
  done: () => body(scan({deferred: HELD, reboot_required: true, reboot_pkgs: ["linux-image-7.0.0-1031-generic"]}),
    {state: "done", started_at: now() - 900, ended_at: now() - 420, exit: 0, installed: 24, reboot_required: true, log: LOG}),
  failed: () => body(full(), {state: "failed", started_at: now() - 1200, ended_at: now() - 1080, exit: 100, log: FAIL_LOG}),
  /* every string a remote host sends, with markup in it */
  hostile: () => body(scan({count: 2, security: 1, error: '<b id="evil">boom</b>', error_at: now() - 60, remove: [HOSTILE], reboot_required: true, reboot_pkgs: ["<b>k</b>"],
    packages: [pkg("x" + HOSTILE, "1<b>2", "3<i>4", ["s<u>1-security"]), pkg("y&amp;z", null, "5<s>6", ["<em>stable</em>"], {origin: "<script>window.__pwned=1</script>", third_party: true})]}),
    {state: "failed", started_at: now() - 100, ended_at: now() - 50, exit: 1, log: ["<script>window.__pwned=1</script>", HOSTILE]}),
  quiet: v => body(v === "nominal" ? scan({}) : {...pending(PKGS.filter(p => !p.security).slice(0, 7)), reboot_required: true, reboot_pkgs: ["linux-image-7.0.0-1025-generic"]}),
};
const begun = new Map(), hits = {upd: 0};        /* variant -> when its install was accepted; GETs of /api/defiant/updates answered */
function updates(variant){
  const m = /^upd-([a-z]+)/.exec(variant), t = begun.get(variant);
  if(t) return body(full(), running({started_at: t, log: LOG.slice(0, 2)}));                 /* Fleet says "running" from the moment it accepts the install */
  return (m && STATES[m[1]] || STATES.quiet)(variant);
}
const updSummary = u => u.scan && u.scan.at ? {count: u.scan.count, security: u.scan.security, deferred: u.scan.deferred.length, remove: u.scan.remove.length, scan_at: u.scan.at, job_state: u.job.state} : null;
const defiant = variant => { const u = updates(variant), upd = updSummary(u), sec = upd ? upd.security : 0;     /* the probe, and the security count Fleet adds to its problems; the probe's count (apt list) also holds the 4 packages phasing keeps back, so it differs from the scan's: a screen that reads the wrong one shows */
  return {configured: true, host: "defiant", os: "Debian 13", kernel: "6.12", uptime_s: 41 * 86400, load: "0.12 0.08 0.05", cpus: 4, cpu_pct: 3,
    mem_total_kb: 8 * KB, mem_avail_kb: 5.6 * KB, disk_total: 80e9, disk_used: 21e9, disk_avail: 59e9, rx_bps: 18000, tx_bps: 64000,
    reboot_required: variant === "nominal" || /^upd-/.test(variant) ? !!(u.scan && u.scan.reboot_required) : true, updates: upd ? upd.count + 4 : 7, containers: [{name: "caddy", state: "running", status: "Up 41 days"}],
    sites: [{domain: "example.org", status: 200, ms: 84, cert_days: 61, error: null, deployed: now() - 86400 * 3}, {domain: "docs.example.org", status: 200, ms: 91, cert_days: 61, error: null, deployed: now() - 86400 * 9}],
    problems: sec ? [`${sec} security update${sec === 1 ? "" : "s"} pending`] : [], errors: [], status: sec ? "warn" : "ok", upd, at: now()}; };

exports.fixtures = {state, sessions, alpha, defiant, updates, begun, hits, models, keyOk, denied, POLICY, now};
exports.get = (p, variant, q, h) => {
  if(variant === "offline") return {__status: 502, body: {error: "upstream down"}};
  if(p === "/api/state") return state(variant);
  if(p === "/api/events" && !(q.get("ev") || /[?&]ev=/.test((h || {}).referer || ""))) return {events: [], latest: 0, open: [], at: now()};   /* the default: no messages. scenes-messages answers a page opened as /padd?ev=<set> */
  if(p === "/api/memoryalpha") return alpha();
  if(p === "/api/defiant") return defiant(variant);
  if(p === "/api/cluster/update") return {state: {phase: "idle"}, pending: {}, pending_at: null};
  if(p === "/api/defiant/updates"){ hits.upd++; return updates(variant); }
  if(p === "/api/computer/sessions") return keyOk(h) ? sessions(variant) : denied;
  if(p === "/api/computer/models") return keyOk(h) ? {models: [{id: "qwen38fn-sglang-tp2-starfleet", vision: true}], servers: {}} : denied;   /* the key check */
};
exports.post = (p, body, variant, h) => {
  const keyed = ["/api/pin", "/api/unload", "/api/cancel", "/api/ctx", "/api/effort", "/api/imgnode"];
  if(keyed.includes(p) && !keyOk(h) && !(p === "/api/pin" && body && body.pin === false)) return denied;
  if(p === "/api/pin" || p === "/api/cancel") return {ok: true};
  if(p === "/api/unload") return {ok: true, freed_mb: 21480};
  if(p === "/api/ctx" || p === "/api/effort") return {ok: true, reloaded: true};
  if(p === "/api/imgnode") return {ok: true, node: body && body.node, queued: 1};
  if(p === "/api/imgunload") return {ok: true, node: "kathryn", message: "freed"};
};

const row = name => `[data-act="model"][data-arg="${name}"]`;
exports.scenes = [
  {name: "bridge", hash: "#/bridge", variant: "busy", groups: true},
  {name: "bridge-nominal", hash: "#/bridge", variant: "nominal"},
  {name: "bridge-fault", hash: "#/bridge", variant: "fault"},
  {name: "bridge-offline", hash: "#/bridge", variant: "offline"},
  {name: "bridge-old-shell", hash: "#/bridge", variant: "busy", shell: "old"},
  {name: "bridge-small", hash: "#/bridge", variant: "busy", shell: "small"},
  {name: "bridge-status-sheet", hash: "#/bridge", variant: "busy", run: (page, t) => t.tap("#p-stat")},
  {name: "bridge-cancel-load", hash: "#/bridge", variant: "busy", run: async (page, t) => { await t.tap('[data-act="cancel"]'); await t.shot("asked"); await t.tap('[data-k="no"]'); }},
  {name: "bridge-cancel-load-yes", hash: "#/bridge", variant: "busy", posts: [["/api/cancel", {model: "gemma4-26b-54tps-kathryn"}]],
    run: async (page, t) => { await t.tap('[data-act="cancel"]'); await t.tap('[data-k="yes"]'); }},
  {name: "models", hash: "#/models", variant: "busy"},
  {name: "models-small", hash: "#/models", variant: "busy", shell: "small"},
  {name: "models-search", hash: "#/models", variant: "busy", run: (page, t) => t.type("#p-q", "qwen")},
  {name: "models-image", hash: "#/models", variant: "busy", run: (page, t) => t.tap('[data-act="mfilter"][data-arg="image"]')},
  {name: "model-sheet-stopped", hash: "#/models", variant: "busy", run: (page, t) => t.tap(row("qwen3.6-35b-mtp4"))},
  {name: "model-sheet-resident", hash: "#/models", variant: "busy", run: (page, t) => t.tap(row("qwen38fn-sglang-tp2-starfleet"))},
  {name: "model-sheet-loading", hash: "#/models", variant: "busy", run: (page, t) => t.tap(row("gemma4-26b"))},
  {name: "model-sheet-image", hash: "#/models", variant: "busy", run: async (page, t) => { await t.tap('[data-act="mfilter"][data-arg="image"]'); await t.tap(row("qwen-image-21")); }},
  {name: "model-load", hash: "#/models", variant: "busy", posts: [["/api/pin", {model: "qwen3.6-35b-85tps-mtp4-jean-luc", pin: true}]],
    run: async (page, t) => { await t.tap(row("qwen3.6-35b-mtp4")); await t.tap('[data-act="pin"]'); }},
  {name: "model-machine-then-load", hash: "#/models", variant: "busy", posts: [["/api/pin", {model: "qwen3.6-35b-87tps-mtp4-kathryn", pin: true}]],
    run: async (page, t) => { await t.tap(row("qwen3.6-35b-mtp4")); await t.tap('[data-act="machine"][data-arg="kathryn"]'); await t.tap('[data-act="pin"]'); }},
  {name: "model-unload", hash: "#/models", variant: "busy", posts: [["/api/unload", {model: "bge-reranker-v2-m3"}]],
    run: async (page, t) => { await t.tap(row("bge-reranker-v2-m3")); await t.tap('[data-act="unload"]'); }},
  {name: "model-ctx-resident", hash: "#/models", variant: "busy", posts: [["/api/ctx", {model: "qwen38fn-sglang-tp2-starfleet", ctx: 131072}]],
    run: async (page, t) => { await t.tap(row("qwen38fn-sglang-tp2-starfleet")); await t.tap('[data-act="ctx"][data-arg="131072"]'); await t.shot("asked"); await t.tap('[data-k="yes"]'); }},
  {name: "model-effort-stopped", hash: "#/models", variant: "busy", posts: [["/api/effort", {model: "qwen3.6-35b-85tps-mtp4-jean-luc", effort: "high"}]],
    run: async (page, t) => { await t.tap(row("qwen3.6-35b-mtp4")); await t.tap('[data-act="effort"][data-arg="high"]'); }},
  {name: "model-sheet-pinned-down", hash: "#/models", variant: "pinneddown", posts: [["/api/pin", {model: "qwen38fn-sglang-tp2-starfleet", pin: true}]],
    run: async (page, t) => { await t.tap(row("qwen38fn-sglang-tp2-starfleet")); await t.shot("sheet");
      const labels = await page.$$eval(".p-sheet .p-acts .p-btn", b => b.map(x => x.textContent.trim()));
      if(labels.join() !== "Unpin,Load") throw new Error("pinned but stopped model offers " + labels.join()); await t.tap('[data-act="pin"][data-arg="1"]'); }},
  {name: "model-unload-pinned-asks", hash: "#/models", variant: "busy",
    run: async (page, t) => { await t.tap(row("qwen38fn-sglang-tp2-starfleet")); await t.tap('[data-act="unload"]'); await t.shot("asked");
      if(t.posts.length) throw new Error("posted before the confirmation"); await t.tap('[data-k="no"]'); }},
  {name: "model-unload-pinned-yes", hash: "#/models", variant: "busy", posts: [["/api/unload", {model: "qwen38fn-sglang-tp2-starfleet"}]],
    run: async (page, t) => { await t.tap(row("qwen38fn-sglang-tp2-starfleet")); await t.tap('[data-act="unload"]'); await page.waitForTimeout(500); await t.tap('[data-k="yes"]'); }},
  {name: "confirm-ignores-double-tap", hash: "#/models", variant: "busy",
    run: async (page, t) => { await t.tap(row("qwen38fn-sglang-tp2-starfleet")); await t.tap('[data-act="unload"]');
      await page.evaluate(() => { const y = document.querySelector('[data-k="yes"]'); y.click(); }); await page.waitForTimeout(150);
      if(!(await page.$('[data-k="yes"]'))) throw new Error("the confirm sheet took a tap in its first moments"); await t.tap('[data-k="no"]'); }},
  {name: "bridge-cancel-after-finish", hash: "#/bridge", variant: "busy",
    run: async (page, t) => { await t.tap('[data-act="cancel"]');
      await page.evaluate(() => { const s = JSON.parse(JSON.stringify(PADD.data.state)); s.loading = []; s.queued = []; PADD.data.state = s; PADD.at.state = Date.now() + 60000; });
      await page.evaluate(() => { PADD.sources.state.url = "/api/state?finished"; });      /* keep the fake poll from putting the load back */
      await t.tap('[data-k="yes"]'); await t.shot("said"); }},
  {name: "status-row-same-screen-closes-sheet", hash: "#/bridge", variant: "fault",
    run: async (page, t) => { await t.tap("#p-stat"); await t.tap('.p-sheet [data-act="go"][data-arg="#/bridge"]'); await page.waitForTimeout(400);
      if(await page.$(".p-sheet.on")) throw new Error("the status sheet stayed open"); }},
  {name: "old-bookmark-ma", hash: "#ma", variant: "nominal",
    run: async (page) => { const title = await page.$eval("#p-title", e => e.textContent); if(title !== "Memory Alpha") throw new Error("#ma opened " + title); }},
  {name: "old-bookmark-filter", hash: "#kathryn", variant: "busy",
    run: async (page) => { const on = await page.$eval('.p-chip.on', e => e.textContent); if(!/kathryn/i.test(on)) throw new Error("#kathryn did not select its filter: " + on); }},
  {name: "models-chip-counts", hash: "#/models", variant: "busy",
    run: async (page, t) => { const n = f => page.$eval(`[data-act="mfilter"][data-arg="${f}"] b`, e => +e.textContent);
      const k = await n("kathryn"); await t.tap('[data-act="mfilter"][data-arg="kathryn"]');
      const rows = await page.$$eval("#p-ml .p-row", r => r.length); if(k !== rows) throw new Error(`Kathryn chip says ${k}, the list has ${rows}`);
      if(!k) throw new Error("no Kathryn models counted"); }},
  {name: "key-sheet-on-load", hash: "#/models", variant: "busy", key: false,
    posts: [["/api/pin", {model: "qwen3.6-35b-85tps-mtp4-jean-luc", pin: true}], ["/api/pin", {model: "qwen3.6-35b-85tps-mtp4-jean-luc", pin: true}]],
    run: async (page, t) => { await t.tap(row("qwen3.6-35b-mtp4")); await t.tap('[data-act="pin"]'); await t.shot("asked");
      await t.type("#p-keyin", "wrong"); await t.tap("#p-keygo"); await t.shot("refused"); await t.type("#p-keyin", "fixture-key"); await t.tap("#p-keygo"); await t.wait(500); }},
];
