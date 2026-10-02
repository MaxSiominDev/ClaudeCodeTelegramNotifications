import base64
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "function"))

import index

SECRET = "topsecret"
ENVIRONMENT = {
    "NOTIFY_SECRET": SECRET,
    "TELEGRAM_BOT_TOKEN": "42:token",
    "TELEGRAM_CHAT_ID": "100500",
}


def sent_headers(request):
    return {name.lower(): value for name, value in request.header_items()}


def request(payload, secret=SECRET, headers=None, body=None):
    if headers is None:
        headers = {"X-Notify-Secret": secret} if secret is not None else {}
    return {
        "headers": headers,
        "body": json.dumps(payload) if body is None else body,
    }


class BuildMessageTest(unittest.TestCase):
    def test_includes_project_branch_and_prompt(self):
        message = index.build_message(
            {"event": "idle", "project": "notifier", "branch": "main", "prompt": "add tests"}
        )
        self.assertEqual(
            message,
            "Claude Code · notifier · main\n"
            "Finished responding, waiting for you\n"
            "\n"
            "> add tests",
        )

    def test_omits_branch_and_prompt_when_absent(self):
        message = index.build_message(
            {"event": "permission", "project": "notifier", "branch": None, "prompt": None}
        )
        self.assertEqual(
            message, "Claude Code · notifier\nWaiting for permission to use a tool"
        )

    def test_truncates_to_telegram_limit(self):
        message = index.build_message(
            {"event": "idle", "project": "p", "branch": None, "prompt": "x" * 5000}
        )
        self.assertEqual(len(message), index.MESSAGE_LIMIT)
        self.assertTrue(message.endswith("..."))


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(index.os.environ, ENVIRONMENT, clear=False))
        self.telegram = self.enterContext(mock.patch.object(index, "send_to_telegram"))

    def payload(self, **overrides):
        payload = {"event": "idle", "project": "notifier", "branch": "main", "prompt": None}
        payload.update(overrides)
        return payload

    def test_sends_message_to_telegram(self):
        result = index.handler(request(self.payload()), None)

        self.assertEqual(result["statusCode"], 200)
        config, text = self.telegram.call_args.args
        self.assertEqual(config["TELEGRAM_CHAT_ID"], "100500")
        self.assertEqual(
            text, "Claude Code · notifier · main\nFinished responding, waiting for you"
        )

    def test_accepts_lowercase_header_name(self):
        result = index.handler(
            request(self.payload(), headers={"x-notify-secret": SECRET}), None
        )
        self.assertEqual(result["statusCode"], 200)

    def test_decodes_base64_body_with_cyrillic_prompt(self):
        event = request(self.payload(prompt="добавь тесты"))
        event["body"] = base64.b64encode(event["body"].encode("utf-8")).decode("ascii")
        event["isBase64Encoded"] = True

        self.assertEqual(index.handler(event, None)["statusCode"], 200)
        self.assertEqual(
            self.telegram.call_args.args[1],
            "Claude Code · notifier · main\nFinished responding, waiting for you\n\n> добавь тесты",
        )

    def test_rejects_non_ascii_secret_header(self):
        result = index.handler(request(self.payload(), secret="парол"), None)

        self.assertEqual(result["statusCode"], 403)
        self.telegram.assert_not_called()

    def test_rejects_wrong_secret(self):
        result = index.handler(request(self.payload(), secret="guess"), None)

        self.assertEqual(result["statusCode"], 403)
        self.telegram.assert_not_called()

    def test_rejects_missing_secret_header(self):
        result = index.handler(request(self.payload(), headers={}), None)

        self.assertEqual(result["statusCode"], 403)
        self.telegram.assert_not_called()

    def test_rejects_unknown_event(self):
        result = index.handler(request(self.payload(event="compacted")), None)

        self.assertEqual(result["statusCode"], 400)
        self.telegram.assert_not_called()

    def test_rejects_empty_project(self):
        result = index.handler(request(self.payload(project="")), None)

        self.assertEqual(result["statusCode"], 400)
        self.telegram.assert_not_called()

    def test_rejects_malformed_body(self):
        result = index.handler(request(None, body="not json"), None)

        self.assertEqual(result["statusCode"], 400)
        self.telegram.assert_not_called()

    def test_reports_telegram_failure(self):
        self.telegram.side_effect = OSError("timed out")

        self.assertEqual(index.handler(request(self.payload()), None)["statusCode"], 502)


class SettingsTest(unittest.TestCase):
    def test_refuses_to_authorise_with_an_empty_secret(self):
        environment = dict(ENVIRONMENT, NOTIFY_SECRET="")
        with mock.patch.dict(index.os.environ, environment, clear=True), mock.patch.object(
            index, "send_to_telegram"
        ) as telegram:
            result = index.handler(request({}, headers={}), None)

        self.assertEqual(result["statusCode"], 500)
        telegram.assert_not_called()

    def test_reports_missing_variables(self):
        with mock.patch.dict(index.os.environ, {"NOTIFY_SECRET": SECRET}, clear=True):
            result = index.handler(request({}), None)

        self.assertEqual(result["statusCode"], 500)


class ApiTargetTest(unittest.TestCase):
    def config(self, **overrides):
        with mock.patch.dict(index.os.environ, dict(ENVIRONMENT, **overrides), clear=True):
            return index.settings()

    def send(self, config, text="готово, ждёт тебя"):
        with mock.patch.object(index.urllib.request, "urlopen") as urlopen:
            index.send_to_telegram(config, text)
        return urlopen.call_args.args[0]

    def test_talks_to_telegram_by_default(self):
        request = self.send(self.config())

        self.assertEqual(
            request.full_url, "https://api.telegram.org/bot42:token/sendMessage"
        )
        self.assertNotIn(index.API_TOKEN_HEADER.lower(), sent_headers(request))
        self.assertEqual(
            urllib.parse.parse_qs(request.data.decode("utf-8")),
            {"chat_id": ["100500"], "text": ["готово, ждёт тебя"]},
        )

    def test_talks_to_a_self_hosted_server_with_its_gateway_token(self):
        request = self.send(
            self.config(
                TELEGRAM_API_URL="https://tg.example.com/", TELEGRAM_API_TOKEN="gateway"
            )
        )

        self.assertEqual(
            request.full_url, "https://tg.example.com/bot42:token/sendMessage"
        )
        self.assertEqual(sent_headers(request)[index.API_TOKEN_HEADER.lower()], "gateway")

    def test_falls_back_to_telegram_on_an_empty_api_url(self):
        self.assertEqual(
            self.config(TELEGRAM_API_URL="")["TELEGRAM_API_URL"], index.DEFAULT_API_URL
        )

    def test_adds_scheme_to_a_bare_host(self):
        self.assertEqual(
            self.config(TELEGRAM_API_URL="tg.example.com")["TELEGRAM_API_URL"],
            "https://tg.example.com",
        )


if __name__ == "__main__":
    unittest.main()
