# Setup

The setup has two parts: a Cloud Function and a hook on your machine. Use your existing Telegram bot token and chat ID.

## 1. Cloud Function

Open Cloud Functions in the [Yandex Cloud console](https://console.yandex.cloud/) and create a function.

Use these settings.

- Runtime `python312`
- Entry point `index.handler`
- Upload `function/index.py` as `index.py`
- An empty `requirements.txt` is enough since there are no dependencies
- Timeout 30 seconds
- Memory 128 MB

Add the following environment variables.

| Name                 | Value                                                                    |
| -------------------- | ------------------------------------------------------------------------ |
| `NOTIFY_SECRET`      | Any long random string. You can generate one with `openssl rand -hex 32` |
| `TELEGRAM_BOT_TOKEN` | The token from BotFather                                                 |
| `TELEGRAM_CHAT_ID`   | Your existing Telegram chat ID                                                  |
| `TELEGRAM_API_URL`   | Optional. Base URL of a self-hosted Bot API server                       |
| `TELEGRAM_API_TOKEN` | Optional. Token required by the gateway in front of that server          |

### Using a self-hosted Bot API server

`api.telegram.org` is not reachable from every network.

A self-hosted [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) server uses the same Bot API, so the function only needs two extra variables.

- `TELEGRAM_API_URL` is the base URL, for example `https://tg.example.com`. If it is empty, the function uses `https://api.telegram.org`.
- `TELEGRAM_API_TOKEN` is sent in the `X-Api-Token` header. Set it only if the server is behind a gateway that requires one.

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` stay the same in both cases.

Enable the public function option. The local hook calls the function without Yandex Cloud credentials.

Access is still protected by `NOTIFY_SECRET`. The hook sends it in the `X-Notify-Secret` header. Requests with the wrong secret get a `403`, and nothing is sent to Telegram.

Copy the function invoke URL. It should look similar to this.

`https://functions.yandexcloud.net/d4e...`

Test the whole path before moving on.
```bash
curl -X POST "<FUNCTION_URL>" \
  -H "X-Notify-Secret: <NOTIFY_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"event":"idle","project":"setup-check","branch":"main","prompt":"hello"}'
```

You should get a Telegram message.

## 2. Hook

Install the hook and copy its config.
```bash
mkdir -p ~/.claude/hooks
cp hooks/cc_tg_notify.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/cc_tg_notify.py
cp hooks/config.example.json ~/.claude/cc-tg-notify.json
chmod 600 ~/.claude/cc-tg-notify.json
```

Open `~/.claude/cc-tg-notify.json` and add the function URL and secret.

The config lives outside the repository intentionally, which keeps the secret out of commits.

Now register both events in `~/.claude/settings.json`.
```json
{
  "hooks": {
    "Notification": [
      {
        "matcher": "idle_prompt",
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME\"/.claude/hooks/cc_tg_notify.py idle",
            "async": true,
            "timeout": 15
          }
        ]
      },
      {
        "matcher": "permission_prompt",
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME\"/.claude/hooks/cc_tg_notify.py permission",
            "async": true,
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

The event name is passed directly to the script instead of being read from the hook payload. This keeps each matcher tied to one notification type.

`async` prevents Claude Code from waiting for the network request to finish.

Restart Claude Code.

You can test the hook directly instead of waiting for a real notification.
```bash
echo "{\"cwd\":\"$PWD\",\"transcript_path\":\"\"}" | ~/.claude/hooks/cc_tg_notify.py idle
```

## When nothing arrives

The hook is designed not to break a Claude Code session.

If something goes wrong, it writes the error to `~/.claude/cc-tg-notify.log` and still exits successfully.

Check the log with
```bash
tail ~/.claude/cc-tg-notify.log
```

If the log is empty but no Telegram message arrives, the request probably reached the function. Check the function logs in Yandex Cloud.

- `403` means the secret in `~/.claude/cc-tg-notify.json` does not match `NOTIFY_SECRET`
- `500 not configured` means one of the required environment variables is missing or empty. The log line shows which one
- `502 telegram unavailable` means the function could not reach the Bot API
- When using a self-hosted server, a `403` inside that error usually means `TELEGRAM_API_TOKEN` is missing or incorrect
