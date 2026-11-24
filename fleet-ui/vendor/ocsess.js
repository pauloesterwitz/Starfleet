/* ================= OpenCode sessions (Computer on the desktop, the PADD phone page) =================
   One component for both pages: OC.mount(el, {get, post, compact, mode, node, id, go}) draws the session list
   and one open session with prompt box, Stop, model / agent / MCP pickers, permission cards and a new-session
   sheet. Everything goes through /api/computer/oc (ocbridge.py), which also decides what is offered, so a new
   OpenCode release cannot reach anything this file does not ask for. The data (S) is shared by every mounted
   instance, so the phone's list and session screens do not refetch from scratch. */
(() => {
"use strict";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const NODES = ["jean-luc", "kathryn"], NAME = {"jean-luc": "Jean-Luc", "kathryn": "Kathryn"};
const base = d => (d || "").replace(/\/+$/, "").split("/").pop() || "/";
const home = d => (d || "").replace(/^\/home\/[^/]+/, "~");
const ago = ms => { if(!ms) return ""; const s = Math.max(0, (Date.now() - ms) / 1000);
  return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min" : s < 172800 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d"; };
const mmss = s => { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
const LS = { get(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } }, set(k, v){ try{ localStorage.setItem(k, v); }catch(e){} } };

const S = { ov: {}, err: {}, sel: null, det: null, detErr: "", models: {}, agents: {}, cmds: {}, projs: {}, mcp: null,
  contract: {}, auto: LS.get("ocAuto") === "1", more: {}, at: 0, sending: false, kick: 0 };
const key = (n, id) => n + "/" + id;

const CSS = `
.oc{--c-or:var(--orange,#f90);--c-bl:var(--blue,#9cf);--c-pe:var(--peach,#f96);--c-am:var(--amber,#fc6);--c-re:var(--red,#c66);--c-mv:var(--mauve,#c9c);
  --c-gr:var(--grid,#332211);--c-di:var(--dim,#a4855f);--c-tx:var(--text,#fc9);height:100%;min-height:0;display:grid;grid-template-columns:310px 1fr;gap:8px;
  color:var(--c-tx);font-family:var(--lcars,Antonio,sans-serif);letter-spacing:.05em}
.oc.oc-c{display:block;position:relative}
.oc button{font-family:inherit;cursor:pointer}
.oc-list{overflow:auto;display:flex;flex-direction:column;gap:6px;padding-right:2px;min-height:0}
.oc-c .oc-list{height:100%;padding:0 10px 10px}
.oc-list>*,.oc-it>*{flex:none}
.oc-c.oc-sess .oc-list,.oc-c:not(.oc-sess) .oc-main{display:none}
.oc-top{display:flex;gap:6px;flex-wrap:wrap}
.oc-btn{border:0;border-radius:0 16px 16px 0;background:var(--c-or);color:#000;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:8px 14px;min-height:44px;font-size:14px}
.oc-btn.sm{border-radius:14px;min-height:36px;padding:4px 12px;font-size:12px}
.oc-btn.alt{background:var(--c-gr);color:var(--c-tx)} .oc-btn.red{background:var(--c-re)} .oc-btn.blue{background:var(--c-bl)} .oc-btn.mv{background:var(--c-mv)}
.oc-btn:disabled{opacity:.35;cursor:default} .oc-btn.on{background:var(--c-or);color:#000}
.oc-grp{display:flex;align-items:center;gap:8px;margin-top:6px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;font-size:15px}
.oc-grp:before{content:"";width:26px;height:14px;border-radius:7px 0 0 7px;background:var(--c-mv)} .oc-grp.k:before{background:var(--c-bl)}
.oc-grp small{font-size:11px;color:var(--c-di);letter-spacing:.1em} .oc-grp i{flex:1;height:4px;background:var(--c-gr)}
.oc-it{text-align:left;display:flex;flex-direction:column;gap:3px;background:#0b0803;color:var(--c-tx);border:0;border-left:8px solid var(--c-gr);padding:8px 10px;min-height:56px;width:100%}
.oc-it:hover{border-left-color:var(--c-bl)} .oc-it.on{background:#160f06;border-left-color:var(--c-or)}
.oc-it.wait{background:#2b1d08;border-left-color:var(--c-am)} .oc-it.busy{border-left-color:var(--c-bl)}
.oc-t{font-size:16px;font-weight:600;letter-spacing:.03em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;color:var(--c-tx)}
.oc-st{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--c-di)} .oc-it.wait .oc-st{color:var(--c-am)} .oc-it.busy .oc-st{color:var(--c-bl)}
.oc-ft{font-size:11px;color:var(--c-di);letter-spacing:.06em;line-height:1.5;margin-top:auto;padding-top:8px}
.oc-ft.bad{color:var(--c-re)} .oc-nw{color:var(--c-am);font-style:normal;font-weight:600}
.oc-note{font:13px/1.4 -apple-system,"Segoe UI",sans-serif;color:var(--c-di);letter-spacing:0;padding:6px 2px}
.oc-note.bad{color:var(--c-re)}
.oc-main{display:flex;flex-direction:column;min-width:0;min-height:0;gap:6px}
.oc-c .oc-main{height:100%;padding:0 10px}
.oc-head{display:flex;gap:8px;align-items:flex-start}
.oc-head>div{flex:1;min-width:0} .oc-head h2{margin:0;font-size:22px;line-height:1.1;font-weight:700;letter-spacing:.03em;overflow-wrap:anywhere}
.oc-head small{font-size:12px;color:var(--c-di);letter-spacing:.1em;text-transform:uppercase}
.oc-bar{display:flex;gap:6px;flex-wrap:wrap}
.oc-chip{border:0;border-radius:14px;min-height:36px;padding:2px 14px;font-weight:600;letter-spacing:.06em;font-size:13px;background:var(--c-bl);color:#000;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.oc-chip.o{background:var(--c-or)} .oc-chip.g{background:var(--c-gr);color:var(--c-tx)} .oc-chip.r{background:var(--c-re)}
.oc-log{flex:1;min-height:0;overflow:auto;background:#050402;border:1px solid var(--c-gr);border-radius:4px 18px 4px 4px;padding:10px 14px;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
.oc-m{margin:0 0 12px;max-width:980px} .oc-w{font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;margin-bottom:3px}
.oc-m.u .oc-w{color:var(--c-pe)} .oc-m.a .oc-w{color:var(--c-bl)}
.oc-bd{border-left:6px solid var(--c-gr);padding:6px 12px;background:#0b0803;color:#f3e6d8;font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;letter-spacing:0;overflow-wrap:anywhere}
.oc-m.u .oc-bd{border-left-color:var(--c-pe);white-space:pre-wrap} .oc-m.a .oc-bd{border-left-color:var(--c-bl)}
.oc-bd p{margin:4px 0 8px} .oc-bd pre{background:#0f1117;border-radius:8px;padding:10px 12px;overflow:auto;font-size:13px}
.oc-bd code{font-family:ui-monospace,Menlo,monospace;font-size:.92em} .oc-bd :not(pre)>code{background:#1d160d;border-radius:4px;padding:1px 5px;color:var(--c-am)}
.oc-bd a{color:var(--c-bl)} .oc-bd table{border-collapse:collapse;display:block;overflow-x:auto} .oc-bd th,.oc-bd td{border:1px solid #3a2c18;padding:3px 8px}
.oc-tl{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0}
.oc-tc{background:var(--c-gr);border-radius:10px;padding:0 10px;font-size:12px;letter-spacing:.05em;color:var(--c-tx);max-width:100%}
.oc-tc summary{cursor:pointer;min-height:28px;line-height:28px;list-style:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.oc-tc summary::-webkit-details-marker{display:none} .oc-tn{color:var(--c-bl);font-weight:700;margin-right:6px;text-transform:uppercase}
.oc-tc.err{color:var(--c-re)} .oc-tc.run .oc-tn{color:var(--c-am)} .oc-tc[open]{flex-basis:100%;border-radius:10px;padding-bottom:6px}
.oc-tc pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.4 ui-monospace,Menlo,monospace;color:#f3e6d8;margin:4px 0;max-height:260px;overflow:auto}
.oc-th{color:var(--c-di);font-size:13px;margin:2px 0} .oc-th summary{cursor:pointer;text-transform:uppercase;letter-spacing:.1em;font-size:11px;min-height:28px;line-height:28px}
.oc-th pre{white-space:pre-wrap;font:13px/1.4 -apple-system,sans-serif;margin:2px 0;color:var(--c-di)}
.oc-perm{border:2px solid var(--c-am);border-left-width:10px;border-radius:0 16px 16px 0;padding:8px 12px;background:#1c1405;display:flex;flex-direction:column;gap:6px}
.oc-perm h4{margin:0;font-size:14px;letter-spacing:.14em;color:var(--c-am);text-transform:uppercase}
.oc-perm code{font:12px/1.5 ui-monospace,Menlo,monospace;color:#ffe2c2;overflow-wrap:anywhere;white-space:pre-wrap}
.oc-row{display:flex;gap:6px;flex-wrap:wrap} .oc-row>*{flex:1 1 auto}
.oc-in{display:grid;grid-template-columns:1fr auto;gap:6px}
.oc-ta{resize:none;height:70px;background:#0b0803;border:2px solid var(--c-gr);border-radius:4px 18px 18px 4px;color:#f3e6d8;padding:8px 14px;font:16px -apple-system,"Segoe UI",sans-serif;outline:none;min-width:0}
.oc-ta:focus{border-color:var(--c-or)} .oc-c .oc-ta{height:56px}
.oc-sd{display:flex;flex-direction:column;gap:4px;width:150px} .oc-c .oc-sd{width:120px} .oc-sd .oc-btn{flex:1;border-radius:0 16px 16px 0}
.oc-q{font-size:12px;color:var(--c-di);letter-spacing:.08em;text-transform:uppercase}
.oc-back{display:none} .oc-c .oc-back{display:inline-block}
.oc-sheet{position:fixed;inset:0;z-index:90;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center}
.oc-sheet>div{background:#0b0803;border:2px solid var(--c-or);border-radius:4px 22px 4px 4px;width:min(560px,94vw);max-height:84vh;display:flex;flex-direction:column;color:var(--c-tx);font-family:var(--lcars,Antonio,sans-serif);letter-spacing:.05em}
.oc-c~.oc-sheet>div,.oc-sheet.c>div{width:100%;max-height:88vh;position:absolute;bottom:0;border-radius:22px 22px 0 0;border-width:2px 0 0}
.oc-sh{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:2px solid var(--c-gr)} .oc-sh h3{margin:0;flex:1;font-size:18px;letter-spacing:.12em;text-transform:uppercase;color:var(--c-or)}
.oc-sb{overflow:auto;padding:8px 10px 18px;display:flex;flex-direction:column;gap:4px;-webkit-overflow-scrolling:touch}
.oc-sb>*{flex:none}
.oc-opt{text-align:left;border:0;background:#160f06;color:var(--c-tx);border-left:6px solid var(--c-gr);padding:8px 12px;min-height:44px;display:flex;flex-direction:column;gap:2px}
.oc-opt:hover{border-left-color:var(--c-bl)} .oc-opt.on{border-left-color:var(--c-or)} .oc-opt:disabled{opacity:.45;cursor:default}
.oc-opt .oc-t{font-size:15px;white-space:normal} .oc-opt small{font:12px/1.35 -apple-system,sans-serif;color:var(--c-di);letter-spacing:0;overflow-wrap:anywhere}
.oc-opt.arm{border-left-color:var(--c-am);background:#2b1d08}
.oc-lab{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--c-di);margin:8px 0 2px}
.oc-sg{display:flex;gap:6px;flex-wrap:wrap} .oc-sg .oc-chip{flex:1 1 auto}
.oc-field{background:#0b0803;border:2px solid var(--c-gr);border-radius:4px;color:#f3e6d8;padding:8px 10px;font:16px -apple-system,"Segoe UI",sans-serif;outline:none;width:100%;box-sizing:border-box}
.oc-st.ok{color:var(--c-bl)} .oc-st.bad{color:var(--c-re)}
@media (max-width:760px){.oc:not(.oc-c){grid-template-columns:1fr}}
`;
function css(){ if(document.getElementById("oc-css")) return; const s = document.createElement("style"); s.id = "oc-css"; s.textContent = CSS; document.head.append(s); }

/* ---------------------------------------------------------------- data */
let api = null;
const U = (node, path, q) => `/api/computer/oc?node=${node}&path=${encodeURIComponent(path)}` + Object.entries(q || {}).map(([k, v]) => `&${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("");
const GET = (node, path, q) => api.get(U(node, path, q));
const POST = (node, path, body, q) => api.post(U(node, path, q), body);
const sid = p => "/api/session/" + p;
const cur = () => S.sel ? (S.ov[S.sel.node] || {sessions: []}).sessions.find(s => s.id === S.sel.id) : null;

async function loadList(){
  await Promise.all(NODES.map(async n => { try{ S.ov[n] = await GET(n, "@overview"); S.err[n] = ""; }catch(e){ S.err[n] = e.message || "no answer"; } }));
  S.at = Date.now();
}
async function loadDetail(){
  const s = S.sel; if(!s) return;
  try{ const d = await GET(s.node, "@detail", {id: s.id, limit: 40}); if(S.sel && S.sel.id === s.id){ S.det = d; S.detErr = ""; } }
  catch(e){ if(S.sel && S.sel.id === s.id) S.detErr = e.message || "no answer"; }
}
async function meta(node, force){
  const m = S.models[node];
  if(m && !force && Date.now() - m.at < 60000) return;
  S.models[node] = {at: Date.now(), list: (m || {}).list || []};
  try{
    const [mo, ag, cm, pr] = await Promise.all([GET(node, "/api/model"), GET(node, "/api/agent"), GET(node, "/api/command"), GET(node, "@projects")]);
    S.models[node] = {at: Date.now(), list: mo.models}; S.agents[node] = ag.agents; S.cmds[node] = cm.commands; S.projs[node] = pr.projects;
  }catch(e){ S.models[node].err = e.message; }
}
async function loadContract(){
  await Promise.all(NODES.map(async n => { try{ S.contract[n] = await GET(n, "@contract"); }catch(e){ S.contract[n] = {error: e.message}; } }));
}
const modelName = (node, ref) => { if(!ref) return "model"; const m = ((S.models[node] || {}).list || []).find(x => x.id === ref.id && x.providerID === ref.providerID);
  return m ? m.name.replace(/\s*·.*$/, "") : String(ref.id).split("/").pop(); };
const waitingTotal = () => NODES.reduce((n, k) => n + ((S.ov[k] || {}).sessions || []).filter(s => s.waiting).length, 0);

/* ---------------------------------------------------------------- one mounted instance */
function mount(el, o){
  css(); api = o.api;
  const compact = !!o.compact;
  el.innerHTML = `<div class="oc${compact ? " oc-c" : ""}"><aside class="oc-list"></aside><section class="oc-main">
    <div class="oc-head"><button class="oc-btn sm alt oc-back" type="button" data-oc="back">Sessions</button><div class="oc-hd"></div><span class="oc-stop"></span></div>
    <div class="oc-bar"></div><div class="oc-log" tabindex="0"></div><div class="oc-pm"></div>
    <div class="oc-in"><textarea class="oc-ta" rows="2" autocomplete="off" placeholder="Prompt this session. /name runs an OpenCode command."></textarea><div class="oc-sd"></div></div>
    <div class="oc-q"></div></section></div>`;
  const root = el.firstChild, $ = s => root.querySelector(s), list = $(".oc-list"), hd = $(".oc-hd"), stopEl = $(".oc-stop"), bar = $(".oc-bar"),
    log = $(".oc-log"), pm = $(".oc-pm"), ta = $(".oc-ta"), sd = $(".oc-sd"), hint = $(".oc-q");
  const put = (e, html) => { if(e._h === html) return; if(e === log && String(getSelection()).length) return; e._h = html; e.innerHTML = html; };
  let alive = true, tl = 0, td = 0, stick = true, sheetEl = null;
  const visible = () => alive && root.isConnected && root.offsetParent !== null && !document.hidden;

  /* ---- list */
  function stat(s){
    if(s.waiting) return ["Waiting for you · permission", "wait"];
    if(s.busy) return ["Generating", "busy"];
    return [(s.outcome === "interrupted" ? "Interrupted " : "Idle ") + ago(s.updated), ""];
  }
  function drawList(){
    let h = `<div class="oc-top"><button class="oc-btn" type="button" data-oc="new">+ New session</button>
      <button class="oc-btn alt sm" type="button" data-oc="auto" aria-pressed="${S.auto}">Automated${autoN() ? " " + autoN() : ""} ${S.auto ? "shown" : "hidden"}</button></div>`;
    for(const n of NODES){
      const ov = S.ov[n], err = S.err[n], all = ov ? ov.sessions.filter(s => S.auto || !s.auto) : [];
      all.sort((a, b) => (b.waiting > 0) - (a.waiting > 0) || b.busy - a.busy || (b.updated || 0) - (a.updated || 0));
      const lim = S.more[n] ? 200 : 25, rows = all.slice(0, lim), act = all.filter(s => s.busy).length;
      h += `<div class="oc-grp${n === "kathryn" ? " k" : ""}">${NAME[n]}<small>${ov ? act + " active · OpenCode " + esc(ov.version) : err ? "not answering" : "..."}</small><i></i></div>`;
      if(err) h += `<div class="oc-note bad">${esc(err)}</div>`;
      for(const s of rows){
        const [t, cls] = stat(s), sel = S.sel && S.sel.id === s.id;
        h += `<button class="oc-it ${cls}${sel ? " on" : ""}" type="button" data-oc="open" data-n="${n}" data-id="${esc(s.id)}"><span class="oc-t">${esc(s.title)}</span>
          <span class="oc-st">${esc(t)}</span><span class="oc-st">${esc(base(s.dir))} · ${esc(modelName(n, s.model))}</span></button>`;
      }
      if(ov && !all.length) h += `<div class="oc-note">No sessions${!S.auto && ov.autoCount ? " (" + ov.autoCount + " automated hidden)" : ""}.</div>`;
      if(all.length > lim) h += `<button class="oc-btn alt sm" type="button" data-oc="more" data-n="${n}">Show ${all.length - lim} more</button>`;
    }
    h += foot();
    put(list, h);
  }
  const autoN = () => NODES.reduce((n, k) => n + ((S.ov[k] || {}).autoCount || 0), 0);
  function foot(){
    const bits = NODES.map(n => { const c = S.contract[n], v = (S.ov[n] || {}).version; if(!c || c.error) return `${NAME[n]} ${esc(v || "?")}`;
      return `${NAME[n]} ${esc(c.version)}${c.latest && c.latest !== c.version ? ` <em class="oc-nw">(${esc(c.latest)} is out)</em>` : ""}`; });
    const bad = NODES.filter(n => S.contract[n] && S.contract[n].ok === false);
    return `<div class="oc-ft${bad.length ? " bad" : ""}">OpenCode ${bits.join(" · ")}<br>${bad.length ? "This version changed something the page relies on: " + bad.map(n => esc(S.contract[n].broken.slice(0, 2).join("; "))).join("; ") :
      NODES.every(n => S.contract[n] && S.contract[n].ok) ? "Page and OpenCode agree." : ""}</div>`;
  }

  /* ---- session view */
  function toolSummary(c){
    const i = (c.state || {}).input || {};
    const v = i.command || i.filePath || i.path || i.pattern || i.query || i.url || i.description || (Object.keys(i).length ? JSON.stringify(i) : "");
    return String(v).replace(/\s+/g, " ").slice(0, 90);
  }
  function renderMd(t){ return typeof md === "function" ? md(t) : `<p>${esc(t).replace(/\n/g, "<br>")}</p>`; }
  function drawMsgs(d){
    let h = "";
    for(const m of d.messages){
      if(m.type === "user"){ if(m.text) h += `<div class="oc-m u"><div class="oc-w">You</div><div class="oc-bd">${esc(m.text)}</div></div>`; continue; }
      if(m.type !== "assistant"){ if(m.type !== "idle") h += `<div class="oc-note">${esc(m.type)}</div>`; continue; }
      let body = "", tools = "";
      for(const c of m.content || []){
        if(c.type === "text" && c.text && c.text.trim()) body += `<div class="oc-bd">${renderMd(c.text)}</div>`;
        else if(c.type === "reasoning" && c.text) body += `<details class="oc-th"><summary>Thinking</summary><pre>${esc(c.text)}</pre></details>`;
        else if(c.type === "tool"){ const st = (c.state || {}).status, out = ((c.state || {}).content || []).map(x => x.text || "").join("\n");
          tools += `<details class="oc-tc ${st === "error" ? "err" : st === "completed" ? "" : "run"}"><summary><span class="oc-tn">${esc(c.name)}</span>${esc(toolSummary(c))}</summary>
            <pre>${esc(JSON.stringify((c.state || {}).input || {}, null, 1))}</pre>${out ? `<pre>${esc(out)}</pre>` : ""}</details>`; }
      }
      h += `<div class="oc-m a"><div class="oc-w">${esc(m.agent || "OpenCode")}</div>${body}${tools ? `<div class="oc-tl">${tools}</div>` : ""}${m.error ? `<div class="oc-note bad">${esc(JSON.stringify(m.error).slice(0, 300))}</div>` : ""}</div>`;
    }
    return h || `<div class="oc-note">No messages yet.</div>`;
  }
  function drawMain(){
    const s = S.sel, row = cur();
    root.classList.toggle("oc-sess", !!s);
    if(!s){ put(hd, `<small>Select a session on the left, or start a new one.</small>`); put(stopEl, ""); put(bar, ""); put(log, ""); put(pm, ""); put(sd, ""); hint.textContent = ""; ta.disabled = true; return; }
    const d = S.det && S.det.session && S.det.session.id === s.id ? S.det : null, ses = d ? d.session : row, busy = d ? d.busy : row && row.busy;
    ta.disabled = false;
    put(hd, `<h2>${esc((ses && ses.title) || (row && row.title) || "Session")}</h2><small>${esc(NAME[s.node])} · ${esc(home(ses && ses.location ? ses.location.directory : row && row.dir))}${busy && ses ? " · generating " + mmss((Date.now() - (ses.time || {}).busy) / 1000) : ""}</small>`);
    put(stopEl, busy ? `<button class="oc-btn red" type="button" data-oc="stop">Stop</button>` : "");
    const m = ses && ses.model, srv = S.mcp && S.mcp.key === key(s.node, s.id) ? S.mcp.servers : null;
    const ag = d && d.messages ? [...d.messages].reverse().find(x => x.type === "assistant" && x.agent) : null;
    put(bar, `<button class="oc-chip" type="button" data-oc="models">${esc(modelName(s.node, m))}${m && m.variant && m.variant !== "default" ? " · " + esc(m.variant) : ""} ▾</button>
      <button class="oc-chip o" type="button" data-oc="agents">${esc((ag && ag.agent) || "agent")} ▾</button>
      <button class="oc-chip g" type="button" data-oc="mcp">MCP${srv ? " " + srv.filter(x => x.status === "connected").length + "/" + srv.length : ""}</button>
      <button class="oc-chip g" type="button" data-oc="cmds">/ Commands</button>
      <button class="oc-chip g" type="button" data-oc="compact">Compact</button>`);
    if(d) put(log, drawMsgs(d)); else put(log, S.detErr ? `<div class="oc-note bad">${esc(S.detErr)}</div>` : `<div class="oc-note">Loading...</div>`);
    if(stick) log.scrollTop = log.scrollHeight;
    const perms = d ? d.permissions || [] : [];
    put(pm, perms.map(p => { const id = p.id || p.requestID, res = (p.resources || []).join("\n"), cmd = (p.metadata && (p.metadata.command || p.metadata.description)) || "";
      return `<div class="oc-perm" role="group" aria-label="Permission request"><h4>Permission needed · ${esc(p.action || "")}</h4><code>${esc(cmd || res).slice(0, 600)}</code>
        <div class="oc-row"><button class="oc-btn blue" type="button" data-oc="perm" data-id="${esc(id)}" data-d="once">Allow once</button>
        <button class="oc-btn alt" type="button" data-oc="perm" data-id="${esc(id)}" data-d="always">Always allow</button>
        <button class="oc-btn alt" type="button" data-oc="perm" data-id="${esc(id)}" data-d="reject">Deny</button></div></div>`; }).join(""));
    put(sd, busy ? `<button class="oc-btn" type="button" data-oc="send" data-d="steer" title="Interrupt the current step and use this prompt now">Steer</button><button class="oc-btn alt" type="button" data-oc="send" data-d="queue" title="Run after the current turn finishes">Queue</button>`
      : `<button class="oc-btn" type="button" data-oc="send">Engage</button>`);
    hint.textContent = busy ? "Busy: Steer applies at once, Queue waits for this turn." : "";
  }
  function draw(){ drawList(); drawMain(); if(o.onDraw) o.onDraw(S); }

  /* ---- actions */
  async function select(node, id){
    S.sel = node && id ? {node, id} : null; S.det = null; S.detErr = ""; S.mcp = null; stick = true;
    if(S.sel){ LS.set("ocSel", node + "/" + id); meta(node).then(draw); loadMcp(); loadDetail().then(draw); }
    draw();
    if(o.onSelect) o.onSelect(S.sel);
  }
  async function loadMcp(){
    const s = S.sel, ses = cur(); if(!s) return;
    try{ const r = await GET(s.node, "@mcp", {dir: ses ? ses.dir : (S.det && S.det.session.location.directory)}); if(S.sel && S.sel.id === s.id){ S.mcp = {key: key(s.node, s.id), servers: r.servers}; draw(); } }catch(e){}
  }
  const toast = (t, bad) => { hint.textContent = t; hint.style.color = bad ? "var(--c-re)" : ""; setTimeout(() => { hint.style.color = ""; }, 5000); };
  async function act(fn){ try{ await fn(); }catch(e){ toast(e.message || "failed", true); } S.kick = Date.now(); await loadDetail(); await loadList(); draw(); }
  async function send(delivery){
    const s = S.sel; let text = ta.value.trim(); if(!s || !text || S.sending) return;
    S.sending = true;
    await act(async () => {
      const mc = text.match(/^\/(\S+)\s*([\s\S]*)$/);
      if(mc && (S.cmds[s.node] || []).some(c => c.name === mc[1])) await POST(s.node, sid(s.id) + "/command", {name: mc[1], text: mc[2], delivery});
      else await POST(s.node, sid(s.id) + "/prompt", {text, delivery});
      ta.value = ""; stick = true;
    });
    S.sending = false;
  }

  /* ---- sheets */
  function closeSheet(){ if(sheetEl){ sheetEl.remove(); sheetEl = null; } }
  function sheet(title, html, on){
    closeSheet();
    const w = document.createElement("div"); w.className = "oc-sheet" + (compact ? " c" : "");
    w.innerHTML = `<div role="dialog" aria-label="${esc(title)}"><div class="oc-sh"><h3>${esc(title)}</h3><button class="oc-btn sm alt" type="button" data-x="1">Close</button></div><div class="oc-sb">${html}</div></div>`;
    w.addEventListener("click", e => { if(e.target === w || e.target.closest("[data-x]")) return closeSheet(); on && on(e, w); });
    root.append(w); sheetEl = w; return w;
  }
  const opt = (a, b, c, d) => `<button class="oc-opt${d && d.on ? " on" : ""}" type="button" ${a}${d && d.off ? " disabled" : ""}><span class="oc-t">${esc(b)}</span>${c ? `<small>${esc(c)}</small>` : ""}</button>`;
  function modelList(node, pick, all){
    const L = (S.models[node] || {}).list || [], hid = all ? 0 : L.filter(m => m.loaded === false).length, groups = {local: "Local (loaded first)", anthropic: "Anthropic", free: "Free"};
    return Object.entries(groups).map(([g, lab]) => { const xs = L.filter(m => m.group === g && (all || m.loaded !== false)); if(!xs.length) return "";
      return `<div class="oc-lab">${lab}</div>` + xs.map(m => opt(`data-m="${esc(m.id)}" data-p="${esc(m.providerID)}"`, m.name.split(" · ")[0], m.loaded === false ? "Not loaded. Load it in Shuttle Bay first." : [m.name.split(" · ").slice(1).join(" · "), m.ctx ? Math.round(m.ctx / 1000) + "K context" : "", m.vision ? "vision" : ""].filter(Boolean).join(" · "),
        {off: m.loaded === false, on: pick && pick.id === m.id})).join(""); }).join("") + (hid ? `<button class="oc-btn alt sm" type="button" data-all="1">Show ${hid} local models that are not loaded</button>` : "") || `<div class="oc-note">${(S.models[node] || {}).err ? esc(S.models[node].err) : "Loading models..."}</div>`;
  }
  function variantStep(node, m, done){
    const w = sheet("Reasoning · " + m.name, opt(`data-v=""`, "Default", "The model's own default") + m.variants.map(v => opt(`data-v="${esc(v)}"`, v)).join(""),
      e => { const b = e.target.closest("[data-v]"); if(b) done(b.dataset.v); });
  }
  async function pickModel(node, cb){
    await meta(node);
    const L = (S.models[node] || {}).list || [];
    sheet("Model · " + NAME[node], modelList(node), (e, w) => {
      if(e.target.closest("[data-all]")){ w.querySelector(".oc-sb").innerHTML = modelList(node, null, true); return; }
      const b = e.target.closest("[data-m]"); if(!b || b.disabled) return;
      const m = L.find(x => x.id === b.dataset.m && x.providerID === b.dataset.p);
      const fin = v => { closeSheet(); cb(Object.assign({id: m.id, providerID: m.providerID}, v ? {variant: v} : {})); };
      m.variants && m.variants.length ? variantStep(node, m, fin) : fin("");
    });
  }

  root.addEventListener("click", async e => {
    const b = e.target.closest("[data-oc]"); if(!b || sheetEl && sheetEl.contains(b)) return;
    const k = b.dataset.oc, s = S.sel;
    if(k === "open") return select(b.dataset.n, b.dataset.id);
    if(k === "back") return select(null);
    if(k === "auto"){ S.auto = !S.auto; LS.set("ocAuto", S.auto ? "1" : "0"); return draw(); }
    if(k === "more"){ S.more[b.dataset.n] = 1; return draw(); }
    if(k === "new") return newSession();
    if(!s) return;
    if(k === "stop") return act(() => POST(s.node, sid(s.id) + "/interrupt", {}));
    if(k === "send") return send(b.dataset.d);
    if(k === "perm") return act(() => POST(s.node, sid(s.id) + "/permission/" + b.dataset.id + "/reply", {decision: b.dataset.d}));
    if(k === "compact") return act(() => POST(s.node, sid(s.id) + "/compact", {}));
    if(k === "models") return pickModel(s.node, ref => act(() => POST(s.node, sid(s.id) + "/model", {model: ref})));
    if(k === "agents"){ await meta(s.node);
      return sheet("Agent", (S.agents[s.node] || []).map(a => opt(`data-a="${esc(a.id)}"`, a.name, a.description)).join("") || `<div class="oc-note">No agents.</div>`,
        e2 => { const x = e2.target.closest("[data-a]"); if(x){ closeSheet(); act(() => POST(s.node, sid(s.id) + "/agent", {agent: x.dataset.a})); } }); }
    if(k === "cmds"){ await meta(s.node);
      return sheet("Commands", (S.cmds[s.node] || []).map(c => opt(`data-c="${esc(c.name)}"`, "/" + c.name, c.description)).join(""),
        e2 => { const x = e2.target.closest("[data-c]"); if(x){ closeSheet(); ta.value = "/" + x.dataset.c + " "; ta.focus(); } }); }
    if(k === "mcp") return mcpSheet();
  });
  ta.addEventListener("keydown", e => { if(e.key === "Enter" && !e.shiftKey && !compact && !e.isComposing){ e.preventDefault(); const d = cur(); send(d && d.busy ? "steer" : undefined); } });
  log.addEventListener("scroll", () => { stick = log.scrollHeight - log.scrollTop - log.clientHeight < 60; });

  function mcpSheet(){
    const s = S.sel; if(!s) return;
    const paint = (armed) => { const srv = S.mcp && S.mcp.key === key(s.node, s.id) ? S.mcp.servers : [];
      const w = sheetEl || sheet("MCP servers · " + NAME[s.node], "", e => {
        const x = e.target.closest("[data-s]"); if(!x) return;
        const name = x.dataset.s, up = x.dataset.up === "1";
        if(mcpSheet.armed !== name){ mcpSheet.armed = name; paint(); setTimeout(() => { if(mcpSheet.armed === name){ mcpSheet.armed = ""; if(sheetEl) paint(); } }, 4000); return; }
        mcpSheet.armed = "";
        act(async () => { await POST(s.node, `/api/experimental/mcp/${name}/${up ? "disconnect" : "connect"}`, {}, {"location[directory]": cur() ? cur().dir : ""}); await new Promise(r => setTimeout(r, 1500)); await loadMcp(); }).then(() => sheetEl && paint());
      });
      w.querySelector(".oc-sb").innerHTML = srv.length ? srv.map(x => { const ok = x.status === "connected", a = mcpSheet.armed === x.name;
        return `<button class="oc-opt${a ? " arm" : ""}" type="button" data-s="${esc(x.name)}" data-up="${ok ? 1 : 0}"><span class="oc-t">${esc(x.name)} · <span class="oc-st ${ok ? "ok" : "bad"}">${esc(x.status)}</span></span>
          <small>${a ? "Tap again to " + (ok ? "disconnect (all sessions in this folder lose it)" : "reconnect") : esc(x.error || (ok ? "Tap to disconnect" : "Tap to reconnect"))}</small></button>`; }).join("") : `<div class="oc-note">Loading...</div>`; };
    mcpSheet.armed = ""; closeSheet(); sheetEl = null; paint(); loadMcp().then(() => sheetEl && paint());
  }

  async function newSession(){
    let node = (S.sel && S.sel.node) || "jean-luc", dir = "", model = null, agent = "";
    await Promise.all(NODES.map(n => meta(n)));
    const draw2 = () => {
      const w = sheetEl; if(!w) return;
      const projs = S.projs[node] || [], list = (S.models[node] || {}).list || [];
      if(!dir || !projs.some(p => p.dir === dir)) dir = (projs.find(p => p.dir !== "/") || projs[0] || {}).dir || "";
      if(!model || !list.some(m => m.id === model.id && m.providerID === model.providerID && m.loaded !== false)){ const m = list.find(x => x.loaded) || list.find(x => x.group !== "local"); model = m ? {id: m.id, providerID: m.providerID} : null; }
      const mn = model ? (list.find(x => x.id === model.id) || {}).name : "none";
      w.querySelector(".oc-sb").innerHTML = `<div class="oc-lab">Machine</div><div class="oc-sg">${NODES.map(n => `<button class="oc-chip ${n === node ? "o" : "g"}" type="button" data-nn="${n}">${NAME[n]}${S.err[n] ? " (down)" : ""}</button>`).join("")}</div>
        <div class="oc-lab">Folder</div><button class="oc-chip" type="button" data-f="1">${esc(home(dir) || "choose")} ▾</button>
        <div class="oc-lab">Model</div><button class="oc-chip" type="button" data-mo="1">${esc((mn || "").replace(/\s*·.*$/, ""))} ▾</button>
        <div class="oc-lab">First prompt (optional)</div><textarea class="oc-field" id="oc-np" rows="4" placeholder="What should it do?"></textarea>
        <div class="oc-row" style="margin-top:10px"><button class="oc-btn" type="button" data-go="1">Start session</button></div><div class="oc-note" id="oc-ne"></div>`;
    };
    const w = sheet("New session", "", async e => {
      const t = e.target.closest("button"); if(!t) return;
      const np = sheetEl.querySelector("#oc-np"), keep = np ? np.value : "";
      if(t.dataset.nn){ node = t.dataset.nn; model = null; dir = ""; draw2(); }
      else if(t.dataset.f){ const old = sheetEl; sheet("Folder · " + NAME[node], (S.projs[node] || []).map(p => opt(`data-d="${esc(p.dir)}"`, base(p.dir) === "/" ? "/" : base(p.dir) || "~", home(p.dir), {on: p.dir === dir})).join(""),
        e2 => { const x = e2.target.closest("[data-d]"); if(x){ dir = x.dataset.d; closeSheet(); root.append(old); sheetEl = old; draw2(); old.querySelector("#oc-np").value = keep; } }); }
      else if(t.dataset.mo){ const old = sheetEl; pickModel(node, ref => { model = ref; root.append(old); sheetEl = old; draw2(); old.querySelector("#oc-np").value = keep; }); }
      else if(t.dataset.go){
        const err = sheetEl.querySelector("#oc-ne"); t.disabled = true; err.textContent = "Starting...";
        try{
          const r = await POST(node, "/api/session", {location: {directory: dir}, model, agent: agent || undefined});
          const id = (r.data || r).id; if(keep.trim()) await POST(node, sid(id) + "/prompt", {text: keep.trim()});
          closeSheet(); await loadList(); await select(node, id);
        }catch(x){ err.textContent = x.message; err.className = "oc-note bad"; t.disabled = false; }
      }
    });
    draw2();
  }

  /* ---- timers */
  const tick = async () => {
    if(!alive) return;
    if(visible()){
      const now = Date.now(), busy = (S.det && S.det.busy) || (cur() && cur().busy);
      if(now - tl > 10000){ tl = now; await loadList(); draw(); }
      if(S.sel && now - td > (busy ? 2000 : 7000)){ td = now; await loadDetail(); draw(); }
    }
    setTimeout(tick, 700);
  };
  const first = async () => {
    await loadList(); draw();
    const want = o.id ? {node: o.node, id: o.id} : o.keep ? S.sel : null;
    if(want && want.id) await select(want.node, want.id);
    else if(!compact && !S.sel){ const last = (LS.get("ocSel") || "").split("/"); const hit = last[1] && ((S.ov[last[0]] || {}).sessions || []).find(x => x.id === last[1]); if(hit) await select(last[0], last[1]); }
    if(!Object.keys(S.contract).length) loadContract().then(draw);
    Promise.all(NODES.map(n => meta(n))).then(draw);
    tick();
  };
  if(o.id && (!S.sel || S.sel.id !== o.id)){ S.sel = {node: o.node, id: o.id}; S.det = null; }
  if(!o.id && compact) S.sel = null;
  draw(); first();
  return { select, refresh: async () => { await loadList(); await loadDetail(); draw(); }, unmount(){ alive = false; closeSheet(); }, waiting: waitingTotal };
}
window.OC = { mount, state: S, waiting: waitingTotal };
})();
