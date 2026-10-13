# Claude Code Telegram notifications

Claude Code can finish a turn while you are looking somewhere else. This project sends you a Telegram message when that happens.

Claude Code already exposes the events we need through its `Notification` hook.

`idle_prompt` fires about a minute after a turn finishes if you have not typed anything since. `permission_prompt` fires when a tool is waiting for approval, and also when Claude Code shows the dialog asking you to choose between answer options. The payload is the same in both cases, so the hook looks into the session transcript: if the pending tool call is `AskUserQuestion`, the event is forwarded as `question` instead of `permission`.

This project forwards these events to Telegram through a Yandex Cloud Function.

```text
Claude Code -> hooks/cc_tg_notify.py -> Cloud Function -> Telegram
```

The hook reads the local session transcript and sends four fields.

- Event
- Project
- Git branch
- Last prompt you entered

The function turns them into a short message.

```text
Claude Code · my-project · feature/notifications
Finished responding, waiting for you

> add tests
```

The notification is meant to tell you which session needs attention. The Claude Code response itself never leaves your machine.

Messages are sent through `api.telegram.org` by default.

If Telegram is not reachable from the cloud environment, you can point the function to a self-hosted [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) server instead. Both use the same Bot API, so switching only requires environment variables.

## Setup

See [docs/setup.md](docs/setup.md).

It covers creating the Telegram bot, deploying the Cloud Function, and installing the local hook.

## Tests

```bash
python3 -m unittest discover -s tests
```

The project has no external dependencies and the tests do not use the network. Both the hook and the Cloud Function use only the Python standard library.

## License

MIT