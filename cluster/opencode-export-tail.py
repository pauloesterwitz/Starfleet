#!/usr/bin/env python3
"""Print the last N messages of a session (opencode or Claude Code) as
compact JSON, in the same shape regardless of which tool it came from.

opencode path wraps `opencode export <id>` and strips heavy/irrelevant
fields (tool attachments, reasoning walls, structural step markers) down to
what's useful for an at-a-glance "what's going on in this session" view.

Claude Code has no export command -- there's just the JSONL transcript
itself at ~/.claude/projects/<escaped-cwd>/<id>.jsonl -- so that path parses
it directly, folding each tool_use block together with its matching
tool_result (a later "user"-type line referencing the same tool_use_id) into
one part, the same way opencode's own single "tool" part already bundles a
call with its outcome.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

OPENCODE_BIN = os.path.expanduser("~/.opencode/bin/opencode")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
MAX_TEXT_CHARS = 2000
DEFAULT_TAIL = 5


def compact_part(part):
    ptype = part.get("type")
    if ptype == "text":
        return {"type": "text", "text": part.get("text", "")[:MAX_TEXT_CHARS]}
    if ptype == "tool":
        state = part.get("state", {})
        return {"type": "tool", "tool": part.get("tool"), "status": state.get("status"), "title": state.get("title")}
    if ptype == "file":
        return {"type": "file", "filename": (part.get("filename") or part.get("url", ""))[:80]}
    return None  # skip reasoning/step-start/step-finish -- structural/verbose noise


def export_opencode_tail(session_id, n):
    # Exports can carry multi-MB embedded attachments (base64 PDFs etc). Piping
    # that through subprocess.run's captured-text mode was observed corrupting
    # the stream; writing to a real file and reading it back is reliable.
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as tmp:
        result = subprocess.run([OPENCODE_BIN, "export", session_id], stdout=tmp, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode != 0:
            return {"error": f"export failed with exit code {result.returncode}"}
        tmp.seek(0)
        raw = tmp.read()

    start = raw.find("{")
    data = json.loads(raw[start:])
    messages = data.get("messages", [])[-n:]

    out = []
    for m in messages:
        info = m.get("info", {})
        parts = [p for p in (compact_part(p) for p in m.get("parts", [])) if p]
        out.append({
            "role": info.get("role"),
            "created": info.get("time", {}).get("created"),
            "completed": info.get("time", {}).get("completed"),
            "finish": info.get("finish"),
            "parts": parts,
        })

    return {"session_id": session_id, "title": data.get("info", {}).get("title"), "messages": out}


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _find_claude_transcript(session_id):
    matches = glob.glob(os.path.join(CLAUDE_PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return matches[0] if matches else None


def export_claude_tail(session_id, n):
    path = _find_claude_transcript(session_id)
    if path is None:
        return {"error": f"no Claude Code transcript found for session {session_id}"}

    title = None
    turns = []  # each: {"role", "ts", "finish", "parts": [...]}
    tool_use_index = {}  # tool_use_id -> the part dict, so a later result can fill it in

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
            ts = _parse_ts(d.get("timestamp"))

            if t == "ai-title":
                title = d.get("aiTitle") or title
                continue

            if t == "assistant":
                msg = d.get("message", {})
                parts = []
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        parts.append({"type": "text", "text": (block.get("text") or "")[:MAX_TEXT_CHARS]})
                    elif btype == "tool_use":
                        inp = block.get("input") or {}
                        # Prefer a short human-written summary of the CALL over the
                        # result text (filled in below if none of these exist) --
                        # "description" is Bash's own one-line explanation of the
                        # command; the rest are the closest equivalent for other
                        # built-in tools (Read/Write/Edit/Glob/Grep-shaped inputs).
                        title = (inp.get("description") or inp.get("command") or inp.get("file_path")
                                 or inp.get("path") or inp.get("pattern") or inp.get("query"))
                        title = " ".join(title.split())[:80] if isinstance(title, str) else None
                        part = {"type": "tool", "tool": block.get("name"), "status": "pending", "title": title}
                        tool_use_index[block.get("id")] = part
                        parts.append(part)
                    # thinking/redacted_thinking blocks skipped -- same "reasoning is
                    # noise for an at-a-glance view" call as opencode's compact_part.
                if parts:
                    turns.append({"role": "assistant", "ts": ts, "finish": msg.get("stop_reason"), "parts": parts})
                continue

            if t == "user":
                if d.get("isMeta"):
                    continue  # Claude Code's own injected wrapper text, not a real turn
                content = d.get("message", {}).get("content")
                if isinstance(content, str):
                    if not content.lstrip().startswith(("<command-name>", "<local-command")):
                        turns.append({"role": "user", "ts": ts, "finish": None,
                                      "parts": [{"type": "text", "text": content[:MAX_TEXT_CHARS]}]})
                elif isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text_parts.append({"type": "text", "text": (block.get("text") or "")[:MAX_TEXT_CHARS]})
                        elif block.get("type") == "tool_result":
                            # Fold into the matching tool_use part instead of becoming
                            # its own turn -- this line exists only to report that
                            # call's outcome, same as opencode's single "tool" part.
                            part = tool_use_index.get(block.get("tool_use_id"))
                            if part is not None:
                                part["status"] = "error" if block.get("is_error") else "completed"
                                if not part.get("title"):
                                    # No usable input-derived summary (e.g. a tool whose
                                    # input has none of the fields checked above) -- fall
                                    # back to a one-line preview of what it returned.
                                    result_content = block.get("content")
                                    if isinstance(result_content, list):
                                        result_content = next(
                                            (b.get("text") for b in result_content
                                             if isinstance(b, dict) and b.get("type") == "text"), None)
                                    if isinstance(result_content, str):
                                        part["title"] = " ".join(result_content.split())[:80]
                    if text_parts:
                        turns.append({"role": "user", "ts": ts, "finish": None, "parts": text_parts})
                continue

    tail = turns[-n:] if n else turns
    out = [
        {"role": t["role"], "created": t["ts"], "completed": t["ts"], "finish": t["finish"], "parts": t["parts"]}
        for t in tail
    ]
    return {"session_id": session_id, "title": title, "messages": out}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: opencode-export-tail.py <session_id> [n] [tool]"}))
        sys.exit(1)
    session_id = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_TAIL
    tool = sys.argv[3] if len(sys.argv) > 3 else "opencode"

    if tool == "claude-code":
        result = export_claude_tail(session_id, n)
    else:
        result = export_opencode_tail(session_id, n)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
