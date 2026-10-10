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


def launch_record(agent_id):
    return {
        "type": "user",
        "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": agent_id},
    }


def task_notification(agent_ids, status="completed"):
    tags = "".join(f"<task-id>{agent_id}</task-id>" for agent_id in agent_ids)
    return f"<task-notification>{tags}<status>{status}</status></task-notification>"


def notification_record(body, record_type="user"):
    if record_type == "queue-operation":
        return {"type": "queue-operation", "operation": "remove", "content": body}
    if record_type == "attachment":
        return {"type": "attachment", "attachment": {"prompt": body}}
    return {"type": "user", "message": {"content": body}}


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


class RunningAgentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def running(self, records):
        return hook.running_agents(transcript(self.tmp_path, records))

    def test_reports_agent_that_has_not_finished(self):
        self.assertEqual(self.running([launch_record("a1")]), {"a1"})

    def test_clears_agent_after_each_notification_shape(self):
        for record_type in ("user", "queue-operation", "attachment"):
            with self.subTest(record_type=record_type):
                records = [
                    launch_record("a1"),
                    notification_record(task_notification(["a1"]), record_type),
                ]
                self.assertEqual(self.running(records), set())

    def test_clears_agent_on_every_terminal_status(self):
        for status in ("completed", "failed", "killed", "stopped"):
            with self.subTest(status=status):
                records = [
                    launch_record("a1"),
                    notification_record(task_notification(["a1"], status)),
                ]
                self.assertEqual(self.running(records), set())

    def test_clears_every_agent_of_a_recovery_notification(self):
        records = [
            launch_record("a1"),
            launch_record("a2"),
            notification_record(task_notification(["a1", "a2"], "stopped")),
        ]
        self.assertEqual(self.running(records), set())

    def test_keeps_agent_when_notification_carries_no_task_id(self):
        body = "<task-notification>Goal check-in: background work still running</task-notification>"
        records = [launch_record("a1"), notification_record(body)]
        self.assertEqual(self.running(records), {"a1"})

    def test_keeps_agent_when_notification_carries_no_status(self):
        body = "<task-notification><task-id>a1</task-id><event>done</event></task-notification>"
        records = [launch_record("a1"), notification_record(body)]
        self.assertEqual(self.running(records), {"a1"})

    def test_skips_records_that_are_not_objects(self):
        path = self.tmp_path / "shapes.jsonl"
        path.write_text(
            '["async_launched"]\n"async_launched"\n' + json.dumps(launch_record("a1")) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(hook.running_agents(str(path)), {"a1"})

    def test_skips_notification_section_that_is_not_an_object(self):
        records = [launch_record("a1"), {"type": "user", "message": "not an object"}]
        self.assertEqual(self.running(records), {"a1"})

    def test_ignores_launch_without_agent_id(self):
        records = [{"type": "user", "toolUseResult": {"status": "async_launched"}}]
        self.assertEqual(self.running(records), set())

    def test_ignores_synchronous_agent_result(self):
        records = [{"type": "user", "toolUseResult": {"status": "completed", "agentId": "a1"}}]
        self.assertEqual(self.running(records), set())

    def test_skips_malformed_lines(self):
        path = self.tmp_path / "broken.jsonl"
        path.write_text(
            '{"type":"user","toolUseResult":{"status":"async_launched","agentId":"a1"}\n'
            + json.dumps(launch_record("a2"))
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(hook.running_agents(str(path)), {"a2"})

    def test_returns_empty_without_transcript_path(self):
        self.assertEqual(hook.running_agents(None), set())

    def test_returns_empty_when_transcript_is_missing(self):
        self.assertEqual(hook.running_agents(str(self.tmp_path / "absent.jsonl")), set())

    def test_returns_empty_when_transcript_is_not_utf8(self):
        path = self.tmp_path / "binary.jsonl"
        path.write_bytes(b'{"toolUseResult":{"status":"async_launched"}}\xff\xfe\n')
        self.assertEqual(hook.running_agents(str(path)), set())


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

    def config_file(self):
        config = self.tmp_path / "config.json"
        config.write_text(
            json.dumps({"url": "https://example.com", "secret": "s"}), encoding="utf-8"
        )
        return config

    def hook_input(self, records):
        payload = {"cwd": "/tmp", "transcript_path": transcript(self.tmp_path, records)}
        return io.StringIO(json.dumps(payload))

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

    def test_suppresses_idle_while_a_background_agent_runs(self):
        with mock.patch.object(hook, "CONFIG_PATH", self.config_file()), mock.patch.object(
            sys, "argv", ["cc_tg_notify.py", "idle"]
        ), mock.patch.object(sys, "stdin", self.hook_input([launch_record("a1")])), mock.patch.object(
            hook, "send"
        ) as send:
            self.assertEqual(hook.main(), 0)

        send.assert_not_called()
        self.assertIn("suppressed", self.log.read_text(encoding="utf-8"))
        self.assertIn("a1", self.log.read_text(encoding="utf-8"))

    def test_sends_idle_after_a_background_agent_finished(self):
        records = [launch_record("a1"), notification_record(task_notification(["a1"]))]
        with mock.patch.object(hook, "CONFIG_PATH", self.config_file()), mock.patch.object(
            sys, "argv", ["cc_tg_notify.py", "idle"]
        ), mock.patch.object(sys, "stdin", self.hook_input(records)), mock.patch.object(
            hook, "git_branch", return_value=None
        ), mock.patch.object(hook, "send") as send:
            self.assertEqual(hook.main(), 0)

        send.assert_called_once()
        self.assertFalse(self.log.exists())

    def test_does_not_suppress_permission_prompt(self):
        with mock.patch.object(hook, "CONFIG_PATH", self.config_file()), mock.patch.object(
            sys, "argv", ["cc_tg_notify.py", "permission"]
        ), mock.patch.object(sys, "stdin", self.hook_input([launch_record("a1")])), mock.patch.object(
            hook, "git_branch", return_value=None
        ), mock.patch.object(hook, "send") as send:
            self.assertEqual(hook.main(), 0)

        send.assert_called_once()
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
