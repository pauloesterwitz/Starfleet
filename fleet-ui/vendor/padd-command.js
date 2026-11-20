/* ================= Starfleet PADD: Command =================
   The Starfleet key on this device, Red Alert, Auto Destruct, and Help / About / Desktop page.
   Red Alert and Auto Destruct are fleet.html's sequences (L1077-1250) with a phone's rules on top:
     - armed by sliding a knob to the end of its track, never by a tap (Enter or Space asks instead);
     - the cancel window runs from a deadline. It cancels itself when the page is hidden, and when a
       tick arrives more than 2 s late: a page that was suspended must never wake up and fire;
     - the overlay is #p-alert, above the tab bar and outside the routed view, so a route change
       leaves it alone. */
(() => {
"use strict";
const P = window.PADD, H = P.h, T = P._test, A = document.getElementById("p-alert");
const hush = p => { if(p && p.catch) p.catch(() => {}); };

P.css(`
html.p-alerting{overflow:hidden}
.p-slide span{transition:opacity .15s}
.p-slide.p-lock{background:#5a1a1a}
.p-code{text-align:center;text-transform:uppercase;font-size:20px;letter-spacing:.16em}
.p-code::placeholder{letter-spacing:.1em}
.p-shake{animation:p-shake .3s}
@keyframes p-shake{25%{transform:translateX(-8px)}75%{transform:translateX(8px)}}
.p-facts{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px;margin:12px 0 4px}
.p-facts span,.p-facts b{background:var(--card);padding:11px 12px 10px;font-size:15px;line-height:1.2}
.p-facts span{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.12em;line-height:18px}
.p-facts b{font-weight:600;color:var(--peach);overflow-wrap:anywhere}
.p-help b{display:block;margin:14px 0 0;font-size:13px;font-weight:600;color:var(--amber);text-transform:uppercase;letter-spacing:.12em}
.p-help p{margin:3px 0 0;font:400 15px/1.4 var(--body);letter-spacing:0;color:var(--text)}
.p-help i{font-style:normal;font-family:var(--lcars);font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--c)}

/* ---- the alert overlay: a red elbow frame over the whole screen ---- */
#p-alert{position:fixed;inset:0;z-index:80;display:grid;grid-template-columns:52px minmax(0,1fr);
  grid-template-rows:64px minmax(0,1fr) auto;gap:4px;touch-action:none;background:var(--ink);--c:var(--red);
  padding:calc(var(--sat) + 4px) calc(10px + var(--sar)) calc(var(--sab) + 6px) calc(10px + var(--sal))}
.p-ae1{background:var(--c);border-top-left-radius:28px}
.p-ahd{background:var(--c);border-radius:0 32px 32px 0;display:flex;align-items:center;padding:0 22px;color:#000;
  font-size:30px;font-weight:700;text-transform:uppercase;letter-spacing:.14em;white-space:nowrap;overflow:hidden}
.p-aside{background:repeating-linear-gradient(180deg,var(--c) 0 26px,#000 26px 30px)}
.p-ae2{background:var(--c);border-bottom-left-radius:28px}
.p-amain{min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;
  padding:0 6px 0 2px;text-align:center}
.p-count{font-size:min(200px,26vh);font-weight:700;line-height:1;margin-bottom:10px;color:var(--c);letter-spacing:0;font-variant-numeric:tabular-nums}
.p-count.p-sm{font-size:84px;line-height:1.1;letter-spacing:.04em}
.p-acap{font-size:15px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.16em}
.p-ahl{margin:0;font-size:32px;font-weight:700;line-height:1.12;text-transform:uppercase;letter-spacing:.08em;color:var(--c)}
.p-abar{align-self:stretch;position:relative;height:10px;margin:14px 0 18px;background:var(--track);overflow:hidden}
.p-abar i{position:absolute;left:0;top:0;bottom:0;background:var(--c);transition:width .2s linear}
.p-abar.p-busy i{width:34%;transition:none;animation:p-sweep 1.4s steps(12) infinite}
.p-ahint{margin:0;max-width:290px;font:400 15px/1.4 var(--body);letter-spacing:0;color:var(--text);white-space:pre-line}
.p-amain .p-msg{align-self:stretch;text-align:left}
.p-aft{display:flex}
.p-aft .p-btn{flex:1;min-height:68px;border-radius:0 34px 34px 0;font-size:19px;outline:0}
/* armed: the frame flashes. This is the signal, the silent switch mutes the klaxon */
#p-alert.p-armed{animation:p-abg 1s steps(2) infinite}
.p-armed .p-ae1,.p-armed .p-ahd,.p-armed .p-ae2{animation:p-aflash 1s steps(2) infinite}
.p-armed .p-aside,.p-count.p-crit{animation:p-ablink 1s steps(2) infinite}
.p-count.p-crit{animation-duration:.5s}
@keyframes p-abg{50%{background:#2a0d0d}}
@keyframes p-aflash{50%{background:#5a1a1a}}
@keyframes p-ablink{50%{opacity:.15}}
/* the report */
.p-amain.p-arep{align-items:stretch;justify-content:flex-start;gap:0;text-align:left;padding:8px 2px 8px 6px;
  overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;touch-action:pan-y}
.p-arep .p-lab{margin-top:14px}
.p-aitem{margin-top:2px;padding:8px 10px 7px;background:var(--card);border-left:4px solid var(--grid);
  font-size:15px;line-height:1.25;letter-spacing:.04em;color:var(--text);overflow-wrap:anywhere}
.p-aitem.p-none{color:var(--dim);font-size:13px;text-transform:uppercase;letter-spacing:.1em}
.p-aitem.p-bad{border-left-color:var(--red);color:#f0a9a9;font:400 14px/1.35 var(--body);letter-spacing:0}
@media (prefers-reduced-motion:reduce){ #p-alert,#p-alert *{animation:none!important} .p-abar.p-busy i{width:100%} }
`);

/* ---------------------------------------------------------------- klaxon
   The clip is optional and its route can drop the connection, so it is fetched and decoded here, and a
   media element is not used (the route ignores Range, which iOS wants). Until the clip is there, and
   whenever it is not, the two-tone klaxon from fleet.html L1077 plays. unlock() must run inside a touch:
   iOS keeps a context silent until a gesture resumes it. */
const klaxon = (() => {
  const AC = window.AudioContext || window.webkitAudioContext;
  let ac, clip, src, osc, gain, iv, on = false;
  const quiet = () => { clearInterval(iv); for(const n of [src, osc]) try{ if(n) n.stop(); }catch(e){} src = osc = null; };
  const play = () => { quiet(); try{
    gain = ac.createGain(); gain.connect(ac.destination);
    if(clip){ src = ac.createBufferSource(); src.buffer = clip; src.loop = true; gain.gain.value = .85; src.connect(gain); src.start(); return; }
    osc = ac.createOscillator(); osc.type = "sine"; gain.gain.value = 0; osc.connect(gain); osc.start();
    const o = osc, g = gain, cycle = () => { const t = ac.currentTime;
      for(const [f, d] of [[880, 0], [660, .55]]){ o.frequency.setValueAtTime(f, t + d);
        g.gain.setValueAtTime(0, t + d); g.gain.linearRampToValueAtTime(.16, t + d + .03);
        g.gain.setValueAtTime(.16, t + d + .45); g.gain.linearRampToValueAtTime(0, t + d + .5); } };
    cycle(); iv = setInterval(cycle, 1100); }catch(e){} };
  return {
    unlock(){ try{
      if(!AC) return;
      if(!ac){ ac = new AC();
        hush(fetch("/sounds/tng_red_alert1.mp3").then(r => r.ok ? r.arrayBuffer() : Promise.reject())
          .then(b => new Promise((ok, bad) => hush(ac.decodeAudioData(b, ok, bad)))).then(b => { clip = b; if(on) play(); })); }
      if(ac.state !== "running") hush(ac.resume()); }catch(e){} },
    start(){ this.unlock(); if(ac){ on = true; play(); } },
    stop(){ on = false; quiet(); } };
})();

/* ---------------------------------------------------------------- the overlay */
const MODES = {
  red: { title: "Red Alert", secs: 5, test: "alertSeconds", stop: "Return to green alert",
    hint: "Nothing has been stopped yet. At zero, every model is unloaded and the GPU containers on both Sparks are stopped.",
    cxh: "Green alert restored", cxp: "Red Alert cancelled. No model was stopped." },
  destruct: { title: "Auto Destruct", secs: 10, test: "destructSeconds", stop: "Cancel auto destruct",
    hint: "Nothing has been stopped yet. At zero, every model is stopped and both Sparks reboot.",
    cxh: "Auto destruction cancelled", cxp: "No model was stopped. Both machines remain online." } };
const RETURN = `<button class="p-btn" type="button" data-act="cmd-close">Return to command</button>`;
let seq = 0;        /* bumped by dismiss(): late answers of a sequence the user left stay quiet (fleet.html "run") */
let timer = null, win = null, skew = 0;
/* The wall clock on purpose: performance.now() stands still while iOS has the page suspended, so a late
   tick would look punctual. skew is only ever moved by the test hook lateTick. */
const now = () => Date.now() + skew;
A.setAttribute("role", "alertdialog");

function frame(color, cls, title, main, foot){
  if(A.hidden){ P.closeSheets(); A.hidden = false; document.documentElement.classList.add("p-alerting"); }
  A.className = cls; A.style.setProperty("--c", `var(--${color})`); A.setAttribute("aria-label", title);
  A.innerHTML = `<div class="p-ae1"></div><div class="p-ahd">${esc(title)}</div><div class="p-aside"></div>
    <div class="p-amain${cls === "p-rep" ? " p-arep" : ""}">${main}</div><div class="p-ae2"></div><div class="p-aft">${foot}</div>`;
  A.querySelector("button").focus({preventScroll: true});
}
function dismiss(){
  seq++; clearTimeout(timer); win = null; klaxon.stop();
  A.hidden = true; A.innerHTML = ""; document.documentElement.classList.remove("p-alerting"); P.refresh();
}
const COUNT = cap => `<div class="p-count" role="timer"></div><div class="p-acap">${cap}</div><div class="p-abar"><i></i></div><p class="p-ahint"></p>`;
const parts = () => [".p-count", ".p-abar i", ".p-ahint"].map(q => A.querySelector(q));

/* The cancel window both sequences share: klaxon and countdown, then fire(), unless cancel() got there
   first. Nothing has been sent when it opens except the check. */
function openWindow(m, fire){
  const total = (T[m.test] || m.secs) * 1000, end = now() + total; let shown = 0;
  frame("red", "p-armed", m.title, COUNT("Seconds to cancel"), `<button class="p-btn blue" type="button" data-act="cmd-cancel">${m.stop}</button>`);
  const [cnt, bar, hint] = parts(); hint.textContent = m.hint;
  klaxon.start();
  const tick = () => {
    const t = now(), left = end - t;
    if(document.hidden) return cancel(AWAY);          /* also a window that opened while the page was hidden */
    if(t - win.due > 2000) return cancel("The countdown was interrupted.");
    if(left <= 0){ win = null; P.haptic("error"); return fire(); }
    const n = Math.ceil(left / 1000);
    if(n !== shown){ shown = n; cnt.textContent = n; cnt.setAttribute("aria-label", n + " seconds left"); cnt.classList.toggle("p-crit", n <= 3); P.haptic("warning"); }
    bar.style.width = (left / total * 100) + "%";
    const step = Math.min(200, left); win.due = t + step; timer = setTimeout(tick, step);
  };
  win = {m, tick, due: now()}; tick();
}
let lift = 0;       /* the finger that cancelled is still down: its click must not land on "Return to command" */
function cancel(why){
  if(!win) return;
  const m = win.m; win = null; clearTimeout(timer); klaxon.stop(); P.haptic("success"); lift = Date.now() + 500;
  frame("blue", "", m.title, `<h2 class="p-ahl">${m.cxh}</h2><p class="p-ahint" style="margin-top:8px">${m.cxp}${why ? " " + why : ""}</p>`, RETURN);
  if(!why) timer = setTimeout(dismiss, 2200);      /* a window that cancelled itself stays up until it has been read */
}
const AWAY = "The app left the screen during the countdown.";
document.addEventListener("visibilitychange", () => { if(document.hidden) cancel(AWAY); });
/* Cancel acts on touch down: a finger that lands before zero and lifts after it has still cancelled. */
A.addEventListener("pointerdown", e => { if(e.target.closest('[data-act="cmd-cancel"]')) cancel(); });
A.addEventListener("keydown", e => { if(e.key === "Escape") cancel(); });
P.on("cmd-cancel", () => cancel());
P.on("cmd-close", () => { if(Date.now() > lift) dismiss(); });

const busy = m => frame("red", "p-armed", m.title, `<h2 class="p-ahl">Stopping all models</h2><div class="p-abar p-busy"><i></i></div>
  <p class="p-ahint">Do not power off. This can take a few minutes, and it continues if you leave this screen.</p>`, RETURN);
function fail(m, e){
  klaxon.stop(); P.haptic("error");
  frame("red", "", m.title, `<h2 class="p-ahl">${m.title} failed</h2>${H.msg(e.status === 403 && !/code/i.test(e.message)
    ? "Fleet refused the Starfleet key. Enter it again under Access." : e.message, true)}
    <p class="p-ahint">Check Bridge and Models to see what is still running.</p>`, RETURN);
}

/* ---------------------------------------------------------------- Red Alert */
async function redAlert(){
  await P.api.post("/api/redalert", {check: true});     /* a missing key is refused here, not after the countdown */
  openWindow(MODES.red, runRed);
}
async function runRed(){
  const me = seq, m = MODES.red; busy(m);
  try{
    const j = await P.api.post("/api/redalert", {}, {timeout: 0, prompt: false});
    if(me !== seq) P.toast("Red Alert complete."); else { klaxon.stop(); P.haptic("success"); report(j || {}); }
  }catch(e){ if(me !== seq) P.toast("Red Alert failed: " + e.message, "err"); else fail(m, e); }
  P.refresh();
}
/* What Red Alert stopped (fleet.html showResult): models, GPU containers per machine, orphans, dormant. */
function report(j){
  const TAG = P.model.TAG, gpu = Object.entries(j.stopped || {}), errs = Object.entries(j.errors || {}), f = j.freed_mb;
  const list = (t, items, note) => `<div class="p-lab">${esc(t)} · ${items.length}</div>${note ? `<p class="p-note" style="margin:0 0 5px">${note}</p>` : ""}${
    items.length ? items.map(x => `<div class="p-aitem p-sel">${esc(x)}</div>`).join("") : `<div class="p-aitem p-none">None</div>`}`;
  const n = gpu.reduce((a, [, v]) => a + v.length, 0) + (j.orphans || []).length + (j.dormant || []).length;
  frame(j.ok ? "blue" : "amber", "p-rep", "Red Alert", `<h2 class="p-ahl">Red Alert complete</h2>
    ${j.ok ? "" : H.msg("Not everything stopped. Check Bridge and Models for what is still running.", true)}
    <div class="p-read"><div><b>${(j.unloaded || []).length}</b><span>Models</span></div><div><b>${n}</b><span>Containers</span></div>
      <div><b>${f == null ? "?" : f >= 1024 ? (f / 1024).toFixed(1) + " GB" : f + " MB"}</b><span>Freed</span></div></div>
    <p class="p-note">Memory freed is measured on Jean-Luc.</p>
    ${errs.length ? `<div class="p-lab">Errors · ${errs.length}</div>${errs.map(([k, v]) => `<div class="p-aitem p-bad p-sel">${esc(TAG[k] || k)}: ${esc(v)}</div>`).join("")}` : ""}
    ${list("Models unloaded", j.unloaded || [])}
    ${gpu.map(([node, names]) => list("GPU containers stopped on " + (TAG[node] || node), names)).join("")}
    ${list("Orphaned containers stopped", j.orphans || [])}
    ${list("Dormant-session containers stopped", j.dormant || [], "Their sessions were idle for over 24 h.")}`, RETURN);
}

/* ---------------------------------------------------------------- Auto Destruct */
let ad = null;     /* the open code sheet */
function codeSheet(){
  const s = P.sheet({title: "Auto Destruct", sub: "Authorization code required", color: "red", onClose: () => { ad = null; },
    body: `<p class="p-text">Command authorization required. This reboots Kathryn and Jean-Luc after every model is stopped.</p>
      <label class="p-fieldbox" style="margin-top:12px"><input class="p-field p-code" type="text" autocomplete="off" autocapitalize="characters"
        autocorrect="off" spellcheck="false" enterkeyhint="go" placeholder="Enter code" aria-label="Authorization code"></label>
      <div></div>
      <div class="p-pair p-acts"><button class="p-btn ghost" type="button" data-act="cmd-abort">Abort</button>
        <button class="p-btn red" type="button" data-act="cmd-go">Authorize</button></div>`});
  const box = s.body.querySelector("label"), input = box.firstChild, msg = box.nextElementSibling, go = s.body.querySelector('[data-act="cmd-go"]');
  const send = async () => {
    if(go.disabled) return;
    const code = input.value.trim().toUpperCase();
    klaxon.unlock(); go.disabled = true; go.textContent = "Verifying..."; msg.innerHTML = "";
    try{ await P.api.post("/api/autodestruct", {code, check: true}); }      /* proves the code and the key, runs nothing */
    catch(e){
      go.disabled = false; go.textContent = "Authorize";
      if(e.quiet) return;
      const denied = e.status === 403 && /code/i.test(e.message);
      msg.innerHTML = H.msg(denied ? "Access denied." : e.status === 503
        ? "Auto Destruct is disabled: no authorization code is set on the Fleet server. Set STARFLEET_AUTODESTRUCT_CODE in the Fleet service to enable it." : e.message, true);
      if(denied){ box.classList.remove("p-shake"); void box.offsetWidth; box.classList.add("p-shake"); input.select(); }
      P.haptic("error"); return;
    }
    if(!s.open) return;                                  /* closed while verifying: that was an abort */
    s.close(); openWindow(MODES.destruct, () => execute(code));
  };
  input.addEventListener("keydown", e => { if(e.key === "Enter") send(); });
  input.focus({preventScroll: true});                    /* still inside the slide's touch, so the keyboard comes up */
  ad = {send, close: () => s.close()};
}
P.on("cmd-go", () => ad && ad.send());
P.on("cmd-abort", () => ad && ad.close());

async function execute(code){
  const me = seq, m = MODES.destruct; busy(m);
  /* the boot id to compare against after the reboot. Not awaited: nothing may stand between zero and the POST */
  const boot0 = P.api.get("/api/boot", {timeout: 8000}).then(j => j.boot, () => null);
  let j;
  try{ j = await P.api.post("/api/autodestruct", {code}, {timeout: 0, prompt: false}) || {}; }
  catch(e){ if(me !== seq) P.toast("Auto Destruct failed: " + e.message, "err"); else fail(m, e); P.refresh(); return; }
  if(me !== seq){ P.toast("Auto Destruct is running. Both Sparks reboot now."); return; }
  const total = (j.jean_luc_in_s || 10) * 1000, kat = (j.kathryn_in_s || 3) * 1000, t0 = now();
  frame("red", "p-armed", m.title, COUNT("Seconds to self-destruct"), RETURN);
  const [cnt, bar, hint] = parts();
  const tick = () => {
    if(me !== seq) return;
    const gone = now() - t0, left = total - gone, n = Math.ceil(left / 1000);
    if(left <= 0){ boot0.then(b => { if(me === seq) waitForReboot(me, b); }); return; }
    cnt.textContent = n; cnt.classList.toggle("p-crit", n <= 5); bar.style.width = (left / total * 100) + "%";
    hint.textContent = (gone >= kat ? "Kathryn: rebooting" : "Kathryn: reboot imminent") + "\nJean-Luc: standing by";
    timer = setTimeout(tick, 200);
  };
  tick();
}
/* Both machines go down. Poll until Jean-Luc answers with a new boot id (or came back after being
   unreachable) and Kathryn's probe is healthy again. "Return to command" stays, so this is never a trap. */
function waitForReboot(me, boot0){
  klaxon.stop();
  frame("orange", "", "Stand by", `<h2 class="p-ahl"></h2><div class="p-count p-sm" role="timer"></div><div class="p-abar p-busy"><i></i></div><p class="p-ahint"></p>`, RETURN);
  const [cnt, bar, hint] = parts(), head = A.querySelector("h2"), t0 = now();
  let jlUp = false, sawDown = false, ka = false, done = false;
  const say = () => { cnt.textContent = mmss((now() - t0) / 1000);
    head.textContent = done ? "All systems online" : jlUp ? "Jean-Luc online, waiting for Kathryn" : "Systems rebooting";
    hint.textContent = `Jean-Luc: ${jlUp ? "online" : "rebooting"}\nKathryn: ${ka ? "online" : jlUp ? "connecting" : "rebooting"}`; };
  const clock = setInterval(() => { if(me !== seq || done) clearInterval(clock); else say(); }, 1000);
  const poll = async () => {
    if(me !== seq) return;
    let boot = null;
    try{ boot = (await P.api.get("/api/boot", {timeout: 5000})).boot; }catch(e){}
    if(me !== seq) return;
    if(!boot) sawDown = true; else if((boot0 && boot !== boot0) || sawDown) jlUp = true;
    if(jlUp && boot){
      try{ const k = ((await P.api.get("/api/state", {timeout: 8000})).nodes || {}).kathryn; ka = !!(k && !k.error && k.mem_total_kb); }catch(e){}
      if(me !== seq) return;
    }
    done = jlUp && ka; say();
    if(!done){ timer = setTimeout(poll, 3000); return; }
    bar.parentNode.classList.remove("p-busy"); bar.style.width = "100%"; A.style.setProperty("--c", "var(--blue)");
    A.querySelector(".p-ahd").textContent = "Online"; P.haptic("success");
    timer = setTimeout(() => { if(me === seq) dismiss(); }, 2500);
  };
  say(); poll();
}

/* ---------------------------------------------------------------- arming */
let arming = false;
async function arm(name){
  if(arming || !A.hidden) return;
  arming = true; await P.run(name === "red" ? redAlert : codeSheet); arming = false;
}
function slider(knob){
  const track = knob.parentNode, label = track.querySelector("span");
  let x0 = null, max = 0, dx = 0;
  const set = (x, ease) => { dx = x; knob.style.transition = ease ? "transform .2s" : "none"; knob.style.transform = `translateX(${x}px)`;
    label.style.opacity = max ? 1 - x / max : 1; track.classList.toggle("p-lock", max > 0 && x >= max); };
  knob.addEventListener("pointerdown", e => { klaxon.unlock(); x0 = e.clientX; max = track.clientWidth - knob.offsetWidth - 8;
    try{ knob.setPointerCapture(e.pointerId); }catch(_){} });
  knob.addEventListener("pointermove", e => { if(x0 == null) return; const was = dx >= max;
    set(Math.max(0, Math.min(max, e.clientX - x0))); if(dx >= max && !was) P.haptic("heavy"); });
  const up = e => { if(x0 == null) return; x0 = null; const locked = e.type === "pointerup" && max > 0 && dx >= max; set(0, true);
    if(locked){ klaxon.unlock(); arm(knob.dataset.arg); } };      /* lifting the finger is the gesture iOS unlocks audio on */
  knob.addEventListener("pointerup", up); knob.addEventListener("pointercancel", up);
}
/* A click that reaches the knob with detail 0 came from Enter, Space or assistive technology: there is no
   slide to make, so a sheet asks instead. A finger's or a mouse's click has detail 1 and does nothing. */
P.on("cmd-slide", async (name, el, e) => {
  if(e.detail) return;
  klaxon.unlock();
  const red = name === "red";
  if(await P.confirm({title: red ? "Arm Red Alert?" : "Begin Auto Destruct?", danger: true, yes: red ? "Arm" : "Begin",
    text: red ? "Unloads every model and stops the GPU containers on both Sparks. You get 5 seconds to cancel."
              : "Red Alert, then both Sparks reboot. The authorization code is asked for next."})) arm(name);
});
Object.assign(T, { slide: arm,
  lateTick(ms){ if(win){ skew += ms || 3000; clearTimeout(timer); win.tick(); } } });   /* the clock jumps as after a suspension */

/* ---------------------------------------------------------------- the screen */
const ARROWS = `<svg viewBox="0 0 22 16" aria-hidden="true"><path d="M3 2l6 6-6 6M12 2l6 6-6 6" fill="none" stroke="#000" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const panel = (name, c, title, text, label, top) => `<section class="p-card" style="--c:${c}${top ? ";margin-top:6px" : ""}"><h3>${title}</h3><p>${text}</p>
  <div class="p-slide"><button class="p-knob" type="button" data-act="cmd-slide" data-arg="${name}" aria-label="${label} ${title}">${ARROWS}</button><span>${label}</span></div></section>`;
P.screens.command = { tab: "command", title: "Command", color: "mauve",
  mount(el){
    el.innerHTML = `${H.sec("Access")}<div id="p-ckey"></div>${H.sec("Emergency")}
      ${panel("red", "var(--red)", "Red Alert", "Unloads every model and stops the GPU containers on both Sparks. You get 5 seconds to cancel.", "Slide to arm")}
      ${panel("destruct", "#d06a6a", "Auto Destruct", "Red Alert, then both Sparks reboot. Asks for the authorization code first.", "Slide to begin", 1)}
      <div class="p-pair" style="margin-top:16px"><button class="p-btn sm ghost" type="button" data-act="cmd-help">Help</button>
        <button class="p-btn sm ghost" type="button" data-act="cmd-about">About</button>
        <button class="p-btn sm ghost" type="button" data-act="cmd-desktop">Desktop page</button></div>`;
    el.querySelectorAll(".p-knob").forEach(slider);
  },
  render(el){
    P.hx(`<span class="p-sync">${esc(P.syncText())}</span>`);
    const has = !!P.key.get();
    P.put(el.querySelector("#p-ckey"), `<section class="p-card" style="--c:var(--orange)"><h3>Starfleet key</h3>
      <p>${has ? "Stored on this device and sent only to Fleet." : "Not stored on this device. Fleet asks for it when an action needs it."}</p>
      <div class="p-pair" style="margin-top:11px">${has
        ? `<button class="p-btn sm ghost" type="button" data-act="key">Change</button><button class="p-btn sm ghost" type="button" data-act="cmd-keyrm">Remove</button>`
        : `<button class="p-btn sm" type="button" data-act="key">Enter key</button>`}</div></section>`);
  } };
P.on("cmd-keyrm", async () => {
  if(!await P.confirm({title: "Remove the Starfleet key?", danger: true, yes: "Remove",
    text: "It is deleted from this device. Fleet asks for it again when an action needs it."})) return;
  P.key.set(""); P.haptic("success"); P.toast("Starfleet key removed from this device."); P.paint();
});

/* ---------------------------------------------------------------- Help, About, Desktop page */
const TABHELP = { bridge: "Both Sparks at a glance, loads in progress and what needs attention.",
  models: "Load, pin and unload models. Set context and thinking effort.",
  computer: "Ask a resident model by text or voice, with the wiki and web search.",
  systems: "Sessions, Main Engineering, Deep Space NAS, Memory Alpha, Defiant and Memory Core.",
  command: "The Starfleet key, Red Alert and Auto Destruct." };
P.on("cmd-help", () => P.sheet({title: "Help", color: "mauve", body: `<div class="p-help p-sel">
  <b>The five tabs</b>${P.tabs.map(t => `<p><i style="--c:var(--${t.color})">${t.label}</i> ${TABHELP[t.id] || ""}</p>`).join("")}
  <b>Status</b><p>The word at the top right is Nominal, Attention, Fault or Sync lost. Tap it to list the reasons.</p>
  <b>Starfleet key</b><p>Asked for when an action needs it. It is kept on this device and never sent anywhere but Fleet.</p>
  <b>Refresh</b><p>Pull down to refresh.</p>
  <b>Red Alert</b><p>Unloads every model and stops the GPU containers on both Sparks, after 5 seconds to cancel.</p>
  <b>Auto Destruct</b><p>Red Alert, then both Sparks reboot. It asks for the authorization code and gives 10 seconds to cancel.</p>
  <b>Updates</b><p>This interface is served by Fleet, so it updates without reinstalling the app.</p></div>`}));
const REPO = "https://github.com/pauloesterwitz/Starfleet";
P.on("cmd-about", () => P.sheet({title: "Starfleet Command PADD", color: "mauve", body: `
  <div class="p-facts"><span>Version</span><b>${esc(P.version)}</b><span>Fleet address</span><b class="p-sel">${esc(location.host)}</b><span>Licence</span><b>MIT</b></div>
  <a class="p-row" href="${REPO}" target="_blank" rel="noopener" data-act="cmd-ext" data-arg="${REPO}" style="--c:var(--blue)">
    <span class="p-m"><span class="p-t">Public repository</span><span class="p-s p-sel" style="text-transform:none">${REPO.slice(8)}</span></span>${H.chev}</a>
  <p class="p-note p-sel" style="margin-top:12px">Star Trek and its iconography are trademarks of Paramount. A personal, non-commercial project with no affiliation.</p>
  <div class="p-pair p-acts" style="margin-top:14px"><button class="p-btn sm ghost" type="button" data-act="cmd-reload">Reload page</button></div>`}));
/* pull to refresh updates the data in place; this loads the interface itself again (after an update of Fleet) */
P.on("cmd-reload", () => location.reload());
/* the app opens outside links in Safari; a web view without that bridge has nowhere to open one */
P.on("cmd-ext", url => { if(P.native("open", {url})) return;
  const w = window.open(url, "_blank"); if(w) w.opener = null; else P.toast("This link cannot open here. Its address is on the sheet to copy."); });
P.on("cmd-desktop", async () => {
  if(await P.confirm({title: "Desktop page", yes: "Open", text: "Opens the desktop page here. Pull down to refresh to come back."})) location.href = "/desktop";
});
})();
