/* ================= Computer voice, on this device =================
   Runs the distilled Computer voice inside the page: Piper phonemizer (espeak-ng, WebAssembly) -> the voice model (onnxruntime-web, WebAssembly)
   -> WAV. Works the same in the Mac app, the iPhone app and a browser; nothing leaves the device after the one-time download.
   Files: the runtime comes from Fleet's /voice-rt/ (public code), the two voice models from /api/computer/voice-model/<lang>.onnx|.json
   (Starfleet key; they carry the imitated voice) and are kept in IndexedDB, so they are fetched once per device.
   CVLocal.synth(text, lang, headers) -> Promise<Blob> (audio/wav). Pure helpers (guess, ids, wav) are exported for ~/fleet-ui-test/computer-voice-test.js. */
(function(root){
"use strict";
const DE = new Set("der die das und ist nicht ein eine zu mit für auf dass den dem des sich von im auch es wir sie ich wird sind oder aber als bei nach wie noch kann habe haben wurde werden dieser diese hier ihre einen einem einer sehr mehr nur schon".split(" "));
const EN = new Set("the and is of to in that it for with as are on be this was by not or from have an at you your will can has had were their which would there what about into than them these those more only just".split(" "));
function guess(text){                                     /* the same rule as the server's: stop words, umlauts count double, ties go to English */
  const low = text.toLowerCase(), w = low.match(/[a-zäöüß]+/g) || [];
  const de = w.filter(x => DE.has(x)).length + 2 * (low.match(/[äöüß]/g) || []).length;
  return de > w.filter(x => EN.has(x)).length ? "de" : "en";
}
/* phonemes of one sentence -> Piper ids: BOS, pad, then each phoneme followed by a pad, EOS (a phoneme the voice does not know is skipped) */
function ids(phonemes, map){
  const o = [...map["^"], ...map["_"]];
  for(const p of phonemes){ const m = map[p]; if(m){ o.push(...m, ...map["_"]); } }
  return o.concat(map["$"]);
}
function sentences(phonemes){                             /* the phonemizer returns one flat list: cut after . ? ! (and the space after it) */
  const out = []; let cur = [];
  for(let i = 0; i < phonemes.length; i++){
    const p = phonemes[i]; cur.push(p);
    if(/^[.?!]$/.test(p)){ if(phonemes[i + 1] === " ") i++; out.push(cur); cur = []; }
  }
  if(cur.some(p => p !== " ")) out.push(cur);
  return out;
}
function wav(f32, sr){
  const b = new ArrayBuffer(44 + f32.length * 2), v = new DataView(b), w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + f32.length * 2, true); w(8, "WAVEfmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, f32.length * 2, true);
  for(let i = 0; i < f32.length; i++){ const x = Math.max(-1, Math.min(1, f32[i])); v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true); }
  return b;
}
function polish(y, sr){                                   /* like the server: short lead and tail, 5 ms fades, peak at 0.8 */
  let pk = 0; for(const x of y) pk = Math.max(pk, Math.abs(x)); if(!pk) return y;
  const hop = Math.max(1, Math.floor(sr / 100)); let a = 0, b = y.length;
  const loud = i => { let m = 0; for(let j = i; j < Math.min(i + hop, y.length); j++) m = Math.max(m, Math.abs(y[j])); return m > 0.02 * pk; };
  while(a < y.length && !loud(a)) a += hop; while(b > a && !loud(b - hop)) b -= hop;
  a = Math.max(0, a - Math.floor(0.06 * sr)); b = Math.min(y.length, b + Math.floor(0.12 * sr));
  const o = y.slice(a, b), g = 0.8 / pk, f = Math.floor(0.005 * sr);
  for(let i = 0; i < o.length; i++) o[i] *= g;
  for(let i = 0; i < f && i < o.length; i++){ o[i] *= i / f; o[o.length - 1 - i] *= i / f; }
  return o;
}
if(typeof document === "undefined"){ root.CVLocal = {guess, ids, sentences, wav, polish}; return; }

const RT = "/voice-rt/", cfg = {}, sess = {};
let rtP = null, ph = null, out = [];
const script = u => new Promise((ok, bad) => { const s = document.createElement("script"); s.src = u; s.onload = ok; s.onerror = () => bad(new Error("could not load " + u)); document.head.appendChild(s); });
function runtime(){
  return rtP || (rtP = (async () => {
    await script(RT + "ort.wasm.min.js"); await script(RT + "piper_phonemize.js");
    ort.env.wasm.wasmPaths = RT; ort.env.wasm.numThreads = 1; ort.env.wasm.proxy = false;        /* one thread: no cross-origin isolation needed */
    ph = await createPiperPhonemize({ print: l => out.push(l), printErr: () => {}, locateFile: u => RT + u });
  })().catch(e => { rtP = null; throw e; }));
}
/* IndexedDB, because Cache Storage needs https and Fleet is often served over http on the tailnet */
const db = () => new Promise((ok, bad) => { const r = indexedDB.open("computer-voice", 1); r.onupgradeneeded = () => r.result.createObjectStore("f"); r.onsuccess = () => ok(r.result); r.onerror = () => bad(r.error); });
const idb = async (mode, fn) => { const d = await db(); return new Promise((ok, bad) => { const t = d.transaction("f", mode), r = fn(t.objectStore("f")); t.oncomplete = () => ok(r && r.result); t.onerror = () => bad(t.error); }); };
async function file(name, headers){
  const hit = await idb("readonly", s => s.get(name)).catch(() => null);
  if(hit) return hit;
  const r = await fetch("/api/computer/voice-model/" + name, { headers });
  if(!r.ok){ const j = await r.json().catch(() => ({})); throw Object.assign(new Error(j.error || "Fleet answered " + r.status), { status: r.status }); }
  const buf = await r.arrayBuffer();
  await idb("readwrite", s => s.put(buf, name)).catch(() => {});                         /* a full disk only means: download again next time */
  return buf;
}
async function voice(lang, headers){
  if(sess[lang]) return sess[lang];
  return sess[lang] = (async () => {
    await runtime();
    cfg[lang] = JSON.parse(new TextDecoder().decode(await file(lang + ".onnx.json", headers)));
    return ort.InferenceSession.create(new Uint8Array(await file(lang + ".onnx", headers)), { executionProviders: ["wasm"] });
  })().catch(e => { delete sess[lang]; throw e; });
}
async function synth(text, lang, headers){
  lang = lang || guess(text);
  const s = await voice(lang, headers), c = cfg[lang], sr = c.audio.sample_rate;
  out.length = 0;
  ph.callMain(["-l", lang === "de" ? "de" : "en-us", "--input", JSON.stringify([{ text }]), "--espeak_data", "/espeak-ng-data"]);
  const phon = JSON.parse(out[0]).phonemes || [], parts = [];
  for(const sent of sentences(phon)){
    const id = ids(sent, c.phoneme_id_map); if(id.length < 5) continue;
    const r = await s.run({ input: new ort.Tensor("int64", BigInt64Array.from(id, BigInt), [1, id.length]), input_lengths: new ort.Tensor("int64", BigInt64Array.from([BigInt(id.length)]), [1]),
      scales: new ort.Tensor("float32", Float32Array.from([0.5, 1.0, 0.6]), [3]) });     /* noise, length, noise_w: the calm settings the voice was tuned with */
    parts.push(r.output.data);
  }
  const n = parts.reduce((a, p) => a + p.length, 0); if(!n) throw Object.assign(new Error("nothing to say"), { status: 400 });
  const y = new Float32Array(n); let o = 0; for(const p of parts){ y.set(p, o); o += p.length; }
  return new Blob([wav(polish(y, sr), sr)], { type: "audio/wav" });
}
root.CVLocal = { synth, guess, ids, sentences, wav, polish, ready: lang => voice(lang) };
})(typeof window !== "undefined" ? window : globalThis);
if(typeof module !== "undefined") module.exports = globalThis.CVLocal;
