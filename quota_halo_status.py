"""Headless Codex/Claude usage cache refresher for the GNOME extension."""

import os
import sys
import json
import time
import re
import shutil
import subprocess
import base64
from pathlib import Path
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError


STATUS_DIR = Path.home() / ".cache" / "quotahalo"
STATUS_LABEL_FILE = STATUS_DIR / "usage-label.txt"
STATUS_JSON_FILE = STATUS_DIR / "usage-status.json"
CLAUDE_USAGE_CACHE_FILE = STATUS_DIR / "claude-usage-cache.json"
CLAUDE_OAUTH_BACKOFF_FILE = STATUS_DIR / "claude-oauth-backoff.json"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_USAGE_QUERY_TRASH_DIR = STATUS_DIR / "claude-usage-query-trash"
CLAUDE_USAGE_REFRESH_SECONDS = 300
CLAUDE_OAUTH_BACKOFF_SECONDS = 300
USAGE_QUERY_PROMPTS = {"/usage", "usage"}


def _claude_project_dir_for_cwd(cwd):
    return CLAUDE_PROJECTS_DIR / str(Path(cwd).resolve()).replace("/", "-")


def _content_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts).strip()
    return ""


def _normalize_usage_prompt(text):
    text = str(text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _is_usage_query_transcript(path):
    prompts = []
    saw_cli_marker = False

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue

                if entry.get("entrypoint") == "sdk-cli":
                    saw_cli_marker = True

                prompt = None
                entry_type = entry.get("type")
                if entry_type == "queue-operation" and entry.get("operation") == "enqueue":
                    prompt = entry.get("content")
                    saw_cli_marker = True
                elif entry_type == "user":
                    prompt = _content_text((entry.get("message") or {}).get("content"))
                elif entry_type == "last-prompt":
                    prompt = entry.get("lastPrompt")

                if prompt is None:
                    continue
                prompt = _normalize_usage_prompt(prompt)
                if not prompt:
                    continue
                if prompt not in USAGE_QUERY_PROMPTS:
                    return False
                prompts.append(prompt)
    except Exception:
        return False

    return bool(prompts) and saw_cli_marker


def _snapshot_project_jsonl(project_dir):
    if not project_dir.exists():
        return {}
    snapshot = {}
    for path in project_dir.glob("*.jsonl"):
        try:
            stat = path.stat()
            snapshot[path] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
    return snapshot


def _move_usage_query_transcripts(paths, dry_run=False):
    moved = []
    trash_root = CLAUDE_USAGE_QUERY_TRASH_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")

    for path in paths:
        if not _is_usage_query_transcript(path):
            continue
        moved.append(path)
        if dry_run:
            continue
        try:
            rel = path.relative_to(CLAUDE_PROJECTS_DIR)
        except ValueError:
            rel = Path(path.name)
        dest = trash_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(dest)
        except Exception as e:
            print(f"[QuotaHalo] Claude usage cleanup err: {path}: {e}", flush=True)

    return moved


def _move_legacy_usage_trash_dirs(dry_run=False):
    moved = []
    trash_root = (
        CLAUDE_USAGE_QUERY_TRASH_DIR
        / datetime.now().strftime("%Y%m%d_%H%M%S")
        / "legacy-project-trash"
    )

    for path in CLAUDE_PROJECTS_DIR.glob("*/_usage_trash_*"):
        if not path.is_dir():
            continue
        moved.append(path)
        if dry_run:
            continue
        try:
            rel = path.relative_to(CLAUDE_PROJECTS_DIR)
        except ValueError:
            rel = Path(path.name)
        dest = trash_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(dest)
        except Exception as e:
            print(f"[QuotaHalo] Claude legacy trash cleanup err: {path}: {e}", flush=True)

    return moved


def _cleanup_new_usage_query_sessions(project_dir, before):
    candidates = []
    if not project_dir.exists():
        return []
    for path in project_dir.glob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        current = (stat.st_mtime_ns, stat.st_size)
        if before.get(path) != current:
            candidates.append(path)
    return _move_usage_query_transcripts(candidates)


def cleanup_claude_usage_query_history(dry_run=False):
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    moved = _move_usage_query_transcripts(
        CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"),
        dry_run=dry_run,
    )
    moved.extend(_move_legacy_usage_trash_dirs(dry_run=dry_run))
    return moved


# ─────────────────────────────────────────────
# Data fetcher
# ─────────────────────────────────────────────

class ClaudeDataFetcher:
    def __init__(self):
        self.data = self._empty()

    def _empty(self):
        return {
            "provider": "Claude", "plan": "Unknown", "updated": "Never",
            "updated_epoch": None,
            "session_used_pct": 0, "session_reset": "unknown", "session_reset_epoch": None,
            "weekly_used_pct": 0, "weekly_reset": "unknown", "weekly_reset_epoch": None,
            "opus_used_pct": 0,
            "model": "",
            "cost_today": 0.0, "cost_today_tokens": "0",
            "cost_30d": 0.0, "cost_30d_tokens": "0",
            "source": "none", "error": None,
            "installed": False,
        }

    def _load_usage_cache(self, max_age=CLAUDE_USAGE_REFRESH_SECONDS):
        try:
            raw = json.loads(CLAUDE_USAGE_CACHE_FILE.read_text(encoding="utf-8"))
            cached_at = float(raw.get("_cached_at") or 0)
            if max_age is not None and (time.time() - cached_at) > max_age:
                return None
            raw.pop("_cached_at", None)
            if raw.get("source") in ("oauth", "cli"):
                raw["plan"] = self._local_subscription_plan() or self._format_plan(
                    raw.get("plan"), raw.get("plan") or "Unknown")
                raw["installed"] = True
                return raw
        except Exception:
            pass
        return None

    def _load_status_fallback(self):
        try:
            raw = json.loads(STATUS_JSON_FILE.read_text(encoding="utf-8"))
            claude = raw.get("claude") or {}
            if claude.get("source") not in ("oauth", "cli"):
                return None
            d = self._empty()
            d.update({
                "provider": "Claude",
                "plan": self._format_plan(claude.get("plan"), claude.get("plan") or "Unknown"),
                "updated": claude.get("updated", "Never"),
                "updated_epoch": claude.get("updated_epoch"),
                "session_used_pct": _clamp_pct(claude.get("session_used_pct", 0)),
                "session_reset": claude.get("session_reset") or "unknown",
                "session_reset_epoch": claude.get("session_reset_epoch"),
                "weekly_used_pct": _clamp_pct(claude.get("weekly_used_pct", 0)),
                "weekly_reset": claude.get("weekly_reset") or "unknown",
                "weekly_reset_epoch": claude.get("weekly_reset_epoch"),
                "model": claude.get("model", ""),
                "source": claude.get("source"),
                "installed": bool(claude.get("available", True)),
            })
            return d
        except Exception:
            return None

    def _save_usage_cache(self):
        try:
            STATUS_DIR.mkdir(parents=True, exist_ok=True)
            payload = dict(self.data)
            payload["_cached_at"] = time.time()
            tmp = CLAUDE_USAGE_CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            tmp.replace(CLAUDE_USAGE_CACHE_FILE)
        except Exception:
            pass

    def _oauth_backoff_active(self):
        try:
            raw = json.loads(CLAUDE_OAUTH_BACKOFF_FILE.read_text(encoding="utf-8"))
            failed_at = float(raw.get("failed_at") or 0)
            return (time.time() - failed_at) < CLAUDE_OAUTH_BACKOFF_SECONDS
        except Exception:
            return False

    def _format_plan(self, tier, fallback="Pro"):
        value = str(tier or "").strip()
        normalized = value.lower()
        if normalized.startswith("default_claude_"):
            normalized = normalized[len("default_claude_"):]
        normalized = normalized.replace("-", "_").replace(" ", "_")
        names = {
            "ai": "Claude AI",
            "claude_ai": "Claude AI",
            "pro": "Pro",
            "max": "Max",
            "team": "Team",
            "enterprise": "Enterprise",
            "free": "Free",
        }
        if normalized in names:
            return names[normalized]
        if normalized:
            return normalized.replace("_", " ").title()
        return fallback

    def _local_subscription_plan(self):
        try:
            if not self._CREDS_PATH.exists():
                return ""
            creds = json.loads(self._CREDS_PATH.read_text(encoding="utf-8"))
            oauth = creds.get("claudeAiOauth") or {}
            subscription = oauth.get("subscriptionType") or ""
            tier = oauth.get("rateLimitTier") or ""
            return self._format_plan(subscription or tier, "")
        except Exception:
            return ""

    def _record_oauth_failure(self, error):
        try:
            STATUS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CLAUDE_OAUTH_BACKOFF_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "failed_at": time.time(),
                "error": str(error)[:500],
            }, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            tmp.replace(CLAUDE_OAUTH_BACKOFF_FILE)
        except Exception:
            pass

    def _clear_oauth_failure(self):
        try:
            CLAUDE_OAUTH_BACKOFF_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _is_claude_installed(self):
        """Check if Claude Code is installed (CLI in PATH or ~/.claude exists)."""
        claude_dir = Path.home() / ".claude"
        if claude_dir.exists():
            return True
        if self._find_claude():
            return True
        if shutil.which("claude"):
            return True
        return False

    def fetch_all(self):
        print("[QuotaHalo] Fetching real usage data...")
        got_usage = False
        used_usage_cache = False
        skip_cli_fallback = False

        cached = self._load_usage_cache()
        if cached:
            self.data = cached
            got_usage = True
            used_usage_cache = True
            print(
                f"  OK Claude cache: session {cached['session_used_pct']}%, "
                f"weekly {cached['weekly_used_pct']}%",
                flush=True)

        # 1) Try OAuth usage endpoint from local Claude credentials.
        if not got_usage:
            cred = self._fetch_oauth_api()
            if cred:
                self.data = cred
                got_usage = cred.get("source") == "oauth"
                if got_usage:
                    print(
                        f"  OK OAuth: session {cred['session_used_pct']}%, "
                        f"weekly {cred['weekly_used_pct']}%",
                        flush=True)
                else:
                    stale = self._load_status_fallback()
                    if stale:
                        if cred.get("plan") and cred.get("plan") != "Unknown":
                            stale["plan"] = cred["plan"]
                        self.data = stale
                        got_usage = True
                        used_usage_cache = True
                        self._save_usage_cache()
                        print(
                            f"  OK Claude stale cache: session {stale['session_used_pct']}%, "
                            f"weekly {stale['weekly_used_pct']}%",
                            flush=True)
                    else:
                        skip_cli_fallback = True
                        print(f"  OK Credentials: plan={cred['plan']} (usage unavailable)", flush=True)
            else:
                print("  -- Credentials: not available")

        # 2) Try CLI fallback only when OAuth/cache did not provide quota.
        if not got_usage and not skip_cli_fallback:
            cli = self._fetch_cli()
            if cli and cli.get("source") == "cli":
                self.data = cli
                got_usage = True
                print(f"  OK CLI: session {cli['session_used_pct']}%, weekly {cli['weekly_used_pct']}%")
            else:
                print("  -- CLI: not available")

        # 3) Always try JSONL for cost data
        cost = self._fetch_jsonl()
        if cost:
            self.data["cost_today"] = cost["cost_today"]
            self.data["cost_today_tokens"] = cost["cost_today_tokens"]
            self.data["cost_30d"] = cost["cost_30d"]
            self.data["cost_30d_tokens"] = cost["cost_30d_tokens"]
            if cost.get("model"):
                self.data["model"] = cost["model"]
            if self.data["source"] == "none":
                self.data["source"] = "logs"
            print(f"  OK Logs: today ${cost['cost_today']:.2f}, 30d ${cost['cost_30d']:.2f}")
        else:
            print("  -- Logs: no JSONL found")

        if not used_usage_cache:
            self.data["updated"] = datetime.now().strftime("Updated %H:%M:%S")
            self.data["updated_epoch"] = int(time.time())
            if got_usage and self.data.get("source") in ("oauth", "cli"):
                self._save_usage_cache()
        self.data["installed"] = self._is_claude_installed()
        return self.data

    def _fetch_cli(self):
        """Get usage via subprocess (not PTY — PTY hangs on many Linux setups)."""
        cmd = self._find_claude()
        if not cmd:
            return None

        # Quick pre-check: run "claude /usage" as a subprocess with hard timeout
        try:
            print("    CLI: trying subprocess...", flush=True)
            result = self._run_usage_cli(cmd, ["/usage"])
            raw = (result.stdout or "") + (result.stderr or "")
            print(f"    CLI: exit={result.returncode}, len={len(raw)}", flush=True)

            if not raw:
                print("    CLI: no output", flush=True)
                return None

            lower = raw.lower()
            if "unknown skill" in lower or "unknown command" in lower:
                print("    CLI: /usage not supported on this version", flush=True)
                return None

            if "%" in raw and ("session" in lower or "week" in lower):
                return self._parse_usage(raw)

            # Try alternative: "claude usage" without slash
            print("    CLI: trying 'claude usage'...", flush=True)
            result2 = self._run_usage_cli(cmd, ["usage"])
            raw2 = (result2.stdout or "") + (result2.stderr or "")
            if raw2 and "%" in raw2:
                lower2 = raw2.lower()
                if "session" in lower2 or "week" in lower2:
                    return self._parse_usage(raw2)

            print("    CLI: output didn't contain usage data", flush=True)
        except subprocess.TimeoutExpired:
            print("    CLI: timed out after 10s", flush=True)
        except Exception as e:
            print(f"    CLI err: {e}", flush=True)
        return None

    def _run_usage_cli(self, cmd, args):
        cwd = Path.home()
        project_dir = _claude_project_dir_for_cwd(cwd)
        before = _snapshot_project_jsonl(project_dir)
        try:
            return subprocess.run(
                [cmd, *args],
                capture_output=True, text=True, timeout=10,
                cwd=str(cwd),
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            )
        finally:
            moved = _cleanup_new_usage_query_sessions(project_dir, before)
            if moved:
                print(f"    CLI cleanup: moved {len(moved)} usage transcript(s)", flush=True)

    def _find_claude(self):
        places = [
            Path.home() / ".local" / "bin" / "claude",
            Path.home() / ".npm-global" / "bin" / "claude",
            Path.home() / ".nvm" / "current" / "bin" / "claude",
            Path("/usr/local/bin/claude"),
            Path("/usr/bin/claude"),
            Path.home() / ".claude" / "local" / "claude",
        ]
        # Also check common nvm paths
        nvm_dir = Path.home() / ".nvm" / "versions" / "node"
        if nvm_dir.exists():
            for node_ver in sorted(nvm_dir.iterdir(), reverse=True):
                p = node_ver / "bin" / "claude"
                if p.exists():
                    places.insert(0, p)
                    break

        for p in places:
            if p.exists():
                print(f"    Found claude: {p}")
                return str(p)
        r = shutil.which("claude")
        if r:
            print(f"    Found claude in PATH: {r}")
        return r

    def _parse_usage(self, raw):
        """Parse the /usage output from the interactive Claude CLI."""
        clean = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', raw)
        clean = re.sub(r'\x1b\][^\x07\x1b]*[\x07]', '', clean)
        clean = re.sub(r'\x1b[()>][0-9A-Z]', '', clean)
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', clean)

        d = self._empty()
        d["source"] = "cli"

        clean = re.sub(r'(Current\s+session)', r'\n\1', clean, flags=re.I)
        clean = re.sub(r'(Current\s+week)', r'\n\1', clean, flags=re.I)
        clean = re.sub(r'(\d+\s*%\s*used)', r'\n\1', clean, flags=re.I)
        clean = re.sub(r'([Rr]es(?:et)?s?\s+\w)', r'\n\1', clean)

        lines = clean.split("\n")
        section = None

        for line in lines:
            lo = line.lower().strip()
            if "current session" in lo:
                section = "session"
            elif "current week" in lo and "sonnet" not in lo:
                section = "weekly"
            elif "sonnet" in lo and "week" in lo:
                section = "sonnet"
            if not section:
                continue

            m = re.search(r'(\d+)\s*%\s*used', line, re.I)
            if m:
                pct = int(m.group(1))
                if section == "session":
                    d["session_used_pct"] = pct
                elif section == "weekly":
                    d["weekly_used_pct"] = pct

            rm = re.search(r'[Rr]es[et]*s?\s*(.+)', line)
            if rm:
                val = rm.group(1).strip()
                val = re.sub(r'\s*Esc.*$', '', val).rstrip(". ")
                val = re.sub(r'\s*\([^)]*\)\s*$', '', val).strip()
                if val and len(val) > 2:
                    if section == "session":
                        d["session_reset"] = val
                    elif section == "weekly":
                        d["weekly_reset"] = val

        m = re.search(r'Claude\s*(Max|Pro|Team|Enterprise|Free)', clean, re.I)
        if m:
            d["plan"] = m.group(1).title()
        return d

    # ── OAuth token fetcher ───────────────────

    _CREDS_PATH = Path.home() / ".claude" / ".credentials.json"

    def _fetch_oauth_api(self):
        """Fetch Claude usage via the same OAuth endpoint used by Claude Code."""
        if not self._CREDS_PATH.exists():
            return None
        try:
            with open(self._CREDS_PATH, "r", encoding="utf-8") as f:
                creds = json.load(f)
            oauth = creds.get("claudeAiOauth") or {}
            token = oauth.get("accessToken")
            if not token:
                return None

            tier = oauth.get("rateLimitTier") or ""
            subscription = oauth.get("subscriptionType") or ""
            plan_local = self._format_plan(subscription or tier)
            print(f"    OAuth: credentials found, plan: {plan_local}", flush=True)

            if self._oauth_backoff_active():
                print("    OAuth usage API: skipped during backoff", flush=True)
                d = self._empty()
                d["plan"] = plan_local
                d["source"] = "credentials"
                d["error"] = "OAuth usage API skipped during recent failure backoff"
                d["installed"] = True
                return d

            last_error = None
            try:
                req = Request(
                    "https://api.anthropic.com/api/oauth/usage",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "anthropic-beta": "oauth-2025-04-20",
                        "User-Agent": f"claude-code/{self._claude_version() or '2.1.0'}",
                    })
                with urlopen(req, timeout=15) as resp:
                    usage = json.loads(resp.read())
                self._clear_oauth_failure()
                return self._parse_oauth_usage(usage, plan_local)
            except HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:240]
                except Exception:
                    pass
                message = f"HTTP {e.code} {body}"
                last_error = message
                self._record_oauth_failure(message)
                print(f"    OAuth usage API err: {message}", flush=True)
            except Exception as e:
                last_error = str(e)
                self._record_oauth_failure(e)
                print(f"    OAuth usage API err: {e}", flush=True)

            d = self._empty()
            d["plan"] = plan_local
            d["source"] = "credentials"
            d["error"] = last_error or "OAuth usage API unavailable"
            d["installed"] = True
            return d
        except Exception as e:
            print(f"    OAuth creds err: {e}", flush=True)
            return None

    def _claude_version(self):
        cmd = self._find_claude()
        if not cmd:
            return None
        try:
            result = subprocess.run(
                [cmd, "--version"],
                capture_output=True, text=True, timeout=3,
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            )
            raw = ((result.stdout or "") + " " + (result.stderr or "")).strip()
            m = re.search(r'(\d+(?:\.\d+){1,3})', raw)
            return m.group(1) if m else None
        except Exception:
            return None

    def _parse_oauth_usage(self, usage, plan_hint):
        d = self._empty()
        d["source"] = "oauth"
        d["plan"] = plan_hint or "Pro"
        d["installed"] = True

        def window(name):
            blob = usage.get(name)
            return blob if isinstance(blob, dict) else None

        def pct(blob):
            if not blob:
                return None
            for key in ("utilization", "used_percent", "used_pct", "usage_percent"):
                if blob.get(key) is not None:
                    return _clamp_pct(blob.get(key))
            return None

        def reset_epoch(blob):
            if not blob:
                return None
            raw = blob.get("resets_at") or blob.get("reset_at") or blob.get("expires_at")
            if not raw:
                return None
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return int(dt.timestamp())
            except Exception:
                return None

        def reset_text(epoch):
            if not epoch:
                return "unknown"
            secs = max(0, int(epoch - time.time()))
            if secs <= 0:
                return "now"
            mins = secs // 60
            days, rem = divmod(mins, 24 * 60)
            hours, minutes = divmod(rem, 60)
            if days:
                return f"{days}d {hours}h"
            return f"{hours}h {minutes:02d}m"

        session = window("five_hour") or window("seven_day")
        weekly = window("seven_day") or window("seven_day_oauth_apps")
        model_weekly = window("seven_day_sonnet") or window("seven_day_opus")

        session_pct = pct(session)
        weekly_pct = pct(weekly)
        model_pct = pct(model_weekly)
        if session_pct is not None:
            d["session_used_pct"] = session_pct
        if weekly_pct is not None:
            d["weekly_used_pct"] = weekly_pct
        if model_pct is not None:
            d["opus_used_pct"] = model_pct

        session_epoch = reset_epoch(session)
        weekly_epoch = reset_epoch(weekly)
        d["session_reset_epoch"] = session_epoch
        d["weekly_reset_epoch"] = weekly_epoch
        d["session_reset"] = reset_text(session_epoch)
        d["weekly_reset"] = reset_text(weekly_epoch)
        return d

    def _fetch_jsonl(self):
        dirs = [Path.home() / ".claude" / "projects", Path.home() / ".claude"]
        total_in = total_out = total_cache = today_in = today_out = 0
        latest_model = ""
        latest_model_at = -1
        seen = set()
        today = datetime.now().date()
        nfiles = 0

        for d in dirs:
            if not d.exists(): continue
            for f in d.rglob("*.jsonl"):
                if "_usage_trash_" in str(f):
                    continue
                nfiles += 1
                try:
                    with open(f, 'r', encoding='utf-8', errors='ignore') as fh:
                        for line in fh:
                            line = line.strip()
                            if not line or len(line) < 10: continue
                            try: entry = json.loads(line)
                            except Exception: continue
                            if entry.get("type") != "assistant": continue
                            msg = entry.get("message",{})
                            model = msg.get("model") or entry.get("model")
                            if model:
                                try:
                                    ts = entry.get("timestamp","")
                                    model_at = datetime.fromisoformat(
                                        ts.replace("Z","+00:00")).timestamp() if ts else f.stat().st_mtime
                                except Exception:
                                    model_at = f.stat().st_mtime
                                if model_at > latest_model_at:
                                    latest_model = str(model)
                                    latest_model_at = model_at
                            usage = entry.get("message",{}).get("usage",{})
                            if not usage: continue
                            mid = entry.get("message",{}).get("id","")
                            rid = entry.get("requestId","")
                            key = f"{mid}:{rid}"
                            if key in seen: continue
                            seen.add(key)
                            inp = usage.get("input_tokens",0)
                            out = usage.get("output_tokens",0)
                            cr = usage.get("cache_read_input_tokens",0)
                            cc = usage.get("cache_creation_input_tokens",0)
                            total_in += inp; total_out += out; total_cache += cr+cc
                            ts = entry.get("timestamp","")
                            if ts:
                                try:
                                    if datetime.fromisoformat(ts.replace("Z","+00:00")).date() == today:
                                        today_in += inp; today_out += out
                                except Exception: pass
                except Exception: continue

        print(f"    Scanned {nfiles} files, {len(seen)} messages")
        if total_in + total_out == 0: return None

        c30 = (total_in*3 + total_out*15 + total_cache*1.5) / 1e6
        ct = (today_in*3 + today_out*15) / 1e6

        def fmt(n):
            if n >= 1e6: return f"{n/1e6:.0f}M"
            if n >= 1e3: return f"{n/1e3:.0f}K"
            return str(n)

        return {
            "cost_today": round(ct,2), "cost_today_tokens": fmt(today_in+today_out),
            "cost_30d": round(c30,2), "cost_30d_tokens": fmt(total_in+total_out+total_cache),
            "model": latest_model,
        }


# ─────────────────────────────────────────────

def _clamp_pct(pct):
    try:
        return max(0, min(100, int(pct)))
    except Exception:
        return 0


def _remaining_pct(used_pct):
    return 100 - _clamp_pct(used_pct)


def _compact_reset(reset_value):
    if not reset_value or reset_value == "unknown":
        return "--"
    return str(reset_value)


# OpenAI Codex data fetcher
# ─────────────────────────────────────────────

class CodexDataFetcher:
    """Fetch usage data from OpenAI Codex local session files (~/.codex/)."""

    CODEX_DIR = Path.home() / ".codex"

    @staticmethod
    def _empty():
        return {
            "provider": "Codex", "plan": "Plus",
            "updated": "Never", "source": "none",
            "session_used_pct": 0, "session_reset": "unknown", "session_reset_epoch": None,
            "weekly_used_pct": 0, "weekly_reset": "unknown", "weekly_reset_epoch": None,
            "cost_today": 0, "cost_today_tokens": "0",
            "cost_30d": 0, "cost_30d_tokens": "0",
            "model": "",
            "error": None, "available": False,
        }

    def fetch(self):
        d = self._empty()
        if not self.CODEX_DIR.exists():
            d["error"] = "Codex not installed"
            return d
        d["available"] = True

        try:
            config = self.CODEX_DIR / "config.toml"
            if config.exists():
                for line in config.read_text().splitlines():
                    if line.startswith("model"):
                        d["model"] = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass

        try:
            auth = self.CODEX_DIR / "auth.json"
            if auth.exists():
                aj = json.loads(auth.read_text(encoding="utf-8"))
                tokens = aj.get("tokens", {})
                at = tokens.get("access_token", "")
                if at:
                    parts = at.split(".")
                    if len(parts) >= 2:
                        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                        claims = json.loads(base64.b64decode(payload))
                        plan = claims.get("https://api.openai.com/auth", {}).get(
                            "chatgpt_plan_type", "")
                        if plan:
                            d["plan"] = plan.capitalize()
        except Exception:
            pass

        self._scan_sessions(d)
        return d

    def _scan_sessions(self, d):
        sessions_dir = self.CODEX_DIR / "sessions"
        if not sessions_dir.exists():
            d["source"] = "config"
            return

        jsonl_files = sorted(sessions_dir.rglob("*.jsonl"),
                             key=lambda f: f.stat().st_mtime, reverse=True)
        if not jsonl_files:
            d["source"] = "config"
            return

        print(f"    Codex: scanning {len(jsonl_files)} session files")

        latest_limits = None
        latest_limits_updated = None
        latest_limits_score = (-1, -1, -1)
        for jf in jsonl_files:
            rate_limits = self._extract_rate_limits(jf)
            if rate_limits:
                limits, updated, sort_key = rate_limits
                score = self._rate_limit_score(limits, sort_key)
                if score > latest_limits_score:
                    latest_limits = limits
                    latest_limits_updated = updated
                    latest_limits_score = score

        if latest_limits:
            rl = latest_limits
            primary = rl.get("primary", {})
            if primary:
                self._apply_rate_limit(d, primary, "session")
            secondary = rl.get("secondary", {})
            if secondary:
                self._apply_rate_limit(d, secondary, "weekly")
            plan = rl.get("plan_type", "")
            if plan:
                d["plan"] = plan.capitalize()
            if latest_limits_updated:
                d["updated"] = latest_limits_updated
            d["source"] = "sessions"
        else:
            d["source"] = "config"

        total_in = total_out = today_in = today_out = 0
        today = datetime.now().date()

        for jf in jsonl_files:
            try:
                tokens = self._extract_total_tokens(jf)
                if not tokens:
                    continue
                inp = tokens.get("input_tokens", 0)
                out = tokens.get("output_tokens", 0)
                total_in += inp
                total_out += out
                try:
                    ts_str = jf.stem.split("rollout-")[1][:10]
                    if datetime.strptime(ts_str, "%Y-%m-%d").date() == today:
                        today_in += inp
                        today_out += out
                except Exception:
                    pass
            except Exception:
                continue

        c30 = (total_in * 2.5 + total_out * 10) / 1e6
        ct = (today_in * 2.5 + today_out * 10) / 1e6

        def fmt(n):
            if n >= 1e6: return f"{n / 1e6:.1f}M"
            if n >= 1e3: return f"{n / 1e3:.0f}K"
            return str(n)

        d["cost_today"] = round(ct, 2)
        d["cost_today_tokens"] = fmt(today_in + today_out)
        d["cost_30d"] = round(c30, 2)
        d["cost_30d_tokens"] = fmt(total_in + total_out)

    @staticmethod
    def _extract_rate_limits(jsonl_path):
        best = None
        best_updated = None
        best_sort_key = -1
        best_score = (-1, -1, -1)
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or "rate_limits" not in line:
                        continue
                    try:
                        e = json.loads(line)
                        p = e.get("payload", {})
                        if isinstance(p, dict) and p.get("type") == "token_count":
                            rl = p.get("rate_limits")
                            if rl:
                                updated, sort_key = CodexDataFetcher._format_event_time(
                                    e.get("timestamp"), jsonl_path)
                                score = CodexDataFetcher._rate_limit_score(rl, sort_key)
                                if score > best_score:
                                    best = rl
                                    best_updated = updated
                                    best_sort_key = sort_key
                                    best_score = score
                    except Exception:
                        pass
        except Exception:
            pass
        if not best:
            return None
        return best, best_updated, best_sort_key

    @staticmethod
    def _rate_limit_score(rate_limits, sort_key):
        primary = rate_limits.get("primary", {}) if isinstance(rate_limits, dict) else {}
        reset_at = primary.get("resets_at") or 0
        used_pct = primary.get("used_percent", 0)
        try:
            reset_at = int(reset_at)
        except Exception:
            reset_at = 0
        # Codex can emit the same window with resets_at differing by a second.
        reset_bucket = reset_at // 60 if reset_at > 0 else 0
        return (reset_bucket, _clamp_pct(used_pct), sort_key)

    @staticmethod
    def _to_epoch(value):
        if value in (None, "", "unknown"):
            return None
        try:
            epoch = float(value)
            if epoch > 100_000_000_000:
                epoch = epoch / 1000
            return epoch if epoch > 0 else None
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None

    @classmethod
    def _rate_limit_expired(cls, rate_limit):
        epoch = cls._to_epoch(rate_limit.get("resets_at") if isinstance(rate_limit, dict) else None)
        return bool(epoch and epoch <= time.time())

    @classmethod
    def _apply_rate_limit(cls, data, rate_limit, prefix):
        used_key = f"{prefix}_used_pct"
        reset_key = f"{prefix}_reset"
        epoch_key = f"{prefix}_reset_epoch"
        reset_epoch = cls._to_epoch(rate_limit.get("resets_at"))

        if reset_epoch:
            data[epoch_key] = int(reset_epoch)
            data[reset_key] = "now" if reset_epoch <= time.time() else cls._format_reset(reset_epoch)
        data[used_key] = 0 if cls._rate_limit_expired(rate_limit) else _clamp_pct(rate_limit.get("used_percent", 0))

    @staticmethod
    def _extract_total_tokens(jsonl_path):
        last = None
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or "total_token_usage" not in line:
                        continue
                    try:
                        e = json.loads(line)
                        p = e.get("payload", {})
                        if isinstance(p, dict) and p.get("type") == "token_count":
                            t = p.get("info", {}).get("total_token_usage")
                            if t:
                                last = t
                    except Exception:
                        pass
        except Exception:
            pass
        return last

    @staticmethod
    def _format_reset(epoch):
        try:
            dt = datetime.fromtimestamp(epoch)
            now = datetime.now()
            if dt.date() == now.date():
                return dt.strftime("%H:%M:%S")
            return dt.strftime("%m-%d %H:%M:%S")
        except Exception:
            return "unknown"

    @staticmethod
    def _format_event_time(timestamp, fallback_path=None):
        try:
            if timestamp:
                dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone()
            elif fallback_path:
                dt = datetime.fromtimestamp(fallback_path.stat().st_mtime)
            else:
                return "Never", -1

            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            sort_key = dt.timestamp()
            if dt.date() == now.date():
                return dt.strftime("Updated %H:%M:%S"), sort_key
            return dt.strftime("Updated %m-%d %H:%M:%S"), sort_key
        except Exception:
            return "Never", -1


# ─────────────────────────────────────────────
# Status writer


def _provider_label(data):
    if not data.get("available", False):
        return "Codex --"
    if data.get("source") in ("none", "config"):
        return "Codex --"
    return f"Codex {_clamp_pct(data.get('session_used_pct', 0))}%"


def _provider_title(data):
    if not data.get("available", False):
        return "QuotaHalo - Codex not detected"
    if data.get("source") in ("none", "config"):
        return "QuotaHalo - Codex usage unavailable"

    session = _clamp_pct(data.get("session_used_pct", 0))
    weekly = _clamp_pct(data.get("weekly_used_pct", 0))
    title = (
        "QuotaHalo - Codex Quota: "
        f"Session {session}% used ({_remaining_pct(session)}% remaining), "
        f"Weekly {weekly}% used ({_remaining_pct(weekly)}% remaining)"
    )
    session_reset = data.get("session_reset")
    weekly_reset = data.get("weekly_reset")
    if session_reset and session_reset != "unknown":
        title += f" | Session resets {session_reset}"
    if weekly_reset and weekly_reset != "unknown":
        title += f" | Weekly resets {weekly_reset}"
    return title


def _has_claude_quota(data):
    if data.get("source") not in ("oauth", "cli"):
        return False
    return data.get("session_used_pct") is not None


def _panel_status_payload(claude, codex):
    return {
        "label": _provider_label(codex),
        "title": _provider_title(codex),
        "provider": "Codex",
        "available": bool(codex.get("available", False)),
        "source": codex.get("source", "none"),
        "error": codex.get("error"),
        "updated": codex.get("updated", "Never"),
        "updated_epoch": codex.get("updated_epoch"),
        "plan": codex.get("plan", ""),
        "model": codex.get("model", ""),
        "session_used_pct": _clamp_pct(codex.get("session_used_pct", 0)),
        "session_remaining_pct": _remaining_pct(codex.get("session_used_pct", 0)),
        "session_reset": _compact_reset(codex.get("session_reset")),
        "session_reset_epoch": codex.get("session_reset_epoch"),
        "weekly_used_pct": _clamp_pct(codex.get("weekly_used_pct", 0)),
        "weekly_remaining_pct": _remaining_pct(codex.get("weekly_used_pct", 0)),
        "weekly_reset": _compact_reset(codex.get("weekly_reset")),
        "weekly_reset_epoch": codex.get("weekly_reset_epoch"),
        "cost_today": codex.get("cost_today", 0),
        "cost_today_tokens": codex.get("cost_today_tokens", "0"),
        "cost_30d": codex.get("cost_30d", 0),
        "cost_30d_tokens": codex.get("cost_30d_tokens", "0"),
        "cost_window": "30d",
        "claude": {
            "provider": "Claude",
            "available": _has_claude_quota(claude),
            "source": claude.get("source", "none"),
            "error": claude.get("error"),
            "updated": claude.get("updated", "Never"),
            "updated_epoch": claude.get("updated_epoch"),
            "plan": claude.get("plan", ""),
            "model": claude.get("model", ""),
            "session_used_pct": _clamp_pct(claude.get("session_used_pct", 0)),
            "session_remaining_pct": _remaining_pct(claude.get("session_used_pct", 0)),
            "session_reset": _compact_reset(claude.get("session_reset")),
            "session_reset_epoch": claude.get("session_reset_epoch"),
            "weekly_used_pct": _clamp_pct(claude.get("weekly_used_pct", 0)),
            "weekly_remaining_pct": _remaining_pct(claude.get("weekly_used_pct", 0)),
            "weekly_reset": _compact_reset(claude.get("weekly_reset")),
            "weekly_reset_epoch": claude.get("weekly_reset_epoch"),
            "cost_today": claude.get("cost_today", 0),
            "cost_today_tokens": claude.get("cost_today_tokens", "0"),
            "cost_30d": claude.get("cost_30d", 0),
            "cost_30d_tokens": claude.get("cost_30d_tokens", "0"),
            "cost_window": "30d",
        },
    }


def _write_panel_status(claude, codex):
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        payload = _panel_status_payload(claude, codex)

        label_tmp = STATUS_LABEL_FILE.with_suffix(".txt.tmp")
        label_tmp.write_text(payload["label"] + "\n", encoding="utf-8")
        label_tmp.replace(STATUS_LABEL_FILE)

        json_tmp = STATUS_JSON_FILE.with_suffix(".json.tmp")
        json_tmp.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        json_tmp.replace(STATUS_JSON_FILE)
    except Exception as e:
        print(f"[QuotaHalo] Status write err: {e}", flush=True)


def refresh_once():
    claude_fetcher = ClaudeDataFetcher()
    codex_fetcher = CodexDataFetcher()

    try:
        claude_data = claude_fetcher.fetch_all()
    except Exception as e:
        print(f"[QuotaHalo] Claude refresh err: {e}", flush=True)
        claude_data = claude_fetcher.data

    try:
        codex_data = codex_fetcher.fetch()
    except Exception as e:
        print(f"[QuotaHalo] Codex refresh err: {e}", flush=True)
        codex_data = CodexDataFetcher._empty()

    _write_panel_status(claude_data, codex_data)
    print("[QuotaHalo] Refreshed once", flush=True)


# ─────────────────────────────────────────────

if __name__ == '__main__':
    if '--refresh-once' in sys.argv:
        refresh_once()
        sys.exit(0)
    if '--cleanup-claude-usage-queries' in sys.argv:
        dry_run = '--dry-run' in sys.argv
        moved = cleanup_claude_usage_query_history(dry_run=dry_run)
        action = "Would move" if dry_run else "Moved"
        print(f"[QuotaHalo] {action} {len(moved)} Claude usage query item(s)")
        sys.exit(0)

    print("QuotaHalo is now a headless GNOME usage refresher.")
    print("Run: python3 quota_halo_status.py --refresh-once")
