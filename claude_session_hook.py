#!/usr/bin/env python3
"""Claude Code session-status hook for the QuotaHalo GNOME widget.

Claude Code invokes this script on session lifecycle events (SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse, SubagentStop, Notification, Stop,
SessionEnd). The hook reads the event payload from stdin and records the
session's current state into one file per session at

    ~/.cache/quotahalo/sessions/<session_id>.json

The GNOME extension polls that directory and renders the aggregate state as a
dot on the usage pill plus a per-session list in the popup.

State machine (keyed on ``hook_event_name``):
    SessionStart                         -> awaiting_reply (records cwd/model/started)
    UserPromptSubmit / PreToolUse /
        PostToolUse                      -> working
    SubagentStop                         -> state unchanged (may arrive after Stop)
    Notification (permission or question)-> needs_input (blocked on the user)
    Notification (idle prompt)           -> idle         (finished, user away)
    Stop                                 -> awaiting_reply (turn finished, your turn)
    SessionEnd                           -> the session file is deleted

The session label is its Claude Code title — the ``name`` in
~/.claude/sessions/<pid>.json, else the latest ``aiTitle`` in the transcript
(generated right after the first prompt), else the cwd basename.

Because hook events can be missed (e.g. a session killed mid-turn, or a Stop
that never lands), every run also reconciles cached sessions against Claude
Code's own ~/.claude/sessions metadata: a session whose process has exited is
dropped, and one we still show as "working" while Claude reports it idle is
corrected to awaiting_reply/idle.

Design constraints (this runs inside the user's Claude Code turn):
    * pure standard library, fast cold-start (registered under /usr/bin/python3);
    * prints NOTHING to stdout -- on UserPromptSubmit/SessionStart any non-JSON
      stdout is injected into the model context;
    * always exits 0, swallowing every error, so it can never break a session.

Manual test:
    echo '{"hook_event_name":"SessionStart","session_id":"t1","cwd":"/tmp/x"}' \
        | python3 claude_session_hook.py
"""

import json
import os
import re
import sys
import time
from pathlib import Path


STATUS_DIR = Path.home() / ".cache" / "quotahalo"
SESSIONS_DIR = STATUS_DIR / "sessions"
# Claude Code keeps per-process session metadata here (one file per pid); we
# read its AI-generated ``name`` as the session title when available.
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# A session file is considered dead (and pruned) once it has not received any
# event for this many seconds. "working" gets the longest leash because a long
# tool run can legitimately stay quiet; an unanswered prompt or an idle session
# is safe to reap sooner. Kept in sync with the reader in extension.js.
STALE_SECONDS = {
    "working": 6 * 3600,
    "needs_input": 3 * 3600,
    "awaiting_reply": 2 * 3600,
    "idle": 2 * 3600,
}
DEFAULT_STALE_SECONDS = 2 * 3600

# Reconciliation against Claude Code's own session metadata. We wait a few
# seconds before trusting an "idle" reading to avoid the turn-start race (Claude
# flips to "busy" a moment after a prompt is submitted), and treat a session
# idle longer than this as "away".
RECONCILE_IDLE_GRACE = 10
AWAY_AFTER_SECONDS = 60
_HAS_PROC = os.path.isdir("/proc")

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")

# Optional event tracing: appends every handled event to hook-events.log, but
# only while the sentinel file hook-debug.enabled exists (off by default, cheap).
DEBUG_LOG_FILE = STATUS_DIR / "hook-events.log"
DEBUG_FLAG_FILE = STATUS_DIR / "hook-debug.enabled"


def _debug_log(line):
    try:
        if not DEBUG_FLAG_FILE.exists():
            return
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _safe_id(session_id):
    """Return a filesystem-safe stem for a session id (or '' if unusable)."""
    if not session_id:
        return ""
    return _SAFE_ID_RE.sub("_", str(session_id)).strip("._")


def _next_state(payload, prev_state):
    """Map a hook event to the next session state.

    ``prev_state`` is the state currently on disk (or None for a new session).
    Returns one of: "working", "awaiting_reply", "needs_input", "idle", "ended".
    """
    event = payload.get("hook_event_name") or ""

    if event == "SessionStart":
        return "awaiting_reply"
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "working"
    if event in ("SubagentStart", "SubagentStop"):
        # A subagent finishing does NOT mean the main agent resumed. These can
        # arrive seconds-to-minutes AFTER the main Stop (background subagents),
        # so they must never flip the state back to working.
        return prev_state or "working"
    if event == "Stop":
        return "awaiting_reply"
    if event == "SessionEnd":
        return "ended"
    if event == "Notification":
        ntype = str(payload.get("notification_type") or "").lower()
        msg = str(payload.get("message") or "").lower()
        # Blocked on the user: a permission prompt or a question (elicitation).
        if "permission" in ntype or "permission" in msg:
            return "needs_input"
        if "elicitation" in ntype and "complete" not in ntype and "response" not in ntype:
            return "needs_input"
        # Finished and the user has been away for a while.
        if "idle" in ntype or "waiting for your" in msg:
            return "idle"
        # Unknown notification (auth, elicitation result, ...): keep current state.
        return prev_state or "awaiting_reply"

    # Unknown event: do not invent activity; keep whatever we had.
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


def _claude_pid_status():
    """Map sessionId -> {status, status_epoch (seconds), alive} from Claude's metadata.

    Claude Code writes ~/.claude/sessions/<pid>.json with a ``status`` of
    "idle"/"busy" and ``statusUpdatedAt``; the filename is the process pid.
    """
    out = {}
    try:
        entries = list(CLAUDE_SESSIONS_DIR.glob("*.json"))
    except Exception:
        return out
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = data.get("sessionId")
        if not sid:
            continue
        pid = entry.stem
        raw = data.get("statusUpdatedAt") or data.get("updatedAt") or 0
        epoch = (raw / 1000.0) if raw and raw > 1e12 else float(raw or 0)
        alive = (not _HAS_PROC) or (not pid.isdigit()) or os.path.exists("/proc/" + pid)
        cur = out.get(sid)
        if cur is None or epoch >= cur["status_epoch"]:
            out[sid] = {"status": data.get("status"), "status_epoch": epoch, "alive": alive}
    return out


def _prune_and_reconcile(now):
    """Reconcile drifted states against Claude Code's metadata, then prune.

    Runs on every hook event (so an actively-used session keeps the others
    honest): drop sessions whose process has exited; if we still show "working"
    but Claude reports the session idle, the turn actually ended; otherwise drop
    files that have gone stale.
    """
    try:
        entries = list(SESSIONS_DIR.glob("*.json"))
    except Exception:
        return
    claude = _claude_pid_status()
    for entry in entries:
        try:
            data = _read(entry) or {}
            if not data:
                continue
            sid = data.get("session_id")
            state = data.get("state") or "idle"
            info = claude.get(sid)

            # 1) The session's process has exited -> it's gone.
            if info is not None and not info["alive"]:
                entry.unlink(missing_ok=True)
                continue

            # 2) We say "working" but Claude says idle -> the turn really ended.
            if (state == "working" and info and info.get("status") == "idle"
                    and info["status_epoch"] > 0):
                idle_for = now - info["status_epoch"]
                if idle_for >= RECONCILE_IDLE_GRACE:
                    data["state"] = "idle" if idle_for >= AWAY_AFTER_SECONDS else "awaiting_reply"
                    data["state_since_epoch"] = int(info["status_epoch"])
                    data["updated_epoch"] = now
                    _write(entry, data)
                    continue

            # 3) Staleness backstop (e.g. a crash that left no Claude metadata).
            updated = float(data.get("updated_epoch") or 0)
            limit = STALE_SECONDS.get(state, DEFAULT_STALE_SECONDS)
            if updated <= 0 or now - updated > limit:
                entry.unlink(missing_ok=True)
        except Exception:
            continue


def _session_title(session_id):
    """Return Claude Code's session title (its stored ``name``), or None.

    Claude Code writes per-process metadata to ~/.claude/sessions/<pid>.json,
    including an AI-generated ``name`` (the title shown in the resume picker).
    We match on ``sessionId`` and take the most recently updated entry.
    """
    if not session_id:
        return None
    best = None
    best_updated = -1
    try:
        entries = list(CLAUDE_SESSIONS_DIR.glob("*.json"))
    except Exception:
        return None
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("sessionId") != session_id:
            continue
        name = data.get("name")
        updated = data.get("updatedAt") or 0
        if name and updated >= best_updated:
            best = name
            best_updated = updated
    return best


def _session_pid(session_id):
    """Return the process pid for a session from Claude's pid files, or None."""
    if not session_id:
        return None
    best_pid = None
    best_updated = -1
    try:
        entries = list(CLAUDE_SESSIONS_DIR.glob("*.json"))
    except Exception:
        return None
    for entry in entries:
        if not entry.stem.isdigit():
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("sessionId") != session_id:
            continue
        updated = data.get("updatedAt") or 0
        if updated >= best_updated:
            best_updated = updated
            best_pid = int(entry.stem)
    return best_pid


def _ancestor_pids(pid, max_depth=8):
    """Walk the process tree up from ``pid`` (claude -> shell -> terminal).

    Returns the chain of pids up to but excluding the session manager, so the
    extension can match a window owned by the session's terminal process.
    """
    chain = []
    cur = pid
    depth = 0
    while cur and cur > 1 and depth < max_depth:
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
        chain.append(cur)
        cur = ppid
        depth += 1
    return chain


def _last_ai_title(transcript_path, tail_bytes=131072):
    """Return the most recent ``aiTitle`` from the transcript tail, or None.

    Claude Code appends an ``{"type":"ai-title","aiTitle":...}`` line each time it
    regenerates the session title (starting right after the first prompt), so the
    last such line in the file is the current title. We only read the tail to keep
    this cheap regardless of transcript size.
    """
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            chunk = fh.read()
    except Exception:
        return None
    lines = chunk.decode("utf-8", "replace").split("\n")
    if size > tail_bytes and lines:
        lines = lines[1:]  # drop the partial line we seeked into
    for line in reversed(lines):
        line = line.strip()
        if not line or '"ai-title"' not in line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if data.get("type") == "ai-title":
            title = data.get("aiTitle") or data.get("title")
            if title:
                return str(title)
    return None


def handle_event(payload, now=None):
    """Apply one hook event. Returns the written record, or None.

    Exposed for tests; ``payload`` is the parsed stdin JSON.
    """
    if now is None:
        now = int(time.time())
    if not isinstance(payload, dict):
        return None

    event = payload.get("hook_event_name") or ""
    stem = _safe_id(payload.get("session_id"))
    if not stem:
        return None
    path = SESSIONS_DIR / (stem + ".json")

    if event == "SessionEnd":
        _debug_log("%d %-16s sid=%s -> DELETE" % (now, event, stem[:8]))
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        _prune_and_reconcile(now)
        return None

    prev = _read(path)
    prev_state = prev.get("state") if prev else None
    state = _next_state(payload, prev_state)

    record = dict(prev) if isinstance(prev, dict) else {}
    record["session_id"] = payload.get("session_id")
    record["state"] = state
    record["event"] = event
    record["updated_epoch"] = now
    record.setdefault("started_epoch", now)

    cwd = payload.get("cwd")
    if cwd:
        record["cwd"] = cwd
        record["project"] = os.path.basename(os.path.normpath(cwd)) or cwd
    title = _session_title(payload.get("session_id"))
    if not title:
        title = _last_ai_title(payload.get("transcript_path"))
    if title:
        record["title"] = title

    # Record the terminal process ancestry so the panel can focus the right
    # window if title matching fails. Recompute on (re)start; stable otherwise.
    if event == "SessionStart" or "ancestor_pids" not in record:
        spid = _session_pid(payload.get("session_id"))
        ancestors = _ancestor_pids(spid) if spid else []
        if ancestors:
            record["ancestor_pids"] = ancestors
    model = payload.get("model")
    if model:
        record["model"] = model
    transcript = payload.get("transcript_path")
    if transcript:
        record["transcript_path"] = transcript

    # Track the active tool so the popup can show "Working · Bash".
    if event == "PreToolUse":
        record["current_tool"] = payload.get("tool_name") or ""
    elif event in ("PostToolUse", "UserPromptSubmit", "Stop"):
        record["current_tool"] = ""

    # Keep the notification text as the detail while blocked; clear it otherwise.
    if state == "needs_input" and payload.get("message"):
        record["detail"] = str(payload.get("message"))
    elif state != "needs_input":
        record["detail"] = ""

    # state_since_epoch advances only when the state actually changes.
    if prev_state != state or "state_since_epoch" not in record:
        record["state_since_epoch"] = now

    _debug_log("%d %-16s sid=%s -> %s" % (now, event, stem[:8], state))
    _write(path, record)
    _prune_and_reconcile(now)
    return record


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        handle_event(payload)
    except Exception:
        # Never let a hook failure disrupt the Claude Code session.
        pass
    # Stay silent on stdout and always succeed.
    sys.exit(0)


if __name__ == "__main__":
    main()
