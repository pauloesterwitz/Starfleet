/* ================= Starfleet PADD: Systems =================
   The Systems tab and five of its screens: Sessions, Main Engineering, Deep Space NAS, Memory Alpha and
   Defiant (Memory Core is padd-memcore.js). The rules are the desktop page's, so a row means the same on
   both: when fleet.html changes one, change it here too. Source lines as of 2026-11-19: nasKind L1442,
   NAS actions L1456-1465 and L1567, hardware L1537, containers L1597-1631, formats L1659, Memory Alpha
   L1678, Defiant L1734. Sessions follow the Mac app (Sources/OpencodeMonitor/Models.swift, ContentView.swift).
   Payloads: containers_state, nas_state, nas_stats, memory_alpha_state, defiant_state, /api/defiant/updates,
   sessions_state and session_tail in fleet.py. The Updates block of Defiant follows fleet.html's. */
(() => {
"use strict";
const P = window.PADD, H = P.h, TAG = P.model.TAG;
const nn = n => TAG[n] || n;
const now = () => Date.now() / 1000;
const clock = t => new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
const mb = m => m == null ? "–" : m >= 1024 ? (m / 1024).toFixed(1) + " GB" : m + " MB";
const fmtB = b => b == null ? "–" : b >= 1e12 ? (b / 1e12).toFixed(2) + " TB" : b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(0) + " MB" : Math.round(b / 1e3) + " kB";
const fmtN = n => n == null ? "–" : n.toLocaleString("en-US");
const fmtT = t => t ? new Date(t * 1000).toLocaleString([], {weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}) : "–";
const hm = t => t ? new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false}) : "–";
const until = t => !t ? "–" : t < now() ? "due" : "in " + dur(t - now());
const rate = b => b == null ? "–" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB/s" : (b / 1e3).toFixed(0) + " kB/s";
const STATE = {ok: ["Nominal", "ready"], warn: ["Attention", "warn"], error: ["Fault", "fault"], off: ["Not configured", ""]};
const stateTag = st => STATE[st] || [st || "Unknown", "warn"];
const order = names => [...new Set(["jean-luc", "kathryn"].filter(n => names.includes(n)).concat(names))];
const sent = t => String(t).replace(/[.\s]+$/, "") + ".";
const empty = t => `<div class="p-empty" style="margin-top:14px">${esc(t)}</div>`;
const synced = (k, short) => P.hx(P.err[k] === "key" ? "" : `<span class="p-sync">${P.err[k] ? "Sync lost" : P.at[k] ? (short ? "" : "Synced ") + clock(P.at[k]) : "Checking"}</span>`);
/* the last poll failed but an older answer is on screen */
const lost = k => P.data[k] && P.err[k] && P.err[k] !== "key" ? H.msg(`${sent(P.err[k])} Showing the last answer, from ${clock(P.at[k])}.`, true) : "";
function wait(el, k){ if(P.data[k]) return false; P.put(el, P.err[k] ? H.msg(P.err[k], true) : empty("Checking...")); return true; }
const say = e => sent(/^[a-z]+ /.test(e) ? e[0].toUpperCase() + e.slice(1) : e);   /* fleet.py writes them lower case; a domain keeps its case */
const notes = d => [[d.errors, " err"], [d.problems, ""]].map(([l, c]) => (l || []).length ? `<div class="p-msg${c} p-sel">${l.map(e => esc(say(e))).join("<br>")}</div>` : "").join("");
const fail = (what, e) => { if(e && e.quiet) throw e; throw new Error(`${what}: ${(e && e.message) || e}`); };
const fig = v => { const [n, u] = String(v).split(" "); return `<b>${esc(n)}${u ? `<small>${esc(u)}</small>` : ""}</b>`; };
/* a row that only shows something; lines are HTML */
const info = o => `<div class="p-row p-sy-flat" style="--c:var(--${o.c || "grid"})"><span class="p-m"><span class="p-t${o.wrap ? " wrap" : ""} p-sel">${esc(o.t)}</span>${
  (o.lines || []).filter(Boolean).map(l => `<span class="p-s">${l}</span>`).join("")}${o.e ? `<span class="p-s wrap p-sel" style="color:var(--red)">${esc(o.e)}</span>` : ""}</span>${o.tag ? H.tag(o.tag, o.tagc) : ""}</div>`;
const dl = rows => `<div class="p-sy-dl">${rows.map(([k, v]) => `<div><span>${esc(k)}</span><b class="p-sel">${esc(v == null || v === "" ? "–" : v)}</b></div>`).join("")}</div>`;
const pan = (c, title, big, sub, kv, mid, tail) => `<div class="p-card" style="--c:var(--${c})"><h3>${esc(title)}</h3>${big ? `<div class="p-free">${big}</div>` : ""}${mid || ""}${
  sub ? `<div class="p-s wrap p-sel">${esc(sub)}</div>` : ""}${kv ? `<div class="p-kv">${kv.map(([l, v]) => `<div><b>${esc(v ?? "–")}</b><span>${esc(l)}</span></div>`).join("")}</div>` : ""}${tail || ""}</div>`;
const gauge = (pct, c) => `<div${c ? ` style="--g:var(--${c})"` : ""}>${H.gauge(pct || 0, 20)}</div>`;
/* P.sheet upper-cases and cuts its sub-line: a path or an image name keeps its case and wraps */
const keepCase = s => { const el = s.el.querySelector(".p-shd .p-s"); if(el) el.classList.add("p-sy-path"); };

P.css(`
.p-sy-fig{flex:none;display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.p-sy-fig b{font-size:20px;font-weight:700;line-height:1;color:var(--peach);white-space:nowrap}
.p-sy-fig small{margin-left:4px;font-size:12px;font-weight:600;color:var(--dim);letter-spacing:.1em;text-transform:uppercase}
.p-sy-amb{color:var(--amber)}
.p-kv .p-sy-amb{color:var(--amber)}
.p-btn.amber{background:var(--amber)}            /* Reboot Defiant while a reboot is waiting: amber, red is for failure */
.p-msg.p-sy-ok{background:#07131c;border-left-color:var(--blue);color:var(--blue)}
.p-sy-up{overflow-wrap:anywhere}            /* an apt error or a package name can hold a URL with no break in it */
.p-sy-flat:active{background:var(--card)}
.p-sy-log{margin-top:8px;padding:8px 10px;background:var(--ink);border-left:4px solid var(--track);font:400 11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:0;color:var(--text);white-space:pre-wrap;overflow-wrap:anywhere}
.p-sy-log.err{color:#f0a9a9;border-left-color:var(--red)}
.p-sy-state{display:flex;align-items:center;gap:10px;margin:8px 0 2px}
.p-sy-pans{display:flex;flex-direction:column;gap:6px}
.p-sy-pans .p-kv{margin-bottom:0}
.p-sy-pans .p-free{margin-bottom:2px}
.p-sy-pans .p-gauge{margin:8px 0 6px}
.p-sy-hw{display:grid;grid-template-columns:40px minmax(0,1fr) 124px;align-items:center;column-gap:10px;min-height:30px}
.p-sy-hw span{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em}
.p-sy-hw .p-gauge{margin:0}
.p-sy-hw b{text-align:right;font-size:15px;font-weight:600;color:var(--g,var(--text));white-space:nowrap}
.p-sy-dl{display:flex;flex-direction:column;gap:2px;margin-top:12px}
.p-sy-dl div{display:flex;align-items:baseline;gap:12px;background:var(--card);padding:9px 10px}
.p-sy-dl span{flex:none;width:92px;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em}
.p-sy-dl b{flex:1;min-width:0;font:400 14px/1.35 var(--body);letter-spacing:0;color:var(--text);overflow-wrap:anywhere}
.p-sy-path{text-transform:none;white-space:normal;overflow-wrap:anywhere;letter-spacing:.04em}
.p-sy-tail .p-shb{margin-top:8px;border-top:2px solid var(--track)}   /* a transcript always scrolls: a line where it goes under the header */
.p-sy-tr{display:flex;flex-direction:column;gap:14px;margin:4px 0 8px}
.p-sy-msg{--c:var(--blue);border-left:4px solid var(--c);padding:1px 0 1px 12px;min-width:0}
.p-sy-msg.me{--c:var(--orange)}
.p-sy-who{display:flex;align-items:baseline;gap:8px;font-size:12px;font-weight:700;color:var(--c);text-transform:uppercase;letter-spacing:.12em}
.p-sy-who small{font-size:11px;font-weight:400;color:var(--dim)}
.p-sy-txt{margin-top:4px;font:400 15px/1.45 var(--body);letter-spacing:0;color:#f3e6d8;white-space:pre-wrap;overflow-wrap:anywhere}
.p-sy-txt.clamp{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:8;overflow:hidden}
.p-sy-more{display:block;height:44px;min-width:44px;margin-bottom:-8px;padding:0;border:0;background:none;color:var(--blue);
  font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.12em}
.p-sy-part{display:flex;align-items:center;gap:8px;margin-top:6px;min-width:0;font-size:13px;line-height:1.3;color:var(--dim);letter-spacing:.05em}
.p-sy-part i{flex:none;width:8px;height:8px;border-radius:4px;background:var(--d,var(--amber))}
.p-sy-part span{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
`);

/* ================================================================ Systems: one row per screen
   Each line fills in when its own source answers; a source that is still on its way says so. */
const pend = k => P.err[k] && P.err[k] !== "key" ? {e: P.err[k]} : {s: "Checking"};
const SUM = {
  sessions(){
    if(P.err.sessions === "key") return {s: "Starfleet key needed"};
    if(!P.data.sessions) return pend("sessions");
    const all = P.sessionsOf(), st = all.filter(x => x.stalled).length;
    return {s: `${all.length} active · ${all.filter(x => x.generating).length} generating`, tag: st ? `${st} stalled` : "", tagc: "warn"};
  },
  engineering(){
    const d = P.data.containers; if(!d) return pend("containers");
    const cs = d.containers || [], sum = k => Math.round(cs.reduce((a, c) => a + (c[k] || 0), 0) / 1024), stale = Object.keys(d.errors || {});
    return {s: `${sum("ram_mb")} GB RAM · ${sum("gpu_mb")} GB GPU`, s2: stale.length ? `${stale.map(nn).join(" and ")} not answering` : "", tag: `${cs.length} running`, tagc: stale.length ? "warn" : ""};
  },
  nas(){
    const d = P.data.nas, h = P.data.nasstats, s = h && h.stats, out = {};
    if(s){ const hot = Math.max(...(s.temps || []).map(t => t.c));
      out.s = [`CPU ${s.cpu_pct.toFixed(0)}%`, `RAM ${(100 * s.mem_used_mb / s.mem_total_mb).toFixed(0)}%`, isFinite(hot) ? hot.toFixed(0) + "°" : ""].filter(Boolean).join(" · "); }
    else if(h) out.s = "No hardware figures";
    else Object.assign(out, pend("nasstats"));
    if(d){ const cs = d.containers || [], kinds = cs.map(c => nasKind(c)[0]);
      out.tag = d.error ? "Stale" : `${cs.filter(c => c.State === "running").length} of ${cs.length} up`;
      out.tagc = d.error ? "warn" : kinds.includes("bad") ? "fault" : kinds.includes("warn") ? "warn" : ""; }
    else if(P.err.nas && !out.e){ out.e = P.err.nas; if(out.s === "Checking") out.s = ""; }
    return out;
  },
  alpha(){
    const d = P.data.alpha; if(!d) return pend("alpha");
    const L = d.last, [tag, tagc] = stateTag(d.status);
    return {s: L && L.end ? `Last snapshot ${ago(L.end)} · ${fmtB(L.size)}` : "No snapshot reported", s2: `Off-site backup · next ${until(d.next_snapshot)}`, tag, tagc};
  },
  defiant(){
    const d = P.data.defiant; if(!d) return pend("defiant");
    if(d.configured === false) return {s: "Not set up", tag: "Not configured"};
    const sites = d.sites || [], up = sites.filter(x => !siteBad(x)).length, cert = Math.min(...sites.map(x => x.cert_days).filter(x => x != null));
    const [tag, tagc] = d.status === "ok" && d.reboot_required ? ["Reboot", "warn"] : stateTag(d.status);
    const n = d.upd ? d.upd.count : d.updates, m = d.upd ? d.upd.security : 0;       /* the hourly scan, the probe's count until there is one */
    return {s: `${up === sites.length ? up : up + " of " + sites.length} site${sites.length === 1 ? "" : "s"} up${isFinite(cert) ? ` · certificates ${cert} days` : ""}`,
      s2: n ? `${m ? "" : "Web server · "}${plural(n, "update")} pending${m ? ` · ${m} security` : ""}` : "Web server", tag, tagc};
  },
  core: () => ({}),
};
const ROWS = [["sessions", "Sessions", "magenta", "Claude Code and OpenCode on both Sparks"], ["engineering", "Main Engineering", "amber", "Every container on both Sparks"],
  ["nas", "Deep Space NAS", "blue", "Containers and hardware of the NAS"], ["alpha", "Memory Alpha", "mauve", "Off-site backup"],
  ["defiant", "Defiant", "peach", "Web server"], ["core", "Memory Core", "blue", "The wiki as a map"]];
P.screens.systems = { tab: "systems", title: "Systems", color: "amber",
  needs: {sessions: 15000, containers: 60000, nas: 60000, nasstats: 60000, alpha: 120000, defiant: 120000},
  render(el){
    P.hx(`<span class="p-sync">${esc(P.syncText())}</span>`);
    P.put(el, `<div class="p-list" style="margin-top:6px">${ROWS.map(([id, t, c, line]) => { let x; try{ x = SUM[id](); }catch(e){ x = {e: "This answer could not be read"}; }   /* one odd payload never takes the list down */
      return H.row({tall: true, big: true, act: "go", arg: "#/systems/" + id, c, t, s: x.s, s2: x.s2 || line, e: x.e, tag: x.tag, tagc: x.tagc, chev: true}); }).join("")}</div>`);
  } };

/* ================================================================ Sessions: what the Mac menu-bar app shows */
const toolName = t => t === "claude-code" ? "Claude Code" : "OpenCode";
const toolOf = s => s.tool || "opencode";                      /* a host with the older status script sends no tool */
const sKey = s => [s.node, toolOf(s), s.id].join("|");
const sEdge = s => s.stalled ? "red" : s.generating ? "magenta" : s.updated_secs_ago < 1800 ? "blue" : s.updated_secs_ago < 28800 ? "amber" : "";
const sTag = s => s.stalled ? ["Stalled", "fault"] : s.generating ? ["Generating", "pinned"] : [s.status || "idle", ""];
const sAge = s => s.stalled ? "waiting for " + dur(s.updated_secs_ago) : s.generating ? "now" : dur(s.updated_secs_ago) + " ago";
const sTodo = s => { const t = s.todos || {}; return [t.completed || 0, (t.pending || 0) + (t.in_progress || 0) + (t.completed || 0), t.in_progress || 0]; };
let sf = "all", ssheet = null, scur = null, tail = null, tailBusy = false, tailTimer = 0;
const more = new Set();
const sKeep = s => sf === "all" || (sf === "stalled" ? s.stalled : sf === "generating" ? s.generating : s.node === sf);
P.screens["systems/sessions"] = { tab: "systems", title: "Sessions", color: "magenta", parent: "#/systems", needs: {sessions: 5000},
  mount(el){ el.innerHTML = `<div class="p-chips" id="p-sy-sf" style="margin-top:4px"></div><div id="p-sy-sl"></div>`; },
  render(el){
    synced("sessions");
    const chips = el.querySelector("#p-sy-sf"), list = el.querySelector("#p-sy-sl");
    if(P.err.sessions === "key"){ P.put(chips, ""); P.put(list, `<div style="margin-top:10px">${H.keygate("The session list")}</div>`); return; }
    if(wait(list, "sessions")) return;
    const d = P.data.sessions, all = P.sessionsOf(), names = order(Object.keys(d.nodes || {}));
    if(!["all", "stalled", "generating", ...names].includes(sf)) sf = "all";
    P.put(chips, [["all", "All", all.length], ["stalled", "Stalled", all.filter(s => s.stalled).length], ["generating", "Generating", all.filter(s => s.generating).length],
      ...names.map(n => [n, nn(n), all.filter(s => s.node === n).length])].map(([f, label, n]) => H.chip({act: "sys.sf", arg: f, label, n, on: f === sf})).join(""));
    let html = lost("sessions"), shown = 0;
    for(const n of names){
      /* fleet.py sessions_state: a node that did not answer is in errors and keeps its last list; a node
         that answered may still report a problem of its own (no opencode database, say) */
      const v = d.nodes[n] || {}, down = (d.errors || {})[n];
      if((down || v.error) && (sf === n || !names.includes(sf)))
        html += `<div class="p-grp">${esc(nn(n))}</div>${down ? H.msg(`Not answering. Showing its last list. ${down}`, true) : H.msg(v.error)}`;
      for(const tool of ["claude-code", "opencode"]){
        const rows = all.filter(s => s.node === n && toolOf(s) === tool && sKeep(s)).sort((a, b) => (b.generating - a.generating) || (a.updated_secs_ago - b.updated_secs_ago));
        if(!rows.length) continue;
        shown += rows.length;
        html += `<div class="p-grp">${esc(nn(n))} · ${toolName(tool)}</div><div class="p-list">${rows.map(s => { const [tag, tagc] = sTag(s), [done, total] = sTodo(s);
          return H.row({act: "sys.ses", arg: sKey(s), c: sEdge(s), t: s.title || "Untitled session", s: `${total ? `Todos ${done} of ${total}` : "No todos"} · ${sAge(s)}`, tag, tagc}); }).join("")}</div>`;
      }
    }
    if(!shown) html += empty(all.length ? "No sessions match" : "No sessions active in the last 24 h");
    P.put(list, html);
    if(ssheet && ssheet.open) drawSession();
  } };
P.on("sys.sf", f => { sf = f; P.haptic("select"); P.paint(); });

/* the transcript tail in a sheet */
const findS = () => scur && (P.sessionsOf().find(s => sKey(s) === scur.key) || scur.s);
function partHtml(p, k){
  if(p.type === "text"){
    const t = String(p.text || "").trim(); if(!t) return "";
    const long = t.length > 480 || t.split("\n").length > 8, open = more.has(k);
    return `<div class="p-sy-txt p-sel${long && !open ? " clamp" : ""}">${esc(t)}</div>${long ? `<button class="p-sy-more" type="button" data-act="sys.more" data-arg="${esc(k)}">${open ? "Show less" : "Show more"}</button>` : ""}`;
  }
  if(p.type === "tool") return `<div class="p-sy-part p-sel" style="--d:var(--${/err|fail/i.test(p.status || "") ? "red" : /complet|done|success/i.test(p.status || "") ? "blue" : "amber"})"><i></i><span>${
    esc([p.tool || "tool", p.status || "?", p.title].filter(Boolean).join(" · "))}</span></div>`;
  if(p.type === "file") return `<div class="p-sy-part p-sel" style="--d:var(--mauve)"><i></i><span>${esc(p.filename || "File")}</span></div>`;
  return "";
}
function drawSession(toEnd){
  const s = findS(); if(!s || !ssheet) return;
  const [tag, tagc] = sTag(s), [done, total, doing] = sTodo(s), msgs = tail && tail.messages;
  const who = m => m.role === "user" ? "You" : toolName(toolOf(s));
  const at = m => { const c = m.created; if(!c) return ""; const ms = c > 1e11 ? c : c * 1000;       /* seconds from Claude Code, milliseconds from opencode */
    return new Date(ms).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false}); };
  const b = ssheet.body, near = b.scrollHeight - b.scrollTop - b.clientHeight < 80;
  ssheet.set(`<div class="p-wrap" style="gap:6px;margin-top:8px">${H.tag(tag, tagc)}${H.tag(toolName(toolOf(s)))}</div>
    <div class="p-read"><div><b>${total ? `${done} of ${total}` : "–"}</b><span>Todos done</span></div><div><b>${doing}</b><span>In progress</span></div>
      <div><b>${esc(dur(s.updated_secs_ago))}</b><span>${s.stalled ? "Waiting" : "Last update"}</span></div></div>
    <div class="p-lab">Transcript${msgs && msgs.length ? `, last ${msgs.length} message${msgs.length > 1 ? "s" : ""}` : ""}</div>
    ${!tail ? `<div class="p-empty">Reading the transcript...</div>` : tail.error ? H.msg(tail.error, true) : !(msgs || []).length ? `<div class="p-empty">No messages yet</div>`
      : `<div class="p-sy-tr">${msgs.map((m, i) => `<div class="p-sy-msg${m.role === "user" ? " me" : ""}"><div class="p-sy-who">${who(m)}<small>${at(m)}</small></div>${
          (m.parts || []).map((p, j) => partHtml(p, `${m.created || i}:${j}`)).join("")}</div>`).join("")}</div>`}`);
  if(toEnd || (near && msgs)) b.scrollTop = b.scrollHeight;       /* a tail is read from its end */
}
async function loadTail(user){
  if(!scur || tailBusy === scur.key) return;       /* one request per session: a late answer of a closed sheet must not block the next */
  tailBusy = scur.key;
  const key = scur.key, s = scur.s, q = encodeURIComponent, first = !tail;
  try{ const j = await P.api.get(`/api/computer/sessions/tail?node=${q(s.node)}&id=${q(s.id)}&tool=${q(toolOf(s))}`, {prompt: user});   /* only a tap may ask for the key */
       if(scur && scur.key === key) tail = j || {error: "Fleet sent no transcript. Try again."}; }
  catch(e){ if(scur && scur.key === key && (user || !tail)) tail = {error: e.message}; }      /* a refresh that fails keeps the last transcript */
  if(tailBusy === key) tailBusy = false;
  if(ssheet && ssheet.open && scur && scur.key === key) drawSession(first);
}
P.on("sys.ses", key => {
  const s = P.sessionsOf().find(x => sKey(x) === key); if(!s) return;
  scur = {key, s}; tail = null; more.clear();
  ssheet = P.sheet({title: s.title || "Untitled session", sub: `${nn(s.node)} · ${s.directory || "no directory"}`, color: sEdge(s) || "mauve",
    onClose: () => { clearInterval(tailTimer); ssheet = null; scur = null; }});
  ssheet.el.classList.add("p-sy-tail"); keepCase(ssheet); drawSession(); loadTail(true);
  tailTimer = setInterval(() => { const x = findS(); if(x && x.generating && !document.hidden) loadTail(false); }, 10000);
});
P.on("sys.more", k => { if(!more.delete(k)) more.add(k); drawSession(); });

/* ================================================================ Main Engineering: containers on both Sparks */
/* "Idle after": one shared value for the Mac and the phone (fleet.py idle_hours, in /api/containers); it sets the amber
   marks and what Cleansing selects. Changing it is a keyed POST /api/engineering/idle {hours}. */
const idleHrs = () => (P.data.containers || {}).idle_hours || 24, idleLim = () => idleHrs() * 3600;
const hrsText = h => h >= 48 && h % 24 === 0 ? `${h / 24} days` : `${h} hour${h === 1 ? "" : "s"}`;
const CKIND = {gpu: ["Model", "pinned", "magenta"], session: ["Session", "ready", "blue"], service: ["Service", "", "mauve"]};   /* L1599 */
function cIdle(c){                                           /* L1600 as [text, over 24 h, what it means] */
  if(c.session_idle_s != null) return [`session idle ${dur(c.session_idle_s)}`, c.session_idle_s > idleLim(), `The session that owns it was last active ${dur(c.session_idle_s)} ago.`];
  if(c.cpu_idle_s == null) return ["", false, "Not measured yet."];
  if(c.cpu_idle_s < 60 && !c.cpu_idle_ge) return ["active", false, "Used CPU in the last minute."];
  return [`idle ${c.cpu_idle_ge ? "≥ " : ""}${dur(c.cpu_idle_s)}`, c.cpu_idle_s > idleLim(),
    `No CPU use for ${c.cpu_idle_ge ? "at least " : ""}${dur(c.cpu_idle_s)}${c.cpu_idle_ge ? ", counted since Fleet started watching" : ""}.`];
}
let cnode = "all", ckind = "", csheet = null, ckey = "";
const cbusy = new Set(), cid = c => c.node + "/" + c.name;
P.screens["systems/engineering"] = { tab: "systems", title: "Main Engineering", color: "amber", parent: "#/systems", needs: {containers: 10000},
  mount(el, arg){ cnode = arg || "all";
    el.innerHTML = `<div id="p-sy-ch"></div><div class="p-chips" id="p-sy-cn" style="margin-top:6px"></div><div class="p-chips" id="p-sy-ck"></div><div id="p-sy-cl" style="margin-top:2px"></div><div id="p-sy-ss"></div>`; },
  render(el){
    synced("containers", true);                                /* the long title leaves room for the time only */
    const list = el.querySelector("#p-sy-cl");
    if(wait(list, "containers")) return;
    const d = P.data.containers, cs = d.containers || [], errs = d.errors || {}, names = order([...new Set(["jean-luc", "kathryn", ...cs.map(c => c.node)])]);
    if(cnode !== "all" && !names.includes(cnode)) cnode = "all";
    const sum = (rows, k) => rows.reduce((a, c) => a + (c[k] || 0), 0);
    P.put(el.querySelector("#p-sy-ch"), `${lost("containers")}${Object.entries(errs).map(([n, e]) => H.msg(`${nn(n)} is not answering. Showing its last list. ${e}`, true)).join("")}
      ${H.sec("Running containers", cs.length)}<div class="p-nodes">${names.map(n => { const rows = cs.filter(c => c.node === n);
        return `<div class="p-node" style="--c:var(--${errs[n] ? "red" : "amber"})"><h3>${esc(nn(n))}</h3><div class="p-free">${rows.length}<small>running</small></div>
          <div class="p-of" style="margin-top:3px">${mb(sum(rows, "ram_mb"))} RAM · ${mb(sum(rows, "gpu_mb"))} GPU</div></div>`; }).join("")}</div>${cleanCard()}`);
    const inNode = cs.filter(c => cnode === "all" || c.node === cnode);
    P.put(el.querySelector("#p-sy-cn"), [["all", "All", cs.length], ...names.map(n => [n, nn(n), cs.filter(c => c.node === n).length])]
      .map(([f, label, n]) => H.chip({act: "sys.cnode", arg: f, label, n, on: f === cnode})).join(""));
    P.put(el.querySelector("#p-sy-ck"), Object.entries(CKIND).map(([k, [label]]) => H.chip({act: "sys.ckind", arg: k, label, n: inNode.filter(c => c.kind === k).length, on: k === ckind})).join(""));
    const shown = inNode.filter(c => !ckind || c.kind === ckind).sort((a, b) => ((b.ram_mb || 0) + (b.gpu_mb || 0)) - ((a.ram_mb || 0) + (a.gpu_mb || 0)));
    P.put(list, `<div class="p-list">${shown.length ? shown.map(c => {
      const [kl, kc, edge] = CKIND[c.kind] || [c.kind, "", ""], [idle, long] = cIdle(c), busy = cbusy.has(cid(c));
      const bits = [cnode === "all" ? esc(nn(c.node)) : "", c.gpu_mb ? "GPU " + mb(c.gpu_mb) : "", c.started ? "up " + dur(now() - c.started) : "",
        idle ? (long ? `<span class="p-sy-amb">${esc(idle)}</span>` : esc(idle)) : ""].filter(Boolean).join(" · ");
      return `<button class="p-row" type="button" data-act="sys.ctr" data-arg="${esc(cid(c))}" style="--c:var(--${long ? "amber" : edge || "grid"})">
        <span class="p-m"><span class="p-t">${esc(c.name)}</span><span class="p-s">${bits}</span><span class="p-s">${esc(c.owner || "")}</span></span>
        <span class="p-sy-fig">${fig(mb(c.ram_mb))}${H.tag(busy ? "Stopping" : kl, busy ? "load" : kc)}</span></button>`; }).join("")
      : `<div class="p-empty">${cs.length ? "No containers match" : "No running containers"}</div>`}</div>`);
    P.put(el.querySelector("#p-sy-ss"), sessionList());
    if(csheet && csheet.open) drawCtr();
    if(cleanSheet && cleanSheet.open) drawClean();
    if(ssheet1 && ssheet1.open) drawSes1();
  } };

/* ---------------------------------------------------------------- Cleansing: end idle agent sessions
   The desktop's Cleansing (fleet.html L1678-1740) on a phone, same rule and same route: every agent session and every
   stoppable container idle for more than the chosen time (never one whose idle time is unknown, never Fleet's own
   process) goes in ONE request to POST /api/engineering/stop {containers:[{node,name}], sessions:[{node,pid}]}, which
   needs the Starfleet key, ends the sessions (SIGTERM) and stops the containers. A session's own containers exit with it.
   fleet.py lists the sessions in /api/containers as `sessions`; a Fleet without that list shows none of this. */
const CLEAN_AFTER = [[2, "2 h"], [12, "12 h"], [24, "24 h"], [72, "3 days"], [168, "7 days"]];
let cleanSheet = null, cleanBusy = false, cleanAt = 0, ssheet1 = null, skey = "";
const sessOf = () => (P.data.containers || {}).sessions;
const idleOf = c => c.session_idle_s ?? c.cpu_idle_s;
const skeyOf = s => s.node + "/" + s.pid;
const sName = s => (s.tool || "Agent") + (s.session ? " · " + s.session : "");
const homeless = p => p ? p.replace(/^\/home\/[^/]+/, "~") : "";
function sIdle(s){                                           /* as [text, over 24 h] */
  if(s.idle_s == null) return ["idle time unknown", false];
  return s.idle_s < 60 ? ["active", false] : [`idle ${dur(s.idle_s)}`, s.idle_s > idleLim()];
}
function cleanPlan(hours){
  const d = P.data.containers || {}, lim = hours * 3600;
  const ss = (d.sessions || []).filter(s => !s.fleet && s.idle_s != null && s.idle_s > lim).sort((a, b) => (b.rss_mb + b.ct_ram_mb) - (a.rss_mb + a.ct_ram_mb));
  const cs = (d.containers || []).filter(c => c.stoppable && idleOf(c) != null && idleOf(c) > lim);
  const ended = new Set(ss.map(s => s.pid));
  const freed = ss.reduce((a, s) => a + s.rss_mb + s.ct_ram_mb, 0) + cs.filter(c => !ended.has(c.session_pid)).reduce((a, c) => a + (c.ram_mb || 0), 0);
  return {ss, cs, mb: freed, n: ss.length + cs.length};
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function cleanCard(){
  if(!Array.isArray(sessOf())) return "";
  const h = idleHrs(), p = cleanPlan(h);
  return `<div class="p-card" style="--c:var(--amber);margin-top:8px"><h3>Cleanse idle sessions</h3>
    <p>${p.n ? `${plural(p.ss.length, "agent session")} and ${plural(p.cs.length, "container")} have been idle for over ${esc(hrsText(h))}. Ending them frees about ${esc(mb(p.mb))}.`
      : `Nothing has been idle for over ${esc(hrsText(h))}.`}</p>
    <div class="p-pair" style="margin-top:11px"><button class="p-btn red" type="button" data-act="sys.copen"${p.n ? "" : " disabled"}>Review and cleanse</button></div></div>`;
}
function sessionList(){
  const ss = sessOf(); if(!Array.isArray(ss)) return "";
  const tot = ss.reduce((a, s) => a + s.rss_mb + s.ct_ram_mb, 0);
  return `${H.sec("Agent sessions", ss.length ? `${ss.length} · ${mb(tot)}` : "")}<div class="p-list">${ss.length ? ss.slice().sort((a, b) => (b.rss_mb + b.ct_ram_mb) - (a.rss_mb + a.ct_ram_mb)).map(s => {
    const [idle, long] = sIdle(s);
    return `<button class="p-row" type="button" data-act="sys.ses1" data-arg="${esc(skeyOf(s))}" style="--c:var(--${s.fleet ? "grid" : long ? "amber" : s.idle_s != null && s.idle_s < 60 ? "blue" : "grid"})">
      <span class="p-m"><span class="p-t">${esc(sName(s))}</span><span class="p-s">${[long ? `<span class="p-sy-amb">${esc(idle)}</span>` : esc(idle), s.containers ? plural(s.containers, "container") : ""].filter(Boolean).join(" · ")}</span>
        <span class="p-s p-sy-path">${esc(homeless(s.cwd))}</span></span>
      <span class="p-sy-fig">${fig(mb(s.rss_mb + s.ct_ram_mb))}</span></button>`; }).join("") : `<div class="p-empty">No agent sessions</div>`}</div>`;
}
function drawClean(){
  const f = cleanSheet.body && cleanSheet.body.querySelector("#p-sy-ih");
  if(f && document.activeElement === f) return;              /* never redraw under a finger that is typing */
  const h = idleHrs(), p = cleanPlan(h);
  cleanSheet.set(`<p class="p-text">Ends the agent sessions and stops the containers that have been idle for longer than the time below. The containers of an ended session stop with it. A session whose idle time is not known is never touched.</p>
    <div class="p-lab">Idle for more than</div>
    <label class="p-fieldbox" style="margin-top:6px"><input class="p-field" id="p-sy-ih" type="number" inputmode="numeric" enterkeyhint="done" min="1" max="720" step="1" value="${h}" aria-label="Idle after, in hours"><span style="color:var(--dim)">hours</span></label>
    <div class="p-wrap" style="margin-top:8px">${CLEAN_AFTER.map(([d, l]) => H.chip({act: "sys.cdays", arg: String(d), label: l, cls: d === h ? "set" : ""})).join("")}</div>
    <p class="p-note">Shared with the Mac. Changing it needs the Starfleet key.</p>
    <div class="p-read"><div><b>${p.ss.length}</b><span>Sessions</span></div><div><b>${p.cs.length}</b><span>Containers</span></div><div><b>${esc(mb(p.mb))}</b><span>Frees about</span></div></div>
    ${p.ss.length ? `<div class="p-grp">Largest sessions</div><div class="p-list">${p.ss.slice(0, 4).map(s => info({c: "amber", t: sName(s), lines: [`${esc(sIdle(s)[0])} · ${esc(mb(s.rss_mb + s.ct_ram_mb))}`]})).join("")}</div>${p.ss.length > 4 ? `<p class="p-note">and ${p.ss.length - 4} more</p>` : ""}` : ""}
    <p class="p-note">An ended session stops running. Its saved conversation stays on disk.</p>
    <div class="p-pair p-acts"><button class="p-btn red" type="button" data-act="sys.cgo"${p.n && !cleanBusy ? "" : " disabled"}>${cleanBusy ? "Cleansing..." : p.n ? `Cleanse ${plural(p.n, "item")}` : "Nothing to cleanse"}</button></div>`);
  const ih = cleanSheet.body.querySelector("#p-sy-ih");
  if(ih) ih.addEventListener("change", () => { ih.blur(); setIdle(Number(ih.value)); });
}
let idleSending = null;
async function setIdle(hours){
  if(!(hours >= 1 && hours <= 720)){ P.toast("Idle after: 1 to 720 hours.", "err"); if(cleanSheet) drawClean(); return; }
  if(hours === idleSending || (idleSending == null && hours === idleHrs())) return;   /* the field's change fires again on blur */
  idleSending = hours;
  try{
    const j = await P.api.post("/api/engineering/idle", {hours});
    if(P.data.containers) P.data.containers.idle_hours = j.idle_hours;
    P.haptic("select"); P.toast(`Idle after ${hrsText(j.idle_hours)}, on the Mac too.`);
  }catch(e){ if(!e.quiet) P.toast("Idle after: " + e.message, "err"); }
  idleSending = null; if(cleanSheet) drawClean(); P.paint();
}
P.on("sys.copen", () => { cleanAt = Date.now();
  cleanSheet = P.sheet({title: "Cleanse idle sessions", color: "red", onClose: () => { cleanSheet = null; }}); drawClean(); });
P.on("sys.cdays", d => setIdle(Number(d)));
/* one request for everything; what the sheet shows is what is sent, and a tap in the first moments is ignored */
async function stopRequest(ss, cs){
  const j = await P.api.post("/api/engineering/stop", {containers: cs.map(c => ({node: c.node, name: c.name})), sessions: ss.map(s => ({node: s.node, pid: s.pid}))}, {timeout: 240000});
  P.haptic("success");
  P.toast(`Ended ${plural((j.ended || []).length, "session")}, stopped ${plural((j.stopped || []).length, "container")}, freed about ${mb(j.freed_mb || 0)}.`);
  const bad = [...(j.refused || []), ...(j.errors || [])];
  if(bad.length) P.toast(`${bad.length} not stopped: ${bad.slice(0, 3).map(b => `${b.name || "pid " + b.pid} (${b.why})`).join(", ")}.`, "err");
}
P.on("sys.cgo", () => P.run(async () => {
  if(cleanBusy || Date.now() - cleanAt < 450) return;
  const p = cleanPlan(idleHrs()); if(!p.n) return;
  cleanBusy = true; drawClean();
  try{ await stopRequest(p.ss, p.cs); if(cleanSheet) cleanSheet.close(); }
  catch(e){ fail("Cleansing did not finish", e); }
  finally{ cleanBusy = false; if(cleanSheet) drawClean(); P.refresh(["containers"]); P.paint(); }
}));
/* one session */
function drawSes1(){
  const s = (sessOf() || []).find(x => skeyOf(x) === skey);
  if(!s){ ssheet1.set(`<div class="p-empty" style="margin-top:12px">This session is no longer running.</div>`); return; }
  const [idle, long] = sIdle(s);
  ssheet1.set(`<div class="p-wrap" style="gap:6px;margin-top:8px">${H.tag(s.tool || "Agent", "ready")}${long ? H.tag("Idle over 24 h", "warn") : ""}${s.fleet ? H.tag("Fleet itself") : ""}</div>
    <div class="p-read"><div><b>${esc(mb(s.rss_mb + s.ct_ram_mb))}</b><span>Memory</span></div><div><b>${s.containers || 0}</b><span>Containers</span></div><div><b>${esc(s.idle_s == null ? "–" : s.idle_s < 60 ? "now" : dur(s.idle_s))}</b><span>Idle</span></div></div>
    ${dl([["Session", s.session], ["Model", s.model], ["Started", fmtT(s.started)], ["Folder", homeless(s.cwd)], ["Process", "pid " + s.pid], ["Idle", s.idle_s == null ? "Not known: Cleansing never ends this one." : idle]])}
    ${s.fleet ? `<p class="p-note">This is the process that runs Fleet. It cannot be ended here.</p>`
      : `<div class="p-pair p-acts"><button class="p-btn red" type="button" data-act="sys.cend">End this session</button></div>`}`);
}
P.on("sys.ses1", k => { const s = (sessOf() || []).find(x => skeyOf(x) === k); if(!s) return;
  skey = k; ssheet1 = P.sheet({title: sName(s), sub: homeless(s.cwd), color: "amber", onClose: () => { ssheet1 = null; }}); keepCase(ssheet1); drawSes1(); });
P.on("sys.cend", () => P.run(async () => {
  const s = (sessOf() || []).find(x => skeyOf(x) === skey); if(!s || s.fleet) return;
  if(!await P.confirm({title: "End this session?", sub: sName(s), danger: true, yes: "End session",
    text: `The process is asked to stop and its ${s.containers ? plural(s.containers, "container") : "containers"} exit with it. Its saved conversation stays on disk.`})) return;
  if(ssheet1) ssheet1.close();
  try{ await stopRequest([s], []); }catch(e){ fail("Could not end the session", e); }
  finally{ P.refresh(["containers"]); P.paint(); }
}));
P.on("sys.cnode", n => { cnode = n; P.haptic("select"); P.paint(); });
P.on("sys.ckind", k => { ckind = ckind === k ? "" : k; P.haptic("select"); P.paint(); });
function drawCtr(){
  const c = ((P.data.containers || {}).containers || []).find(x => cid(x) === ckey);
  if(!c){ csheet.set(`<div class="p-empty" style="margin-top:12px">This container is no longer running.</div>`); return; }
  const [kl, kc] = CKIND[c.kind] || [c.kind, ""], [, long, why] = cIdle(c), busy = cbusy.has(ckey);
  csheet.set(`<div class="p-wrap" style="gap:6px;margin-top:8px">${H.tag(kl, kc)}${H.tag(nn(c.node))}${long ? H.tag("Idle over 24 h", "warn") : ""}</div>
    <div class="p-read"><div><b>${mb(c.ram_mb)}</b><span>RAM</span></div><div><b>${mb(c.gpu_mb)}</b><span>GPU memory</span></div>
      <div><b>${c.started ? esc(dur(now() - c.started)) : "–"}</b><span>Running for</span></div></div>
    ${dl([["Launched by", c.owner], ["Started", fmtT(c.started)], ["Idle", why], ["Project", c.project || "none"]])}
    ${c.stoppable ? `<div class="p-pair p-acts"><button class="p-btn red" type="button" data-act="sys.cstop"${busy ? " disabled" : ""}>${busy ? "Stopping" : "Stop"}</button></div>`
      : c.kind !== "gpu" ? `<p class="p-note">Fleet does not stop this container.</p>`
      : `<p class="p-note">This is a model server. Unload it on the Models screen, not here.</p>
         <div class="p-pair p-acts"><button class="p-btn ghost" type="button" data-act="go" data-arg="#/models">Open Models</button></div>`}`);
}
P.on("sys.ctr", k => { const c = ((P.data.containers || {}).containers || []).find(x => cid(x) === k); if(!c) return;
  ckey = k; csheet = P.sheet({title: c.name, sub: c.image, color: (CKIND[c.kind] || [])[2] || "amber", onClose: () => { csheet = null; }});
  keepCase(csheet); drawCtr(); });
P.on("sys.cstop", () => P.run(async () => {
  const k = ckey, i = k.indexOf("/"), node = k.slice(0, i), name = k.slice(i + 1);
  if(cbusy.has(k)) return;
  if(!await P.confirm({title: "Stop this container?", sub: `${name} on ${nn(node)}`, danger: true, yes: "Stop",
    text: "Docker gives it 10 seconds to shut down. It stays down until whatever launched it starts it again."})) return;
  if(csheet) csheet.close();
  cbusy.add(k); P.paint();
  try{ const j = await P.api.post("/api/containers/stop", {node, name}, {timeout: 120000}); P.haptic("success");
       P.toast(`${name} stopped${j && j.freed_mb != null ? `: ${j.freed_mb} MB freed` : ""}.`); }
  catch(e){ fail(`Could not stop ${name}`, e); }
  finally{ cbusy.delete(k); P.refresh(["containers"]); P.paint(); }
}));

/* ================================================================ Deep Space NAS: hardware and containers, without the station */
const NAS_FROZEN = ["kopia"];                                 /* L1567: same list as FROZEN in nas_gate.py */
function nasKind(c){                                           /* L1442: [level, label] from Container Manager's State and Status */
  const st = c.Status || "";
  if(c.State === "running") return /unhealthy/.test(st) ? ["bad", "unhealthy"] : /starting/.test(st) ? ["warn", "starting"] : ["ok", /healthy/.test(st) ? "healthy" : "running"];
  if(c.State === "restarting" || c.State === "paused") return ["warn", c.State];
  if(c.State === "exited" && /Exited \(0\)/.test(st)) return ["idle", "finished"];   /* one-shot jobs (migrate, init) */
  return ["bad", c.State || "stopped"];
}
const NK = {ok: ["blue", "ready"], warn: ["amber", "warn"], bad: ["red", "fault"], idle: ["", ""]};
const proj = c => c.Names.split(/[-_]/)[0];
const nbusy = {};
let nsheet = null, nname = "";
function hwPanel(){                                            /* L1537 */
  const d = P.data.nasstats, s = d && d.stats;
  if(!s) return `<div class="p-card" style="--c:var(--blue)"><p style="margin:0" class="p-sel">${esc(d && d.error ? "No hardware figures: " + d.error : !d && P.err.nasstats ? P.err.nasstats : "Waiting for the NAS")}</p></div>`;
  const ramP = 100 * s.mem_used_mb / s.mem_total_mb, hot = (s.temps || []).reduce((m, t) => t.c > m ? t.c : m, -Infinity);
  const big = Math.max(s.net_rx_bps, s.net_tx_bps) >= 1e6, nf = b => big ? (b / 1e6).toFixed(1) : (b / 1e3).toFixed(0);
  const lvl = (p, w, c) => p >= c ? "red" : p >= w ? "amber" : "";
  const row = (l, p, v, c) => `<div class="p-sy-hw"${c ? ` style="--g:var(--${c})"` : ""}><span>${l}</span>${H.gauge(p, 20)}<b>${v}</b></div>`;
  const up = s.uptime_s, ut = up >= 86400 ? `${Math.floor(up / 86400)} d ${Math.floor(up % 86400 / 3600)} h` : `${Math.floor(up / 3600)} h ${Math.floor(up % 3600 / 60)} min`;
  return `<div class="p-card" style="--c:var(--blue)">
    ${row("CPU", s.cpu_pct, s.cpu_pct.toFixed(0) + " %", lvl(s.cpu_pct, 70, 90))}
    ${row("RAM", ramP, `${(s.mem_used_mb / 1024).toFixed(1)} / ${(s.mem_total_mb / 1024).toFixed(1)} GB`, lvl(ramP, 80, 92))}
    ${isFinite(hot) ? row("Temp", hot, hot.toFixed(0) + " °C", lvl(hot, 65, 80)) : row("Temp", 0, "No sensor", "")}
    ${row("Net", 100 * Math.max(s.net_rx_bps, s.net_tx_bps) / 125e6, `↓${nf(s.net_rx_bps)} ↑${nf(s.net_tx_bps)} ${big ? "MB/s" : "kB/s"}`, "")}
    <div class="p-of" style="margin-top:8px">Load ${esc(s.load[0])} · up ${ut} · ${esc(s.disk.free_gb)} of ${esc(s.disk.total_gb)} GB free</div></div>`;
}
P.screens["systems/nas"] = { tab: "systems", title: "Deep Space NAS", color: "blue", parent: "#/systems", needs: {nas: 10000, nasstats: 10000},
  render(el){
    synced("nas");
    const d = P.data.nas, h = P.data.nasstats;
    let html = H.sec("Hardware", h && h.error ? "Stale" : ""), hw;
    try{ hw = hwPanel(); }catch(e){ hw = `<div class="p-card" style="--c:var(--blue)"><p style="margin:0">The hardware figures could not be read.</p></div>`; }
    html += hw;
    if(!d) html += P.err.nas ? H.msg(P.err.nas, true) : empty("Checking...");
    else {
      const cs = d.containers || [], g = {};
      cs.forEach(c => (g[proj(c)] = g[proj(c)] || []).push(c));
      html += lost("nas") + (d.error ? H.msg(`The NAS is not answering. Showing the last list. ${d.error}`, true) : "");
      html += Object.keys(g).sort((a, b) => g[b].length - g[a].length || a.localeCompare(b)).map(k =>   /* biggest project first, ties by name */
        H.sec(k, `${g[k].filter(c => c.State === "running").length} of ${g[k].length} up`) + `<div class="p-list">${g[k].map(c => { const [lv, label] = nasKind(c), b = nbusy[c.Names];
          return H.row({act: "sys.nas", arg: c.Names, c: NK[lv][0], t: c.Names, s: c.Status || c.State, tag: b || label, tagc: b ? "load" : NK[lv][1]}); }).join("")}</div>`).join("");
      if(!cs.length) html += empty("No containers on the NAS");
    }
    P.put(el, html);
    if(nsheet && nsheet.open) drawNas();
  } };
function drawNas(){
  const c = ((P.data.nas || {}).containers || []).find(x => x.Names === nname);
  if(!c){ nsheet.set(`<div class="p-empty" style="margin-top:12px">This container is no longer on the NAS.</div>`); return; }
  const [lv, label] = nasKind(c), b = nbusy[c.Names], on = c.State === "running" || c.State === "restarting";
  const gone = ["exited", "created", "dead"].includes(c.State), frozen = NAS_FROZEN.includes(proj(c));
  const btn = (a, t, cls, off) => `<button class="p-btn${cls}" type="button" data-act="sys.nact" data-arg="${a}"${b || off ? " disabled" : ""}>${t}</button>`;
  nsheet.set(`<div class="p-wrap" style="gap:6px;margin-top:8px">${H.tag(b || label, b ? "load" : NK[lv][1])}${H.tag(proj(c))}</div>
    ${dl([["State", c.State], ["Status", c.Status], ["Project", proj(c)]])}
    <div class="p-pair p-acts">${on ? btn("stop", "Stop", " red") + btn("restart", "Restart", "") : (gone ? btn("rm", "Delete", " red", frozen) : "") + btn("start", "Start", "")}</div>
    ${on || !gone ? "" : `<p class="p-note">${frozen ? "Delete is off: this project is frozen and the NAS gate never deletes it. Delete it in DSM Container Manager."
      : "Delete removes the stopped container. Its volumes stay."}</p>`}`);
}
P.on("sys.nas", name => { const c = ((P.data.nas || {}).containers || []).find(x => x.Names === name); if(!c) return;
  nname = name; nsheet = P.sheet({title: name, sub: c.Image, color: NK[nasKind(c)[0]][0] || "mauve", onClose: () => { nsheet = null; }});
  keepCase(nsheet); drawNas(); });
const NACT = {start: ["Starting", "started"], stop: ["Stopping", "stopped"], restart: ["Restarting", "restarted"], rm: ["Deleting", "deleted"]};
const NASK = {
  stop: {title: "Stop this container?", yes: "Stop", danger: true, text: "It stays down until it is started again, here or in DSM Container Manager."},
  restart: {title: "Restart this container?", yes: "Restart", text: "The NAS stops it and starts it again. What it serves is away for a moment."},
  rm: {title: "Delete this container?", yes: "Delete", danger: true, text: "The stopped container is removed from the NAS. Its volumes stay."} };
P.on("sys.nact", action => P.run(async () => {
  const name = nname;
  if(!NACT[action] || nbusy[name]) return;
  /* this route takes no Starfleet key: the sheet is the only guard, and only Start goes without it */
  if(action !== "start" && !await P.confirm(Object.assign({sub: name}, NASK[action]))) return;
  if(nsheet) nsheet.close();
  nbusy[name] = NACT[action][0]; P.paint();
  try{ await P.api.post("/api/nas/control", {target: name, action}, {key: false, timeout: 340000});   /* the gate may take 330 s */
       P.haptic("success"); P.toast(`${name} ${NACT[action][1]}.`); }
  catch(e){ fail(`${NACT[action][0]} ${name} failed`, e); }
  finally{ delete nbusy[name]; P.refresh(["nas"]); P.paint(); }
}));

/* ================================================================ Memory Alpha: the off-site backup */
let mabusy = false;
P.screens["systems/alpha"] = { tab: "systems", title: "Memory Alpha", color: "mauve", parent: "#/systems", needs: {alpha: 15000},
  render(el){
    synced("alpha");
    if(wait(el, "alpha")) return;
    const d = P.data.alpha, L = d.last || {}, mt = d.maintenance || {}, pol = d.policy || {}, k = pol.keep || {}, man = d.manual || {};
    const [tag, tagc] = stateTag(d.status), running = man.state === "running", edge = d.status === "ok" ? "mauve" : d.status === "warn" ? "amber" : "red";
    const note = !d.trigger_ready ? "Needs the kopia-snapshot container: rebuild the kopia project once in DSM Container Manager."
      : running ? `Started ${ago(man.started)}. Snapshots take 2 to 6 minutes.`
      : man.started ? `Last manual run ${ago(man.started)}: ${man.exit === 0 ? "completed" : man.exit != null ? `failed (exit ${man.exit})` : "no result"}${man.error ? " · " + man.error : ""}` : "No manual run yet.";
    P.put(el, `<div class="p-sy-state">${H.tag(tag, tagc)}<span class="p-s">Checked ${esc(fmtT(d.checked))}</span></div>
      ${lost("alpha")}${notes(d)}
      ${H.sec("Off-site backup")}<div class="p-sy-pans">
      ${pan(edge, "Last snapshot", L.end ? `${esc(dur(now() - L.end))}<small>ago</small>` : "–",
        (L.end ? `${fmtT(L.end)} · took ${dur(L.end - L.start)}` : "No snapshot reported") + (L.description ? " · " + L.description : ""),
        [["Protected", fmtB(L.size)], ["Files", fmtN(L.files)], ["Folders", fmtN(L.dirs)], ["Errors", L.errors ?? "–"]])}
      ${pan("mauve", "Stored in Memory Alpha", d.stored ? esc(fmtB(d.stored.bytes)) : "–", d.stored ? `${fmtN(d.stored.count)} blobs, deduplicated and compressed` : "Reported by kopia-check after the DSM rebuild",
        [["Object lock", !d.maintenance ? "–" : mt.locks ? "Renewed" : "Off"], ["Full maintenance", until(mt.next_full)], ["Quick maintenance", "hourly"]])}
      ${pan("mauve", "Schedule", d.next_snapshot ? esc(until(d.next_snapshot)) : "–", `${(pol.times || []).length ? "Daily at " + pol.times.join(", ") : "No snapshot time set"} · next ${fmtT(d.next_snapshot)}`,
        [["Keep latest", k.latest], ["Daily", k.daily], ["Monthly", k.monthly], ["Annual", k.annual]])}
      <div class="p-card" style="--c:var(--mauve)"><h3>Back up now</h3>
        <div class="p-pair" style="margin-top:11px"><button class="p-btn" type="button" data-act="sys.backup"${!d.trigger_ready || running || mabusy ? " disabled" : ""}>${running ? "Snapshot running" : mabusy ? "Starting" : "Back up now"}</button></div>
        <p class="p-note p-sel">${esc(note)}</p></div></div>
      ${H.sec("Backup containers on the NAS", (d.containers || []).length || "")}
      <div class="p-list">${(d.containers || []).map(c => info({c: c.state === "running" ? "blue" : "", t: c.name, lines: [esc(c.status)], tag: c.state, tagc: c.state === "running" ? "ready" : ""})).join("")
        || `<div class="p-empty">No containers reported</div>`}</div>`);
  } };
P.on("sys.backup", () => P.run(async () => {
  if(mabusy) return;
  if(!await P.confirm({title: "Back up now?", sub: "Memory Alpha", yes: "Start snapshot",
    text: "Kopia on the NAS snapshots the whole volume into Memory Alpha. Snapshots take 2 to 6 minutes."})) return;
  mabusy = true; P.paint();
  try{ await P.api.post("/api/memoryalpha/backup", {}, {timeout: 120000}); P.haptic("success"); P.toast("Snapshot started."); }
  catch(e){ fail("Back up now failed", e); }
  finally{ mabusy = false; P.refresh(["alpha"]); P.paint(); }
}));

/* ================================================================ Defiant: the web server and its package updates */
const siteBad = s => !!(s.error || !s.status || (s.status >= 400 && s.status !== 401));    /* 401 = password-protected on purpose */
P.screens["systems/defiant"] = { tab: "systems", title: "Defiant", color: "peach", parent: "#/systems", needs: {defiant: 15000, updates: 30000},
  render(el){
    synced("defiant");
    if(upLive(P.data.updates) && !upTimer) upTimer = setTimeout(() => { upTimer = 0; P.refresh(["updates"]); }, 3000);     /* a scan or the install is on its way: ask again in 3 s (after Back a late timer reads nothing: only the open screen's sources are polled) */
    if(wait(el, "defiant")) return;
    const d = P.data.defiant;
    if(d.configured === false){ P.put(el, `<div class="p-sy-state">${H.tag("Not configured")}</div><div class="p-card" style="--c:var(--peach)"><h3>Defiant is not set up</h3>
      <p class="p-sel">Set FLEET_DEFIANT_HOST, the ssh target of the web server, in the Fleet service environment to enable this view.</p></div>`); return; }
    const [tag, tagc] = stateTag(d.status), memU = d.mem_total_kb ? (1 - d.mem_avail_kb / d.mem_total_kb) * 100 : null, diskU = d.disk_total ? d.disk_used / d.disk_total * 100 : null;
    const sc = p => p == null ? "" : p > 90 ? "red" : p > 80 ? "amber" : "", gbi = kb => kb ? (kb / 1048576).toFixed(1) : "–";
    const hostC = d.status === "ok" ? "peach" : d.status === "warn" ? "amber" : "red", cpuC = sc(Math.max(d.cpu_pct || 0, memU || 0)), diskC = sc(diskU);
    const sites = d.sites || [], ctrs = d.containers || [], upn = d.upd ? d.upd.count : d.updates;       /* the scan's count (the probe's also counts what phasing holds back) until there is a scan */
    if(d.host == null){ P.put(el, `<div class="p-sy-state">${H.tag(tag, tagc)}</div>${lost("defiant")}${notes(d) || empty("The web server sent no figures")}${rbCard()}${updBlock()}`); return; }   /* the probe failed: its error, and the install and the reboot still in view (the upgrade can restart the network, a reboot takes the probe down) */
    P.put(el, `<div class="p-sy-state">${H.tag(tag, tagc)}<span class="p-s">${esc(d.os || "")}${d.kernel ? " · kernel " + esc(d.kernel) : ""}</span></div>
      ${lost("defiant")}${notes(d)}
      ${d.reboot_required ? H.msg(`Reboot required.${upn ? ` ${plural(upn, "update")} ${upn === 1 ? "is" : "are"} pending.` : ""}`) : ""}
      ${H.sec("Server")}<div class="p-sy-pans">
      ${pan(hostC, d.host || "Defiant", d.uptime_s ? `${esc(dur(d.uptime_s))}<small>up</small>` : "–", "",
        [["Cores", d.cpus || "–"], ["Load", String(d.load || "–").split(" ")[0]], ["Updates", upn ?? "–"], ["Reboot", d.reboot_required ? "Required" : "No"]], "", rbBlock())}
      <div class="p-card" style="--c:var(--${cpuC || "peach"})"><h3>CPU and memory</h3><div class="p-free">${esc(d.cpu_pct ?? "–")}<small>% CPU</small></div>${gauge(d.cpu_pct, sc(d.cpu_pct))}
        <div class="p-s wrap">Memory ${memU == null ? "–" : memU.toFixed(0) + " % used"} · ${gbi(d.mem_avail_kb)} of ${gbi(d.mem_total_kb)} GiB free</div>${gauge(memU, sc(memU))}</div>
      ${pan(diskC || "peach", "Disk and network", `${esc(fmtB(d.disk_used))}<small>of ${esc(fmtB(d.disk_total))}</small>`, "",
        [["Free", fmtB(d.disk_avail)], ["In", rate(d.rx_bps)], ["Out", rate(d.tx_bps)]], gauge(diskU, diskC))}</div>
      ${updBlock()}
      ${H.sec("Sites", sites.length || "")}<div class="p-list">${sites.map(s => { const bad = siteBad(s), soon = s.cert_days != null && s.cert_days < 14;
        return info({c: bad ? "red" : soon ? "amber" : "blue", t: s.domain, e: s.error, tag: s.status ? s.status + (s.status === 401 ? " protected" : "") : "down", tagc: bad ? "fault" : "ready",
          lines: [[s.ms == null ? "" : s.ms + " ms", s.cert_days == null ? "" : `<span${soon ? ' class="p-sy-amb"' : ""}>certificate ${esc(s.cert_days)} days</span>`].filter(Boolean).join(" · "),
            s.deployed ? "Last deploy " + esc(ago(s.deployed)) : ""]}); }).join("") || `<div class="p-empty">No sites found</div>`}</div>
      ${H.sec("Containers", ctrs.length || "")}<div class="p-list">${ctrs.map(c => info({c: c.state === "running" ? "blue" : "red", t: c.name, lines: [esc(c.status)], tag: c.state, tagc: c.state === "running" ? "ready" : "fault"})).join("")
        || `<div class="p-empty">No containers</div>`}</div>`);
  } };

/* ---- package updates (fleet.html, Updates block: same rules, same words). Fleet runs an apt simulation on Defiant every
   hour and keeps the result in memory, so /api/defiant/updates is cheap. While a scan, the install or a reboot runs, or none
   has run yet (and for up to 90 s after an install that left a reboot due), the screen asks again every 3 s. Package names,
   versions and log lines come from the server: escaped everywhere.
   The reboot ("reboot" in the same answer, see the Reboot Defiant block below) is idle, running, done or failed; an install
   that needs a reboot starts one by itself (reboot.auto), but only for a request that says reboot_ok, as this screen's does. */
const UP_SVC = /^(docker|containerd|tailscale)/, UP_BOOT = /^(linux-image|libc6$)/;      /* restart services / need a reboot */
const UP_ROWS = 12;                                                                       /* a phone lists the first 12, security first; the Mac page lists them all */
const upName = x => typeof x === "string" ? x : x && x.name != null ? x.name : "";     /* a removal may arrive as a name or as an object */
const upSome = (l, n) => l.length > n ? `${l.slice(0, n).join(", ")}, +${l.length - n} more` : l.join(", ");
const upFam = (re, l) => [...new Set(l.map(x => (String(x).match(re) || [])[1]).filter(Boolean))];
let sgrace = 0, igrace = 0, rgrace = 0, smark = "", imark = 0, rmark = 0, upTimer = 0;     /* a scan / the install / a reboot counts as starting until this time: forever while its request is out, 8 s after Fleet accepted it */
const going = t => Date.now() < t;
const upMark = u => u && u.scan ? `${u.scan.at}|${u.scan.error_at}` : "";     /* changes when a scan ends */
const sgoing = u => going(sgrace) && upMark(u) === smark;     /* a scan asked for here is running until its result shows, not for the whole grace */
const igoing = u => going(igrace) && ((u.job || {}).started_at || 0) === imark;     /* an install asked for here is starting until Fleet shows its job (a new start time) */
const rbRun = u => !!u && !!u.reboot && u.reboot.state === "running";
const rbWait = u => { const j = u.job || {}; return !!u.reboot && u.reboot.state === "idle" && j.state === "done" && j.reboot_required === true && (u.now || 0) - (j.ended_at || 0) < 90; };     /* an install just ended with a reboot due: Fleet starts it by itself within seconds, ask again soon */
const rgoing = u => going(rgrace) && ((u.reboot || {}).started_at || 0) === rmark;     /* a reboot asked for here is starting until Fleet shows it (a new start time) */
const upLive = u => !!u && u.configured !== false && (!u.scan || u.scan.busy || (u.job || {}).state === "running" || sgoing(u) || igoing(u) || rbRun(u) || rgoing(u) || rbWait(u));
const upWhy = (u, sc) => (u.job || {}).state === "running" ? "An install is running, progress below." : rbRun(u) || rgoing(u) ? "A reboot is running, progress above." : igoing(u) ? "Starting the install..."
  : sc.busy || sgoing(u) ? "A scan is running, try again in a minute." : !sc.at ? "No scan yet: scan first." : !sc.count ? "Nothing to install." : "";     /* Fleet answers 409 to an install during a scan */
const upLog = (log, bad) => { const l = (log || []).slice(-10); return l.length ? `<div class="p-sy-log p-sel${bad ? " err" : ""}">${l.map(esc).join("\n")}</div>` : ""; };
const upRow = p => info({c: p.security ? "amber" : p.third_party ? "mauve" : "blue", t: p.name, wrap: true, tag: p.security ? "Security" : "", tagc: "warn",
  lines: [`<span class="p-sy-path">${p.from ? `${esc(p.from)} → ${esc(p.to)}` : `new ${esc(p.to)}`}</span>`,
    `<span class="p-sy-path">${esc((p.suites || []).join(", "))}${p.third_party ? ` · third party${p.origin ? ` (${esc(p.origin)})` : ""}` : ""}</span>`]});
function upJob(j, reboot){      /* reboot: the latest scan still wants one and none is running */
  const at = j.started_at ? `Started ${hm(j.started_at)}` : "";
  if(j.state === "running") return `<div class="p-card" style="--c:var(--${j.lost ? "amber" : "peach"})"><h3>Installing updates</h3>
    <div class="p-free">${j.started_at ? `${esc(dur(now() - j.started_at))}<small>running</small>` : "..."}</div><div class="p-track indet"><i></i></div>
    <div class="p-s wrap p-sel">${at} · log refreshes every 3 seconds</div>
    ${j.lost ? H.msg("Lost contact with Defiant, retrying. The install keeps running there.") : ""}
    ${upLog(j.log) || `<div class="p-s" style="margin-top:8px">Waiting for the first line of output...</div>`}</div>`;
  if(j.state === "done") return `<div class="p-card" style="--c:var(--blue)"><h3>Install finished</h3>
    <div class="p-free">${j.installed != null ? `${esc(j.installed)}<small>installed</small>` : "Done"}</div>
    <div class="p-s wrap p-sel">${j.ended_at ? `${ago(j.ended_at)} · ` : ""}reboot required afterwards: ${j.reboot_required == null ? "unknown" : j.reboot_required ? "yes" : "no"}</div>
    ${reboot ? `<p>A reboot is waiting. Use Reboot Defiant when it suits you.</p>` : ""}</div>`;
  if(j.state === "failed") return `<div class="p-card" style="--c:var(--red)"><h3>Install failed</h3>
    <div class="p-free" style="color:var(--red)">${j.exit != null ? `exit ${esc(j.exit)}` : "no exit code"}</div>
    <div class="p-s wrap p-sel">${at}${j.ended_at ? ` · ended ${ago(j.ended_at)}` : ""}</div>
    ${upLog(j.log, true) || `<div class="p-s" style="margin-top:8px">No log lines.</div>`}</div>`;
  return "";
}
function updBlock(){
  const u = P.data.updates;
  if(!u) return H.sec("Updates") + (P.err.updates ? H.msg(P.err.updates, true) : empty("Checking..."));
  if(u.configured === false) return "";
  const s = u.scan || {}, j = u.job || {}, ok = !!s.at, run = j.state === "running", pk = s.packages || [], gone = (s.remove || []).map(upName), bp = s.reboot_pkgs || [];
  const n = +s.count || 0, sec = +s.security || 0, held = (s.deferred || []).length, scanning = !!s.busy || sgoing(u), why = upWhy(u, s);
  const shown = pk.slice(0, UP_ROWS), rest = n - shown.length, restSec = pk.slice(UP_ROWS).filter(p => p.security).length;
  return `<div class="p-sy-up">${H.sec("Updates", ok ? `${n} pending` : "")}${lost("updates")}
    ${s.error ? H.msg(sent(`Scan failed${s.error_at ? " " + ago(s.error_at) : ""}${ok ? ", showing the scan from " + hm(s.at) : ""}: ${s.error}`)) : ""}
    <div class="p-card" style="--c:var(--${sec || s.reboot_required || s.error ? "amber" : "peach"})"><h3>Pending updates</h3>
      ${ok ? `<div class="p-free">${n}<small>to install</small></div>` : ""}
      ${scanning ? `<div class="p-track indet"><i></i></div><div class="p-s">Scanning...</div>` : ""}
      ${ok ? `<div class="p-kv"><div><b${sec ? ' class="p-sy-amb"' : ""}>${sec}</b><span>Security</span></div><div><b>${held}</b><span>Held back</span></div>${
        gone.length ? `<div><b>${gone.length}</b><span>Remove</span></div>` : ""}<div><b${s.reboot_required ? ' class="p-sy-amb"' : ""}>${s.reboot_required ? "Required" : "No"}</b><span>Reboot</span></div></div>` : ""}
      <div class="p-s wrap p-sel">${ok ? `Last scan ${hm(s.at)} (${ago(s.at)})${s.next_at ? ` · next ${hm(s.next_at)}` : ""}` : s.error ? `No scan yet. Fleet tries again at ${hm(s.next_at)}.` : "No scan yet. The first one runs shortly after Fleet starts."}</div>
      ${held ? `<p>${held} held back by Ubuntu's staged rollout: Ubuntu releases them in steps, they follow by themselves.</p>` : ""}
      ${s.reboot_required && bp.length ? `<p>Reboot needed for ${esc(upSome(bp, 4))}.</p>` : ""}</div>
    <div class="p-pair" style="margin-top:8px"><button class="p-btn ghost" type="button" data-act="sys.scan"${scanning || run || rbRun(u) ? " disabled" : ""}>${scanning ? "Scanning" : "Scan now"}</button>
      <button class="p-btn" type="button" data-act="sys.install"${why ? " disabled" : ""}>${igoing(u) && !run ? "Starting" : run ? "Installing" : "Install updates"}</button></div>
    <p class="p-note p-sel">${esc(why || "Fleet scans every hour and sends security updates and a pending reboot once to Telegram. Install needs the Starfleet key. If a reboot is needed afterwards, Fleet reboots Defiant.")}</p>
    ${upJob(j, s.reboot_required && !rbRun(u))}${j.state === "done" && rbOwn(u.reboot, j) ? rbLine(u.reboot) : ""}
    ${gone.length ? H.msg(`The install removes ${plural(gone.length, "package")}: ${upSome(gone, 6)}.`) : ""}
    ${pk.length ? `${H.sec("Packages", n)}<div class="p-list">${shown.map(upRow).join("")}${rest > 0 ? info({t: `+${rest} more`, lines: [esc(restSec ? `${restSec} of them security updates` : "No security updates among them")]}) : ""}</div>` : ""}</div>`;
}
P.on("sys.scan", () => P.run(async () => {
  if(sgoing(P.data.updates)) return;
  smark = upMark(P.data.updates); sgrace = Infinity; P.paint();
  try{ const j = await P.api.post("/api/defiant/updates/scan", {}, {key: false});             /* a scan only looks: no key */
       if(j && j.started === false){ sgrace = 0; P.toast(j.reason === "busy" ? "A scan is already running." : j.reason === "rebooting" ? "A reboot is running." : "A scan ran a moment ago."); }
       else { sgrace = Date.now() + 8000; P.haptic("success"); P.toast("Scan started."); } }
  catch(e){ sgrace = 0; fail("Scan failed", e); }
  finally{ P.refresh(["updates"]); P.paint(); }
}));
P.on("sys.install", () => P.run(async () => {
  const u = P.data.updates, s = (u && u.scan) || {}; if(!u || upWhy(u, s)) return;
  const names = (s.packages || []).map(p => p.name), gone = (s.remove || []).map(upName), svc = upFam(UP_SVC, names), reb = upFam(UP_BOOT, names), sec = +s.security || 0;
  if(!await P.confirm({title: "Install updates?", sub: "Defiant", yes: "Install",
    text: `${plural(+s.count, "package")} will be upgraded${sec ? `, ${sec} of them security updates` : ""}.`
      + (gone.length ? ` It removes ${plural(gone.length, "package")}: ${upSome(gone, 6)}.` : "")
      + (svc.length ? ` It restarts services (${svc.join(", ")}): the sites can be unreachable for a few seconds.` : "")
      + (reb.length ? ` It needs a reboot afterwards (${reb.join(", ")}).` : "")
      + (s.reboot_required ? " A reboot is already waiting, so Defiant reboots by itself right after this install, and the sites are unreachable for about a minute."
        : " If a reboot is needed afterwards, Defiant reboots by itself and the sites are unreachable for about a minute.")})) return;
  imark = (P.data.updates.job || {}).started_at || 0; igrace = Infinity; P.paint();
  try{ await P.api.post("/api/defiant/updates/install", {reboot_ok: true}, {timeout: 60000}); igrace = Date.now() + 8000; P.haptic("success"); P.toast("Install started."); }       /* the sheet announced the reboot: Fleet reboots afterwards only for a request that says so */
  catch(e){ igrace = 0; fail("Install failed", e); }
  finally{ P.refresh(["updates"]); P.paint(); }
}));

/* ---- Reboot Defiant (fleet.html, Reboot block in the Server section: same rules, same words). "reboot" in /api/defiant/updates is
   idle, running (Fleet sees Defiant go down and come back, 3 s polls), done (shown for 10 minutes) or failed (shown until the next
   reboot). The button sits in the Server panel next to the Reboot figure and needs the Starfleet key. An older Fleet sends no
   "reboot": then there is no button. The error text comes from the server: escaped. */
const RB_SHOW = 600;                                                                      /* seconds a finished reboot stays on screen */
const rbOwn = (r, j) => !!r && !!r.auto && (r.started_at || r.ended_at || 0) >= (j.ended_at || 0) - 5;      /* the reboot this install started by itself (it came right after it), not an older one */
const rbWhy = u => (u.job || {}).state === "running" ? "An install is running, progress below." : igoing(u) ? "Starting the install..." : (u.scan || {}).busy || sgoing(u) ? "A scan is running, try again in a minute." : "";     /* Fleet answers 409 to a reboot during either */
const rbNb = s => esc(s).replace(/\(?\b\d+ s\b\)?/g, m => `<span style="white-space:nowrap">${m}</span>`);      /* "(62 s)" stays on one line */
function rbLine(r){      /* the running, finished or failed reboot as one line */
  const t = now(), w = r.state === "running" ? (r.down_at ? `Rebooting. Defiant has been down since ${clock(r.down_at * 1000)} (${Math.max(0, Math.round(t - r.down_at))} s).` : "Rebooting. Waiting for Defiant to go down.")
    : r.state === "done" && t - r.ended_at < RB_SHOW ? `Defiant rebooted and answered again after ${Math.round(+r.back_after || 0)} s.` : r.state === "failed" ? r.error || "The reboot failed." : "";
  return w ? `<div class="p-msg${r.state === "done" ? " p-sy-ok" : r.state === "failed" ? " err" : ""} p-sel">${rbNb(w)}</div>` : "";
}
function rbBlock(){      /* the button with the hint and the state, for the Server panel */
  const u = P.data.updates, r = u && u.reboot; if(!r || u.configured === false) return "";
  const run = rbRun(u), go = rgoing(u), why = rbWhy(u);
  return `<div class="p-pair" style="margin-top:12px"><button class="p-btn${(u.scan || {}).reboot_required ? " amber" : ""}" type="button" data-act="sys.reboot"${run || go || why ? " disabled" : ""}>${run ? "Rebooting" : go ? "Starting" : "Reboot Defiant"}</button></div>
    ${rbLine(r)}${run ? "" : `<p class="p-sel">${esc(why || "Reboots the whole server. All sites are unreachable for about a minute. Needs the Starfleet key.")}</p>`}`;
}
const rbCard = () => { const b = rbBlock(); return b ? `${H.sec("Server")}<div class="p-card" style="--c:var(--peach)"><h3>Defiant</h3>${b}</div>` : ""; };      /* the probe failed: the Server panel is gone, the reboot is not */
P.on("sys.reboot", () => P.run(async () => {
  const u = P.data.updates; if(!u || u.configured === false || !u.reboot || rbRun(u) || rgoing(u) || rbWhy(u)) return;
  const s = u.scan || {}, bp = s.reboot_pkgs || [], n = ((P.data.defiant || {}).sites || []).length;
  if(!await P.confirm({title: "Reboot Defiant?", sub: "Defiant", danger: true, yes: "Reboot",
    text: `This reboots the whole Defiant server. ${n ? plural(n, "site") : "The sites"} and all containers are unreachable for about a minute, and Fleet cannot reach Defiant until it is back.`
      + (s.reboot_required && bp.length ? ` A reboot is waiting for: ${bp.slice(0, 4).join(", ")}${bp.length > 4 ? ` and ${bp.length - 4} more` : ""}.` : "")})) return;
  rmark = (u.reboot || {}).started_at || 0; rgrace = Infinity; P.paint();
  try{ await P.api.post("/api/defiant/reboot", {}, {timeout: 60000}); rgrace = Date.now() + 8000; P.haptic("success"); P.toast("Reboot started."); }
  catch(e){ rgrace = 0; if(e && e.status === 409) P.toast(e.message); else fail("Reboot failed", e); }      /* 409: Fleet's own reason (an install runs, a reboot a moment ago), no failure of the page: a plain toast */
  finally{ P.refresh(["updates"]); P.paint(); }
}));
})();
