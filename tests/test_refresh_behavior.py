import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

import quota_halo_status as qhs


class RefreshBehaviorTests(unittest.TestCase):
    def test_claude_force_refresh_bypasses_fresh_usage_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp) / "quotahalo"
            status_dir.mkdir()
            cache_path = status_dir / "claude-usage-cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "provider": "Claude",
                        "plan": "Pro",
                        "updated": "Updated 00:00:00",
                        "updated_epoch": int(time.time()),
                        "session_used_pct": 11,
                        "session_reset": "1h",
                        "weekly_used_pct": 22,
                        "weekly_reset": "1d",
                        "source": "oauth",
                        "_cached_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )

            original_status_dir = qhs.STATUS_DIR
            original_cache_file = qhs.CLAUDE_USAGE_CACHE_FILE
            qhs.STATUS_DIR = status_dir
            qhs.CLAUDE_USAGE_CACHE_FILE = cache_path
            try:
                class Fetcher(qhs.ClaudeDataFetcher):
                    def __init__(self):
                        super().__init__()
                        self.oauth_calls = 0

                    def _fetch_oauth_api(self, force=False):
                        self.oauth_calls += 1
                        data = self._empty()
                        data.update(
                            {
                                "plan": "Pro",
                                "source": "oauth",
                                "session_used_pct": 44,
                                "weekly_used_pct": 55,
                            }
                        )
                        return data

                    def _fetch_jsonl(self):
                        return None

                    def _is_claude_installed(self):
                        return True

                fetcher = Fetcher()
                data = fetcher.fetch_all(force=True)
            finally:
                qhs.STATUS_DIR = original_status_dir
                qhs.CLAUDE_USAGE_CACHE_FILE = original_cache_file

            self.assertEqual(fetcher.oauth_calls, 1)
            self.assertEqual(data["session_used_pct"], 44)
            self.assertEqual(data["weekly_used_pct"], 55)

    def test_codex_uses_latest_rate_limit_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / ".codex"
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)

            old_file = sessions_dir / "old.jsonl"
            new_file = sessions_dir / "new.jsonl"
            now_epoch = int(time.time())
            old_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-10T08:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 90,
                                    "resets_at": now_epoch + 7200,
                                }
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            new_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-10T09:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 12,
                                    "resets_at": now_epoch + 3600,
                                }
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(old_file, (time.time() - 200, time.time() - 200))
            os.utime(new_file, (time.time(), time.time()))

            original_codex_dir = qhs.CodexDataFetcher.CODEX_DIR
            qhs.CodexDataFetcher.CODEX_DIR = codex_dir
            try:
                data = qhs.CodexDataFetcher().fetch()
            finally:
                qhs.CodexDataFetcher.CODEX_DIR = original_codex_dir

            self.assertEqual(data["source"], "sessions")
            self.assertEqual(data["session_used_pct"], 12)

    def test_copilot_refresh_updates_ui_after_subprocess_finishes(self):
        extension_path = (
            Path(__file__).resolve().parents[1]
            / "gnome-extension"
            / "quotahalo@local"
            / "extension.js"
        )
        text = extension_path.read_text(encoding="utf-8")
        match = re.search(
            r"_requestCopilotRefresh: function\(\) \{(?P<body>.*?)\n    \},\n\n    _update",
            text,
            re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("var self = this;", body)
        self.assertIn("self._update();", body)

    def test_usage_actions_row_is_reactive_for_nested_buttons(self):
        extension_path = (
            Path(__file__).resolve().parents[1]
            / "gnome-extension"
            / "quotahalo@local"
            / "extension.js"
        )
        text = extension_path.read_text(encoding="utf-8")
        match = re.search(
            r"function addUsageActionsControl\(.*?\) \{(?P<body>.*?)\n\}",
            text,
            re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertNotIn("PopupBaseMenuItem({ reactive: false })", body)


if __name__ == "__main__":
    unittest.main()
