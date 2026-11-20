/* Messages and push on the phone interface: the News rows on Bridge, the Messages screen under Command, the push
   card, the toast for a new message, and the page's end of the app bridge (PADD.pushState, PADD.pushToken, PADD.open).
   A scene picks its message list by opening the page as /padd?ev=<set>#/route: the fixture reads the set from the
   Referer (scenes-core answers an empty list when there is none), so the state fixtures of scenes-core still choose
   "busy" or "nominal". The app is a fake installed in the page: window.webkit.messageHandlers.padd collects what the
   page sends into window.__native. Nothing here reaches a real Fleet; all names, numbers and the token are made up. */
"use strict";
const C = require("./scenes-core.js").fixtures;
const now = () => Date.now() / 1000;
const MID = new Date().setHours(0, 0, 0, 0) / 1000;          /* local midnight */
const today = ago => Math.max(MID + 60, now() - ago);        /* earlier today, even a few minutes after midnight */
const before = h => MID - h * 3600;                          /* h hours before today began: yesterday */
const TOKEN = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12", TOKEN2 = "00ff11ee22dd33cc44bb55aa66997788";
const SEEN = "paddMsgSeen", STORED = "paddPushToken", ALL = "/api/computer/push";

const L = (at, kind, level, state, episode, title, text, o) => Object.assign({id: 0, at, kind, level, state, episode, title, text, go: "", node: "", reminder: 0}, o || {});
/* lines in time order get their ids; open = the episodes still open */
const pack = (lines, open) => { lines = lines.slice().sort((a, b) => a.at - b.at).map((e, i) => Object.assign({}, e, {id: i + 1})); return {events: lines, latest: lines.length, open: open || []}; };
const plus = (set, lines) => ({events: set.events.concat(lines.map((e, i) => Object.assign({}, e, {id: set.latest + 1 + i}))), latest: set.latest + lines.length, open: set.open.concat(lines.filter(e => e.state === "open" && !set.open.includes(e.episode)).map(e => e.episode))});

const LOST = "model-lost:qwen38fn-sglang-tp2-starfleet";
const mixed = pack([
  L(before(20), "backup-done", "info", "point", "", "Backup finished", "Memory Alpha saved 1.9 TB. 0 errors.", {go: "#/systems/alpha"}),
  L(before(14), "load-done", "info", "point", "", "Load finished", "qwen3.6-35b-85tps-mtp4 is ready on Jean-Luc after 6 min.", {go: "#/models", node: "jean-luc"}),
  L(before(9), "memory-low", "act", "open", "memory-low:jean-luc", "Memory low", "Jean-Luc has 2.4 GB free. The floor is 3 GB.", {go: "#/systems/engineering/jean-luc", node: "jean-luc"}),
  L(before(8.5), "memory-low", "act", "resolved", "memory-low:jean-luc", "Memory is back", "Jean-Luc has 9.1 GB free again.", {go: "#/systems/engineering/jean-luc", node: "jean-luc"}),
  L(before(3), "cert", "notice", "point", "", "Certificate ends soon", "The certificate for example.org ends in 13 days.", {go: "#/systems/defiant"}),
  L(today(7 * 3600), "model-lost", "act", "open", "model-lost:gemma4-26b-54tps-kathryn", "Pinned model lost", "gemma4-26b is pinned but not running (2 min). Fleet cannot reload it without you: open Models and press Load.", {go: "#/models", node: "kathryn"}),
  L(today(6.8 * 3600), "model-lost", "act", "resolved", "model-lost:gemma4-26b-54tps-kathryn", "Pinned model is back", "gemma4-26b is running again on Kathryn.", {go: "#/models", node: "kathryn"}),
  L(today(5 * 3600), "reboot-needed", "notice", "open", "reboot-needed:defiant", "Web server needs a reboot", "Defiant has 7 updates waiting and needs a reboot.", {go: "#/systems/defiant"}),
  L(today(4 * 3600), "audit", "info", "point", "", "Red Alert", "Red Alert ran from the phone: 2 models unloaded.", {go: "#/bridge"}),
  L(today(3 * 3600), "backup", "act", "open", "backup:memory-alpha", "Backup is stale", "The last Memory Alpha snapshot is 31 hours old.", {go: "#/systems/alpha"}),
  L(today(2.5 * 3600), "link-slow", "notice", "open", "link-slow:jean-luc", "Slow network port", "Jean-Luc's network port runs at 100 Mb/s.", {go: "#/systems/engineering/jean-luc", node: "jean-luc"}),
  L(today(2 * 3600), "model-lost", "act", "open", LOST, "Pinned model lost", "qwen38fn-sglang is pinned but not running (3 min). Fleet cannot reload it without you: open Models and press Load.", {go: "#/models"}),
  L(today(1.2 * 3600), "sessions-waiting", "act", "open", "sessions-waiting:jean-luc", "Sessions waiting", "3 sessions on Jean-Luc have been waiting for 40 min.", {go: "#/systems/sessions", node: "jean-luc"}),
  L(today(1 * 3600), "test", "info", "point", "", "Test message", "This is a test from Fleet."),
  L(today(30 * 60), "backup", "act", "open", "backup:memory-alpha", "Backup is stale", "The last Memory Alpha snapshot is 37 hours old.", {go: "#/systems/alpha", reminder: 1}),
  L(today(10 * 60), "node-rebooted", "notice", "point", "", "Kathryn restarted", "Kathryn restarted by itself 12 min ago.", {go: "#/systems/engineering/kathryn", node: "kathryn"}),
], [LOST, "sessions-waiting:jean-luc", "backup:memory-alpha", "reboot-needed:defiant", "link-slow:jean-luc"]);
/* 10 unread, 3 act now (the lost model, the waiting sessions, the stale backup's reminder), a resolved episode twice over */
const NEW = L(now() - 4, "node-down", "act", "open", "node-down:kathryn", "Node not answering", "Kathryn has not answered for 90 seconds.", {go: "#/systems/engineering/kathryn", node: "kathryn"});
const NOTE = L(now() - 3, "cert", "notice", "point", "", "Certificate ends soon", "The certificate for example.org ends in 9 days.", {go: "#/systems/defiant"});
const INFO = L(now() - 3, "audit", "info", "point", "", "Auto Destruct", "Auto Destruct was cancelled from the phone.", {go: "#/bridge"});

/* only resolved and info lines: nothing unread, nothing open */
const quiet = pack([
  L(before(20), "backup-done", "info", "point", "", "Backup finished", "Memory Alpha saved 1.9 TB. 0 errors.", {go: "#/systems/alpha"}),
  L(before(9), "memory-low", "act", "resolved", "memory-low:jean-luc", "Memory is back", "Jean-Luc has 9.1 GB free again.", {go: "#/systems/engineering/jean-luc"}),
  L(today(3600), "audit", "info", "point", "", "Red Alert", "Red Alert ran from the phone: 2 models unloaded.", {go: "#/bridge"}),
]);
/* two unread, the newest without a route */
const few = pack([
  L(before(5), "audit", "info", "point", "", "Red Alert", "Red Alert ran from the phone: 2 models unloaded.", {go: "#/bridge"}),
  L(today(2 * 3600), "node-down", "act", "open", "node-down:kathryn", "Node not answering", "Kathryn has not answered for 90 seconds.", {go: "#/systems/engineering/kathryn", node: "kathryn"}),
  L(today(600), "cert", "notice", "point", "", "Certificate ends soon", "The certificate for example.org ends in 13 days."),
], ["node-down:kathryn"]);
/* 150 lines, 30 minutes apart (about three days), a model lost and back every seventh, the last line an open act */
const many = (() => {
  const T = [["audit", "info", "Red Alert", "Red Alert ran from the phone: 2 models unloaded."], ["load-done", "info", "Load finished", "qwen3.6-35b is ready on Jean-Luc after 6 min."],
    ["cert", "notice", "Certificate ends soon", "The certificate for example.org ends in 12 days."], ["node-rebooted", "notice", "Kathryn restarted", "Kathryn restarted by itself."]];
  const lines = [], t0 = now() - 151 * 1800;
  while(lines.length < 148){
    const n = lines.length, at = t0 + n * 1800;
    if(n % 7 === 3){ const ep = "model-lost:demo-model-" + n;
      lines.push(L(at, "model-lost", "act", "open", ep, "Pinned model lost", `demo-model-${n} is pinned but not running (3 min).`, {go: "#/models"}), L(at + 600, "model-lost", "act", "resolved", ep, "Pinned model is back", `demo-model-${n} is running again.`, {go: "#/models"})); }
    else { const [kind, level, title, text] = T[n % 4]; lines.push(L(at, kind, level, "point", "", title, text, {go: "#/bridge"})); }
  }
  lines.push(L(now() - 1800, "memory-low", "notice", "point", "", "Memory dipped", "Kathryn went down to 5 GB free for a moment."),
    L(now() - 120, "node-down", "act", "open", "node-down:kathryn", "Node not answering", "Kathryn has not answered for 90 seconds.", {go: "#/systems/engineering/kathryn"}));
  return pack(lines.slice(0, 150), ["node-down:kathryn"]);
})();

/* one problem that came back: the same episode opened, resolved, opened again (the server reuses the key) */
const AGAIN = "model-lost:gemma4-26b-54tps-kathryn";
const reopened = pack([
  L(today(5 * 3600), "model-lost", "act", "open", AGAIN, "Pinned model lost", "gemma4-26b is pinned but not running (2 min). Fleet cannot reload it without you: open Models and press Load.", {go: "#/models", node: "kathryn"}),
  L(today(4 * 3600), "model-lost", "act", "resolved", AGAIN, "Pinned model is back", "gemma4-26b is running again on Kathryn.", {go: "#/models", node: "kathryn"}),
  L(today(1 * 3600), "model-lost", "act", "open", AGAIN, "Pinned model lost", "gemma4-26b is pinned but not running (4 min). Fleet cannot reload it without you: open Models and press Load.", {go: "#/models", node: "kathryn"}),
], [AGAIN]);
/* one unread line whose route is the Bridge itself */
const selfroute = pack([
  L(today(600), "node-down", "act", "open", "node-down:kathryn", "Node not answering", "Kathryn has not answered for 90 seconds.", {go: "#/bridge", node: "kathryn"}),
], ["node-down:kathryn"]);

const SETS = {empty: pack([]), mixed, quiet, few, many, reopened, selfroute, "mixed-new": plus(mixed, [NEW]), "mixed-note": plus(mixed, [NOTE]), "mixed-info": plus(mixed, [INFO]), "mixed-two": plus(mixed, [NEW, NOTE])};

/* the set and the push condition a scene asked for: in the request's own query, else in the page's address */
const ask = (q, h) => { const ref = new URLSearchParams(String((h || {}).referer || "").split("?")[1] || ""); return k => q && q.get(k) || ref.get(k); };
exports.get = (p, variant, q, h) => {
  const param = ask(q, h);
  if(p === "/api/events"){
    const ev = param("ev"); if(!ev) return;
    if(ev === "error") return {__status: 502, body: {error: "Fleet could not read its message list."}};
    return Object.assign({}, SETS[ev], {at: now()});
  }
  if(p === ALL) return !C.keyOk(h) ? C.denied : param("push") === "off" ? {sender: "off", reason: "No push key on Fleet yet.", devices: 0, env: "development"} : {sender: "ready", reason: "", devices: 1, env: "development"};
};
exports.post = (p, body, variant, h) => {
  if(!p.startsWith(ALL + "/")) return;
  if(!C.keyOk(h)) return C.denied;
  const push = ask(null, h)("push");
  if(p === ALL + "/register") return push === "regfail" ? {__status: 502, body: {error: "Fleet could not save the device."}} : {ok: true, devices: 1};
  if(p === ALL + "/unregister") return {ok: true, devices: 0};
  if(p === ALL + "/test") return push === "off" ? {__status: 409, body: {error: "Push is not set up on Fleet yet."}}
    : push === "nobody" ? {__status: 409, body: {error: "No iPhone has registered yet."}}
    : push === "apple" ? {__status: 502, body: {error: "Apple did not accept the test. See the Fleet log."}} : {ok: true, sent: 1};
};

const must = (ok, what) => { if(!ok) throw new Error(what); };
const fake = page => page.evaluate(() => { window.__native = []; window.webkit = {messageHandlers: {padd: {postMessage: m => window.__native.push(m)}}}; });
/* the same fake, there before the page starts (the page asks the app at boot); like an older app build it never answers */
const fakeAtBoot = page => page.addInitScript(() => { window.__native = []; window.webkit = {messageHandlers: {padd: {postMessage: m => window.__native.push(m)}}}; });
const sent = (page, type) => page.evaluate(t => window.__native.filter(m => m.type === t), type);
const ls = (page, k) => page.evaluate(k => localStorage.getItem(k), k);
const setLs = (page, k, v) => page.evaluate(([k, v]) => localStorage.setItem(k, v), [k, v]);
const pushState = (page, s) => page.evaluate(s => PADD.pushState(s), s);
const toasts = page => page.$$eval(".p-toast", els => els.map(e => e.textContent));
const words = (page, sel) => page.$$eval(sel, els => els.map(e => e.textContent.replace(/\s+/g, " ").trim()));
const card = async page => (await words(page, "#p-mpush"))[0] || "";
const buttons = page => words(page, "#p-mpush .p-btn");
const rows = page => page.$$eval("#p-mlist .p-row", els => els.map(e => ({tag: e.tagName, go: e.dataset.arg || "", edge: e.style.cssText, text: e.textContent.replace(/\s+/g, " ").trim()})));
const tagsOf = page => page.$$eval("#p-mlist .p-row", els => els.map(e => (e.querySelector(".p-tag") || {}).textContent || ""));
const chipN = (page, f) => page.$eval(`[data-act="msg-filter"][data-arg="${f}"] b`, e => +e.textContent);
const tabFig = page => page.$eval('[data-act="tab"][data-arg="command"] b', e => e.textContent);
const hash = page => page.evaluate(() => location.hash);
const waitEvents = page => page.waitForFunction(() => PADD.at.events);
const MSG = '[data-act="go"][data-arg="#/command/messages"]';
/* the picture is one screen tall: bring the News section into it */
const toNews = async page => { await waitEvents(page); await page.evaluate(() => { const h = [...document.querySelectorAll(".p-sec h2")].find(x => x.textContent === "News"); if(h) scrollTo(0, h.getBoundingClientRect().top + scrollY - 150); }); await page.waitForTimeout(250); };
const unread = set => set.events.filter(e => (e.level === "act" || e.level === "notice") && (e.state === "open" || e.state === "point")).length;

/* a scene on one screen with one message set; o may change anything, "variant" is scenes-core's state fixture */
const S = (name, ev, route, o) => Object.assign({name: "msg-" + name, hash: `?ev=${ev}#/${route}`, variant: "nominal"}, o);
/* the push card in one state: the fake app answers, the Messages screen opens from the Command tab.
   o.msg is the message that must stand under the card, o.off that Send a test is disabled, o.scene overrides the scene */
const pushScene = (name, st, says, btns, o) => { o = o || {}; return S("push-" + name, "mixed", "command", Object.assign({run: async (page, t) => {
  await fake(page); if(st) await pushState(page, st);
  await t.tap(MSG); await t.wait(400);
  const c = await card(page);
  must(c.includes(says), `the push card says ${JSON.stringify(c)}, it should say "${says}"`);
  const b = (await buttons(page)).join(" | "); must(b === btns.join(" | "), `the push card has buttons "${b}", expected "${btns.join(" | ")}"`);
  const m = (await words(page, "#p-mpush .p-msg"))[0]; must(m === o.msg, `the message under the card is ${JSON.stringify(m)}, expected ${JSON.stringify(o.msg)}`);
  if(o.off) must(await page.$eval('[data-act="msg-pushtest"]', e => e.disabled), "Send a test stays off while Fleet cannot send");
  must((await sent(page, "push-status")).length === 1, "opening the screen should ask the app for its state once");
} }, o.scene)); };
const reg = (token, env) => ["/api/computer/push/register", {token, env}];

exports.scenes = [
  /* ---- Bridge ---- */
  S("bridge", "mixed", "bridge", {variant: "busy", run: async (page, t) => {
    await waitEvents(page);
    const sec = await words(page, ".p-sec h2"); must(sec.indexOf("News") >= 0 && sec.indexOf("News") < sec.indexOf("Needs attention"), "News should stand above Needs attention: " + sec);
    const r = await words(page, '#p-view .p-row[data-act="go"][data-arg]'); const news = r.slice(r.findIndex(x => /Kathryn restarted/.test(x)));
    must(/Kathryn restarted/.test(news[0]) && /Backup is stale/.test(news[1]) && /Sessions waiting/.test(news[2]) && /All messages/.test(news[3]), "News rows: " + news.slice(0, 4).join(" || "));
    must(/37 hours old/.test(news[1]) && !/31 hours/.test(news.join(" ")), "an episode shows once, with its newest line");
    must(/10 unread/.test(news[3]), "the All messages row should carry the unread count: " + news[3]);
    const edges = await page.$$eval('#p-view .p-row.wrap', els => els.map(e => e.style.cssText)); must(/amber/.test(edges[0]) && /red/.test(edges[1]) && /red/.test(edges[2]), "edges " + edges);
    await toNews(page);
  }}),
  S("bridge-nominal", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page); must((await tabFig(page)) === "9+", "ten unread read 9+ on the Command tab, not " + (await tabFig(page))); await toNews(page); }}),
  S("bridge-none", "empty", "bridge", {run: async (page, t) => { await waitEvents(page);
    const sec = await words(page, ".p-sec h2"); must(!sec.includes("News"), "no messages: no News section, got " + sec); must(!(await words(page, "#p-view")).join().includes("All messages"), "no All messages row either"); }}),
  S("bridge-quiet", "quiet", "bridge", {run: async (page, t) => { await waitEvents(page);
    const r = await words(page, '#p-view [data-arg="#/command/messages"]'); must(r.length === 1 && /^All messages$/.test(r[0]), "nothing unread or open: only All messages, got " + JSON.stringify(r));
    must(!(await page.$("#p-view .p-row.wrap")), "no message rows"); await toNews(page); }}),
  S("bridge-error", "error", "bridge", {run: async page => { await page.waitForFunction(() => PADD.err.events);
    must(!(await words(page, ".p-sec h2")).includes("News"), "a failing list must not draw News"); }}),
  S("bridge-old-shell", "mixed", "bridge", {variant: "busy", shell: "old", run: toNews}),
  S("bridge-small", "mixed", "bridge", {variant: "busy", shell: "small", run: toNews}),
  S("bridge-tap-route", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page);
    await t.tap('#p-view .p-row.wrap[data-arg="#/systems/sessions"]'); must((await hash(page)) === "#/systems/sessions", "a News row opens its route, not " + await hash(page)); }}),
  S("bridge-tap-noroute", "few", "bridge", {run: async (page, t) => { await waitEvents(page);
    await t.tap("#p-view .p-row.wrap"); must((await hash(page)) === "#/command/messages", "a row without a route opens the list, not " + await hash(page)); }}),
  S("bridge-tap-self", "selfroute", "bridge", {run: async (page, t) => { await waitEvents(page);
    must(await page.$eval("#p-view .p-row.wrap", e => e.dataset.arg) === "#/command/messages", "a row whose route is the Bridge itself would do nothing here: it opens the list instead");
    await t.tap("#p-view .p-row.wrap"); must((await hash(page)) === "#/command/messages", "tapping it opens the list, not " + await hash(page)); }}),
  S("bridge-tap-all", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page);
    await t.tap('#p-view .p-row:not(.wrap)[data-arg="#/command/messages"]'); must((await hash(page)) === "#/command/messages", "All messages opens the list"); }}),

  /* ---- the Command screen and the tab ---- */
  S("command", "mixed", "command", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    const r = (await words(page, MSG))[0]; must(/Messages/.test(r) && /3 need you now/.test(r) && /10 unread/.test(r), "the Messages card says " + r);
    must((await page.$$eval("#p-view .p-row", e => e.length)) === 1 && (await page.$eval(MSG, e => e.offsetHeight)) >= 84, "one tall row, under Access");
    await t.shot("card"); await t.tap(MSG); must((await hash(page)) === "#/command/messages", "the card opens the list"); }}),
  S("command-empty", "empty", "command", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    const r = (await words(page, MSG))[0]; must(/Nothing needs you now/.test(r) && !/unread/.test(r), "an empty list: " + r); }}),
  S("command-small", "mixed", "command", {shell: "small"}),
  S("tab-figure", "few", "bridge", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    must((await tabFig(page)) === "2", "two unread read 2 on the Command tab, not " + await tabFig(page));
    await t.tap('[data-act="tab"][data-arg="command"]'); await t.tap(MSG); await t.tap('[data-act="msg-read"]');
    must((await tabFig(page)) === "", "nothing unread leaves the figure empty, not " + await tabFig(page)); }}),

  /* ---- the Messages screen ---- */
  S("list-empty", "empty", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    must((await words(page, "#p-mlist"))[0].includes("No messages yet. Fleet writes one here when something needs you."), "the empty state");
    must(await page.$eval("#p-mlist .p-empty", e => getComputedStyle(e).marginTop) === "6px", "the empty block keeps a gap under the push card"); }}),
  S("reopened", "reopened", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    const r = await rows(page), tg = await tagsOf(page);
    must(r.length === 3, "three lines: " + r.length);
    must(tg.join("|") === "Open|Recovered|Was open", "newest first: the second opening is Open, the end Recovered, the first opening Was open, not " + tg.join("|"));
    must(/red/.test(r[0].edge) && /blue/.test(r[1].edge) && /blue/.test(r[2].edge), "only the newest opening keeps its level colour: " + r.map(x => x.edge).join(" | "));
    must((await chipN(page, "act")) === 1 && (await chipN(page, "unread")) === 2, "one act now (the newest opening), two unread openings");
    const a = await page.evaluate(() => PADD.events.act); must(a === 1, "the badge counts the one open episode, not two: " + a);
    await t.shot("reopened"); }}),
  S("list", "mixed", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    const r = await rows(page); must(r.length === mixed.events.length, `${r.length} rows for ${mixed.events.length} messages`);
    const days = await words(page, "#p-mlist .p-grp"); must(days[0] === "Today" && days[1] === "Yesterday" && days.length === 2, "day headings " + days);
    must(/Node|Kathryn restarted/.test(r[0].text) && /Backup finished/.test(r[r.length - 1].text), "newest first: " + r[0].text.slice(0, 40) + " ... " + r[r.length - 1].text.slice(0, 40));
    const was = r.filter(x => /Was open/.test(x.text)).length, rec = r.filter(x => /Recovered/.test(x.text)).length, op = r.filter(x => /Open|Reminder 1/.test(x.text) && /red|amber/.test(x.edge)).length;
    must(was === 2 && rec === 2, `an open line of an ended episode reads "Was open" (${was} of 2), the end reads Recovered (${rec} of 2)`);
    must(/Reminder 1/.test(r.find(x => /37 hours/.test(x.text)).text), "the reminder is tagged"); must(op === 6, "open lines keep their level colour: " + op);
    const lost = r.find(x => /gemma4-26b is pinned/.test(x.text)); must(/blue/.test(lost.edge), "an open line whose episode ended is blue: " + lost.edge);
    must(r.filter(x => x.tag === "DIV").length === mixed.events.filter(e => !e.go).length, "a line without a route is not a button");
    must((await chipN(page, "all")) === 16 && (await chipN(page, "unread")) === 10 && (await chipN(page, "act")) === 3, "chip counts");
    must((await ls(page, SEEN)) === null, "opening the screen must not mark anything read");
    must(!(await page.$$eval('#p-mpush button, #p-mpush a', e => e.length)), "no app, no push controls");
    await page.evaluate(() => scrollTo(0, 560)); await t.wait(200); await t.shot("middle"); await page.evaluate(() => scrollTo(0, 99999)); await t.wait(200); await t.shot("oldest"); }}),
  S("list-many", "many", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(500);
    must((await rows(page)).length === 150, "150 rows"); const days = await words(page, "#p-mlist .p-grp"); must(days.length >= 3 && days[0] === "Today", "day headings " + days); }}),
  S("list-filters", "mixed", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    await t.tap('[data-act="msg-filter"][data-arg="unread"]'); const u = await rows(page); must(u.length === 10 && u.every(x => /New/.test(x.text)), `Unread: ${u.length} rows`); await t.shot("unread");
    await t.tap('[data-act="msg-filter"][data-arg="act"]'); const a = await rows(page); must(a.length === 3 && a.every(x => /red/.test(x.edge)), `Act now: ${a.length} rows`); await t.shot("act");
    await t.tap('[data-act="msg-filter"][data-arg="all"]'); must((await rows(page)).length === 16, "All again"); }}),
  S("list-filter-empty", "quiet", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    await t.tap('[data-act="msg-filter"][data-arg="unread"]'); must((await words(page, "#p-mlist"))[0].includes("Nothing unread."), "an empty filter says so");
    await t.tap('[data-act="msg-filter"][data-arg="act"]'); must((await words(page, "#p-mlist"))[0].includes("Nothing needs you right now."), "an empty Act now says so"); }}),
  S("list-read", "mixed", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    must((await ls(page, SEEN)) === null && (await chipN(page, "unread")) === 10 && (await tabFig(page)) === "9+", "before: nothing read, 10 unread, tab 9+");
    await t.shot("before"); await t.tap('[data-act="msg-read"]');
    must((await ls(page, SEEN)) === String(mixed.latest), `paddMsgSeen should be ${mixed.latest}, it is ${await ls(page, SEEN)}`);
    must((await chipN(page, "unread")) === 0 && (await tabFig(page)) === "" && (await page.$$eval("#p-mlist .p-badge", e => e.length)) === 0, "after: 0 unread, no figure, no New badges");
    must(await page.$eval('[data-act="msg-read"]', e => e.disabled), "nothing left to mark"); }}),
  S("list-seen-cursor", "mixed", "command/messages", {run: async (page, t) => { await setLs(page, SEEN, String(mixed.latest - 3)); await page.evaluate(() => PADD.refresh(["events"])); await t.wait(500);
    const n = await chipN(page, "unread"); must(n === 2, "after id 13 come 14 (info), 15 (reminder), 16 (restart): 2 unread, got " + n); }}),
  S("list-error", "error", "command/messages", {run: async (page, t) => { await page.waitForFunction(() => PADD.err.events); await t.wait(300);
    must((await words(page, "#p-mlist"))[0].includes("Messages cannot be read: Fleet could not read its message list."), "the error state"); }}),
  S("list-tap-route", "mixed", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    await t.tap('#p-mlist .p-row[data-arg="#/models"]'); must((await hash(page)) === "#/models", "a line with a route opens it"); }}),
  S("list-back", "mixed", "command/messages", {run: async (page, t) => { await t.tap("#p-back"); must((await hash(page)) === "#/command" && (await page.$eval("#p-title", e => e.textContent)) === "Command", "Back goes to Command"); }}),
  S("list-old-shell", "mixed", "command/messages", {shell: "old"}),
  S("list-small", "mixed", "command/messages", {shell: "small"}),
  S("list-nokey", "mixed", "command/messages", {key: false, run: async (page, t) => { await waitEvents(page); await t.wait(300);
    must((await rows(page)).length === 16, "the list needs no key"); }}),

  /* ---- the push card ---- */
  S("push-noapp", "mixed", "command/messages", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    must((await card(page)).includes("Push works in the Starfleet app. Open this page there."), "no app: " + await card(page)); }}),
  pushScene("asking", null, "Asking the app...", []),
  pushScene("undetermined", {permission: "undetermined", registered: false, error: ""}, "Get a message on this iPhone when something needs you", ["Turn on push"]),
  pushScene("entitlement", {permission: "granted", registered: false, error: "entitlement"}, "Push needs the paid Apple membership. It is not set up on this build yet.", []),
  pushScene("denied", {permission: "denied", registered: false, error: ""}, "Notifications are off for Starfleet. Turn them on in the iPhone Settings.", []),
  pushScene("not-registered", {permission: "granted", registered: false, error: ""}, "Push is off or not registered yet.", ["Turn on push"]),
  pushScene("error-text", {permission: "granted", registered: false, error: "No network"}, "Push is off or not registered yet.", ["Turn on push"], {msg: "The app reports a push error: No network"}),
  pushScene("on", {permission: "granted", registered: true, error: ""}, "Push is on for this iPhone.", ["Send a test", "Turn off"]),
  pushScene("sender-off", {permission: "granted", registered: true, error: ""}, "Push is on for this iPhone.", ["Send a test", "Turn off"],
    {msg: "Fleet cannot send push yet: No push key on Fleet yet.", off: true, scene: {hash: "?ev=mixed&push=off#/command"}}),
  pushScene("nokey", {permission: "granted", registered: true, error: ""}, "Push needs the Starfleet key.", ["Enter key"], {scene: {key: false}}),
  pushScene("nokey-denied", {permission: "denied", registered: false, error: ""}, "Notifications are off for Starfleet.", [], {scene: {key: false}}),
  S("push-not-registered-on", "mixed", "command", {run: async (page, t) => {
    await fake(page); await pushState(page, {permission: "granted", registered: false, error: ""}); await t.tap(MSG); await t.tap('[data-act="msg-pushon"]');
    must((await sent(page, "push-enable")).length === 1 && !t.posts.length, "Turn on push in this state asks the app again, and sends nothing to Fleet"); }}),
  S("push-enable", "mixed", "command", {posts: [reg(TOKEN, "development")], run: async (page, t) => {
    await fake(page); await pushState(page, {permission: "undetermined", registered: false, error: ""}); await t.tap(MSG); await t.tap('[data-act="msg-pushon"]');
    const e = await sent(page, "push-enable"); must(e.length === 1 && !t.posts.length, "Turn on push asks the app, and sends nothing to Fleet yet");
    await pushState(page, {permission: "granted", registered: false, error: ""}); await t.wait(200); must((await card(page)).includes("Push is off or not registered yet.") && (await buttons(page)).join() === "Turn on push", "no token yet: " + await card(page));
    await pushState(page, {permission: "granted", registered: true, error: ""}); await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(400);
    must((await card(page)).includes("Push is on for this iPhone.") && (await ls(page, STORED)) === TOKEN, "on, and the token is kept"); }}),
  S("push-test", "mixed", "command", {posts: [["/api/computer/push/test", {}]], run: async (page, t) => {
    await fake(page); await pushState(page, {permission: "granted", registered: true, error: ""}); await t.tap(MSG); await t.tap('[data-act="msg-pushtest"]'); await t.wait(300);
    must((await toasts(page)).includes("Test sent."), "toast: " + await toasts(page)); }}),
  S("push-test-fails", "mixed", "command", {posts: [["/api/computer/push/test", {}]], hash: "?ev=mixed&push=apple#/command", run: async (page, t) => {
    await fake(page); await pushState(page, {permission: "granted", registered: true, error: ""}); await t.tap(MSG); await t.tap('[data-act="msg-pushtest"]'); await t.wait(300);
    must((await toasts(page)).includes("Apple did not accept the test. See the Fleet log."), "the server's error text for a 502: " + await toasts(page));
    must(!(await page.$eval('[data-act="msg-pushtest"]', e => e.disabled)), "the button works again after a failure"); }}),
  S("push-test-nobody", "mixed", "command", {posts: [["/api/computer/push/test", {}]], hash: "?ev=mixed&push=nobody#/command", run: async (page, t) => {
    await fake(page); await pushState(page, {permission: "granted", registered: true, error: ""}); await t.tap(MSG); await t.tap('[data-act="msg-pushtest"]'); await t.wait(300);
    must((await toasts(page)).includes("No iPhone has registered yet."), "the server's error text for a 409: " + await toasts(page));
    must(!(await toasts(page)).includes("Test sent."), "no success toast");
    must(!(await page.$eval('[data-act="msg-pushtest"]', e => e.disabled)), "the button works again"); }}),
  S("push-off", "mixed", "command", {posts: [["/api/computer/push/unregister", {token: TOKEN}]], run: async (page, t) => {
    await setLs(page, STORED, TOKEN); await fake(page); await pushState(page, {permission: "granted", registered: true, error: ""}); await t.tap(MSG);
    await t.tap('[data-act="msg-pushoff"]'); await t.wait(300);
    must((await sent(page, "push-disable")).length === 1, "Turn off tells the app"); must((await ls(page, STORED)) === null, "the stored token is gone");
    await pushState(page, {permission: "granted", registered: false, error: ""}); await t.wait(200);
    must((await card(page)).includes("Push is off for this iPhone.") && (await buttons(page)).join() === "Turn on push", "off, with a way back on: " + await card(page)); }}),
  S("push-off-nothing-stored", "mixed", "command", {run: async (page, t) => {
    await fake(page); await pushState(page, {permission: "granted", registered: true, error: ""}); await t.tap(MSG); await t.tap('[data-act="msg-pushoff"]'); await t.wait(300);
    must((await sent(page, "push-disable")).length === 1, "Turn off still tells the app when Fleet never had a token"); }}),
  S("push-token", "mixed", "command", {posts: [reg(TOKEN, "development"), reg(TOKEN2, "production")], run: async (page, t) => {
    await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(300);
    must(t.posts.length === 1 && (await ls(page, STORED)) === TOKEN, "the first token is registered and kept");
    await page.evaluate(a => PADD.pushToken(a.toUpperCase(), "development"), TOKEN); await t.wait(300);
    must(t.posts.length === 1, "the same token again sends nothing");
    await page.evaluate(a => PADD.pushToken(a, "production"), TOKEN2); await t.wait(300);
    must(t.posts.length === 2 && (await ls(page, STORED)) === TOKEN2, "a new token is registered"); }}),
  S("push-token-dropped", "mixed", "command", {posts: [reg(TOKEN, "development")], run: async (page, t) => {
    await setLs(page, STORED, TOKEN);       /* an earlier page load registered it; Fleet has dropped the device since (Apple said 410) */
    await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(300);
    must(t.posts.length === 1, "a stored token must not stop the registration: " + t.posts.length);
    await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(300);
    must(t.posts.length === 1, "but once per page load is enough"); }}),
  S("push-token-after-off", "mixed", "command", {posts: [reg(TOKEN, "development"), ["/api/computer/push/unregister", {token: TOKEN}], reg(TOKEN, "development")], run: async (page, t) => {
    await fake(page); await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(300);
    await pushState(page, {permission: "granted", registered: true, error: ""}); await t.tap(MSG); await t.tap('[data-act="msg-pushoff"]'); await t.wait(300);
    await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(300);      /* turned on again: the app hands over the same token */
    must(t.posts.length === 3, "after Turn off the same token is registered again: " + t.posts.map(x => x[0]).join(", ")); }}),
  S("push-token-junk", "mixed", "command", {run: async (page, t) => {
    await page.evaluate(() => { PADD.pushToken("", "development"); PADD.pushToken("not a token", "development"); PADD.pushToken(null); }); await t.wait(200);
    must(!t.posts.length && (await ls(page, STORED)) === null, "nothing that is not a hex token is sent"); }}),
  /* the app never answers push-status, like a build from before push: after 3 seconds the card says so */
  S("push-old-app", "mixed", "command", {run: async (page, t) => {
    await fake(page); await t.tap(MSG);
    must((await card(page)).includes("Asking the app..."), "first it asks: " + await card(page));
    await t.wait(3000);
    const c = await card(page); must(c.includes("This app build does not know push yet. Install the new build from the Mac.") && !c.includes("Asking the app..."), "then it says the build is old: " + c);
    must(!(await buttons(page)).length, "nothing to press"); await t.shot("old");
    await pushState(page, {permission: "granted", registered: true, error: ""}); await t.wait(200);
    must((await card(page)).includes("Push is on for this iPhone."), "a late answer still replaces it: " + await card(page)); }}),
  S("push-old-app-nokey", "mixed", "command", {key: false, run: async (page, t) => {
    await fake(page); await t.tap(MSG); await t.wait(3000);
    must((await card(page)).includes("This app build does not know push yet."), "a key cannot help an old build, so the card does not ask for one: " + await card(page)); }}),
  S("push-old-app-boot", "mixed", "command", {run: async (page, t) => {
    await fakeAtBoot(page); await page.reload(); await page.waitForTimeout(3300);              /* the page asked at boot and has waited long enough */
    must((await sent(page, "push-status")).length >= 1, "the page asks at boot");
    await t.tap(MSG); must((await card(page)).includes("This app build does not know push yet."), "the screen opens with the answer, not a new wait: " + await card(page)); }}),
  S("push-asking-answered", "mixed", "command", {run: async (page, t) => {
    await fake(page); await t.tap(MSG); await pushState(page, {permission: "granted", registered: true, error: ""}); await t.wait(3300);
    must((await card(page)).includes("Push is on for this iPhone.") && !(await page.evaluate(() => PADD.push.old)), "an answer in time stops the timer: " + await card(page)); }}),
  S("push-token-nokey", "mixed", "command", {key: false, posts: [reg(TOKEN, "development")], run: async (page, t) => {
    await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(400);
    must(!(await page.$(".p-sheet")), "a token the app reports on its own never opens the key sheet");
    must((await ls(page, STORED)) === null, "a token Fleet did not take is not remembered, so it is tried again later"); }}),
  S("push-token-fails", "mixed", "command", {hash: "?ev=mixed&push=regfail#/command", posts: [reg(TOKEN, "development"), reg(TOKEN, "development")], run: async (page, t) => {
    await fake(page); await pushState(page, {permission: "granted", registered: true, error: ""});
    await page.evaluate(a => PADD.pushToken(a, "development"), TOKEN); await t.wait(300); await t.tap(MSG); await t.wait(300);   /* opening the screen tries once more */
    must((await words(page, "#p-mpush .p-msg"))[0] === "Fleet has not registered this iPhone yet: Fleet could not save the device.", "the failure is shown: " + await words(page, "#p-mpush .p-msg"));
    must((await ls(page, STORED)) === null, "a token Fleet did not take is not remembered"); }}),

  /* ---- a new message while the page is open ---- */
  S("toast-new", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page); await t.wait(400);
    must(!(await toasts(page)).length, "history is never toasted: " + await toasts(page));
    await fake(page);
    await page.evaluate(() => { PADD.sources.events.url = "/api/events?ev=mixed-new"; PADD.refresh(["events"]); }); await t.wait(700);
    const a = await toasts(page); must(a.length === 1 && a[0] === "Node not answering: Kathryn has not answered for 90 seconds.", "toast " + JSON.stringify(a));
    must((await sent(page, "haptic")).some(m => m.style === "warning"), "an act line buzzes with the warning haptic");
    const b = await sent(page, "badge"); must(b.length === 1 && b[0].n === 4, "the badge follows the open act count: " + JSON.stringify(b));
    await page.evaluate(() => PADD.refresh(["events"])); await t.wait(600);
    must((await toasts(page)).length === 1 && (await sent(page, "badge")).length === 1, "the same list again: no second toast, no second badge");
    await t.shot("toast"); }}),
  S("toast-note", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page); await fake(page);
    await page.evaluate(() => { PADD.sources.events.url = "/api/events?ev=mixed-note"; PADD.refresh(["events"]); }); await t.wait(700);
    const a = await toasts(page); must(a.length === 1 && /^Certificate ends soon: /.test(a[0]), "toast " + JSON.stringify(a));
    must((await sent(page, "haptic")).some(m => m.style === "light") && !(await sent(page, "badge")).length, "a notice gets the light haptic and no badge change");
    must(!(await page.$(".p-toast.err")), "a notice is not drawn as an error"); }}),
  S("toast-info", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page); await fake(page);
    await page.evaluate(() => { PADD.sources.events.url = "/api/events?ev=mixed-info"; PADD.refresh(["events"]); }); await t.wait(700);
    must(!(await toasts(page)).length && !(await sent(page, "haptic")).length, "an info line is listed quietly: no toast, no haptic"); }}),
  S("toast-two", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page); await fake(page);
    await page.evaluate(() => { PADD.sources.events.url = "/api/events?ev=mixed-two"; PADD.refresh(["events"]); }); await t.wait(700);
    const a = await toasts(page); must(a.length === 1 && a[0] === "2 new messages. Newest: Certificate ends soon", "one summary toast " + JSON.stringify(a));
    must((await sent(page, "haptic")).filter(m => m.style === "warning").length === 1, "one haptic, the stronger kind"); }}),
  S("toast-after-empty", "empty", "bridge", {run: async (page, t) => { await waitEvents(page); await t.wait(300);
    await page.evaluate(() => { PADD.sources.events.url = "/api/events?ev=mixed"; PADD.refresh(["events"]); }); await t.wait(700);
    must((await toasts(page)).length === 1 && /messages/.test((await toasts(page))[0]), "an empty first answer is the baseline too: later lines are new " + await toasts(page)); }}),

  /* ---- the app calls PADD.open ---- */
  S("open", "mixed", "bridge", {run: async (page, t) => { await waitEvents(page);
    must(await page.evaluate(() => PADD.open("#/models")) === true, "PADD.open answers true for a route"); await t.wait(300);
    must((await page.$eval("#p-title", e => e.textContent)) === "Models", "a tapped notification opens its route");
    await page.evaluate(() => PADD.open("#/command/messages")); await t.wait(300); must((await page.$eval("#p-title", e => e.textContent)) === "Messages", "the Messages route");
    const h = await hash(page); must(await page.evaluate(() => PADD.open("javascript:alert(1)")) === false && await page.evaluate(() => PADD.open("https://example.org/")) === false && (await hash(page)) === h, "anything but a route of this page is ignored"); }}),
  S("open-closes-sheet", "mixed", "bridge", {variant: "busy", run: async (page, t) => { await t.tap("#p-stat"); must(!!(await page.$(".p-sheet.on")), "the status sheet is open");
    await page.evaluate(() => PADD.open("#/bridge")); await t.wait(400); must(!(await page.$(".p-sheet.on")), "a tapped notification closes an open sheet"); }}),
];
