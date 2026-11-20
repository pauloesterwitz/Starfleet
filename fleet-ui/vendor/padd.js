/* ================= Starfleet PADD: core =================
   Fleet for a phone (padd.html). This file is the frame every screen stands on, plus the two screens that
   only need /api/state: Bridge and Models. The other screens live in padd-systems.js, padd-computer.js,
   padd-memcore.js and padd-command.js and register themselves here.

   What a screen file uses (all on window.PADD, short name P):
     P.screens["systems/nas"] = { tab, title, color, parent, stage, needs, mount(el,arg), render(el,arg), unmount() }
         tab     which tab is lit ("bridge" | "models" | "computer" | "systems" | "command")
         color   a token name (peach, blue, magenta, amber, mauve, orange, red) for the header
         parent  hash of the screen Back goes to; a screen without one is a tab root
         stage   true = fixed-height screen (chat, map): it gets #p-stage instead of the scrolling #p-view
         needs   { source: milliseconds } polled while the screen is open; render() runs after every answer
     P.data.<source>, P.at.<source> (ms), P.err.<source> ("key" = Starfleet key missing or refused)
         sources: state, containers, nas, nasstats, alpha, defiant, sessions, events; add one with P.sources.x = {url, key}
     P.events               Fleet's message list (see "messages" below); the app calls PADD.pushState, PADD.pushToken, PADD.open
     P.api.get(url, opt), P.api.post(url, body, opt)   JSON in, JSON out, throws Error with .status
         a 403 that means "key" opens the key sheet and retries once; opt.prompt:false turns that off
     P.put(el, html)        replace only when changed, never under a finger or a focused field
     P.on(name, fn)         click on any [data-act=name] calls fn(data-arg, element, event)
     P.go(hash), P.back(), P.paint(), P.refresh([sources])
     P.sheet({title, sub, color, body}) -> {el, body, set(html), close()}; P.confirm({...}) -> Promise<bool>
     P.toast(text, "err"), P.haptic(style), P.run(asyncFn) (errors become a toast)
     P.hx(html)             the header's right slot; P.css(text) adds a stylesheet; P.load(url) a script or css
     P.h.sec / row / tag / chip / gauge   markup helpers; esc, ago, dur, gb, mmss, kfmt are plain globals
   Rules: every tappable thing is a real <button> or <a> at least 44 px; class names start with p-;
   nothing here ever asks for the key in the background, only an action the user took does. */
"use strict";

var view = "";   /* vendor/memcore.js reads this bare global: "wiki" while its screen is open */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dur = s => { s = Math.max(0, s); return s < 90 ? Math.round(s) + " s" : s < 5400 ? Math.round(s / 60) + " min" : s < 172800 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d"; };
const ago = t => t ? dur(Date.now() / 1000 - t) + " ago" : "–";            /* t: epoch seconds */
const gb = kb => (kb / 1048576).toFixed(1);
const kfmt = n => !n ? "" : n % 1024 === 0 ? (n / 1024) + "K" : n >= 10000 ? Math.round(n / 1000) + "K" : String(n);
function mmss(s){ s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60), r = s % 60; return m ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`; }   /* fleet.html L1261 */

const PADD = window.PADD = (() => {
const P = { version: 1, screens: {}, data: {}, at: {}, err: {}, h: {} };
const $ = id => document.getElementById(id);
const LS = { get(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } },
             set(k, v){ try{ v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); }catch(e){} } };
P.ls = LS;
const clock = t => new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});

/* ---------------------------------------------------------------- the app around the page
   The iPhone app listens on webkit.messageHandlers.padd (haptics, edge to edge, links). Safari and an
   older app build have no such handler: every call is then a no-op and the page works the same. */
P.native = (type, data) => { try{ const h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.padd;
  if(h){ h.postMessage(Object.assign({type}, data || {})); return true; } }catch(e){} return false; };
P.haptic = style => P.native("haptic", {style: style || "light"});

/* ---------------------------------------------------------------- key and API */
const KEY = "starfleetKey";        /* the same store the desktop page uses */
P.key = { get: () => LS.get(KEY) || "", set: v => { LS.set(KEY, v || null); for(const k in P.err) if(P.err[k] === "key"){ P.err[k] = null; next[k] = 0; } } };
class ApiError extends Error { constructor(status, msg, quiet){ super(msg); this.status = status; this.quiet = !!quiet; } }
/* fleet.py's POLICY text and ctxproxy's own refusal both mean "ask for the key". "Cannot verify ... right
   now" is the gate not answering, and "Authorization code rejected" is Auto Destruct's code: neither is. */
const wantsKey = msg => /Starfleet (Command|key)/i.test(msg) && !/Cannot verify|code/i.test(msg);
async function call(method, url, body, opt){
  opt = opt || {};
  const keyed = opt.key != null ? opt.key : (method !== "GET" || url.startsWith("/api/computer/"));
  const headers = {}, k = P.key.get();
  if(body !== undefined && !opt.raw) headers["Content-Type"] = "application/json";
  if(opt.type) headers["Content-Type"] = opt.type;
  if(keyed && k) headers["X-Starfleet-Key"] = k;
  const ctl = new AbortController(), tm = opt.timeout === 0 ? null : setTimeout(() => ctl.abort(), opt.timeout || 30000);
  let r;
  try{ r = await fetch(url, {method, headers, signal: ctl.signal, body: body === undefined ? undefined : opt.raw ? body : JSON.stringify(body)}); }
  catch(e){ throw new ApiError(0, ctl.signal.aborted ? "Fleet did not answer in time." : "No contact with Fleet. Is Tailscale connected?"); }
  finally{ clearTimeout(tm); }
  let j = null; try{ j = await r.json(); }catch(e){}
  if(r.ok) return j;
  const er = j && (j.error || j.message), msg = String((er && er.message) || er || "Fleet answered " + r.status);
  if(r.status === 403 && keyed && wantsKey(msg) && opt.prompt !== false && !opt._again){
    if(await P.needKey(k ? "Fleet did not accept the stored key." : "")) return call(method, url, body, Object.assign({}, opt, {_again: true}));
    throw new ApiError(403, "Starfleet key needed.", true);
  }
  throw new ApiError(r.status, msg);
}
P.api = { get: (url, opt) => call("GET", url, undefined, opt), post: (url, body, opt) => call("POST", url, body === undefined ? {} : body, opt), ApiError };
P.run = async fn => { try{ return await fn(); }catch(e){ if(!(e && e.quiet)){ P.toast((e && e.message) || String(e), "err"); P.haptic("error"); } } };

/* ---------------------------------------------------------------- polling
   Only what the open screen needs, on a 1 s tick, paused while the page is hidden. A failing source backs
   off; a keyed source without an accepted key waits for a new key instead of asking for one. */
P.sources = { state: {url: "/api/state", timeout: 8000}, containers: {url: "/api/containers"}, nas: {url: "/api/nas"},
  nasstats: {url: "/api/nas/stats"}, alpha: {url: "/api/memoryalpha"}, defiant: {url: "/api/defiant"},
  sessions: {url: "/api/computer/sessions", key: true}, events: {url: "/api/events", timeout: 8000} };
const BASE = {state: 6000, sessions: 30000, alpha: 120000, defiant: 120000, events: 15000};   /* every screen: the header's status and the tab figures */
let needs = Object.assign({}, BASE);
const inflight = {}, next = {}, fails = {};
async function pull(k){
  const s = P.sources[k]; if(!s || inflight[k]) return;
  if(s.key && !P.key.get()){ P.err[k] = "key"; return; }
  inflight[k] = true;
  try{ P.data[k] = await call("GET", s.url, undefined, {prompt: false, timeout: s.timeout || 30000, key: !!s.key});
       P.at[k] = Date.now(); P.err[k] = null; fails[k] = 0; if(k === "events") gotEvents(); }
  catch(e){ P.err[k] = e.status === 403 && s.key && wantsKey(e.message) ? "key" : (e.message || "failed"); fails[k] = (fails[k] || 0) + 1; }
  inflight[k] = false;
  const base = needs[k] || 6000;
  next[k] = P.err[k] === "key" ? Infinity : Date.now() + (fails[k] ? Math.max(base, Math.min(30000, base * Math.pow(2, Math.min(3, fails[k])))) : base);
  P.paint();
}
function tick(){
  if(document.hidden) return;
  for(const k in needs) if(Date.now() >= (next[k] || 0)) pull(k);
  if((P.data.state && (P.data.state.loading || []).length) || P.err.state) P.paint();   /* elapsed time counts up between polls */
}
P.refresh = keys => { for(const k of keys || Object.keys(needs)){ if(next[k] !== Infinity || P.key.get()) next[k] = 0; } tick(); };

/* ---------------------------------------------------------------- drawing */
let ptr = null; const pending = new Map();
function flush(){ for(const [el, h] of [...pending]){ pending.delete(el); P.put(el, h); } }
addEventListener("pointerdown", e => { ptr = e.target; }, true);
for(const ev of ["pointerup", "pointercancel"]) addEventListener(ev, () => { ptr = null; if(pending.size) setTimeout(flush, 250); }, true);
addEventListener("focusout", () => { if(pending.size) setTimeout(flush, 250); }, true);
/* ponytail: a changed list is replaced whole. Keyed rows would be finer; add them if a list ever flickers. */
P.put = (el, html) => {
  if(!el || el._h === html) return;
  const a = document.activeElement;
  if((ptr && el.contains(ptr)) || (a && a !== document.body && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))){ pending.set(el, html); return; }
  el._h = html; el.innerHTML = html;
};
const CHEV = '<svg class="p-chev" viewBox="0 0 10 16" aria-hidden="true"><path d="M2 2l6 6-6 6" fill="none" stroke="#a4855f" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const col = c => !c ? "" : `--c:${/^[a-z]+$/.test(c) ? `var(--${c})` : c}`;
const H = P.h;
H.chev = CHEV;
H.sec = (t, small) => `<div class="p-sec"><i></i><h2>${esc(t)}</h2><u></u>${small != null && small !== "" ? `<small>${esc(small)}</small>` : ""}</div>`;
H.tag = (t, cls) => `<span class="p-tag ${cls || ""}">${esc(t)}</span>`;
/* o.wrap: title and lines wrap, s reads as a sentence and can be selected; a wrap row without an action is a plain div (a message) */
H.row = o => { const div = o.wrap && !o.act, el = div ? "div" : "button";
  return `<${el} class="p-row${o.tall ? " tall" : ""}${o.wrap ? " wrap" : ""}"${div ? "" : ` type="button" data-act="${o.act || ""}" data-arg="${esc(o.arg ?? "")}"`} style="${col(o.c)}">
  <span class="p-m"><span class="p-t${o.big ? " big" : ""}${o.wrap ? " wrap" : ""}">${esc(o.t)}${o.badge ? `<span class="p-badge">${esc(o.badge)}</span>` : ""}</span>${
    o.s ? `<span class="p-s${o.wrap ? " wrap txt" : ""}">${esc(o.s)}</span>` : ""}${o.s2 ? `<span class="p-s${o.wrap ? " wrap" : ""}">${esc(o.s2)}</span>` : ""}${
    o.e ? `<span class="p-s" style="color:var(--red)">${esc(o.e)}</span>` : ""}</span>${o.tag ? H.tag(o.tag, o.tagc) : ""}${o.chev ? CHEV : ""}</${el}>`; };
H.chip = o => `<button class="p-chip${o.on ? " on" : ""}${o.cls ? " " + o.cls : ""}" type="button" data-act="${o.act}" data-arg="${esc(o.arg ?? "")}" aria-pressed="${!!(o.on || o.cls === "set")}"${o.off ? " disabled" : ""}><span>${esc(o.label)}${o.n != null ? `<b>${o.n}</b>` : ""}</span></button>`;
H.gauge = (pct, n) => { n = n || 16; const on = Math.max(0, Math.min(n, Math.round(pct / 100 * n)));
  return `<div class="p-gauge" role="img" aria-label="${Math.round(pct)} percent">${Array.from({length: n}, (_, i) => `<i${i < on ? ' class="on"' : ""}></i>`).join("")}</div>`; };
H.msg = (t, err) => t ? `<div class="p-msg${err ? " err" : ""} p-sel">${esc(t)}</div>` : "";
/* what a keyed screen shows instead of its data while there is no accepted key */
H.keygate = what => `<div class="p-card" style="--c:var(--orange)"><h3>Starfleet key</h3><p>${esc(what)} needs the Starfleet key.</p>
  <div class="p-pair" style="margin-top:11px"><button class="p-btn sm" type="button" data-act="key">Enter key</button></div></div>`;

P.css = text => { const s = document.createElement("style"); s.textContent = text; document.head.appendChild(s); };
P.css(`.p-row.wrap .p-t,.p-row.wrap .p-s{overflow-wrap:anywhere;-webkit-user-select:text;user-select:text}
.p-row.wrap .p-s.txt{font:400 14px/1.35 var(--body);letter-spacing:0;text-transform:none;color:var(--text)}`);
const loaded = {};
P.load = url => loaded[url] || (loaded[url] = new Promise((ok, bad) => {
  const css = /\.css$/.test(url), el = document.createElement(css ? "link" : "script");
  if(css){ el.rel = "stylesheet"; el.href = url; } else el.src = url;
  el.onload = () => ok(); el.onerror = () => { delete loaded[url]; bad(new Error("Could not load " + url)); };
  document.head.appendChild(el); }));

const acts = {};
P.on = (name, fn) => { acts[name] = fn; };
addEventListener("click", e => {
  const el = e.target.closest && e.target.closest("[data-act]");
  if(!el || el.disabled) return;
  const fn = acts[el.dataset.act];
  if(fn){ e.preventDefault(); fn(el.dataset.arg || "", el, e); }
});
addEventListener("touchstart", () => {}, {passive: true});   /* iOS: without a touch listener :active never shows */

P.toast = (text, kind) => {
  const el = document.createElement("div"); el.className = "p-toast p-sel" + (kind ? " " + kind : ""); el.textContent = text;
  $("p-toast").appendChild(el); setTimeout(() => el.remove(), kind === "err" ? 9000 : 5000);
};

/* ---------------------------------------------------------------- sheets
   Bottom sheets stand in for confirm(), alert() and the desktop's two-click "Sure?". They are not history
   entries: a route change closes them. Drag the top down, tap outside, or Close. */
const sheets = [];
P.sheet = o => {
  const scrim = document.createElement("div"), el = document.createElement("section");
  scrim.className = "p-scrim"; el.className = "p-sheet"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", o.title || "");
  el.style.cssText = col(o.color || "blue"); scrim.style.zIndex = 40 + sheets.length * 2; el.style.zIndex = 41 + sheets.length * 2;
  el.innerHTML = `<div class="p-grabzone"><div class="p-grab"></div></div>
    <div class="p-shd"><div class="p-m"><h3 class="p-sel">${esc(o.title || "")}</h3>${o.sub ? `<div class="p-s p-sel">${esc(o.sub)}</div>` : ""}</div>
      <button class="p-x" type="button" aria-label="Close"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="#ffcc99" stroke-width="2.4" stroke-linecap="round"/></svg></button></div>
    <div class="p-shb"></div>`;
  const body = el.querySelector(".p-shb"), s = { el, body, open: true, set: html => P.put(body, html),
    close(result){ if(!s.open) return; s.open = false; sheets.splice(sheets.indexOf(s), 1);
      removeEventListener("pointermove", move); removeEventListener("pointerup", up); removeEventListener("pointercancel", up);
      el.classList.remove("on"); scrim.classList.remove("on"); if(!sheets.length) document.documentElement.classList.remove("p-locked");
      setTimeout(() => { el.remove(); scrim.remove(); }, 260); if(o.onClose) o.onClose(result); } };
  scrim.onclick = () => s.close(); el.querySelector(".p-x").onclick = () => s.close();
  let y0 = null, dy = 0;
  const down = e => { if(e.target.closest(".p-x")) return; y0 = e.clientY; dy = 0; el.classList.add("drag"); };
  const move = e => { if(y0 == null) return; dy = Math.max(0, e.clientY - y0); el.style.transform = `translateY(${dy}px)`; };
  const up = () => { if(y0 == null) return; y0 = null; el.classList.remove("drag"); el.style.transform = ""; if(dy > 90) s.close(); };
  for(const z of el.querySelectorAll(".p-grabzone,.p-shd")){ z.addEventListener("pointerdown", down); }
  addEventListener("pointermove", move); addEventListener("pointerup", up); addEventListener("pointercancel", up);
  $("p-sheets").append(scrim, el); sheets.push(s); document.documentElement.classList.add("p-locked");
  if(o.body != null) s.set(o.body);
  requestAnimationFrame(() => requestAnimationFrame(() => { el.classList.add("on"); scrim.classList.add("on"); }));
  P.haptic("light");
  return s;
};
P.closeSheets = () => { for(const s of [...sheets]) s.close(); };
/* confirm: the confirming button sits at the bottom, in thumb reach */
P.confirm = o => new Promise(done => {
  const s = P.sheet({title: o.title, sub: o.sub, color: o.color || (o.danger ? "red" : "orange"), onClose: r => done(r === true),
    body: `${o.text ? `<p class="p-text p-sel">${esc(o.text)}</p>` : ""}<div class="p-pair p-acts">
      <button class="p-btn ghost" type="button" data-k="no">${esc(o.no || "Cancel")}</button>
      <button class="p-btn${o.danger ? " red" : ""}" type="button" data-k="yes">${esc(o.yes || "Confirm")}</button></div>`});
  const t0 = Date.now();      /* a double tap on the button that opened this sheet must not confirm it while it slides in */
  s.body.addEventListener("click", e => { const b = e.target.closest("[data-k]"); if(b && !(b.dataset.k === "yes" && Date.now() - t0 < 450)) s.close(b.dataset.k === "yes"); });
});

/* The Starfleet key, asked for when an action needs it. It is checked against Computer's model list, a
   keyed GET that changes nothing, and kept where the desktop page keeps it: this origin's localStorage. */
let keyAsk = null;
P.needKey = why => keyAsk || (keyAsk = new Promise(done => {
  let ok = false;
  const s = P.sheet({title: "Starfleet key", color: "orange", onClose: () => { keyAsk = null; done(ok); },
    body: `<p class="p-text">${esc(why || "This needs the Starfleet key.")} It stays on this device.</p>
      <label class="p-fieldbox" style="margin-top:12px"><input class="p-field" id="p-keyin" type="password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Starfleet key" aria-label="Starfleet key"></label>
      <div id="p-keymsg"></div>
      <div class="p-pair p-acts"><button class="p-btn" type="button" id="p-keygo">Save key</button></div>`});
  const input = s.body.querySelector("#p-keyin"), go = s.body.querySelector("#p-keygo"), msg = s.body.querySelector("#p-keymsg");
  const save = async () => {
    const v = input.value.trim(); if(!v) return;
    go.disabled = true; go.textContent = "Checking...";
    const old = P.key.get(); LS.set(KEY, v);
    try{ await call("GET", "/api/computer/models", undefined, {prompt: false, timeout: 12000}); ok = true; P.key.set(v); P.haptic("success"); s.close(); P.refresh(); }
    catch(e){ LS.set(KEY, old || null); go.disabled = false; go.textContent = "Save key";
      msg.innerHTML = H.msg(e.status === 403 && wantsKey(e.message) ? "Fleet did not accept this key." : e.message, true); P.haptic("error"); }
  };
  go.onclick = save; input.addEventListener("keydown", e => { if(e.key === "Enter") save(); });
}));
P.on("key", () => { P.needKey(""); });

/* ---------------------------------------------------------------- model logic, ported from fleet.html
   Same rules as the desktop registry, so a row means the same thing on both. When fleet.html changes one of
   these, change it here too (fleet.py keeps a third copy of modelName). Source lines as of 2026-11-19. */
const isImg = m => !!(m.kind && m.kind !== "llm");                         /* L699 */
const isRerank = m => /rerank/i.test(m.id), isEmbed = m => /embed/i.test(m.id);   /* L707 */
const KINDLBL = {image: "Image", video: "Video", judge: "Visual QA"};
const TAG = {"jean-luc": "Jean-Luc", "kathryn": "Kathryn", "dynamic": "Auto"};   /* L673 */
const TPS_IN_NAME = /-\d+(?:\.\d+)?tps(?=-|$)/;                             /* L729 */
function modelName(id){ return id.replace(/-(jean-luc|kathryn|fastest-node|starfleet)$/, "").replace(/-tp2(?=-|$)/, "").replace(TPS_IN_NAME, ""); }
function familyBase(m){ if(isImg(m) || m.nodes.length !== 1 || /-tp2(?=-|$)|-starfleet$/.test(m.id)) return null; return modelName(m.id); }
const MACHINE_ORDER = ["jean-luc", "kathryn", "dynamic"];
/* L775: one logical model deployed per node is several llama-swap members; they fold into one row with a
   machine picker. filter and machineSel are parameters here (globals on the desktop). */
function groupModels(models, run, filter, machineSel){
  const byBase = {}, out = [];
  for(const m of models){ const base = familyBase(m); if(base){ (byBase[base] = byBase[base] || []).push(m); } else out.push(m); }
  for(const base in byBase){
    const variants = byBase[base], byMachine = {}; let ambiguous = false;
    for(const v of variants){ if(byMachine[v.nodes[0]]) ambiguous = true; else byMachine[v.nodes[0]] = v; }
    if(ambiguous || variants.length < 2){ out.push(...variants); continue; }
    const pick = byMachine[filter] ? filter : byMachine[machineSel[base]] ? machineSel[base]
      : variants.find(v => run[v.id]) ? variants.find(v => run[v.id]).nodes[0]
      : variants.find(v => v.pinned) ? variants.find(v => v.pinned).nodes[0] : MACHINE_ORDER.find(n => byMachine[n]);
    machineSel[base] = pick;
    const speeds = variants.map(v => v.tps).filter(t => typeof t === "number");
    const tps = speeds.length ? Math.round(speeds.reduce((a, t) => a + t, 0) / speeds.length * 10) / 10 : null;
    out.push(Object.assign({}, byMachine[pick], {tps, _base: base, _family: MACHINE_ORDER.filter(n => byMachine[n])}));
  }
  return out;
}
function keep(m, run, filter, query){                                       /* L915 */
  if(query && !(m.id + " " + modelName(m.id)).toLowerCase().includes(query)) return false;
  if(filter === "image") return isImg(m);
  if(isImg(m)) return false;
  switch(filter){
    case "all": return true;
    case "live": return !!run[m.id];
    case "pinned": return m.pinned;
    case "vision": return m.vision === "on";
    case "starfleet": return m.nodes.length > 1;
    case "embed": return isEmbed(m);
    case "rerank": return isRerank(m);
    default: return m.nodes.includes(filter) && m.nodes.length === 1;
  }
}
const CTX_LADDER = [8192, 16384, 32768, 65536, 131072, 262144];            /* L965 */
function ctxTitle(m){
  if(!m.ctx_max) return "Maximum not measured yet.";
  const parts = [`Max ${m.ctx_max} tokens`];
  if(m.ctx_native && m.ctx_pool && m.ctx_pool < m.ctx_native) parts.push(`limited by the measured KV pool (checkpoint allows ${m.ctx_native})`);
  else if(m.ctx_pool) parts.push(`checkpoint max; KV pool holds ${m.ctx_pool}`);
  else if(m.ctx_native && m.ctx_deployed && m.ctx_deployed < m.ctx_native) parts.push(`capped at the launcher's configured ${m.ctx_deployed} until one load has measured the KV pool (checkpoint allows ${m.ctx_native})`);
  else parts.push("checkpoint max; KV pool not measured yet");
  return parts.join(", ") + ".";
}
function ctxOptions(m){                                                     /* L988, as [value, label, selected] */
  const cur = m.ctx, max = m.ctx_max, opts = [["", "Default"]];
  for(const v of CTX_LADDER){ if(max && v >= max) break; opts.push([String(v), v % 1024 === 0 ? (v / 1024) + "K" : String(v)]); }
  if(max) opts.push(["max", `Max ${kfmt(max)}`]);
  if(cur && cur !== max && !opts.some(([v]) => v === String(cur))) opts.push([String(cur), String(cur)]);
  const sel = cur == null ? "" : (max && cur === max ? "max" : String(cur));
  return opts.map(([v, l]) => [v, l, v === sel]);
}
function effortOptions(m){                                                  /* L1006 */
  const cur = m.effort || "Default", opts = (m.effort_options || []).slice();
  if(cur !== "Default" && !opts.includes(cur)) opts.push(cur);
  return opts.map(v => [v === "Default" ? "" : v, v, v === cur]);
}
P.model = { isImg, isEmbed, isRerank, modelName, familyBase, groupModels, keep, ctxOptions, effortOptions, TAG, KINDLBL };
(function selfCheck(){                                                      /* fleet.html L811, the cases that matter here */
  const llm = (id, node, tps) => ({id, kind: "llm", nodes: [node], pinned: false, tps: tps ?? null});
  const g = groupModels([llm("qwen3.6-35b-85tps-mtp4-jean-luc", "jean-luc", 85), llm("qwen3.6-35b-87tps-mtp4-kathryn", "kathryn", 87),
    llm("qwen3.6-35b-86tps-mtp4-fastest-node", "dynamic", 86), llm("nomic-embed-text", "jean-luc"), llm("nomic-embed-text-kathryn", "kathryn"),
    llm("qwen3.8-27b-19tps-jean-luc", "jean-luc"), llm("qwen3.8-27b-vision-jean-luc", "jean-luc"), llm("deepseek-v4-flash", "jean-luc"),
    {id: "qwen38fn-sglang-tp2-starfleet", kind: "llm", nodes: ["jean-luc", "kathryn"], pinned: false}], {}, "all", {});
  const fam = b => g.find(m => m._base === b);
  console.assert(fam("qwen3.6-35b-mtp4") && fam("qwen3.6-35b-mtp4")._family.join() === "jean-luc,kathryn,dynamic", "groupModels: per-node tps must not split a family");
  console.assert(fam("nomic-embed-text") && fam("nomic-embed-text")._family.join() === "jean-luc,kathryn", "groupModels: an unsuffixed member is Jean-Luc's half");
  console.assert(fam("qwen3.6-35b-mtp4").tps === 86 && fam("nomic-embed-text").tps === null, "groupModels: family speed is the average, unmeasured stays blank");
  console.assert(g.length === 6 && g.some(m => m.id === "qwen38fn-sglang-tp2-starfleet" && !m._family), "groupModels: 9 members are 2 families and 4 lone rows");
  console.assert(modelName("qwen38fn-long-sglang-tp2-starfleet") === "qwen38fn-long-sglang", "modelName: no node, TP=2 marker or tps");
})();

/* ---------------------------------------------------------------- status
   Nominal, Attention, Fault: the desktop's words (fleet.html vstate). Never from memory use: a pinned model
   fills a Spark on purpose. Fault = something is not answering; Attention = something wants a look. */
const STALL_SHOW = 24 * 3600;
P.sessionsOf = () => { const out = [], d = P.data.sessions;
  for(const [node, v] of Object.entries((d && d.nodes) || {})) for(const s of (v && v.sessions) || []) if(s.updated_secs_ago < STALL_SHOW) out.push(Object.assign({node}, s));
  return out; };
P.status = () => {
  const s = P.data.state, items = [];
  if(!s) return {level: P.err.state ? "sync" : "wait", items};
  if(P.err.state) items.push({lv: 2, t: "No contact with Fleet", s: P.err.state});
  if(s.errors && s.errors._swap) items.push({lv: 2, t: "llama-swap is not answering", s: s.errors._swap, go: "#/models"});
  for(const [n, v] of Object.entries(s.nodes || {})) if(!v || v.error) items.push({lv: 2, t: `${TAG[n] || n} is not answering`, s: (v && v.error) || "Probing", go: "#/bridge"});
  for(const m of s.models || []) if(m.error) items.push({lv: 1, t: `${modelName(m.id)} reported an error`, s: m.error, go: "#/models"});
  const stalled = P.sessionsOf().filter(x => x.stalled);
  if(stalled.length) items.push({lv: 1, t: `${stalled.length} session${stalled.length > 1 ? "s" : ""} stalled`, sys: 1,
    s: [...new Set(stalled.map(x => TAG[x.node] || x.node))].join(", ") + " · waiting for " + dur(Math.max(...stalled.map(x => x.updated_secs_ago))), go: "#/systems/sessions"});
  const a = P.data.alpha;
  if(a && (a.status === "warn" || a.status === "error")) items.push({lv: a.status === "error" ? 2 : 1, sys: 1, t: "Memory Alpha", s: [...(a.errors || []), ...(a.problems || [])][0] || "Backup needs a look", go: "#/systems/alpha"});
  const d = P.data.defiant;
  if(d && d.configured !== false){
    if(d.status === "error" || d.status === "warn") items.push({lv: d.status === "error" ? 2 : 1, sys: 1, t: "Defiant", s: [...(d.errors || []), ...(d.problems || [])][0] || "Web server needs a look", go: "#/systems/defiant"});
    else if(d.reboot_required) items.push({lv: 1, sys: 1, t: "Defiant needs a reboot", s: `Web server${d.updates != null ? ` · ${d.updates} updates` : ""}`, go: "#/systems/defiant"});
    for(const x of d.sites || []) if(x.cert_days != null && x.cert_days < 14 && d.status === "ok") items.push({lv: 1, sys: 1, t: `Certificate ends in ${x.cert_days} d`, s: x.domain, go: "#/systems/defiant"});
  }
  return {level: P.err.state ? "sync" : ["nominal", "attention", "fault"][Math.max(0, ...items.map(i => i.lv))], items};
};
const STAT = {wait: ["Standby", "wait"], sync: ["Sync lost", "fault"], nominal: ["Nominal", ""], attention: ["Attention", "warn"], fault: ["Fault", "fault"]};
const itemRow = i => H.row({act: i.go ? "go" : "", arg: i.go || "", c: i.lv > 1 ? "red" : "amber", t: i.t, s: i.s, chev: !!i.go});
P.on("status", () => {
  const st = P.status();
  P.sheet({title: STAT[st.level][0], color: st.level === "nominal" ? "blue" : st.level === "attention" ? "amber" : st.level === "wait" ? "mauve" : "red",
    sub: P.at.state ? "Last answer from Fleet " + clock(P.at.state) : "No answer from Fleet yet",
    body: `<div class="p-list" style="margin:12px 0 4px">${st.items.length ? st.items.map(itemRow).join("") : `<div class="p-empty">All systems nominal</div>`}</div>`});
});

/* ---------------------------------------------------------------- messages
   Fleet's list of what happened (GET /api/events, no key). Unread: an act or notice line, opened or one-off, newer than
   the id in paddMsgSeen; a resolved or info line never counts. An episode is open while Fleet lists it AND its newest
   line is still "open". Act now = the open act episodes; that number is also the app icon's badge. */
const SEEN = "paddMsgSeen";
const EV = P.events = {list: [], latest: 0, open: new Set(), newest: {}, ended: {}, keys: new Set(), seen: 0, unread: 0, act: 0, base: null};
const upish = e => (e.level === "act" || e.level === "notice") && (e.state === "open" || e.state === "point");
const epOpen = e => !!e.episode && EV.open.has(e.episode) && (EV.newest[e.episode] || e).state === "open";
EV.isNew = e => e.id > EV.seen && upish(e);
EV.live = e => e.state === "open" && epOpen(e) && EV.newest[e.episode] === e;
/* An episode key is used again when a problem comes back, so "open" also needs the line to be newer than the last
   "resolved" line of its episode (EV.ended). An opening line the episode has ended since reads as recovered. */
const lineOpen = e => epOpen(e) && e.id > (EV.ended[e.episode] || 0);
EV.edge = e => e.state === "resolved" ? "blue" : e.state === "open" ? (!lineOpen(e) ? "blue" : e.level === "act" ? "red" : "amber") : e.level === "act" ? "red" : e.level === "notice" ? "amber" : "";
EV.tag = e => { const c = e.level === "act" ? "fault" : "warn";
  return e.state === "resolved" ? ["Recovered", "ready"] : e.state === "open" ? (!lineOpen(e) ? ["Was open", ""] : [e.reminder ? "Reminder " + e.reminder : "Open", c])
    : e.level === "info" ? null : ["Note", c]; };
function derive(){
  const d = P.data.events; if(!d || !Array.isArray(d.events)) return false;
  EV.list = d.events; EV.open = new Set(d.open || []); EV.newest = {}; EV.ended = {}; EV.keys = new Set(); EV.unread = EV.act = 0;
  EV.latest = EV.list.reduce((m, e) => Math.max(m, e.id), d.latest || 0);
  for(const e of EV.list) if(e.episode){ EV.newest[e.episode] = e; if(e.state === "resolved") EV.ended[e.episode] = Math.max(e.id, EV.ended[e.episode] || 0); }
  EV.seen = Number(LS.get(SEEN)) || 0;
  if(EV.seen > EV.latest){ EV.seen = 0; LS.set(SEEN, "0"); }      /* Fleet's list started over */
  for(const e of EV.list){ if(EV.isNew(e)){ EV.unread++; EV.keys.add(e.episode || "#" + e.id); } if(EV.live(e) && e.level === "act") EV.act++; }
  return true;
}
EV.read = () => { LS.set(SEEN, String(EV.latest)); derive(); P.paint(); };
let badge = null;
const short = t => { t = String(t || ""); return t.length > 110 ? t.slice(0, 107).trimEnd() + "..." : t; };
/* The first answer of a session is history: it only sets the baseline. Later, a new act or notice line shows a toast and a
   haptic. A page that was hidden toasts when it comes back and the next answer is the first one it sees. */
function gotEvents(){
  try{
    if(!derive()) return;
    if(EV.base == null) EV.base = EV.latest;
    else if(!document.hidden){
      const fresh = EV.list.filter(e => e.id > EV.base && upish(e)), n = fresh[fresh.length - 1], act = fresh.some(e => e.level === "act");
      EV.base = Math.max(EV.base, EV.latest);
      if(n){ P.toast(fresh.length > 1 ? `${fresh.length} new messages. Newest: ${n.title}` : `${n.title}: ${short(n.text)}`, act ? "err" : ""); P.haptic(act ? "warning" : "light"); }
    }
    if(EV.act !== badge){ badge = EV.act; P.native("badge", {n: EV.act}); }
  }catch(e){ console.error(e); }
}

/* ---------------------------------------------------------------- push: the page's side of the app bridge
   The app calls these three through evaluateJavaScript. The device token is registered with Fleet once per page
   load (PU.done); Fleet's register route is idempotent, and a stored copy would hide a device Fleet has dropped since
   (Apple answered 410), so paddPushToken is only written, never read to skip a registration. A registration the user
   did not just ask for never opens the key sheet. PU.old: the app has not answered "push-status" in 3 seconds. */
const PUSHKEY = "paddPushToken";
const PU = P.push = {state: null, token: "", done: "", env: "development", user: 0, err: "", old: false};
let askT = 0;
PU.ask = () => {
  if(!P.native("push-status")) return false;
  if(!PU.state && !PU.old && !askT) askT = setTimeout(() => { askT = 0; if(!PU.state){ PU.old = true; P.paint(); } }, 3000);
  return true;
};
P.pushState = s => { s = s || {}; clearTimeout(askT); askT = 0; PU.old = false;
  PU.state = {permission: s.permission, registered: !!s.registered, error: String(s.error || "")}; if(s.registered) PU.off = false; P.paint(); };
let regging = "";
PU.sync = async () => {
  const t = PU.token; if(!t || PU.done === t || regging === t) return;
  regging = t;
  try{ await P.api.post("/api/computer/push/register", {token: t, env: PU.env}, {prompt: Date.now() - PU.user < 120000}); PU.done = t; LS.set(PUSHKEY, t); PU.err = ""; }
  catch(e){ PU.err = e.status === 403 ? "Fleet needs the Starfleet key to register this iPhone." : e.message; }
  regging = ""; P.paint();
};
P.pushToken = (token, env) => {
  token = String(token || "").toLowerCase(); if(!/^[0-9a-f]{8,}$/.test(token)) return;
  PU.token = token; PU.env = env === "production" ? "production" : "development"; return PU.sync();
};
/* a tapped notification: only a route of this page */
P.open = hash => { if(typeof hash !== "string" || !/^#\/[\w./%-]*$/.test(hash)) return false; P.go(hash); P.refresh(["events"]); return true; };

/* ---------------------------------------------------------------- frame: header, tabs, routes */
const TABS = [
  {id: "bridge", label: "Bridge", color: "peach", fig(){ const n = Object.values((P.data.state || {}).nodes || {}); return n.length ? n.filter(v => v && !v.error).length + "/" + n.length : ""; }},
  {id: "models", label: "Models", color: "blue", fig(){ const s = P.data.state; return s ? String((s.running || []).length) : ""; }},
  {id: "computer", label: "Computer", color: "magenta", fig: () => ""},
  {id: "systems", label: "Systems", color: "amber", fig(){ const n = P.status().items.filter(i => i.sys).length; return n ? String(n) : ""; }},
  {id: "command", label: "Command", color: "mauve", fig: () => EV.unread ? (EV.unread > 9 ? "9+" : String(EV.unread)) : ""},
];
P.tabs = TABS;
let cur = null, curId = "", curArg = "", curEl = null;
const scrolls = {};
function parse(){ const h = location.hash.replace(/^#\/?/, ""), parts = h.split("/").filter(Boolean);
  for(let n = parts.length; n > 0; n--){ const id = parts.slice(0, n).join("/"); if(P.screens[id]) return [id, parts.slice(n).map(decodeURIComponent).join("/")]; }
  return [parts[0] && TABS.some(t => t.id === parts[0]) ? parts[0] : "bridge", ""]; }
function route(){
  const [id, arg] = parse();
  if(cur && curId === id && curArg === arg) return;
  pending.clear();
  if(cur){ scrolls[curId + "/" + curArg] = scrollY; try{ if(cur.unmount) cur.unmount(); }catch(e){ console.error(e); } }
  P.closeSheets();
  const sc = P.screens[id] || { tab: id, title: (TABS.find(t => t.id === id) || {}).label || id, color: (TABS.find(t => t.id === id) || {}).color,
    mount(el){ el.innerHTML = `<div class="p-empty" style="margin-top:14px">This screen is not available.</div>`; } };
  cur = sc; curId = id; curArg = arg;
  const tab = TABS.find(t => t.id === sc.tab) || TABS[0], stage = !!sc.stage;
  document.documentElement.classList.toggle("p-stage", stage);
  $("p-hd").style.cssText = col(sc.color || tab.color);
  $("p-title").textContent = typeof sc.title === "function" ? sc.title(arg) : sc.title;
  $("p-back").hidden = !sc.parent; $("p-back").style.color = `var(--${sc.color || tab.color})`;
  P.hx("");
  $("p-stage").hidden = !stage;
  curEl = stage ? $("p-stage") : $("p-view");
  for(const el of [$("p-view"), $("p-stage")]){ el._h = null; el.innerHTML = ""; }
  needs = Object.assign({}, BASE, sc.needs || {});
  for(const k in BASE) if(sc.needs && sc.needs[k]) needs[k] = Math.min(BASE[k], sc.needs[k]);
  try{ if(sc.mount) sc.mount(curEl, arg); }catch(e){ console.error(e); curEl.innerHTML = H.msg("This screen could not be drawn: " + e.message, true); }
  LS.set("paddRoute", JSON.stringify({h: location.hash, t: Date.now()}));
  drawNow(); tick();
  if(!stage) scrollTo(0, scrolls[id + "/" + arg] || 0);
}
P.hx = html => { P.put($("p-hx"), html || ""); };
const trail = [];      /* hashes this page pushed, so Back can use history instead of piling up entries */
P.go = (hash, replace) => { if(location.hash === hash){ P.closeSheets(); route(); return; }
  if(replace) location.replace(hash); else { trail.push(location.hash); location.hash = hash; } };
P.back = () => { if(!cur || !cur.parent) return;
  if(trail.length && trail[trail.length - 1] === cur.parent){ trail.pop(); history.back(); } else P.go(cur.parent, true); };
P.on("go", arg => { P.haptic("select"); P.go(arg); });
P.on("back", () => { P.haptic("select"); P.back(); });
/* tabs replace the history entry (a back swipe never walks through tabs); the lit tab goes to its root,
   then to the top */
P.on("tab", id => { P.haptic("select"); trail.length = 0;
  const root = "#/" + id;
  if(cur && cur.tab === id && location.hash === root) scrollTo({top: 0, behavior: "smooth"}); else P.go(root, true); });
function drawTabs(){
  const lit = cur && cur.tab;
  P.put($("p-tabs"), TABS.map(t => `<button class="p-tab" type="button" data-act="tab" data-arg="${t.id}" style="${col(t.color)}"${t.id === lit ? ' aria-current="page"' : ""}><b>${esc(t.fig())}</b><span>${t.label}</span></button>`).join(""));
}
function drawNow(){
  const st = P.status(), [word, cls] = STAT[st.level], pill = $("p-stat");
  pill.textContent = word; pill.className = "p-stat " + cls;
  drawTabs();
  if(cur && cur.render){ try{ cur.render(curEl, curArg); }catch(e){ console.error(e); } }
  if(msheet && msheet.open) drawModel();
}
let raf = 0;
P.paint = () => { if(!raf) raf = requestAnimationFrame(() => { raf = 0; drawNow(); }); };
P.syncText = () => P.err.state ? (P.at.state ? "Last " + clock(P.at.state) : "No answer") : P.at.state ? "Synced " + clock(P.at.state) : "Contacting Fleet";

/* the visual viewport: height, offset and keyboard as CSS variables (padd.css) */
/* Two kinds of host: one leaves the layout viewport alone and the keyboard covers its lower part (Safari, the
   app edge to edge), so fixed things must rise by --kb; the other shrinks the whole web view (the app with the
   page inside the safe area), so nothing is covered but the screen is short. Both hide the tab bar. */
let tallest = 0, wide = 0;
function viewport(){
  const vv = window.visualViewport, d = document.documentElement.style;
  const h = vv ? vv.height : innerHeight, top = vv ? vv.offsetTop : 0, kb = Math.max(0, innerHeight - h - top);
  if(innerWidth !== wide){ wide = innerWidth; tallest = 0; }           /* rotated: forget the old height */
  tallest = Math.max(tallest, innerHeight);
  d.setProperty("--vvh", h + "px"); d.setProperty("--vvt", top + "px"); d.setProperty("--kb", kb + "px");
  document.documentElement.classList.toggle("kb", kb > 80 || tallest - innerHeight > 150);
}
P._test = { kb(px){ const d = document.documentElement; d.style.setProperty("--kb", px + "px"); d.style.setProperty("--vvh", (innerHeight - px) + "px"); d.classList.toggle("kb", px > 80); } };

P.boot = () => {
  if(history.scrollRestoration) history.scrollRestoration = "manual";
  if(window.visualViewport){ visualViewport.addEventListener("resize", viewport); visualViewport.addEventListener("scroll", viewport); }
  addEventListener("resize", viewport); viewport();
  /* the app's pull to refresh reloads "/" and drops the hash: come back to where the user was */
  const old = {nas: "#/systems/nas", ctr: "#/systems/engineering", ma: "#/systems/alpha", def: "#/systems/defiant", comp: "#/computer", wiki: "#/systems/core"};
  const bare = location.hash.replace(/^#\/?/, "");
  if(old[bare]) history.replaceState(null, "", old[bare]);
  else if(FILTERS_KEYS.includes(bare)){ filter = bare; history.replaceState(null, "", "#/models"); }
  if(!location.hash || location.hash === "#"){ let h = "#/bridge";
    try{ const r = JSON.parse(LS.get("paddRoute") || "null"); if(r && r.h && Date.now() - r.t < 30 * 60000) h = r.h; }catch(e){}
    history.replaceState(null, "", h); }
  addEventListener("hashchange", () => { if(trail.length && location.hash === trail[trail.length - 1]) trail.pop(); route(); });
  document.addEventListener("visibilitychange", () => { if(!document.hidden) P.refresh(); });
  setInterval(tick, 1000);
  route();
  P.native("ready", {edgeToEdge: true});
  PU.ask();
};

/* ================================================================ Bridge */
const edgeOf = (m, st) => m.error ? "red" : st === "loading" ? "peach" : st === "queued" ? "amber" : m.pinned ? "magenta" : st !== "stopped" ? "blue" : "";
const busy = new Set(), machineSel = {};
let filter = "all", query = "";
const FILTERS_KEYS = ["jean-luc", "kathryn", "starfleet", "dynamic", "live", "pinned", "vision", "embed", "rerank", "image"];
function world(){
  const s = P.data.state || {models: [], running: [], nodes: {}}, run = {}, qpos = {};
  (s.running || []).forEach(r => run[r.model] = r);
  (s.queued || []).forEach((id, i) => { qpos[id] = i + 1; });
  const loading = {}; (s.loading || []).forEach(l => loading[l.model] = l);
  /* which member a family row shows: the desktop looks at running and pinned members only, so a member
     that is loading on the other machine would hide behind a "stopped" row. Loading and queued count too,
     and only a machine the user picked stays picked (groupModels writes its choice into the map it gets). */
  const active = Object.assign({}, qpos, loading, run);
  const groups = f => groupModels(s.models || [], active, f, Object.assign({}, machineSel));
  const stOf = m => isImg(m) ? (busy.has(m.id) ? "loading" : qpos[m.id] ? "queued" : m.loaded_at ? "ready" : "stopped")
    : loading[m.id] ? "loading" : busy.has(m.id) ? "working" : qpos[m.id] ? "queued" : (run[m.id] ? run[m.id].state : "stopped");
  return {s, run, qpos, loading, stOf, groups};
}
const place = (m, live) => isImg(m) ? (m.nodes || []).map(n => TAG[n] || n).join(", ") : m.nodes.length > 1 ? "Starfleet TP2"
  : m._family && !live ? m._family.map(n => TAG[n] || n).join(" / ") : (TAG[m.nodes[0]] || m.nodes[0] || "");
function loadLine(l, drift){            /* fleet.html L1272: the live projection wins once there is 4 % to divide by */
  const t = l.elapsed + drift, p = l.progress, live = p >= .04 ? t * (1 - p) / p : null, left = live != null ? live : (l.eta ? l.eta - t : null);
  const over = left != null && left <= 0, det = p != null || !!l.eta;
  const pct = Math.round(p != null ? Math.max(1, Math.min(99, p * 100)) : (l.eta ? Math.min(99, t / l.eta * 100) : 0));
  const sub = p != null ? `${l.phase || "Loading"} · ${left != null ? `about ${mmss(left)} left` : "measuring"}`
    : !l.eta ? "First recorded load · no estimate yet" : over ? "Taking longer than usual" : `About ${mmss(left)} left`;
  return {t, pct, det, over, sub};
}
function modelTag(m, st, w){
  if(st === "loading"){ const l = w.loading[m.id]; return [l && (l.progress != null || l.eta) ? "Loading " + loadLine(l, 0).pct + "%" : "Loading", "load"]; }
  if(st === "queued") return ["Queued " + (w.qpos[m.id] || ""), "warn"];
  if(st === "working") return ["Working", "pinned"];
  if(st === "stopped") return m.pinned ? ["Pinned", "pinned"] : ["Stopped", ""];
  if(st === "ready") return m.pinned ? ["Pinned", "pinned"] : [isImg(m) ? "Loaded" : "Ready", "ready"];
  return [st, "warn"];           /* starting, stopping, ... */
}
function modelRow(m, w){
  const st = w.stOf(m), r = w.run[m.id], [tag, tagc] = modelTag(m, st, w);
  if(isImg(m)) return H.row({act: "model", arg: m.id, c: edgeOf(m, st), t: m.id, tag, tagc,
    s: [KINDLBL[m.kind] || m.kind, m.need_mb ? (m.need_mb / 1024).toFixed(1) + " GB" : "", place(m)].filter(Boolean).join(" · "), e: m.error});
  const ctx = m.ctx || m.ctx_deployed;
  const both = m._family && (w.s.models || []).some(x => x.id !== m.id && familyBase(x) === m._base && w.run[x.id]);
  return H.row({act: "model", arg: m._base || m.id, c: edgeOf(m, st), t: modelName(m.id), tag, tagc, e: m.error,
    badge: isEmbed(m) ? (m.vision === "on" ? "Multimodal" : "Text") : m.vision ? (m.vision === "on" ? "IMG" : "IMG off") : "",
    s: [both ? "Jean-Luc + Kathryn" : place(m, st !== "stopped"), m.tps ? m.tps + " t/s" : "", ctx ? kfmt(ctx) : "", r && r.ttl ? "Unloads in " + Math.round(r.ttl / 60) + " min" : ""].filter(Boolean).join(" · ")});
}
function nodeCard(name, n){
  if(!n || n.error) return `<button class="p-node" type="button" data-act="status" style="--c:var(--red)"><h3>${esc(TAG[name] || name)}</h3>
    <div class="p-free" style="font-size:19px;color:var(--red)">Not answering</div><div class="p-of p-sel" style="white-space:normal">${esc((n && n.error) || "Probing")}</div></button>`;
  const used = n.mem_total_kb - n.mem_avail_kb, pct = used / n.mem_total_kb * 100, v = x => x == null || x === "" ? "–" : x;
  return `<button class="p-node" type="button" data-act="go" data-arg="#/systems/engineering/${esc(name)}">
    <h3>${esc(n.host || TAG[name] || name)}</h3>
    <div class="p-free">${gb(n.mem_avail_kb)}<small>GB free</small></div>${H.gauge(pct)}
    <div class="p-of">${gb(used)} of ${gb(n.mem_total_kb)} GB used</div>
    <div class="p-kv"><div><b>${esc(v(n.gpu_util))}%</b><span>GPU</span></div><div><b>${esc(v(n.gpu_temp))}°</b><span>Temp</span></div><div><b>${esc(v(n.gpu_power != null && n.gpu_power !== "" ? Math.round(n.gpu_power) : null))} W</b><span>Power</span></div></div>
    <div class="p-of">Load ${esc(String(v(n.load)).split(" ")[0])} · ${(n.containers || []).length} containers</div></button>`;
}
/* News: the newest three messages that are unread or still open (one row per episode), then the way into the list.
   Nothing at all while Fleet has no messages. */
function newsHtml(){
  if(!EV.list.length) return "";
  const rows = [], used = new Set();
  for(let i = EV.list.length - 1; i >= 0 && rows.length < 3; i--){
    const e = EV.list[i], k = e.episode || "#" + e.id;
    if(used.has(k)) continue;
    used.add(k);
    if(EV.live(e) || EV.keys.has(k)) rows.push(H.row({wrap: true, act: "go", arg: e.go && e.go !== "#/bridge" ? e.go : "#/command/messages", c: EV.edge(e), t: e.title, s: e.text, s2: ago(e.at), chev: true}));
  }
  rows.push(H.row({act: "go", arg: "#/command/messages", c: "mauve", t: "All messages", tag: EV.unread ? EV.unread + " unread" : "", tagc: "warn", chev: true}));
  return `${H.sec("News")}<div class="p-list">${rows.join("")}</div>`;
}
P.screens.bridge = { tab: "bridge", title: "Bridge", color: "peach", needs: {state: 3000, sessions: 15000, alpha: 120000, defiant: 120000},
  render(el){
    P.hx(`<span class="p-sync">${esc(P.syncText())}</span>`);
    const s = P.data.state;
    if(!s){ P.put(el, P.err.state ? H.msg(P.err.state, true) : `<div class="p-empty" style="margin-top:14px">Contacting Fleet...</div>`); return; }
    const w = world(), names = Object.keys(s.nodes || {}), up = names.filter(k => s.nodes[k] && !s.nodes[k].error).length;
    const drift = (Date.now() - (P.at.state || Date.now())) / 1000;
    const g = w.groups("all");
    const res = g.filter(m => w.stOf(m) !== "stopped" && w.stOf(m) !== "loading" && w.stOf(m) !== "queued");
    const st = P.status(), order = ["jean-luc", "kathryn", ...names.filter(k => k !== "jean-luc" && k !== "kathryn")].filter(k => k in s.nodes);
    P.put(el, `${s.errors && s.errors._swap ? H.msg(s.errors._swap, true) : ""}
      ${H.sec("Node status", names.length ? `${up} of ${names.length} up` : "")}
      <div class="p-nodes">${order.map(k => nodeCard(k, s.nodes[k])).join("")}</div>
      ${(s.loading || []).map(l => { const x = loadLine(l, drift); return `<div class="p-load${x.over ? " over" : ""}">
        <div class="p-m"><div class="p-t">${esc(modelName(l.model))}</div><div class="p-track${x.det ? "" : " indet"}"><i style="${x.det ? `width:${x.pct}%` : ""}"></i></div>
        <div class="p-s wrap">${esc(x.sub)} · ${mmss(x.t)} elapsed</div></div>
        <button class="p-btn sm ghost" type="button" data-act="cancel" data-arg="${esc(l.model)}"${busy.has(l.model) ? " disabled" : ""}>Cancel</button></div>`; }).join("")}
      ${(s.queued || []).length ? `<div class="p-s wrap" style="margin-top:8px;color:var(--amber)">${(s.llm_loading || []).length ? `LLM loading (${s.llm_loading.map(modelName).map(esc).join(", ")}) · ` : ""}Queue, one at a time: ${s.queued.map((id, i) => `${i + 1}. ${esc(modelName(id))}`).join(" · ")}</div>` : ""}
      ${H.sec("Resident", res.length)}
      <div class="p-list">${res.length ? res.map(m => modelRow(m, w)).join("") : `<div class="p-empty">No model loaded</div>`}</div>
      ${newsHtml()}
      ${H.sec("Needs attention", st.items.length || "")}
      <div class="p-list">${st.items.length ? st.items.map(itemRow).join("") : `<div class="p-empty">All systems nominal</div>`}</div>`);
  } };

/* ================================================================ Models */
const FILTERS = [["all", "All"], ["live", "Resident"], ["pinned", "Pinned"], ["jean-luc", "Jean-Luc"], ["kathryn", "Kathryn"], ["starfleet", "TP2"],
  ["dynamic", "Auto"], ["vision", "Vision"], ["embed", "Embeddings"], ["rerank", "Rerankers"], ["image", "Image"]];
const RANK = {loading: 0, working: 0, queued: 1, stopped: 3};
P.screens.models = { tab: "models", title: "Models", color: "blue", needs: {state: 3000},
  mount(el){
    el.innerHTML = `<label class="p-search" style="margin-top:6px"><svg viewBox="0 0 18 18" aria-hidden="true"><circle cx="7.5" cy="7.5" r="5.5" fill="none" stroke="#a4855f" stroke-width="2.2"/><path d="M12 12l4.5 4.5" stroke="#a4855f" stroke-width="2.2" stroke-linecap="round"/></svg>
      <input id="p-q" type="search" enterkeyhint="search" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Search models" aria-label="Search models"></label>
      <div class="p-chips" id="p-mf" style="margin-top:4px"></div><div id="p-ml" style="margin-top:2px"></div>`;
    const q = el.querySelector("#p-q"); q.value = query;
    q.oninput = () => { query = q.value.trim().toLowerCase(); P.paint(); };
    q.onkeydown = e => { if(e.key === "Enter") q.blur(); };
  },
  render(el){
    const s = P.data.state, w = world(), free = Object.values(s ? s.nodes || {} : {}).filter(n => n && !n.error).reduce((a, n) => a + n.mem_avail_kb, 0);
    P.hx(`<span class="p-sync">${s ? gb(free) + " GB free" : esc(P.syncText())}</span>`);
    if(!s){ P.put(el.querySelector("#p-ml"), P.err.state ? H.msg(P.err.state, true) : `<div class="p-empty">Contacting Fleet...</div>`); return; }
    const g = w.groups(filter), llm = g.filter(m => !isImg(m));
    const count = f => f === "image" ? g.filter(isImg).length : w.groups(f).filter(m => !isImg(m) && keep(m, w.run, f, "")).length;
    P.put(el.querySelector("#p-mf"), FILTERS.map(([f, l]) => H.chip({act: "mfilter", arg: f, label: l, on: f === filter, n: count(f)})).join(""));
    const shown = g.filter(m => keep(m, w.run, filter, query)).sort((a, c) => {
      const ra = RANK[w.stOf(a)] ?? 2, rc = RANK[w.stOf(c)] ?? 2;
      return (ra - rc) || modelName(a.id).localeCompare(modelName(c.id), undefined, {numeric: true, sensitivity: "base"}) || a.id.localeCompare(c.id); });
    P.put(el.querySelector("#p-ml"), `${s.errors && s.errors._swap ? H.msg(s.errors._swap, true) : ""}<div class="p-list">${
      shown.length ? shown.map(m => modelRow(m, w)).join("") : `<div class="p-empty">No models match</div>`}</div>`);
  } };
P.on("mfilter", f => { filter = f; P.haptic("select"); P.paint(); });

/* ---- the model sheet: everything the desktop row does, with room for a thumb */
let msheet = null, mid = "";
const findModel = () => { const s = P.data.state; if(!s) return null; const w = world();
  const g = w.groups(cur === P.screens.models && ["jean-luc", "kathryn", "dynamic"].includes(filter) ? filter : "all");
  return g.find(m => (m._base || m.id) === mid) || g.find(m => m.id === mid) || null; };
function drawModel(){
  const m = findModel(), w = world();
  if(!m){ msheet.set(`<div class="p-empty" style="margin-top:12px">This model is no longer in the registry.</div>`); return; }
  const st = w.stOf(m), r = w.run[m.id], off = busy.has(m.id), [tag, tagc] = modelTag(m, st, w), live = !!r;
  const dis = off ? " disabled" : "";
  let html = `<div class="p-wrap" style="gap:6px;margin-top:8px">${H.tag(st === "ready" ? (isImg(m) ? "Loaded" : "Ready") : tag, st === "ready" ? "ready" : tagc)}${
    m.pinned ? H.tag("Pinned", "pinned") : ""}${!isImg(m) && m.vision ? H.tag(m.vision === "on" ? "Vision on" : "Vision off") : ""}${
    isImg(m) ? H.tag(KINDLBL[m.kind] || m.kind) : ""}</div>${H.msg(m.error, true)}`;
  if(isImg(m)){
    const nodes = m.img_nodes || ["jean-luc", "kathryn"], loaded = !!m.loaded_at, q = w.qpos[m.id];
    html += `<div class="p-read"><div><b>${m.need_mb ? (m.need_mb / 1024).toFixed(1) + " GB" : "–"}</b><span>Needs</span></div>
      <div><b>${esc(place(m) || "–")}</b><span>Runs on</span></div><div><b>${loaded ? esc(ago(m.loaded_at)) : "–"}</b><span>Loaded</span></div></div>
      ${m.note ? `<p class="p-note p-sel">${esc(m.note)}</p>` : ""}
      <div class="p-lab">Machine</div><div class="p-seg">${nodes.map(n => `<button type="button" class="${m.nodes.includes(n) ? "on" : ""}" data-act="imgnode" data-arg="${n}"${dis}>${esc(TAG[n] || n)}</button>`).join("")}</div>
      <p class="p-note">Picking a machine moves the model: it is warmed there, and renders route there.</p>
      <div class="p-pair p-acts">${loaded || q ? `<button class="p-btn red" type="button" data-act="imgunload"${dis}>${q && !loaded ? "Dequeue" : "Unload"}</button>` : ""}
        ${!loaded ? `<button class="p-btn" type="button" data-act="imgload"${dis}>Load</button>` : ""}</div>
      ${loaded ? `<p class="p-note">Unload frees every image model on that machine.</p>` : ""}`;
    msheet.set(html); return;
  }
  const ctx = m.ctx || m.ctx_deployed, fam = m._family;
  html += `<div class="p-read"><div><b>${m.tps ? m.tps + " t/s" : "–"}</b><span>Speed</span></div>
    <div><b>${ctx ? kfmt(ctx) : "Default"}</b><span>Context</span></div><div><b>${r && r.ttl ? Math.round(r.ttl / 60) + " min" : "–"}</b><span>Unloads in</span></div></div>
    <div class="p-lab">Machine</div><div class="p-seg">${fam
      ? fam.map(n => `<button type="button" class="${n === m.nodes[0] ? "on" : ""}" data-act="machine" data-arg="${n}">${esc(TAG[n] || n)}</button>`).join("")
      : `<button type="button" class="on" disabled>${m.nodes.length > 1 ? "Starfleet TP2 · both Sparks" : esc(TAG[m.nodes[0]] || m.nodes[0] || "")}</button>`}</div>`;
  if(m.ctx_capable) html += `<div class="p-lab">Context</div><div class="p-wrap">${ctxOptions(m).map(([v, l, on]) => H.chip({act: "ctx", arg: v, label: l, cls: on ? "set" : "", off})).join("")}</div>
    <p class="p-note" style="margin-top:2px">${esc(ctxTitle(m))}</p>`;
  if((m.effort_options || []).length) html += `<div class="p-lab">Thinking effort</div><div class="p-wrap">${effortOptions(m).map(([v, l, on]) => H.chip({act: "effort", arg: v, label: l, cls: on ? "set" : "", off})).join("")}</div>`;
  if(live && (m.ctx_capable || (m.effort_options || []).length)) html += `<p class="p-note">Context and effort are set when a model starts. Changing one unloads this model; it applies at the next load.</p>`;
  const q = w.qpos[m.id]; let acts = "", note = "";
  if(st === "loading"){ acts = `<button class="p-btn red" type="button" data-act="cancel" data-arg="${esc(m.id)}"${dis}>Cancel load</button>`; }
  else if(q && !live){ acts = `<button class="p-btn red" type="button" data-act="unload"${dis}>Dequeue</button>`; note = `Number ${q} in the load queue.`; }
  else if(live){ acts = `<button class="p-btn red" type="button" data-act="unload"${dis}>Unload</button>` + (m.pinned
      ? `<button class="p-btn ghost" type="button" data-act="pin" data-arg="0"${dis}>Unpin</button>` : `<button class="p-btn" type="button" data-act="pin" data-arg="1"${dis}>Pin</button>`);
    note = m.pinned ? "Pinned: Fleet keeps it resident. Unload also drops the pin." : "Not pinned: it unloads by itself when its time is up."; }
  else if(m.pinned){ acts = `<button class="p-btn ghost" type="button" data-act="pin" data-arg="0"${dis}>Unpin</button><button class="p-btn" type="button" data-act="pin" data-arg="1"${dis}>Load</button>`;
    note = "Pinned but not running. Fleet loads it only when you press Load."; }
  else { acts = `<button class="p-btn" type="button" data-act="pin" data-arg="1"${dis}>Load and pin</button>`;
    note = `Loads on ${m.nodes.length > 1 ? "both Sparks" : esc(TAG[m.nodes[0]] || m.nodes[0])} and keeps it resident until you unpin it.`; }
  html += `<div class="p-pair p-acts">${acts}</div>${note ? `<p class="p-note">${note}</p>` : ""}`;
  msheet.set(html);
}
P.on("model", id => { mid = id; const m = findModel(); if(!m) return;
  msheet = P.sheet({title: isImg(m) ? m.id : modelName(m.id), sub: !isImg(m) && modelName(m.id) !== m.id ? m.id : "", color: m.pinned ? "magenta" : "blue", onClose: () => { msheet = null; }});
  drawModel(); });
/* one action on the sheet's model: busy while it runs, a toast when it fails, a fresh poll after */
async function act(fn){
  const m = findModel(); if(!m || busy.has(m.id)) return;
  busy.add(m.id); P.paint(); if(msheet) drawModel();
  await P.run(() => fn(m, world()));
  busy.delete(m.id); P.refresh(["state"]); P.paint(); if(msheet && msheet.open) drawModel();
}
P.on("machine", node => { const m = findModel(); if(!m || !m._base) return; machineSel[m._base] = node; P.haptic("select");
  if(["jean-luc", "kathryn", "dynamic"].includes(filter)) filter = node;        /* a node filter outranks the pick: follow it (fleet.html L754) */
  const n = findModel(); if(n && msheet){ const sub = msheet.el.querySelector(".p-shd .p-s"); if(sub) sub.textContent = n.id; } P.paint(); drawModel(); });
P.on("pin", v => act(async m => { await P.api.post("/api/pin", {model: m.id, pin: v === "1"}); P.haptic("success"); }));
/* Unload drops the pin and dequeues BEFORE fleet.py checks the key, so a resident model's key is checked first,
   with a keyed GET that changes nothing. A pinned model or one on both Sparks asks first: it takes minutes to come back. */
P.on("unload", () => act(async (m, w) => {
  if(w.run[m.id]){
    if((m.pinned || m.nodes.length > 1) && !await P.confirm({title: "Unload this model?", sub: modelName(m.id), danger: true, yes: "Unload", no: "Keep it",
      text: `${m.pinned ? "Unloading also drops the pin. " : ""}${m.nodes.length > 1 ? "It runs on both Sparks. " : ""}Loading it again takes minutes.`})) return;
    await P.api.get("/api/computer/models");
  }
  const j = await P.api.post("/api/unload", {model: m.id}); P.haptic("success");
  if(j && j.freed_mb != null) P.toast(`${modelName(m.id)} unloaded: ${j.freed_mb} MB freed.`); }));
P.on("cancel", id => P.run(async () => {
  if(!await P.confirm({title: "Cancel this load?", sub: modelName(id), danger: true, yes: "Cancel load", no: "Keep loading",
    text: "The engine is stopped where it stands and the model is unpinned, so it is not loaded again straight away. Anything already loaded is discarded."})) return;
  const w = world();     /* the load may have finished while the sheet was open: cancelling then would evict a working model */
  if(!w.loading[id] && !w.qpos[id]){ P.toast("Already loaded or gone: nothing was cancelled."); P.refresh(["state"]); return; }
  await P.api.get("/api/computer/models");      /* the key first: cancelling unpins before the key is checked */
  busy.add(id); P.paint();
  try{ await P.api.post("/api/cancel", {model: id}); P.haptic("success"); } finally{ busy.delete(id); P.refresh(["state"]); }
}));
/* Context and effort are load-time settings. On a resident model fleet.py stores the value, then unloads,
   and the model stays down. It also returns early when the value is already stored, so "403, key, retry"
   would do nothing: the key is checked first, with a keyed GET that changes nothing. */
async function loadTime(m, w, what, post){
  if(w.run[m.id]){
    if(!await P.confirm({title: `Change ${what}?`, sub: modelName(m.id), yes: "Change and unload", danger: true,
      text: `${what[0].toUpperCase() + what.slice(1)} is set when a model starts. Fleet stores the new value and unloads this model now; it applies at the next load.`})) return;
    await P.api.get("/api/computer/models");
  }
  const j = await post(); P.haptic("success");
  if(j && j.reloaded) P.toast(`${modelName(m.id)} unloaded. Load it again to use the new ${what}.`);
}
P.on("ctx", v => act((m, w) => { if(v === "max" && !m.ctx_max) throw new Error("This model's maximum has not been measured yet.");
  return loadTime(m, w, "context", () => P.api.post("/api/ctx", {model: m.id, ctx: v === "" ? null : v === "max" ? m.ctx_max : Number(v)})); }));
P.on("effort", v => act((m, w) => loadTime(m, w, "thinking effort", () => P.api.post("/api/effort", {model: m.id, effort: v || null}))));
const imgSay = j => { if(j && j.message && (j.warmed === false || j.ok === false)) P.toast(j.message, "err"); else P.haptic("success"); };
P.on("imgnode", node => act(async m => imgSay(await P.api.post("/api/imgnode", {model: m.id, node}, {timeout: 0}))));
P.on("imgload", () => act(async m => imgSay(await P.api.post("/api/imgnode", {model: m.id, node: (m.nodes && m.nodes[0]) || "kathryn"}, {timeout: 0}))));
P.on("imgunload", () => act(async m => imgSay(await P.api.post("/api/imgunload", {model: m.id}, {timeout: 0}))));

return P;
})();
