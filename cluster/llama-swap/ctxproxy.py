#!/usr/bin/env python3
"""ctxproxy.py: per-request context size in front of llama-swap.

    other app  ->  127.0.0.1:28081 (this)  ->  llama-swap :28080  ->  vLLM

Ask for a context size by suffixing the model name with @<tokens>:

    curl http://127.0.0.1:28081/v1/chat/completions -H 'content-type: application/json' \
      -d '{"model":"qwen3.6-35b-57tps-mtp4-jean-luc@32768","messages":[...]}'

or keep the plain model name and send the header  X-Context-Size: 32768  instead
(for clients that validate model names, e.g. some SDK wrappers).

How it works: the size is written to $GB10_STATE_DIR/ctx/<member> (default
~/.gb10/ctx/<member>). ctx-env.sh, which prefixes every wired member's cmd, reads it
at launch and exports MML_OVERRIDE / MAX_MODEL_LEN, which the fleet launchers already
honour. If the member is loaded at a different size it is unloaded first so llama-swap
brings it back at the requested one.

Requests without a size pass through untouched, and :28080 keeps working exactly as
before: nothing is required to go through this proxy.

COST: vLLM allocates its KV pool at startup, so CHANGING the context restarts the
model. That is a cold load (minutes for the big members), not a free parameter. Asking
for the same size as last time restarts nothing. Pick a few sizes and stay on them.

BUDGET: --gpu-memory-utilization and memcheck's need_mb are NOT recomputed. A larger
context buys KV out of the same fixed pool; if it does not fit, vLLM fails at startup
with a clean error instead of eating the box.

Run:        ./ctxproxy.py            (or the llama-swap-ctxproxy.service user unit)
Self-check: ./ctxproxy.py --selftest
"""
import hashlib
import hmac
import http.client
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ponytail: llama-swap listens on the private :28079 (only Starfleet Command, fleet-ui, talks to it
# directly); every other client reaches it through here on :28080/:28081 with the guard always on.
UPSTREAM = ("127.0.0.1", 28079)
LISTEN = [("127.0.0.1", 28080), ("127.0.0.1", 28081)]
LS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(LS_DIR, "config.yaml")
CTX_DIR = os.path.join(os.environ.get("GB10_STATE_DIR") or os.path.expanduser("~/.gb10"), "ctx")

# Same bounds ctx-env.sh enforces on the other side of the file.
MIN_CTX, MAX_CTX = 256, 1048576

POLICY = "Only Starfleet Command may load models and Agents may not load models on their own. This is a policy!"
# Only the SHA-256 of the Starfleet key is stored (starfleet-keygen.py); the key itself is shown once.
KEY_HASH_FILE = os.path.expanduser("~/.config/starfleet/key.sha256")
# Internal secret that llama-swap's apiKeys expects; only this proxy holds it (EnvironmentFile).
PROXY_KEY = os.environ.get("PROXY_KEY", "")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade"}

_lock = threading.Lock()


def split_model(name):
    """'member@32768' -> ('member', 32768); no suffix -> (name, None); bad suffix -> ValueError."""
    if "@" not in name:
        return name, None
    member, _, raw = name.rpartition("@")
    if not member:
        raise ValueError("empty model name before '@'")
    if not raw.isdigit():
        raise ValueError("context size must be a positive integer, got %r" % raw)
    ctx = int(raw)
    if not MIN_CTX <= ctx <= MAX_CTX:
        raise ValueError("context size %d is outside %d..%d" % (ctx, MIN_CTX, MAX_CTX))
    return member, ctx


def wired_members(path=CONFIG):
    """Members whose cmd runs through ctx-env.sh: the only ones a context size can reach.

    Read fresh on every sized request so a member added to config.yaml works without
    restarting this proxy (llama-swap itself picks it up via -watch-config).
    """
    found, current = set(), None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            key = re.match(r"^  ([A-Za-z0-9._:-]+):\s*$", line)
            if key:
                current = key.group(1)
            elif current and line.startswith("    cmd:") and "ctx-env.sh" in line:
                found.add(current)
    return found


def load_groups(path=CONFIG):
    """-> ({group: {swap, exclusive, members}}, {alias: member}) read fresh from config.yaml."""
    groups, aliases, grp, model, in_alias, in_groups = {}, {}, None, None, False, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.split(" #", 1)[0].rstrip()
            if re.match(r"^      groups:\s*$", line):
                in_groups = True
            elif in_groups and (m := re.match(r"^        ([A-Za-z0-9._:-]+):\s*$", line)):
                grp = groups.setdefault(m.group(1), {"swap": False, "exclusive": False, "members": []})
            elif in_groups and grp and (m := re.match(r"^          (swap|exclusive):\s*(true|false)", line)):
                grp[m.group(1)] = m.group(2) == "true"
            elif in_groups and grp and (m := re.match(r"^            - ([A-Za-z0-9._:-]+)\s*$", line)):
                grp["members"].append(m.group(1))
            elif not in_groups and (m := re.match(r"^  ([A-Za-z0-9._:-]+):\s*$", line)):
                model, in_alias = m.group(1), False
            elif not in_groups and model and re.match(r"^    aliases:\s*$", line):
                in_alias = True
            elif not in_groups and in_alias and (m := re.match(r"^      - ([A-Za-z0-9._:-]+)\s*$", line)):
                aliases[m.group(1)] = model
            elif not in_groups and in_alias and not line.startswith("      -"):
                in_alias = False
    return groups, aliases


def would_evict(requested, running, groups, aliases):
    """Running models that loading `requested` makes llama-swap unload (config.yaml semantics:
    swap:true evicts same-group peers; exclusive:true evicts running models of other exclusive groups).
    """
    member = aliases.get(requested, requested)
    mine = next((g for g in groups.values() if member in g["members"]), None)
    if mine is None or member in running:
        return []
    out = []
    for g in groups.values():
        hit = [m for m in g["members"] if m in running and m != member]
        if g is mine and g["swap"]:
            out += hit
        elif g is not mine and mine["exclusive"] and g["exclusive"]:
            out += hit
    return out


def starfleet_ok(presented, path=KEY_HASH_FILE):
    """True when `presented` hashes to the stored Starfleet key hash (read fresh: rotation needs no restart)."""
    try:
        with open(path) as fh:
            want = fh.read().strip()
    except OSError:
        return False
    got = hashlib.sha256((presented or "").encode()).hexdigest()
    return bool(presented) and hmac.compare_digest(got, want)


def _auth():
    return {"Authorization": "Bearer " + PROXY_KEY} if PROXY_KEY else {}


def _upstream_json(method, path, timeout):
    conn = http.client.HTTPConnection(*UPSTREAM, timeout=timeout)
    try:
        conn.request(method, path, headers=_auth())
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, (json.loads(body) if body.strip().startswith(b"{") else None)
    finally:
        conn.close()


def _is_running(member):
    try:
        _, data = _upstream_json("GET", "/running", timeout=10)
    except Exception as exc:                       # llama-swap down or slow
        sys.stderr.write("ctxproxy: /running failed (%s), assuming %s is loaded\n" % (exc, member))
        return True                                # unloading a stopped model is harmless; the
                                                   # reverse (serving a stale context) is not
    for entry in (data or {}).get("running") or []:
        if (entry.get("model") if isinstance(entry, dict) else entry) == member:
            return True
    return False


def recorded_ctx(member, ctx_dir=CTX_DIR):
    try:
        with open(os.path.join(ctx_dir, member)) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def apply_ctx(member, ctx, ctx_dir=CTX_DIR):
    """Record the requested size; unload the member if it is loaded at a different one.

    Returns True when a reload was triggered.
    """
    path = os.path.join(ctx_dir, member)
    # ponytail: one global lock, so two callers cannot interleave write and unload. Callers
    # asking for DIFFERENT sizes at the same time still thrash (last writer wins) since each
    # reload is minutes long; add per-member queuing only if that turns out to happen.
    with _lock:
        try:
            with open(path) as fh:
                current = fh.read().strip()
        except OSError:
            current = ""
        if current == str(ctx):
            return False
        os.makedirs(ctx_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("%d\n" % ctx)
        os.replace(tmp, path)                      # atomic: ctx-env.sh never reads a half file
        if not _is_running(member):
            return False
        sys.stderr.write("ctxproxy: %s -> ctx %d, unloading to reload\n" % (member, ctx))
        _upstream_json("POST", "/api/models/unload/%s" % member, timeout=300)
        return True


class EvictionRefused(Exception):
    pass


UNLOAD_PATHS = ("/unload", "/api/models/unload", "/models/unload")


def path_refusal(path, running, groups, aliases):
    """Message when this path would load or unload a model, else None (/upstream/<m>/ loads <m>)."""
    path = path.split("?", 1)[0]
    if path.rstrip("/") in UNLOAD_PATHS or path.startswith(tuple(p + "/" for p in UNLOAD_PATHS)):
        return "refused: only Starfleet Command (:8090) unloads models."
    m = re.match(r"^/upstream/([^/]+)", path)
    if m:
        member = aliases.get(m.group(1), m.group(1))
        if member not in running and any(member in g["members"] for g in groups.values()):
            return "refused: %s is not loaded. Only Starfleet Command (:8090) loads models." % member
    return None


class Handler(BaseHTTPRequestHandler):
    # ponytail: HTTP/1.0 + Connection: close frames the body by closing the socket, so streamed
    # SSE passes through without re-chunking it. Costs keep-alive, which is noise next to a
    # multi-second completion.
    protocol_version = "HTTP/1.0"
    server_version = "ctxproxy"

    def log_message(self, fmt, *args):
        sys.stderr.write("ctxproxy %s %s\n" % (self.log_date_time_string(), fmt % args))

    def _fail(self, code, message):
        body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _rewrite(self, body, starfleet=False):
        """Strip the size off the model name, apply it, return the body llama-swap should see."""
        header_ctx = self.headers.get("X-Context-Size")
        if not body.strip().startswith(b"{"):
            return body
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            return body

        member, ctx = split_model(payload["model"])
        if ctx is None and header_ctx:
            _, ctx = split_model("x@" + header_ctx.strip())
        if ctx is None:
            return body
        if member not in wired_members():
            raise ValueError(
                "model %r does not support a dynamic context size (its cmd in config.yaml is not "
                "wired through ctx-env.sh)" % member)

        if not starfleet and recorded_ctx(member) != str(ctx):
            raise EvictionRefused("refused: changing %s to ctx %d reloads it. Only Starfleet Command "
                                  "(:8090) loads or unloads models." % (member, ctx))
        apply_ctx(member, ctx)
        payload["model"] = member
        return json.dumps(payload).encode()

    def _guard(self, body):
        """Always on: refuse any request that would load or unload a model (only Starfleet Command does).

        Fail open if llama-swap/config can't be read: a broken guard must not block all work.
        """
        try:
            groups, aliases = load_groups()
            _, data = _upstream_json("GET", "/running", timeout=10)
            running = {e["model"] for e in (data or {}).get("running") or []}
            why = path_refusal(self.path, running, groups, aliases)
            if why:
                raise EvictionRefused(why)
            if self.command != "POST":
                return
            model = json.loads(body).get("model")
            if not isinstance(model, str):
                return
        except (ValueError, AttributeError, OSError, KeyError):
            return
        member = aliases.get(split_model(model)[0], split_model(model)[0])
        if member in running or not any(member in g["members"] for g in groups.values()):
            return
        victims = would_evict(member, running, groups, aliases)
        raise EvictionRefused("refused: %s is not loaded. Only Starfleet Command (:8090) loads or "
                              "unloads models%s." % (model, "; it would evict " + ", ".join(victims) if victims else ""))

    def _relay(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        starfleet = starfleet_ok(self.headers.get("X-Starfleet-Key"))
        try:
            if not starfleet:
                self._guard(body)
            body = self._rewrite(body, starfleet)
        except ValueError as exc:
            return self._fail(400, str(exc))
        except EvictionRefused as exc:
            sys.stderr.write("ctxproxy: %s\n" % exc)
            return self._fail(403, POLICY)

        secret = {"x-starfleet-key", "authorization", "x-api-key"}   # client creds never reach llama-swap
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP | secret}
        headers.update(_auth())
        headers.pop("X-Context-Size", None)
        headers.pop("X-No-Evict", None)
        headers["Content-Length"] = str(len(body))
        # No timeout: a context change means a cold load, and llama-swap holds the request until
        # the model is ready (healthCheckTimeout 2400 in config.yaml).
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=None)
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                                   # client hung up mid-stream
        except OSError as exc:
            self._fail(502, "llama-swap upstream unreachable: %s" % exc)
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _relay


def selftest():
    import tempfile
    assert split_model("qwen3.6-35b-57tps-mtp4-jean-luc") == ("qwen3.6-35b-57tps-mtp4-jean-luc", None)
    assert split_model("gemma4:26b@32768") == ("gemma4:26b", 32768)
    for bad in ("m@", "m@abc", "m@0", "m@99", "m@2000000", "@4096"):
        try:
            split_model(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("accepted bad model name %r" % bad)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("timeouts:\n  idle: 0\nmodels:\n"
                 "  wired-one:\n    cmd: /x/ctx-env.sh wired-one /x/serve.sh --port ${PORT}\n"
                 "  plain-one:\n    cmd: /x/serve.sh --port ${PORT}\n"
                 "routing:\n  router:\n    use: group\n")
        cfg = fh.name
    assert wired_members(cfg) == {"wired-one"}, wired_members(cfg)
    os.unlink(cfg)

    globals()["_is_running"] = lambda member: False          # selftest must not touch a live llama-swap
    with tempfile.TemporaryDirectory() as tmp:
        assert apply_ctx("m", 32768, tmp) is False          # not loaded: write only, no unload
        assert open(os.path.join(tmp, "m")).read().strip() == "32768"
        assert apply_ctx("m", 32768, tmp) is False          # unchanged: no restart
    g = {"big": {"swap": True, "exclusive": True, "members": ["ds4"]},
         "small": {"swap": False, "exclusive": True, "members": ["gem"]},
         "fleet": {"swap": True, "exclusive": False, "members": ["ornith", "qwen38"]}}
    al = {"orn": "ornith"}
    assert would_evict("qwen38", {"ornith"}, g, al) == ["ornith"]       # starfleet swap
    assert would_evict("orn", {"ornith"}, g, al) == []                  # already loaded
    assert would_evict("gem", {"ornith"}, g, al) == []                  # exclusive:false is safe
    assert would_evict("gem", {"ds4"}, g, al) == ["ds4"]                # exclusive vs exclusive
    assert would_evict("unknown", {"ornith"}, g, al) == []
    run = {"ornith"}
    assert path_refusal("/upstream/qwen38/health", run, g, al)          # would load qwen38
    assert path_refusal("/upstream/ornith/health", run, g, al) is None  # already loaded
    assert path_refusal("/api/models/unload/ornith", run, g, al)
    assert path_refusal("/unload?model=x", run, g, al)
    assert path_refusal("/running", run, g, al) is None
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write(hashlib.sha256(b"right-key").hexdigest() + "\n")
    assert starfleet_ok("right-key", fh.name)
    assert not starfleet_ok("wrong-key", fh.name) and not starfleet_ok("", fh.name) and not starfleet_ok(None, fh.name)
    assert not starfleet_ok("right-key", fh.name + ".missing")      # no hash file: nobody is Starfleet
    os.unlink(fh.name)
    print("ctxproxy selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    os.makedirs(CTX_DIR, exist_ok=True)
    srvs = [ThreadingHTTPServer(addr, Handler) for addr in LISTEN]
    for srv in srvs:
        srv.daemon_threads = True
    sys.stderr.write("ctxproxy: %s:%d -> llama-swap %s:%d, state %s\n"
                     % (LISTEN[0] + UPSTREAM + (CTX_DIR,)))
    for srv in srvs[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    srvs[0].serve_forever()
