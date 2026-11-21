/* Scenes for the Computer voice on the phone page (vendor/computer-voice.js wired into vendor/padd-computer.js).
   The harness cannot answer with audio, so each scene answers /api/computer/speak itself at page level (a page route wins over the
   harness's own) and keeps its own list of requests. The speaker is replaced by a stub: it never loads a source (headless WebKit has no
   audio codecs, so the real element would fire an error at once), records every play and "ends" it after a while.
   The fixtures for models, the stored chat and the answer stream come from scenes-computer.js (variant "comp"). Everything is made up. */
"use strict";
const ID = "c0ffee0000000001", Q = "When did the last off-site backup run, and how big is it?";
const M1 = "qwen38fn-sglang-tp2-starfleet", ALL = ["wiki", "vane", "google"];

function wav(){                                        /* 0.2 s of silence, 32 kHz mono PCM16 */
  const n = 6400, b = Buffer.alloc(44 + n * 2);
  b.write("RIFF", 0); b.writeUInt32LE(36 + n * 2, 4); b.write("WAVEfmt ", 8); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(32000, 24); b.writeUInt32LE(64000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(n * 2, 40);
  return b;
}
async function voice(page, mode, ms){
  const log = [];
  await page.route("**/api/computer/speak", async route => {
    const req = route.request(); let body = null; try{ body = JSON.parse(req.postData()); }catch(e){}
    log.push({text: body && body.text, key: req.headers()["x-starfleet-key"]});
    if(mode === "off") return route.fulfill({status: 409, contentType: "application/json", body: JSON.stringify({error: "The Computer voice is not loaded. Load it in Shuttle Bay (Speech)."})});
    return route.fulfill({status: 200, contentType: "audio/wav", body: wav()});
  });
  await page.evaluate(ms => {
    localStorage.setItem("computerVoiceLocal", "0");        /* these scenes test Fleet's voice route; the on-device engine has its own test */
    window.__played = [];
    Object.defineProperty(HTMLMediaElement.prototype, "src", {configurable: true, get(){ return this.__src || ""; }, set(v){ this.__src = String(v); }});
    const rm = HTMLElement.prototype.removeAttribute; HTMLMediaElement.prototype.removeAttribute = function(n){ if(n === "src") this.__src = ""; else rm.call(this, n); };
    HTMLMediaElement.prototype.play = function(){ window.__played.push(this.src); const el = this, src = this.src, tok = this.__tok = {};
      setTimeout(() => { if(el.__tok === tok && el.src === src) el.dispatchEvent(new Event("ended")); }, ms); return Promise.resolve(); };
    HTMLMediaElement.prototype.pause = function(){ this.__tok = null; };
    HTMLMediaElement.prototype.load = function(){};
  }, ms || 40);
  return log;
}
const must = async (page, fn, arg, what) => { if(!await page.evaluate(fn, arg)) throw new Error(what); };
const ok = (cond, what) => { if(!cond) throw new Error(what); };
const state = page => page.evaluate(() => { const b = document.querySelector(".p-spk"); return b ? b.dataset.state || "" : "none"; });
const played = page => page.evaluate(() => window.__played.length - 1);       /* minus the silent clip that unlocks the speaker */
const ta = ".p-input textarea", main = '[data-act="comp-main"]', voiceChip = '[data-act="comp-voice"]';
const S = (name, o) => Object.assign({name: "voice-" + name, hash: "#/computer/" + ID, variant: "comp"}, o);

exports.scenes = [
  S("speak-stored", {run: async (page, t) => {
    const log = await voice(page, "on", 500);
    await t.wait(500);
    await t.tap(".p-spk");                                                    /* the tap itself waits 380 ms; the first piece plays for 500 */
    ok(["loading", "playing"].includes(await state(page)), "the button shows no state right after the tap: " + JSON.stringify(await state(page)));
    await t.wait(2200);
    ok(log.length === 2, "expected 2 requests, got " + JSON.stringify(log.map(l => l.text)));
    ok(log[1].text === "It protects 1.85 TB in 27,862 files. List the snapshots yourself: Sources: Backup notes and the retention guide.", "second request: " + log[1].text);
    ok(log.every(l => l.key === "fixture-key"), "a request went out without the Starfleet key");
    ok(log.every(l => l.text && l.text.length <= 330), "a request is empty or too long");
    ok(log.every(l => !/[|`*]|https?:|!\[|\bbash\b|demo-backup/.test(l.text)), "code, table, URL or image text reached the voice: " + JSON.stringify(log.map(l => l.text)));
    ok(log[0].text === "The last snapshot finished at 03:32 this morning and took 2 minutes.", "first request: " + log[0].text);
    ok(await played(page) === 2, `${log.length} requests but ${await played(page)} plays`);
    ok(await state(page) === "", "the button still shows a state after the last sentence: " + await state(page));
  }}),
  S("speak-stop", {run: async (page, t) => {
    const log = await voice(page, "on", 700);
    await t.wait(500);
    await t.tap(".p-spk");
    await t.wait(300);
    const before = log.length;
    ok(before >= 1 && before <= 3, "prefetch should stay within 3 requests, got " + before);
    await t.tap(".p-spk");                                                   /* the same button stops it */
    ok(await state(page) === "", "Stop left a state on the button: " + await state(page));
    const heard = await played(page);
    await t.wait(1500);
    ok(log.length === before && await played(page) === heard, `something kept going after Stop: requests ${before} -> ${log.length}, plays ${heard} -> ${await played(page)}`);
  }}),
  S("speak-leave", {run: async (page, t) => {                                /* leaving the screen ends the speech: its Stop button is gone */
    const log = await voice(page, "on", 700);
    await t.wait(500);
    await t.tap(".p-spk");
    await t.wait(300);
    const before = log.length, heard = await played(page);
    ok(before >= 1, "nothing was requested before leaving");
    await page.evaluate(() => { window.__btn = document.querySelector(".p-spk"); });          /* the screen's nodes are detached once it is left */
    await page.evaluate(() => PADD.go("#/models"));
    await t.wait(1600);
    ok(log.length === before && await played(page) === heard, `speech went on after leaving the screen: requests ${before} -> ${log.length}, plays ${heard} -> ${await played(page)}`);
    ok(await page.evaluate(() => !window.__btn.dataset.state), "the Speak button still shows a state after leaving");
  }}),
  S("speak-local-fallback", {run: async (page, t) => {                     /* on-device engine switched on but its files are missing: Fleet's voice takes over, once told */
    const log = await voice(page, "on");
    await page.evaluate(() => localStorage.setItem("computerVoiceLocal", "1"));
    await t.wait(500);
    await t.tap(".p-spk");
    await t.wait(2500);
    ok(log.length >= 1, "Fleet's voice was not used after the on-device engine failed");
    await must(page, () => /on-device voice is not available/.test(document.getElementById("p-toast").textContent), null, "no toast says the on-device voice failed");
  }}),
  S("speak-not-loaded", {run: async (page, t) => {
    const log = await voice(page, "off");
    await t.wait(500);
    await t.tap(".p-spk");
    await t.wait(400);
    await must(page, () => /not loaded/.test(document.getElementById("p-toast").textContent), null, "no toast says the voice is not loaded");
    const first = log.length;
    ok(await state(page) === "" && first >= 1 && first <= 2, `state "${await state(page)}", ${first} requests: a refusal must end it after the prefetched ones`);
    await t.wait(600);
    ok(log.length === first, "requests continued after the refusal");
    await t.tap(".p-spk"); await t.wait(400);
    ok(log.length > first, "a second tap should ask again");
  }}),
  S("auto-stream", {hash: "#/computer", posts: [["/api/computer/send", {id: null, text: Q, model: M1, tools: ALL, images: []}]], run: async (page, t) => {
    const log = await voice(page, "on");
    await t.tap(voiceChip);
    await must(page, () => localStorage.getItem("computerVoice") === "1", null, "the Voice chip did not store computerVoice=1");
    await must(page, () => document.querySelector('[data-act="comp-voice"]').getAttribute("aria-pressed") === "true", null, "the Voice chip is not pressed");
    await t.type(ta, Q); await t.wait(300); await t.tap(main); await t.wait(2000);
    ok(log.length === 2, "the streamed answer should make 2 requests, got " + JSON.stringify(log.map(l => l.text)));
    ok(log[0].text === "The last snapshot finished at 03:32 this morning and took 2 minutes.", "first spoken piece: " + log[0].text);
    ok(log[1].text === "It protects 1.85 TB in 27,862 files. The next one runs tonight at 03:30.", "second spoken piece: " + log[1].text);
    ok(await played(page) === 2 && await state(page) === "", `plays ${await played(page)}, state "${await state(page)}"`);
  }}),
  S("auto-persists", {hash: "#/computer", run: async (page, t) => {
    await t.tap(voiceChip);
    await page.reload(); await t.wait(900);
    await must(page, () => document.querySelector('[data-act="comp-voice"]').getAttribute("aria-pressed") === "true", null, "the Voice switch did not survive a reload");
    await t.tap(voiceChip);
    await must(page, () => localStorage.getItem("computerVoice") === "0", null, "switching it off did not store 0");
  }}),
];
