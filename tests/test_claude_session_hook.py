import json
import tempfile
import unittest
from pathlib import Path

import claude_session_hook as chs


class SessionHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._orig_status = chs.STATUS_DIR
        self._orig_sessions = chs.SESSIONS_DIR
        self._orig_claude_sessions = chs.CLAUDE_SESSIONS_DIR
        chs.STATUS_DIR = base / "quotahalo"
        chs.SESSIONS_DIR = chs.STATUS_DIR / "sessions"
        chs.CLAUDE_SESSIONS_DIR = base / "claude_sessions"  # empty unless a test populates it

    def tearDown(self):
        chs.STATUS_DIR = self._orig_status
        chs.SESSIONS_DIR = self._orig_sessions
        chs.CLAUDE_SESSIONS_DIR = self._orig_claude_sessions
        self._tmp.cleanup()

    def _file(self, sid):
        return chs.SESSIONS_DIR / (sid + ".json")

    def _read(self, sid):
        return json.loads(self._file(sid).read_text(encoding="utf-8"))

    def test_lifecycle_transitions(self):
        now = 1000
        chs.handle_event({
            "hook_event_name": "SessionStart", "session_id": "s1",
            "cwd": "/home/u/proj", "model": "claude-opus-4-8",
        }, now=now)
        rec = self._read("s1")
        self.assertEqual(rec["state"], "awaiting_reply")
        self.assertEqual(rec["project"], "proj")
        self.assertEqual(rec["model"], "claude-opus-4-8")
        self.assertEqual(rec["started_epoch"], now)

        chs.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "s1"}, now=now + 5)
        self.assertEqual(self._read("s1")["state"], "working")

        chs.handle_event({
            "hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
        }, now=now + 6)
        rec = self._read("s1")
        self.assertEqual(rec["state"], "working")
        self.assertEqual(rec["current_tool"], "Bash")
        self.assertEqual(rec["started_epoch"], now)  # preserved across events

        chs.handle_event({
            "hook_event_name": "Notification", "session_id": "s1",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission to use Bash",
        }, now=now + 7)
        rec = self._read("s1")
        self.assertEqual(rec["state"], "needs_input")
        self.assertIn("permission", rec["detail"].lower())

        chs.handle_event({"hook_event_name": "Stop", "session_id": "s1"}, now=now + 8)
        rec = self._read("s1")
        self.assertEqual(rec["state"], "awaiting_reply")
        self.assertEqual(rec["current_tool"], "")

        chs.handle_event({
            "hook_event_name": "SessionEnd", "session_id": "s1", "reason": "clear",
        }, now=now + 9)
        self.assertFalse(self._file("s1").exists())

    def test_idle_notification_maps_to_idle(self):
        chs.handle_event({
            "hook_event_name": "Notification", "session_id": "s3",
            "notification_type": "idle_prompt",
            "message": "Claude is waiting for your input",
        }, now=10)
        self.assertEqual(self._read("s3")["state"], "idle")

    def test_subagentstop_after_stop_does_not_resurrect(self):
        # SubagentStop can arrive after the main Stop (background subagents);
        # it must never flip a finished session back to "working".
        chs.handle_event({"hook_event_name": "Stop", "session_id": "sub1"}, now=100)
        self.assertEqual(self._read("sub1")["state"], "awaiting_reply")
        chs.handle_event({"hook_event_name": "SubagentStop", "session_id": "sub1"}, now=103)
        self.assertEqual(self._read("sub1")["state"], "awaiting_reply")
        # ...and must not resurrect an idle (away) session either.
        chs.handle_event({"hook_event_name": "Notification", "session_id": "sub1",
                          "notification_type": "idle_prompt",
                          "message": "waiting for your input"}, now=160)
        self.assertEqual(self._read("sub1")["state"], "idle")
        chs.handle_event({"hook_event_name": "SubagentStop", "session_id": "sub1"}, now=200)
        self.assertEqual(self._read("sub1")["state"], "idle")

    def test_state_since_only_moves_on_change(self):
        chs.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "s2"}, now=100)
        since1 = self._read("s2")["state_since_epoch"]
        self.assertEqual(since1, 100)
        chs.handle_event({
            "hook_event_name": "PreToolUse", "session_id": "s2", "tool_name": "Read",
        }, now=150)
        self.assertEqual(self._read("s2")["state_since_epoch"], since1)  # still working
        chs.handle_event({"hook_event_name": "Stop", "session_id": "s2"}, now=200)
        self.assertEqual(self._read("s2")["state_since_epoch"], 200)  # awaiting_reply now

    def test_prune_removes_stale_files(self):
        chs.handle_event({"hook_event_name": "Stop", "session_id": "old"}, now=1000)
        self.assertTrue(self._file("old").exists())
        later = 1000 + chs.STALE_SECONDS["idle"] + 100
        chs.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "new"}, now=later)
        self.assertFalse(self._file("old").exists())
        self.assertTrue(self._file("new").exists())

    def test_working_pruned_more_conservatively(self):
        # A "working" session quiet past the awaiting_reply cap must survive;
        # an "awaiting_reply" session quiet for the same span must be reaped.
        chs.handle_event({"hook_event_name": "PreToolUse", "session_id": "busy",
                          "tool_name": "Bash"}, now=1000)
        chs.handle_event({"hook_event_name": "Stop", "session_id": "done"}, now=1000)
        later = 1000 + chs.STALE_SECONDS["awaiting_reply"] + 100  # past short cap, under working cap
        chs.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "trigger"}, now=later)
        self.assertTrue(self._file("busy").exists())
        self.assertFalse(self._file("done").exists())

    def test_unsafe_session_id_has_no_traversal(self):
        chs.handle_event({"hook_event_name": "SessionStart",
                          "session_id": "../../evil", "cwd": "/x"}, now=1)
        files = list(chs.SESSIONS_DIR.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, chs.SESSIONS_DIR)

    def test_title_from_claude_sessions_metadata(self):
        cs = Path(self._tmp.name) / "claude_sessions"
        cs.mkdir()
        orig = chs.CLAUDE_SESSIONS_DIR
        chs.CLAUDE_SESSIONS_DIR = cs
        try:
            (cs / "111.json").write_text(json.dumps(
                {"sessionId": "sX", "name": "old-title", "updatedAt": 100}), encoding="utf-8")
            (cs / "222.json").write_text(json.dumps(
                {"sessionId": "sX", "name": "fix-login-bug", "updatedAt": 200}), encoding="utf-8")
            (cs / "333.json").write_text(json.dumps(
                {"sessionId": "other", "name": "unrelated", "updatedAt": 999}), encoding="utf-8")
            chs.handle_event({"hook_event_name": "UserPromptSubmit",
                              "session_id": "sX", "cwd": "/home/u/myrepo"}, now=10)
            rec = self._read("sX")
            self.assertEqual(rec["title"], "fix-login-bug")  # most recently updated match
            self.assertEqual(rec["project"], "myrepo")        # dir kept as fallback
        finally:
            chs.CLAUDE_SESSIONS_DIR = orig

    def test_title_falls_back_to_transcript_aititle(self):
        cs = Path(self._tmp.name) / "no_meta"
        cs.mkdir()
        chs.CLAUDE_SESSIONS_DIR = cs  # no pid metadata -> use the transcript
        tp = Path(self._tmp.name) / "t.jsonl"
        tp.write_text("\n".join([
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
            json.dumps({"type": "ai-title", "aiTitle": "early sentence title", "sessionId": "sZ"}),
            json.dumps({"type": "assistant", "message": {}}),
            json.dumps({"type": "ai-title", "aiTitle": "final-slug-title", "sessionId": "sZ"}),
        ]) + "\n", encoding="utf-8")
        chs.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "sZ",
                          "cwd": "/home/u/proj", "transcript_path": str(tp)}, now=10)
        self.assertEqual(self._read("sZ")["title"], "final-slug-title")  # last ai-title wins

    def test_pid_name_preferred_over_transcript(self):
        cs = Path(self._tmp.name) / "with_meta"
        cs.mkdir()
        (cs / "1.json").write_text(json.dumps(
            {"sessionId": "sW", "name": "pid-name", "updatedAt": 5}), encoding="utf-8")
        chs.CLAUDE_SESSIONS_DIR = cs
        tp = Path(self._tmp.name) / "w.jsonl"
        tp.write_text(json.dumps(
            {"type": "ai-title", "aiTitle": "transcript-title", "sessionId": "sW"}) + "\n",
            encoding="utf-8")
        chs.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "sW",
                          "cwd": "/home/u/proj", "transcript_path": str(tp)}, now=10)
        self.assertEqual(self._read("sW")["title"], "pid-name")

    def test_no_title_without_metadata(self):
        cs = Path(self._tmp.name) / "empty_claude_sessions"
        cs.mkdir()
        orig = chs.CLAUDE_SESSIONS_DIR
        chs.CLAUDE_SESSIONS_DIR = cs
        try:
            chs.handle_event({"hook_event_name": "UserPromptSubmit",
                              "session_id": "sY", "cwd": "/home/u/repo2"}, now=10)
            rec = self._read("sY")
            self.assertNotIn("title", rec)
            self.assertEqual(rec["project"], "repo2")
        finally:
            chs.CLAUDE_SESSIONS_DIR = orig

    def test_reconcile_stuck_working_when_claude_idle(self):
        import os
        cs = Path(self._tmp.name) / "cs"
        cs.mkdir()
        chs.CLAUDE_SESSIONS_DIR = cs
        chs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        now = 1_781_000_000
        # our cache is stuck at "working"
        (chs.SESSIONS_DIR / "B.json").write_text(json.dumps(
            {"session_id": "B", "state": "working", "updated_epoch": now - 5,
             "state_since_epoch": now - 400}), encoding="utf-8")
        # Claude says session B is idle (300s ago); use this live pid so it's "alive"
        (cs / (str(os.getpid()) + ".json")).write_text(json.dumps(
            {"sessionId": "B", "status": "idle", "statusUpdatedAt": (now - 300) * 1000}),
            encoding="utf-8")
        chs._prune_and_reconcile(now)
        rec = json.loads((chs.SESSIONS_DIR / "B.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["state"], "idle")  # idle >60s -> away
        self.assertEqual(rec["state_since_epoch"], now - 300)

    def test_reconcile_recent_idle_is_awaiting_reply(self):
        import os
        cs = Path(self._tmp.name) / "cs"
        cs.mkdir()
        chs.CLAUDE_SESSIONS_DIR = cs
        chs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        now = 1_781_000_000
        (chs.SESSIONS_DIR / "B.json").write_text(json.dumps(
            {"session_id": "B", "state": "working", "updated_epoch": now - 5}), encoding="utf-8")
        (cs / (str(os.getpid()) + ".json")).write_text(json.dumps(
            {"sessionId": "B", "status": "idle", "statusUpdatedAt": (now - 20) * 1000}),
            encoding="utf-8")
        chs._prune_and_reconcile(now)
        rec = json.loads((chs.SESSIONS_DIR / "B.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["state"], "awaiting_reply")  # idle <60s -> just finished

    def test_reconcile_leaves_busy_session_working(self):
        import os
        cs = Path(self._tmp.name) / "cs"
        cs.mkdir()
        chs.CLAUDE_SESSIONS_DIR = cs
        chs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        now = 1_781_000_000
        (chs.SESSIONS_DIR / "B.json").write_text(json.dumps(
            {"session_id": "B", "state": "working", "updated_epoch": now - 5}), encoding="utf-8")
        (cs / (str(os.getpid()) + ".json")).write_text(json.dumps(
            {"sessionId": "B", "status": "busy", "statusUpdatedAt": (now - 300) * 1000}),
            encoding="utf-8")
        chs._prune_and_reconcile(now)
        rec = json.loads((chs.SESSIONS_DIR / "B.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["state"], "working")  # Claude still busy -> untouched

    def test_dead_process_session_is_dropped(self):
        cs = Path(self._tmp.name) / "cs"
        cs.mkdir()
        chs.CLAUDE_SESSIONS_DIR = cs
        chs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        now = 1_781_000_000
        (chs.SESSIONS_DIR / "C.json").write_text(json.dumps(
            {"session_id": "C", "state": "working", "updated_epoch": now - 5}), encoding="utf-8")
        # pid 2147483647 is not a live process
        (cs / "2147483647.json").write_text(json.dumps(
            {"sessionId": "C", "status": "busy", "statusUpdatedAt": now * 1000}), encoding="utf-8")
        chs._prune_and_reconcile(now)
        self.assertFalse((chs.SESSIONS_DIR / "C.json").exists())

    def test_no_session_id_is_noop(self):
        self.assertIsNone(chs.handle_event({}, now=1))
        self.assertIsNone(chs.handle_event({"hook_event_name": "Stop"}, now=1))
        self.assertEqual(list(chs.SESSIONS_DIR.glob("*.json")) if chs.SESSIONS_DIR.exists() else [], [])


if __name__ == "__main__":
    unittest.main()
