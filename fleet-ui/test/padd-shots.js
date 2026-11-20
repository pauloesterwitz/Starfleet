#!/usr/bin/env node
/* Screenshots and checks for the phone interface (padd.html), on WebKit at iPhone size.

   Everything is answered from generated fixtures under the made-up origin http://fleet.test: the page
   under test never reaches a real Fleet, so it can never pin, unload, stop or alert anything, and it
   never needs a real Starfleet key (the fixtures accept the string "fixture-key"). Requests to any other
   origin are aborted and fail the run, Google Fonts excepted. Every POST is recorded and compared with
   what the scene declared.

   Scenes and their fixtures live in scenes-*.js next to this file; each exports
     { scenes: [{name, hash, variant, key, shell, run(page, t), posts, skip}], get(path, variant, query, headers), post(path, body, variant, headers) }
   get/post return undefined for paths they do not know, an object for a JSON answer,
   {__status, body} for an error, or {__sse: [events]} for a stream.

   Run:  PW=<path to the playwright package> node fleet-ui/test/padd-shots.js [name-filter]
         OUT=<folder> for the pictures (default: fleet-ui/test/out). Exit code 1 when a check fails. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const { webkit, devices } = require(process.env.PW || "playwright");
const UI = path.join(__dirname, ".."), OUT = process.env.OUT || path.join(__dirname, "out"), BASE = "http://fleet.test";
const only = process.argv[2] || "";
const mods = fs.readdirSync(__dirname).filter(f => /^scenes-.*\.js$/.test(f)).sort().map(f => {
  try{ return require(path.join(__dirname, f)); }catch(e){ console.error(`SKIPPED ${f}: ${e.message.split("\n")[0]}`); return {}; } });
const scenes = mods.flatMap(m => m.scenes || []).filter(s => !s.skip && (!only || s.name.includes(only)));
const SHELL = { app: {w: 393, h: 852, sat: 59, sab: 34},      /* the app showing the page edge to edge */
                old: {w: 393, h: 759, sat: 0, sab: 0},        /* the installed app: web view inside the safe area */
                small: {w: 375, h: 667, sat: 20, sab: 0} };   /* the narrowest iPhone still on iOS 17 */
const TYPES = {".js": "application/javascript", ".css": "text/css"};

function answer(method, url, body, variant, headers){
  for(const m of mods){
    const r = method === "GET" ? (m.get && m.get(url.pathname, variant, url.searchParams, headers)) : (m.post && m.post(url.pathname, body, variant, headers));
    if(r !== undefined) return r;
  }
}
async function fulfil(route, r){
  if(r && r.__sse) return route.fulfill({status: 200, contentType: "text/event-stream", body: r.__sse.map(e => "data: " + JSON.stringify(e) + "\n\n").join("")});
  if(r && r.__status) return route.fulfill({status: r.__status, contentType: "application/json", body: JSON.stringify(r.body || {})});
  return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(r)});
}

/* The desktop page's own grouping, run on the same fixture: the phone's copy must give the same rows. */
function desktopGroups(models){
  const src = fs.readFileSync(path.join(UI, "fleet.html"), "utf8");
  const a = src.indexOf("const TPS_IN_NAME="), b = src.indexOf("(function selfCheck(){", a);
  if(a < 0 || b < 0) throw new Error("fleet.html: grouping block not found, re-check the port in padd.js");
  const box = {filter: "all", TAG: {}, document: {activeElement: null}, isImg: m => !!(m.kind && m.kind !== "llm"), out: null, models};
  vm.runInNewContext(src.slice(a, b) + "\nout = groupModels(models, {}).map(m => [m.id, m._base || null, m._family || null, m.tps ?? null]);", box);
  return JSON.stringify(box.out);
}

(async () => {
  fs.mkdirSync(OUT, {recursive: true});
  const browser = await webkit.launch(), failures = [], shots = [];
  for(const sc of scenes){
    const sh = SHELL[sc.shell || "app"], problems = [], posts = [], missing = [];
    const ctx = await browser.newContext(Object.assign({}, devices["iPhone 15"], {viewport: {width: sh.w, height: sh.h}, deviceScaleFactor: 2}));
    await ctx.route("**/*", async route => {
      const req = route.request(), url = new URL(req.url());
      if(/^fonts\.(googleapis|gstatic)\.com$/.test(url.host)) return route.continue();
      if(url.origin !== BASE){ problems.push("request left the fixture origin: " + url.origin); return route.abort(); }
      if(req.method() !== "GET"){
        let body = null; const raw = req.postData();
        try{ body = raw ? JSON.parse(raw) : null; }catch(e){ body = "<" + (raw || "").length + " bytes>"; }
        posts.push([url.pathname, body]);
        const r = answer("POST", url, body, sc.variant, req.headers());
        if(r === undefined){ missing.push("POST " + url.pathname); return route.fulfill({status: 404, contentType: "application/json", body: '{"error":"no fixture"}'}); }
        return fulfil(route, r);
      }
      if(url.pathname === "/" || url.pathname === "/padd") return route.fulfill({status: 200, contentType: "text/html; charset=utf-8", body: fs.readFileSync(path.join(UI, "padd.html"))});
      if(url.pathname.startsWith("/vendor/")){
        const f = path.join(UI, "vendor", path.basename(url.pathname));
        return fs.existsSync(f) ? route.fulfill({status: 200, contentType: TYPES[path.extname(f)] || "text/plain", body: fs.readFileSync(f)}) : route.fulfill({status: 404, body: ""});
      }
      if(url.pathname.startsWith("/api/")){
        const r = answer("GET", url, null, sc.variant, req.headers());
        if(r === undefined){ missing.push("GET " + url.pathname); return route.fulfill({status: 404, contentType: "text/plain", body: "not found"}); }
        return fulfil(route, r);
      }
      return route.fulfill({status: 404, body: ""});
    });
    await ctx.addInitScript(([key, sat, sab]) => {
      try{ if(key) localStorage.setItem("starfleetKey", "fixture-key"); else localStorage.removeItem("starfleetKey"); localStorage.removeItem("paddRoute"); }catch(e){}
      addEventListener("DOMContentLoaded", () => { const s = document.createElement("style"); s.textContent = `:root{--sat:${sat}px;--sab:${sab}px}`; document.head.appendChild(s); });
    }, [sc.key !== false, sh.sat, sh.sab]);
    const page = await ctx.newPage();
    page.on("pageerror", e => problems.push("page error: " + e.message));
    page.on("console", m => { if(m.type() === "error" && !/Failed to load resource/.test(m.text())) problems.push("console: " + m.text()); });
    const extra = [];
    const t = { tap: async sel => { if(/data-k="yes"/.test(sel)) await page.waitForTimeout(500);       /* a confirm ignores taps in its first 450 ms: no person is faster */
                  await page.locator(sel).first().tap(); await page.waitForTimeout(380); },
                type: async (sel, text) => { await page.locator(sel).first().fill(text); await page.waitForTimeout(150); },
                wait: ms => page.waitForTimeout(ms), posts,
                shot: async name => { const f = path.join(OUT, `${sc.name}--${name}.png`); await page.screenshot({path: f}); extra.push(f); } };
    try{
      await page.goto(BASE + "/padd" + (sc.hash || ""));
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(sc.settle || 500);
      if(sc.run) await sc.run(page, t);
      await page.waitForTimeout(250);
      const found = await page.evaluate(() => {
        const out = [], vw = innerWidth, vh = innerHeight, slop = ".p-stat,.p-hbtn";
        if(document.documentElement.scrollWidth > vw + 1) out.push(`page is ${document.documentElement.scrollWidth}px wide in a ${vw}px viewport`);
        for(const el of document.querySelectorAll("button,a[href],[data-act],input,textarea,select")){
          const box = (/^(INPUT|TEXTAREA)$/.test(el.tagName) && el.closest("label")) || el, r = box.getBoundingClientRect(), cs = getComputedStyle(el);
          if(!r.width || !r.height || cs.visibility === "hidden" || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
          const top = document.elementFromPoint(Math.min(vw - 1, Math.max(0, r.left + r.width / 2)), Math.min(vh - 1, Math.max(0, r.top + r.height / 2)));
          if(top && !box.contains(top) && !top.contains(box)) continue;                 /* covered by a sheet */
          const what = `${el.tagName.toLowerCase()}${el.dataset.act ? "[" + el.dataset.act + "]" : ""} "${(el.textContent || el.placeholder || "").trim().slice(0, 24)}"`;
          if(!/^(BUTTON|A|INPUT|TEXTAREA|SELECT|LABEL|SUMMARY)$/.test(el.tagName)) out.push(`not a real control: ${what}`);
          if(!el.matches(slop) && (r.width < 43.5 || r.height < 43.5)) out.push(`touch target ${Math.round(r.width)}x${Math.round(r.height)}: ${what}`);
          if(/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && parseFloat(cs.fontSize) < 16) out.push(`field under 16px (iOS zooms in): ${what}`);
        }
        return out;
      });
      problems.push(...found);
      if(sc.groups){
        const mine = await page.evaluate(() => JSON.stringify(PADD.model.groupModels(PADD.data.state.models.filter(m => !m.kind || m.kind === "llm"), {}, "all", {}).map(m => [m.id, m._base || null, m._family || null, m.tps ?? null])));
        const state = answer("GET", new URL(BASE + "/api/state"), null, sc.variant, {});
        if(mine !== desktopGroups(state.models.filter(m => !m.kind || m.kind === "llm"))) problems.push("model grouping differs from fleet.html: re-port groupModels in padd.js");
      }
      if(JSON.stringify(posts) !== JSON.stringify(sc.posts || [])) problems.push(`POSTs sent ${JSON.stringify(posts)}, scene expects ${JSON.stringify(sc.posts || [])}`);
      if(missing.length) problems.push("no fixture for " + [...new Set(missing)].join(", "));
    }catch(e){ problems.push("scene failed: " + e.message.split("\n")[0]); }
    const file = path.join(OUT, sc.name + ".png");
    await page.screenshot({path: file}).catch(() => {});
    shots.push({name: sc.name, file}, ...extra.map(f => ({name: path.basename(f, ".png"), file: f})));
    console.log((problems.length ? "FAIL " : "ok   ") + sc.name + (problems.length ? "\n       " + [...new Set(problems)].join("\n       ") : ""));
    if(problems.length) failures.push(sc.name);
    await ctx.close();
  }
  /* contact sheet */
  if(shots.length){
    const ctx = await browser.newContext({viewport: {width: 1700, height: 900}, deviceScaleFactor: 1}), page = await ctx.newPage();
    await page.setContent(`<body style="margin:0;background:#18181a;font:600 15px system-ui,sans-serif;color:#ddd"><div style="display:grid;grid-template-columns:repeat(6,1fr);gap:18px;padding:18px">${
      shots.map(s => `<figure style="margin:0"><figcaption style="padding:0 0 6px">${s.name}</figcaption><img style="width:100%;display:block" src="data:image/png;base64,${fs.readFileSync(s.file).toString("base64")}"></figure>`).join("")}</div></body>`);
    await page.waitForTimeout(300);
    await page.screenshot({path: path.join(OUT, "_sheet.png"), fullPage: true});
    await ctx.close();
  }
  await browser.close();
  console.log(`\n${scenes.length - failures.length} of ${scenes.length} scenes pass. Pictures: ${OUT}`);
  process.exit(failures.length ? 1 : 0);
})();
