/* Memory Core on the phone (vendor/padd-memcore.js hosting vendor/memcore.js): fixtures for the three wiki
   routes and the scenes. The wiki is generated from a seed: about 60 made-up pages, 150 links, two
   Computer sessions. The screen only reads, so no scene may send a POST. */
"use strict";
const { keyOk, denied, now } = require("./scenes-core.js").fixtures;

/* ---------------------------------------------------------------- a generated wiki */
const PAGES = {
  concept: ["Warp field theory", "Warp field stability", "Subspace harmonics", "Plasma flow regulation", "Plasma conduit wear", "Dilithium crystal lattice",
    "Dilithium recrystallisation", "Antimatter containment", "Deflector geometry", "Sensor phase variance", "Tachyon pulse scan", "Isolinear storage",
    "Structural integrity field", "Inertial damping", "Hull stress model", "Bussard collection", "Positronic pathways", "Holomatrix rendering"],
  entity: ["Test Vessel One", "Test Vessel Two", "Demo Station Alpha", "Demo Station Beta", "Example Shipyard", "Relay Buoy 7", "Cargo Bay 3", "Shuttle Demo-1",
    "Warp core Mark 2", "Deflector dish B", "Sensor array 4", "Plasma injector set", "Dilithium chamber", "Main computer core", "Holodeck 2", "Engine room crew"],
  source: ["Dilithium supply notes", "Warp field test log 12", "Engineering manual part 2", "Survey report example", "Plasma leak review", "Sensor calibration sheet",
    "Refit checklist", "Hull inspection log", "Power budget table", "Computer core audit", "Shakedown cruise notes", "Antimatter handling guide",
    "Deflector tuning memo", "Field trial summary"],
  meta: ["Open questions", "Glossary", "Reading list", "Index of demos", "Refit decision log", "Core Mark 1 and Mark 2 compared", "Warp field review session",
    "Supply check session", "Test log template", "Roadmap sketch", "Unsorted ideas", "How to read this wiki"] };
const FOLDER = {concept: "concepts", entity: "entities", source: "sources", meta: "meta"};
const TOPICS = [["propulsion", /warp|plasma|bussard|engine|shuttle|vessel/i], ["power", /dilithium|antimatter|power|core mark/i],
  ["sensors", /sensor|tachyon|deflector|relay|survey|subspace/i], ["structure", /hull|structural|inertial|refit|shipyard|cargo|station/i],
  ["computing", /isolinear|positronic|holo|computer|glossary|index|wiki/i]];
function rng(seed){ let s = seed >>> 0; return () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296; }
const WIKI = (() => {
  const r = rng(1701), pages = [];
  for(const [type, titles] of Object.entries(PAGES)) titles.forEach((t, i) => {
    const topic = (TOPICS.find(x => x[1].test(t)) || TOPICS[i % 5])[0];
    pages.push({p: `wiki/${FOLDER[type]}/${t}.md`, t, type, topic, tags: [topic, topic + (i % 2 ? " tests" : " basics")]}); });
  const seen = new Set(), edges = [], add = (u, v, k) => { const id = Math.min(u, v) + "-" + Math.max(u, v);
    if(u === v || seen.has(id)) return; seen.add(id); edges.push([u, v, k]); };
  const kind = (a, b) => a.type === "source" || b.type === "source" ? (r() < .6 ? "s" : "l") : r() < .4 ? "r" : "l";
  pages.forEach((a, u) => {                                 /* two or three links inside the topic, sometimes one out of it */
    const same = pages.map((b, v) => v).filter(v => v !== u && pages[v].topic === a.topic);
    for(let n = 2 + (r() < .5 ? 1 : 0); n > 0; n--){ const v = same[Math.floor(r() * same.length)]; add(u, v, kind(a, pages[v])); }
    if(r() < .32){ const v = Math.floor(r() * pages.length); add(u, v, "l"); } });
  while(edges.length < 150){ const u = Math.floor(r() * pages.length), v = Math.floor(r() * pages.length); add(u, v, pages[u].topic === pages[v].topic ? kind(pages[u], pages[v]) : "l"); }
  const inw = pages.map(() => 0); edges.forEach(([u, v, k]) => { inw[v] += k === "r" ? 1 : .6; });
  const nodes = pages.map((x, i) => [x.p, x.t, x.type, x.tags, Math.round(inw[i] * 100) / 100,
    `Generated demo page about ${x.t.toLowerCase()}. It belongs to the ${x.topic} notes and exists only to draw this map.`]);
  return {pages, at: now() - 40, graph: {nodes, edges, stubs: 3, log_mtime: now() - 2 * 3600}};
})();
const path = t => WIKI.pages.find(x => x.t === t).p;
const hit = (t, via, text) => ({p: path(t), how: via ? `via [[${via}]] (related): generated` : text ? "text fallback: generated" : "entry (catalogue match: title)",
  via: via || null, rel: via ? "related" : null, text: !!text});
const step = (tool, at, o) => Object.assign({at, error: false, blocked: false, tool, name: "wiki_" + tool, hits: []}, o);
function trails(){
  const a = now() - 300, b = now() - 5400;
  return [
    {id: "c0ffee0000000001", title: "How stable is the warp field?", model: "demo-model-1", updated: a + 58, steps: [
      {tool: "ask", arg: "How stable is the warp field at high load, which tests cover it, and what do the logs of the last trial say about the plasma flow in the conduits?", at: a, hits: []},
      step("overview", a + 3, {arg: "", pages: 60}),
      step("search", a + 9, {arg: "warp field stability", hits: [hit("Warp field stability"), hit("Warp field theory"), hit("Warp field test log 12"),
        hit("Warp core Mark 2", "Warp field theory"), hit("Subspace harmonics", "Warp field stability"), hit("Field trial summary", null, true)]}),
      step("read", a + 16, {arg: "Warp field stability", target: path("Warp field stability")}),
      step("related", a + 22, {arg: "Warp field stability", target: path("Warp field stability"), depth: 1,
        hits: [{p: path("Warp field theory")}, {p: path("Subspace harmonics")}, {p: path("Plasma flow regulation")}, {p: path("Warp core Mark 2")}]}),
      step("read", a + 30, {arg: "Warp field test log 12", target: path("Warp field test log 12")}),
      step("search", a + 37, {arg: "plasma flow", hits: [hit("Plasma flow regulation"), hit("Plasma conduit wear"), hit("Plasma injector set", "Plasma flow regulation")]}),
      step("read", a + 44, {arg: "Plasma flow regulation", target: path("Plasma flow regulation")}),
      {at: a + 50, error: false, blocked: true, tool: "web", name: "google_search", arg: "warp field stability limits", hits: []},
      {tool: "answer", at: a + 58, hits: [], cites: [path("Warp field stability"), path("Warp field test log 12"), "Plasma flow regulation"]}]},
    {id: "c0ffee0000000002", title: "Dilithium supply check", model: "demo-model-1", updated: b + 31, steps: [
      {tool: "ask", arg: "Is there enough dilithium for the next trial?", at: b, hits: []},
      step("search", b + 6, {arg: "dilithium supply", hits: [hit("Dilithium supply notes"), hit("Dilithium chamber"), hit("Dilithium crystal lattice"),
        hit("Power budget table", "Dilithium supply notes")]}),
      step("read", b + 14, {arg: "Dilithium supply notes", target: path("Dilithium supply notes")}),
      step("read", b + 22, {arg: "Power budget table", target: path("Power budget table")}),
      {tool: "answer", at: b + 31, hits: [], cites: [path("Dilithium supply notes")]}]}];
}
function pageMd(p){
  const i = WIKI.pages.findIndex(x => x.p === p); if(i < 0) return null;
  const x = WIKI.pages[i], near = WIKI.graph.edges.filter(e => e[0] === i || e[1] === i).map(e => WIKI.pages[e[0] === i ? e[1] : e[0]].t);
  return `---\ntype: ${x.type}\ntags: [${x.tags.join(", ")}]\ncreated: 2026-01-01\n---\n# ${x.t}\n\nA generated demo page. It stands in for a real note and says nothing true about anything. ` +
    `See [[${near[0]}]] first, then [[${near[1] || near[0]}|the second neighbour]].\n\n## What it covers\n\n- The ${x.topic} side of ${x.t.toLowerCase()}\n- A link that leads nowhere: [[No such page]]\n` +
    `- Inline \`code\` and a table\n\n| Reading | Value |\n|---|---|\n| Demo figure | 42 |\n| Trial runs | 7 |\n\n## Related\n\n${near.slice(0, 6).map(t => `- [[${t}]]`).join("\n")}\n\n` +
    "```\ndemo-app-1 --check example.org\n```\n\nThe last paragraph is here to make the page long enough to scroll on a phone. ".repeat(1) +
    "It repeats itself on purpose. ".repeat(12) + "\n";
}

exports.get = (p, variant, q, h) => {
  if(!p.startsWith("/api/computer/wiki/")) return;
  if(!keyOk(h)) return denied;
  if(p === "/api/computer/wiki/graph") return {graph: WIKI.graph, at: WIKI.at, refreshing: false,
    error: variant === "mc-error" ? "wiki graph from memory-core: export failed Showing the last saved map." : null,
    note: variant === "mc-note" ? "memory-core is stopped: Computer cannot use the wiki until it runs again." : null};
  if(p === "/api/computer/wiki/trails") return {trails: variant === "mc-empty" ? [] : trails()};
  if(p === "/api/computer/wiki/page"){ const md = pageMd(q.get("p")); return md == null ? {__status: 404, body: {error: "not a wiki page"}} : {p: q.get("p"), md}; }
};

/* ---------------------------------------------------------------- checks that run in the page */
/* the map has a size, and nothing a finger can tap inside the component is under 44 px */
const check = page => page.evaluate(() => {
  const bad = [], cv = document.getElementById("mccv").getBoundingClientRect();
  if(cv.width < 120 || cv.height < 120) bad.push(`#mccv is ${Math.round(cv.width)}x${Math.round(cv.height)}`);
  for(const el of document.querySelectorAll("#wikiview button,#wikiview a,#wikiview input,#wikiview summary,#wikiview .mci,#wikiview .mcs,#wikiview #mcsegs i,#wikiview .tg u,.p-mcbar button")){
    const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    if(!r.width || !r.height || getComputedStyle(el).visibility === "hidden" || x < 0 || x > innerWidth || y < 0 || y > innerHeight) continue;
    const top = document.elementFromPoint(x, y);
    if(top && !el.contains(top) && !top.contains(el)) continue;            /* scrolled out of its panel, or covered */
    if(r.width < 43.5 || r.height < 43.5) bad.push(`${Math.round(r.width)}x${Math.round(r.height)} ${el.tagName.toLowerCase()}${el.id ? "#" + el.id : "." + el.className} "${el.textContent.trim().slice(0, 20)}"`);
  }
  if(bad.length) throw new Error("Memory Core: " + bad.join("; "));
});
const expect = (page, what, fn, arg) => page.evaluate(fn, arg).then(ok => { if(!ok) throw new Error("expected " + what); });
const viewIs = (page, v) => expect(page, `the ${v} view`, v => document.getElementById("wikiview").dataset.v === v, v);
/* Where pages sit on the map, top to bottom: the solid 3 px blocks in a page-type colour on the base canvas
   that the overlay canvas, not a button, would receive a tap for. The component keeps its pages to itself,
   so the picture is the only way to find one. plain: without a ring on the overlay (not the selected page). */
const COLOURS = {blue: [153, 204, 255], amber: [255, 204, 102], mauve: [204, 153, 204], peach: [255, 153, 102]};
const dots = (page, names, plain) => page.evaluate(([rgbs, plain]) => {
  const c = document.getElementById("mccv"), o = document.getElementById("mcov"), b = c.getBoundingClientRect(), s = c.width / b.width, n = Math.round(3 * s), found = [];
  const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data, od = o.getContext("2d").getImageData(0, 0, o.width, o.height).data;
  const is = (x, y) => { const i = (y * c.width + x) * 4; return d[i + 3] === 255 && rgbs.some(q => d[i] === q[0] && d[i + 1] === q[1] && d[i + 2] === q[2]); };
  const ringed = (x, y) => { for(let j = -14 * s; j <= 14 * s; j += 2) for(let i = -14 * s; i <= 14 * s; i += 2){
    const X = Math.round(x + i), Y = Math.round(y + j); if(X >= 0 && Y >= 0 && X < o.width && Y < o.height && od[(Y * o.width + X) * 4 + 3] > 200) return true; } return false; };
  for(let y = 0; y < c.height - n; y += 2) for(let x = 0; x < c.width - n; x += 2){
    let ok = true;
    for(let j = 0; j <= n && ok; j += 2) for(let i = 0; i <= n && ok; i += 2) ok = is(x + i, y + j);
    if(!ok) continue;
    const pt = {x: b.left + (x + n / 2) / s, y: b.top + (y + n / 2) / s};
    if(!found.some(f => Math.hypot(f.x - pt.x, f.y - pt.y) < 24) && document.elementFromPoint(pt.x, pt.y) === o && !(plain && ringed(x + n / 2, y + n / 2))) found.push(pt);
  }
  if(!found.length) throw new Error("no such page found on the map");
  return found;
}, [(names || "blue amber mauve peach").split(" ").map(k => COLOURS[k]), !!plain]);
/* at: an index into the dots, counted from the bottom when negative */
const tapDot = async (page, t, names, at, plain) => { const all = await dots(page, names, plain), pt = all.at(at || 0) || all[0]; await page.touchscreen.tap(pt.x, pt.y); await t.wait(700); return pt; };
/* the selected page's white ring on the overlay: where it is in the map box, and the box's height */
const ring = page => page.evaluate(() => {
  const o = document.getElementById("mcov"), b = o.getBoundingClientRect(), s = o.width / b.width, d = o.getContext("2d").getImageData(0, 0, o.width, o.height).data; let sx = 0, sy = 0, n = 0;
  for(let y = 0; y < o.height; y += 2) for(let x = 0; x < o.width; x += 2){ const i = (y * o.width + x) * 4; if(d[i] === 255 && d[i + 1] === 255 && d[i + 2] === 255 && d[i + 3] > 200){ sx += x; sy += y; n++; } }
  if(!n) throw new Error("the selected page's ring is not on the map");
  return {x: b.left + sx / n / s, y: sy / n / s, h: o.height / s};
});
/* a pan moves the page up or down only: had the zoom changed, it would have moved sideways too */
const panned = (r, pt, want, where) => { if(Math.abs(r.y - want) > 8 || Math.abs(r.x - pt.x) > 4)
  throw new Error(`the tapped page sits ${Math.round(r.y)} px into a ${Math.round(r.h)} px strip and ${Math.round(r.x - pt.x)} px sideways, expected ${where} and 0`); };
const seg = v => `.p-mcbar [data-arg="${v}"]`;
const pick = async (page, t, title) => { await t.type("#mcq", title); await page.locator("#mcq").press("Enter"); await t.wait(900); };   /* the datalist way to a page */
const cardTitle = page => page.evaluate(() => document.querySelector("#mcpage h3").firstChild.textContent);

const H = "#/systems/core", S = 3200;      /* S: the force layout and the camera have come to rest */
exports.scenes = [
  {name: "mc-map", hash: H, settle: S, run: async page => { await check(page); await viewIs(page, "map");
    await expect(page, "the meta line in the header", () => /60 pages · 150 links/.test(document.getElementById("p-hx").textContent)); }},
  {name: "mc-map-empty", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { await t.shot("plain"); await t.tap("#mczh"); await check(page);
    await expect(page, "no replay bar without a session", () => !document.getElementById("mctl").offsetHeight); }},
  {name: "mc-core", hash: H, settle: S, run: async (page, t) => { await t.tap("#mccore"); await t.tap("#mcheat"); await t.wait(1500); await check(page); }},
  /* a drag pans and two fingers zoom: both must still reach the overlay canvas */
  {name: "mc-gestures", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => {
    const snap = () => page.evaluate(() => { const u = document.getElementById("mccv").toDataURL(); let h = 0; for(let i = 0; i < u.length; i += 7) h = (h * 31 + u.charCodeAt(i)) | 0; return h; });
    const c = await page.evaluate(() => { const r = document.getElementById("mcov").getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 3}; });
    const before = await snap();
    await page.mouse.move(c.x, c.y); await page.mouse.down(); await page.mouse.move(c.x + 70, c.y + 40, {steps: 5}); await page.mouse.up(); await t.wait(300);
    if(await snap() === before) throw new Error("a drag on the map did not move it");
    const zoom = () => page.evaluate(() => parseInt(document.getElementById("mczl").textContent, 10));
    const z0 = await zoom();
    await page.evaluate(({x, y}) => {
      const ov = document.getElementById("mcov");
      for(const dx of [-30, 30, -80, 80]) if(document.elementFromPoint(x + dx, y) !== ov) throw new Error("something covers the map at a finger");
      const ev = (type, id, dx) => ov.dispatchEvent(new PointerEvent(type, {pointerId: id, pointerType: "touch", clientX: x + dx, clientY: y, bubbles: true}));
      ev("pointerdown", 11, -30); ev("pointerdown", 12, 30); ev("pointermove", 11, -80); ev("pointermove", 12, 80); ev("pointerup", 11, -80); ev("pointerup", 12, 80);
    }, c);
    await t.wait(300);
    if(!(await zoom() > z0 * 1.5)) throw new Error("a pinch on the map did not zoom it");
    await expect(page, "touch-action none on the overlay canvas", () => getComputedStyle(document.getElementById("mcov")).touchAction === "none");
    await viewIs(page, "map"); }},
  {name: "mc-page", hash: H, settle: S, run: async (page, t) => { await tapDot(page, t, "blue", 4); await viewIs(page, "page"); await check(page);
    await expect(page, "the page card", () => document.getElementById("mcpage").offsetHeight > 100); }},
  /* the map shrinks to a strip when the card opens: a page tapped above or below it is pulled back in, the zoom stays */
  {name: "mc-page-pan-top", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { const pt = await tapDot(page, t, "", 0); await viewIs(page, "page"); panned(await ring(page), pt, 40, "40 px"); }},
  {name: "mc-page-pan-bottom", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { const pt = await tapDot(page, t, "", -1); await viewIs(page, "page"); const r = await ring(page); panned(r, pt, r.h - 40, "40 px from its end");
    await t.tap(seg("map")); await t.wait(500); const m = await ring(page); if(m.y < 40 || m.y > m.h - 40) throw new Error("the page left the map on the way back"); }},
  {name: "mc-page-scrolled", hash: H, settle: S, run: async (page, t) => { await tapDot(page, t, "blue", 4);
    await page.evaluate(() => { document.getElementById("mcr").scrollTop = 1e5; }); await t.wait(200); await check(page); }},
  /* inside the card: a tag probes, a neighbour becomes the page, and its card starts at the top of the panel */
  {name: "mc-page-links", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { await pick(page, t, "Warp field theory"); await t.tap("#mcpage .tg u");
    await expect(page, "the tag as a probe, still on the Page view", () => document.getElementById("mcprobe").style.display === "block" && document.getElementById("mcq").value === "propulsion" && document.getElementById("wikiview").dataset.v === "page");
    await t.shot("probe"); const was = await cardTitle(page); await t.tap("#mcpage .nb a >> nth=-1"); await t.wait(700);
    if(await cardTitle(page) === was) throw new Error("a tap on a neighbour did not select it");
    await expect(page, "the new card at the top of the panel", () => { const r = document.getElementById("mcr").getBoundingClientRect(), c = document.getElementById("mcpage").getBoundingClientRect(); return c.top - r.top > 0 && c.top - r.top < 24; });
    await check(page); }},
  {name: "mc-page-close", hash: H, settle: S, run: async (page, t) => { await tapDot(page, t, "amber", 2); await viewIs(page, "page"); await t.tap("#mcpx"); await viewIs(page, "map"); await check(page); }},
  /* Path to, then a second page: the touch way to the desktop's Shift-click */
  {name: "mc-path", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { await tapDot(page, t, "blue", 3); await t.tap("#mcpp"); await t.wait(600); await tapDot(page, t, "", 0, true);
    await expect(page, "a shortest path in the page card", () => !!document.querySelector("#mcpage .nb.path a")); await viewIs(page, "page");
    await page.evaluate(() => { document.getElementById("mcr").scrollTop = 190; }); await t.wait(200); await check(page); }},
  {name: "mc-page-empty", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { await t.tap(seg("page")); await check(page);
    await expect(page, "the empty note", () => document.getElementById("p-mcnone").offsetHeight > 0); }},
  {name: "mc-sessions", hash: H, settle: S, run: async (page, t) => { await t.tap(seg("sessions")); await viewIs(page, "sessions"); await check(page); }},
  {name: "mc-sessions-empty", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { await t.tap(seg("sessions")); await check(page); }},
  {name: "mc-session-replay", hash: H, settle: S, run: async (page, t) => { await t.tap("#mcsegs i"); await t.wait(1200); await t.shot("question");
    await t.tap(seg("sessions")); await t.tap("#mcchats .mci >> nth=1"); await viewIs(page, "map");
    await expect(page, "the second session selected", () => document.querySelectorAll("#mcsegs i").length === 5);
    await t.tap("#mcsegs i >> nth=1"); await t.wait(1400); await check(page);
    await expect(page, "the step caption on the map", () => /02 Search/i.test(document.getElementById("mccap").textContent)); }},
  {name: "mc-replay-playing", hash: H, settle: S, run: async (page, t) => { await t.tap("#mcplay"); await t.wait(4200); await check(page);
    await expect(page, "the replay to be running", () => document.getElementById("mcplay").textContent === "Pause" && document.querySelectorAll("#mcsegs i.fut").length > 0); }},
  {name: "mc-term", hash: H, settle: S, run: async (page, t) => { await t.tap(seg("sessions")); await t.tap("#mcterms .mcterm"); await t.wait(900); await viewIs(page, "map");
    await expect(page, "the term in the probe field", () => document.getElementById("mcq").value.length > 2); await t.shot("map"); await t.tap(seg("page")); await check(page); }},
  {name: "mc-consulted", hash: H, settle: S, run: async (page, t) => { await t.tap(seg("sessions")); await page.evaluate(() => { document.getElementById("mcl").scrollTop = 1e5; }); await t.wait(200);
    await t.shot("list"); await t.tap("#mctop .mci"); await t.wait(900); await viewIs(page, "page"); await check(page); }},
  /* a step of the access log selects the page it read; a finger must not leave the row or the bar "hot" */
  {name: "mc-log-step", hash: H, settle: S, run: async (page, t) => { await t.tap(seg("page")); await t.tap("#mclog .mcs >> nth=3"); await t.wait(900); await viewIs(page, "page");
    await expect(page, "the page that step read", () => /Warp field stability/.test(document.querySelector("#mcpage h3").textContent));
    await expect(page, "hover handlers kept away from a finger", () => { for(const el of document.querySelectorAll("#mclog .mcs,#mcpage .nb a")) el.dispatchEvent(new MouseEvent("mouseenter"));
      return matchMedia("(hover: none)").matches && !document.querySelector("#mclog .mcs.hot,#mcsegs i.hot"); });
    await check(page); }},
  {name: "mc-probe", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => { await t.type("#mcq", "warp plasma"); await t.wait(1300); await viewIs(page, "map"); await check(page);
    await t.shot("map"); await t.tap(seg("page")); await check(page); await expect(page, "the probe card", () => /Term probe/.test(document.getElementById("mcprobe").textContent)); }},
  /* the keyboard is up: the tab bar and the view bar make room, the map stays in sight */
  {name: "mc-probe-keyboard", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => {
    await page.evaluate(() => { Object.defineProperty(window, "visualViewport", {configurable: true, value: {height: innerHeight - 336, offsetTop: 0, addEventListener(){}}}); dispatchEvent(new Event("resize")); });
    await t.type("#mcq", "dilithium"); await t.wait(1300); await check(page);
    await expect(page, "a map of at least 150 px over the keyboard", () => document.getElementById("mcbox").clientHeight >= 150); }},
  {name: "mc-doc", hash: H, settle: S, run: async (page, t) => { await pick(page, t, "Warp field theory"); await viewIs(page, "page");
    await expect(page, "the picked page", () => /Warp field theory/.test(document.querySelector("#mcpage h3").textContent));
    await t.shot("page"); await t.tap("#mcpo"); await t.wait(500); await check(page);
    await expect(page, "the document over the whole screen", () => { const r = document.getElementById("mcdoc").getBoundingClientRect(), x = document.getElementById("mcdx").getBoundingClientRect();
      return r.left === 0 && r.width === innerWidth && Math.abs(r.bottom - innerHeight) < 1 && r.height > innerHeight * .9 && x.height >= 44 && document.elementFromPoint(innerWidth / 2, innerHeight - 30).closest("#mcdoc"); });
    await t.shot("top"); await t.tap("#mcdoc summary"); await check(page); }},
  {name: "mc-doc-scrolled", hash: H, variant: "mc-empty", shell: "small", settle: S, run: async (page, t) => { await pick(page, t, "Dilithium supply notes"); await t.tap("#mcpo"); await t.wait(500);
    await page.evaluate(() => { document.querySelector("#mcdoc .cm").scrollTop = 420; }); await t.wait(200); await check(page); }},
  /* [[wiki links]] in the document select that page and the document follows. The markdown renderer is
     Computer's (window.md); here a three-line stand-in turns the links into anchors so the scene does not
     depend on that file. */
  {name: "mc-doc-link", hash: H, variant: "mc-empty", settle: S, run: async (page, t) => {
    await page.evaluate(() => { window.md = s => "<p>" + s.replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]))
      .replace(/\[([^\]]+)\]\((#wiki:[^)]+)\)/g, '<a href="$2">$1</a>').replace(/\n\n/g, "</p><p>") + "</p>"; });
    await pick(page, t, "Warp field theory"); await t.tap("#mcpo"); await t.wait(500); await check(page); await t.shot("first");
    const to = await page.evaluate(() => document.querySelector("#mcdoc .body a:not(.dead)").textContent);
    await t.tap("#mcdoc .body a:not(.dead)"); await t.wait(700);
    await expect(page, "the document to follow the link to " + to, to => document.querySelector("#mcdoc .hd b").textContent === to && /generated demo page/i.test(document.querySelector("#mcdoc .body").textContent), to);
    await expect(page, "a dead link marked", () => !!document.querySelector("#mcdoc .body a.dead"));
    await t.shot("second"); await t.tap("#mcdx"); await expect(page, "the document closed", () => !document.getElementById("mcdoc")); await viewIs(page, "page"); await check(page); }},
  /* without Computer's file there is no markdown renderer: the page still opens, as plain text */
  {name: "mc-doc-plain", hash: "#/bridge", variant: "mc-empty", settle: 600, run: async (page, t) => { await page.evaluate(() => { delete window.md; location.hash = "#/systems/core"; }); await t.wait(S);
    await pick(page, t, "Glossary"); await t.tap("#mcpo"); await t.wait(500); await check(page);
    await expect(page, "the page as plain text", () => /# Glossary/.test(document.querySelector("#mcdoc .body").textContent) && !document.querySelector("#mcdoc .body h1,#mcdoc .body a")); }},
  /* the component's files do not arrive: a message and a way to try again, not a blank stage */
  {name: "mc-load-failed", hash: "#/bridge", settle: 600, run: async (page, t) => {
    await page.evaluate(() => { const load = PADD.load; window.mcLoad = load; PADD.load = u => /memcore\.js/.test(u) ? Promise.reject(new Error("offline")) : load(u); location.hash = "#/systems/core"; }); await t.wait(900);
    await expect(page, "the message and Try again", () => /could not be loaded/.test(document.querySelector(".p-mcgate").textContent) && !!document.querySelector('[data-act="mcretry"]') && document.getElementById("wikiview").hidden);
    await t.shot("failed"); await page.evaluate(() => { PADD.load = window.mcLoad; }); await t.tap('[data-act="mcretry"]'); await t.wait(S); await check(page);
    await expect(page, "the map after Try again", () => view === "wiki" && /60 pages/.test(document.getElementById("p-hx").textContent)); }},
  {name: "mc-nokey", hash: H, key: false, settle: 900, run: page => expect(page, "the key card instead of the map", () =>
    /Memory Core needs the Starfleet key/.test(document.getElementById("p-stage").textContent) && !document.getElementById("mccv") && document.querySelector(".p-mcbar").hidden)},
  {name: "mc-key-entered", hash: H, key: false, settle: 900, run: async (page, t) => { await t.tap('.p-mcgate [data-act="key"]'); await t.type("#p-keyin", "fixture-key"); await t.shot("asked");
    await t.tap("#p-keygo"); await t.wait(S); await check(page); await expect(page, "the map once the key is in", () => !document.querySelector(".p-mcgate").offsetHeight && /60 pages/.test(document.getElementById("p-hx").textContent)); }},
  /* a stored key that Fleet refuses: the component closes and the key card takes its place */
  {name: "mc-wrong-key", hash: "#/bridge", key: false, settle: 600, run: async (page, t) => {
    await page.evaluate(() => { PADD.key.set("stale-key"); location.hash = "#/systems/core"; }); await t.wait(1500);
    await expect(page, "the key card after the refusal", () => /Memory Core needs the Starfleet key/.test(document.querySelector(".p-mcgate").textContent) && document.getElementById("wikiview").hidden && view === "");
    await t.shot("refused"); await t.tap('.p-mcgate [data-act="key"]'); await t.type("#p-keyin", "fixture-key"); await t.tap("#p-keygo"); await t.wait(S); await check(page);
    await expect(page, "the map once the key is accepted", () => view === "wiki" && !document.getElementById("wikiview").hidden && /60 pages/.test(document.getElementById("p-hx").textContent)); }},
  /* leave and come back: the same element, the same map, the document closed */
  {name: "mc-revisit", hash: H, settle: S, run: async (page, t) => { await pick(page, t, "Glossary"); await t.tap("#mcpo");
    await page.evaluate(() => { window.mcEl = document.getElementById("wikiview"); location.hash = "#/bridge"; }); await t.wait(700);
    await expect(page, "the component closed on the way out", () => view === "" && !document.body.classList.contains("wiki") && !document.getElementById("wikiview"));
    await page.evaluate(() => { location.hash = "#/systems/core"; }); await t.wait(1200); await check(page);
    await expect(page, "the kept element with its page card", () => document.getElementById("wikiview") === window.mcEl && view === "wiki" && !document.getElementById("mcdoc") && /Glossary/.test(document.querySelector("#mcpage h3").textContent)); }},
  {name: "mc-note", hash: H, variant: "mc-note", settle: S, run: page => check(page)},
  {name: "mc-error", hash: H, variant: "mc-error", settle: S, run: page => check(page)},
  {name: "mc-old-shell", hash: H, shell: "old", settle: S, run: page => check(page)},
  {name: "mc-old-shell-page", hash: H, shell: "old", settle: S, run: async (page, t) => { await tapDot(page, t, "mauve", 2); await viewIs(page, "page"); await check(page); }},
  {name: "mc-small", hash: H, shell: "small", settle: S, run: page => check(page)},
  {name: "mc-small-page", hash: H, shell: "small", settle: S, run: async (page, t) => { await tapDot(page, t, "blue", -1); await viewIs(page, "page"); await check(page);
    await expect(page, "a map strip of at least 150 px", () => document.getElementById("mcbox").clientHeight >= 150); }},
  {name: "mc-small-sessions", hash: H, shell: "small", settle: S, run: async (page, t) => { await t.tap(seg("sessions")); await check(page); }},
];
