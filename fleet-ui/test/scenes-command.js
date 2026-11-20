/* Command, Red Alert and Auto Destruct on the phone interface: fixtures for /api/redalert, /api/autodestruct
   and /api/boot, and scenes that prove each action sends exactly what the desktop page sends, and that a
   cancel, a late timer or a hidden page sends nothing. All names and numbers are made up. */
"use strict";
const C = require("./scenes-core.js").fixtures;
const RA = "/api/redalert", AD = "/api/autodestruct", CODE = "ALPHA-1", REPO = "https://github.com/pauloesterwitz/Starfleet";
const CHECK = [RA, {check: true}], FIRE = [RA, {}], AD_CHECK = [AD, {code: CODE, check: true}], AD_FIRE = [AD, {code: CODE}];

const REPORT = {ok: true, unloaded: ["demo-chat-32b-starfleet", "demo-embed-small"], stopped: {"jean-luc": ["demo-engine-1"], "kathryn": ["demo-engine-2", "demo-render-1"]},
  orphans: ["demo-app-7"], dormant: ["demo-tools-3", "demo-tools-4"], errors: {}, freed_mb: 84210};
const PARTIAL = {ok: false, unloaded: ["demo-chat-32b-starfleet"], stopped: {"jean-luc": ["demo-engine-1"]}, orphans: [], dormant: [],
  errors: {kathryn: "ssh: connect to host kathryn port 22: Connection timed out"}, freed_mb: 612};
let rebooted = false;      /* Jean-Luc's boot id changes once Auto Destruct has run (variant "rebooted") */

exports.get = (p, variant) => {
  if(p === "/api/boot") return {boot: variant === "rebooted" && rebooted ? "0000b00t-0002" : "0000b00t-0001"};
};
exports.post = (p, body, variant, h) => {
  body = body || {};
  if(p === RA){
    if(!C.keyOk(h)) return C.denied;
    if(body.check) return {ok: true};
    return variant === "partial" ? PARTIAL : variant === "down" ? {__status: 502, body: {error: "llama-swap did not answer the unload request"}} : REPORT;
  }
  if(p === AD){                      /* fleet.py: no code configured, then the code, then the key */
    if(variant === "nocode") return {__status: 503, body: {error: "Auto Destruct is disabled: set STARFLEET_AUTODESTRUCT_CODE in the Fleet UI service environment."}};
    if(String(body.code || "").trim().toUpperCase() !== CODE) return {__status: 403, body: {error: "Authorization code rejected."}};
    if(!C.keyOk(h)) return C.denied;
    if(!body.check && variant === "noreboot") return {__status: 502, body: {error: "reboot NOT scheduled: sudo: a password is required"}};
    rebooted = !body.check;
    return body.check ? {ok: true} : {ok: true, red_alert: REPORT, kathryn_in_s: 1, jean_luc_in_s: 2};
  }
};

const must = (ok, what) => { if(!ok) throw new Error(what); };
const set = (page, o) => page.evaluate(o => Object.assign(PADD._test, o), o);
const slide = (page, name) => page.evaluate(n => { PADD._test.slide(n); }, name);     /* as a completed slide would */
const armed = async (page, t, name) => { await slide(page, name); await t.wait(350); };
const overlay = page => page.evaluate(() => { const a = document.getElementById("p-alert"); return a.hidden ? null : a.innerText; });
const says = async (page, text) => { const o = await overlay(page); must(o && o.toLowerCase().includes(text.toLowerCase()), `the overlay should say "${text}", it says ${JSON.stringify(o)}`); };
const knob = n => `.p-knob[data-arg="${n}"]`;
/* a real drag on the knob, frac of the track */
const drag = (page, name, frac) => page.evaluate(([sel, frac]) => {
  const k = document.querySelector(sel), r = k.getBoundingClientRect(), tr = k.parentNode.getBoundingClientRect(), x0 = r.left + r.width / 2, far = (tr.width - r.width) * frac;
  const ev = (type, x) => k.dispatchEvent(new PointerEvent(type, {pointerId: 9, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: x, clientY: r.top + r.height / 2}));
  ev("pointerdown", x0); for(let i = 1; i <= 8; i++) ev("pointermove", x0 + far * i / 8); ev("pointerup", x0 + far);
}, [knob(name), frac]);
const code = async (page, t, text) => { await armed(page, t, "destruct"); await t.type(".p-code", text); await t.tap('[data-act="cmd-go"]'); };
const enterKey = async t => { await t.type("#p-keyin", "fixture-key"); await t.tap("#p-keygo"); await t.wait(500); };
const hide = (page, on) => page.evaluate(on => { if(on) Object.defineProperty(document, "hidden", {configurable: true, get: () => true}); else delete document.hidden;
  if(on) document.dispatchEvent(new Event("visibilitychange")); }, on);

const S = (name, o) => Object.assign({name: "cmd" + (name ? "-" + name : ""), hash: "#/command", variant: "nominal"}, o);
exports.scenes = [
  S(""),
  S("no-key", {key: false}),
  S("old-shell", {shell: "old", variant: "busy"}),
  S("small", {shell: "small", variant: "busy"}),
  S("key-enter", {key: false, run: async (page, t) => { await t.tap('[data-act="key"]'); await enterKey(t);
    must(await page.locator('[data-act="cmd-keyrm"]').count() === 1, "the panel should show Change and Remove once a key is stored"); }}),
  S("key-remove", {run: async (page, t) => { await t.tap('[data-act="cmd-keyrm"]'); await t.shot("asked"); await t.tap('[data-k="yes"]');
    must(await page.evaluate(() => localStorage.getItem("starfleetKey")) === null, "the key should be gone from this device");
    must(await page.locator('[data-act="cmd-keyrm"]').count() === 0, "the panel should offer Enter key again"); }}),
  S("key-remove-cancel", {run: async (page, t) => { await t.tap('[data-act="cmd-keyrm"]'); await t.tap('[data-k="no"]');
    must(await page.evaluate(() => localStorage.getItem("starfleetKey")) === "fixture-key", "cancel must keep the key"); }}),

  /* ---- Red Alert ---- */
  S("red-armed", {posts: [CHECK], run: async (page, t) => { await armed(page, t, "red"); await says(page, "Seconds to cancel");
    must(await page.evaluate(() => document.documentElement.classList.contains("p-alerting") && !!document.elementFromPoint(innerWidth / 2, innerHeight - 30).closest("#p-alert")),
      "the overlay should lock scrolling and cover the tab bar"); }}),
  S("red-armed-small", {shell: "small", posts: [CHECK], run: (page, t) => armed(page, t, "red")}),
  S("red-armed-old", {shell: "old", posts: [CHECK], run: (page, t) => armed(page, t, "red")}),
  S("red-slide", {posts: [CHECK], run: async (page, t) => {
    await t.tap(knob("red")); await drag(page, "red", .7); await t.wait(400);
    must(!t.posts.length && await overlay(page) === null, "a tap or a short drag must spring back and send nothing");
    await drag(page, "red", 1); await t.wait(400); await says(page, "Seconds to cancel"); await t.shot("armed");
    await t.tap('[data-act="cmd-cancel"]'); await says(page, "Green alert restored"); }}),
  S("red-keyboard", {run: async (page, t) => { await page.locator(knob("red")).focus(); await page.keyboard.press("Enter"); await t.wait(400);
    must(await page.locator('[data-k="yes"]').count() === 1, "Enter on the knob should ask through a sheet"); await t.shot("asked"); await t.tap('[data-k="no"]'); }}),
  S("red-keyboard-yes", {posts: [CHECK], run: async (page, t) => { await page.locator(knob("red")).focus(); await page.keyboard.press("Enter"); await t.wait(400);
    await t.tap('[data-k="yes"]'); await t.wait(300); await says(page, "Seconds to cancel"); await t.tap('[data-act="cmd-cancel"]'); }}),
  S("red-cancel", {posts: [CHECK], run: async (page, t) => { await set(page, {alertSeconds: 2}); await armed(page, t, "red"); await t.tap('[data-act="cmd-cancel"]');
    await says(page, "Green alert restored"); await t.shot("restored"); await t.wait(2600);
    must(await overlay(page) === null, "the overlay should close by itself after a cancel"); }}),
  S("red-complete", {posts: [CHECK, FIRE], run: async (page, t) => { await set(page, {alertSeconds: 1}); await armed(page, t, "red"); await t.wait(1300);
    await says(page, "Red Alert complete"); await says(page, "demo-engine-2"); }}),
  S("red-complete-small", {shell: "small", posts: [CHECK, FIRE], run: async (page, t) => { await set(page, {alertSeconds: 1}); await armed(page, t, "red"); await t.wait(1300);
    await page.evaluate(() => { document.querySelector(".p-arep").scrollTop = 9999; }); }}),
  S("red-complete-return", {posts: [CHECK, FIRE], run: async (page, t) => { await set(page, {alertSeconds: 1}); await armed(page, t, "red"); await t.wait(1300);
    await t.tap('[data-act="cmd-close"]'); must(await overlay(page) === null, "Return to command should close the report"); }}),
  S("red-partial", {variant: "partial", shell: "old", posts: [CHECK, FIRE], run: async (page, t) => { await set(page, {alertSeconds: 1}); await armed(page, t, "red"); await t.wait(1300);
    await says(page, "Not everything stopped"); }}),
  S("red-failed", {variant: "down", posts: [CHECK, FIRE], run: async (page, t) => { await set(page, {alertSeconds: 1}); await armed(page, t, "red"); await t.wait(1300);
    await says(page, "Red Alert failed"); }}),
  /* the busy state, held open: the real POST is swallowed in the page, so only the check leaves it */
  S("red-busy", {posts: [CHECK], run: async (page, t) => { await set(page, {alertSeconds: 1});
    await page.evaluate(() => { const post = PADD.api.post; PADD.api.post = (u, b, o) => u === "/api/redalert" && !b.check ? new Promise(() => {}) : post(u, b, o); });
    await armed(page, t, "red"); await t.wait(1300); await says(page, "Do not power off"); }}),
  /* a tick that arrives 3 s late, with the deadline already behind it, must cancel and never fire */
  S("red-late-tick", {posts: [CHECK], run: async (page, t) => { await set(page, {alertSeconds: 2}); await armed(page, t, "red");
    await page.evaluate(() => PADD._test.lateTick()); await t.wait(1500);
    await says(page, "Green alert restored"); await says(page, "interrupted"); must(t.posts.length === 1, "a late tick sent something"); }}),
  S("red-hidden", {posts: [CHECK], run: async (page, t) => { await set(page, {alertSeconds: 1}); await armed(page, t, "red");
    await hide(page, true); await t.wait(1600); await hide(page, false);
    await says(page, "Green alert restored"); await says(page, "left the screen"); must(t.posts.length === 1, "a hidden page sent something"); }}),
  /* the check comes back while the page is already hidden: no visibilitychange will follow, and still nothing may fire */
  S("red-hidden-at-open", {posts: [CHECK], run: async (page, t) => { await set(page, {alertSeconds: 1});
    await page.evaluate(() => Object.defineProperty(document, "hidden", {configurable: true, get: () => true}));
    await armed(page, t, "red"); await t.wait(1400); await hide(page, false);
    await says(page, "Green alert restored"); must(t.posts.length === 1, "a window that opened on a hidden page sent something"); }}),
  S("red-route", {posts: [CHECK], run: async (page, t) => { await armed(page, t, "red"); await page.evaluate(() => { location.hash = "#/bridge"; }); await t.wait(400);
    await says(page, "Seconds to cancel"); await t.tap('[data-act="cmd-cancel"]'); await says(page, "Green alert restored"); }}),
  S("red-no-key", {key: false, posts: [CHECK, CHECK], run: async (page, t) => { await slide(page, "red"); await t.wait(500);
    must(await overlay(page) === null && await page.locator("#p-keyin").count() === 1, "without a key the key sheet opens and nothing is armed"); await t.shot("asked");
    await enterKey(t); await says(page, "Seconds to cancel"); await t.tap('[data-act="cmd-cancel"]'); }}),
  S("red-no-key-dismissed", {key: false, posts: [CHECK], run: async (page, t) => { await slide(page, "red"); await t.wait(500); await t.tap(".p-x"); await t.wait(300);
    must(await overlay(page) === null, "closing the key sheet must not arm anything"); }}),

  /* ---- Auto Destruct ---- */
  S("destruct-sheet", {run: (page, t) => armed(page, t, "destruct")}),
  S("destruct-sheet-small", {shell: "small", run: (page, t) => armed(page, t, "destruct")}),
  S("destruct-abort", {run: async (page, t) => { await armed(page, t, "destruct"); await t.type(".p-code", "alpha-1"); await t.tap('[data-act="cmd-abort"]');
    must(await page.locator(".p-code").count() === 0 || !(await page.locator(".p-sheet.on").count()), "Abort should close the sheet"); }}),
  S("destruct-wrong-code", {posts: [[AD, {code: "OMEGA-9", check: true}]], run: async (page, t) => { await code(page, t, "omega-9");
    must(/Access denied/.test(await page.locator(".p-sheet").innerText()) && await overlay(page) === null, "a wrong code should say Access denied and arm nothing"); }}),
  S("destruct-no-code-set", {variant: "nocode", posts: [AD_CHECK], run: async (page, t) => { await code(page, t, "alpha-1");
    must(/no authorization code is set/.test(await page.locator(".p-sheet").innerText()), "503 should say that no code is configured"); }}),
  S("destruct-armed", {posts: [AD_CHECK], run: async (page, t) => { await code(page, t, " alpha-1 "); await says(page, "Seconds to cancel"); await says(page, "Cancel auto destruct"); }}),
  S("destruct-armed-small", {shell: "small", posts: [AD_CHECK], run: (page, t) => code(page, t, "alpha-1")}),
  S("destruct-cancel", {posts: [AD_CHECK], run: async (page, t) => { await set(page, {destructSeconds: 2}); await code(page, t, "alpha-1"); await t.tap('[data-act="cmd-cancel"]');
    await says(page, "Auto destruction cancelled"); await t.wait(2200); must(t.posts.length === 1, "a cancelled Auto Destruct sent something"); }}),
  S("destruct-late-tick", {posts: [AD_CHECK], run: async (page, t) => { await set(page, {destructSeconds: 2}); await code(page, t, "alpha-1");
    await page.evaluate(() => PADD._test.lateTick()); await t.wait(1500); await says(page, "Auto destruction cancelled"); }}),
  S("destruct-hidden", {posts: [AD_CHECK], run: async (page, t) => { await set(page, {destructSeconds: 1}); await code(page, t, "alpha-1");
    await hide(page, true); await t.wait(1600); await hide(page, false); await says(page, "Auto destruction cancelled"); }}),
  S("destruct-no-key", {key: false, posts: [AD_CHECK, AD_CHECK], run: async (page, t) => { await code(page, t, "alpha-1"); await t.wait(300);
    must(await page.locator("#p-keyin").count() === 1, "the right code without a key should open the key sheet"); await t.shot("asked");
    await enterKey(t); await says(page, "Seconds to cancel"); await t.tap('[data-act="cmd-cancel"]'); }}),
  S("destruct-failed", {variant: "noreboot", posts: [AD_CHECK, AD_FIRE], run: async (page, t) => { await set(page, {destructSeconds: 1}); await code(page, t, "alpha-1");
    await t.wait(1300); await says(page, "Auto Destruct failed"); await says(page, "reboot NOT scheduled"); }}),
  /* through to the end: the cancel window, the real call, the countdown to the reboot, a new boot id */
  S("destruct-complete", {variant: "rebooted", posts: [AD_CHECK, AD_FIRE], run: async (page, t) => { await set(page, {destructSeconds: 1}); await code(page, t, "alpha-1");
    await t.wait(1100); await says(page, "Seconds to self-destruct"); await t.shot("countdown");
    await t.wait(2300); await says(page, "All systems online"); await t.shot("online");
    await t.wait(2800); must(await overlay(page) === null, "the overlay should close once both Sparks are back"); }}),
  /* the boot id never changes here: the wait goes on, and Return to command leaves it */
  S("destruct-reboot-wait", {posts: [AD_CHECK, AD_FIRE], run: async (page, t) => { await set(page, {destructSeconds: 1}); await code(page, t, "alpha-1");
    await t.wait(4300); await says(page, "Systems rebooting"); await t.shot("waiting");
    await t.tap('[data-act="cmd-close"]'); must(await overlay(page) === null, "Return to command should leave the reboot wait"); }}),

  /* ---- Help, About, Desktop page ---- */
  S("help", {run: (page, t) => t.tap('[data-act="cmd-help"]')}),
  S("help-small", {shell: "small", run: async (page, t) => { await t.tap('[data-act="cmd-help"]'); await page.evaluate(() => { document.querySelector(".p-shb").scrollTop = 9999; }); }}),
  S("about", {run: async (page, t) => { await t.tap('[data-act="cmd-about"]');
    must(await page.locator(`.p-sheet a[href="${REPO}"]`).count() === 1, "About should link the public repository"); }}),
  /* the link: a new tab where there is one, the app's bridge inside the app, and never a request from this page */
  S("about-link", {run: async (page, t) => { await t.tap('[data-act="cmd-about"]');
    await page.evaluate(() => { window.__open = []; window.open = u => { window.__open.push(u); return null; }; });
    await t.tap('[data-act="cmd-ext"]');
    must((await page.evaluate(() => window.__open)).join() === REPO && await page.locator(".p-toast").count() === 1, "without the bridge the link tries a new tab and says so when it cannot open");
    await page.evaluate(() => { window.__msg = []; Object.defineProperty(window, "webkit", {configurable: true, value: {messageHandlers: {padd: {postMessage: m => window.__msg.push(m)}}}}); });
    await t.tap('[data-act="cmd-ext"]');
    const sent = (await page.evaluate(() => window.__msg)).filter(m => m.type === "open");
    must(sent.length === 1 && sent[0].url === REPO && (await page.evaluate(() => window.__open.length)) === 1, "inside the app the link goes through the bridge"); }}),
  S("desktop", {run: async (page, t) => { await t.tap('[data-act="cmd-desktop"]'); await t.shot("asked"); await t.tap('[data-k="no"]');
    must(/\/padd#\/command$/.test(page.url()), "cancel must stay on this page"); }}),
  S("desktop-yes", {run: async (page, t) => { await t.tap('[data-act="cmd-desktop"]'); await t.tap('[data-k="yes"]'); await t.wait(400);
    must(/\/desktop$/.test(page.url()), "Open should go to /desktop, it went to " + page.url()); }}),
];
