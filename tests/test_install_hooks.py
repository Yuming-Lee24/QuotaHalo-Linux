import json
import tempfile
import unittest
from pathlib import Path

import install_hooks as ih


class InstallHooksTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self._tmp.name) / ".claude" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            json.dumps({"theme": "dark", "effortLevel": "high"}, indent=2),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _load(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_preserves_keys_and_adds_events(self):
        changed, command = ih.install(str(self.settings), "/repo")
        self.assertTrue(changed)
        data = self._load()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["effortLevel"], "high")
        for event in ih.HOOK_EVENTS:
            self.assertIn(event, data["hooks"])
            cmd = data["hooks"][event][0]["hooks"][0]["command"]
            self.assertIn("claude_session_hook.py", cmd)
            self.assertIn("/usr/bin/python3", cmd)
        self.assertTrue(
            self.settings.with_name("settings.json.quotahalo.bak").exists())
        # SubagentStop must NOT be registered (it can arrive after Stop and
        # would otherwise resurrect a finished session to "working").
        self.assertNotIn("SubagentStop", data["hooks"])

    def test_install_idempotent(self):
        ih.install(str(self.settings), "/repo")
        changed, _ = ih.install(str(self.settings), "/repo")
        self.assertFalse(changed)
        data = self._load()
        for event in ih.HOOK_EVENTS:
            self.assertEqual(len(data["hooks"][event]), 1)

    def test_uninstall_removes_only_ours(self):
        data = self._load()
        data.setdefault("hooks", {})["Stop"] = [
            {"matcher": "", "hooks": [{"type": "command", "command": "/bin/echo user-hook"}]}
        ]
        self.settings.write_text(json.dumps(data, indent=2), encoding="utf-8")

        ih.install(str(self.settings), "/repo")
        changed = ih.uninstall(str(self.settings))
        self.assertTrue(changed)

        data = self._load()
        for event in ih.HOOK_EVENTS:
            for group in data.get("hooks", {}).get(event, []):
                for hook in group.get("hooks", []):
                    self.assertNotIn("claude_session_hook.py", hook.get("command", ""))
        # the user's own Stop hook survives, sibling keys untouched
        stop_cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("/bin/echo user-hook", stop_cmds)
        self.assertEqual(data["theme"], "dark")

    def test_uninstall_without_hooks_is_noop(self):
        self.assertFalse(ih.uninstall(str(self.settings)))

    def test_command_quoting(self):
        cmd = ih.build_command("/usr/bin/python3", "/path with space/claude_session_hook.py")
        # paths with spaces must stay a single shell token
        self.assertIn("claude_session_hook.py", cmd)
        self.assertTrue("'" in cmd or "\\" in cmd)


if __name__ == "__main__":
    unittest.main()
