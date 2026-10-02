import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import cc_tg_notify as hook


def transcript(tmp_path, records):
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return str(path)


class LastPromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_returns_latest_prompt(self):
        path = transcript(
            self.tmp_path,
            [
                {"type": "last-prompt", "lastPrompt": "first"},
                {"type": "assistant", "message": {}},
                {"type": "last-prompt", "lastPrompt": "second"},
            ],
        )
        self.assertEqual(hook.last_prompt(path), "second")

    def test_collapses_whitespace(self):
        path = transcript(
            self.tmp_path, [{"type": "last-prompt", "lastPrompt": "  add\n\n  tests  "}]
        )
        self.assertEqual(hook.last_prompt(path), "add tests")

    def test_truncates_long_prompt(self):
        path = transcript(
            self.tmp_path, [{"type": "last-prompt", "lastPrompt": "x" * 500}]
        )
        prompt = hook.last_prompt(path)
        self.assertEqual(len(prompt), hook.PROMPT_LIMIT)
        self.assertTrue(prompt.endswith("..."))

    def test_skips_malformed_lines(self):
        path = self.tmp_path / "broken.jsonl"
        path.write_text(
            '{"type":"last-prompt", broken\n{"type":"last-prompt","lastPrompt":"ok"}\n',
            encoding="utf-8",
        )
        self.assertEqual(hook.last_prompt(str(path)), "ok")

    def test_returns_none_without_prompt_records(self):
        path = transcript(self.tmp_path, [{"type": "assistant", "message": {}}])
        self.assertIsNone(hook.last_prompt(path))

    def test_returns_none_without_transcript_path(self):
        self.assertIsNone(hook.last_prompt(None))

    def test_returns_none_when_transcript_is_missing(self):
        self.assertIsNone(hook.last_prompt(str(self.tmp_path / "absent.jsonl")))

    def test_returns_none_when_transcript_is_not_utf8(self):
        path = self.tmp_path / "binary.jsonl"
        path.write_bytes(b'{"type":"last-prompt","lastPrompt":"\xff\xfe"}\n')
        self.assertIsNone(hook.last_prompt(str(path)))

    def test_ignores_prompt_that_is_not_a_string(self):
        path = transcript(self.tmp_path, [{"type": "last-prompt", "lastPrompt": {"text": "x"}}])
        self.assertIsNone(hook.last_prompt(path))


class GitBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_returns_branch_of_repository_without_commits(self):
        subprocess.run(["git", "init", "-b", "feature", "-q", str(self.tmp_path)], check=True)
        self.assertEqual(hook.git_branch(str(self.tmp_path)), "feature")

    def test_returns_none_outside_repository(self):
        with mock.patch.object(hook.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], returncode=128, stdout="")
            self.assertIsNone(hook.git_branch("/tmp"))

    def test_returns_none_on_detached_head(self):
        with mock.patch.object(hook.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], returncode=0, stdout="\n")
            self.assertIsNone(hook.git_branch("/tmp"))

    def test_returns_none_when_git_times_out(self):
        with mock.patch.object(
            hook.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 2)
        ):
            self.assertIsNone(hook.git_branch("/tmp"))


class BuildPayloadTest(unittest.TestCase):
    def test_takes_project_from_working_directory(self):
        with mock.patch.object(hook, "git_branch", return_value="main"):
            payload = hook.build_payload("idle", {"cwd": "/Users/max/projects/notifier/"})

        self.assertEqual(payload["project"], "notifier")
        self.assertEqual(payload["branch"], "main")
        self.assertEqual(payload["event"], "idle")
        self.assertIsNone(payload["prompt"])


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_rejects_config_without_secret(self):
        path = self.tmp_path / "config.json"
        path.write_text(json.dumps({"url": "https://example.com"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "secret"):
            hook.load_config(path)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.tmp_path / "hook.log"
        self.enterContext(mock.patch.object(hook, "LOG_PATH", self.log))
        self.enterContext(mock.patch.object(sys, "stdin", io.StringIO('{"cwd": "/tmp"}')))

    def test_reports_unknown_event_without_reading_stdin(self):
        with mock.patch.object(sys, "argv", ["cc_tg_notify.py"]):
            self.assertEqual(hook.main(), 0)
        self.assertIn("unknown event", self.log.read_text(encoding="utf-8"))

    def test_survives_missing_config(self):
        missing = self.tmp_path / "absent.json"
        with mock.patch.object(hook, "CONFIG_PATH", missing), mock.patch.object(
            sys, "argv", ["cc_tg_notify.py", "idle"]
        ):
            self.assertEqual(hook.main(), 0)
        self.assertIn("FileNotFoundError", self.log.read_text(encoding="utf-8"))

    def test_sends_payload_for_a_valid_event(self):
        config = self.tmp_path / "config.json"
        config.write_text(
            json.dumps({"url": "https://example.com", "secret": "s"}), encoding="utf-8"
        )
        with mock.patch.object(hook, "CONFIG_PATH", config), mock.patch.object(
            sys, "argv", ["cc_tg_notify.py", "permission"]
        ), mock.patch.object(hook, "git_branch", return_value="main"), mock.patch.object(
            hook, "send"
        ) as send:
            self.assertEqual(hook.main(), 0)

        send.assert_called_once_with(
            {"url": "https://example.com", "secret": "s"},
            {"event": "permission", "project": "tmp", "branch": "main", "prompt": None},
        )
        self.assertFalse(self.log.exists())

    def test_survives_unreachable_function(self):
        config = self.tmp_path / "config.json"
        config.write_text(
            json.dumps({"url": "https://example.com", "secret": "s"}), encoding="utf-8"
        )
        with mock.patch.object(hook, "CONFIG_PATH", config), mock.patch.object(
            sys, "argv", ["cc_tg_notify.py", "idle"]
        ), mock.patch.object(hook, "send", side_effect=OSError("connection refused")):
            self.assertEqual(hook.main(), 0)
        self.assertIn("connection refused", self.log.read_text(encoding="utf-8"))


class SendTest(unittest.TestCase):
    def test_posts_payload_with_secret_header(self):
        config = {"url": "https://functions.example/notify", "secret": "topsecret"}
        payload = {"event": "idle", "project": "notifier", "branch": None, "prompt": None}

        with mock.patch.object(hook.urllib.request, "urlopen") as urlopen:
            hook.send(config, payload)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, config["url"])
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["x-notify-secret"], "topsecret")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], hook.REQUEST_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
