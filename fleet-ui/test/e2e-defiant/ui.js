#!/usr/bin/env node
/* The real pages against the real Fleet of the end to end test, in WebKit: the desktop page (/desktop#def, the Mac
   app window) or the phone page (/padd#/systems/defiant, the iPhone app). e2e.py starts Fleet and the made-up Defiant
   and runs this; the steps change what the made-up Defiant says by writing the files in E2E_HOST (apt-sim.txt,
   job.conf, apt-lock, scan-delay). The key is the made-up string fixture-key, stored where the pages store theirs.
   Steps: no scan yet, a failed first scan, a slow first scan (Scanning...), the data, Scan now, a wrong key, the
   confirm step, the progress, the result (an install that needs a reboot is followed by Fleet's own: down, back, the
   rescan), a failed install, and an install that apt refuses because it would have to remove a package no scan showed
   (the hint, then the removal named in the next scan and confirm step); then the Reboot Defiant button by itself: a wrong
   key, the confirm step with its guard, a refusal right after a reboot, the running reboot, the finished one and a reboot
   that does not happen (failed); last an install whose reboot cannot start, and the install confirm while a reboot is
   already waiting (the definite sentence). Every install the pages send carries the flag that lets Fleet reboot afterwards
   ({"reboot_ok": true}), so the reboot that follows an install is the proof that they send it. Pictures go to OUT, one
   line per check to stdout.

   Usage: PW=<playwright package> E2E_BASE=http://127.0.0.1:PORT E2E_HOST=<host folder> E2E_SIMS=<json: name -> file>
          E2E_GAP=<REBOOT_GAP of this Fleet, seconds> OUT=<folder> node ui.js desktop|phone
          (FONTS=1 lets the pages load Google Fonts for prettier pictures) */
"use strict";
const fs = require("fs"), path = require("path");
const { webkit, devices } = require(process.env.PW || "playwright");
const WHICH = process.argv[2], BASE = process.env.E2E_BASE, HOST = process.env.E2E_HOST, SIMS = JSON.parse(process.env.E2E_SIMS || "{}");
const OUT = process.env.OUT || path.join(__dirname, "out"), KEY = "fixture-key", PHONE = WHICH === "phone";
const sleep = ms => new Promise(r => setTimeout(r, ms));
const hostPath = f => path.join(HOST, f), hostWrite = (f, text) => fs.writeFileSync(hostPath(f), text);
const pendingReboot = on => ["reboot-required", "reboot-required.pkgs"].forEach((f, i) => on ? hostWrite("var/run/" + f, i ? "linux-image-6.8.0-99-generic\nlibc6\n" : "") : fs.rmSync(hostPath("var/run/" + f), { force: true }));
const sim = name => fs.copyFileSync(SIMS[name], hostPath("apt-sim.txt"));
const HOSTILE = '<img src=x onerror="window.__pwned=1"><b id="evil">x</b>', quote = t => "'" + t.replace(/'/g, "'\\''") + "'";     // what a host must not be able to inject
const units = what => (fs.existsSync(hostPath("calls.log")) ? fs.readFileSync(hostPath("calls.log"), "utf8").split("\n") : []).filter(l => l.split(" ")[1] === "systemd-run" && l.includes("--unit=" + what)).length;
const started = () => units("fleet-apt-upgrade "), rebooted = () => units("fleet-reboot ");      /* installs and reboots Fleet asked the host for (a unit's name ends the option) */
const E_AUTO = "The reboot after the install did not start: apt is busy on Defiant, an upgrade may be running. Try again in a few minutes. Reboot Defiant when apt is idle.";      /* what Fleet says when the reboot after an install cannot start */
const E_SAME = "Defiant is still on the same boot three minutes after the request. It may still reboot. If it does not, check it in your hosting provider's console.";      /* what Fleet says about a reboot that never happened */
const HINT = "Fleet: apt would remove packages, nothing was changed. Rescanning now: check the list, then install again.";
const WAITING = /A reboot is already waiting, so Defiant reboots by itself right after this install, and the sites are unreachable for about a minute\./i;      /* the install confirm while the latest scan already wants a reboot (the Mac page's text is shown in capitals: innerText) */
let failed = 0;

function check(name, ok, detail) {
  if (!ok) failed++;
  console.log((ok ? "ok   " : "FAIL ") + WHICH + ": " + name + (ok ? "" : "  [" + String(detail).replace(/\s+/g, " ").slice(0, 500) + "]"));
  return !!ok;
}
function checks(name, conds, detail) {                    /* one check made of named parts, so that a failure says which part */
  const bad = Object.entries(conds).filter(([, v]) => !v).map(([k]) => k);
  return check(name, !bad.length, (bad.length ? "failed: " + bad.join("; ") + " | " : "") + detail);
}
async function until(fn, ms = 25000) {                     // true as soon as fn() is true, false when the time is up
  for (const end = Date.now() + ms; Date.now() < end; await sleep(250)) { try { if (await fn()) return true; } catch (e) { /* not there yet */ } }
  return false;
}

const rescanned = p => until(() => p.evaluate(() => fetch("/api/defiant/updates").then(r => r.json()).then(u => !!u.scan && !u.scan.busy && u.scan.at > u.job.ended_at)));

async function session(browser, key) {
  const ctx = await browser.newContext(PHONE ? Object.assign({}, devices["iPhone 15"], { viewport: { width: 393, height: 852 }, deviceScaleFactor: 2 })
                                             : { viewport: { width: 1280, height: 900 } });
  const errors = [], strays = [], fonts = /^fonts\.(googleapis|gstatic)\.com$/;
  await ctx.route("**/*", route => {                       // nothing leaves the sandbox: not even the fonts, unless asked (see the proxy where the browser starts)
    const u = new URL(route.request().url());
    if (u.origin === BASE || (process.env.FONTS && fonts.test(u.host))) return route.continue();
    if (!fonts.test(u.host)) strays.push(u.origin);
    return route.abort();
  });
  await ctx.addInitScript(k => { try { if (k) localStorage.setItem("starfleetKey", k); else localStorage.removeItem("starfleetKey"); localStorage.removeItem("paddRoute"); } catch (e) { /* private mode */ } }, key);
  const page = await ctx.newPage();
  page.on("pageerror", e => errors.push("page error: " + e.message));
  /* the dead proxy refuses the font preconnects of both pages, and WebKit says so in the console: expected, and only for those two hosts */
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource|Failed to preconnect to https:\/\/fonts\.(googleapis|gstatic)\.com/.test(m.text())) errors.push("console: " + m.text()); });
  return { page, shot: n => page.screenshot({ path: path.join(OUT, `e2e-${WHICH}-${n}.png`) }),
           end: async what => { await ctx.close(); check(`${what}: no script error, no request left the sandbox`, !errors.length && !strays.length, errors.concat(strays)); } };
}

/* ------------------------------------------------------------------ the desktop page */
async function desktop(browser) {
  hostWrite("apt-lock", ""); sim("many");
  let s = await session(browser, KEY), p = s.page;
  const block = () => p.locator("#updbox").innerText().catch(() => ""), has = re => async () => re.test(await block());
  const button = name => p.locator(`#updbox .node:has(h3:text-is("${name}")) button.bigbtn`), rows = () => p.locator("#updbox table tbody tr").count();
  const rbtn = () => p.locator("#rebbox button.bigbtn"), rbox = () => p.locator("#rebbox").innerText().catch(() => ""), rhas = re => async () => re.test(await rbox());
  const rnote = () => p.locator("#rebbox .updnote").first().innerText().catch(() => ""), rkind = () => p.locator("#rebbox .updnote").first().getAttribute("class").catch(() => "");
  const below = () => p.locator("#updbox .nodes > .updnote");   /* the reboot line under the install panel */
  const RB_HINT = /Reboots the whole server\. All sites are unreachable for about a minute\. Needs the Starfleet key\./i;
  await p.goto(BASE + "/desktop#def");
  check("before the first scan: No scan yet, Install is off and says why, Scan now is on", await until(has(/No scan yet/i)) && await button("Install Updates").isDisabled()
        && /No scan yet: scan first/i.test(await block()) && await button("Scan").isEnabled(), await block());
  await s.shot("1-no-scan");
  await button("Scan").click();
  check("a failed first scan shows its error and no package table", await until(has(/Scan failed .*Could not get lock/i)) && await rows() === 0 && await button("Scan").isEnabled(), await block());
  await s.shot("2-first-scan-failed");
  fs.rmSync(hostPath("apt-lock")); hostWrite("scan-delay", "5"); await sleep(2500);
  await button("Scan").click();
  check("while the first scan runs the page says Scanning... and shows no package table", await until(has(/Scanning\.\.\./i)) && await rows() === 0 && await button("Install Updates").isDisabled(), await block());
  await s.shot("3-scanning");
  hostWrite("scan-delay", "0");
  check("the first scan appears by itself: 20 packages, 11 security, 21 held back", await until(async () => await rows() === 20)
        && /20\s*to install/i.test(await block()) && /Security\s*11/i.test(await block()) && /Held back\s*21/i.test(await block())
        && /21 held back by Ubuntu's staged rollout/i.test(await block()), await block());
  check("security packages come first and carry the Security tag, third parties are marked", await p.locator("#updbox table tbody tr:first-child .tag.k-sec").count() === 1
        && await p.locator("#updbox .tag.k-sec").count() === 11 && await p.locator("#updbox .tag.k-3p").count() === 5
        && /apache2-bin/i.test(await p.locator("#updbox table tbody tr:first-child").innerText()), await block());
  check("the scan times and the Install button are shown", /Last scan .* · next /i.test(await block()) && await button("Install Updates").isEnabled(), await block());
  checks("the Install hint says that Fleet reboots Defiant when a reboot is needed afterwards, no longer that it never does", {
    "the new hint": /If a reboot is needed afterwards, Fleet reboots Defiant\./i.test(await block()), "no 'never reboots'": !/never reboots/i.test(await block()) }, await block());
  checks("the Server block has the Reboot Defiant button next to the Reboot figure: enabled, plain, with its hint", {
    "the text": await until(rhas(/Reboot Defiant/i)) && /^reboot defiant$/i.test(await rbtn().innerText()), enabled: await rbtn().isEnabled(),
    plain: !/\b(warn|armed)\b/.test(await rbtn().getAttribute("class")), hint: RB_HINT.test(await rbox()), "no line below the install panel": await below().count() === 0,
    "in the first Server panel": await p.locator("#defbody .node").first().locator("#rebbox").count() === 1 }, await rbox());
  await s.shot("4-data");
  await p.reload(); await p.waitForSelector("#updbox table");
  check("the Server panel and the problems carry the update figures", await until(async () => /Updates\s*20/i.test((await p.locator("#defbody .node").first().innerText()).replace(/\n/g, ""))
        && /11 security updates pending/i.test(await p.locator("#defmsg").innerText())), await p.locator("#defmsg").innerText());

  sim("three"); await sleep(2200);
  await button("Scan").click();
  check("Scan now shows what the new scan found", await until(async () => await rows() === 5) && /Security\s*3/i.test(await block()) && /Held back\s*1/i.test(await block()), await block());
  await s.shot("5-scan-now");
  sim("many"); await sleep(2200);
  await button("Scan").click();
  await until(async () => await rows() === 20);
  await s.end("desktop page");

  s = await session(browser, "wrong-key"); p = s.page;
  await p.goto(BASE + "/desktop#def"); await until(async () => await rows() === 20);
  await button("Install Updates").click(); await sleep(550); await button("Install Updates").click();
  check("Install with a wrong key shows the same 403 wording as Back up now, and starts nothing", await until(async () => /Install updates failed: Starfleet key missing or wrong \(enter it on the Shuttle Bay page, Save key\)/i.test(await p.locator("#defmsg").innerText()))
        && started() === 0, await p.locator("#defmsg").innerText());
  await s.shot("6-wrong-key");
  await rbtn().click(); await sleep(550); await rbtn().click();
  check("Reboot Defiant with a wrong key shows the same 403 wording, and starts nothing", await until(async () => /Reboot Defiant failed: Starfleet key missing or wrong \(enter it on the Shuttle Bay page, Save key\)/i.test(await p.locator("#defmsg").innerText()))
        && await rbtn().isEnabled() && rebooted() === 0, await p.locator("#defmsg").innerText());
  await s.end("wrong key");

  s = await session(browser, KEY); p = s.page;
  hostWrite("job.conf", `RC=0\nDELAY=14\nPER=2\nREBOOT=1\nNOTE=${quote(HOSTILE)}\n`); fs.copyFileSync(SIMS.quiet, hostPath("apt-sim-after.txt"));
  hostWrite("reboot.conf", "DOWN=10\nNEVER=0\n");      /* the reboot that follows the install: Defiant is down for ten seconds, long enough for the page to see it */
  await p.goto(BASE + "/desktop#def"); await until(async () => await rows() === 20);
  await button("Install Updates").click();
  const ask = await block();
  check("the first click asks, with the count and the warnings, and says that Defiant reboots by itself when a reboot is needed", /Sure\? Install 20 updates/i.test(ask) && /Install 20 updates \(11 security\) on Defiant now\?/i.test(ask)
        && /Restarts services \(tailscale\)/i.test(ask) && /Needs a reboot afterwards \(libc6\)/i.test(ask) && !/never reboots/i.test(ask)
        && /If a reboot is needed afterwards, Defiant reboots by itself and the sites are unreachable for about a minute\./i.test(ask) && /Click again to start/i.test(ask) && started() === 0, ask);
  await s.shot("7-confirm");
  await sleep(550); await button("Install Updates").click();
  check("the second click starts the install: the progress panel shows the log", await until(async () => /Installing updates/i.test(await block()))
        && await until(async () => /Setting up libexample/i.test(await p.locator("#updbox .joblog").innerText())) && started() === 1, await block());
  check("text from the host is shown as text: a log line with markup does nothing", await p.evaluate(() => !window.__pwned && !document.querySelector("#evil, #updbox img"))
        && (await p.locator("#updbox .joblog").innerText()).includes(HOSTILE), await p.locator("#updbox .joblog").innerText());
  checks("while the install runs the Scan hint says that a scan follows it and the reboot after it, and Scan is off", {
    "the hint": /Scans again by itself when the install \(and a reboot after it\) is done\./i.test(await block()), "not the old one": !/when the install ends/i.test(await block()),
    "Scan is off": await button("Scan").isDisabled() }, await block());
  await s.shot("8-running");
  check("the page shows the result: installed 20 packages, reboot required afterwards", await until(async () => /Install finished/i.test(await block()), 60000)
        && /installed 20 packages/i.test(await block()) && /reboot required afterwards: yes/i.test(await block()), await block());
  await s.shot("9-done");
  /* the install needed a reboot, so Fleet reboots Defiant by itself: its lines show below the install panel and in the Server block */
  checks("Fleet reboots Defiant by itself: the line below the install panel and the Reboot button say so, Install and Scan are off", {
    "a line below the install panel": await until(has(/Rebooting\. /), 40000), "the line in the Server block": await until(rhas(/Rebooting\./)),
    "the button is off and says Rebooting...": await rbtn().isDisabled() && /Rebooting\.\.\./i.test(await rbtn().innerText()), "Install is off": await button("Install Updates").isDisabled(),
    "Scan is off and says why": await button("Scan").isDisabled() && /Scans again by itself when Defiant is back\./i.test(await block()), "one reboot asked for": rebooted() === 1,
    "no hint to reboot by hand meanwhile": !/A reboot is waiting/i.test(await block()), "the line is amber": !/\b(ok|bad)\b/.test(await below().first().getAttribute("class")) }, await block());
  const downSecs = async () => { const m = /Defiant has been down since \d\d:\d\d:\d\d \((\d+) s\)\./.exec(await below().first().innerText().catch(() => "")); return m ? +m[1] : null; };
  let n1 = null, n2 = null;
  await until(async () => (n1 = await downSecs()) !== null, 40000); await s.shot("9b-down");
  await until(async () => (n2 = await downSecs()) > n1, 12000);
  check("once Fleet saw Defiant go down the lines say since when, and the seconds count up", n1 !== null && n2 > n1 && /Defiant has been down since \d\d:\d\d:\d\d \(\d+ s\)\./.test(await rnote()), [n1, n2, await rnote()]);
  checks("when Defiant answers again both lines say how long it took, in the ok colour", {
    "the line below the install panel": await until(has(/Defiant rebooted and answered again after \d+ s\./), 60000), "in the ok colour": /\bok\b/.test(await below().first().getAttribute("class")),
    "the line in the Server block": await until(rhas(/Defiant rebooted and answered again after \d+ s\./)), "also ok": /\bok\b/.test(await rkind()) }, [await block(), await rbox()]);
  await s.shot("10-rebooted");
  checks("the rescan after the reboot finds nothing to install and no reboot pending: the job panel keeps what the install saw, no hint to reboot", {
    "0 to install": await until(has(/0\s*to install/i), 30000), "nothing to install": /Nothing to install/i.test(await block()), "no reboot pending": /Reboot\s*No\b/i.test(await block()),
    "the install result as it was": /reboot required afterwards: yes/i.test(await block()), "no hint to reboot by hand": !/A reboot is waiting/i.test(await block()),
    "the Reboot button is back": await until(async () => await rbtn().isEnabled()), "Scan is back": await button("Scan").isEnabled() }, await block());
  await s.shot("11-after");

  sim("many"); fs.rmSync(hostPath("apt-sim-after.txt")); hostWrite("job.conf", "RC=100\nDELAY=2\nPER=16\n"); await sleep(2200);      // the next install fails, soon
  await button("Scan").click(); await until(async () => await rows() === 20);
  await button("Install Updates").click(); await sleep(550); await button("Install Updates").click();
  check("a failed install shows its exit code and the log, and Install is not stuck on Starting", await until(has(/Install failed \(exit 100\)/i), 40000)
        && /FLEET-APT-EXIT 100/.test(await p.locator("#updbox .joblog").innerText()) && !/Starting/i.test(await block()), await block());
  await s.shot("12-failed");

  /* the guard: no scan showed the removal apt would need. The install stops before it changes anything, the log ends with what to
     do next (the page needs no change for that), and the next scan and confirm step name the package */
  const lastLine = async () => (await p.locator("#updbox .joblog").innerText({ timeout: 1000 }).catch(() => "")).trim().split("\n").pop();
  await rescanned(p);
  sim("removal"); hostWrite("job.conf", "RC=0\nDELAY=2\nPER=2\n"); await sleep(2200);
  await button("Install Updates").click(); await sleep(550); await button("Install Updates").click();
  check("an install that apt refuses (--no-remove) shows its exit code and ends with the hint, as the last line of the log", await until(async () => await lastLine() === HINT, 40000)
        && /Install failed \(exit 100\)/i.test(await block()) && /remove is disabled/.test(await p.locator("#updbox .joblog").innerText()), await block());
  await s.shot("13-guard");
  check("the scan that follows names the removal on the page", await until(has(/The install removes 1 package: libold-compat1/i), 40000) && /Remove\s*1/i.test(await block()), await block());
  const before = started();
  await button("Install Updates").click();
  check("and the next confirm step names it", /Removes 1 package: libold-compat1/i.test(await block()) && started() === before, await block());
  await s.shot("14-confirm-removal");
  await s.end("failed install");

  /* the Reboot Defiant button by itself, on a host that wants a reboot: a refusal, the guard, a reboot that does not happen, then one that does */
  s = await session(browser, KEY); p = s.page;
  const r0 = rebooted(), bad = E_SAME;
  const cls = async () => (await rbtn().getAttribute("class").catch(() => "")) || "";
  pendingReboot(true); hostWrite("reboot.conf", "DOWN=10\nNEVER=1\n"); sim("quiet"); await sleep(2200);
  await p.goto(BASE + "/desktop#def"); await until(rhas(/Reboot Defiant/i));
  await button("Scan").click();
  checks("a scan finds the pending reboot: the Reboot button turns amber (red is for failure) and the chip says Required", {
    amber: await until(async () => /\bwarn\b/.test(await cls())), "the chip": /Reboot\s*Required/i.test(await block()), enabled: await rbtn().isEnabled(),
    "the finished reboot of before is still shown": /Defiant rebooted and answered again after \d+ s\./.test(await rbox()) }, await rbox());
  await s.shot("15-reboot-amber");
  hostWrite("scan-delay", "4"); await sleep(2200);
  await button("Scan").click(); await until(has(/Scanning\.\.\./i), 8000);
  checks("while a scan runs the Reboot button is off and says why (Fleet would answer 409), and nothing is sent", {
    off: await until(async () => await rbtn().isDisabled(), 8000), reason: await until(rhas(/A scan is running, try again in a minute\./i)), "nothing sent": rebooted() === r0,
    "no failure line": !/Reboot Defiant failed/i.test(await p.locator("#defmsg").innerText()) }, await rbox());
  hostWrite("scan-delay", "0"); await until(async () => !/Scanning\.\.\./i.test(await block()), 15000);
  check("when the scan has ended the Reboot button is back, with its own hint", await until(async () => await rbtn().isEnabled(), 10000) && RB_HINT.test(await rbox()), await rbox());
  hostWrite("ssh-down-until", String(Math.floor(Date.now() / 1000) + 5));      /* Defiant does not answer at the moment the button is pressed */
  await rbtn().click(); await sleep(550); await rbtn().click();
  check("a reboot while Defiant does not answer shows the generic failure line with Fleet's fixed text, and nothing is scheduled", await until(async () => /Reboot Defiant failed: Defiant did not accept the reboot request\./i.test(await p.locator("#defmsg").innerText()), 10000)
        && await rbtn().isEnabled() && rebooted() === r0, await p.locator("#defmsg").innerText());
  await sleep(5500);                                                           /* the window ends */
  await rbtn().click();
  checks("the first click asks: Sure?, the whole server, what waits for the reboot, and sends nothing", {
    "the label": /^sure\? reboot defiant$/i.test(await rbtn().innerText()), armed: /\barmed\b/.test(await cls()),
    "the text": /This reboots the whole Defiant server\. The sites and all containers are unreachable for about a minute, and Fleet cannot reach Defiant until it is back\. A reboot is waiting for: linux-image-6\.8\.0-99-generic, libc6\./i.test(await rbox()),
    "nothing sent": rebooted() === r0 }, await rbox());
  await s.shot("16-reboot-confirm");
  await p.evaluate(() => { const b = document.querySelector("#rebbox button.bigbtn"); b.click(); b.click(); });      /* a double click: the second is inside the 450 ms guard */
  check("a second click within 450 ms is no confirmation: still asking, nothing sent", /^sure\? reboot defiant$/i.test(await rbtn().innerText()) && rebooted() === r0, await rbox());
  await sleep(550); await rbtn().click();
  checks("the second click sends it: the button says Rebooting..., the line says Defiant is going down, Install and Scan are off", {
    "the line": await until(rhas(/Rebooting\. Waiting for Defiant to go down\./i)), off: await rbtn().isDisabled(), "the label": /Rebooting\.\.\./i.test(await rbtn().innerText()),
    "amber": !/\b(ok|bad)\b/.test(await rkind()), "Install is off": await button("Install Updates").isDisabled(), "Scan is off": await button("Scan").isDisabled(),
    "one reboot asked for": rebooted() === r0 + 1, "no line below the install panel": await below().count() === 0 }, await rbox());
  await s.shot("17-reboot-running");
  checks("a reboot that does not happen ends failed: the error from Fleet in a red line that stays, the button is back", {
    "the error": await until(rhas(/Defiant is still on the same boot/i), 40000) && (await rnote()).includes(bad), red: /\bbad\b/.test(await rkind()), enabled: await rbtn().isEnabled(),
    "the label": /^reboot defiant$/i.test(await rbtn().innerText()), "still amber, the reboot is still wanted": /\bwarn\b/.test(await cls()) }, await rbox());
  await s.shot("18-reboot-failed");
  await sleep(4000);
  check("... and it is still there a few polls later", (await rnote()).includes(bad) && await rbtn().isEnabled(), await rbox());

  hostWrite("reboot.conf", "DOWN=10\nNEVER=0\n");
  await rbtn().click(); await sleep(550); await rbtn().click();
  checks("the next reboot replaces the failure with its own lines: down since, the seconds count up", {
    "down since": await until(rhas(/Defiant has been down since \d\d:\d\d:\d\d \(\d+ s\)\./), 40000), "the failure is gone": !/still on the same boot/i.test(await rbox()),
    off: await rbtn().isDisabled(), "two reboots asked for": rebooted() === r0 + 2 }, await rbox());
  const mySecs = async () => { const m = /\((\d+) s\)\./.exec(await rnote()); return m ? +m[1] : null; };
  n1 = await mySecs(); await until(async () => (n2 = await mySecs()) > n1, 12000);
  check("the elapsed seconds on the Server block's line count up too", n1 !== null && n2 > n1, [n1, n2]);
  checks("when Defiant answers again the line says how long it took, in the ok colour, and the plain button is back, no longer amber", {
    "the line": await until(rhas(/Defiant rebooted and answered again after \d+ s\./), 60000), "ok colour": /\bok\b/.test(await rkind()),
    "plain button": await until(async () => await rbtn().isEnabled() && !/\bwarn\b/.test(await cls())), "no line below the install panel": await below().count() === 0 }, await rbox());
  check("the rescan clears the reboot flag on the page", await until(async () => !/Reboot\s*Required/i.test(await block()), 30000), await block());
  await s.shot("19-rebooted");

  /* an install whose own reboot cannot start: the nightly run takes the apt lock the moment the install ends. A red line shows below the install
     result and in the Server block, the install stays finished, and once apt is idle a scan finds the reboot still wanted: the hint to use the button */
  sim("many"); fs.rmSync(hostPath("apt-lock"), { force: true }); await sleep(2200);
  await button("Scan").click(); await until(async () => await rows() === 20);
  hostWrite("job.conf", "RC=0\nDELAY=3\nPER=2\nREBOOT=1\nLOCK=1\n");
  await button("Install Updates").click(); await sleep(550); await button("Install Updates").click();
  checks("an install whose own reboot cannot start: finished, with a red line below it and in the Server block that gives Fleet's reason", {
    "Install finished": await until(has(/Install finished/i), 40000), "the line below the install result": await until(async () => (await below().first().innerText().catch(() => "")).includes(E_AUTO), 20000),
    red: /\bbad\b/.test(await below().first().getAttribute("class")), "the line in the Server block": (await rnote()).includes(E_AUTO) && /\bbad\b/.test(await rkind()),
    "the button is back": await rbtn().isEnabled(), "nothing was scheduled for it": rebooted() === r0 + 2 }, [await block(), await rbox()]);
  await s.shot("20-auto-failed");
  fs.rmSync(hostPath("apt-lock")); sim("quiet"); await sleep(2200);
  await button("Scan").click();
  checks("once apt is idle a scan finds the reboot still wanted: the hint to use Reboot Defiant shows in the install result, the button is amber, the red line stays", {
    hint: await until(has(/A reboot is waiting\. Use Reboot Defiant when it suits you\./i), 20000), amber: await until(async () => /\bwarn\b/.test(await cls())),
    "the red line stays": (await below().first().innerText()).includes(E_AUTO) }, await block());
  await s.shot("21-auto-hint");

  /* the install confirm while a reboot is already waiting (the install above left one, and the scan has found it): the sentence is definite, not conditional */
  sim("many"); await sleep(2200);
  await button("Scan").click(); await until(async () => await rows() === 20);
  const n0 = started();
  await button("Install Updates").click();
  const ask2 = await block();
  checks("with a reboot already waiting the first click says so: Defiant reboots by itself right after this install (no longer 'if one is needed'), and nothing is sent", {
    "asking": /Sure\? Install 20 updates/i.test(ask2) && /Click again to start/i.test(ask2), "the definite sentence": WAITING.test(ask2),
    "not the conditional one": !/If a reboot is needed afterwards, Defiant reboots by itself/i.test(ask2), "the scan wants a reboot": /Reboot\s*Required/i.test(ask2), "nothing started": started() === n0 }, ask2);
  await s.shot("22-confirm-reboot-waiting");
  await s.end("reboot button");
}

/* ------------------------------------------------------------------ the phone page */
async function phone(browser) {
  hostWrite("apt-lock", ""); sim("many");
  let s = await session(browser, KEY), p = s.page;
  const view = () => p.locator("#p-view").textContent().catch(() => ""), has = re => async () => re.test(await view());
  const tap = async sel => { await p.locator(sel).first().tap(); await sleep(380); };
  const SCAN = '[data-act="sys.scan"]', INSTALL = '[data-act="sys.install"]', YES = '[data-k="yes"]';
  const pkgRows = () => p.evaluate(() => [...document.querySelectorAll("#p-view .p-sy-flat")].filter(r => r.querySelector(".p-sy-path")).length);
  const toUpd = () => p.evaluate(() => { const h = [...document.querySelectorAll("#p-view .p-sec h2")].find(x => x.textContent === "Updates");
    if (h) scrollTo(0, h.getBoundingClientRect().top + scrollY - document.getElementById("p-hd").offsetHeight - 6); });
  const sheet = () => p.locator(".p-sheet").textContent().catch(() => "");
  const disabled = sel => p.locator(sel).first().isDisabled();
  const REBOOT = '[data-act="sys.reboot"]', NO = '[data-k="no"]', rlabel = () => p.locator(REBOOT).first().textContent();
  const toasts = () => p.evaluate(() => [...document.querySelectorAll("#p-toast .p-toast")].map(x => ({ text: x.textContent, err: x.classList.contains("err") }))).catch(() => []);
  const toSrv = () => p.evaluate(() => { const h = [...document.querySelectorAll("#p-view .p-sec h2")].find(x => x.textContent === "Server");
    if (h) scrollTo(0, h.getBoundingClientRect().top + scrollY - document.getElementById("p-hd").offsetHeight - 6); });
  /* the reboot line in the Server panel, and the one right under the "Install finished" card: {text, kind} with kind "" (amber), "ok" (blue) or "err" (red), null when there is none */
  const rline = () => p.evaluate(() => { const m = document.querySelector('[data-act="sys.reboot"]').closest(".p-card").querySelector(".p-msg");
    return m ? { text: m.textContent, kind: m.classList.contains("err") ? "err" : m.classList.contains("p-sy-ok") ? "ok" : "" } : null; }).catch(() => null);
  const aline = () => p.evaluate(() => { const c = [...document.querySelectorAll("#p-view .p-sy-up .p-card")].find(x => x.querySelector("h3").textContent === "Install finished"), m = c && c.nextElementSibling;
    return m && m.classList.contains("p-msg") ? { text: m.textContent, kind: m.classList.contains("err") ? "err" : m.classList.contains("p-sy-ok") ? "ok" : "" } : null; }).catch(() => null);
  const RB_HINT = "Reboots the whole server. All sites are unreachable for about a minute. Needs the Starfleet key.";
  await p.goto(BASE + "/padd#/systems/defiant");
  await until(async () => await p.locator(SCAN).count()); await toUpd();
  check("before the first scan: No scan yet, Install is off and says why, Scan now is on", await until(has(/No scan yet/)) && await disabled(INSTALL)
        && /No scan yet: scan first/.test(await view()) && !await disabled(SCAN), await view());
  await s.shot("1-no-scan");
  await tap(SCAN);
  check("a failed first scan shows its error and no package list", await until(has(/Scan failed .*Could not get lock/)) && await pkgRows() === 0 && !await disabled(SCAN), await view());
  await toUpd(); await s.shot("2-first-scan-failed");
  fs.rmSync(hostPath("apt-lock")); hostWrite("scan-delay", "5"); await sleep(2500);
  await tap(SCAN);
  check("while the first scan runs the page says Scanning... and shows no package list", await until(has(/Scanning\.\.\./)) && await pkgRows() === 0 && await disabled(INSTALL), await view());
  await toUpd(); await s.shot("3-scanning");
  hostWrite("scan-delay", "0");
  check("the first scan appears by itself: 20 packages, 11 security, 21 held back", await until(async () => await pkgRows() === 12)
        && /20\s*to install/.test(await view()) && /11\s*Security/.test(await view()) && /21\s*Held back/.test(await view())
        && /21 held back by Ubuntu's staged rollout/.test(await view()), await view());
  check("the list shows the first 12, security first, with the Security tag, then +8 more", await p.evaluate(() => {
    const r = [...document.querySelectorAll("#p-view .p-sy-flat")].filter(x => x.querySelector(".p-sy-path")), more = [...document.querySelectorAll("#p-view .p-sy-flat .p-t")].find(x => /^\+\d+ more$/.test(x.textContent));
    return r.map(x => x.querySelector(".p-tag.warn") ? "S" : "-").join("") === "SSSSSSSSSSS-" && more && more.textContent === "+8 more" && r[0].textContent.startsWith("apache2-bin"); }), await view());
  check("the scan times are shown and Install is on", /Last scan .* · next /.test(await view()) && !await disabled(INSTALL), await view());
  checks("the note under the buttons says that Fleet reboots Defiant when a reboot is needed afterwards, no longer that it never does", {
    "the new note": /Install needs the Starfleet key\. If a reboot is needed afterwards, Fleet reboots Defiant\./.test(await view()), "no 'never reboots'": !/never reboots/i.test(await view()) }, await view());
  await toUpd(); await s.shot("4-data");
  await toSrv();
  checks("the Server panel has the Reboot Defiant button next to the Reboot figure: enabled, plain, with its hint", {
    label: await rlabel() === "Reboot Defiant", enabled: !await disabled(REBOOT), plain: !/\bamber\b/.test(await p.locator(REBOOT).first().getAttribute("class")),
    hint: (await p.locator(REBOOT).first().evaluate(b => b.closest(".p-card").textContent)).includes(RB_HINT),
    "in the Server panel": await p.locator(REBOOT).first().evaluate(b => !!b.closest(".p-card").querySelector(".p-kv") && !b.closest(".p-sy-up")), "no reboot line": await rline() === null }, await view());
  await s.shot("4b-reboot-idle"); await toUpd();
  await p.evaluate(() => { location.hash = "#/systems"; }); await p.reload();
  check("the Systems row says how many updates are pending and how many are security", await until(async () => /20 updates pending · 11 security/.test(
        await p.locator('#p-view [data-act="go"][data-arg="#/systems/defiant"]').first().textContent())), await view());
  await s.shot("5-systems-row");
  await p.evaluate(() => { location.hash = "#/bridge"; }); await p.reload();
  check("the Bridge lists one amber item: Defiant: 11 security updates pending", await until(async () => await p.evaluate(() => {
    const r = [...document.querySelectorAll('#p-view [data-act="go"][data-arg="#/systems/defiant"]')].filter(x => /security update/.test(x.textContent));
    return r.length === 1 && /Defiant: 11 security updates pending/.test(r[0].textContent) && /var\(--amber\)/.test(r[0].getAttribute("style")); })), await view());
  await s.shot("6-bridge");
  await p.evaluate(() => { location.hash = "#/systems/defiant"; }); await p.reload(); await until(async () => await pkgRows() === 12); await toUpd();

  sim("three"); await sleep(2200);
  await tap(SCAN);
  check("Scan now shows what the new scan found", await until(async () => await pkgRows() === 5) && /3\s*Security/.test(await view()) && /1\s*Held back/.test(await view()), await view());
  await toUpd(); await s.shot("7-scan-now");
  sim("many"); await sleep(2200);
  await tap(SCAN);
  await until(async () => await pkgRows() === 12);
  await s.end("phone page");

  s = await session(browser, "wrong-key"); p = s.page;
  await p.goto(BASE + "/padd#/systems/defiant"); await until(async () => await pkgRows() === 12); await toUpd();
  await tap(INSTALL); await sleep(550); await tap(YES);
  check("Install with a wrong key asks for the key again, as Back up now does, and starts nothing", await until(async () => /Fleet did not accept the stored key/.test(await sheet())) && started() === 0, await sheet());
  await s.shot("8-wrong-key");
  await p.evaluate(() => PADD.closeSheets()); await sleep(400); await toSrv();
  await tap(REBOOT); await sleep(550); await tap(YES);
  check("Reboot with a wrong key asks for the key again, as Back up now does, and starts nothing", await until(async () => /Fleet did not accept the stored key/.test(await sheet())) && rebooted() === 0, await sheet());
  await s.end("wrong key");

  s = await session(browser, KEY); p = s.page;
  hostWrite("job.conf", `RC=0\nDELAY=14\nPER=2\nREBOOT=1\nNOTE=${quote(HOSTILE)}\n`); fs.copyFileSync(SIMS.quiet, hostPath("apt-sim-after.txt"));
  await p.goto(BASE + "/padd#/systems/defiant"); await until(async () => await pkgRows() === 12); await toUpd();
  await tap(INSTALL);
  const ask = await sheet();
  checks("the confirm sheet names the count and the warnings, and says that Defiant reboots by itself when a reboot is needed", {
    title: /Install updates\?/.test(ask), count: /20 packages will be upgraded, 11 of them security updates\./.test(ask),
    services: /It restarts services \(tailscale\): the sites can be unreachable for a few seconds\./.test(ask), "the reboot it needs": /It needs a reboot afterwards \(libc6\)\./.test(ask),
    "the sentence at the end": (await p.locator(".p-sheet .p-text").textContent()).endsWith(" If a reboot is needed afterwards, Defiant reboots by itself and the sites are unreachable for about a minute."),
    "no 'never reboots'": !/never reboots/i.test(ask),
    "nothing started": started() === 0 }, ask);
  await s.shot("9-confirm");
  await sleep(550); await tap(YES);
  check("the progress card shows the log, Install and Scan now are off", await until(has(/Installing updates/)) && await until(async () => /Setting up libexample/.test(await p.locator("#p-view .p-sy-log").textContent()))
        && await disabled(INSTALL) && await disabled(SCAN) && started() === 1, await view());
  check("text from the host is shown as text: a log line with markup does nothing", await p.evaluate(() => !window.__pwned && !document.querySelector("#evil, #p-view img"))
        && (await p.locator("#p-view .p-sy-log").textContent()).includes(HOSTILE), await p.locator("#p-view .p-sy-log").textContent());
  await toUpd(); await s.shot("10-running");
  check("the page shows the result: 20 installed, reboot required afterwards", await until(has(/Install finished/), 60000) && /20\s*installed/.test(await view()) && /reboot required afterwards: yes/.test(await view()), await view());
  await s.shot("11-done");
  /* the install needed a reboot, so Fleet reboots Defiant by itself: its line shows right under the install result and in the Server panel */
  checks("Fleet reboots Defiant by itself: the line under the install result and the Reboot button say so, Install and Scan are off", {
    "a line under the install result": await until(async () => /^Rebooting\. /.test(((await aline()) || {}).text || ""), 40000), "the line in the Server panel": /^Rebooting\. /.test(((await rline()) || {}).text || ""),
    "the button is off and says Rebooting": await disabled(REBOOT) && await rlabel() === "Rebooting", "Install is off": await disabled(INSTALL), "Scan is off": await disabled(SCAN),
    "the note says why": /A reboot is running, progress above\./.test(await p.locator("#p-view .p-sy-up .p-note").textContent()), "one reboot asked for": rebooted() === 1,
    "no hint to reboot by hand meanwhile": !/A reboot is waiting\./.test(await view()), "the line is amber": ((await aline()) || {}).kind === "" }, await view());
  const downSecs = async () => { const m = /Defiant has been down since \d\d:\d\d:\d\d \((\d+) s\)\./.exec(((await aline()) || {}).text || ""); return m ? +m[1] : null; };
  let n1 = null, n2 = null;
  await until(async () => (n1 = await downSecs()) !== null, 40000); await toUpd(); await s.shot("11b-down");
  await until(async () => (n2 = await downSecs()) > n1, 12000);
  check("once Fleet saw Defiant go down the lines say since when, and the seconds count up", n1 !== null && n2 > n1 && /Defiant has been down since \d\d:\d\d:\d\d \(\d+ s\)\./.test(((await rline()) || {}).text || ""), [n1, n2, await rline()]);
  checks("when Defiant answers again both lines say how long it took, in the ok colour", {
    "the line under the install result": await until(async () => /^Defiant rebooted and answered again after \d+ s\.$/.test(((await aline()) || {}).text || ""), 60000), "ok colour": ((await aline()) || {}).kind === "ok",
    "the line in the Server panel": /^Defiant rebooted and answered again after \d+ s\.$/.test(((await rline()) || {}).text || ""), "also ok": ((await rline()) || {}).kind === "ok" }, [await aline(), await rline()]);
  await toUpd(); await s.shot("12-rebooted");
  checks("the rescan after the reboot finds nothing to install and no reboot pending: the result keeps what the install saw, no hint to reboot", {
    "0 to install": await until(has(/0\s*to install/), 30000), "nothing to install": /Nothing to install\./.test(await view()),
    "no reboot pending in the scan: no amber button, no packages named": !/\bamber\b/.test(await p.locator(REBOOT).first().getAttribute("class")) && !/Reboot needed for/.test(await view()),
    "the install result as it was": /reboot required afterwards: yes/.test(await view()), "no hint to reboot by hand": !/A reboot is waiting\./.test(await view()),
    "the Reboot button is back": await until(async () => !await disabled(REBOOT)) && await rlabel() === "Reboot Defiant", "Scan is back": !await disabled(SCAN) }, await view());
  await toUpd(); await s.shot("13-after");

  sim("many"); fs.rmSync(hostPath("apt-sim-after.txt")); hostWrite("job.conf", "RC=100\nDELAY=2\nPER=16\n"); await sleep(2200);      // the next install fails, soon
  await toUpd(); await tap(SCAN); await until(async () => await pkgRows() === 12);
  await tap(INSTALL); await sleep(550); await tap(YES);
  check("a failed install shows its exit code and the log in red, and Install is not stuck on Starting", await until(has(/Install failed/), 40000)
        && /exit 100/.test(await view()) && /FLEET-APT-EXIT 100/.test(await p.locator("#p-view .p-sy-log.err").textContent())
        && await p.locator(INSTALL).first().textContent() === "Install updates" && !/Starting the install/.test(await view()), await view());
  await toUpd(); await s.shot("14-failed");

  /* the guard, as on the desktop page: no page change is needed for the hint */
  const lastLine = async () => (await p.locator("#p-view .p-sy-log.err").textContent({ timeout: 1000 }).catch(() => "")).trim().split("\n").pop();
  await rescanned(p);
  sim("removal"); hostWrite("job.conf", "RC=0\nDELAY=2\nPER=2\n"); await sleep(2200);
  await toUpd(); await tap(INSTALL); await sleep(550); await tap(YES);
  check("an install that apt refuses (--no-remove) shows its exit code and ends with the hint, as the last line of the log", await until(async () => await lastLine() === HINT, 40000)
        && /exit 100/.test(await view()) && /remove is disabled/.test(await p.locator("#p-view .p-sy-log.err").textContent()), await view());
  await toUpd(); await s.shot("15-guard");
  check("the scan that follows names the removal on the page", await until(has(/The install removes 1 package: libold-compat1/), 40000) && /1\s*Remove/.test(await view()), await view());
  const before = started();
  await toUpd(); await tap(INSTALL);
  check("and the confirm sheet names it", /It removes 1 package: libold-compat1\./.test(await sheet()) && started() === before, await sheet());
  await s.shot("16-confirm-removal");
  await tap('[data-k="no"]');
  await s.end("failed install");

  /* the Reboot Defiant button by itself, on a host that wants a reboot: a refusal, the guard, a reboot that does not happen, then one that does */
  s = await session(browser, KEY); p = s.page;
  const r0 = rebooted(), bad = E_SAME;
  const cls = () => p.locator(REBOOT).first().getAttribute("class");
  pendingReboot(true); hostWrite("reboot.conf", "DOWN=10\nNEVER=1\n"); sim("quiet"); await sleep(2200);
  await p.goto(BASE + "/padd#/systems/defiant"); await until(async () => await p.locator(REBOOT).count()); await toUpd();
  await tap(SCAN);
  checks("a scan finds the pending reboot: the Reboot button turns amber (red is for failure) and the figure says Required", {
    amber: await until(async () => /\bamber\b/.test(await cls())), "the figure": await until(has(/Required\s*Reboot/)), enabled: !await disabled(REBOOT),
    "the finished reboot of before is still shown": /^Defiant rebooted and answered again after \d+ s\.$/.test(((await rline()) || {}).text || "") }, await view());
  await toSrv(); await s.shot("17-reboot-amber");
  hostWrite("scan-delay", "4"); await sleep(2200);
  await toUpd(); await tap(SCAN); await until(async () => await disabled(SCAN), 8000);
  await toSrv();
  const rcard = () => p.evaluate(() => document.querySelector('[data-act="sys.reboot"]').closest(".p-card").textContent);
  checks("while a scan runs the Reboot button is off and says why (Fleet would answer 409): no sheet opens and nothing is sent", {
    off: await until(async () => await disabled(REBOOT), 8000), reason: /A scan is running, try again in a minute\./.test(await rcard()), "no sheet": await p.locator(".p-sheet.on").count() === 0,
    "nothing sent": rebooted() === r0 }, await rcard());
  hostWrite("scan-delay", "0"); await toUpd(); await until(async () => !await disabled(SCAN), 15000); await toSrv();
  check("when the scan has ended the Reboot button is back, with its own hint", await until(async () => !await disabled(REBOOT), 10000) && (await rcard()).includes(RB_HINT), await rcard());
  hostWrite("ssh-down-until", String(Math.floor(Date.now() / 1000) + 5));      /* Defiant does not answer at the moment the button is pressed */
  await tap(REBOOT); await sleep(550); await tap(YES);
  checks("a reboot while Defiant does not answer shows Fleet's fixed text in a toast, and nothing is scheduled", {
    toast: await until(async () => (await toasts()).some(t => t.text === "Reboot failed: Defiant did not accept the reboot request." && t.err), 10000), "nothing sent": rebooted() === r0,
    "the button is enabled": !await disabled(REBOOT) }, JSON.stringify(await toasts()));
  await sleep(5500);                                                           /* the window ends */
  await p.locator(REBOOT).first().tap();
  const ask2 = await sheet();
  checks("the confirm sheet asks: the whole server, what waits for the reboot, and sends nothing", {
    title: /Reboot Defiant\?/.test(ask2), text: /This reboots the whole Defiant server\. The sites and all containers are unreachable for about a minute, and Fleet cannot reach Defiant until it is back\. A reboot is waiting for: linux-image-6\.8\.0-99-generic, libc6\./.test(ask2),
    "nothing sent": rebooted() === r0 }, ask2);
  await s.shot("18-reboot-confirm");
  await p.evaluate(() => document.querySelector('[data-k="yes"]').click());      /* straight away: inside the 450 ms guard */
  await sleep(300);
  check("a tap on Reboot within 450 ms is no confirmation: the sheet stays, nothing is sent", /Reboot Defiant\?/.test(await sheet()) && rebooted() === r0, await sheet());
  await sleep(300); await tap(YES);
  checks("the confirmation sends it: the button says Rebooting, the line says Defiant is going down, Install and Scan are off", {
    "the line": await until(async () => /^Rebooting\. Waiting for Defiant to go down\.$/.test(((await rline()) || {}).text || "")), off: await disabled(REBOOT), label: await rlabel() === "Rebooting",
    amber: ((await rline()) || {}).kind === "", "Install is off": await disabled(INSTALL), "Scan is off": await disabled(SCAN), "one reboot asked for": rebooted() === r0 + 1,
    "no line under an install result": await aline() === null, "the toast": (await toasts()).some(t => t.text === "Reboot started." && !t.err) }, await view());
  await s.shot("19-reboot-running");
  checks("a reboot that does not happen ends failed: the error from Fleet in a red line that stays, the button is back", {
    "the error": await until(async () => ((await rline()) || {}).text === bad, 40000), red: ((await rline()) || {}).kind === "err", enabled: !await disabled(REBOOT),
    label: await rlabel() === "Reboot Defiant", "still amber, the reboot is still wanted": /\bamber\b/.test(await cls()) }, await rline());
  await s.shot("20-reboot-failed");
  await sleep(4000);
  check("... and it is still there a few polls later", ((await rline()) || {}).text === bad && !await disabled(REBOOT), await rline());

  hostWrite("reboot.conf", "DOWN=10\nNEVER=0\n");
  await tap(REBOOT); await sleep(550); await tap(YES);
  checks("the next reboot replaces the failure with its own line: down since, the seconds count up", {
    "down since": await until(async () => /^Rebooting\. Defiant has been down since \d\d:\d\d:\d\d \(\d+ s\)\.$/.test(((await rline()) || {}).text || ""), 40000),
    "the failure is gone": ((await rline()) || {}).kind === "", off: await disabled(REBOOT), "two reboots asked for": rebooted() === r0 + 2 }, await rline());
  const mySecs = async () => { const m = /\((\d+) s\)\./.exec(((await rline()) || {}).text || ""); return m ? +m[1] : null; };
  n1 = await mySecs(); await until(async () => (n2 = await mySecs()) > n1, 12000);
  check("the elapsed seconds count up", n1 !== null && n2 > n1, [n1, n2]);
  checks("when Defiant answers again the line says how long it took, in the ok colour, and the plain button is back, no longer amber", {
    "the line": await until(async () => /^Defiant rebooted and answered again after \d+ s\.$/.test(((await rline()) || {}).text || ""), 60000), "ok colour": ((await rline()) || {}).kind === "ok",
    "plain button": await until(async () => !await disabled(REBOOT) && !/\bamber\b/.test(await cls())), "no line under an install result": await aline() === null }, await rline());
  check("the rescan clears the reboot flag on the page", await until(async () => !/Required\s*Reboot/.test(await view()), 30000), await view());
  await s.shot("21-rebooted");

  /* an install whose own reboot cannot start: the nightly run takes the apt lock the moment the install ends. A red line shows right under the install
     result and in the Server panel, the install stays finished, and once apt is idle a scan finds the reboot still wanted: the hint to use the button */
  sim("many"); fs.rmSync(hostPath("apt-lock"), { force: true }); await sleep(2200);
  await toUpd(); await tap(SCAN); await until(async () => await pkgRows() === 12);
  hostWrite("job.conf", "RC=0\nDELAY=3\nPER=2\nREBOOT=1\nLOCK=1\n");
  await tap(INSTALL); await sleep(550); await tap(YES);
  checks("an install whose own reboot cannot start: finished, with a red line right under it and in the Server panel that gives Fleet's reason", {
    "Install finished": await until(has(/Install finished/), 40000), "the line under the install result": await until(async () => ((await aline()) || {}).text === E_AUTO, 20000),
    red: ((await aline()) || {}).kind === "err", "the line in the Server panel": ((await rline()) || {}).text === E_AUTO && ((await rline()) || {}).kind === "err",
    "the button is back": !await disabled(REBOOT), "nothing was scheduled for it": rebooted() === r0 + 2 }, [await aline(), await rline()]);
  await toUpd(); await s.shot("22-auto-failed");
  fs.rmSync(hostPath("apt-lock")); sim("quiet"); await sleep(2200);
  await tap(SCAN);
  checks("once apt is idle a scan finds the reboot still wanted: the hint to use Reboot Defiant shows in the install result, the button is amber, the red line stays", {
    hint: await until(has(/A reboot is waiting\. Use Reboot Defiant when it suits you\./), 20000), amber: await until(async () => /\bamber\b/.test(await cls())),
    "the red line stays": ((await aline()) || {}).text === E_AUTO }, await view());
  await toUpd(); await s.shot("23-auto-hint");

  /* the install confirm while a reboot is already waiting (the install above left one, and the scan has found it): the sentence is definite, not conditional */
  sim("many"); await sleep(2200);
  await toUpd(); await tap(SCAN); await until(async () => await pkgRows() === 12);
  const n0 = started();
  await tap(INSTALL);
  const ask3 = await sheet();
  checks("with a reboot already waiting the sheet says so: Defiant reboots by itself right after this install (no longer 'if one is needed'), and nothing is sent", {
    title: /Install updates\?/.test(ask3) && /20 packages will be upgraded/.test(ask3), "the definite sentence at the end": WAITING.test(await p.locator(".p-sheet .p-text").textContent())
      && (await p.locator(".p-sheet .p-text").textContent()).endsWith(" A reboot is already waiting, so Defiant reboots by itself right after this install, and the sites are unreachable for about a minute."),
    "not the conditional one": !/If a reboot is needed afterwards, Defiant reboots by itself/.test(ask3), "nothing started": started() === n0 }, ask3);
  await s.shot("24-confirm-reboot-waiting");
  await tap(NO);
  await s.end("reboot button");
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  /* WebKit opens a connection for a <link rel=preconnect> of the page before a route can abort anything, and both pages have such links
     (Google Fonts): so the browser's own proxy is a dead loopback port, and the page's origin is on its bypass list. Nothing can leave the
     machine, not even a TCP handshake or a DNS question. FONTS=1 lifts this. */
  const browser = await webkit.launch(process.env.FONTS ? {} : { proxy: { server: "http://127.0.0.1:1", bypass: "127.0.0.1,localhost" } });
  try { await (PHONE ? phone : desktop)(browser); }
  catch (e) { check("the page script ran to the end", false, e.stack || e); }
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
