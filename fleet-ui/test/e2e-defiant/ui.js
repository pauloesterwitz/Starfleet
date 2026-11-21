#!/usr/bin/env node
/* The real pages against the real Fleet of the end to end test, in WebKit: the desktop page (/desktop#def, the Mac
   app window) or the phone page (/padd#/systems/defiant, the iPhone app). e2e.py starts Fleet and the made-up Defiant
   and runs this; the steps change what the made-up Defiant says by writing the files in E2E_HOST (apt-sim.txt,
   job.conf, apt-lock, scan-delay). The key is the made-up string fixture-key, stored where the pages store theirs.
   Steps: no scan yet, a failed first scan, a slow first scan (Scanning...), the data, Scan now, a wrong key, the
   confirm step, the progress, the result and the rescan, a failed install, and an install that apt refuses because it
   would have to remove a package no scan showed (the hint, then the removal named in the next scan and confirm step). Pictures go to OUT, one line per check to stdout.

   Usage: PW=<playwright package> E2E_BASE=http://127.0.0.1:PORT E2E_HOST=<host folder> E2E_SIMS=<json: name -> file>
          OUT=<folder> node ui.js desktop|phone           (FONTS=1 lets the pages load Google Fonts for prettier pictures) */
"use strict";
const fs = require("fs"), path = require("path");
const { webkit, devices } = require(process.env.PW || "playwright");
const WHICH = process.argv[2], BASE = process.env.E2E_BASE, HOST = process.env.E2E_HOST, SIMS = JSON.parse(process.env.E2E_SIMS || "{}");
const OUT = process.env.OUT || path.join(__dirname, "out"), KEY = "fixture-key", PHONE = WHICH === "phone";
const sleep = ms => new Promise(r => setTimeout(r, ms));
const hostPath = f => path.join(HOST, f), hostWrite = (f, text) => fs.writeFileSync(hostPath(f), text);
const sim = name => fs.copyFileSync(SIMS[name], hostPath("apt-sim.txt"));
const HOSTILE = '<img src=x onerror="window.__pwned=1"><b id="evil">x</b>', quote = t => "'" + t.replace(/'/g, "'\\''") + "'";     // what a host must not be able to inject
const started = () => (fs.existsSync(hostPath("calls.log")) ? fs.readFileSync(hostPath("calls.log"), "utf8").split("\n") : []).filter(l => l.split(" ")[1] === "systemd-run").length;
const HINT = "Fleet: apt would remove packages, nothing was changed. Rescanning now: check the list, then install again.";
let failed = 0;

function check(name, ok, detail) {
  if (!ok) failed++;
  console.log((ok ? "ok   " : "FAIL ") + WHICH + ": " + name + (ok ? "" : "  [" + String(detail).replace(/\s+/g, " ").slice(0, 500) + "]"));
  return !!ok;
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
  await s.end("wrong key");

  s = await session(browser, KEY); p = s.page;
  hostWrite("job.conf", `RC=0\nDELAY=14\nPER=2\nREBOOT=1\nNOTE=${quote(HOSTILE)}\n`); fs.copyFileSync(SIMS.quiet, hostPath("apt-sim-after.txt"));
  await p.goto(BASE + "/desktop#def"); await until(async () => await rows() === 20);
  await button("Install Updates").click();
  const ask = await block();
  check("the first click asks, with the count and the warnings", /Sure\? Install 20 updates/i.test(ask) && /Install 20 updates \(11 security\) on Defiant now\?/i.test(ask)
        && /Restarts services \(tailscale\)/i.test(ask) && /Needs a reboot afterwards \(libc6\)/i.test(ask) && /Click again to start/i.test(ask) && started() === 0, ask);
  await s.shot("7-confirm");
  await sleep(550); await button("Install Updates").click();
  check("the second click starts the install: the progress panel shows the log", await until(async () => /Installing updates/i.test(await block()))
        && await until(async () => /Setting up libexample/i.test(await p.locator("#updbox .joblog").innerText())) && started() === 1, await block());
  check("text from the host is shown as text: a log line with markup does nothing", await p.evaluate(() => !window.__pwned && !document.querySelector("#evil, #updbox img"))
        && (await p.locator("#updbox .joblog").innerText()).includes(HOSTILE), await p.locator("#updbox .joblog").innerText());
  await s.shot("8-running");
  check("the page shows the result: installed 20 packages, reboot required", await until(async () => /Install finished/i.test(await block()), 60000)
        && /installed 20 packages/i.test(await block()) && /reboot required afterwards: yes/i.test(await block()), await block());
  await s.shot("9-done");
  check("the block refreshes by itself after the rescan: nothing to install, reboot required", await until(has(/0\s*to install/i), 30000)
        && /Nothing to install/i.test(await block()) && /Reboot\s*Required/i.test(await block()), await block());
  await s.shot("10-after");

  sim("many"); fs.rmSync(hostPath("apt-sim-after.txt")); hostWrite("job.conf", "RC=100\nDELAY=2\nPER=16\n"); await sleep(2200);      // the next install fails, soon
  await button("Scan").click(); await until(async () => await rows() === 20);
  await button("Install Updates").click(); await sleep(550); await button("Install Updates").click();
  check("a failed install shows its exit code and the log, and Install is not stuck on Starting", await until(has(/Install failed \(exit 100\)/i), 40000)
        && /FLEET-APT-EXIT 100/.test(await p.locator("#updbox .joblog").innerText()) && !/Starting/i.test(await block()), await block());
  await s.shot("11-failed");

  /* the guard: no scan showed the removal apt would need. The install stops before it changes anything, the log ends with what to
     do next (the page needs no change for that), and the next scan and confirm step name the package */
  const lastLine = async () => (await p.locator("#updbox .joblog").innerText({ timeout: 1000 }).catch(() => "")).trim().split("\n").pop();
  await rescanned(p);
  sim("removal"); hostWrite("job.conf", "RC=0\nDELAY=2\nPER=2\n"); await sleep(2200);
  await button("Install Updates").click(); await sleep(550); await button("Install Updates").click();
  check("an install that apt refuses (--no-remove) shows its exit code and ends with the hint, as the last line of the log", await until(async () => await lastLine() === HINT, 40000)
        && /Install failed \(exit 100\)/i.test(await block()) && /remove is disabled/.test(await p.locator("#updbox .joblog").innerText()), await block());
  await s.shot("12-guard");
  check("the scan that follows names the removal on the page", await until(has(/The install removes 1 package: libold-compat1/i), 40000) && /Remove\s*1/i.test(await block()), await block());
  const before = started();
  await button("Install Updates").click();
  check("and the next confirm step names it", /Removes 1 package: libold-compat1/i.test(await block()) && started() === before, await block());
  await s.shot("13-confirm-removal");
  await s.end("failed install");
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
  await toUpd(); await s.shot("4-data");
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
  await s.end("wrong key");

  s = await session(browser, KEY); p = s.page;
  hostWrite("job.conf", `RC=0\nDELAY=14\nPER=2\nREBOOT=1\nNOTE=${quote(HOSTILE)}\n`); fs.copyFileSync(SIMS.quiet, hostPath("apt-sim-after.txt"));
  await p.goto(BASE + "/padd#/systems/defiant"); await until(async () => await pkgRows() === 12); await toUpd();
  await tap(INSTALL);
  const ask = await sheet();
  check("the confirm sheet names the count and the warnings", /Install updates\?/.test(ask) && /20 packages will be upgraded, 11 of them security updates\./.test(ask)
        && /It restarts services \(tailscale\): the sites can be unreachable for a few seconds\./.test(ask) && /It needs a reboot afterwards \(libc6\), and this button never reboots\./.test(ask) && started() === 0, ask);
  await s.shot("9-confirm");
  await sleep(550); await tap(YES);
  check("the progress card shows the log, Install and Scan now are off", await until(has(/Installing updates/)) && await until(async () => /Setting up libexample/.test(await p.locator("#p-view .p-sy-log").textContent()))
        && await disabled(INSTALL) && await disabled(SCAN) && started() === 1, await view());
  check("text from the host is shown as text: a log line with markup does nothing", await p.evaluate(() => !window.__pwned && !document.querySelector("#evil, #p-view img"))
        && (await p.locator("#p-view .p-sy-log").textContent()).includes(HOSTILE), await p.locator("#p-view .p-sy-log").textContent());
  await toUpd(); await s.shot("10-running");
  check("the page shows the result: 20 installed, reboot required", await until(has(/Install finished/), 60000) && /20\s*installed/.test(await view()) && /reboot required afterwards: yes/.test(await view()), await view());
  await s.shot("11-done");
  check("the block refreshes by itself after the rescan: nothing to install, reboot required", await until(has(/0\s*to install/), 30000)
        && /Nothing to install\./.test(await view()) && /Required\s*Reboot/.test(await view()), await view());
  await toUpd(); await s.shot("12-after");

  sim("many"); fs.rmSync(hostPath("apt-sim-after.txt")); hostWrite("job.conf", "RC=100\nDELAY=2\nPER=16\n"); await sleep(2200);      // the next install fails, soon
  await toUpd(); await tap(SCAN); await until(async () => await pkgRows() === 12);
  await tap(INSTALL); await sleep(550); await tap(YES);
  check("a failed install shows its exit code and the log in red, and Install is not stuck on Starting", await until(has(/Install failed/), 40000)
        && /exit 100/.test(await view()) && /FLEET-APT-EXIT 100/.test(await p.locator("#p-view .p-sy-log.err").textContent())
        && await p.locator(INSTALL).first().textContent() === "Install updates" && !/Starting the install/.test(await view()), await view());
  await toUpd(); await s.shot("13-failed");

  /* the guard, as on the desktop page: no page change is needed for the hint */
  const lastLine = async () => (await p.locator("#p-view .p-sy-log.err").textContent({ timeout: 1000 }).catch(() => "")).trim().split("\n").pop();
  await rescanned(p);
  sim("removal"); hostWrite("job.conf", "RC=0\nDELAY=2\nPER=2\n"); await sleep(2200);
  await toUpd(); await tap(INSTALL); await sleep(550); await tap(YES);
  check("an install that apt refuses (--no-remove) shows its exit code and ends with the hint, as the last line of the log", await until(async () => await lastLine() === HINT, 40000)
        && /exit 100/.test(await view()) && /remove is disabled/.test(await p.locator("#p-view .p-sy-log.err").textContent()), await view());
  await toUpd(); await s.shot("14-guard");
  check("the scan that follows names the removal on the page", await until(has(/The install removes 1 package: libold-compat1/), 40000) && /1\s*Remove/.test(await view()), await view());
  const before = started();
  await toUpd(); await tap(INSTALL);
  check("and the confirm sheet names it", /It removes 1 package: libold-compat1\./.test(await sheet()) && started() === before, await sheet());
  await s.shot("15-confirm-removal");
  await tap('[data-k="no"]');
  await s.end("failed install");
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
