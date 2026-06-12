#!/usr/bin/env python3
"""Codex CLI session-status hook for the QuotaHalo GNOME widget.

The Codex CLI runs this on its lifecycle hook events (SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, SubagentStop,
Stop, ...). The payload is one JSON object on stdin (same shape as Claude
Code's hooks). We record each session's state into one file per session at

    ~/.cache/quotahalo/codex-sessions/<session_id>.json

which the GNOME extension renders as a Codex status dot plus a "Codex Sessions"
list in the popup.

State machine (keyed on ``hook_event_name``):
    SessionStart                         -> awaiting_reply
    UserPromptSubmit / PreToolUse /
        PostToolUse                      -> working
    SubagentStop                         -> state unchanged (may arrive after Stop)
    PermissionRequest                    -> needs_input (awaiting your approval)
    Stop                                 -> awaiting_reply (turn finished, your turn)

Codex has no SessionEnd or idle event, so ended sessions are dropped by
reconciling against process liveness: the hook records the codex process pid
and every run drops sessions whose process has exited (plus a staleness cap).

Constraints (runs inside the user's Codex turn): pure stdlib, prints NOTHING to
stdout, always exits 0. NOTE: Codex requires command hooks to be trusted (run
`/hooks` once); keep this script stable so its trust hash does not churn.
"""

import json
import os
import re
import sys
import time
from pathlib import Path


STATUS_DIR = Path.home() / ".cache" / "quotahalo"
SESSIONS_DIR = STATUS_DIR / "codex-sessions"

# Mirrors the reader in extension.js. "working" gets the longest leash; the
# others are reaped sooner once a session has gone quiet.
STALE_SECONDS = {
    "working": 6 * 3600,
    "needs_input": 3 * 3600,
    "awaiting_reply": 2 * 3600,
    "idle": 2 * 3600,
}
DEFAULT_STALE_SECONDS = 2 * 3600
_HAS_PROC = os.path.isdir("/proc")

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_id(session_id):
    if not session_id:
        return ""
    return _SAFE_ID_RE.sub("_", str(session_id)).strip("._")


def _next_state(payload, prev_state):
    """Map a Codex hook event to the next session state."""
    event = payload.get("hook_event_name") or ""

    if event == "SessionStart":
        return "awaiting_reply"
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "working"
    if event in ("SubagentStart", "SubagentStop"):
        # A subagent finishing does not mean the main agent resumed, and it can
        # arrive after the main Stop, so never let it flip the state.
        return prev_state or "working"
    if event == "PermissionRequest":
        return "needs_input"
    if event == "Stop":
        return "awaiting_reply"
    # Compaction / unknown events: keep whatever we had.
    return prev_state or "awaiting_reply"


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write(path, record):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(record, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _ancestry():
    """Walk this hook's process tree up to the terminal.

    Returns (proc_pid, ancestor_pids): proc_pid is the nearest ``codex`` process
    (for liveness checks); ancestor_pids is the chain of pids up to the session
    manager (so the panel can focus the session's terminal window).
    """
    proc_pid = None
    chain = []
    cur = os.getpid()
    depth = 0
    while cur and cur > 1 and depth < 10:
        try:
            with open("/proc/%d/stat" % cur, "r", encoding="utf-8") as fh:
                content = fh.read()
            rparen = content.rindex(")")
            comm = content[content.index("(") + 1:rparen]
            ppid = int(content[rparen + 2:].split()[1])
        except Exception:
            break
        if comm in ("systemd", "init"):
            break
        if proc_pid is None and comm == "codex":
            proc_pid = cur
        # Skip our own transient python hook process in the focus chain.
        if comm not in ("python3", "python"):
            chain.append(cur)
        cur = ppid
        depth += 1
    return proc_pid, chain


def _prune_and_reconcile(now):
    """Drop sessions whose process has exited, then prune stale files."""
    try:
        entries = list(SESSIONS_DIR.glob("*.json"))
    except Exception:
        return
    for entry in entries:
        try:
            data = _read(entry) or {}
            if not data:
                continue
            proc_pid = data.get("proc_pid")
            if (_HAS_PROC and isinstance(proc_pid, int) and proc_pid > 1
                    and not os.path.exists("/proc/%d" % proc_pid)):
                entry.unlink(missing_ok=True)
                continue
            state = data.get("state") or "idle"
            updated = float(data.get("updated_epoch") or 0)
            limit = STALE_SECONDS.get(state, DEFAULT_STALE_SECONDS)
            if updated <= 0 or now - updated > limit:
                entry.unlink(missing_ok=True)
        except Exception:
            continue


def handle_event(payload, now=None):
    """Apply one Codex hook event. Returns the written record, or None."""
    if now is None:
        now = int(time.time())
    if not isinstance(payload, dict):
        return None

    event = payload.get("hook_event_name") or ""
    stem = _safe_id(payload.get("session_id"))
    if not stem:
        return None
    path = SESSIONS_DIR / (stem + ".json")

    prev = _read(path)
    prev_state = prev.get("state") if prev else None
    state = _next_state(payload, prev_state)

    record = dict(prev) if isinstance(prev, dict) else {}
    record["provider"] = "codex"
    record["session_id"] = payload.get("session_id")
    record["state"] = state
    record["event"] = event
    record["updated_epoch"] = now
    record.setdefault("started_epoch", now)

    cwd = payload.get("cwd")
    if cwd:
        record["cwd"] = cwd
        record["project"] = os.path.basename(os.path.normpath(cwd)) or cwd
    model = payload.get("model")
    if model:
        record["model"] = model
    transcript = payload.get("transcript_path")
    if transcript:
        record["transcript_path"] = transcript

    if event == "PreToolUse":
        record["current_tool"] = payload.get("tool_name") or ""
    elif event in ("PostToolUse", "UserPromptSubmit", "Stop"):
        record["current_tool"] = ""

    if state != "needs_input":
        record["detail"] = ""
    elif payload.get("tool_name"):
        record["detail"] = str(payload.get("tool_name"))

    if event == "SessionStart" or "ancestor_pids" not in record:
        proc_pid, ancestors = _ancestry()
        if proc_pid:
            record["proc_pid"] = proc_pid
        if ancestors:
            record["ancestor_pids"] = ancestors

    if prev_state != state or "state_since_epoch" not in record:
        record["state_since_epoch"] = now

    _write(path, record)
    _prune_and_reconcile(now)
    return record


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        handle_event(payload)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
