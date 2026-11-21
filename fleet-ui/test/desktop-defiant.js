#!/usr/bin/env node
/* Screenshots and checks for the Defiant view of the desktop page (fleet.html: the page the Mac app shows),
   on WebKit at desktop size.

   Everything is answered from generated fixtures under the made-up origin http://fleet.test: the page under
   test never reaches a real Fleet, so it can never install, scan, stop or alert anything, and it never needs
   a real Starfleet key (the fixtures accept the string "fixture-key"). Requests to any other origin are
   aborted and fail the run, Google Fonts excepted. Every POST is recorded and compared with what the scene
   declared; a GET of an /api path without a fixture fails the scene too.

   The scenes walk the states of the Updates block: never scanned, scanning, scan error with old data, nothing
   pending, updates with security and removals and deferred, install confirm, wrong or missing key, installing
   with log tail and lost contact, done with reboot required, failed. Further scenes check escaping of hostile
   package names, a failing updates endpoint, the unconfigured view, the narrow window and (on a fake clock) the
   polling: every 15 s when idle, every 3 s while a scan or the install runs, nothing after the view is left.

   Run:  PW=<path to the playwright package> node fleet-ui/test/desktop-defiant.js [name-filter]
         OUT=<folder> for the pictures (default: fleet-ui/test/out). HTML=<file> to test another copy of the page.
         UPD_JSON=<file> adds a scene that shows that file as the body of GET /api/defiant/updates (for example
         what the backend printed for a real apt simulation). Exit code 1 when a check fails. */
"use strict";
const fs = require("fs"), path = require("path");
const { webkit } = require(process.env.PW || "playwright");
let core = {fixtures: {}};
try{ core = require("./scenes-core.js"); }catch(e){ console.error("scenes-core.js not loadable, using a minimal /api/state: " + e.message.split("\n")[0]); }
const UI = path.join(__dirname, ".."), OUT = process.env.OUT || path.join(__dirname, "out"), BASE = "http://fleet.test";
const HTML = process.env.HTML || path.join(UI, "fleet.html");
const only = process.argv[2] || "";
const TYPES = {".js": "application/javascript", ".css": "text/css"};
const KEY = "fixture-key", UPD = "/api/defiant/updates";
const POLICY = (core.fixtures && core.fixtures.POLICY) || "Only Starfleet Command may load models.";
const AMBER = "rgb(255, 204, 102)", RED = "rgb(204, 102, 102)";

/* ---------- fixtures: all names and numbers are made up ---------- */
const NOW = () => Date.now() / 1000, KB = 1048576;
const pkg = (name, from, to, suites, o) => Object.assign({name, from, to, suites, origin: "Ubuntu",
  security: suites.some(s => /-security$/.test(s)), third_party: false}, o || {});
const SEC = ["resolute-updates", "resolute-security"], UP = ["resolute-updates"];
const PACKAGES = [
  pkg("libc6", "2.43-0ubuntu3", "2.43-0ubuntu4", SEC), pkg("libssl3t64", "3.5.1-1ubuntu1", "3.5.1-1ubuntu2", SEC),
  pkg("linux-image-7.0.0-14-generic", null, "7.0.0-14.14", SEC), pkg("openssl", "3.5.1-1ubuntu1", "3.5.1-1ubuntu2", ["resolute-security"]),
  pkg("python3-urllib3", "2.4.0-1", "2.4.0-1ubuntu0.1", ["resolute-security"]),
  pkg("bash", "5.3-1ubuntu1", "5.3-1ubuntu2", UP), pkg("ca-certificates", "20250619", "20250911", UP), pkg("cloud-init", "25.2-0ubuntu1", "25.3-0ubuntu1", UP),
  pkg("curl", "8.14.1-2ubuntu1", "8.14.1-2ubuntu2", UP), pkg("git", "1:2.51.0-1ubuntu1", "1:2.51.0-1ubuntu2", UP),
  pkg("libcurl4t64", "8.14.1-2ubuntu1", "8.14.1-2ubuntu2", UP), pkg("snapd", "2.71+ubuntu26.04", "2.72+ubuntu26.04", UP),
  pkg("systemd", "258-2ubuntu1", "258-2ubuntu2", UP), pkg("tar", "1.35+dfsg-3", "1.35+dfsg-3build2", UP),
  pkg("tzdata", "2025b-5ubuntu1", "2025c-0ubuntu1", UP), pkg("ubuntu-pro-client", "37.1", "38", UP),
  pkg("udev", "258-2ubuntu1", "258-2ubuntu2", UP), pkg("vim-tiny", "2:9.1.1200-1ubuntu1", "2:9.1.1200-1ubuntu2", UP),
  pkg("xxd", "2:9.1.1200-1ubuntu1", "2:9.1.1200-1ubuntu2", UP),
  pkg("containerd.io", "1.7.27-1", "1.7.28-1", ["resolute/stable"], {origin: "Docker", third_party: true}),
  pkg("docker-ce", "5:28.0.1-1~ubuntu.26.04~resolute", "5:28.1.0-1~ubuntu.26.04~resolute", ["resolute/stable"], {origin: "Docker", third_party: true}),
  pkg("docker-ce-cli", "5:28.0.1-1~ubuntu.26.04~resolute", "5:28.1.0-1~ubuntu.26.04~resolute", ["resolute/stable"], {origin: "Docker", third_party: true}),
  pkg("tailscale", "1.80.2", "1.82.0", ["stable/main"], {origin: "Tailscale", third_party: true}),
  pkg("unattended-upgrades", "2.12ubuntu5", "2.12ubuntu6", UP),
];
function mkScan(o){
  const s = Object.assign({at: NOW() - 720, next_at: NOW() + 2880, busy: false, error: null, error_at: null, packages: PACKAGES.slice(),
    remove: [], deferred: [], reboot_required: false, reboot_pkgs: []}, o || {});
  s.packages.sort((a, b) => (b.security - a.security) || a.name.localeCompare(b.name));
  if(!o || o.count == null) s.count = s.packages.length;
  s.security = s.packages.filter(p => p.security).length;
  return s;
}
const mkJob = o => Object.assign({state: "idle", started_at: null, ended_at: null, exit: null, log: [], lost: false, installed: null, reboot_required: null}, o || {});
const mkUpd = o => Object.assign({configured: true, scan: mkScan(), job: mkJob()}, o || {});
const finish = u => u.configured === false ? {configured: false}
  : Object.assign({}, u, {now: NOW(), critical: !!(u.scan && (u.scan.security > 0 || u.scan.reboot_required))});
const logLines = n => Array.from({length: n}, (_, i) => i % 3 === 0 ? `Unpacking libfoo${i}:arm64 (1.${i}-2) over (1.${i}-1) ...` : i % 3 === 1 ? `Preparing to unpack .../libfoo${i}_1.${i}-2_arm64.deb ...` : `Setting up libfoo${i}:arm64 (1.${i}-2) ...`);
function defBody(u){
  const s = u.scan, sec = s ? s.security : 0;
  return {configured: true, host: "defiant.example", os: "Ubuntu 26.04 LTS", kernel: "7.0.0-12-generic", uptime_s: 41 * 86400 + 3600, load: "0.12 0.08 0.05", cpus: 4, cpu_pct: 3.4,
    mem_total_kb: 8 * KB, mem_avail_kb: 5.6 * KB, disk_total: 80e9, disk_used: 21e9, disk_avail: 59e9, rx_bps: 18000, tx_bps: 64000,
    reboot_required: !!(s && s.reboot_required), updates: s ? s.count + s.deferred.length : 0,
    containers: [{name: "caddy", state: "running", status: "Up 41 days"}, {name: "app-web", state: "running", status: "Up 12 days (healthy)"}, {name: "app-db", state: "running", status: "Up 12 days"}],
    sites: [{domain: "example.org", status: 200, ms: 84, cert_days: 61, error: null, deployed: NOW() - 86400 * 3}, {domain: "docs.example.org", status: 200, ms: 91, cert_days: 61, error: null, deployed: NOW() - 86400 * 9},
            {domain: "status.example.net", status: 401, ms: 77, cert_days: 40, error: null, deployed: null}],
    problems: sec ? [`${sec} security update${sec === 1 ? "" : "s"} pending`] : [], errors: [], status: sec ? "warn" : "ok", at: NOW(),
    upd: s && s.at ? {count: s.count, security: sec, deferred: s.deferred.length, remove: s.remove.length, scan_at: s.at, job_state: u.job.state} : null};
}
const keyed = h => (h || {})["x-starfleet-key"] === KEY;
const state = () => core.fixtures.state ? core.fixtures.state("nominal") : {nodes: {}, running: [], pins: [], errors: {}, queued: [], llm_loading: [], models: [], loading: []};

/* ---------- what the made-up Fleet answers; W is the scene's world, scenes change it while the page runs ---------- */
function answer(W, method, url, body, headers){
  const p = url.pathname;
  if(method === "GET"){
    if(p === "/api/state") return state();
    if(p === "/api/memoryalpha") return core.fixtures.alpha ? core.fixtures.alpha() : {status: "ok", problems: [], errors: [], containers: [], at: NOW()};
    if(p === "/api/containers") return {containers: [], sessions: [], errors: {}, at: NOW()};
    if(p === "/api/cluster/update") return {state: {phase: "idle"}, pending: {}, pending_at: null};   /* the cluster software strip under Node Status: not part of this feature, only answered so the page loads */
    if(p === "/api/defiant" || p === UPD) W.gets[p] = (W.gets[p] || 0) + 1;
    if(p === "/api/defiant") return W.upd.configured === false ? {configured: false, status: "off", at: NOW()} : defBody(W.upd);
    if(p === UPD) return W.updStatus ? {__status: W.updStatus, body: {error: "boom"}} : W.updOff ? {configured: false} : finish(W.upd);
    return;
  }
  if(p === UPD + "/scan") return W.scanReply || {ok: true, started: true};
  if(p === UPD + "/install"){
    if(!keyed(headers)) return {__status: 403, body: {error: POLICY}};
    if(W.installReply) return W.installReply;
    W.upd.job = mkJob({state: "running", started_at: NOW()});
    return {ok: true, started_at: W.upd.job.started_at};
  }
}
const fulfil = (route, r) => r && r.__status ? route.fulfill({status: r.__status, contentType: "application/json", body: JSON.stringify(r.body || {})})
  : route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(r)});

/* ---------- scenes: {name, upd (fixture), key, size, clock, posts, run(page, t)} ---------- */
const btn = label => `#updbox button.bigbtn:text-matches("^${label}", "i")`;
const click2 = async t => { await t.click(btn("Install")); await t.page.waitForTimeout(500); await t.click(btn("Sure")); };   /* a confirm ignores a click in its first 450 ms */
const text = async (page, sel) => page.locator(sel).first().innerText();
const SAMPLE = {remove: ["libfoo-old1", "python3-oldlib"], deferred: ["distro-info", "python3-distro-info", "software-properties-common", "systemd-resolved"]};
const PLAIN = PACKAGES.filter(p => !p.security && !/^(docker|containerd|tailscale|linux-image|libc6)/.test(p.name)).slice(0, 8);
const HOSTILE = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2<\/script>';

const scenes = [
  {name: "never-scanned", upd: () => mkUpd({scan: null}), posts: [[UPD + "/scan", {}]],
    run: async (page, t) => {
      t.ok(/no scan yet/i.test(await text(page, "#updbox")), "never scanned: the block says there is no scan yet");
      t.ok(await page.locator(btn("Install")).isDisabled(), "never scanned: Install updates is disabled");
      t.ok(/scan first/i.test(await text(page, "#updbox")), "never scanned: the reason for the disabled Install is shown");
      t.ok(/first scan runs shortly after fleet starts/i.test(await text(page, "#updbox .node:nth-child(2) .sub2")), "never scanned: the Scan panel says the first scan is still to come");
      t.ok(await page.locator(btn("Scan now")).isEnabled(), "never scanned: Scan now is enabled");
      await t.shot("1-empty");
      await t.click(btn("Scan now"));
      t.ok(/scanning/i.test(await text(page, btn("Scanning"))), "after Scan now the button says Scanning");
      t.ok(await page.locator(btn("Scanning")).isDisabled(), "after Scan now the button is disabled");
      await t.shot("2-asked");
      t.W.upd = mkUpd({scan: mkScan({busy: true, at: null, packages: []})}); await t.poll();
      t.ok(/scanning/i.test(await text(page, "#updbox")), "the worker reports busy: still Scanning");
      t.W.upd = mkUpd(); await t.poll();
      t.ok(await page.locator("#updbox tbody tr").count() === PACKAGES.length, "the first scan appears by itself, one row per package");
    }},
  {name: "first-scan-failed", upd: () => mkUpd({scan: mkScan({at: null, next_at: NOW() + 3000, error: "apt-get update failed: could not get lock /var/lib/dpkg/lock-frontend", error_at: NOW() - 20, packages: []})}),
    run: async (page, t) => {
      const tx = await text(page, "#updbox");
      t.ok(/no scan yet/i.test(await text(page, "#updbox .node:nth-child(2) .mem")), "first scan failed: still no scan");
      t.ok(/scan failed .*could not get lock/i.test(tx) && !/last good scan/i.test(tx), "first scan failed: the error shows, and no old data is claimed");
      t.ok(await page.locator(btn("Install")).isDisabled() && /scan first/i.test(tx), "first scan failed: Install updates is disabled, scan first");
      t.ok(await page.locator(btn("Scan now")).isEnabled(), "first scan failed: Scan now can retry");
      t.ok(await page.locator("#updbox table").count() === 0, "first scan failed: no table");
      const sub = await text(page, "#updbox .node:nth-child(2) .sub2");
      t.ok(/fleet tries again at .*\d/i.test(sub) && !/shortly after fleet starts/i.test(tx), "first scan failed: says when Fleet tries again, not that the first scan is still to come: " + sub);
    }},
  {name: "scanning", upd: () => mkUpd({scan: mkScan(Object.assign({busy: true}, SAMPLE))}),
    run: async (page, t) => {
      t.ok(/scanning\.\.\./i.test(await text(page, "#updbox .nodes")), "scanning: the block says Scanning...");
      t.ok(await page.locator(btn("Scanning")).isDisabled(), "scanning: the Scan button is disabled");
      t.ok(await page.locator("#updbox tbody tr").count() === PACKAGES.length, "scanning: the old list stays on screen");
      t.ok(await page.locator(btn("Install")).isDisabled() && /a scan is running/i.test(await text(page, "#updbox .node:nth-child(3)")), "scanning: Install updates waits for the scan, and says why");
    }},
  {name: "scan-now-first", upd: () => mkUpd({scan: null}), posts: [[UPD + "/scan", {}]],
    run: async (page, t) => {
      await t.click(btn("Scan now"));
      t.W.upd = mkUpd(); await t.poll();                 /* the first scan was never visible as busy: it simply arrives */
      t.ok(!/scanning/i.test(await text(page, "#updbox .nodes")) && await page.locator(btn("Scan now")).isEnabled(), "first scan: the Scanning label ends the moment the scan appears");
      t.ok(await page.locator("#updbox tbody tr").count() === PACKAGES.length && await page.locator(btn("Install")).isEnabled(), "first scan: the list and Install updates are there");
    }},
  {name: "updates-endpoint-says-unconfigured", upd: () => mkUpd(), updOff: true,
    run: async (page, t) => {
      t.ok(await page.locator("#updbox button").count() === 0 && !/Pending Updates/i.test(await text(page, "#defbody")), "updates endpoint unconfigured: no Updates block and no button");
      t.ok(await page.locator("#defbody table[data-table=sites] tbody tr").count() === 3, "updates endpoint unconfigured: the rest of the view is intact");
    }},
  {name: "scan-error", upd: () => mkUpd({scan: mkScan(Object.assign({error: "ssh: connect to host defiant.example port 22: Connection timed out", error_at: NOW() - 300}, SAMPLE))}),
    run: async (page, t) => {
      const tx = await text(page, "#updbox");
      t.ok(/connection timed out/i.test(tx) && /last good scan/i.test(tx), "scan error: the error line names the error and the old data");
      t.ok(await page.locator("#updbox tbody tr").count() === PACKAGES.length, "scan error: the old list stays on screen");
      t.ok(await page.locator("#updbox .err").evaluate(e => getComputedStyle(e).color) === RED, "scan error: the error line is red");
      t.ok(await page.locator("#updbox .node.warn").count() >= 2, "scan error: the Scan panel turns amber");
      t.ok(await page.locator(btn("Scan now")).isEnabled(), "scan error: Scan now can retry");
    }},
  {name: "nothing-pending", upd: () => mkUpd({scan: mkScan({packages: [], deferred: SAMPLE.deferred})}),
    run: async (page, t) => {
      const tx = await text(page, "#updbox");
      t.ok(/nothing to install/i.test(tx), "nothing pending: says so");
      t.ok(/staged rollout/i.test(tx) && /4 held back/i.test(tx), "nothing pending: the held back count comes with the staged rollout explanation");
      t.ok(await page.locator(btn("Install")).isDisabled(), "nothing pending: Install updates is disabled");
      t.ok(await page.locator("#updbox table").count() === 0, "nothing pending: no empty table");
      t.ok(await page.locator("#defmsg").evaluate(e => getComputedStyle(e).display) === "none", "nothing pending: no warning bar");
    }},
  {name: "updates", upd: () => mkUpd({scan: mkScan(SAMPLE)}),
    run: async (page, t) => {
      const rows = await page.$$eval("#updbox tbody tr", r => r.map(x => [x.className, x.cells[0].textContent, x.cells[1].textContent, x.cells[2].textContent, x.cells[3].textContent, x.cells[4].textContent]));
      t.ok(rows.length === PACKAGES.length, `updates: one row per package (${rows.length})`);
      const nsec = PACKAGES.filter(p => p.security).length;
      t.ok(rows.slice(0, nsec).every(r => r[0] === "secure" && /Security/.test(r[5])) && rows.slice(nsec).every(r => r[0] !== "secure"), "updates: security rows come first and carry the Security tag");
      t.ok(rows.filter(r => /Third party/.test(r[5])).length === 4, "updates: four third-party packages carry the hint");
      t.ok(rows.find(r => r[1] === "linux-image-7.0.0-14-generic")[2] === "not installed", "updates: a new install has no installed version");
      const tx = await text(page, "#updbox");
      t.ok(/libfoo-old1, python3-oldlib/.test(tx) && /removes 2 packages/i.test(tx), "updates: the removals are listed with a warning");
      t.ok(/4 held back/i.test(tx) && /staged rollout/i.test(tx), "updates: deferred count and explanation");
      t.ok(/5 security/i.test(tx), "updates: the security count");
      t.ok(/security updates and a pending reboot are sent once to telegram/i.test(tx), "updates: the Scan panel says where the alerts go");
      t.ok(await page.locator("#updbox .stat.warn b").first().evaluate(e => getComputedStyle(e).color) === AMBER, "updates: the security count is amber");
      t.ok(/security updates pending/i.test(await text(page, "#defmsg")), "updates: the backend's problem text shows in the warning bar");
      t.ok(/^Updates24$/.test(await page.locator("#defbody .node").first().locator(".stat", {hasText: "Updates"}).textContent()), "updates: the Server panel counts the same 24 updates");
      t.ok(/Last scan .+\(12 min ago\) . next /.test(await page.locator("#updbox .sub2").nth(1).textContent()), "updates: Last scan <time> (<ago>) and next <time>");
      t.ok(await page.locator(btn("Install")).isEnabled(), "updates: Install updates is enabled");
      await t.shot("list");
      await page.locator('#updbox th[data-sort="name"]').click(); await page.waitForTimeout(200);
      const names = await page.$$eval("#updbox tbody tr td:first-child", c => c.map(x => x.textContent));
      t.ok(names.join() === names.slice().sort((a, b) => a.localeCompare(b, undefined, {numeric: true, sensitivity: "base"})).join(), "updates: clicking Package sorts by name");
      await page.locator('#updbox th[data-sort="kind"]').click(); await page.waitForTimeout(200);
      const kinds = await page.$$eval("#updbox tbody tr td:last-child", c => c.map(x => x.textContent));
      t.ok(/Security/.test(kinds[0]) && /Security/.test(kinds[4]) && !/Security/.test(kinds[5]), "updates: clicking Kind puts the security rows first");
      t.ok(await page.evaluate(() => /updpkg/.test(localStorage.getItem("fleetSort") || "")), "updates: the sort choice is kept in the browser");
      await page.locator('#updbox th[data-sort="kind"]').click(); await page.locator('#updbox th[data-sort="kind"]').click(); await page.waitForTimeout(200);
    }},
  {name: "capped-list", upd: () => mkUpd({scan: mkScan({count: 530, packages: PACKAGES.slice(0, 12)})}),
    run: async (page, t) => {
      t.ok(/\+518 more not listed/i.test(await text(page, "#updbox tbody")), "capped: the rows the backend did not send are counted");
      t.ok(/530/.test(await text(page, "#updbox .mem")), "capped: the headline number is the true count");
    }},
  {name: "install-confirm", upd: () => mkUpd({scan: mkScan(SAMPLE)}),
    run: async (page, t) => {
      await t.click(btn("Install"));
      const b = await text(page, btn("Sure")), tx = await text(page, "#updbox .node:nth-child(3)");
      t.ok(/sure\? install 24 updates/i.test(b), "confirm: the first click asks, with the package count: " + b);
      t.ok(/install 24 updates \(5 security\)/i.test(tx), "confirm: the text names the package count and the security share");
      t.ok(/removes 2 packages: libfoo-old1, python3-oldlib/i.test(tx), "confirm: lists the removals");
      t.ok(/restarts services \(containerd, docker, tailscale\).*unreachable for a few seconds/i.test(tx), "confirm: warns that docker, containerd and tailscale restart");
      t.ok(/needs a reboot afterwards \(libc6, linux-image\)\.\s*this button never reboots/i.test(tx), "confirm: warns about the kernel and libc6, and that the button never reboots");
      t.ok(await page.locator(btn("Sure")).evaluate(e => getComputedStyle(e).backgroundColor) === "rgb(204, 102, 102)", "confirm: the armed button is red");
      t.ok(t.posts.length === 0, "confirm: nothing is sent by the first click");
      await t.shot("armed");
    }},
  {name: "install-double-click", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/install", {}]],
    run: async (page, t) => {
      await page.locator(btn("Install")).dblclick(); await page.waitForTimeout(300);
      t.ok(t.posts.length === 0 && /sure\?/i.test(await text(page, "#updbox .bigbtn.armed")), "double click: it only asks, nothing is sent");
      await page.waitForTimeout(600); await t.click(btn("Sure"));
      t.ok(t.posts.length === 1, "double click: a later click confirms");
    }},
  {name: "install-confirm-plain", upd: () => mkUpd({scan: mkScan({packages: PLAIN.concat([pkg("libc6-dev", "2.43-0ubuntu3", "2.43-0ubuntu4", UP), pkg("linux-libc-dev", "7.0.0-12.12", "7.0.0-14.14", UP)])})}),
    run: async (page, t) => {
      await t.click(btn("Install"));
      const tx = await text(page, "#updbox .node:nth-child(3)");
      t.ok(/install 10 updates on defiant now\?/i.test(tx), "plain confirm: names the count, no security share: " + tx);
      t.ok(!/removes|restarts|reboot/i.test(tx.replace(/never reboots/i, "")), "plain confirm: no removal, restart or reboot warning when the list has none of them (libc6-dev and linux-libc-dev are not libc6 or a kernel image): " + tx);
    }},
  {name: "install-wrong-key", upd: () => mkUpd({scan: mkScan(SAMPLE)}), key: "wrong", posts: [[UPD + "/install", {}]],
    run: async (page, t) => {
      await click2(t);
      const m = await text(page, "#defmsg");
      t.ok(/install updates failed: starfleet key missing or wrong \(enter it on the shuttle bay page, save key\)/i.test(m), "wrong key: the 403 wording of Back up now: " + m);
      t.ok(await page.locator("#defmsg").evaluate(e => e.classList.contains("err")), "wrong key: the bar is red");
      t.ok(await page.locator(btn("Install")).isEnabled(), "wrong key: Install updates can be tried again");
      t.ok(t.W.upd.job.state === "idle", "wrong key: no job was started");
      await t.shot("refused");
    }},
  {name: "install-no-key", upd: () => mkUpd({scan: mkScan(SAMPLE)}), key: "", posts: [[UPD + "/install", {}]],
    run: async (page, t) => {
      await click2(t);
      t.ok(/starfleet key missing or wrong/i.test(await text(page, "#defmsg")), "no key: the same wording");
    }},
  {name: "install-gate-down-403", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/install", {}]],
    run: async (page, t) => {
      t.W.installReply = {__status: 403, body: {error: "Cannot verify the Starfleet key right now (gate not answering); try again in a moment."}};
      await click2(t);
      const m = await text(page, "#defmsg");
      t.ok(/install updates failed: cannot verify the starfleet key right now \(gate not answering\); try again in a moment\./i.test(m), "gate down: the server's own words show: " + m);
      t.ok(!/key missing or wrong|save key/i.test(m), "gate down: the key is not blamed: " + m);
      t.ok(await page.locator("#defmsg").evaluate(e => e.classList.contains("err")), "gate down: the bar is red");
      t.ok(await page.locator(btn("Install")).isEnabled() && t.W.upd.job.state === "idle", "gate down: no job was started, Install updates can be tried again");
    }},
  {name: "install-ssh-502", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/install", {}]],
    run: async (page, t) => {
      t.W.installReply = {__status: 502, body: {error: "ssh: connect to host defiant.example port 22: Connection timed out"}};
      await click2(t);
      t.ok(/install updates failed: ssh: connect to host defiant\.example/i.test(await text(page, "#defmsg")), "502: the server's reason shows");
      t.ok(await page.locator(btn("Install")).isEnabled() && !/installing/i.test(await text(page, "#updbox")), "502: no progress panel for an install that did not start");
    }},
  {name: "install-busy-409", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/install", {}]],
    run: async (page, t) => {
      t.W.installReply = {__status: 409, body: {error: "an install is already running"}};
      await click2(t);
      t.ok(/install updates failed: an install is already running/i.test(await text(page, "#defmsg")), "409: the server's reason shows");
    }},
  {name: "scan-now-recent", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/scan", {}]],
    run: async (page, t) => {
      t.W.scanReply = {ok: true, started: false, reason: "recent"};
      await t.click(btn("Scan now")); await page.waitForTimeout(400);
      t.ok(await page.locator(btn("Scan now")).isEnabled() && !/scanning/i.test(await text(page, "#updbox .nodes")), "scan now, answered 'recent': nothing pretends to scan");
      t.ok(await page.locator("#defmsg").evaluate(e => !e.classList.contains("err")), "scan now, answered 'recent': no error");
      t.ok(/a scan ran a moment ago\./i.test(await text(page, "#toast")), "scan now, answered 'recent': the page says why nothing happened");
    }},
  {name: "scan-now-busy", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/scan", {}]],
    run: async (page, t) => {
      t.W.scanReply = {ok: true, started: false, reason: "busy"};
      await t.click(btn("Scan now")); await page.waitForTimeout(400);
      t.ok(/a scan is already running\./i.test(await text(page, "#toast")), "scan now, answered 'busy': the page says a scan is running");
      t.ok(await page.locator(btn("Scan now")).isEnabled() && !/scanning/i.test(await text(page, "#updbox .nodes")), "scan now, answered 'busy': nothing pretends to scan");
    }},
  {name: "scan-now-started", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/scan", {}]],
    run: async (page, t) => {
      await t.click(btn("Scan now"));
      t.ok(await page.locator("#toast").count() === 0, "scan now, accepted: no refusal toast");
      t.ok(/scanning\.\.\./i.test(await text(page, "#updbox .nodes")) && await page.locator(btn("Scanning")).isDisabled(), "scan now: Scanning... at once, before the worker reports busy");
      t.W.upd.scan = mkScan(Object.assign({busy: true}, SAMPLE)); await t.poll();
      t.ok(/scanning\.\.\./i.test(await text(page, "#updbox .nodes")), "scan now: the worker's busy flag keeps it on Scanning...");
      t.W.upd.scan = mkScan(Object.assign({at: NOW()}, SAMPLE)); await t.poll();
      t.ok(!/scanning/i.test(await text(page, "#updbox .nodes")) && await page.locator(btn("Scan now")).isEnabled(), "scan now: back to Scan now the moment the scan is over, not 8 s later");
      t.ok(/\(\d+ s ago\)/i.test(await page.locator("#updbox .sub2").nth(1).textContent()), "scan now: the new scan time shows");
    }},
  {name: "scan-now-refused", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/scan", {}]],
    run: async (page, t) => {
      t.W.scanReply = {__status: 400, body: {error: "Defiant is not configured"}};
      await t.click(btn("Scan now")); await page.waitForTimeout(300);
      t.ok(/scan now failed: defiant is not configured/i.test(await text(page, "#defmsg")), "scan now, refused: the reason shows");
    }},
  {name: "installing", upd: () => mkUpd({scan: mkScan(SAMPLE)}), posts: [[UPD + "/install", {}]],
    run: async (page, t) => {
      await click2(t);
      t.ok(t.posts.length === 1, "installing: the second click sends the install request");
      t.ok(/installing updates/i.test(await text(page, "#updbox")), "installing: the progress panel shows");
      t.ok(/waiting for the first line/i.test(await text(page, "#updbox .joblog")), "installing: before the first log line");
      t.ok(await page.locator(btn("Installing")).isDisabled(), "installing: Install updates is disabled");
      t.ok(/install is running/i.test(await text(page, "#updbox")), "installing: the reason for the disabled button");
      t.ok(await page.locator(btn("Scan now")).isDisabled(), "installing: Scan now is disabled while the install runs");
      t.ok(/started .*\(\d+ s ago\)/i.test(await text(page, "#updbox .node:last-child .sub2")), "installing: started time");
      t.ok(!/lost contact/i.test(await text(page, "#updbox")), "installing: no lost contact line while contact is fine");
      t.W.upd.job.log = logLines(40); await t.poll();
      t.ok((await text(page, "#updbox .joblog")).includes("Setting up libfoo38:arm64"), "installing: the log tail follows");
      t.ok(await page.locator("#updbox .joblog").evaluate(e => e.scrollTop + e.clientHeight >= e.scrollHeight - 2), "installing: the log stays scrolled to its newest line");
      await t.shot("log");
      t.W.upd.job.lost = true; await t.poll();
      t.ok(/lost contact with defiant, retrying/i.test(await text(page, "#updbox")), "installing: lost contact is said");
      t.ok((await text(page, "#updbox .joblog")).includes("Setting up libfoo38:arm64"), "installing: the log stays while contact is lost");
      await t.shot("lost");
      t.W.upd.job = mkJob({state: "done", started_at: NOW() - 240, ended_at: NOW() - 5, exit: 0, installed: 24, reboot_required: true, log: logLines(40)});
      t.W.upd.scan = mkScan({busy: true, remove: [], deferred: SAMPLE.deferred, reboot_required: true, reboot_pkgs: ["linux-image-7.0.0-14-generic", "libc6"]}); await t.poll();
      const tx = await text(page, "#updbox");
      t.ok(/install finished/i.test(tx) && /installed 24 packages/i.test(tx) && /reboot required afterwards: yes/i.test(tx), "done: the result line has the count and the reboot flag");
      t.ok(/scanning\.\.\./i.test(tx), "done: the block rescans and says so");
      t.ok(!/lost contact/i.test(tx), "done: no lost contact line");
      await t.shot("done-rescan");
      t.W.upd.scan = mkScan({packages: [], deferred: SAMPLE.deferred, reboot_required: true, reboot_pkgs: ["linux-image-7.0.0-14-generic", "libc6"]}); await t.poll();
      const tx2 = await text(page, "#updbox");
      t.ok(/nothing to install/i.test(tx2) && /reboot needed for linux-image-7.0.0-14-generic, libc6/i.test(tx2), "done: the refreshed block shows nothing pending and the packages that want a reboot");
      t.ok(await page.locator("#updbox .stat.warn b", {hasText: "Required"}).count() === 1, "done: the Reboot chip is amber");
      await t.shot("done");
    }},
  {name: "failed", upd: () => mkUpd({scan: mkScan(SAMPLE), job: mkJob({state: "failed", started_at: NOW() - 900, ended_at: NOW() - 600, exit: 100,
      log: logLines(36).concat(["E: Sub-process /usr/bin/dpkg returned an error code (1)", "FLEET-APT-EXIT 100 1763632000"])})}),
    run: async (page, t) => {
      const tx = await text(page, "#updbox");
      t.ok(/install failed \(exit 100\)/i.test(tx), "failed: the exit code is in the heading");
      t.ok((await text(page, "#updbox .joblog")).includes("Sub-process /usr/bin/dpkg returned an error code (1)"), "failed: the log tail shows");
      t.ok(await page.locator("#updbox .node.crit .joblog").evaluate(e => getComputedStyle(e).color) === RED, "failed: the log is red");
      t.ok(await page.locator("#updbox .node.crit h3").evaluate(e => getComputedStyle(e).color) === RED, "failed: the heading is red");
      t.ok(await page.locator(btn("Install")).isEnabled(), "failed: Install updates can be tried again");
      t.ok(/ended 10 min ago/i.test(tx), "failed: when it ended");
    }},
  {name: "failed-no-exit-code", upd: () => mkUpd({scan: mkScan(SAMPLE), job: mkJob({state: "failed", started_at: NOW() - 900, ended_at: NOW() - 600, exit: null, log: ["unit ended without an exit line"]})}),
    run: async (page, t) => {
      t.ok(/install failed/i.test(await text(page, "#updbox")) && !/exit/i.test(await text(page, "#updbox .node.crit h3")), "failed without an exit line: no invented exit code");
    }},
  {name: "done-unknown-count", upd: () => mkUpd({scan: mkScan({packages: [], count: 0}), job: mkJob({state: "done", started_at: NOW() - 900, ended_at: NOW() - 600, exit: 0, installed: null, reboot_required: null})}),
    run: async (page, t) => {
      const tx = await text(page, "#updbox .node:last-child");
      t.ok(/install finished/i.test(tx) && !/installed \d/i.test(tx) && /reboot required afterwards: unknown/i.test(tx), "done with unknown numbers: nothing is guessed: " + tx);
    }},
  {name: "done-after-reboot", upd: () => mkUpd({scan: mkScan({packages: [], reboot_required: false}), job: mkJob({state: "done", started_at: NOW() - 86400 * 3, ended_at: NOW() - 86400 * 3 + 200, exit: 0, installed: 12, reboot_required: true})}),
    run: async (page, t) => {
      const tx = await text(page, "#updbox .node:last-child");
      t.ok(/installed 12 packages/i.test(tx) && /reboot required afterwards: yes/i.test(tx), "old result: says what it was: " + tx);
      t.ok(await page.locator("#updbox .node.warn").count() === 0 && !/reboot defiant when/i.test(tx), "old result: no amber and no reboot hint once the latest scan says no reboot is pending");
    }},
  {name: "main-view-loads-nothing-of-defiant", hash: "", upd: () => mkUpd({scan: mkScan(SAMPLE)}),
    run: async (page, t) => {
      await page.waitForTimeout(3500);
      t.ok(!(t.W.gets["/api/defiant"] || 0) && !(t.W.gets[UPD] || 0), `main view: Defiant and its updates are not polled until the view is opened (${t.W.gets["/api/defiant"] || 0}, ${t.W.gets[UPD] || 0})`);
      await t.click("#defbtn"); await page.waitForSelector("#updbox tbody tr", {timeout: 5000});
      t.ok(await page.locator("#updbox tbody tr").count() === PACKAGES.length, "main view: opening Defiant from the rail shows the Updates block");
      t.ok(await page.evaluate(() => location.hash) === "#def", "main view: the address keeps the view");
    }},
  {name: "leaving-to-other-views", upd: () => mkUpd({scan: mkScan(SAMPLE)}),
    run: async (page, t) => {
      await t.click("#mabtn"); await page.waitForTimeout(400);
      t.ok(await page.evaluate(() => document.body.classList.contains("ma")) && !(await page.locator("#defview").isVisible()), "other views: Memory Alpha opens from the Defiant view");
      const d0 = t.W.gets["/api/defiant"] || 0, u0 = t.W.gets[UPD] || 0;
      await t.click("#ctrbtn"); await page.waitForTimeout(400); await t.click("#defbtn"); await page.waitForTimeout(600);
      t.ok(await page.evaluate(() => document.body.classList.contains("def")) && await page.locator("#updbox tbody tr").count() === PACKAGES.length, "other views: back in Defiant the Updates block is there again");
      t.ok((t.W.gets["/api/defiant"] || 0) > d0 && (t.W.gets[UPD] || 0) > u0, "other views: Defiant polls again after the return");
      await t.click("#ctrbtn"); await page.waitForTimeout(300);
      const u1 = t.W.gets[UPD] || 0, e1 = t.W.gets["/api/defiant"] || 0; await page.waitForTimeout(3600);
      t.ok((t.W.gets[UPD] || 0) === u1 && (t.W.gets["/api/defiant"] || 0) === e1, "other views: Engineering does not poll Defiant or its updates");
    }},
  {name: "reboot-only", upd: () => mkUpd({scan: mkScan({packages: [], reboot_required: true, reboot_pkgs: ["linux-image-7.0.0-12-generic", "libc6"]})}),
    run: async (page, t) => {
      t.ok(await page.locator("#updbox .node.warn").count() >= 1, "reboot only: the panel is amber although nothing is pending");
      t.ok(/reboot needed for linux-image-7.0.0-12-generic, libc6/i.test(await text(page, "#updbox")), "reboot only: says which packages");
    }},
  {name: "hostile-text", upd: () => mkUpd({scan: mkScan({error: "boom " + HOSTILE, error_at: NOW() - 10, remove: [HOSTILE + "-old"], deferred: [HOSTILE],
      reboot_required: true, reboot_pkgs: [HOSTILE + "-boot"],
      packages: [pkg("evil" + HOSTILE, "1" + HOSTILE, "2" + HOSTILE, ["x" + HOSTILE + "-security"], {origin: HOSTILE, third_party: true})].concat(PLAIN.slice(0, 2))}),
      job: mkJob({state: "failed", started_at: NOW() - 60, ended_at: NOW() - 30, exit: HOSTILE, log: ["line " + HOSTILE]})}),
    run: async (page, t) => {
      await t.click(btn("Install"));
      const bad = await page.evaluate(() => ({pwned: window.__pwned || 0, scripts: document.querySelectorAll("#defbody script").length, imgs: document.querySelectorAll("#defbody img").length,
        injected: document.querySelectorAll("#defbody [onerror]").length}));
      t.ok(!bad.pwned && !bad.scripts && !bad.imgs && !bad.injected, "hostile: nothing from the server runs or becomes markup: " + JSON.stringify(bad));
      t.ok((await text(page, "#updbox")).includes('<img src=x onerror="window.__pwned=1">'), "hostile: the text is shown literally");
      t.ok((await page.locator("#updbox tbody tr td").first().innerText()).includes("<script>"), "hostile: the package name is shown literally");
      await t.shot("shown-literally");
    }},
  {name: "not-configured", upd: () => ({configured: false}),
    run: async (page, t) => {
      t.ok(/FLEET_DEFIANT_HOST/.test(await text(page, "#defmsg")), "not configured: the existing hint shows");
      t.ok(await page.locator("#updbox").count() === 0 && await page.locator("#updbox button").count() === 0, "not configured: no Updates block, so no Install button to press");
    }},
  {name: "updates-endpoint-down", upd: () => mkUpd(), updStatus: 500,
    run: async (page, t) => {
      t.ok(/updates: fleet not answering/i.test(await text(page, "#updbox")), "endpoint down: the block says so");
      t.ok(await page.locator("#defbody table[data-table=sites] tbody tr").count() === 3, "endpoint down: the rest of the view is intact");
      t.W.updStatus = 0; await t.poll();
      t.ok(await page.locator("#updbox tbody tr").count() === PACKAGES.length, "endpoint down: recovers by itself");
    }},
  {name: "narrow-window", upd: () => mkUpd({scan: mkScan(SAMPLE)}), size: [820, 760],
    run: async (page, t) => { await t.click(btn("Install")); await t.shot("armed"); }},

  {name: "phone-width", upd: () => mkUpd({scan: mkScan(SAMPLE)}), size: [390, 844],
    run: async (page, t) => { await t.click(btn("Install")); await t.shot("armed"); }},

  ...(process.env.UPD_JSON ? [{name: "from-file", upd: () => JSON.parse(fs.readFileSync(process.env.UPD_JSON, "utf8")),
    run: async (page, t) => {
      const s = t.W.upd.scan, n = s ? s.packages.length : 0;
      t.ok(await page.locator("#updbox tbody tr").count() === n + (s && s.count > n ? 1 : 0), "file: one row per package");
      if(s) t.ok(new RegExp(`Updates . ${s.count}`, "i").test(await text(page, "#updbox .sec")) || !s.at, "file: the headline count");
      await t.shot("file");
    }}] : []),

  /* polling on a fake clock: the page's timers only fire when the scene advances time */
  {name: "polling-idle", clock: true, upd: () => mkUpd({scan: mkScan(SAMPLE)}),
    run: async (page, t) => {
      const c = () => t.W.gets[UPD] || 0, d = () => t.W.gets["/api/defiant"] || 0, c0 = c(), d0 = d();
      t.ok(c0 === 1 && d0 === 1, `idle: one request of each kind on entering the view (${c0}, ${d0})`);
      await t.advance(14000); t.ok(c() === c0 && d() === d0, `idle: nothing before 15 s (${c() - c0}, ${d() - d0})`);
      await t.advance(2000); t.ok(c() === c0 + 1 && d() === d0 + 1, `idle: one more of each at 15 s (${c() - c0}, ${d() - d0})`);
      await t.advance(30000); t.ok(c() - c0 >= 2 && c() - c0 <= 3, `idle: the 15 s loop goes on, no 3 s polling (${c() - c0} in 46 s)`);
      await t.click("#defbtn");
      const c1 = c(), d1 = d(); await t.advance(60000);
      t.ok(c() === c1 && d() === d1, `idle: nothing after the view is left (${c() - c1}, ${d() - d1})`);
    }},
  {name: "polling-job", clock: true, upd: () => mkUpd({scan: mkScan(SAMPLE), job: mkJob({state: "running", started_at: NOW() - 30, log: logLines(5)})}),
    run: async (page, t) => {
      const c = () => t.W.gets[UPD] || 0, d = () => t.W.gets["/api/defiant"] || 0, c0 = c();
      await t.advance(12000); t.ok(c() - c0 >= 3 && c() - c0 <= 5, `job: the updates are polled every 3 s while the install runs (${c() - c0} in 12 s)`);
      t.ok(d() === 1, `job: the page's own loop stays at 15 s (${d()} requests in 12 s)`);
      t.W.upd.job = mkJob({state: "done", started_at: NOW() - 60, ended_at: NOW(), exit: 0, installed: 3, reboot_required: false});
      await t.advance(4000); const c1 = c(); await t.advance(9000);
      t.ok(c() - c1 <= 1, `job: back to the 15 s loop once the job is over (${c() - c1} in 9 s)`);
      t.W.upd.job = mkJob({state: "running", started_at: NOW() - 30}); await page.evaluate(() => updPoll()); await t.advance(500);
      await t.click("#defbtn");
      const c2 = c(), d2 = d(); await t.advance(60000);
      t.ok(c() === c2 && d() === d2, `job: nothing is polled after the view is left, although the job still runs (${c() - c2}, ${d() - d2})`);
      await t.click("#defbtn"); await t.advance(7000);
      t.ok(c() - c2 >= 2, `job: coming back resumes the 3 s polling (${c() - c2} in 7 s)`);
    }},
  {name: "polling-no-scan-and-scanning", clock: true, upd: () => mkUpd({scan: null}),
    run: async (page, t) => {
      const c = () => t.W.gets[UPD] || 0, c0 = c();
      await t.advance(9000); t.ok(c() - c0 >= 2 && c() - c0 <= 4, `no scan yet: polls every 3 s (${c() - c0} in 9 s)`);
      t.W.upd = mkUpd({scan: mkScan({busy: true})}); await t.advance(4000);
      const c1 = c(); await t.advance(9000); t.ok(c() - c1 >= 2 && c() - c1 <= 4, `scanning: polls every 3 s (${c() - c1} in 9 s)`);
      t.W.upd = mkUpd(); await t.advance(4000);
      const c2 = c(); await t.advance(10000); t.ok(c() - c2 <= 1, `scan done: back to the 15 s loop (${c() - c2} in 10 s)`);
    }},
  {name: "polling-confirm-window", clock: true, upd: () => mkUpd({scan: mkScan(SAMPLE)}),
    run: async (page, t) => {
      await t.click(btn("Install")); await t.advance(300);
      await t.advance(7000); t.ok(/sure/i.test(await text(page, "#updbox .bigbtn.armed")), "confirm window: still asking after 7 s");
      await t.advance(1500); t.ok(await page.locator("#updbox .bigbtn.armed").count() === 0 && /^install updates$/i.test(await text(page, btn("Install"))), "confirm window: the question lapses after 8 s");
      t.ok(t.posts.length === 0, "confirm window: nothing was sent");
      await t.click(btn("Install")); await t.advance(300);
      await t.click("#defbtn"); await t.click("#defbtn"); await t.advance(1000);
      t.ok(await page.locator("#updbox .bigbtn.armed").count() === 0, "confirm window: leaving the view ends the question");
    }},
];

/* ---------- runner ---------- */
(async () => {
  fs.mkdirSync(OUT, {recursive: true});
  const todo = scenes.filter(s => !only || s.name.includes(only)), failures = [];
  const browser = await webkit.launch();
  for(const sc of todo){
    const problems = [], posts = [], missing = [], extra = [];
    const W = {upd: sc.upd(), gets: {}, inflight: 0, updStatus: sc.updStatus || 0, updOff: !!sc.updOff, installReply: null, scanReply: null};
    const size = sc.size || [1280, 900];
    const ctx = await browser.newContext({viewport: {width: size[0], height: size[1]}, deviceScaleFactor: 1});
    const handle = async route => {
      const req = route.request(), url = new URL(req.url());
      if(/^fonts\.(googleapis|gstatic)\.com$/.test(url.host)) return route.continue();
      if(url.origin !== BASE){ problems.push("request left the fixture origin: " + url.origin); return route.abort(); }
      if(req.method() !== "GET"){
        let body = null; const raw = req.postData();
        try{ body = raw ? JSON.parse(raw) : null; }catch(e){ body = "<" + (raw || "").length + " bytes>"; }
        posts.push([url.pathname, body]);
        const r = answer(W, "POST", url, body, req.headers());
        if(r === undefined){ missing.push("POST " + url.pathname); return route.fulfill({status: 404, contentType: "application/json", body: '{"error":"no fixture"}'}); }
        return fulfil(route, r);
      }
      if(url.pathname === "/") return route.fulfill({status: 200, contentType: "text/html; charset=utf-8", body: fs.readFileSync(HTML)});
      if(url.pathname.startsWith("/vendor/")){
        const f = path.join(UI, "vendor", path.basename(url.pathname));
        return fs.existsSync(f) ? route.fulfill({status: 200, contentType: TYPES[path.extname(f)] || "text/plain", body: fs.readFileSync(f)}) : route.fulfill({status: 404, body: ""});
      }
      if(url.pathname.startsWith("/api/")){
        const r = answer(W, "GET", url, null, req.headers());
        if(r === undefined){ missing.push("GET " + url.pathname); return route.fulfill({status: 404, contentType: "text/plain", body: "not found"}); }
        return fulfil(route, r);
      }
      return route.fulfill({status: 404, body: ""});   /* ds9.jpg and the like: the page copes without them */
    };
    await ctx.route("**/*", async route => { W.inflight++; try{ await handle(route); }finally{ W.inflight--; } });
    const keyValue = sc.key === undefined ? KEY : sc.key;
    await ctx.addInitScript(key => { try{ if(key) localStorage.setItem("starfleetKey", key); else localStorage.removeItem("starfleetKey"); localStorage.removeItem("fleetSort"); }catch(e){} }, keyValue);
    const page = await ctx.newPage();
    page.on("pageerror", e => problems.push("page error: " + e.message));
    page.on("console", m => { if(m.type() === "error" && !/Failed to load resource/.test(m.text())) problems.push("console: " + m.text()); });
    const t = {W, posts, page,
      ok: (cond, msg) => { if(!cond) problems.push(msg); },
      click: async sel => { if(sc.clock) await page.locator(sel).first().evaluate(e => e.click()); else await page.locator(sel).first().click(); await page.waitForTimeout(sc.clock ? 80 : 250); },
      poll: async () => { await page.evaluate(() => updPoll()); await page.waitForTimeout(250); },
      advance: async ms => { for(let done = 0; done < ms; done += 500){ await page.clock.runFor(Math.min(500, ms - done));
        for(let i = 0; i < 40 && W.inflight; i++) await page.waitForTimeout(5);        /* let the answers arrive before the next slice of time */
        await page.waitForTimeout(30); } },
      shot: async name => { const f = path.join(OUT, `${sc.name}--${name}.png`); await page.screenshot({path: f, fullPage: true}); extra.push(f);
        const b = page.locator("#updbox"); if(await b.count()) await b.screenshot({path: path.join(OUT, `${sc.name}--${name}-block.png`)}); },
    };
    try{
      if(sc.clock) await page.clock.install({time: Date.now()});
      await page.goto(BASE + (sc.hash === undefined ? "/#def" : "/" + sc.hash));
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(sc.clock ? 600 : 700);
      if(sc.clock) await page.clock.pauseAt(await page.evaluate(() => Date.now()) + 50);
      if(!sc.name.startsWith("not-configured") && !sc.updStatus && !sc.clock && sc.hash === undefined) await page.waitForSelector("#updbox", {state: "attached", timeout: 5000});
      await sc.run(page, t);
      await page.waitForTimeout(150);
      if(!sc.clock){
        const found = await page.evaluate(narrow => {
          const out = [], box = (b => b && b.getClientRects().length ? b : null)(document.getElementById("updbox"));   /* a view that was left has nothing to measure */
          if(document.documentElement.scrollWidth > innerWidth + 1) out.push(`page is ${document.documentElement.scrollWidth}px wide in a ${innerWidth}px viewport`);
          if(box && !narrow){
            const b = box.getBoundingClientRect(), nodes = [...box.querySelectorAll(".node")];
            for(const el of box.querySelectorAll("*")){ const r = el.getBoundingClientRect(); if(r.width && (r.right > b.right + 1 || r.left < b.left - 1)) out.push(`sticks out of the Updates block: <${el.tagName.toLowerCase()}> "${(el.textContent || "").trim().slice(0, 30)}"`); }
            for(const n of nodes) for(const c of n.querySelectorAll("*")){ const a = n.getBoundingClientRect(), r = c.getBoundingClientRect(); if(r.width && (r.right > a.right + 1 || r.bottom > a.bottom + 1)) out.push(`sticks out of its panel: <${c.tagName.toLowerCase()}> "${(c.textContent || "").trim().slice(0, 30)}"`); }
            for(let i = 0; i < nodes.length; i++) for(let j = i + 1; j < nodes.length; j++){ const a = nodes[i].getBoundingClientRect(), r = nodes[j].getBoundingClientRect();
              if(a.left < r.right - 1 && r.left < a.right - 1 && a.top < r.bottom - 1 && r.top < a.bottom - 1) out.push("two panels overlap"); }
          }
          if(box){
            for(const tg of box.querySelectorAll(".tag")){ if(tg.getBoundingClientRect().height > 24) out.push("a tag wraps: " + tg.textContent); }
            for(const b2 of box.querySelectorAll("button")){ const r = b2.getBoundingClientRect(), s = getComputedStyle(b2); if(r.height < 28 || s.visibility === "hidden") out.push("button too small or hidden: " + b2.textContent); }
          }
          return out;
        }, size[0] <= 700);
        problems.push(...found);
      }
      if(JSON.stringify(posts) !== JSON.stringify(sc.posts || [])) problems.push(`POSTs sent ${JSON.stringify(posts)}, scene expects ${JSON.stringify(sc.posts || [])}`);
      if(missing.length) problems.push("no fixture for " + [...new Set(missing)].join(", "));
    }catch(e){ problems.push("scene failed: " + e.message.split("\n")[0]); }
    if(!sc.clock) await page.screenshot({path: path.join(OUT, sc.name + ".png"), fullPage: true}).catch(() => {});
    console.log((problems.length ? "FAIL " : "ok   ") + sc.name + (problems.length ? "\n       " + [...new Set(problems)].join("\n       ") : ""));
    if(problems.length) failures.push(sc.name);
    await ctx.close();
  }
  await browser.close();
  console.log(`\n${todo.length - failures.length} of ${todo.length} scenes pass. Pictures: ${OUT}`);
  process.exit(failures.length ? 1 : 0);
})();
