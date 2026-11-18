#!/usr/bin/env python3
"""Send privacy-conscious Codex turn attribution to Vantage as an OTLP log.

This hook never reads or sends prompts, responses, transcripts, tool data, or
local paths. Failures never block or continue the Codex turn. It intentionally
uses only the Python standard library.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


SCHEMA_VERSION = "1"
PLUGIN_VERSION = "0.3.1"
USER_AGENT = f"vantage-codex-plugin/{PLUGIN_VERSION}"
EVENT_NAME = "codex.turn.attribution"
PURPOSES = ("research", "development", "design", "unclassified")
SOURCES = ("explicit", "inferred", "manual")
DEFAULT_ENDPOINT = "https://ingest.vantage.sh"
INGEST_ENDPOINT = f"{DEFAULT_ENDPOINT}/v1/logs"
TAG_SECONDS = 604_800
SKILL_TAGS = ("purpose", "project", "source")
GITHUB_REPOSITORY = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
TOKEN = re.compile(r'authorization\s*=\s*"Bearer ([A-Za-z0-9._~+/=\-]{8,512})"')
MANAGED_BLOCK = re.compile(
    r"# BEGIN Vantage telemetry.*?# END Vantage telemetry", re.DOTALL
)
OTEL_SECTION = re.compile(
    r"^\s*\[\s*otel\s*\]\s*(?:#.*)?$.*?(?=^\s*\[|\Z)",
    re.MULTILINE | re.DOTALL,
)


def run(
    stdin=sys.stdin,
    env=os.environ,
    stdout=sys.stdout,
    stderr=sys.stderr,
    now_ns=None,
):
    try:
        payload = json.loads(stdin.read() or "")
        if isinstance(payload, dict):
            handle(payload, env=env, stdout=stdout, stderr=stderr, now_ns=now_ns)
    except json.JSONDecodeError:
        return
    except Exception as error:  # Hooks must never interrupt the Codex turn.
        print(
            f"[vantage_codex_hook] {type(error).__name__}: {error}",
            file=stderr,
        )


def tag(
    argv,
    env=os.environ,
    stdout=sys.stdout,
    stderr=sys.stderr,
):
    try:
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--session")
        for name in SKILL_TAGS:
            parser.add_argument(f"--{name}")
        options = {
            name: value.strip()
            for name, value in vars(parser.parse_args(argv)).items()
            if value is not None and value.strip()
        }

        session_id = options.pop("session", "")
        if not session_id:
            raise ValueError("--session is required")
        if options.get("purpose") not in (None, *PURPOSES):
            raise ValueError(f"--purpose must be one of {', '.join(PURPOSES)}")
        if options.get("source") not in (None, *SOURCES):
            raise ValueError(f"--source must be one of {', '.join(SOURCES)}")
        if not set(options).difference({"source"}):
            raise ValueError("give at least one tag")

        if not update_tags(env, session_id, options):
            raise ValueError("could not save tags")

        print(
            f"Saved tags for session {session_id}. "
            "They are sent with this turn's record.",
            file=stdout,
        )
        return 0
    except (Exception, SystemExit) as error:
        message = "invalid tag arguments" if isinstance(error, SystemExit) else str(error)
        print(f"[vantage_codex_hook] {message}", file=stderr)
        return 1


def build_request(
    payload,
    env=os.environ,
    now_ns=None,
):
    if payload.get("hook_event_name") != "Stop":
        return None

    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return None

    turn_id = str(payload.get("turn_id") or "").strip()
    git = workspace_tags(str(payload.get("cwd") or "")) or {}
    skill = skill_tags(env, session_id)
    attributes = compact(
        [
            string_attribute("codex.turn.id", turn_id),
            string_attribute("vcs.ref.head.name", git.get("branch")),
            string_attribute("vantage.purpose", skill.get("purpose")),
            string_attribute("vantage.project", skill.get("project")),
            string_attribute("vantage.tag_source", skill.get("source")),
        ]
    )
    return envelope(
        EVENT_NAME,
        session_id,
        git.get("repository"),
        attributes,
        now_ns if now_ns is not None else time.time_ns(),
        "codex_hook",
    )


def deliver(
    body,
    *,
    token,
    endpoint,
    stderr=sys.stderr,
):
    url = urllib.parse.urljoin(endpoint, "/v1/logs")
    parsed = urllib.parse.urlparse(url)
    if not https_or_loopback(parsed):
        raise ValueError(f"refusing to send the ingest token over {parsed.scheme}")

    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        print(f"[vantage_codex_hook] ingest responded {error.code}", file=stderr)
        return error

    status = getattr(response, "status", 200)
    if not 200 <= status < 300:
        print(f"[vantage_codex_hook] ingest responded {status}", file=stderr)
    return response


def handle(
    payload,
    *,
    env,
    stdout,
    stderr,
    now_ns,
):
    events_path = str(env.get("VANTAGE_CODEX_EVENTS_PATH", "")).strip()
    if payload.get("hook_event_name") == "SessionStart":
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            print(attribution_context(session_id), file=stdout)
        return

    token = ingest_token(env)
    if not events_path and not token:
        return

    body = build_request(payload, env=env, now_ns=now_ns)
    if body is None:
        return
    if events_path:
        append_local(body, events_path)
        return

    deliver(body, token=token, endpoint=endpoint_for(env), stderr=stderr)


def ingest_token(env):
    try:
        config = (codex_home(env) / "config.toml").read_text(encoding="utf-8")
        managed_match = MANAGED_BLOCK.search(config)
        if managed_match:
            token_match = TOKEN.search(managed_match.group(0))
            if token_match:
                return token_match.group(1)

        section_match = OTEL_SECTION.search(config)
        section = section_match.group(0) if section_match else ""
        exporter = next(
            (
                line
                for line in section.splitlines()
                if not line.lstrip().startswith("#")
                and re.match(r"^\s*exporter\s*=", line)
                and INGEST_ENDPOINT in line
            ),
            None,
        )
        if exporter is None:
            exporter = next(
                (
                    line
                    for line in config.splitlines()
                    if not line.lstrip().startswith("#")
                    and re.match(r"^\s*otel\.exporter\s*=", line)
                    and INGEST_ENDPOINT in line
                ),
                "",
            )
        token_match = TOKEN.search(exporter)
        return token_match.group(1) if token_match else ""
    except (OSError, UnicodeError):
        return ""


def codex_home(env):
    configured = str(env.get("CODEX_HOME", "")).strip()
    return Path(configured or "~/.codex").expanduser()


def append_local(body, path):
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        lock_file(handle)
        handle.write(json.dumps(body, separators=(",", ":")) + "\n")


def endpoint_for(env):
    return str(env.get("VANTAGE_CODEX_INGEST_ENDPOINT", "")).strip() or DEFAULT_ENDPOINT


def attribution_context(session_id):
    command = f'"{sys.executable}" "{Path(__file__).resolve()}" tag --session {session_id}'
    return (
        f"Vantage session attribution command for this Codex session: {command}\n"
        "Use it only as the vantage-session-attribution skill directs, "
        "appending that skill's flags."
    )


def envelope(
    event_name,
    session_id,
    repository,
    attributes,
    now_ns,
    source,
):
    timestamp = str(now_ns)
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": compact(
                        [
                            string_attribute("service.name", "codex"),
                            string_attribute("vcs.repository.name", repository),
                            string_attribute("vantage.source", source),
                            string_attribute("vantage.schema_version", SCHEMA_VERSION),
                        ]
                    )
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "vantage.codex_hooks",
                            "version": SCHEMA_VERSION,
                        },
                        "logRecords": [
                            {
                                "timeUnixNano": timestamp,
                                "observedTimeUnixNano": timestamp,
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "eventName": event_name,
                                "body": {"stringValue": event_name},
                                "attributes": compact(
                                    [
                                        string_attribute("event.name", event_name),
                                        string_attribute("vantage.event_id", str(uuid.uuid4())),
                                        string_attribute("conversation.id", session_id),
                                    ]
                                )
                                + attributes,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def workspace_tags(root):
    if not root or not Path(root).is_dir():
        return None

    top = git_output(["git", "-C", root, "rev-parse", "--show-toplevel"])
    if top is None:
        return None
    branch = git_output(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        branch = None
    remote = git_output(["git", "-C", root, "remote", "get-url", "origin"])
    repository = github_repository(remote)
    tags = {
        "repository": repository or Path(top).name,
        "branch": branch,
    }
    return tags


def git_output(argv):
    stdout, ok = capture(argv)
    output = stdout.strip()
    return output if ok and output else None


def github_repository(remote):
    match = GITHUB_REPOSITORY.search(remote or "")
    return match.group(1) if match else None


def skill_tags(env, session_id):
    try:
        current = json.loads((state_dir(env) / "tags.json").read_text(encoding="utf-8"))
        entry = current.get(session_id) if isinstance(current, dict) else None
        return entry if isinstance(entry, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def update_tags(
    env, session_id, options
):
    try:
        directory = state_dir(env)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / "tags.json"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            lock_file(handle)
            raw = handle.read()
            current = json.loads(raw) if raw.strip() else {}
            if not isinstance(current, dict):
                current = {}

            now = int(time.time())
            current = {
                key: entry
                for key, entry in current.items()
                if isinstance(entry, dict)
                and isinstance(entry.get("at"), int)
                and 0 <= now - entry["at"] <= TAG_SECONDS
            }
            entry = current.pop(session_id, {})
            if not isinstance(entry, dict):
                entry = {}
            current[session_id] = {**entry, **options, "at": now}
            while len(current) > 200 and next(iter(current)) != session_id:
                del current[next(iter(current))]

            handle.seek(0)
            handle.truncate()
            json.dump(current, handle, separators=(",", ":"))
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def state_dir(env):
    configured = str(env.get("VANTAGE_CODEX_HOOK_STATE_DIR", ""))
    return Path(configured) if configured else Path(tempfile.gettempdir()) / "vantage-codex-hook"


def capture(argv):
    try:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            argv,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.stdout, result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return "", False


def lock_file(handle):
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        return


def https_or_loopback(parsed):
    return parsed.scheme == "https" or (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )


def string_attribute(key, value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return {"key": key, "value": {"stringValue": text}}


def compact(values):
    return [value for value in values if value is not None]


if __name__ == "__main__":
    if sys.argv[1:2] == ["tag"]:
        raise SystemExit(tag(sys.argv[2:]))
    run()
