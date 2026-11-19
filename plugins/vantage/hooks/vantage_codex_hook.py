#!/usr/bin/env python3
"""Send privacy-conscious Codex turn attribution to Vantage as an OTLP log.

This hook never reads or sends prompts, responses, transcripts, tool data, or
workspace files. It reads only the plugin-managed classifier and credentials.
Failures never block or continue the Codex turn. It intentionally uses only the
Python standard library.
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
PLUGIN_VERSION = "0.4.0"
USER_AGENT = f"vantage-codex-plugin/{PLUGIN_VERSION}"
EVENT_NAME = "codex.turn.attribution"
DEFAULT_ENDPOINT = "https://ingest.vantage.sh"
DEFAULT_CLASSIFIER_ENDPOINT = "https://api.vantage.sh/plugins/realtime_ai_classifier"
INGEST_ENDPOINT = f"{DEFAULT_ENDPOINT}/v1/logs"
MAX_CLASSIFIER_BYTES = 64 * 1024
CLASSIFIER_TIMEOUT = 2
TAG_SECONDS = 604_800
MAX_CLASSIFICATION_TAGS = 32
MAX_CLASSIFICATION_VALUE_LENGTH = 512
CLASSIFICATION_TAG_KEY = re.compile(r"\A[a-z][a-z0-9_.-]{0,63}\Z")
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
        parser.add_argument("--tag", action="append", default=[])
        parsed = vars(parser.parse_args(argv))
        session_id = (parsed.pop("session") or "").strip()
        if not session_id:
            raise ValueError("--session is required")

        raw_tags = parsed.pop("tag")
        options = {}
        for raw_tag in raw_tags:
            key, separator, value = raw_tag.partition("=")
            if not separator:
                raise ValueError("--tag must use key=value")
            add_classification_tag(options, key, value)
        if not options:
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
        ]
    ) + classification_attributes(skill)
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
        refresh_classifier(env, stderr=stderr)
        classifier = classifier_markdown_path(env)
        if session_id and classifier.is_file():
            print(attribution_context(session_id, classifier), file=stdout)
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


def classifier_directory(env):
    return codex_home(env) / "vantage"


def classifier_token_path(env):
    return classifier_directory(env) / "classifier_read_token"


def classifier_markdown_path(env):
    return classifier_directory(env) / "classifier.md"


def classifier_etag_path(env):
    return classifier_directory(env) / "classifier.etag"


def classifier_endpoint(env):
    return (
        str(env.get("VANTAGE_CLASSIFIER_ENDPOINT", "")).strip()
        or DEFAULT_CLASSIFIER_ENDPOINT
    )


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def refresh_classifier(env, stderr=sys.stderr):
    cached = classifier_markdown_path(env).is_file()
    try:
        try:
            token = classifier_token_path(env).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return cached
        if not token:
            return cached

        endpoint = classifier_endpoint(env)
        parsed = urllib.parse.urlparse(endpoint)
        if not https_or_loopback(parsed):
            raise ValueError(
                f"refusing to send the classifier credential over {parsed.scheme}"
            )

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        }
        try:
            etag = classifier_etag_path(env).read_text(encoding="utf-8").strip()
            if etag:
                headers["If-None-Match"] = etag
        except (OSError, UnicodeError):
            pass

        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        opener = urllib.request.build_opener(NoRedirectHandler)
        try:
            response = opener.open(request, timeout=CLASSIFIER_TIMEOUT)
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return cached
            raise

        with response:
            raw = response.read(MAX_CLASSIFIER_BYTES + 1)
            if len(raw) > MAX_CLASSIFIER_BYTES:
                raise ValueError("classifier response is too large")
            payload = json.loads(raw.decode("utf-8"))
            markdown = payload.get("classifier_markdown") if isinstance(payload, dict) else None
            if not isinstance(markdown, str) or not markdown.strip():
                raise ValueError("classifier response is empty")
            write_private_file(classifier_markdown_path(env), markdown.encode("utf-8"))
            new_etag = response.headers.get("ETag", "").strip()
            if new_etag:
                write_private_file(classifier_etag_path(env), new_etag.encode("utf-8"))
        return True
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(
            f"[vantage_codex_hook] classifier refresh failed: {type(error).__name__}",
            file=stderr,
        )
        return cached


def write_private_file(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def append_local(body, path):
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        lock_file(handle)
        handle.write(json.dumps(body, separators=(",", ":")) + "\n")


def endpoint_for(env):
    return str(env.get("VANTAGE_CODEX_INGEST_ENDPOINT", "")).strip() or DEFAULT_ENDPOINT


def attribution_context(session_id, classifier):
    command = f'"{sys.executable}" "{Path(__file__).resolve()}" tag --session {session_id}'
    return (
        f"Vantage session attribution command for this Codex session: {command}\n"
        f"Vantage classifier file for this session: {classifier}\n"
        "Use the vantage-session-attribution skill to read that classifier and "
        "append only the flags it directs."
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
        if not isinstance(entry, dict):
            return {}
        tagged_at = entry.get("at")
        if not isinstance(tagged_at, int) or not 0 <= int(time.time()) - tagged_at <= TAG_SECONDS:
            return {}
        return {
            key: value
            for key, value in entry.items()
            if key != "at" and valid_classification_tag(key, value)
        }
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def valid_classification_tag(key, value):
    return (
        isinstance(key, str)
        and CLASSIFICATION_TAG_KEY.fullmatch(key) is not None
        and isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_CLASSIFICATION_VALUE_LENGTH
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def add_classification_tag(tags, key, value):
    key = key.strip()
    value = value.strip()
    if not valid_classification_tag(key, value):
        raise ValueError("classification tags require a lowercase key and a non-empty value")
    if key not in tags and len(tags) >= MAX_CLASSIFICATION_TAGS:
        raise ValueError(f"at most {MAX_CLASSIFICATION_TAGS} classification tags are allowed")
    tags[key] = value


def classification_attributes(tags):
    return compact(
        [
            string_attribute(f"vantage.classification.{key}", value)
            for key, value in sorted(tags.items())
            if valid_classification_tag(key, value)
        ]
    )


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
            current.pop(session_id, None)
            current[session_id] = {**options, "at": now}
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
