import json
import tempfile
import unittest
from pathlib import Path

import codex_session_hook as chs


class CodexHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = chs.SESSIONS_DIR
        chs.SESSIONS_DIR = Path(self._tmp.name) / "codex-sessions"

    def tearDown(self):
        chs.SESSIONS_DIR = self._orig
        self._tmp.cleanup()

    def _read(self, sid):
        return json.loads((chs.SESSIONS_DIR / (sid + ".json")).read_text(encoding="utf-8"))

    def test_lifecycle_transitions(self):
        chs.handle_event({"hook_event_name": "SessionStart", "session_id": "c1",
                          "cwd": "/home/u/roboxai", "model": "gpt-5.5"}, now=10)
        rec = self._read("c1")
        self.assertEqual(rec["state"], "awaiting_reply")
        self.assertEqual(rec["project"], "roboxai")
        self.assertEqual(rec["provider"], "codex")

        chs.handle_event({"hook_event_name": "UserPromptSubmit", "session_id": "c1"}, now=11)
        self.assertEqual(self._read("c1")["state"], "working")

        chs.handle_event({"hook_event_name": "PreToolUse", "session_id": "c1",
                          "tool_name": "apply_patch"}, now=12)
        rec = self._read("c1")
        self.assertEqual(rec["state"], "working")
        self.assertEqual(rec["current_tool"], "apply_patch")

        # Codex's approval prompt -> needs_input (the "等待你批准" state).
        chs.handle_event({"hook_event_name": "PermissionRequest", "session_id": "c1",
                          "tool_name": "Bash"}, now=13)
        self.assertEqual(self._read("c1")["state"], "needs_input")

        chs.handle_event({"hook_event_name": "Stop", "session_id": "c1"}, now=14)
        rec = self._read("c1")
        self.assertEqual(rec["state"], "awaiting_reply")
        self.assertEqual(rec["current_tool"], "")

    def test_subagentstop_after_stop_keeps_state(self):
        chs.handle_event({"hook_event_name": "Stop", "session_id": "c2"}, now=10)
        self.assertEqual(self._read("c2")["state"], "awaiting_reply")
        chs.handle_event({"hook_event_name": "SubagentStop", "session_id": "c2"}, now=12)
        self.assertEqual(self._read("c2")["state"], "awaiting_reply")

    def test_dead_process_session_dropped(self):
        chs.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        (chs.SESSIONS_DIR / "c3.json").write_text(json.dumps(
            {"session_id": "c3", "state": "working",
             "updated_epoch": 1_781_000_000, "proc_pid": 2147483647}), encoding="utf-8")
        chs._prune_and_reconcile(1_781_000_010)  # 2147483647 is not a live pid
        self.assertFalse((chs.SESSIONS_DIR / "c3.json").exists())

    def test_no_session_id_is_noop(self):
        self.assertIsNone(chs.handle_event({"hook_event_name": "Stop"}, now=1))


if __name__ == "__main__":
    unittest.main()
