/* Fixtures and scenes for the Systems screens (vendor/padd-systems.js): Sessions, Main Engineering,
   Deep Space NAS, Memory Alpha, Defiant. All names and numbers are made up. This file answers only
   /api/containers, /api/nas, /api/nas/stats, the session tail and the three POSTs; sessions, Memory Alpha
   and Defiant come from scenes-core.js, and a scene that needs another answer from one of those swaps it
   in with over(). */
"use strict";
const F = require("./scenes-core.js").fixtures, { keyOk, denied, now } = F;
const KB = 1048576, SSH = "ssh: connect to host demo-host port 22: Connection timed out";

/* ---- /api/containers (containers_state in fleet.py) */
const ctr = (node, name, kind, ram, o) => Object.assign({node, name, image: `registry.example.org/${name.replace(/-\d+$/, "")}:1.4`, started: now() - 3 * 3600, project: null,
  ram_mb: ram, gpu_mb: null, owner: "detached", kind, session_idle_s: null, cpu_idle_s: 20, cpu_idle_ge: false, stoppable: kind !== "gpu"}, o || {});
function containers(variant){
  if(variant === "empty") return {containers: [], errors: {}, at: now()};
  const rows = [
    ctr("jean-luc", "demo-model-head", "gpu", 3210, {gpu_mb: 58800, owner: "llama-swap › serve-demo.sh", started: now() - 5 * 86400, cpu_idle_s: 3}),
    ctr("kathryn", "demo-model-worker", "gpu", 2890, {gpu_mb: 57600, owner: "llama-swap › serve-demo.sh (worker)", started: now() - 5 * 86400, cpu_idle_s: 3}),
    ctr("jean-luc", "demo-reranker", "gpu", 1440, {gpu_mb: 9200, owner: "llama-swap › serve-embed.sh", started: now() - 9 * 3600, cpu_idle_s: 2400}),
    ctr("jean-luc", "demo-app-1", "service", 2300, {project: "demo", owner: "compose · demo", started: now() - 12 * 86400, cpu_idle_s: 12}),
    ctr("jean-luc", "mcp-scrape-1", "session", 1890, {owner: "Claude Code", session_idle_s: 3 * 86400 + 7200, started: now() - 4 * 86400}),
    ctr("jean-luc", "demo-db-1", "service", 780, {project: "demo", owner: "compose · demo", started: now() - 12 * 86400, cpu_idle_s: 4000, cpu_idle_ge: true}),
    ctr("jean-luc", "mcp-docs-2", "session", 655, {owner: "opencode", session_idle_s: 5400, started: now() - 7200}),
    ctr("jean-luc", "mcp-search-1", "session", 412, {owner: "Claude Code › run-search.sh", session_idle_s: 140, started: now() - 1500}),
    ctr("kathryn", "mcp-search-2", "session", 388, {owner: "Claude Code (on jean-luc)", cpu_idle_s: 2 * 86400, cpu_idle_ge: true, started: now() - 6 * 86400}),
    ctr("jean-luc", "demo-notes", "service", 240, {owner: "systemd · demo-notes.service", cpu_idle_s: 700, started: now() - 30 * 86400}),
    ctr("kathryn", "demo-app-2", "service", 96, {owner: "docker · restart unless-stopped", cpu_idle_s: null, started: now() - 40}),
  ];
  rows.sort((a, b) => (b.ram_mb + (b.gpu_mb || 0)) - (a.ram_mb + (a.gpu_mb || 0)));
  return {containers: rows, errors: variant === "stale" ? {kathryn: SSH} : {}, at: now()};
}

/* ---- /api/nas and /api/nas/stats (nas_state, nas_stats: rows as Container Manager lists them) */
const nasC = (Names, State, Status, img) => ({Names, State, Status, Image: `registry.example.org/${img}`});
function nas(variant){
  const list = [
    nasC("media-server-1", "running", "Up 3 days (healthy)", "media-server:2.1"), nasC("media-db-1", "running", "Up 3 days (healthy)", "postgres:17"),
    nasC("media-cache-1", "running", "Up 3 days", "valkey:8"), nasC("media-migrate-1", "exited", "Exited (0) 3 days ago", "media-server:2.1"),
    nasC("git-server", "running", "Up 9 days", "git-server:1.24"), nasC("git-db", "running", "Up 9 days (unhealthy)", "postgres:17"),
    nasC("kopia", "running", "Up 3 days", "kopia:0.21"), nasC("kopia-check", "running", "Up 3 days", "kopia:0.21"),
    nasC("kopia-kopia-init-1", "exited", "Exited (0) 3 days ago", "kopia:0.21"), nasC("kopia-snapshot", "exited", "Exited (1) 6 hours ago", "kopia:0.21"),
    nasC("notes-app-1", "running", "Up 2 minutes (health: starting)", "notes:3"), nasC("bridge-demo-1", "exited", "Exited (137) 2 hours ago", "bridge:0.3"),
    nasC("dns-filter", "restarting", "Restarting (1) 4 seconds ago", "dns-filter:5"),
  ];
  return {containers: variant === "empty" ? [] : list, error: variant === "stale" ? SSH : null, at: now()};
}
function nasStats(variant){
  if(variant === "stale") return {stats: null, error: "unknown verb: stats", at: now()};
  const hot = variant === "hot";
  return {error: null, at: now(), stats: {cpu_pct: hot ? 93.2 : 12.4, cores: 4, load: hot ? [6.1, 4.8, 3.2] : [0.42, 0.38, 0.35], mem_used_mb: hot ? 13650 : 6520, mem_total_mb: 15890, swap_used_mb: 120,
    temps: [{label: "CPU", c: hot ? 68 : 41}, {label: "Disk 1", c: 36}, {label: "Disk 2", c: 37}], net_rx_bps: hot ? 48e6 : 1240000, net_tx_bps: hot ? 2.1e6 : 310000,
    nics: {eth0: [1240000, 310000]}, uptime_s: hot ? 5 * 3600 + 720 : 12 * 86400 + 3 * 3600, disk: {free_gb: 5210, total_gb: 10680}}};
}

/* ---- /api/computer/sessions/tail: seconds from Claude Code, milliseconds from opencode */
const LONG = "The budget check now lives in its own module.\n\nWhat changed:\n1. The router asks the budget module before every provider call.\n2. The monthly rollover is a pure function of the date, so the test can pin it.\n3. A provider that runs out mid-request fails over once and reports why.\n4. The usage file is written through a temp file and a rename.\n5. Old counters are kept for one month for the report.\n\nThe rollover test fails on the last day of a month with 31 days. I am not sure whether the counter should reset at midnight local time or UTC, and the two existing tests disagree with each other.";
let tailHits = 0;
function tail(q){
  tailHits++;
  const id = q.get("id"), ms = q.get("tool") === "opencode", t = s => ms ? (now() - s) * 1000 : now() - s;
  if(!q.get("node") || !id || !q.get("tool")) return {__status: 400, body: {error: "node, id and tool are required"}};
  if(/3$/.test(id)) return {error: "No transcript found for this session."};
  return {session_id: id, title: "Demo session", messages: [
    {role: "user", created: t(2400), completed: t(2400), finish: null, parts: [{type: "text", text: "Split the provider budget check out of the router and add a test for the monthly rollover."}, {type: "file", filename: "router.ts"}]},
    {role: "assistant", created: t(2380), completed: t(2100), finish: "tool-calls", parts: [{type: "text", text: "I will read the router first, then move the check."},
      {type: "tool", tool: "Read", status: "completed", title: "src/router.ts"}, {type: "tool", tool: "Edit", status: "completed", title: "src/budget.ts"}, {type: "tool", tool: "Bash", status: "error", title: "npm test -- budget"}]},
    {role: "assistant", created: t(2090), completed: /2$/.test(id) ? null : t(840), finish: /2$/.test(id) ? null : "stop", parts: [{type: "text", text: LONG}, {type: "tool", tool: "Bash", status: /2$/.test(id) ? "running" : "completed", title: "npm test"}]},
    {role: "user", created: t(845), completed: t(845), finish: null, parts: [{type: "text", text: "UTC. Fix the tests to match."}]},
    {role: "assistant", created: t(840), completed: /2$/.test(id) ? null : t(838), finish: null, parts: [{type: "text", text: "Should I also change the report, which prints local dates?"}]},
  ]};
}

exports.get = (p, variant, q, h) => {
  if(variant === "offline") return;                            /* scenes-core answers everything with 502 */
  if(p === "/api/containers") return containers(variant);
  if(p === "/api/nas") return nas(variant);
  if(p === "/api/nas/stats") return nasStats(variant);
  if(p === "/api/computer/sessions/tail") return keyOk(h) ? tail(q) : denied;
};
exports.post = (p, body, variant, h) => {
  body = body || {};
  if(p === "/api/containers/stop"){
    if(!keyOk(h)) return denied;
    const row = containers(variant).containers.find(c => c.node === body.node && c.name === body.name);
    if(!row) return {__status: 400, body: {error: `${body.name} is not running on ${body.node}`}};
    if(!row.stoppable) return {__status: 409, body: {error: "Model server: unload it in the Model Registry, not here."}};
    if(variant === "refuse") return {__status: 502, body: {error: "docker stop exit 1"}};
    return {ok: true, node: body.node, name: body.name, freed_mb: row.ram_mb};
  }
  if(p === "/api/nas/control"){                                 /* takes no key, so none may be sent */
    if(h["x-starfleet-key"]) return {__status: 400, body: {error: "fixture: the Starfleet key was sent to a route that takes none"}};
    if(!["start", "stop", "restart", "rm"].includes(body.action)) return {__status: 400, body: {error: "action must be start, stop, restart or rm"}};
    if(!nas(variant).containers.some(c => c.Names === body.target)) return {__status: 400, body: {error: "unknown container"}};
    if(variant === "gatefail") return {__status: 502, body: {error: "gate: container did not stop within 300 s"}};
    return {ok: true};
  }
  if(p === "/api/memoryalpha/backup") return !keyOk(h) ? denied : variant === "refuse" ? {__status: 409, body: {error: "A snapshot is already running."}} : {ok: true};
};

/* ---- scenes */
/* another answer for a path scenes-core.js serves, then a fresh poll of that source */
const over = (path, src, body) => async page => {
  await page.route(u => u.pathname === path, r => r.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(body())}));
  await page.evaluate(k => PADD.refresh([k]), src);
  await page.waitForTimeout(400);
};
const down = (path, src) => async page => {                       /* the source stops answering after its first answer */
  await page.route(u => u.pathname === path, r => r.fulfill({status: 502, contentType: "application/json", body: '{"error":"upstream down"}'}));
  await page.evaluate(k => PADD.refresh([k]), src);
  await page.waitForTimeout(400);
};
const must = async (page, fn, what) => { if(!await page.evaluate(fn)) throw new Error("expected " + what); };
/* sessions_state in fleet.py: Kathryn did not answer (last list kept, error in both places); Jean-Luc answered with a problem of its own */
const sessErr = over("/api/computer/sessions", "sessions", () => { const s = F.sessions("busy");
  s.nodes.kathryn.error = SSH; s.errors = {kathryn: SSH};
  s.nodes["jean-luc"].error = "opencode database not found"; s.nodes["jean-luc"].sessions = s.nodes["jean-luc"].sessions.filter(x => x.tool === "claude-code"); return s; });
const sessNone = over("/api/computer/sessions", "sessions", () => ({at: now(), errors: {}, nodes: {"jean-luc": {host: {}, sessions: [], error: null}, "kathryn": {host: {}, sessions: [], error: null}}}));
const alphaWarn = over("/api/memoryalpha", "alpha", () => { const a = F.alpha(); return Object.assign(a, {status: "warn", problems: ["last snapshot 41 h ago", "object lock renewal is off"],
  last: Object.assign(a.last, {start: now() - 41 * 3600 - 150, end: now() - 41 * 3600, errors: 2}), maintenance: {locks: false, next_full: now() - 3600, next_quick: now() + 600},
  manual: {state: "running", started: now() - 95, exit: null, error: null, snapshot: null}}); });
const alphaFailed = over("/api/memoryalpha", "alpha", () => Object.assign(F.alpha(), {manual: {state: "exited", started: now() - 7200, exit: 1, error: "snapshot failed: repository not reachable", snapshot: null}}));
const alphaDown = over("/api/memoryalpha", "alpha", () => ({status: "error", problems: [], errors: [SSH], at: now()}));
const defDown = over("/api/defiant", "defiant", () => ({configured: true, containers: [], sites: [], problems: [], errors: [SSH], status: "error", at: now()}));   /* defiant_state when the probe fails */
const defOff = over("/api/defiant", "defiant", () => ({configured: false, status: "off", at: now()}));
const defBad = over("/api/defiant", "defiant", () => Object.assign(F.defiant("nominal"), {status: "warn", cpu_pct: 86.5, mem_avail_kb: 0.5 * KB, disk_used: 74e9, disk_avail: 6e9,
  problems: ["shop.example.org: 502", "old.example.org: [Errno 111] Connection refused", "docs.example.org: certificate expires in 9 days", "disk over 85 % full", "container demo-worker is exited"],
  sites: [{domain: "example.org", status: 200, ms: 84, cert_days: 61, error: null, deployed: now() - 86400 * 3}, {domain: "docs.example.org", status: 200, ms: 91, cert_days: 9, error: null, deployed: now() - 86400 * 9},
    {domain: "shop.example.org", status: 502, ms: 1210, cert_days: 61, error: null, deployed: null}, {domain: "intern.example.org", status: 401, ms: 60, cert_days: 61, error: null, deployed: now() - 3600},
    {domain: "old.example.org", status: null, ms: null, cert_days: null, error: "[Errno 111] Connection refused", deployed: null}],
  containers: [{name: "caddy", state: "running", status: "Up 41 days"}, {name: "demo-worker", state: "exited", status: "Exited (1) 2 hours ago"}]}));

const ses = (node, tool, id) => `[data-act="sys.ses"][data-arg="${node}|${tool}|${id}"]`, S1 = ses("jean-luc", "claude-code", "11111111-aaaa-4bbb-8ccc-000000000001");
const cRow = k => `[data-act="sys.ctr"][data-arg="${k}"]`, nRow = n => `[data-act="sys.nas"][data-arg="${n}"]`, nAct = a => `[data-act="sys.nact"][data-arg="${a}"]`;
const YES = '[data-k="yes"]', NO = '[data-k="no"]', STOP = '[data-act="sys.cstop"]', BACKUP = '[data-act="sys.backup"]';
const enterKey = async t => { await t.type("#p-keyin", "fixture-key"); await t.tap("#p-keygo"); await t.wait(600); };
const ENG = "#/systems/engineering", NAS = "#/systems/nas", SES = "#/systems/sessions", ALPHA = "#/systems/alpha", DEF = "#/systems/defiant";
const stopPost = ["/api/containers/stop", {node: "jean-luc", name: "mcp-scrape-1"}], nasPost = (target, action) => ["/api/nas/control", {target, action}];

exports.scenes = [
  /* Systems */
  {name: "sys-root", hash: "#/systems", variant: "busy"},
  {name: "sys-root-nominal", hash: "#/systems", variant: "nominal"},
  {name: "sys-root-checking", hash: "#/bridge", variant: "busy", run: async (page, t) => {       /* three sources that have not answered yet */
    await page.route(u => ["/api/containers", "/api/nas", "/api/nas/stats"].includes(u.pathname), () => {}); await t.tap('[data-act="tab"][data-arg="systems"]');
    await must(page, () => (document.querySelector("#p-view").textContent.match(/Checking/g) || []).length === 2 && document.querySelectorAll('#p-view [data-act="go"]').length === 6, "six rows, two of them still checking"); }},
  {name: "sys-root-offline", hash: "#/systems", variant: "offline"},
  {name: "sys-root-nokey", hash: "#/systems", variant: "busy", key: false},
  {name: "sys-root-old", hash: "#/systems", variant: "busy", shell: "old"},
  {name: "sys-root-small", hash: "#/systems", variant: "busy", shell: "small"},
  {name: "sys-root-open", hash: "#/systems", variant: "busy", run: (page, t) => t.tap('[data-act="go"][data-arg="#/systems/nas"]')},

  /* Sessions */
  {name: "sys-sessions", hash: SES, variant: "busy"},
  {name: "sys-sessions-small", hash: SES, variant: "busy", shell: "small"},
  {name: "sys-sessions-stalled", hash: SES, variant: "busy", run: (page, t) => t.tap('[data-act="sys.sf"][data-arg="stalled"]')},
  {name: "sys-sessions-node", hash: SES, variant: "busy", run: (page, t) => t.tap('[data-act="sys.sf"][data-arg="kathryn"]')},
  {name: "sys-sessions-nokey", hash: SES, variant: "busy", key: false},
  {name: "sys-sessions-errors", hash: SES, variant: "busy", run: sessErr},
  {name: "sys-sessions-empty", hash: SES, variant: "busy", run: sessNone},
  {name: "sys-sessions-offline", hash: SES, variant: "offline"},
  {name: "sys-session-sheet", hash: SES, variant: "busy", run: async (page, t) => { await t.tap(S1); await t.wait(300); await page.evaluate(() => { document.querySelector(".p-shb").scrollTop = 0; }); }},
  {name: "sys-session-sheet-end", hash: SES, variant: "busy", run: async (page, t) => { await t.tap(S1); await t.wait(300); }},
  {name: "sys-session-sheet-more", hash: SES, variant: "busy", run: async (page, t) => { await t.tap(S1); await t.wait(300); await t.tap('[data-act="sys.more"]'); }},
  {name: "sys-session-sheet-opencode", hash: SES, variant: "busy", run: async (page, t) => { await t.tap(ses("jean-luc", "opencode", "ses_demo0000000000000002")); await t.wait(300); }},
  {name: "sys-session-sheet-nodir", hash: SES, variant: "busy", shell: "old", run: async (page, t) => { await t.tap(ses("kathryn", "claude-code", "22222222-aaaa-4bbb-8ccc-000000000001")); await t.wait(300); }},
  /* the tail is read again every 10 s while the session is generating, and only then */
  {name: "sys-session-tail-refresh", hash: SES, variant: "busy", run: async (page, t) => { tailHits = 0; await t.tap(ses("jean-luc", "opencode", "ses_demo0000000000000002")); await t.wait(10800);
    if(tailHits < 2) throw new Error("expected a second read of the tail after 10 s, saw " + tailHits); }},
  {name: "sys-session-tail-idle", hash: SES, variant: "busy", run: async (page, t) => { tailHits = 0; await t.tap(S1); await t.wait(10800);
    if(tailHits !== 1) throw new Error("expected one read of the tail for a session that is not generating, saw " + tailHits); }},
  {name: "sys-session-sheet-error", hash: SES, variant: "busy", run: async (page, t) => { await t.tap(ses("jean-luc", "claude-code", "11111111-aaaa-4bbb-8ccc-000000000003")); await t.wait(300); }},

  /* Main Engineering */
  {name: "sys-eng", hash: ENG, variant: "busy"},
  {name: "sys-eng-small", hash: ENG, variant: "busy", shell: "small"},
  {name: "sys-eng-kathryn", hash: ENG + "/kathryn", variant: "busy", run: page => must(page, () => document.querySelector('[data-act="sys.cnode"][data-arg="kathryn"]').classList.contains("on")
    && document.querySelectorAll('[data-act="sys.ctr"]').length === 3, "the Kathryn filter lit and three rows")},
  {name: "sys-eng-kind", hash: ENG, variant: "busy", run: (page, t) => t.tap('[data-act="sys.ckind"][data-arg="session"]')},
  {name: "sys-eng-empty", hash: ENG, variant: "empty"},
  {name: "sys-eng-stale", hash: ENG, variant: "stale"},
  {name: "sys-eng-offline", hash: ENG, variant: "offline"},
  {name: "sys-eng-sheet", hash: ENG, variant: "busy", run: (page, t) => t.tap(cRow("jean-luc/mcp-scrape-1"))},
  {name: "sys-eng-sheet-small", hash: ENG, variant: "busy", shell: "small", run: (page, t) => t.tap(cRow("jean-luc/demo-db-1"))},
  {name: "sys-eng-sheet-model", hash: ENG, variant: "busy", run: (page, t) => t.tap(cRow("jean-luc/demo-model-head"))},
  {name: "sys-eng-stop", hash: ENG, variant: "busy", posts: [stopPost],
    run: async (page, t) => { await t.tap(cRow("jean-luc/mcp-scrape-1")); await t.tap(STOP); await t.shot("asked"); await t.tap(YES); await t.wait(300); }},
  {name: "sys-eng-stop-failed", hash: ENG, variant: "refuse", posts: [stopPost],
    run: async (page, t) => { await t.tap(cRow("jean-luc/mcp-scrape-1")); await t.tap(STOP); await t.tap(YES); await t.wait(300); }},
  {name: "sys-eng-stop-cancel", hash: ENG, variant: "busy", run: async (page, t) => { await t.tap(cRow("jean-luc/mcp-scrape-1")); await t.tap(STOP); await t.tap(NO); }},
  {name: "sys-eng-stop-nokey", hash: ENG, variant: "busy", key: false, posts: [stopPost, stopPost],
    run: async (page, t) => { await t.tap(cRow("jean-luc/mcp-scrape-1")); await t.tap(STOP); await t.tap(YES); await t.shot("asked"); await enterKey(t); }},

  /* Deep Space NAS */
  {name: "sys-nas", hash: NAS, variant: "busy"},
  {name: "sys-nas-old", hash: NAS, variant: "busy", shell: "old"},
  {name: "sys-nas-small", hash: NAS, variant: "hot", shell: "small"},
  {name: "sys-nas-hot", hash: NAS, variant: "hot"},
  {name: "sys-nas-stale", hash: NAS, variant: "stale"},
  {name: "sys-nas-empty", hash: NAS, variant: "empty"},
  {name: "sys-nas-offline", hash: NAS, variant: "offline"},
  {name: "sys-nas-lost", hash: NAS, variant: "busy", run: down("/api/nas", "nas")},
  {name: "sys-nas-sheet", hash: NAS, variant: "busy", run: (page, t) => t.tap(nRow("git-server"))},
  {name: "sys-nas-sheet-stopped", hash: NAS, variant: "busy", run: (page, t) => t.tap(nRow("bridge-demo-1"))},
  {name: "sys-nas-sheet-frozen", hash: NAS, variant: "busy", run: async (page, t) => { await t.tap(nRow("kopia-snapshot"));
    await page.locator(nAct("rm")).tap({force: true}).catch(() => {}); await t.wait(300);              /* a disabled Delete sends nothing */
    await must(page, () => document.querySelector('[data-act="sys.nact"][data-arg="rm"]').disabled && !document.querySelector('[data-k="yes"]'), "Delete disabled and no confirm sheet"); }},
  {name: "sys-nas-restart", hash: NAS, variant: "busy", posts: [nasPost("git-server", "restart")],
    run: async (page, t) => { await t.tap(nRow("git-server")); await t.tap(nAct("restart")); await t.shot("asked"); await t.tap(YES); await t.wait(300); }},
  {name: "sys-nas-stop", hash: NAS, variant: "busy", posts: [nasPost("git-db", "stop")],
    run: async (page, t) => { await t.tap(nRow("git-db")); await t.tap(nAct("stop")); await t.shot("asked"); await t.tap(YES); await t.wait(300); }},
  {name: "sys-nas-stop-cancel", hash: NAS, variant: "busy", run: async (page, t) => { await t.tap(nRow("git-db")); await t.tap(nAct("stop")); await t.tap(NO); }},
  {name: "sys-nas-restart-cancel", hash: NAS, variant: "busy", run: async (page, t) => { await t.tap(nRow("git-server")); await t.tap(nAct("restart")); await t.tap(NO); }},
  {name: "sys-nas-start", hash: NAS, variant: "busy", posts: [nasPost("bridge-demo-1", "start")],
    run: async (page, t) => { await t.tap(nRow("bridge-demo-1")); await t.tap(nAct("start")); await t.wait(300); }},
  {name: "sys-nas-delete", hash: NAS, variant: "busy", posts: [nasPost("bridge-demo-1", "rm")],
    run: async (page, t) => { await t.tap(nRow("bridge-demo-1")); await t.tap(nAct("rm")); await t.shot("asked"); await t.tap(YES); await t.wait(300); }},
  {name: "sys-nas-delete-cancel", hash: NAS, variant: "busy", run: async (page, t) => { await t.tap(nRow("bridge-demo-1")); await t.tap(nAct("rm")); await t.tap(NO); }},
  {name: "sys-nas-stop-nokey", hash: NAS, variant: "busy", key: false, posts: [nasPost("git-db", "stop")],          /* no key, no key sheet */
    run: async (page, t) => { await t.tap(nRow("git-db")); await t.tap(nAct("stop")); await t.tap(YES); await t.wait(300); }},
  {name: "sys-nas-stop-failed", hash: NAS, variant: "gatefail", posts: [nasPost("git-db", "stop")],
    run: async (page, t) => { await t.tap(nRow("git-db")); await t.tap(nAct("stop")); await t.tap(YES); await t.wait(300); }},

  /* Memory Alpha */
  {name: "sys-alpha", hash: ALPHA, variant: "busy"},
  {name: "sys-alpha-small", hash: ALPHA, variant: "busy", shell: "small"},
  {name: "sys-alpha-bottom", hash: ALPHA, variant: "busy", run: page => page.evaluate(() => scrollTo(0, 9999))},
  {name: "sys-alpha-warn", hash: ALPHA, variant: "busy", run: alphaWarn},
  {name: "sys-alpha-manual-failed", hash: ALPHA, variant: "busy", run: async (page, t) => { await alphaFailed(page); await page.evaluate(() => scrollTo(0, 9999)); }},
  {name: "sys-alpha-down", hash: ALPHA, variant: "busy", run: alphaDown},
  {name: "sys-alpha-offline", hash: ALPHA, variant: "offline"},
  {name: "sys-alpha-backup", hash: ALPHA, variant: "busy", posts: [["/api/memoryalpha/backup", {}]],
    run: async (page, t) => { await t.tap(BACKUP); await t.shot("asked"); await t.tap(YES); await t.wait(300); }},
  {name: "sys-alpha-backup-refused", hash: ALPHA, variant: "refuse", posts: [["/api/memoryalpha/backup", {}]],
    run: async (page, t) => { await t.tap(BACKUP); await t.tap(YES); await t.wait(300); }},
  {name: "sys-alpha-backup-cancel", hash: ALPHA, variant: "busy", run: async (page, t) => { await t.tap(BACKUP); await t.tap(NO); }},
  {name: "sys-alpha-backup-nokey", hash: ALPHA, variant: "busy", key: false, posts: [["/api/memoryalpha/backup", {}], ["/api/memoryalpha/backup", {}]],
    run: async (page, t) => { await t.tap(BACKUP); await t.tap(YES); await enterKey(t); }},
  {name: "sys-alpha-backup-off", hash: ALPHA, variant: "busy", run: async (page, t) => { await alphaWarn(page);       /* a run is in progress: the button sends nothing */
    await page.locator(BACKUP).evaluate(el => el.scrollIntoView({block: "center"})); await page.locator(BACKUP).tap({force: true}).catch(() => {}); await t.wait(300);
    await must(page, () => location.hash === "#/systems/alpha" && document.querySelector('[data-act="sys.backup"]').disabled, "the disabled button still on screen"); }},

  /* Defiant */
  {name: "sys-defiant", hash: DEF, variant: "busy"},
  {name: "sys-defiant-old", hash: DEF, variant: "busy", shell: "old"},
  {name: "sys-defiant-nominal", hash: DEF, variant: "nominal"},
  {name: "sys-defiant-problems", hash: DEF, variant: "nominal", run: defBad},
  {name: "sys-defiant-sites", hash: DEF, variant: "nominal", run: async (page, t) => { await defBad(page); await page.evaluate(() => scrollTo(0, 9999)); }},
  {name: "sys-defiant-off", hash: DEF, variant: "busy", run: defOff},
  {name: "sys-defiant-down", hash: DEF, variant: "busy", run: defDown},
  {name: "sys-defiant-offline", hash: DEF, variant: "offline"},
];
