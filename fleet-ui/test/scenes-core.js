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
  const quiet = variant === "nominal";
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
const defiant = variant => ({configured: true, host: "defiant", os: "Debian 13", kernel: "6.12", uptime_s: 41 * 86400, load: "0.12 0.08 0.05", cpus: 4, cpu_pct: 3,
  mem_total_kb: 8 * KB, mem_avail_kb: 5.6 * KB, disk_total: 80e9, disk_used: 21e9, disk_avail: 59e9, rx_bps: 18000, tx_bps: 64000,
  reboot_required: variant !== "nominal", updates: variant !== "nominal" ? 7 : 0, containers: [{name: "caddy", state: "running", status: "Up 41 days"}],
  sites: [{domain: "example.org", status: 200, ms: 84, cert_days: 61, error: null, deployed: now() - 86400 * 3}, {domain: "docs.example.org", status: 200, ms: 91, cert_days: 61, error: null, deployed: now() - 86400 * 9}],
  problems: [], errors: [], status: "ok", at: now()});

exports.fixtures = {state, sessions, alpha, defiant, models, keyOk, denied, POLICY, now};
exports.get = (p, variant, q, h) => {
  if(variant === "offline") return {__status: 502, body: {error: "upstream down"}};
  if(p === "/api/state") return state(variant);
  if(p === "/api/events" && !(q.get("ev") || /[?&]ev=/.test((h || {}).referer || ""))) return {events: [], latest: 0, open: [], at: now()};   /* the default: no messages. scenes-messages answers a page opened as /padd?ev=<set> */
  if(p === "/api/memoryalpha") return alpha();
  if(p === "/api/defiant") return defiant(variant);
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
