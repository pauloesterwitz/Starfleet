/* Scenes for Cleansing on the phone's Engineering screen (prefix "cln"): end idle agent sessions and stop their
   idle containers. The fixtures follow fleet.py's /api/containers (with its `sessions` list) and POST
   /api/engineering/stop; all names are made up. Variants: cleanse, cleanse-none (nothing idle), cleanse-old (a Fleet
   without the session list), cleanse-empty, cleanse-partial (one session cannot be ended). */
"use strict";
const C = require("./scenes-core.js").fixtures;
const now = () => Math.floor(Date.now() / 1000), HR = 3600;
const session = (pid, tool, id, idleH, rss, ct, cn, o) => Object.assign({node: "jean-luc", pid, tool, session: id, model: "demo-model", started: now() - 9 * 86400,
  idle_s: idleH == null ? null : Math.round(idleH * HR), rss_mb: rss, containers: cn, ct_ram_mb: ct, cwd: "/home/demo/projects/" + pid, fleet: false}, o || {});
const ctr = (node, name, kind, ram, o) => Object.assign({node, name, image: "demo/" + name + ":1", started: now() - 3 * 86400, project: "", ram_mb: ram, gpu_mb: 0, owner: "demo",
  kind, session_idle_s: null, cpu_idle_s: null, cpu_idle_ge: false, stoppable: kind !== "gpu"}, o || {});
const recent = () => [
  session(4104, "Claude Code", "c3d4e5f6", 0.17, 1000, 200, 1, {cwd: "/home/demo/projects/budget"}),
  session(4107, "Claude Code", null, 30, 500, 0, 0, {fleet: true, cwd: "/home/demo/fleet-ui"})];
const SESSIONS = [
  session(4101, "Claude Code", "a1b2c3d4", 73, 4100, 1000, 2, {cwd: "/home/demo/projects/budget"}),
  session(4102, "Claude Code", "b2c3d4e5", 40, 2200, 0, 0, {cwd: "/home/demo/notes"}),
  session(4103, "opencode", null, 30, 1400, 300, 1, {cwd: "/home/demo/projects/evals"}),
  session(4104, "Claude Code", "c3d4e5f6", 0.17, 1000, 200, 1, {cwd: "/home/demo/projects/budget"}),
  session(4105, "Claude Code", "d4e5f6a7", null, 800, 0, 0, {cwd: "/home/demo/unknown"}),
  session(4106, "Claude Code", "e5f6a7b8", 216, 3000, 900, 3, {cwd: "/home/demo/projects/old-experiment"}),
  session(4107, "Claude Code", null, 30, 500, 0, 0, {fleet: true, cwd: "/home/demo/fleet-ui"}),
];
const CONTAINERS = [
  ctr("jean-luc", "demo-scrape-1", "session", 300, {session_idle_s: 73 * HR, session_pid: 4101}),
  ctr("jean-luc", "demo-search-1", "session", 250, {session_idle_s: 73 * HR, session_pid: 4101}),
  ctr("jean-luc", "demo-db-1", "session", 200, {session_idle_s: 30 * HR, session_pid: 4103}),
  ctr("jean-luc", "demo-vane", "service", 600, {cpu_idle_s: 50 * HR}),
  ctr("kathryn", "demo-mcp-9", "session", 150, {cpu_idle_s: 60 * HR, cpu_idle_ge: true}),
  ctr("jean-luc", "model-server-1", "gpu", 20000, {gpu_mb: 30000, cpu_idle_s: 100 * HR}),
  ctr("jean-luc", "demo-active-1", "session", 100, {session_idle_s: 600, session_pid: 4104}),
];
const payload = variant => {
  const o = {containers: CONTAINERS, errors: {}, at: now()};
  if(variant === "cleanse-old") return o;
  if(variant === "cleanse-none") return Object.assign(o, {containers: CONTAINERS.filter(c => c.stoppable && (c.session_idle_s ?? 0) < 3600 && c.cpu_idle_s == null), sessions: recent()});
  if(variant === "cleanse-empty") return Object.assign(o, {containers: [], sessions: []});
  return Object.assign(o, {sessions: SESSIONS});
};
exports.get = (p, variant) => {
  if(p === "/api/containers" && /^cleanse/.test(variant || "")) return payload(variant);
};
exports.post = (p, body, variant, h) => {
  if(p === "/api/engineering/idle" && /^cleanse/.test(variant || "")) return C.keyOk(h) ? {ok: true, idle_hours: body.hours} : C.denied;
  if(p !== "/api/engineering/stop" || !/^cleanse/.test(variant || "")) return;
  if(!C.keyOk(h)) return C.denied;
  const res = {ok: true, stopped: [], ended: [], refused: [], errors: [], freed_mb: 0};
  for(const s of body.sessions || []){
    const r = SESSIONS.find(x => x.pid === s.pid);
    if(!r || r.fleet) res.refused.push({node: s.node, pid: s.pid, why: "not a running session"});
    else if(variant === "cleanse-partial" && s.pid === 4106) res.errors.push({node: s.node, pid: s.pid, why: "Operation not permitted"});
    else { res.ended.push({node: s.node, pid: s.pid}); res.freed_mb += r.rss_mb + r.ct_ram_mb; }
  }
  const ended = new Set(res.ended.map(e => e.pid));
  for(const c of body.containers || []){
    const r = CONTAINERS.find(x => x.node === c.node && x.name === c.name);
    if(!r) res.refused.push({node: c.node, name: c.name, why: "not running"});
    else if(!r.stoppable) res.refused.push({node: c.node, name: c.name, why: "model server"});
    else if(!ended.has(r.session_pid)){ res.stopped.push({node: c.node, name: c.name}); res.freed_mb += r.ram_mb; }
  }
  return res;
};

const must = (ok, what) => { if(!ok) throw new Error(what); };
const open = '[data-act="sys.copen"]', go = '[data-act="sys.cgo"]', X = ".p-sheet.on .p-x";
const tiles = page => page.$$eval(".p-sheet.on .p-read b", b => b.map(x => x.textContent.trim()));
const toasts = page => page.$$eval("#p-toast .p-toast", t => t.map(x => x.textContent.trim()));
const STOP = "/api/engineering/stop", IDLE = "/api/engineering/idle";
const ALL4 = [{node: "jean-luc", pid: 4101}, {node: "jean-luc", pid: 4106}, {node: "jean-luc", pid: 4102}, {node: "jean-luc", pid: 4103}];
const ALLC = ["demo-scrape-1", "demo-search-1", "demo-db-1", "demo-vane"].map(name => ({node: "jean-luc", name})).concat([{node: "kathryn", name: "demo-mcp-9"}]);
const S = (name, variant, o) => Object.assign({name: "cln-" + name, hash: "#/systems/engineering", variant, settle: 700}, o);
const ses = pid => `[data-act="sys.ses1"][data-arg="jean-luc/${pid}"]`;

exports.scenes = [
  S("card", "cleanse", {run: async (page, t) => {
    const txt = await page.$eval("#p-view", e => e.textContent);
    must(/4 agent sessions and 5 containers have been idle for over 24 hours/.test(txt) && /about 13\.3 GB/.test(txt), "the card does not state the plan: " + txt.slice(0, 400));
    must((await page.$$('[data-act="sys.ses1"]')).length === 7, "seven sessions are listed");
    await t.shot("list"); }}),
  S("sheet", "cleanse", {run: async (page, t) => { await t.tap(open);
    const b = await tiles(page); must(b.join() === "4,5,13.3 GB", "plan tiles " + b.join()); await t.shot("sheet"); }}),
  S("sheet-3-days", "cleanse", {posts: [[IDLE, {hours: 72}]], run: async (page, t) => { await t.tap(open); await t.tap('[data-act="sys.cdays"][data-arg="72"]'); await t.wait(300);
    const b = await tiles(page); must(b.join() === "2,2,8.8 GB", "3 days " + b.join()); }}),
  S("sheet-7-days", "cleanse", {posts: [[IDLE, {hours: 168}]], run: async (page, t) => { await t.tap(open); await t.tap('[data-act="sys.cdays"][data-arg="168"]'); await t.wait(300);
    const b = await tiles(page); must(b.join() === "1,0,3.8 GB", "7 days " + b.join()); await t.shot("sheet"); }}),
  S("sheet-field", "cleanse", {posts: [[IDLE, {hours: 35}]], run: async (page, t) => { await t.tap(open);   /* the "Idle after" field, shared with the Mac */
    await page.fill("#p-sy-ih", "35"); await page.$eval("#p-sy-ih", e => e.dispatchEvent(new Event("change", {bubbles: true}))); await t.wait(400);
    const b = await tiles(page); must(b.join() === "3,4,11.7 GB", "35 hours " + b.join());
    must(await page.$eval("#p-sy-ih", e => e.value) === "35", "the field keeps the value"); await t.shot("field"); }}),
  S("go", "cleanse", {posts: [[STOP, {containers: ALLC, sessions: ALL4}]], run: async (page, t) => {
    await t.tap(open); await t.wait(200); await t.tap(go); await t.wait(400);
    const m = await toasts(page); must(m.length === 1 && m[0] === "Ended 4 sessions, stopped 2 containers, freed about 13.3 GB.", "toast " + JSON.stringify(m));
    must(!(await page.$(".p-sheet.on")), "the sheet closes after a cleanse"); await t.shot("done"); }}),
  S("go-3-days", "cleanse", {posts: [[IDLE, {hours: 72}], [STOP, {containers: ALLC.slice(0, 2), sessions: [ALL4[0], ALL4[1]]}]], run: async (page, t) => {
    await t.tap(open); await t.tap('[data-act="sys.cdays"][data-arg="72"]'); await t.wait(400); await t.tap(go); await t.wait(300);
    must((await toasts(page))[0] === "Ended 2 sessions, stopped 0 containers, freed about 8.8 GB.", "3 days result"); }}),
  S("double-tap", "cleanse", {run: async (page, t) => {
    await page.evaluate(() => { document.querySelector('[data-act="sys.copen"]').click(); document.querySelector('[data-act="sys.cgo"]').click(); });
    await t.wait(300); must(!!(await page.$(".p-sheet.on")) && (await toasts(page)).length === 0, "a tap in the first moments must do nothing"); }}),
  S("cancel", "cleanse", {run: async (page, t) => { await t.tap(open); await t.tap(X); must(!(await page.$(".p-sheet.on")), "closed"); }}),
  S("nothing", "cleanse-none", {run: async (page, t) => {
    const txt = await page.$eval("#p-view", e => e.textContent); must(/Nothing has been idle for over 24 hours/.test(txt), "nothing to cleanse");
    must(await page.$eval(open, b => b.disabled), "the button is disabled"); await t.shot("nothing"); }}),
  S("old-server", "cleanse-old", {run: async (page) => {
    const txt = await page.$eval("#p-view", e => e.textContent); must(!/Cleanse idle sessions|Agent sessions/.test(txt), "a Fleet without a session list shows no cleansing"); }}),
  S("empty", "cleanse-empty", {run: async (page) => {
    const txt = await page.$eval("#p-view", e => e.textContent); must(/No agent sessions/.test(txt) && /Nothing has been idle/.test(txt), "empty: " + txt.slice(0, 300)); }}),
  S("no-key", "cleanse", {key: false, posts: [[STOP, {containers: ALLC, sessions: ALL4}], [STOP, {containers: ALLC, sessions: ALL4}]], run: async (page, t) => {
    await t.tap(open); await t.wait(200); await t.tap(go); await t.wait(300); await t.shot("asked");
    await t.type("#p-keyin", "fixture-key"); await t.tap("#p-keygo"); await t.wait(700);
    must((await toasts(page))[0].startsWith("Ended 4 sessions"), "the cleanse went through after the key was accepted"); }}),
  S("partial", "cleanse-partial", {posts: [[STOP, {containers: ALLC, sessions: ALL4}]], run: async (page, t) => {
    await t.tap(open); await t.wait(200); await t.tap(go); await t.wait(400);
    const m = await toasts(page); must(m.length === 2 && /^Ended 3 sessions/.test(m[0]) && /1 not stopped: pid 4106 \(Operation not permitted\)/.test(m[1]), "partial " + JSON.stringify(m)); await t.shot("partial"); }}),
  S("session-sheet", "cleanse", {run: async (page, t) => { await t.tap(ses(4106));
    const txt = await page.$eval(".p-sheet.on", e => e.textContent); must(/Idle over 24 h/.test(txt) && /End this session/.test(txt), "idle session sheet"); await t.shot("sheet"); }}),
  S("session-unknown", "cleanse", {run: async (page, t) => { await t.tap(ses(4105));
    must(/Cleansing never ends this one/.test(await page.$eval(".p-sheet.on", e => e.textContent)), "unknown idle is explained"); }}),
  S("session-fleet", "cleanse", {run: async (page, t) => { await t.tap(ses(4107));
    const txt = await page.$eval(".p-sheet.on", e => e.textContent); must(/cannot be ended here/.test(txt) && !(await page.$('[data-act="sys.cend"]')), "Fleet's own process has no end button"); }}),
  S("session-end", "cleanse", {posts: [[STOP, {containers: [], sessions: [{node: "jean-luc", pid: 4106}]}]], run: async (page, t) => {
    await t.tap(ses(4106)); await t.tap('[data-act="sys.cend"]'); await t.shot("asked"); await t.tap('[data-k="yes"]'); await t.wait(400);
    must((await toasts(page))[0] === "Ended 1 session, stopped 0 containers, freed about 3.8 GB.", "single end result"); }}),
  S("session-end-cancel", "cleanse", {run: async (page, t) => { await t.tap(ses(4106)); await t.tap('[data-act="sys.cend"]'); await t.tap('[data-k="no"]'); }}),
  S("old-shell", "cleanse", {shell: "old", run: async (page, t) => { await t.tap(open); await t.wait(100); }}),
  S("small", "cleanse", {shell: "small", run: async (page, t) => { await t.tap(open); await t.wait(100); await t.shot("sheet"); }}),
];
