/* ================= Starfleet PADD: Computer =================
   Chat with the model that is already resident; Computer never loads one. Ported from fleet.html
   L1782-1960: the same requests, the same stream events, and the same storage keys as the desktop page
   ("computerModel", "computerTools"), so both pages keep one choice.
   This file also owns the markdown path: the globals md() and compHL() (Memory Core's reader calls them)
   and P.mdReady(), which loads marked, DOMPurify and highlight.js on first use.
   The stage's nodes are built once and re-attached on every mount: a draft, the log and an answer that is
   still streaming survive a visit to another tab. The open chat is in the address (#/computer/<id>). */
(() => {
"use strict";
const P = window.PADD, H = P.h, LS = P.ls, MN = P.model.modelName;

P.css(`
.p-cgate{flex:none;padding:0 10px}
.p-cgate>*{margin-top:6px}
.p-clog{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;padding:0 10px}
.p-chat{min-height:calc(100% + 1px);display:flex;flex-direction:column;justify-content:flex-end;gap:12px;padding:8px 0 10px}
.p-chint:not(:only-child){display:none}
.p-mu{align-self:flex-end;max-width:84%;padding:10px 14px;border-radius:18px 18px 4px 18px;background:#1d160d;color:var(--text)}
.p-ma{border-left:4px solid var(--blue);padding:1px 0 1px 12px;color:#f3e6d8}
.p-ma:empty,.p-ctools:empty,.p-cthumbs:empty,.p-catt:empty,.p-tool span:empty{display:none}
.p-mu,.p-ma{min-width:0;font:400 16px/1.45 var(--body);letter-spacing:0;overflow-wrap:anywhere}
:is(.p-mu,.p-ma)>*{margin:0 0 8px}
:is(.p-mu,.p-ma)>:last-child{margin-bottom:0}
:is(.p-mu,.p-ma) :is(h1,h2,h3,h4){margin-top:12px;font:700 19px/1.2 var(--lcars);letter-spacing:.06em;color:var(--orange)}
:is(.p-mu,.p-ma) :is(ul,ol){padding-left:22px}
/* a link in running text: the padding makes it 44 px to hit without moving a line */
:is(.p-mu,.p-ma) a{color:var(--blue);text-decoration:underline;padding:14px 8px;margin:0 -8px}
:is(.p-mu,.p-ma) code{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
:is(.p-mu,.p-ma) :not(pre)>code{background:#1d160d;padding:2px 5px;border-radius:4px}
.p-mu :not(pre)>code{background:var(--ink)}
:is(.p-mu,.p-ma) pre{background:var(--card);border:2px solid var(--grid);border-radius:8px;padding:10px 12px;overflow-x:auto;-webkit-overflow-scrolling:touch}
:is(.p-mu,.p-ma) pre code.hljs{background:none;padding:0;overflow:visible}
:is(.p-mu,.p-ma) table{display:block;overflow-x:auto;border-collapse:collapse;font-size:14px}
:is(.p-mu,.p-ma) :is(th,td){border:1px solid var(--grid);padding:4px 9px;text-align:left}
:is(.p-mu,.p-ma) th{color:var(--amber)}
:is(.p-mu,.p-ma) img{display:block;max-width:100%;max-height:420px;border-radius:6px}
:is(.p-mu,.p-ma) blockquote{padding-left:10px;border-left:3px solid var(--grid);color:var(--dim)}
:is(.p-mu,.p-ma) hr{border:0;border-top:2px solid var(--grid)}
.p-cbot{display:flex;flex-direction:column;gap:8px;min-width:0}
.p-cbot .p-msg{margin:0}
.p-ctools,.p-cthumbs{display:flex;flex-wrap:wrap;gap:6px}
.p-tool{display:inline-flex;align-items:center;gap:8px;max-width:100%;height:28px;padding:0 12px;border-radius:14px;background:#10161c;
  color:var(--blue);font-size:12px;font-weight:600;letter-spacing:.1em}
.p-tool.web{background:#1a1204;color:var(--amber)}
.p-tool i{flex:none;width:8px;height:8px;border-radius:4px;background:currentColor}
.p-tool b{flex:none;font-weight:700;text-transform:uppercase}
.p-tool span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:.04em}
.p-cthink{margin:-6px 0;color:var(--dim)}
.p-cthink summary{display:flex;align-items:center;gap:8px;min-height:44px;list-style:none;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.14em}
.p-cthink summary::-webkit-details-marker{display:none}
.p-cthink summary::before{content:"";border:5px solid transparent;border-left:7px solid var(--dim);border-right:0}
.p-cthink[open] summary::before{transform:rotate(90deg)}
.p-cthink pre{margin:0 0 10px;padding-left:12px;border-left:4px solid var(--grid);white-space:pre-wrap;overflow-wrap:anywhere;font:400 13px/1.4 var(--body);letter-spacing:0}
.p-cthumb{display:inline-block;vertical-align:top;min-width:44px;min-height:44px;max-width:100%;border:0;padding:0;background:none}
.p-cthumb img{height:96px;margin:0}
.p-cfull{display:block;max-width:100%;margin:12px auto 4px;border-radius:6px}
.p-cwork{font:600 12px var(--lcars);letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
.p-cwork .p-track{width:120px}
/* the core shows toasts just above the tab bar, which is where this screen's field and Engage are */
html:has(#p-stage>.p-clog) #p-toast{top:calc(var(--vvt) + var(--sat) + 76px);bottom:auto}
.p-comp{flex:none;padding:0 10px 2px}
html.kb .p-comp{padding-bottom:8px}
.p-comp .p-msg{margin:0 0 2px}
.p-comp .p-card{margin-bottom:10px}
.p-cbar{gap:4px}
.p-cbar .p-chip span{padding:0 12px}
/* the model chip takes the room the three switches leave: when that is too little, first " · vision"
   drops out, then the name is cut */
.p-cmodel{flex:1 1 auto;min-width:0}
.p-cmodel span{flex-wrap:wrap;align-content:flex-start;justify-content:flex-start;overflow:hidden;line-height:36px}
.p-cmodel em{font-style:normal;min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.p-chip.p-cdown.on span{background:var(--red)}
.p-cdown:not(.on) span{color:var(--red)}
.p-catt{display:flex;gap:6px;padding:2px 0 6px}
.p-catt button{position:relative;flex:none;width:56px;height:56px;border:0;padding:0;border-radius:8px;overflow:hidden;background:var(--card)}
.p-catt img{display:block;width:100%;height:100%;object-fit:cover}
.p-catt button::after{content:"×";position:absolute;right:3px;top:3px;width:18px;height:18px;border-radius:9px;background:var(--ink);
  color:var(--text);font:700 14px/17px var(--body);text-align:center}
.p-comprow{display:flex;align-items:flex-end;gap:8px}
.p-round{flex:none;display:inline-flex;align-items:center;justify-content:center;width:48px;height:48px;margin-bottom:6px;border:0;border-radius:24px;background:var(--grid)}
.p-round svg{width:20px;height:20px}
.p-input{flex:1;min-width:0;display:flex;margin-bottom:6px;padding:0 16px;border-radius:24px;background:var(--card);border:2px solid var(--grid)}
.p-input:focus-within{border-color:var(--magenta)}
.p-input textarea{flex:1;min-width:0;height:44px;min-height:44px;max-height:132px;margin:0;padding:11px 0;border:0;outline:0;border-radius:0;resize:none;
  background:none;color:var(--text);font:400 16px/22px var(--body);letter-spacing:0;-webkit-appearance:none;appearance:none;-webkit-user-select:text;user-select:text}
.p-input textarea::placeholder{color:var(--dim);opacity:1}
.p-mic{flex:none;display:inline-flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;width:60px;height:60px;padding:0;border:0;
  border-radius:30px;background:var(--magenta);color:#000;font-size:13px;font-weight:700;line-height:1;text-transform:uppercase;letter-spacing:.08em}
.p-mic svg{width:26px;height:26px}
.p-mic.send{background:var(--orange)}
.p-mic.wait{opacity:1;background:var(--grid);color:var(--text)}
.p-mic.rec{background:var(--red);animation:p-crec 1s steps(2) infinite}
.p-mic.rec svg{width:14px;height:14px}
.p-mic.rec b{text-transform:none}
@keyframes p-crec{50%{opacity:.5}}
@media (prefers-reduced-motion:reduce){ .p-mic.rec{animation:none} }
.p-cchat{display:flex;gap:4px}
.p-cchat .p-row{flex:1;min-width:0}
.p-cdel{flex:none;min-width:76px;border:0;padding:0 12px;background:var(--card);color:var(--red);font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.1em}
.p-spk{align-self:flex-start;display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:0 16px 0 12px;border:0;border-radius:22px;background:var(--card);color:var(--blue);font:700 12px var(--lcars);letter-spacing:.14em;text-transform:uppercase}
.p-spk svg{width:20px;height:20px;fill:currentColor}
.p-spk::after{content:"Speak"} .p-spk[data-state]{background:var(--blue);color:#000} .p-spk[data-state]::after{content:"Stop"}
.p-ma:empty~.p-spk{display:none}
`);

/* ---------------------------------------------------------------- markdown (fleet.html L1795-1810)
   Model and tool text is untrusted: no styles, forms or embeds, and no image may load from another host
   (an image URL can carry chat or wiki text to the internet). Remote images become a link you would have
   to tap. The page's Content-Security-Policy (img-src 'self' data: blob:) blocks them a second time.
   Stricter than the desktop in one respect, because this page acts on any tapped [data-act] and styles by
   class and id: data-* attributes, ids and classes are removed too (a code block keeps its language-x). */
const MDCFG = {ADD_ATTR: ["target"], ALLOW_DATA_ATTR: false, FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "option", "iframe", "object", "embed", "link", "meta"],
  FORBID_ATTR: ["style", "action", "formaction", "srcset", "poster", "background", "id"]};
let mdOn = false, mdLoad = null;       /* mdOn: the libraries are in AND the hook is set; before that, plain escaped text */
const md = window.md = t => {
  if(!mdOn || !window.marked || !window.DOMPurify) return esc(t).replace(/\n/g, "<br>");
  return DOMPurify.sanitize(marked.parse(t || "", {breaks: true, gfm: true}), MDCFG);
};
function compRemote(el){   /* blocked remote images -> plain links with the host shown */
  el.querySelectorAll("img[data-remote]").forEach(i => { const u = i.getAttribute("data-remote"); let h = "";
    try{ h = new URL(u).host; }catch(e){}
    const a = document.createElement("a"); a.href = /^https?:/i.test(u) ? u : "#"; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.textContent = `[remote image${h ? ": " + h : ""}]`; i.replaceWith(a); }); }
const compHL = window.compHL = el => { compRemote(el); if(window.hljs) el.querySelectorAll("pre code").forEach(c => { try{ hljs.highlightElement(c); }catch(e){} }); };
const quiet = () => {};
P.mdReady = () => mdLoad || (mdLoad = Promise.all([P.load("/vendor/marked.min.js"), P.load("/vendor/purify.min.js"),
    P.load("/vendor/highlight.min.js").catch(quiet), P.load("/vendor/atom-one-dark.min.css").catch(quiet)]).then(() => {
  DOMPurify.addHook("afterSanitizeAttributes", n => {
    if(n.hasAttribute("class") && !(n.tagName === "CODE" && /^language-[\w+#.-]+$/.test(n.getAttribute("class")))) n.removeAttribute("class");
    if(n.tagName === "A"){ n.setAttribute("target", "_blank"); n.setAttribute("rel", "noopener noreferrer"); }
    if(n.tagName === "IMG"){ const src = n.getAttribute("src") || "";
      if(!/^(data:image\/|\/(?!\/))/i.test(src)){ n.removeAttribute("src"); n.setAttribute("data-remote", src); } }
  });
  mdOn = true;
}, e => { mdLoad = null; throw e; }));

/* ---------------------------------------------------------------- the stage, built once */
const PLUS = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3v14M3 10h14" stroke="#ffcc99" stroke-width="2.6" stroke-linecap="round"/></svg>';
const FACE = { mic: ['<svg viewBox="0 0 26 26" aria-hidden="true"><rect x="9.5" y="3" width="7" height="12.5" rx="3.5"/><path d="M6 12.5a7 7 0 0014 0M13 19.5V23" fill="none" stroke="#000" stroke-width="2.4" stroke-linecap="round"/></svg>', "Record a question"],
  send: ["Engage", "Engage: send the message"], wait: ["Wait", "Transcribing"],
  rec: ['<svg viewBox="0 0 14 14" aria-hidden="true"><rect width="14" height="14" rx="2"/></svg><b>0s</b>', "Stop recording"] };
const mk = (cls, html) => { const el = document.createElement("div"); el.className = cls; el.innerHTML = html || ""; return el; };
const gate = mk("p-cgate"),
  log = mk("p-clog p-sel", `<div class="p-chat"><div class="p-card p-chint" style="--c:var(--magenta)"><h3>Ready</h3>
    <p>Ask by text or voice. The loaded model answers and can read the wiki and search the web. Web queries are checked first: no customer names, secrets, personal data or pasted text leave this host.</p></div></div>`),
  comp = mk("p-comp", `<div></div><div></div><div class="p-chips p-cbar"></div><div class="p-catt"></div>
    <div class="p-comprow"><button class="p-round" type="button" data-act="comp-attach" aria-label="Attach an image">${PLUS}</button>
      <label class="p-input"><textarea rows="1" placeholder="Ask the computer" aria-label="Message to Computer" autocomplete="off"></textarea></label>
      <button class="p-mic" type="button" data-act="comp-main"></button></div>
    <input type="file" accept="image/*" multiple hidden>`);
const msgs = log.firstChild, hint = msgs.firstChild, [none, warn, bar, att, row, file] = comp.children, [plus, , main] = row.children, ta = row.querySelector("textarea");

const TOOLS = [["wiki", "Wiki", false], ["vane", "Vane", true], ["google", "Google", true]];   /* id, label, talks to the internet */
const WEB = n => /^(vane|google)_/.test(n);
const GUARD = "Web guard: a query with customer names, personal data, a secret or copied text was not sent; the model rephrases it.";
let chat = null, turn = null, imgs = [], rec = null, voice = "", mounted = false, want = "", stuck = true, top = 0, chats = [], hold = 0;
const models = () => (P.data.cmodels && P.data.cmodels.models) || [];
const model = () => { const l = models(), id = LS.get("computerModel"); return l.find(m => m.id === id) || l[0] || null; };
/* the desktop keeps the switched-on tools as a comma list and reads an empty value as "all three" */
const tools = () => { const s = LS.get("computerTools"); return TOOLS.map(t => t[0]).filter(t => !s || s.split(",").includes(t)); };

let lastTop = 0;      /* scrolling UP leaves the bottom at once; coming back within 48 px sticks again */
log.addEventListener("scroll", () => { const t = log.scrollTop, away = log.scrollHeight - t - log.clientHeight;
  if(t < lastTop - 1 && away > 2) stuck = false; else if(away < 48) stuck = true; lastTop = t; }, {passive: true});
const stick = force => { if(force || stuck){ stuck = true; log.scrollTop = log.scrollHeight; } };
if(window.ResizeObserver) new ResizeObserver(() => stick()).observe(log);   /* the keyboard takes room: keep the last line in view */
log.addEventListener("click", e => {
  const a = e.target.closest("a[href]"), href = a && a.getAttribute("href");
  if(!a) ta.blur();                                                           /* a tap on the log puts the keyboard away */
  else if(href[0] === "#" || (/^https?:/i.test(href) && P.native("open", {url: a.href}))) e.preventDefault();   /* model text never changes the route */
});
const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight || 44, 132) + "px"; btn(); };
ta.addEventListener("input", grow);
function btn(){
  const m = model(), s = voice === "rec" ? "rec" : voice === "wait" ? "wait" : ta.value.trim() || imgs.length ? "send" : "mic";
  if(main._s !== s){ main._s = s; main.className = "p-mic " + s; main.innerHTML = FACE[s][0]; main.setAttribute("aria-label", FACE[s][1]);
    hold = Date.now() + 400; }             /* the button just changed its meaning: a second tap right now was meant for the old one */
  main.disabled = s === "wait" || (s === "send" && (!!turn || !m));          /* one turn at a time: Fleet refuses a second send */
  plus.disabled = !(m && m.vision) || imgs.length >= 4;
}

/* ---------------------------------------------------------------- messages */
const thumb = (u, alt) => /^data:image\//i.test(u) ? `<button class="p-cthumb" type="button" data-act="comp-img" aria-label="Enlarge the image"><img src="${esc(u)}" alt="${alt}"></button>` : "";
function userEl(content){
  const el = mk("p-mu", typeof content === "string" ? md(content) : content.map(p => p.type === "text" ? md(p.text) : thumb(p.image_url.url, "attached image")).join(""));
  compRemote(el); return el;
}
const SPK = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 8h3l4-3.5v11L6 12H3z"/><path d="M13 7.5a3.5 3.5 0 010 5M15.5 5a7 7 0 010 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>', spk = new WeakMap();      /* spk: reply element -> its markdown, for the Speak button (vendor/computer-voice.js) */
const botEl = () => mk("p-cbot", `<div class="p-ctools"></div><details class="p-cthink" hidden><summary>Thinking</summary><pre></pre></details><div class="p-ma"></div><div class="p-cthumbs"></div><button class="p-spk" type="button" data-act="comp-speak" aria-label="Read this reply aloud">${SPK}</button>`);
const part = (bot, c) => bot.querySelector("." + c);
const chip = (bot, name, args) => part(bot, "p-ctools").insertAdjacentHTML("beforeend", `<span class="p-tool${WEB(name) ? " web" : ""}"><i></i><b>${
  esc(String(name).replace(/_/g, " "))}</b><span>${esc(String((args && (args.query || args.path || args.q)) || "").slice(0, 80))}</span></span>`);
const thumbs = (bot, urls) => part(bot, "p-cthumbs").insertAdjacentHTML("beforeend", (urls || []).map(u => thumb(u, "tool image")).join(""));
const think = (bot, t) => { const d = part(bot, "p-cthink"); d.hidden = false; d.lastChild.textContent = t; };
const say = (bot, t, err) => bot.insertAdjacentHTML("beforeend", H.msg(t, err));
function fresh(){ CV.stop(); turn = null; chat = null; msgs.replaceChildren(hint); btn(); }

/* a stored chat (fleet.html compLoad). The text of every round is kept, as it is while streaming. */
async function show(c){
  await P.mdReady().catch(quiet);
  msgs.replaceChildren(hint);
  let bot = null, txt = "", th = "";
  for(const m of c.messages || []){
    if(m.role === "user"){ msgs.append(userEl(m.content)); bot = null; }
    else if(m.role === "assistant"){
      if(!bot){ msgs.append(bot = botEl()); txt = th = ""; }
      if(m.reasoning) think(bot, th += m.reasoning);
      (m.tool_calls || []).forEach(tc => { let a = {}; try{ a = JSON.parse(tc.function.arguments || "{}"); }catch(e){} chip(bot, tc.function.name, a); });
      if(m.error) say(bot, m.content, true);
      else if(m.content){ txt += (txt ? "\n\n" : "") + m.content; part(bot, "p-ma").innerHTML = md(txt); spk.set(bot, txt); }
    }
    else if(m.role === "tool" && bot) thumbs(bot, m.images);
  }
  msgs.querySelectorAll(".p-ma").forEach(compHL);
  stick(true);
}
async function open(id){
  await P.run(async () => {
    let c;
    try{ c = await P.api.get("/api/computer/chat?id=" + encodeURIComponent(id)); }
    catch(e){ if(e.status === 404) e.message = "This chat is not stored. A chat that is still being answered is stored when the answer is finished."; throw e; }
    CV.stop(); turn = null; chat = {id: c.id, title: c.title}; await show(c); btn();
    if(mounted) P.go("#/computer/" + c.id, true);
  });
}

/* ---------------------------------------------------------------- one turn (fleet.html compSend)
   The turn runs on the Spark to its end whatever happens to this connection, and it cannot be stopped.
   iOS cuts the stream when the app goes to the background: then the stored chat is asked for every 3 s
   until it is newer than this send (404 = a new chat that is still being answered). */
async function stored(id, since, live){
  for(const end = Date.now() + 12e5; live(); await new Promise(ok => setTimeout(ok, 3000))){
    try{ const c = await P.api.get("/api/computer/chat?id=" + encodeURIComponent(id), {prompt: false}); if(c.updated > since) return c; }catch(e){}
    if(Date.now() > end) return null;
  }
}
async function send(){
  const text = ta.value.trim(), m = model();
  if(turn || !m || (!text && !imgs.length)) return;
  const me = turn = {}, live = () => turn === me, images = imgs;
  const body = {id: chat ? chat.id : null, text, model: m.id, tools: tools(), images};
  const user = userEl(images.length ? [{type: "text", text}, ...images.map(url => ({type: "image_url", image_url: {url}}))] : text), bot = botEl(), out = part(bot, "p-ma");
  imgs = []; ta.value = ""; ta.blur(); grow(); drawAtt(); msgs.append(user, bot); P.haptic("medium");
  CV.stop(); CV.prime(); const vs = CV.stream(part(bot, "p-spk"));
  let txt = "", th = "", id = body.id, since = Date.now() / 1000, done = false, failed = false, guarded = false, timer = 0;
  /* markdown is parsed again at most every 150 ms; code is highlighted once, at the end */
  const paint = last => { clearTimeout(timer); timer = 0; spk.set(bot, txt);
    out.innerHTML = (txt ? md(txt) : "") + (last === true ? "" : `<div class="p-cwork">Working<div class="p-track indet"><i></i></div></div>`);
    if(last === true) compHL(out); else compRemote(out); stick(); };
  paint(); stick(true);

  const ask = async () => {            /* fetch by hand: the answer is read as a stream */
    const k = P.key.get(), r = await fetch("/api/computer/send", {method: "POST", body: JSON.stringify(body),
      headers: Object.assign({"Content-Type": "application/json"}, k && {"X-Starfleet-Key": k})});
    if(r.ok) return r;
    const j = await r.json().catch(() => ({}));
    throw Object.assign(new Error(String(j.error || "Fleet answered " + r.status)), {status: r.status});
  };
  let r;
  try{
    try{ r = await ask(); }
    catch(e){ if(e.status !== 403 || /Cannot verify/i.test(e.message)) throw e;
      if(!await P.needKey(P.key.get() ? "Fleet did not accept the stored key." : "")) throw Object.assign(new Error("Starfleet key needed. The message was not sent."), {status: 403});
      r = await ask(); }
  }catch(e){                           /* nothing reached the model: the draft goes back into the field */
    user.remove(); bot.remove(); if(live()) turn = null;
    if(!ta.value) ta.value = text; if(!imgs.length) imgs = images; grow(); drawAtt();
    P.toast(e.status ? e.message : "No contact with Fleet. Is Tailscale connected? Your message is back in the field.", "err"); P.haptic("error"); return;
  }
  /* Back in the foreground and the stream says nothing for 6 s: the socket may be half dead (no error,
     no end, as over a VPN that slept). Let go of it; the stored chat is asked for instead. */
  let rd = null, beat = 0;
  const wake = () => { const b = beat; if(!document.hidden) setTimeout(() => { if(rd && b === beat) rd.cancel().catch(quiet); }, 6000); };
  document.addEventListener("visibilitychange", wake);
  try{
    since = Date.parse(r.headers.get("Date") || "") / 1000 || since;         /* Fleet's clock, not the phone's */
    rd = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for(;;){ const {value, done: end} = await rd.read(); if(end) break; beat++; buf += dec.decode(value, {stream: true});
      let i; while((i = buf.indexOf("\n\n")) >= 0){ const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if(!line.startsWith("data:")) continue; let e; try{ e = JSON.parse(line.slice(5)); }catch(x){ continue; }
        if(e.type === "token"){ txt += e.text; if(vs) vs.feed(txt); if(!timer) timer = setTimeout(paint, 150); }
        else if(e.type === "reasoning"){ think(bot, th += e.text); stick(); }
        else if(e.type === "tool_call"){ if(txt) txt += "\n\n"; chip(bot, e.name, e.args); stick(); }
        else if(e.type === "tool_result"){ if(e.guarded && !guarded){ guarded = true; say(bot, GUARD); } thumbs(bot, e.images); stick(); }
        else if(e.type === "chat"){ id = e.id; if(live()){ chat = {id, title: e.title}; if(mounted) P.go("#/computer/" + id, true); } }
        else if(e.type === "error"){ failed = true; say(bot, e.text, true); }
        else if(e.type === "done") done = true; } }
  }catch(e){}                          /* a cut connection ends like a stream without "done" */
  rd = null; document.removeEventListener("visibilitychange", wake);
  paint(true); if(vs) vs.end(txt);
  if(!done && !failed){
    if(!id) say(bot, "The connection ended before the answer started. Look in Chats in a moment.", true);
    else { say(bot, "The connection ended before the answer finished. Computer keeps writing it; this chat is checked every 3 seconds."); stick();
      const c = await stored(id, since, live);
      if(live()){ if(c) await show(c); else say(bot, "No stored answer after 20 minutes. Open this chat from Chats later to see what was saved.", true); } }
  }
  P.haptic(done ? "success" : "warning");
  if(live()){ turn = null; btn(); stick(); }
}

/* ---------------------------------------------------------------- images (vision models only)
   At most 4, each scaled down on the phone to 1568 px and stored as JPEG: a camera picture is 3 to 12 MB,
   Fleet takes 8 MB per image, and the model sees no more than this anyway. */
function drawAtt(){
  att.innerHTML = imgs.map((u, i) => `<button type="button" data-act="comp-unattach" data-arg="${i}" aria-label="Remove image ${i + 1}"><img src="${esc(u)}" alt=""></button>`).join("");
  btn();
}
async function attach(files){
  const m = model(); if(!m || !m.vision) return;
  for(const f of files){
    if(!f.type.startsWith("image/")) continue;
    if(imgs.length >= 4){ P.toast("At most 4 images in one message.", "err"); break; }
    try{
      const bmp = await createImageBitmap(f), k = Math.min(1, 1568 / Math.max(bmp.width, bmp.height)), c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(bmp.width * k)); c.height = Math.max(1, Math.round(bmp.height * k));
      const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height); g.drawImage(bmp, 0, 0, c.width, c.height); if(bmp.close) bmp.close();
      const url = c.toDataURL("image/jpeg", .85);
      if(url.length * .75 > 8e6) P.toast("Image over 8 MB. Pick a smaller one.", "err"); else if(imgs.length < 4) imgs.push(url);
    }catch(e){ P.toast("This image could not be read. Try a JPEG or PNG.", "err"); }
    drawAtt();
  }
}
file.addEventListener("change", () => { const l = [...file.files]; file.value = ""; attach(l); });
ta.addEventListener("paste", e => attach([...((e.clipboardData || {}).files || [])]));

/* ---------------------------------------------------------------- voice (fleet.html L1925-1946)
   Recorded here, turned into a 16 kHz mono WAV, transcribed by whisper on a Spark. The text lands in the
   field; sending stays a tap. iOS: MediaRecorder without a mime type or timeslice (it records AAC in MP4,
   which decodeAudioData reads), tracks stopped afterwards so the orange microphone light goes out. */
function wav16(f32){ const b = new ArrayBuffer(44 + f32.length * 2), v = new DataView(b), w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + f32.length * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, f32.length * 2, true);
  for(let i = 0; i < f32.length; i++){ const x = Math.max(-1, Math.min(1, f32[i])); v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true); } return b; }
async function mic(){
  if(rec){ if(rec.state !== "inactive") rec.stop(); return; }
  if(voice) return;
  const fake = P._test.recorder;       /* the test harness has no microphone: a quarter second of silence instead */
  if(!fake && !(window.isSecureContext && navigator.mediaDevices && window.MediaRecorder)){ P.toast("Voice needs HTTPS. Open Fleet over https to use the microphone.", "err"); return; }
  voice = "ask";
  let stream;
  try{ stream = fake ? {getTracks: () => []} : await navigator.mediaDevices.getUserMedia({audio: true}); }
  catch(e){ voice = ""; P.toast(e.name === "NotAllowedError" ? "Microphone not allowed. Allow it for this app in Settings, then try again." : "Microphone: " + e.message, "err"); return; }
  const chunks = [], t0 = Date.now();
  const r = rec = fake ? {start(){}, stop(){ r.state = "inactive"; r.ondataavailable({data: new Blob([wav16(new Float32Array(4000))])}); r.onstop(); }} : new MediaRecorder(stream);
  const tick = setInterval(() => { main.lastChild.textContent = mmss((Date.now() - t0) / 1000);
    if(Date.now() - t0 > 3e5 && r.state !== "inactive") r.stop(); }, 500);                       /* five minutes at most */
  r.ondataavailable = e => chunks.push(e.data);
  r.onstop = async () => {
    clearInterval(tick); stream.getTracks().forEach(t => t.stop()); rec = null; voice = "wait"; btn();
    try{
      const ab = await new Blob(chunks).arrayBuffer(), ctx = new (window.AudioContext || window.webkitAudioContext)();
      const buf = await ctx.decodeAudioData(ab); if(ctx.close) ctx.close();
      const off = new OfflineAudioContext(1, Math.ceil(buf.duration * 16000), 16000), src = off.createBufferSource(); src.buffer = buf; src.connect(off.destination); src.start();
      const out = await off.startRendering();
      const j = await P.api.post("/api/computer/transcribe", wav16(out.getChannelData(0)), {raw: true, type: "audio/wav", timeout: 120000});
      if(j && j.text){ ta.value = (ta.value ? ta.value + " " : "") + j.text; P.haptic("success"); } else P.toast("No speech was recognised. Try again.");
    }catch(e){ if(!e.quiet){ P.toast(e.status === 409 ? "Both Sparks are too busy to transcribe right now. Try again in a moment." : "Voice: " + e.message, "err"); P.haptic("error"); } }
    voice = ""; grow();
  };
  r.start(); voice = "rec"; btn(); P.haptic("medium");
}
P.askComputer = () => { P.go("#/computer"); if(!rec) mic(); };   /* the app's "Ask Computer" shortcut */

/* ---------------------------------------------------------------- actions */
P.on("comp-main", () => { if(Date.now() < hold) return; if(voice === "rec" || !(ta.value.trim() || imgs.length)) mic(); else send(); });
P.on("comp-attach", () => file.click());
P.on("comp-unattach", i => { imgs.splice(+i, 1); drawAtt(); });
P.on("comp-img", (a, el) => { P.sheet({title: "Image", color: "magenta", body: `<img class="p-cfull" src="${esc(el.firstChild.src)}" alt="">`}); });
P.on("comp-speak", (a, el) => { CV.toggle(el, spk.get(el.closest(".p-cbot")) || ""); P.haptic("select"); });
P.on("comp-voice", () => { CV.auto = !CV.auto; if(CV.auto) CV.prime(); P.haptic("select"); P.paint(); });
CV.init({headers: () => ({"X-Starfleet-Key": P.key.get()}), say: (t, e) => P.toast(t, e ? "err" : undefined)});
P.on("comp-new", () => { fresh(); P.haptic("select"); P.go("#/computer", true); });
P.on("comp-tool", t => { const on = tools();
  LS.set("computerTools", TOOLS.map(x => x[0]).filter(x => (x === t) !== on.includes(x)).join(",") || "none");   /* "none": an empty value would mean all */
  P.haptic("select"); P.paint(); });
P.on("comp-models", () => { const cur = model();
  P.sheet({title: "Model", color: "blue", body: `<div class="p-list" style="margin:12px 0 4px">${models().map(m => H.row({act: "comp-model", arg: m.id, c: m === cur ? "blue" : "grid",
      t: MN(m.id) + (m.vision ? " · vision" : ""), s: m.id, tag: m === cur ? "In use" : "", tagc: "ready"})).join("")}</div>
    <p class="p-note">Only models that are loaded right now. Computer never loads a model.</p>`}); });
P.on("comp-model", id => { LS.set("computerModel", id); P.haptic("select"); P.closeSheets(); P.paint(); });

let csheet = null;
const chatRow = c => { const open = !!chat && chat.id === c.id;
  return `<div class="p-cchat">${H.row({act: "comp-load", arg: c.id, c: open ? "magenta" : "grid", t: c.title, tag: open ? "Open" : "", tagc: "pinned",
  s: [ago(c.updated), c.model ? MN(c.model) : ""].filter(Boolean).join(" · ")})}<button class="p-cdel" type="button" data-act="comp-del" data-arg="${esc(c.id)}">Delete</button></div>`; };
async function drawChats(){
  const s = csheet; if(!s) return;
  try{ chats = (await P.api.get("/api/computer/chats")).chats || [];
    s.set(`<div class="p-list" style="margin:12px 0 4px">${chats.length ? chats.map(chatRow).join("") : `<div class="p-empty">No chats yet</div>`}</div>`); }
  catch(e){ s.set(H.msg(e.message, true)); }
}
P.on("comp-chats", () => { csheet = P.sheet({title: "Chats", color: "magenta", onClose(){ csheet = null; }, body: `<div class="p-empty" style="margin-top:12px">Loading...</div>`}); drawChats(); });
P.on("comp-load", id => { P.haptic("select"); P.closeSheets(); open(id); });
P.on("comp-del", id => P.run(async () => {
  const c = chats.find(x => x.id === id);
  if(!await P.confirm({title: "Delete this chat?", sub: c ? c.title : "", danger: true, yes: "Delete", text: "Fleet removes the stored chat. This cannot be undone."})) return;
  await P.api.post("/api/computer/delete", {id}); P.haptic("success");
  if(chat && chat.id === id){ fresh(); P.closeSheets(); P.go("#/computer", true); } else drawChats();
}));

/* ---------------------------------------------------------------- the screen */
P.sources.cmodels = {url: "/api/computer/models", key: true};
const HX = `<button class="p-hbtn" type="button" data-act="comp-new">New</button><button class="p-hbtn" type="button" data-act="comp-chats">Chats</button>`;
P.screens.computer = { tab: "computer", title: "Computer", color: "magenta", stage: true, needs: {cmodels: 10000},
  mount(el, arg){
    el.append(gate, log, comp); mounted = true; grow(); log.scrollTop = stuck ? log.scrollHeight : top;
    P.mdReady().catch(quiet);
    want = arg && arg !== (chat && chat.id) ? arg : "";       /* opened in render(), once the key has been accepted */
  },
  unmount(){ mounted = false; top = log.scrollTop; if(rec) mic(); if(!/^#\/?computer(\/|$)/.test(location.hash)) CV.stop(); },   /* another screen, not just another chat id */
  render(){
    const d = P.data.cmodels, key = P.err.cmodels === "key", m = model();
    P.hx(key ? "" : HX);
    P.put(gate, key ? H.keygate("Computer") : d ? "" : P.err.cmodels ? H.msg(P.err.cmodels, true) : `<div class="p-empty">Contacting Fleet...</div>`);
    log.hidden = key; comp.hidden = key || !d;
    if(key || !d) return;
    if(want){ const id = want; want = ""; open(id); }
    for(const el of [hint, bar, att, row]) el.hidden = !m;
    P.put(none, m ? "" : `<div class="p-card" style="--c:var(--red)"><h3>No model loaded</h3><p>Computer answers with a model that is already loaded and never loads one itself. Load a chat model in Models first.</p>
      <div class="p-pair" style="margin-top:11px"><button class="p-btn sm" type="button" data-act="go" data-arg="#/models">Open Models</button></div></div>`);
    if(!m){ P.put(warn, ""); return; }
    const on = tools(), srv = d.servers || {}, down = TOOLS.filter(([t]) => on.includes(t) && srv[t] && srv[t].error);
    P.put(warn, H.msg(down.map(([t, l]) => `${l} is not answering (${srv[t].error}). Computer answers without it.`).join(" ")));
    P.put(bar, `<button class="p-chip set p-cmodel" type="button" data-act="comp-models" aria-label="Model: ${esc(MN(m.id))}"><span><em>${esc(MN(m.id))}</em>${m.vision ? "<em>&nbsp;· vision</em>" : ""}</span></button>` +
      TOOLS.map(([t, l, web]) => { const is = on.includes(t), bad = srv[t] && srv[t].error;
        return H.chip({act: "comp-tool", arg: t, label: l, on: is, cls: bad ? "p-cdown" : is && web ? "amber" : ""}); }).join("") +
      H.chip({act: "comp-voice", label: "Auto speak", on: CV.auto}));
    if(imgs.length && !m.vision){ imgs = []; drawAtt(); }
    btn();
  } };
})();
