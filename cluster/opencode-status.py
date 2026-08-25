#!/usr/bin/env python3
"""Emit one JSON blob: DGX Spark host stats + opencode session states.

Read-only: touches /proc, nvidia-smi, and opencode.db via a read-only
SQLite URI connection. Intended to be invoked over SSH by a remote monitor.
"""
import glob
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
LLAMA_SWAP_URL = "http://127.0.0.1:28080/running"
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
STALE_SECS = 90
RECENT_WINDOW_SECS = 24 * 3600
CPU_SAMPLE_GAP = 0.2


def read_meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            info[key] = int(val.strip().split()[0])  # kB
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info["MemFree"])
    used = total - avail
    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    return {
        "ram_used_gb": round(used / 1048576, 1),
        "ram_total_gb": round(total / 1048576, 1),
        "ram_pct": round(100 * used / total, 1) if total else 0,
        "swap_used_gb": round(swap_used / 1048576, 1),
        "swap_total_gb": round(swap_total / 1048576, 1),
    }


def read_gpu():
    # ponytail: GB10 has unified memory, no separate VRAM to query -- system RAM covers it.
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        util, power, temp = (p.strip() for p in out.stdout.strip().split(","))
        return {"gpu_util_pct": float(util), "gpu_power_w": float(power), "gpu_temp_c": float(temp)}
    except Exception:
        return {"gpu_util_pct": None, "gpu_power_w": None, "gpu_temp_c": None}


LLAMA_SWAP_CONFIG_PATH = os.path.expanduser("~/llama-swap/config.yaml")
# Routing groups whose members occupy BOTH Sparks at once (TP=2 vLLM+Ray
# clusters). llama-swap's own config schema has no "spans multiple machines"
# field -- that's an architecture fact about what each member's cmd script
# does (serve-starfleet.sh / the ds4-tp2 systemd unit), not something
# llama-swap tracks -- so this reads the routing groups Paul already
# organizes these into, which line up with that reality today.
CLUSTER_GROUP_NAMES = {"starfleet", "ds4-tp2"}


def read_cluster_model_ids():
    """Model keys currently in a both-Sparks routing group, freshly re-read
    from config.yaml every call -- so a model Paul adds to the `starfleet`
    group later is picked up automatically, with nothing to redeploy here.
    """
    try:
        import yaml  # local import: a host without llama-swap may lack PyYAML
        with open(LLAMA_SWAP_CONFIG_PATH) as f:
            config = yaml.safe_load(f)
        groups = config.get("routing", {}).get("router", {}).get("settings", {}).get("groups", {})
        ids = set()
        for name in CLUSTER_GROUP_NAMES:
            ids.update(groups.get(name, {}).get("members", []))
        return ids
    except Exception:
        return set()


def read_llama_swap():
    """Models llama-swap currently has loaded on THIS host, via its own
    /running endpoint -- not /v1/models, which lists every configured model
    (the whole ~28-model catalog) regardless of whether it's actually running.
    Not every Starfleet member runs llama-swap (Kathryn doesn't today) --
    connection refused there is a normal, silent "nothing to report" case,
    not a failure worth surfacing as an error.

    Each entry is also tagged `cluster`: true if it's a TP=2 both-Sparks
    member -- llama-swap itself only runs on one host, so its /running report
    is the only source for these, but the Mac app displays a cluster-tagged
    model under EVERY Starfleet member it actually occupies, not just the one
    whose llama-swap happened to answer.
    """
    try:
        with urllib.request.urlopen(LLAMA_SWAP_URL, timeout=2) as resp:
            data = json.loads(resp.read())
        cluster_ids = read_cluster_model_ids()
        models = [
            {"model": m.get("model"), "state": m.get("state"), "cluster": m.get("model") in cluster_ids}
            for m in data.get("running", [])
        ]
        return {"reachable": True, "models": models}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {"reachable": False, "models": []}


def fetch_generating_flags(cur, sessions):
    """session_id -> is its latest message an assistant reply still being generated.

    cpu_pct (see match_pid()) is an unreliable "is this session active right
    now" signal -- it falls back to attributing ANY opencode process in a
    session's directory, so idle sessions sharing a directory with an active
    one inherit its CPU. The message table's `data` JSON has a `time` object
    with `completed` added only once a message finishes, so "latest message
    is from the assistant and has no `completed` yet" is ground truth for
    "still generating a response" -- unlike a dangling last *user* message
    (no reply yet, a different state), which also lacks `completed` but is
    correctly excluded by the role check.
    """
    if not sessions:
        return {}
    ids = [s["id"] for s in sessions]
    cur.execute(
        "SELECT s.id, m.data FROM session s "
        "JOIN message m ON m.id = ("
        "  SELECT id FROM message WHERE session_id = s.id "
        "  ORDER BY time_created DESC LIMIT 1"
        ") WHERE s.id IN ({})".format(",".join("?" * len(ids))),
        ids,
    )
    generating = {}
    for sid, data in cur.fetchall():
        try:
            d = json.loads(data)
            generating[sid] = d.get("role") == "assistant" and "completed" not in d.get("time", {})
        except (json.JSONDecodeError, TypeError):
            generating[sid] = False  # fail safe: never show a false positive
    return generating


def query_sessions_and_todos():
    # Note: intentionally NOT a `mode=ro` URI connection -- a strict read-only
    # connection can't fully participate in WAL's shared-memory index and was
    # observed returning a stale/incomplete view of the todo table (a session
    # with real todo rows read back as all-zero). A plain connection that only
    # ever issues SELECTs is safe and sees the WAL correctly.
    last_err = None
    for _ in range(3):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=2)
            try:
                cur = conn.cursor()
                cutoff = int((time.time() - RECENT_WINDOW_SECS) * 1000)
                cur.execute(
                    "SELECT id, title, directory, time_updated FROM session "
                    "WHERE time_updated >= ? AND time_archived IS NULL "
                    "ORDER BY time_updated DESC",
                    (cutoff,),
                )
                sessions = [
                    {"id": r[0], "title": r[1], "directory": r[2], "time_updated": r[3]}
                    for r in cur.fetchall()
                ]
                cur.execute("SELECT session_id, status, COUNT(*) FROM todo GROUP BY session_id, status")
                todo_counts = {}
                for sid, status, count in cur.fetchall():
                    todo_counts.setdefault(sid, {})[status] = count
                generating = fetch_generating_flags(cur, sessions)
                return sessions, todo_counts, generating
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(0.15)
    raise last_err


def build_proc_map():
    """pid -> {ppid, cwd, cmdline, utime, stime}, best-effort (processes can vanish mid-read)."""
    procs = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        base = f"/proc/{entry}"
        try:
            with open(f"{base}/stat") as f:
                stat = f.read()
            rest = stat[stat.rindex(")") + 2:].split()
            ppid, utime, stime = int(rest[1]), int(rest[11]), int(rest[12])
            cwd = os.readlink(f"{base}/cwd")
            with open(f"{base}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
        procs[pid] = {"ppid": ppid, "cwd": cwd, "cmdline": cmdline, "utime": utime, "stime": stime}
    return procs


def descendants(procs, root_pid):
    children = {}
    for pid, info in procs.items():
        children.setdefault(info["ppid"], []).append(pid)
    out, stack = [], [root_pid]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def match_pid(procs, session):
    for pid, info in procs.items():
        if session["id"] in info["cmdline"]:
            return pid
    directory = os.path.realpath(session["directory"])
    candidates = [
        pid for pid, info in procs.items()
        if info["cmdline"].split(" ", 1)[0].endswith("opencode")
        and os.path.realpath(info["cwd"]) == directory
    ]
    # most-recently-seen PID wins ties (higher PID ~ started later on Linux)
    return max(candidates) if candidates else None


def cpu_pct_for(before, after, pid):
    pids = descendants(before, pid)
    used_before = sum(before[p]["utime"] + before[p]["stime"] for p in pids if p in before)
    used_after = sum(after[p]["utime"] + after[p]["stime"] for p in pids if p in after)
    delta_ticks = max(used_after - used_before, 0)
    return round(100 * delta_ticks / (CLOCK_TICKS * CPU_SAMPLE_GAP), 1)


def classify(session, todo_counts, cpu_pct, now, pid_found):
    updated_secs_ago = now - session["time_updated"] / 1000
    counts = todo_counts.get(session["id"], {})
    open_todos = counts.get("pending", 0) + counts.get("in_progress", 0)
    if not pid_found:
        return "unknown", False
    if cpu_pct > 5 or updated_secs_ago < 20:
        return "working", False
    if open_todos == 0:
        return "idle", False
    if updated_secs_ago > STALE_SECS:
        return "waiting", True
    return "working", False


def parse_claude_session(path, now):
    """One Claude Code JSONL transcript -> a session dict, or None if stale/unreadable.

    Unlike opencode, there's no separate DB: each line of the transcript is a
    JSON event, and file mtime IS the last-write time (confirmed live: mtime
    advanced within ~0.1s of a session's last appended line). There's also no
    PID to correlate (Claude Code sessions run through a remote server/bridge
    process, not a single directly-attributable CLI process per session like
    opencode), so `pid`/`cpu_pct` are always null/0 here -- recency is the
    only "is this active" signal available.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    updated_secs_ago = now - mtime
    if updated_secs_ago > RECENT_WINDOW_SECS:
        return None

    session_id = os.path.splitext(os.path.basename(path))[0]
    title = None
    directory = None
    first_user_text = None
    last_type = None
    last_stop_reason = None
    todos = None

    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if directory is None and "cwd" in d:
                    directory = d["cwd"]
                if t == "ai-title":
                    # Re-emitted periodically through the file (same text each
                    # time in practice) -- keep the latest, in case a session
                    # ever gets its title revised mid-conversation.
                    title = d.get("aiTitle") or title
                elif t == "user" and first_user_text is None and not d.get("isMeta"):
                    # isMeta marks Claude Code's own synthetic wrapper messages (e.g.
                    # the "<local-command-caveat>..." notice injected around slash
                    # commands) -- real human turns have isMeta absent/false, so
                    # skipping these keeps the title fallback from ever surfacing
                    # internal plumbing text instead of what the user actually typed.
                    content = d.get("message", {}).get("content")
                    text = None
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text")
                                break
                    # Slash-command invocations round-trip through the transcript as
                    # plain "user" text too (e.g. "<command-name>/model</command-name>"),
                    # not just via isMeta -- same "not really what the person typed"
                    # category, so keep scanning for a line with real free text instead.
                    if text and not text.lstrip().startswith(("<command-name>", "<local-command")):
                        first_user_text = text
                elif t == "assistant":
                    msg = d.get("message", {})
                    last_stop_reason = msg.get("stop_reason")
                    for block in msg.get("content") or []:
                        if (isinstance(block, dict) and block.get("type") == "tool_use"
                                and block.get("name") == "TodoWrite"):
                            items = block.get("input", {}).get("todos", [])
                            counts = {"pending": 0, "in_progress": 0, "completed": 0}
                            for item in items:
                                s = item.get("status")
                                if s in counts:
                                    counts[s] += 1
                            todos = counts  # keep latest TodoWrite call, not the first
                if t is not None:
                    last_type = t
    except OSError:
        return None

    if title is None:
        # No ai-title line yet -- only realistic on a session whose very
        # first turn hasn't round-tripped through the (async) title call.
        title = (first_user_text or "(untitled)").strip()[:120]
    if todos is None:
        # TodoWrite isn't used in every session (unlike opencode, where the
        # todo table is core to the app) -- absence just means no todo signal,
        # not zero todos as a fact; render the same as "no open todos" either way.
        todos = {"pending": 0, "in_progress": 0, "completed": 0}

    # Ground truth for "is a response literally in flight" doesn't exist in
    # this file the way opencode's message.time.completed does -- every
    # assistant line is only ever written once fully formed, `stop_reason`
    # included. Recency is the best available proxy instead (mirrors
    # opencode's own `updated_secs_ago < 20` "working" threshold below).
    generating = updated_secs_ago < 20
    open_todos = todos["pending"] + todos["in_progress"]
    if generating:
        status, stalled = "working", False
    elif last_type == "assistant" and last_stop_reason == "tool_use" and updated_secs_ago > STALE_SECS:
        # Last thing written was a tool call with no follow-up turn since --
        # likely sitting on a permission prompt (opencode's `working` state
        # has no equivalent go-between; this maps closest to its `waiting`).
        status, stalled = "waiting", True
    elif open_todos > 0 and updated_secs_ago > STALE_SECS:
        status, stalled = "waiting", True
    else:
        status, stalled = "idle", False

    return {
        "id": session_id,
        "title": title,
        "directory": directory,
        "updated_secs_ago": round(updated_secs_ago),
        "todos": todos,
        "pid": None,
        "cpu_pct": 0.0,
        "status": status,
        "stalled": stalled,
        "generating": generating,
        "tool": "claude-code",
    }


def claude_code_sessions(now):
    sessions = []
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, "*", "*.jsonl")):
        s = parse_claude_session(path, now)
        if s is not None:
            sessions.append(s)
    return sessions


def main():
    now = time.time()
    host = {**read_meminfo(), **read_gpu(), "llama_swap": read_llama_swap()}
    try:
        sessions, todo_counts, generating = query_sessions_and_todos()
    except Exception as e:
        print(json.dumps({"host": host, "sessions": [], "error": str(e)}))
        return

    before = build_proc_map()
    time.sleep(CPU_SAMPLE_GAP)
    after = build_proc_map()

    out_sessions = []
    for s in sessions:
        pid = match_pid(after, s)
        cpu_pct = cpu_pct_for(before, after, pid) if pid else 0.0
        counts = todo_counts.get(s["id"], {})
        status, stalled = classify(s, todo_counts, cpu_pct, now, pid is not None)
        out_sessions.append({
            "id": s["id"],
            "title": s["title"],
            "directory": s["directory"],
            "updated_secs_ago": round(now - s["time_updated"] / 1000),
            "todos": {
                "pending": counts.get("pending", 0),
                "in_progress": counts.get("in_progress", 0),
                "completed": counts.get("completed", 0),
            },
            "pid": pid,
            "cpu_pct": cpu_pct,
            "status": status,
            "stalled": stalled,
            "generating": generating.get(s["id"], False),
            "tool": "opencode",
        })

    try:
        out_sessions.extend(claude_code_sessions(now))
    except Exception:
        pass  # best-effort -- an opencode-only host must not lose its own sessions over this

    print(json.dumps({"host": host, "sessions": out_sessions}))


if __name__ == "__main__":
    main()
