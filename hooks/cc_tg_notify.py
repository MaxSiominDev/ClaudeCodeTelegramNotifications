#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "cc-tg-notify.json"
LOG_PATH = Path.home() / ".claude" / "cc-tg-notify.log"

EVENTS = ("idle", "permission")
PROMPT_LIMIT = 200
REQUEST_TIMEOUT = 5
GIT_TIMEOUT = 2
TASK_ID_PATTERN = re.compile(r"<task-id>([^<]+)</task-id>")


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in ("url", "secret") if not config.get(key)]
    if missing:
        raise ValueError("config is missing " + ", ".join(missing))
    return config


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def last_prompt(transcript_path):
    if not transcript_path:
        return None

    prompt = ""
    try:
        with open(transcript_path, encoding="utf-8") as transcript:
            for line in transcript:
                if '"last-prompt"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "last-prompt" and isinstance(record.get("lastPrompt"), str):
                    prompt = record["lastPrompt"]
    except (OSError, UnicodeDecodeError):
        return None

    if not prompt.strip():
        return None
    return truncate(" ".join(prompt.split()), PROMPT_LIMIT)


def as_dict(value):
    return value if isinstance(value, dict) else {}


def launched_agent(record):
    result = as_dict(record.get("toolUseResult"))
    if result.get("status") == "async_launched":
        return result.get("agentId")
    return None


def finished_agents(record):
    attachment = as_dict(record.get("attachment"))
    message = as_dict(record.get("message"))
    for body in (record.get("content"), attachment.get("prompt"), message.get("content")):
        if isinstance(body, str) and "<status>" in body:
            return TASK_ID_PATTERN.findall(body)
    return []


def running_agents(transcript_path):
    if not transcript_path:
        return set()

    launched = set()
    finished = set()
    try:
        with open(transcript_path, encoding="utf-8") as transcript:
            for line in transcript:
                if "async_launched" not in line and "<task-id>" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                agent = launched_agent(record)
                if agent:
                    launched.add(agent)
                finished.update(finished_agents(record))
    except (OSError, UnicodeDecodeError):
        return set()

    return launched - finished


def git_branch(cwd):
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True,
            check=False,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        return None
    return branch


def build_payload(event, hook_input):
    cwd = hook_input.get("cwd") or ""
    return {
        "event": event,
        "project": os.path.basename(cwd.rstrip("/")) or cwd,
        "branch": git_branch(cwd),
        "prompt": last_prompt(hook_input.get("transcript_path")),
    }


def send(config, payload):
    request = urllib.request.Request(
        config["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Notify-Secret": config["secret"],
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT):
        pass


def log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as logfile:
            logfile.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")  # noqa: DTZ005 - local log timestamp
    except OSError:
        pass


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event not in EVENTS:
        log(f"unknown event {event!r}, expected one of {', '.join(EVENTS)}")
        return 0

    try:
        hook_input = json.load(sys.stdin)
        if event == "idle":
            agents = running_agents(hook_input.get("transcript_path"))
            if agents:
                log(f"idle: suppressed, background agents running: {', '.join(sorted(agents))}")
                return 0
        send(load_config(CONFIG_PATH), build_payload(event, hook_input))
    except Exception as error:  # noqa: BLE001 - a hook must never break the session
        log(f"{event}: {type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
