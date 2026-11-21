/* ================= Computer voice =================
   Speaks Computer's replies in the ship's-computer voice. Every sentence goes to POST /api/computer/speak (fleet.py), which asks the
   "Computer Voice" Shuttle Bay member (Piper + RVC on a Spark) for a WAV. Shared by fleet.html and the phone page (padd).
   Nothing is spoken until the voice is loaded in Shuttle Bay; this file never loads anything.

     CV.init({headers, say})   headers(): extra request headers (the phone adds the Starfleet key; the desktop's fetch wrapper does)
                               say(text, isError): one line of feedback on the page
     CV.auto                   persisted switch: speak each new reply while it streams (CV.onauto(fn) hears changes)
     CV.prime()                call inside a tap or key press: unlocks audio for the automatic playback that follows
     CV.stream(btn)            -> {feed(markdownSoFar), end(markdownFinal)} or null when auto is off; btn shows data-state
     CV.toggle(btn, markdown)  speak this whole reply, or stop if this button is the one speaking
     CV.stop()
   data-state on a button: "loading" (waiting for the first sentence) | "playing" | absent when idle. Pure helpers (clean, sentences,
   Splitter) are exported for ~/fleet-ui-test/computer-voice-test.js. */
(function(root){
"use strict";

/* ---------------------------------------------------------------- text: markdown -> what a person would read aloud */
function clean(md){
  const out = []; let fence = null;
  for(const raw of String(md || "").replace(/\r/g, "").split("\n")){
    const m = raw.match(/^\s{0,3}(`{3,}|~{3,})/);
    if(fence){ if(m && m[1][0] === fence[0] && m[1].length >= fence.length) fence = null; continue; }     /* code is never read aloud */
    if(m){ fence = m[1]; continue; }                                                                  /* an open fence swallows the rest until it closes */
    let l = raw;
    if(/^\s*\|/.test(l) || /^\s*[-:|\s]{3,}$/.test(l) && l.includes("|")) continue;                    /* table rows */
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) continue;                                                  /* horizontal rule */
    l = l.replace(/^\s{0,3}#{1,6}\s+/, "").replace(/^\s*>+\s?/, "").replace(/^\s*([-*+]|\d+[.)])\s+/, "")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/<[^>]+>/g, "")
      .replace(/https?:\/\/\S+/g, "").replace(/`([^`]*)`/g, "$1").replace(/(\*\*|__|~~)/g, "")
      .replace(/(^|[\s(])[*_]+(?=\S)/g, "$1").replace(/(\S)[*_]+(?=$|[\s).,;:!?])/g, "$1").replace(/[_*]/g, " ")
      .replace(/[\p{Extended_Pictographic}️‍]/gu, "").replace(/\s+/g, " ").trim();
    if(!/\p{L}|\p{N}/u.test(l)) continue;
    out.push(/[.!?…:;]$/.test(l) ? l : l + ".");                                                        /* every line ends a sentence: headings and list items pause */
  }
  return out.join(" ");
}
const seg = typeof Intl !== "undefined" && Intl.Segmenter ? new Intl.Segmenter(undefined, {granularity: "sentence"}) : null;
const sentences = t => (seg ? [...seg.segment(t)].map(s => s.segment) : t.match(/[^.!?…]+[.!?…]*/g) || []).map(s => s.trim()).filter(Boolean);

const MAX = 260;                        /* characters per request, well under the server's 600 */
function cut(s){                        /* a very long sentence: at a comma or semicolon, else at a space */
  const o = [];
  while(s.length > MAX){
    let i = Math.max(s.lastIndexOf(", ", MAX), s.lastIndexOf("; ", MAX));
    if(i < 80) i = s.lastIndexOf(" ", MAX);
    if(i < 40) i = MAX;
    o.push(s.slice(0, i + 1).trim()); s = s.slice(i + 1).trim();
  }
  if(s) o.push(s);
  return o;
}
/* Streaming: a sentence is complete once the next one has started (the segmenter only splits then). Short sentences are joined, because
   every request costs about a second on the server: the first piece may be short (fast start), later ones aim for 60+ characters. */
class Splitter {
  constructor(){ this.n = 0; this.pending = ""; this.first = true; }
  _take(s, upto){
    const out = [];
    for(; this.n < upto; this.n++) for(const part of cut(s[this.n])){
      this.pending = this.pending ? this.pending + " " + part : part;
      if(this.pending.length >= (this.first ? 20 : 60)){ out.push(this.pending); this.pending = ""; this.first = false; }
    }
    return out;
  }
  feed(md){ const s = sentences(clean(md)); return this._take(s, s.length - 1); }
  end(md){ const s = sentences(clean(md)), out = this._take(s, s.length); if(this.pending){ out.push(this.pending); this.pending = ""; } return out; }
}

/* ---------------------------------------------------------------- playback: one audio element, two sentences fetched ahead */
if(typeof document === "undefined"){ root.CV = {clean, sentences, Splitter, cut}; return; }

let cfg = {}, gen = 0, ctl = new AbortController(), q = [], cur = null, owner = null, ended = true, shown = false, suspended = false, primed = false;
const A = new Audio(); A.preload = "auto"; A.setAttribute("playsinline", "");
const LS = { get(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } }, set(k, v){ try{ localStorage.setItem(k, v); }catch(e){} } };
const CV = {
  init(c){ cfg = c || {}; },
  get local(){ return LS.get("computerVoiceLocal") !== "0"; },
  set local(on){ LS.set("computerVoiceLocal", on ? "1" : "0"); },
  get auto(){ return LS.get("computerVoice") === "1"; },
  set auto(on){ LS.set("computerVoice", on ? "1" : "0"); suspended = false; if(!on) CV.stop(); if(CV.onauto) CV.onauto(!!on); },
  onauto: null,
  clean, sentences, Splitter,
  prime(){
    if(primed) return; primed = true;                      /* a silent clip played inside the tap: WebKit then lets this element play later without one */
    const n = 1600, b = new ArrayBuffer(44 + n * 2), v = new DataView(b), w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
    w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVEfmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
    const u = URL.createObjectURL(new Blob([b], {type: "audio/wav"}));
    A.src = u; const p = A.play(); if(p && p.then) p.then(() => { if(A.src === u) A.pause(); }, () => { primed = false; }); A.onended = null;
    setTimeout(() => URL.revokeObjectURL(u), 3000);
  },
  stop(){ gen++; ctl.abort(); ctl = new AbortController(); q = []; cur = null; ended = true; A.onended = A.onerror = null;
    try{ A.pause(); A.removeAttribute("src"); A.load(); }catch(e){} mark(null); },
  stream(btn){
    if(!CV.auto || suspended) return null;
    CV.stop(); ended = false; owner = btn; mark("loading"); const sp = new Splitter(), g = gen;
    /* a stop (Stop button, new message, leaving the screen) ends this stream for good: later text of the same reply is not spoken */
    return { feed: md => { if(g === gen) sp.feed(md).forEach(add); }, end: md => { if(g === gen){ sp.end(md).forEach(add); ended = true; pump(); } } };
  },
  toggle(btn, md){
    const mine = owner === btn && (cur || q.length || !ended); CV.stop(); if(mine) return;
    CV.prime(); suspended = false; owner = btn; mark("loading"); ended = false;
    new Splitter().end(md).forEach(add); ended = true; pump();
  },
};
function mark(state){ if(owner){ if(state) owner.dataset.state = state; else delete owner.dataset.state; } if(!state) owner = null; }
function add(text){ q.push({text, blob: null}); pump(); }
function pump(){
  let ahead = 0; for(const it of q) if(it.blob && ++ahead >= 2) break;
  for(const it of q){ if(ahead >= 2) break; if(!it.blob){ it.blob = fetchWav(it.text); it.blob.catch(() => {}); ahead++; } }
  if(!cur && q.length) next();
  else if(!cur) ended ? mark(null) : mark("loading");     /* nothing left to say: done, or waiting for the next sentence of a stream */
}
let localOff = false;
async function fetchWav(text){
  if(!localOff && root.CVLocal && CV.local){                /* on this device first: no network, no Spark; any failure falls back to Fleet's voice for the session */
    try{ return await root.CVLocal.synth(text, null, cfg.headers ? cfg.headers() : {}); }
    catch(e){ if(e.status === 400) throw e; localOff = true; say("The on-device voice is not available (" + e.message + "). Using Fleet's voice.", true); }
  }
  const r = await fetch("/api/computer/speak", {method: "POST", signal: ctl.signal,
    headers: Object.assign({"Content-Type": "application/json"}, cfg.headers ? cfg.headers() : {}), body: JSON.stringify({text})});
  if(!r.ok){ const j = await r.json().catch(() => ({})); throw Object.assign(new Error(j.error || "Fleet answered " + r.status), {status: r.status}); }
  return r.blob();
}
async function next(){
  const g = gen, it = cur = q.shift();
  if(!it.blob){ it.blob = fetchWav(it.text); }
  let blob;
  try{ blob = await it.blob; }
  catch(e){ if(g !== gen) return;
    if(e.status === 409){ suspended = true; CV.stop(); say("The Computer voice is not loaded. Load it in Shuttle Bay (Speech).", true); return; }
    if(!shown){ shown = true; say("Voice: " + e.message, true); }
    cur = null; return pump(); }
  if(g !== gen) return;
  const url = URL.createObjectURL(blob);
  A.onended = A.onerror = () => { URL.revokeObjectURL(url); if(g === gen){ cur = null; pump(); } };
  A.src = url;
  try{ await A.play(); if(g === gen) mark("playing"); }
  catch(e){ if(g === gen){ CV.stop(); say("The browser did not allow audio. Tap the speaker once, then try again.", true); } }
}
function say(t, err){ try{ if(cfg.say) cfg.say(t, err); }catch(e){} }
root.CV = CV;
})(typeof window !== "undefined" ? window : globalThis);
if(typeof module !== "undefined") module.exports = globalThis.CV;
