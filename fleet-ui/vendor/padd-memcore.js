/* ================= Starfleet PADD: Memory Core =================
   The desktop page's Memory Core (vendor/memcore.js and memcore.css, both untouched) on a phone. This file
   gives the component what it expects from fleet.html (compJSON, md, view, #wikiview, body.wiki), lays its
   three columns out for a narrow screen and adds the touch fixes. The component draws and wires its own
   DOM, so everything here is CSS under #wikiview plus a few listeners around it.
   Phone layout: the map fills the stage; the bar under it switches between Map (toolbar, map, replay bar),
   Page (the right column under a shorter map) and Sessions (the left column, likewise). */
(() => {
"use strict";
const P = window.PADD, $ = id => document.getElementById(id);
const HINT = "Pinch: zoom · Drag: move · Tap a page: its neighbourhood · Path to, then a second page: shortest path";
let wv = null, gate, bar, vw = "map", on = false, wired = false, refused = null, fail = "", lastPth = "", tap = null;

/* memcore.js fetches through this bare global. Keyed, and it never asks for the key itself: a refusal
   closes the component and the screen shows the key card (the test is padd.js's wantsKey). */
window.compJSON = async url => {
  try{ return await P.api.get(url, {prompt: false}); }
  catch(e){ if(e.status === 403 && /Starfleet (Command|key)/i.test(e.message) && !/Cannot verify/i.test(e.message)){ refused = P.key.get(); P.paint(); } throw e; }
};

function setView(v){
  if(v === vw) return;
  vw = wv.dataset.v = v;
  for(const b of bar.children){ b.classList.toggle("on", b.dataset.arg === v); b.setAttribute("aria-pressed", b.dataset.arg === v); }
  dispatchEvent(new Event("resize"));            /* memcore.js measures the map box again on resize */
}
/* moves the map without changing the zoom: a drag on the overlay canvas, as a finger would make it */
function pan(dy){
  const ov = $("mcov"), b = ov.getBoundingClientRect(), x = b.left + b.width / 2, y = b.top + b.height / 2;
  for(const [type, to] of [["pointerdown", y], ["pointermove", y + dy], ["pointerup", y + dy]])
    ov.dispatchEvent(new PointerEvent(type, {pointerId: 1e6, clientX: x, clientY: to, bubbles: true}));
}
/* runs when the component repaints its page card, probe card or meta line */
function sync(){
  if(!wv.isConnected) return;
  const pg = $("mcpage"), r = $("mcr"), pth = pg.style.display === "block" ? pg.querySelector(".pth").textContent : "", was = lastPth;
  lastPth = pth;
  $("p-mcnone").hidden = !!pth || $("mcprobe").style.display === "block";
  if(pth && pth !== was){                        /* another page was selected: show its card */
    const from = vw; setView("page"); P.haptic("select");
    r.scrollTop = pg.offsetTop - r.offsetTop - 14;
    /* The map is now the strip above the panel. Its centre stays its centre, so everything on it moved by
       the difference of the two centres: a page tapped outside the strip is pulled back into it. Under
       3 px the component would take the drag for a tap. */
    if(from === "map" && tap && performance.now() - tap.t < 500){
      const b = $("mcbox").getBoundingClientRect(), y = tap.y + b.top + b.height / 2 - tap.mid, to = Math.max(b.top + 40, Math.min(b.bottom - 40, y));
      if(Math.abs(to - y) >= 3) pan(to - y);
    }
  } else if(!pth && was && vw === "page") setView("map");   /* the card was closed */
  P.paint();
}
function wire(){                                 /* once, after the component built its DOM */
  wired = true;
  const q = $("mcq"), ov = $("mcov");
  for(const [k, v] of Object.entries({enterkeyhint: "search", autocapitalize: "off", autocorrect: "off", spellcheck: "false", "aria-label": "Probe terms or page"})) q.setAttribute(k, v);
  q.addEventListener("keydown", e => { if(e.key === "Enter") q.blur(); });
  $("mchint").textContent = HINT;                /* the desktop text speaks of scroll, click and Shift-click */
  $("mcr").insertAdjacentHTML("afterbegin", `<div class="p-empty" id="p-mcnone">Tap a page on the map to read about it here</div>`);
  const mo = new MutationObserver(sync);
  mo.observe($("mcpage"), {childList: true, attributes: true, attributeFilter: ["style"]});
  mo.observe($("mcprobe"), {attributes: true, attributeFilter: ["style"]});
  mo.observe($("mcmeta"), {childList: true, characterData: true, subtree: true});
  /* keyboard, rotation, replay bar: whatever resizes the map box, the canvases follow */
  new ResizeObserver(() => dispatchEvent(new Event("resize"))).observe($("mcbox"));
  /* a finger does not hover: the component's hover handlers would stick and can swallow the first tap */
  if(matchMedia("(hover: none)").matches) for(const ev of ["mouseenter", "mouseleave"]) wv.addEventListener(ev, e => e.stopPropagation(), true);
  wv.addEventListener("pointerup", e => { if(e.target === ov){ const b = ov.getBoundingClientRect(); tap = {y: e.clientY, mid: b.top + b.height / 2, t: performance.now()}; } }, true);
  /* a session or a search term is shown on the map: go there first, so the component fits it to the full map */
  wv.addEventListener("click", e => { if(e.target.closest("#mcchats .mci,#mcterms .mcterm")) setView("map"); }, true);
  sync();
}
async function open(){
  on = true;
  if(typeof md === "undefined") window.md = t => esc(t).replace(/\n/g, "<br>");   /* Computer's screen brings the real one */
  try{ Promise.resolve(P.mdReady && P.mdReady()).catch(() => {}); }catch(e){}
  try{ await P.load("/vendor/memcore.css"); await P.load("/vendor/memcore.js"); }
  catch(e){ on = false; fail = "Memory Core could not be loaded. Check the connection and try again."; P.paint(); return; }
  if(!on || view === "wiki") return;             /* left the screen while it loaded */
  view = "wiki"; document.body.classList.add("wiki");
  window.wikiOpen();
  if(!wired) wire();
}
function close(){
  if(!on) return;
  on = false;
  if(view === "wiki"){ const x = $("mcdx"); if(x) x.click(); window.wikiClose(); }
  view = ""; document.body.classList.remove("wiki");
}

P.screens["systems/core"] = { tab: "systems", parent: "#/systems", color: "blue", title: "Memory Core", stage: true,
  mount(el){
    if(!wv){                                     /* the component builds its DOM once: keep the element across visits */
      gate = Object.assign(document.createElement("div"), {className: "p-mcgate"});
      wv = Object.assign(document.createElement("div"), {id: "wikiview"}); wv.dataset.v = vw;
      bar = Object.assign(document.createElement("div"), {className: "p-seg p-mcbar", innerHTML: [["map", "Map"], ["page", "Page"], ["sessions", "Sessions"]]
        .map(([v, l]) => `<button type="button" data-act="mcview" data-arg="${v}" class="${v === vw ? "on" : ""}" aria-pressed="${v === vw}">${l}</button>`).join("")});
    }
    el.append(gate, wv, bar);
  },
  render(){
    const k = P.key.get(), ok = !!k && k !== refused && !fail;
    wv.hidden = bar.hidden = !ok; gate.hidden = ok;
    if(!ok) P.put(gate, fail ? `${P.h.msg(fail, true)}<button class="p-btn sm" type="button" data-act="mcretry">Try again</button>` : P.h.keygate("Memory Core"));
    if(ok !== on){ if(ok) open(); else close(); }
    const meta = ok && wired ? $("mcmeta").textContent.split(" · ") : [];
    P.hx(meta.length > 1 ? `<span class="p-sync p-mcmeta">${esc(meta.slice(0, 2).join(" · "))}${meta[2] ? "<br>" + esc(meta[2]) : ""}</span>`
      : ok && !(wired && $("mcmsg").textContent) ? `<span class="p-sync">Reading the wiki</span>` : "");
  },
  unmount(){ close(); } };
P.on("mcview", v => { P.haptic("select"); setView(v); });
P.on("mcretry", () => { fail = ""; P.paint(); });

P.css(`
.p-mcgate{padding:14px 10px 0}
.p-mcgate>.p-btn{margin-top:4px}
.p-mcbar{flex:none;margin:6px 10px 4px}
html.kb .p-mcbar{display:none}
.p-mcmeta{text-align:right;line-height:1.35}

#p-stage #wikiview{display:flex;flex-direction:column;flex:1;min-height:0;height:auto}
#wikiview>.top{display:none}
#wikiview .vmsg{display:none;flex:none;margin:6px 10px 0;padding:9px 12px;border-left:8px solid var(--amber);background:#1a1204;color:var(--amber);
  font:400 14px/1.35 var(--body);letter-spacing:0;-webkit-user-select:text;user-select:text}
#wikiview .vmsg.err{border-left-color:var(--red);background:#2a0d0d;color:#f0a9a9}

/* one column: the map on top, the two side columns share the row under it. They are hidden with
   visibility, never display: the component keeps measuring and painting them. */
#wikiview #mc{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) 0;gap:0;margin:0;flex:1;min-height:0}
#wikiview[data-v=page] #mc,#wikiview[data-v=sessions] #mc{grid-template-rows:minmax(0,46fr) minmax(0,54fr)}
#wikiview #mcc{grid-area:1/1;gap:0}
#wikiview #mc #mcl,#wikiview #mc #mcr{grid-area:2/1;display:flex;visibility:hidden;overflow:hidden}
#wikiview[data-v=sessions] #mc #mcl,#wikiview[data-v=page] #mc #mcr{visibility:visible;overflow-y:auto;overscroll-behavior:contain;
  -webkit-overflow-scrolling:touch;padding:0 10px 12px;border-top:4px solid var(--grid)}
#wikiview #mcl>*,#wikiview #mcr>*{flex:none;max-height:none;overflow:visible}
#wikiview:not([data-v=map]) #mctools,#wikiview:not([data-v=map]) #mctl,#wikiview:not([data-v=map]) #mclegend,
#wikiview:not([data-v=map]) #mczh,#wikiview:not([data-v=map]) #mczl{display:none}

/* toolbar: probe field, then the four switches */
#wikiview #mctools{gap:6px 4px;padding:6px 10px}
#wikiview #mctools input{flex:1 1 100%;width:auto;min-width:0;height:46px;padding:0 18px;border-radius:23px;background:var(--card);color:var(--text);
  font-size:16px;-webkit-appearance:none;appearance:none;-webkit-user-select:text;user-select:text}
#wikiview #mctools input::placeholder{color:var(--dim);opacity:1}
#wikiview .pill{flex:1 1 0;min-width:0;height:44px;padding:0 4px;border:0;border-radius:22px;background:var(--grid);color:var(--dim);
  font:700 14px var(--lcars);letter-spacing:.1em;text-transform:uppercase;white-space:nowrap}
#wikiview .pill.on{background:var(--orange);color:#000}
#wikiview #mcnet{border-radius:22px 0 0 22px} #wikiview #mccore{border-radius:0 22px 22px 0}
#wikiview #mctools .sep{flex:none;width:2px}
#wikiview #mctools .grow,#wikiview #mcnos{display:none!important}
/* "Wiki service stopped" is a chip with a tooltip on the desktop: a finger gets the reason itself */
#wikiview #mcnote{flex:1 1 100%;border:0;border-left:8px solid var(--amber);border-radius:0;padding:7px 12px;font-size:0;cursor:default}
#wikiview #mcnote::after{content:attr(title);font:400 13px/1.35 var(--body);letter-spacing:0;text-transform:none}

/* map: legend bottom left, zoom buttons as a column in thumb reach */
#wikiview #mcbox{flex:1;height:auto;min-height:0}
#wikiview #mccap{max-width:calc(100% - 8px)}
#wikiview #mccap>span{max-height:76px;overflow:hidden}
#wikiview #mclegend{display:flex;flex-wrap:wrap;white-space:normal;gap:0 10px;max-width:calc(100% - 62px);padding:4px 10px;line-height:1.5}
#wikiview #mczoom{flex-direction:column-reverse;gap:4px;padding:0;background:none;border-radius:0}
#wikiview .zb{width:44px;height:44px;min-width:0;margin:0;padding:0;border:0;border-radius:22px;background:var(--grid);color:var(--text);
  font:600 22px/1 var(--body);letter-spacing:0}
#wikiview #mczf{font:700 13px var(--lcars);letter-spacing:.08em;text-transform:uppercase}
#wikiview #mczl{width:44px;min-width:0;padding:2px 0;border-radius:9px;background:var(--ink);text-align:center}
#wikiview #mchint{left:6px;right:56px;bottom:6px;max-width:none;background:var(--ink);font-size:13px}
#wikiview #mchint.on{display:block}

/* replay bar: four controls, then one 44 px block per step, scrolling sideways */
#wikiview #mctl{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px;padding:6px 10px 0;min-height:0}
#wikiview #mcspeed,#wikiview #mcfollow{border-radius:0}
#wikiview #mcplay{border-radius:22px 0 0 22px} #wikiview #mcall{grid-area:1/4;border-radius:0 22px 22px 0}
#wikiview #mcsegs{grid-column:1/-1;height:44px;overflow-x:auto;scrollbar-width:none;counter-reset:mc}
#wikiview #mcsegs::-webkit-scrollbar{display:none}
#wikiview #mcsegs i{flex:1 0 44px;display:flex;align-items:center;justify-content:center;counter-increment:mc;outline:0;opacity:1;
  font:700 13px var(--lcars);color:#000}
#wikiview #mcsegs i::before{content:counter(mc,decimal-leading-zero)}
#wikiview #mcsegs i.fut{opacity:.28}
#wikiview #mcsegs i.hot{outline:2px solid #fff;outline-offset:-2px}
#wikiview #mcfunnel{display:none}
#wikiview #mctl .mcempty{grid-column:1/-1}

/* the two panels */
#wikiview .mch{font-size:15px;margin:12px 0 4px}
#wikiview #p-mcnone{margin-top:10px}
#wikiview .mcempty{flex:1 1 100%;padding:14px 12px;background:var(--card);border-left:8px solid var(--grid);font-size:14px;letter-spacing:.1em}
#wikiview #mcchats,#wikiview #mctop{gap:4px}
#wikiview .mci{display:flex;flex-direction:column;justify-content:center;min-height:58px;padding:8px 10px 8px 12px;border-left:8px solid var(--grid);
  background:var(--card);font-size:17px;font-weight:600;line-height:1.3}
#wikiview .mci.on{border-left-color:var(--orange);background:#1a1204}
#wikiview .mci small{margin-top:1px;font-size:12px;font-weight:400;line-height:1.3}
#wikiview .mci.pg{flex-direction:row;justify-content:flex-start;align-items:center;gap:10px;min-height:44px;padding:4px 10px 4px 12px;font-size:16px}
#wikiview .mci.pg b{min-width:26px;font-size:17px}
#wikiview #mcterms{gap:0 6px}
#wikiview .mcterm{height:44px;min-width:44px;padding:0 14px;border:solid transparent;border-width:4px 0;border-radius:22px;
  background:var(--grid) padding-box;color:var(--text);font-size:13px;letter-spacing:.1em}
#wikiview .mcterm b{color:var(--peach)}
#wikiview #mcprobe,#wikiview #mcpage{margin-top:10px;padding:10px 12px 12px}
#wikiview #mcr h3{flex-direction:column;align-items:stretch;gap:10px;font-size:20px;line-height:1.1;letter-spacing:.07em;overflow-wrap:anywhere}
#wikiview #mcprobe h3{flex-direction:row;align-items:center;font-size:15px}
#wikiview #mcr h3 span{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:4px}
#wikiview #mcr h3 button{height:44px;min-width:44px;padding:0 4px;border-radius:0;background:var(--grid);color:var(--text);font-size:13px;letter-spacing:.1em;
  overflow:hidden;text-overflow:ellipsis}
#wikiview #mcr h3 button.on{background:var(--orange);color:#000}
#wikiview #mcr h3 button:first-child{border-radius:22px 0 0 22px} #wikiview #mcr h3 button:last-child{border-radius:0 22px 22px 0}
#wikiview #mcr h3 button:only-child{border-radius:22px;padding:0 18px}
#wikiview #mcpage .pth{margin:8px 0 6px;font-size:12px;-webkit-user-select:text;user-select:text}
#wikiview #mcpage p,#wikiview #mcprobe p{font:400 14px/1.35 var(--body);letter-spacing:0;-webkit-user-select:text;user-select:text}
#wikiview #mcprobe p.d{font:400 12px/1.3 var(--lcars);letter-spacing:.08em}
#wikiview #mcpage .tg{gap:0 6px;margin:0}
#wikiview #mcpage .tg u{display:inline-flex;align-items:center;justify-content:center;height:44px;min-width:44px;padding:0 12px;border:solid transparent;
  border-width:7px 0;border-radius:22px;background:#1a1409 padding-box;color:var(--dim);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
#wikiview .kv span{padding:3px 10px;font-size:12px} #wikiview .kv b{font-size:14px}
#wikiview .nbh{margin:10px 0 2px;font-size:12px}
#wikiview .nb a{display:flex;align-items:center;flex-wrap:wrap;min-height:44px;padding:4px 0 4px 10px;color:var(--text);font-size:15px;line-height:1.25}
#wikiview .nb a small{font-size:11px}
#wikiview .nb small.mcempty{font-size:12px;padding:10px 12px}
#wikiview .mcs{grid-template-columns:34px 76px minmax(0,1fr) auto;min-height:48px;background:var(--card);font-size:14px}
#wikiview .mcs>b{background:var(--grid);color:var(--dim);font-size:13px}
#wikiview .mcs.hot{background:#1a1204} #wikiview .mcs.hot>b{background:var(--orange);color:#000}
#wikiview .mcs>span small,#wikiview .mcs>span u,#wikiview .mcs>em{font-size:11px}

/* Open: the page's markdown over the whole screen, below the status bar */
#wikiview #mcdoc{position:fixed;z-index:30;left:var(--sal);right:var(--sar);top:calc(var(--vvt) + var(--sat) + 4px);bottom:auto;width:auto;
  height:calc(var(--vvh) - var(--sat) - 4px);background:var(--ink);box-shadow:none}
#wikiview #mcdoc .hd{align-items:center;padding:10px 10px 0 12px}
#wikiview #mcdoc .hd b{font-size:22px;line-height:1.1;letter-spacing:.06em;-webkit-user-select:text;user-select:text}
#wikiview #mcdoc .hd button{flex:none;height:44px;padding:0 18px;border-radius:22px;background:var(--grid);color:var(--text);font-size:13px;letter-spacing:.1em}
#wikiview #mcdoc .pth{padding:6px 12px 8px;font-size:12px;-webkit-user-select:text;user-select:text}
#wikiview #mcdoc .cm{margin:0;max-width:none;padding:0 12px calc(var(--sab) + 20px);overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
#wikiview #mcdoc .body{padding:0;background:none;color:#f3e6d8;font:400 16px/1.5 var(--body);letter-spacing:0;overflow-wrap:anywhere;
  -webkit-user-select:text;user-select:text;-webkit-touch-callout:default}
#wikiview #mcdoc .body h1,#wikiview #mcdoc .body h2,#wikiview #mcdoc .body h3{margin:14px 0 4px;color:var(--orange);font:700 20px/1.2 var(--lcars);letter-spacing:.06em}
#wikiview #mcdoc .body h1{font-size:24px} #wikiview #mcdoc .body h3{font-size:17px}
#wikiview #mcdoc .body p{margin:4px 0 10px}
#wikiview #mcdoc .body ul,#wikiview #mcdoc .body ol{padding-left:22px}
/* a link in running text: 44 px to hit, the line keeps its height */
#wikiview #mcdoc .body a{display:inline-block;min-width:44px;margin:-13px 0;padding:13px 0;color:var(--blue);text-align:center}
#wikiview #mcdoc .body a.dead{color:var(--dim)}
#wikiview #mcdoc .body img{display:block;max-width:100%;margin:6px 0;border-radius:6px}
#wikiview #mcdoc .body pre{overflow:auto;padding:10px 12px;border-radius:8px;background:#0f1117;font-size:13px}
#wikiview #mcdoc .body pre code.hljs{padding:0;background:none}
#wikiview #mcdoc .body code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}
#wikiview #mcdoc .body :not(pre)>code{padding:1px 5px;border-radius:4px;background:#1d160d;color:var(--amber)}
#wikiview #mcdoc .body table{display:block;overflow-x:auto;margin:6px 0;border-collapse:collapse}
#wikiview #mcdoc .body th,#wikiview #mcdoc .body td{padding:4px 9px;border:1px solid #3a2c18;font-size:14px}
#wikiview #mcdoc .body th{color:var(--amber)}
#wikiview #mcdoc .fm{margin:0 0 4px;font:400 12px var(--lcars);letter-spacing:.08em}
#wikiview #mcdoc .fm summary{line-height:44px}
#wikiview #mcdoc .fm pre{margin:0 0 8px;white-space:pre-wrap;background:var(--card);color:var(--dim)}
#wikiview #mcdoc .st{color:var(--peach);font:600 13px var(--lcars);letter-spacing:.08em;text-transform:uppercase}
#wikiview #mcdoc .err{color:#f0a9a9}
`);
})();
