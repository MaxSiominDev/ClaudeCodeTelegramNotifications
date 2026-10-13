import base64
import hmac
import json
import os
import urllib.parse
import urllib.request

MESSAGE_LIMIT = 4096
TELEGRAM_TIMEOUT = 10
SECRET_HEADER = "x-notify-secret"
REQUIRED_SETTINGS = ("NOTIFY_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

DEFAULT_API_URL = "https://api.telegram.org"
API_TOKEN_HEADER = "X-Api-Token"

EVENT_LINES = {
    "idle": "Finished responding, waiting for you",
    "permission": "Waiting for permission to use a tool",
    "question": "Needs your answer",
}


def settings():
    values = {name: os.environ.get(name, "") for name in REQUIRED_SETTINGS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("missing or empty environment variables: " + ", ".join(missing))

    api_url = os.environ.get("TELEGRAM_API_URL") or DEFAULT_API_URL
    if "://" not in api_url:
        api_url = "https://" + api_url
    values["TELEGRAM_API_URL"] = api_url.rstrip("/")
    values["TELEGRAM_API_TOKEN"] = os.environ.get("TELEGRAM_API_TOKEN", "")
    return values


def response(status, message):
    return {"statusCode": status, "body": message}


def header(headers, name):
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value
    return None


def request_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def build_message(payload):
    title = "Claude Code · " + payload["project"]
    if payload.get("branch"):
        title += " · " + payload["branch"]

    lines = [title, EVENT_LINES[payload["event"]]]
    if payload.get("prompt"):
        lines += ["", "> " + payload["prompt"]]

    text = "\n".join(lines)
    if len(text) <= MESSAGE_LIMIT:
        return text
    return text[: MESSAGE_LIMIT - 3].rstrip() + "..."


def send_to_telegram(config, text):
    data = urllib.parse.urlencode(
        {"chat_id": config["TELEGRAM_CHAT_ID"], "text": text}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config['TELEGRAM_API_URL']}/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data=data,
    )
    if config["TELEGRAM_API_TOKEN"]:
        request.add_header(API_TOKEN_HEADER, config["TELEGRAM_API_TOKEN"])

    with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT):
        pass


def secret_matches(headers, secret):
    given = header(headers, SECRET_HEADER) or ""
    return hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))


def handler(event, context):
    try:
        config = settings()
    except RuntimeError as error:
        print(error)
        return response(500, "not configured")

    if not secret_matches(event.get("headers"), config["NOTIFY_SECRET"]):
        return response(403, "forbidden")

    try:
        payload = request_body(event)
        if payload["event"] not in EVENT_LINES:
            raise ValueError("unknown event " + repr(payload["event"]))
        if not payload.get("project"):
            raise ValueError("project is empty")
        text = build_message(payload)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as error:
        return response(400, f"bad request: {error}")

    try:
        send_to_telegram(config, text)
    except OSError as error:
        print(f"telegram request failed: {error}")
        return response(502, "telegram unavailable")

    return response(200, "sent")
