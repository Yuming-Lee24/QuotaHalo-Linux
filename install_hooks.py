#!/usr/bin/env python3
"""Register (or remove) QuotaHalo's Claude Code session-status hooks.

This merges a ``hooks`` block into ``~/.claude/settings.json`` so Claude Code
calls ``claude_session_hook.py`` on every session lifecycle event. The merge is
non-destructive: sibling keys are preserved, the original file is backed up once,
and re-running ``--install`` is a no-op. ``--uninstall`` removes only the entries
this tool added, leaving any user-defined hooks intact.

Usage:
    python3 install_hooks.py --install <repo_dir> [--python /usr/bin/python3]
    python3 install_hooks.py --uninstall
    # --settings <path> targets a different settings file (used by tests).

The hook command is registered under ``/usr/bin/python3`` by default: it is the
fast-starting, always-present system interpreter and the hook is pure stdlib, so
it must not depend on a conda/venv environment.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path


DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULT_PYTHON = "/usr/bin/python3"
HOOK_SCRIPT_NAME = "claude_session_hook.py"
MARKER = HOOK_SCRIPT_NAME  # identifies entries owned by this tool
HOOK_TIMEOUT = 5

# Every event the session hook listens for. One command, branching on the
# hook_event_name carried in the stdin payload.
HOOK_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
]

# Codex CLI uses the same hooks.json shape (~/.codex/hooks.json) but a slightly
# different event set: PermissionRequest instead of a permission Notification,
# and no SessionEnd. Command hooks must additionally be trusted via `/hooks`.
CODEX_HOOK_SCRIPT_NAME = "codex_session_hook.py"
CODEX_MARKER = CODEX_HOOK_SCRIPT_NAME
DEFAULT_CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
CODEX_HOOK_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
]


def build_command(python_bin, hook_script):
    """Build the shell command Claude Code will run for each event."""
    return "{} {}".format(shlex.quote(str(python_bin)), shlex.quote(str(hook_script)))


def load_settings(path):
    """Load settings.json into a dict (empty dict if missing/unreadable)."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path, data, backup=True):
    """Atomically write settings.json, backing up the original once."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        bak = path.with_name(path.name + ".quotahalo.bak")
        if not bak.exists():
            shutil.copy2(path, bak)
    tmp = path.with_name(path.name + ".quotahalo.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _group_has_marker(group, marker=MARKER):
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []) or []:
        if isinstance(hook, dict) and marker in str(hook.get("command", "")):
            return True
    return False


def merge_install(data, command, events=HOOK_EVENTS, marker=MARKER, timeout=HOOK_TIMEOUT):
    """Add our hook command to every event it isn't already on.

    Returns True if anything changed.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    changed = False
    for event in events:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
            hooks[event] = groups
        if any(_group_has_marker(g, marker) for g in groups):
            continue  # idempotent: our command is already registered
        groups.append({
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": timeout,
            }],
        })
        changed = True
    return changed


def merge_uninstall(data, marker=MARKER):
    """Remove only the entries this tool added. Returns True if changed."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False

    changed = False
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            kept = [h for h in (group.get("hooks") or [])
                    if not (isinstance(h, dict) and marker in str(h.get("command", "")))]
            if len(kept) != len(group.get("hooks") or []):
                changed = True
            if kept:
                group["hooks"] = kept
                new_groups.append(group)
            # else: group becomes empty -> drop it
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
            changed = True
    if not hooks:
        data.pop("hooks", None)
    return changed


def install(settings_path, repo_dir, python_bin=DEFAULT_PYTHON, hook_script=None,
            script_name=HOOK_SCRIPT_NAME, events=HOOK_EVENTS, marker=MARKER,
            timeout=HOOK_TIMEOUT):
    if hook_script is None:
        hook_script = Path(repo_dir) / script_name
    command = build_command(python_bin, hook_script)
    data = load_settings(settings_path)
    changed = merge_install(data, command, events, marker, timeout)
    if changed:
        save_settings(settings_path, data)
    return changed, command


def install_codex(hooks_path, repo_dir, python_bin=DEFAULT_PYTHON):
    return install(hooks_path, repo_dir, python_bin=python_bin,
                   script_name=CODEX_HOOK_SCRIPT_NAME, events=CODEX_HOOK_EVENTS,
                   marker=CODEX_MARKER, timeout=10)


def uninstall(settings_path, marker=MARKER):
    data = load_settings(settings_path)
    changed = merge_uninstall(data, marker)
    if changed:
        save_settings(settings_path, data)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage QuotaHalo agent session hooks.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", metavar="REPO_DIR",
                       help="Register hooks pointing at REPO_DIR's session hook script")
    group.add_argument("--uninstall", action="store_true",
                       help="Remove the hooks this tool added.")
    parser.add_argument("--codex", action="store_true",
                        help="Target Codex CLI (~/.codex/hooks.json) instead of Claude Code.")
    parser.add_argument("--settings", default=None,
                        help="Override the hooks file path.")
    parser.add_argument("--python", default=DEFAULT_PYTHON,
                        help="Interpreter for the hook command (default: /usr/bin/python3)")
    parser.add_argument("--hook-script", default=None,
                        help="Override the hook script path.")
    args = parser.parse_args(argv)

    path = args.settings or (str(DEFAULT_CODEX_HOOKS) if args.codex else str(DEFAULT_SETTINGS))
    marker = CODEX_MARKER if args.codex else MARKER
    agent = "Codex" if args.codex else "Claude Code"

    if args.uninstall:
        changed = uninstall(path, marker)
        print("[QuotaHalo] {} hooks removed from {}".format(agent, path) if changed
              else "[QuotaHalo] No QuotaHalo {} hooks found in {}".format(agent, path))
        return 0

    if args.codex:
        changed, command = install_codex(path, args.install, python_bin=args.python)
    else:
        changed, command = install(path, args.install,
                                   python_bin=args.python, hook_script=args.hook_script)
    if changed:
        print("[QuotaHalo] Registered {} session hooks in {}".format(agent, path))
        print("            command: {}".format(command))
        if args.codex:
            print("            NOTE: run `/hooks` inside Codex once to trust this hook.")
    else:
        print("[QuotaHalo] {} session hooks already present in {} (no change)".format(agent, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
